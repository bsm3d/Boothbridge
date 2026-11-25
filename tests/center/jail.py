# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Audit hook that keeps a test process inside its temporary folder and blocks outside writes, listings and network use.

# An audit hook sees every Python level file write, directory listing, process start and network call, whatever code makes it.
# Writes outside the work folder, listings of folders outside the install, other programs than this Python and network use raise PermissionError.
# Qt's C++ file access is not seen by the hook, so monkey.py also stubs the dialogs and the drive code.

from __future__ import annotations

import contextlib
import os
import site
import sys
import tempfile
import threading

_LOCK = threading.Lock()
_state: dict = {"root": "", "reads": (), "off": 0, "blocked": [], "python": ()}
_installed = False

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

def norm(path) -> str:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def blocked() -> list[str]:
    with _LOCK:
        return list(_state["blocked"])


def _deny(event: str, what):
    text = f"{event}: {what}"
    with _LOCK:
        if len(_state["blocked"]) < 200:
            _state["blocked"].append(text)
    raise PermissionError(f"test jail blocked {text}")


# the test tools themselves write their results outside the work folder
@contextlib.contextmanager
def trusted():
    with _LOCK:
        _state["off"] += 1
    try:
        yield
    finally:
        with _LOCK:
            _state["off"] -= 1


def _writable(path) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return True
    return inside(norm(path), _state["root"])


# ----------------------------------------------------------
def _readable(path) -> bool:
    if path is None:
        path = "."
    if not isinstance(path, (str, bytes, os.PathLike)):
        return True
    full = norm(path)
    return any(inside(full, root) for root in _state["reads"])


def _check_open(args):
    path, _mode, flags = args
    if isinstance(flags, int) and flags & WRITE_FLAGS and not _writable(path):
        _deny("write", path)


def _check_write(args):
    for path in args[:1]:
        if not _writable(path):
            _deny("write", path)


def _check_two(args):
    for path in args[:2]:
        if not _writable(path):
            _deny("write", path)


def _check_dest(args):
    if len(args) > 1 and not _writable(args[1]):
        _deny("write", args[1])


def _check_list(args):
    if not _readable(args[0] if args else None):
        _deny("list", args[0] if args else ".")


def _is_python(*parts) -> bool:
    text = " ".join(os.fsdecode(p) if isinstance(p, (bytes, os.PathLike)) else str(p) for p in parts if p)
    low = text.lower().replace("/", "\\")
    return any(p and p in low for p in _state["python"])


# ------------------------------------------------------------
def _check_process(event):
    def check(args):
        if not _is_python(*(a if not isinstance(a, (list, tuple)) else " ".join(map(str, a)) for a in args[:2])):
            _deny(event, args[0] if args else "")
    return check


# ------------------------------------------------------------
def _check_socket(args):
    address = args[1] if len(args) > 1 else None
    host = address[0] if isinstance(address, tuple) and address else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        _deny("network", address)


def _check_startfile(args):
    _deny("startfile", args[:1])


CHECKS = {
    "open": _check_open,
    "os.remove": _check_write, "os.rmdir": _check_write, "os.mkdir": _check_write,
    "os.chmod": _check_write, "os.truncate": _check_write, "os.utime": _check_write,
    "shutil.rmtree": _check_write,
    "os.rename": _check_two, "os.link": _check_two, "os.symlink": _check_dest,
    "shutil.copyfile": _check_dest, "shutil.copytree": _check_dest, "shutil.move": _check_dest,
    "os.listdir": _check_list, "os.scandir": _check_list,
    "subprocess.Popen": _check_process("process"), "os.system": _check_process("process"),
    "os.spawn": _check_process("process"), "os.posix_spawn": _check_process("process"),
    "os.exec": _check_process("process"), "_winapi.CreateProcess": _check_process("process"),
    "os.startfile": _check_startfile,
    "socket.connect": _check_socket, "socket.getaddrinfo": _check_socket,
    "socket.gethostbyname": _check_socket, "socket.sendto": _check_socket,
}


# ------------------------------------------------------------
def _hook(event, args):
    check = CHECKS.get(event)
    if check is None or _state["off"] or not _state["root"]:
        return
    try:
        check(args)
    except PermissionError:
        raise
    except Exception:  # a hook that fails on odd arguments must not break the program  # noqa: BLE001
        return


# ------------------------------------------------------------
# arms the jail on root (created if needed). Returns the normalised root. Cannot be undone
def install(root) -> str:
    global _installed
    root_path = norm(root)
    os.makedirs(root_path, exist_ok=True)
    reads = {root_path, norm(sys.prefix), norm(sys.base_prefix), norm(sys.exec_prefix),
             norm(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
             norm(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))}
    with contextlib.suppress(Exception):
        reads.add(norm(site.getusersitepackages()))
        reads.update(norm(p) for p in site.getsitepackages())
    reads.update(norm(p) for p in sys.path if p and os.path.isabs(p))
    with _LOCK:
        _state.update(root=root_path, reads=tuple(reads), blocked=[],
                      python=tuple({sys.executable.lower().replace("/", "\\"),
                                    os.path.dirname(sys.executable).lower().replace("/", "\\")}))
    sys.dont_write_bytecode = True
    tempfile.tempdir = os.path.join(root_path, "tmp")
    os.makedirs(tempfile.tempdir, exist_ok=True)
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    return root_path
