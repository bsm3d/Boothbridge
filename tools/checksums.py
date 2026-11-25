# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Command line tool that writes the SHA-256 sums of release files in the format that sha256sum -c reads.

# One line per file, "hash *name", the format sha256sum -c reads. To check a download, use Get-FileHash (Windows) or sha256sum -c (Linux, macOS).
# python tools/checksums.py FILE [FILE ...] [--out SHA256SUMS.txt]

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def main(argv: list[str]) -> int:
    out = None
    names = []
    i = 0
    while i < len(argv):
        if argv[i] == "--out" and i + 1 < len(argv):
            out = Path(argv[i + 1])
            i += 2
            continue
        names.append(argv[i])
        i += 1
    if not names:
        print(__doc__)
        return 2
    lines = []
    for name in names:
        path = Path(name)
        if not path.is_file():
            print(f"not a file: {name}")
            return 1
        lines.append(f"{sha256(path)} *{path.name}")
    text = "\n".join(lines) + "\n"
    if out:
        out.write_text(text, encoding="ascii", newline="\n")
    print(text, end="")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
