"""Minimal TaskAgent entry point executed inside the per-question sandbox."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import socket


_TASK_ENV_NAMES = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "GAN_TASK_DESIGN", "GAN_TASK_TOOLS_DIR", "GAN_TOOLS_LOAD_REPORT",
    "GAN_TASK_KNOWLEDGE_DIR", "GAN_TASK_BRIEF",
}


def _task_child_env(base_env):
    return {key: value for key, value in base_env.items()
            if key in _TASK_ENV_NAMES or key.startswith("LC_")}


class _LocalRelay:
    def __init__(self, unix_path):
        self.unix_path = unix_path
        self.listener = None
        self.port = 0

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = int(self.listener.getsockname()[1])
        threading.Thread(target=self._serve, daemon=True).start()
        return self.port

    def _serve(self):
        while self.listener is not None:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._bridge, args=(client,), daemon=True).start()

    def _bridge(self, client):
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.settimeout(10)
            upstream.connect(self.unix_path)
            threads = []
            for source, target in ((client, upstream), (upstream, client)):
                thread = threading.Thread(target=self._copy, args=(source, target),
                                          daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
        except OSError:
            pass
        finally:
            client.close()

    @staticmethod
    def _copy(source, target):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                target.sendall(data)
        except OSError:
            return

    def close(self):
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.close()


def _load_task_agent(agent_path: str):
    if agent_path.endswith(".py") or os.path.exists(agent_path):
        spec = importlib.util.spec_from_file_location(
            "sandboxed_task_agent", os.path.abspath(agent_path),
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load TaskAgent from {agent_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(agent_path)
    if not hasattr(module, "TaskAgent"):
        raise AttributeError(f"No TaskAgent found in {agent_path}")
    return module.TaskAgent


def _write_json_fd(fd: int, payload) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _run_agent_child(result_fd: int) -> None:
    if "GAN_DATASET_ROOT" in os.environ:
        raise RuntimeError("GAN_DATASET_ROOT must not enter the TaskAgent sandbox")

    payload = json.load(sys.stdin)
    TaskAgent = _load_task_agent(payload["agent_path"])
    agent = TaskAgent(
        model=payload["model"],
        chat_history_file=payload["trajectory_path"],
    )
    prediction, _ = agent.forward(payload["inputs"])
    _write_json_fd(result_fd, {"prediction": prediction})


def _run_trusted_worker() -> None:
    if "GAN_DATASET_ROOT" in os.environ:
        raise RuntimeError("GAN_DATASET_ROOT must not enter the TaskAgent sandbox")

    payload = json.load(sys.stdin)
    parent_result_fd = int(payload.pop("result_fd"))
    relay = None

    try:
        child_env = _task_child_env(os.environ)
        proxy_socket = payload.get("proxy_socket")
        proxy_token = payload.get("proxy_token")
        if proxy_socket:
            if not proxy_token:
                raise RuntimeError("Task proxy socket requires a scoped token")
            relay = _LocalRelay(proxy_socket)
            port = relay.start()
            child_env["OPENAI_API_BASE"] = f"http://127.0.0.1:{port}/v1"
            child_env["OPENAI_API_KEY"] = str(proxy_token)
        with tempfile.TemporaryFile(mode="w+b") as child_result_file:
            child_result_fd = child_result_file.fileno()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "domains.task_worker",
                    "--agent-child",
                    str(child_result_fd),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                close_fds=True,
                pass_fds=(child_result_fd,),
            )
            child_stdout, child_stderr = child.communicate(
                json.dumps(payload, ensure_ascii=False, default=str)
            )

            if child_stdout:
                sys.stdout.write(child_stdout)
                sys.stdout.flush()
            if child_stderr:
                sys.stderr.write(child_stderr)
                sys.stderr.flush()
            if child.returncode != 0:
                raise RuntimeError(f"TaskAgent child failed (rc={child.returncode})")

            child_result_file.seek(0)
            raw_result = child_result_file.read()

        if not raw_result:
            raise RuntimeError("TaskAgent child returned no result")
        result = json.loads(raw_result.decode("utf-8"))
        if not isinstance(result, dict) or set(result) != {"prediction"}:
            raise RuntimeError("TaskAgent child returned an invalid result object")
        _write_json_fd(parent_result_fd, result)
    finally:
        if relay is not None:
            relay.close()
        try:
            os.close(parent_result_fd)
        except OSError:
            pass


def main() -> None:
    if "--agent-child" in sys.argv:
        pos = sys.argv.index("--agent-child") + 1
        if pos >= len(sys.argv):
            raise ValueError("--agent-child requires a result fd")
        _run_agent_child(int(sys.argv[pos]))
        return
    _run_trusted_worker()


if __name__ == "__main__":
    main()
