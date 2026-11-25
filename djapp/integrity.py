# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Startup check that compares the compiled core files with their sealed SHA-256 values and stops on changes.

# compile_core.py writes the SHA-256 of the compiled core into djapp/_seal.py, packed in the executable; the program checks the files on disk against it.
# A file that changed, vanished or was added beside them stops the program. The checker stays in the executable's Python archive, not in an extension DLL.
# A casual swap is all it keeps out: whoever rebuilds the executable can rebuild the seal.

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

EXTENSIONS = (".pyd", ".dll", ".so", ".dylib")

HEAD = "Boothbridge found files that were changed or damaged, so it will not start."

MESSAGE = (
    HEAD + "\n\n"
    "Run the installer again, or download the portable version again and unzip it to a new "
    "folder. Do not unzip over an older copy."
)

# Only unexpected files: most likely an older copy left them when a new version was unzipped over it
STALE = (
    HEAD + "\n\n{details}\n\n"
    "These files do not belong to this version. An older copy probably left them. Delete them "
    "from the runtime\\djapp folder next to Boothbridge.exe, then start the program again.\n\n"
    "To avoid this, unzip a new version into a new folder, or use the installer."
)

# ----------------------------------------------------------
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

# ----------------------------------------------------------
def scan(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): sha256(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in EXTENSIONS
    }

# ----------------------------------------------------------
# what differs between the files on disk and the seal, one line each
def problems(root: Path, expected: dict[str, str]) -> list[str]:
    found = scan(root)
    out = [f"missing: {rel}" for rel in expected if rel not in found]
    out += [
        f"changed: {rel}"
        for rel, digest in expected.items()
        if rel in found and found[rel] != digest
    ]
    out += [f"unexpected: {rel}" for rel in found if rel not in expected]
    return out

# ----------------------------------------------------------
# empty when everything is as sealed, or when this is not a sealed build (a run from
# the sources has no seal and nothing to protect)
def check() -> list[str]:
    if not getattr(sys, "frozen", False):
        return []
    try:
        from . import _seal
    except ImportError:
        return ["missing integrity seal in frozen build"]
    if (
        not isinstance(getattr(_seal, "HASHES", None), dict)
        or not _seal.HASHES
        or any(
            not isinstance(rel, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            for rel, digest in _seal.HASHES.items()
        )
    ):
        return ["invalid integrity seal in frozen build"]
    root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "djapp"
    return problems(root, _seal.HASHES)

def details(found: list[str], limit: int = 6) -> str:
    lines = found[:limit]
    if len(found) > limit:
        lines.append(f"... and {len(found) - limit} more")
    return "\n".join(lines)

# ----------------------------------------------------------
# logs what was found and tells the user, without Qt (nothing has been loaded yet).
# quiet: command line use, a dialog would block a script, the message goes to stderr
def stop(found: list[str], quiet: bool = False) -> int:
    for line in found:
        log.error("integrity: %s", line)
    if found and all(line.startswith("unexpected:") for line in found):
        text = STALE.format(details=details(found))
    else:
        text = MESSAGE + "\n\n" + details(found)
    if quiet and sys.stderr is not None:
        print(text, file=sys.stderr)
    elif sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Boothbridge", 0x10)
    elif sys.stderr is not None:
        print(text, file=sys.stderr)
    return 3
