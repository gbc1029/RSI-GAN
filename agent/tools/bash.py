"""One-shot bash tool for the task agent.

Replaces the former persistent interactive-shell design (``/bin/bash -i`` +
appended ``; echo '<<exit>>'`` sentinel + 0.2s stdout polling).  The field
audit of run small_1030_095410 (docs/18) showed that protocol:

* broke valid heredoc-terminated commands -- the glue joined the terminator
  line, so bash collected the heredoc forever (4 of the 11 timeouts);
* leaked one orphan ``bash -i`` per call (``stop()`` was never called);
* sent SIGTERM to the ``sh -c`` wrapper instead of the shell's process group;
* silently truncated output whose text contained the fixed sentinel;
* discarded the collected stderr on timeout, destroying all diagnostics.

Design of this version:

* one-shot ``bash -c`` subprocess per call -- process exit IS the completion
  signal; no sentinel, no session, no cross-call state;
* ``stdin=DEVNULL`` -- ``read``-style commands get EOF instead of hanging;
* ``start_new_session=True`` -- the command runs in its own process group,
  so timeout escalation via ``killpg`` reaches the whole tree;
* non-interactive bash: history expansion is off (a ``!`` in a pattern is
  safe);
* a non-zero exit code is reported as a trailing ``[exit N]`` marker;
* timeout keeps the collected stdout/stderr tails in the error text.
"""

import os
import signal
import subprocess

BASH_TIMEOUT_S = 120.0   # tool-level budget; <= dispatch C1 default (600)
KILL_GRACE_S = 5.0       # SIGTERM -> SIGKILL escalation window
TAIL_CHARS = 2000        # stdout/stderr tails kept in the timeout error


def tool_info():
    return {
        "name": "bash",
        "description": """Run commands in a bash shell
* The command runs as a one-shot non-interactive bash process (bash -c).
* The contents of the "command" parameter does NOT need to be XML-escaped.
* Each call starts fresh: no state (cwd, variables, functions) persists
between calls -- chain dependent commands with && or ; inside one command.
* A non-zero exit code is reported as a trailing "[exit N]" marker.
* Long-running commands are killed after 120 seconds; do not start servers
and do not wait on stdin.
* Please avoid commands that may produce a very large amount of output.
* To inspect a particular line range of a file, e.g. lines 10-25, try 'sed -n 10,25p /path/to/the/file'.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to run."
                }
            },
            "required": ["command"]
        }
    }


def _tail(label, text):
    """Tail of one captured stream for the timeout error text (defect 6)."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > TAIL_CHARS:
        text = "...[truncated]..." + text[-TAIL_CHARS:]
    return f"\nlast {label}: {text}"


def _killpg(proc, sig):
    """Signal the command's whole process group (bash + its children)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def _format(out, err, returncode):
    """Keep the historical output contract: stdout + '\\nError:\\n' + stderr."""
    result = (out or "").strip()
    if returncode:
        result += f"\n[exit {returncode}]"
    err = (err or "").strip()
    if err:
        result += "\nError:\n" + err
    return result.strip()


def tool_function(command):
    """Execute `command` as a one-shot `bash -c` process and return its output."""
    proc = subprocess.Popen(
        ["/bin/bash", "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        env=os.environ.copy(),
    )
    try:
        out, err = proc.communicate(timeout=BASH_TIMEOUT_S)
        return _format(out, err, proc.returncode)
    except subprocess.TimeoutExpired:
        # Escalate on the WHOLE process group: TERM -> grace -> KILL.
        _killpg(proc, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)
            out, err = proc.communicate()
        msg = (f"Error: Timed out: the command did not finish within "
               f"{BASH_TIMEOUT_S:g} seconds and was killed.")
        msg += _tail("stdout", out)
        msg += _tail("stderr", err)
        return msg


if __name__ == "__main__":
    # Example usage
    import sys

    if len(sys.argv) < 2:
        print("Usage: python bash.py '<command>'")
    else:
        input_command = ' '.join(sys.argv[1:])
        print(tool_function(input_command))
