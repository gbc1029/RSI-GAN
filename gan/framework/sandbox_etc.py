"""Create and validate the minimal ``/etc`` exposed to secure GAN workers."""
from __future__ import annotations

import glob
import os
import re
import shutil
import stat
from typing import Dict, Iterable, Optional, Tuple


SANDBOX_ETC_MARKER = ".rsi-gan-sandbox"
SANDBOX_ETC_MARKER_CONTENT = "RSI_GAN_SANDBOX_ETC_V1\n"
SANDBOX_HOSTNAME = "rsi-gan-sandbox"

_REQUIRED_FILES = {
    SANDBOX_ETC_MARKER,
    "group",
    "hostname",
    "hosts",
    "localtime",
    "nsswitch.conf",
    "passwd",
    "timezone",
}
_OPTIONAL_FILES = {
    "ld.so.cache",
    "ssl/openssl.cnf",
    "ssl/certs/ca-certificates.crt",
}
_ALLOWED_DIRS = {"ssl", "ssl/certs"}
_MAX_RUNTIME_FILE_BYTES = 16 * 1024 * 1024

# Debian/Ubuntu ship OpenJDK's security config as /etc-managed real files while
# the JDK tree symlinks into them (conf/security/java.security ->
# /etc/java-N-openjdk/security/java.security). A synthetic /etc without that
# subtree leaves every symlink dangling and Java unusable inside the sandbox
# ("Error loading java.security file"; field audit small_1030_095410). The
# subtree is projected as dereferenced real files when the host has it.
_JAVA_ETC_GLOB = "/etc/java-*-openjdk"
_JAVA_ETC_RE = re.compile(r"java-\d+-openjdk(/.*)?$")
# Debian alternatives registry (symlink entries only; see create_sandbox_etc)
_ALT_RE = re.compile(r"alternatives(/.*)?$")


def _trusted_file_bytes(path: str, allowed_roots: Iterable[str]) -> Optional[bytes]:
    if not os.path.exists(path):
        return None
    real = os.path.realpath(path)
    roots = tuple(os.path.realpath(root) for root in allowed_roots)
    if not any(os.path.commonpath([real, root]) == root for root in roots):
        raise RuntimeError(f"sandbox /etc source resolves outside its trusted root: {path}")

    current = os.path.sep
    for component in real.strip(os.path.sep).split(os.path.sep)[:-1]:
        current = os.path.join(current, component)
        info = os.lstat(current)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise RuntimeError(f"sandbox /etc source has an untrusted parent: {current}")

    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022
                or info.st_size > _MAX_RUNTIME_FILE_BYTES):
            raise RuntimeError(f"sandbox /etc source is not a trusted regular file: {path}")
        data = bytearray()
        while len(data) <= _MAX_RUNTIME_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024,
                                    _MAX_RUNTIME_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_RUNTIME_FILE_BYTES:
            raise RuntimeError(f"sandbox /etc source is too large: {path}")
        return bytes(data)
    finally:
        os.close(fd)


def _first_trusted_file(candidates: Iterable[str],
                        allowed_roots: Iterable[str]) -> Optional[bytes]:
    for path in candidates:
        data = _trusted_file_bytes(path, allowed_roots)
        if data is not None:
            return data
    return None


def _write_file(root: str, relative: str, data: bytes) -> None:
    target = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o444)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while creating sandbox /etc")
            view = view[written:]
        os.fchmod(fd, 0o444)
    finally:
        os.close(fd)


def create_sandbox_etc(
    parent: str,
    *,
    owner: Tuple[int, int],
    task: Tuple[int, int],
    planner: Tuple[int, int],
    evaluator: Tuple[int, int],
) -> str:
    """Create one immutable, credential-free runtime ``/etc`` projection."""
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("sandbox /etc projection must be created by root")
    parent = os.path.realpath(parent)
    parent_info = os.lstat(parent)
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0
            or parent_info.st_mode & 0o022):
        raise RuntimeError("sandbox /etc projection requires a private root-owned parent")
    root = os.path.join(parent, "runtime-etc")
    if os.path.lexists(root):
        raise RuntimeError("sandbox /etc projection already exists")
    os.mkdir(root, 0o700)
    try:
        identities = (
            ("root", 0, 0),
            ("rsi_gan_owner", int(owner[0]), int(owner[1])),
            ("rsi_gan_task", int(task[0]), int(task[1])),
            ("rsi_gan_planner", int(planner[0]), int(planner[1])),
            ("rsi_gan_evaluator", int(evaluator[0]), int(evaluator[1])),
        )
        uids = [uid for _name, uid, _gid in identities]
        if any(uid < 0 or gid < 0 for _name, uid, gid in identities):
            raise RuntimeError("sandbox /etc identities must be non-negative")
        if len(set(uids)) != len(uids):
            raise RuntimeError("sandbox /etc identities must use distinct UIDs")

        passwd = "".join(
            f"{name}:x:{uid}:{gid}::/tmp:/bin/false\n"
            for name, uid, gid in identities
        ).encode("ascii")
        groups: Dict[int, str] = {}
        for name, _uid, gid in identities:
            groups.setdefault(gid, name)
        group = "".join(
            f"{name}:x:{gid}:\n" for gid, name in groups.items()
        ).encode("ascii")
        generated = {
            SANDBOX_ETC_MARKER: SANDBOX_ETC_MARKER_CONTENT.encode("ascii"),
            "passwd": passwd,
            "group": group,
            "nsswitch.conf": (
                b"passwd: files\n"
                b"group: files\n"
                b"shadow: files\n"
                b"hosts: files\n"
                b"networks: files\n"
            ),
            "hosts": (
                b"127.0.0.1 localhost rsi-gan-sandbox\n"
                b"::1 localhost ip6-localhost ip6-loopback rsi-gan-sandbox\n"
            ),
            "hostname": (SANDBOX_HOSTNAME + "\n").encode("ascii"),
            "timezone": b"Etc/UTC\n",
        }
        for relative, data in generated.items():
            _write_file(root, relative, data)

        utc = _first_trusted_file(
            ("/usr/share/zoneinfo/Etc/UTC", "/usr/share/zoneinfo/UTC"),
            ("/usr/share/zoneinfo",),
        )
        if utc is None:
            raise RuntimeError("sandbox /etc requires a trusted UTC zoneinfo file")
        _write_file(root, "localtime", utc)

        optional_sources = (
            ("ld.so.cache", ("/etc/ld.so.cache",), ("/etc",)),
            (
                "ssl/openssl.cnf",
                ("/etc/ssl/openssl.cnf", "/etc/pki/tls/openssl.cnf"),
                ("/etc", "/usr/lib/ssl", "/usr/share"),
            ),
            (
                "ssl/certs/ca-certificates.crt",
                (
                    "/etc/ssl/certs/ca-certificates.crt",
                    "/etc/pki/tls/certs/ca-bundle.crt",
                    "/etc/ssl/cert.pem",
                ),
                ("/etc", "/usr/share"),
            ),
        )
        for relative, candidates, allowed_roots in optional_sources:
            data = _first_trusted_file(candidates, allowed_roots)
            if data is not None:
                _write_file(root, relative, data)

        # OpenJDK security config (optional projection): the /etc side of the
        # JDK's conf/security symlinks. Files are dereferenced, trust-checked
        # against /etc, and re-created 0444 root-owned; a source whose target
        # escapes /etc (or fails the trust walk) is SKIPPED, never followed —
        # an optional projection must not fail the sandbox, and the validate
        # gate below still rejects anything this loop should not have written.
        for java_dir in sorted(glob.glob(_JAVA_ETC_GLOB)):
            if os.path.islink(java_dir) or not os.path.isdir(java_dir):
                continue
            for current, _dirs, files in os.walk(java_dir, followlinks=True):
                for name in files:
                    real = os.path.realpath(os.path.join(current, name))
                    if not real.startswith("/etc/"):
                        continue
                    try:
                        data = _trusted_file_bytes(real, ("/etc",))
                    except RuntimeError:
                        continue
                    if data is None:
                        continue
                    relative = os.path.relpath(real, "/etc").replace(os.sep, "/")
                    _write_file(root, relative, data)

        # Debian alternatives registry (optional projection): /usr/bin/cc ->
        # /etc/alternatives/cc -> /usr/bin/gcc — rustc's default linker
        # driver. Same failure class as the JDK block above: the synthetic
        # /etc without this subtree leaves the toolchain's symlinks dangling
        # ("linker 'cc' not found"; field audit small_1030_095410, rust
        # exercises). The SYMLINKS are re-created verbatim — the sandbox /usr
        # is the host's read-only view, so the original targets resolve there.
        # Only entries whose target exists (host view) are reproduced; a
        # non-symlink or dangling entry is skipped, never followed.
        alt_src = "/etc/alternatives"
        if os.path.isdir(alt_src) and not os.path.islink(alt_src):
            alt_root = os.path.join(root, "alternatives")
            os.makedirs(alt_root, exist_ok=True)
            for name in sorted(os.listdir(alt_src)):
                entry = os.path.join(alt_src, name)
                if not os.path.islink(entry):
                    continue
                if os.path.isdir(entry):
                    continue  # symlink-to-directory: out of scope for a flat registry
                target = os.readlink(entry)
                if not os.path.exists(entry):
                    continue  # dangling on the host: nothing to provide
                os.symlink(target, os.path.join(alt_root, name))

        for current, directories, _files in os.walk(root, topdown=False):
            for name in directories:
                os.chmod(os.path.join(current, name), 0o555)
        os.chmod(root, 0o555)
        return validate_sandbox_etc(root)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def validate_sandbox_etc(path: str) -> str:
    """Return the canonical projection path, or reject an unsafe projection."""
    absolute = os.path.abspath(path)
    real = os.path.realpath(absolute)
    if absolute != real:
        raise RuntimeError("sandbox /etc projection path must not be a symlink")
    root_info = os.lstat(real)
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != 0
            or stat.S_IMODE(root_info.st_mode) != 0o555):
        raise RuntimeError("sandbox /etc projection root has unsafe ownership or mode")

    observed_files = set()
    observed_dirs = set()
    for current, directories, files in os.walk(real, followlinks=False):
        relative_root = os.path.relpath(current, real)
        relative_root = "" if relative_root == "." else relative_root.replace(os.sep, "/")
        for name in directories:
            relative = "/".join(part for part in (relative_root, name) if part)
            info = os.lstat(os.path.join(current, name))
            allowed_dir = (relative in _ALLOWED_DIRS
                           or bool(_JAVA_ETC_RE.match(relative))
                           or bool(_ALT_RE.match(relative)))
            if (not allowed_dir or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o555):
                raise RuntimeError(f"sandbox /etc contains an unsafe directory: {relative}")
            observed_dirs.add(relative)
        for name in files:
            relative = "/".join(part for part in (relative_root, name) if part)
            info = os.lstat(os.path.join(current, name))
            allowed_file = (relative in _REQUIRED_FILES | _OPTIONAL_FILES
                            or bool(_JAVA_ETC_RE.match(relative))
                            or bool(_ALT_RE.match(relative)))
            if _ALT_RE.match(relative):
                # alternatives entries are re-created SYMLINKS (their targets
                # live in the sandbox's read-only /usr view); a regular file
                # here means the projection was tampered with
                if (not stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                        or info.st_nlink != 1):
                    raise RuntimeError(
                        f"sandbox /etc contains an unsafe alternatives entry: {relative}")
            elif (not allowed_file or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o444 or info.st_nlink != 1):
                raise RuntimeError(f"sandbox /etc contains an unsafe file: {relative}")
            observed_files.add(relative)
    if not _REQUIRED_FILES <= observed_files:
        missing = sorted(_REQUIRED_FILES - observed_files)
        raise RuntimeError(f"sandbox /etc projection is incomplete: {missing}")
    extra_dirs = observed_dirs - _ALLOWED_DIRS
    if any(not (_JAVA_ETC_RE.match(d) or _ALT_RE.match(d)) for d in extra_dirs):
        unexpected = sorted(d for d in extra_dirs
                            if not (_JAVA_ETC_RE.match(d) or _ALT_RE.match(d)))
        raise RuntimeError(f"sandbox /etc projection contains unexpected directories: {unexpected}")

    marker = os.path.join(real, SANDBOX_ETC_MARKER)
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        content = os.read(fd, len(SANDBOX_ETC_MARKER_CONTENT) + 1)
    finally:
        os.close(fd)
    if content != SANDBOX_ETC_MARKER_CONTENT.encode("ascii"):
        raise RuntimeError("sandbox /etc projection marker is invalid")
    return real
