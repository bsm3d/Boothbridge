# Benoit Saint-Moulin
# Boothbridge : gives the program another name
#
#   python tools/rename.py NEWNAME            shows what would change
#   python tools/rename.py NEWNAME --apply    does it
#
# The name is a value, djapp/brand.py holds it. What cannot read it at run time (texts, the spec, the installer
# script, the names of a few files) is rewritten here, in the three spellings the tree uses: Name, name and NAME.
# The tests live outside the tree: pass their folder with --also to rewrite them too.

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "djapp" / "brand.py"
TEXT_SUFFIX = (
    ".py",
    ".md",
    ".txt",
    ".spec",
    ".bat",
    ".iss",
    ".svg",
    ".html",
    ".toml",
    ".cfg",
    ".ini",
    ".json",
)
SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    "old",
    ".venv",
}
VALID = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,30}$")


def current_name() -> str:
    m = re.search(
        r'^APP_NAME = "([^"]+)"', BRAND.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not m:
        sys.exit("APP_NAME not found in djapp/brand.py")
    return m.group(1)


def spellings(old: str, new: str) -> list[tuple[str, str]]:
    return [(old, new), (old.lower(), new.lower()), (old.upper(), new.upper())]


def files(base: Path):
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if (
            path.is_file()
            and path.suffix != ".log"
            and not any(part in SKIP_DIRS for part in rel.parts)
        ):
            yield path


def plan(base: Path, pairs: list[tuple[str, str]]):
    edits, renames = [], []
    for path in files(base):
        if path.suffix.lower() in TEXT_SUFFIX:
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            out = text
            for old, new in pairs:
                out = out.replace(old, new)
            if out != text:
                edits.append((path, raw, out.encode("utf-8")))
        name = path.name
        for old, new in pairs:
            name = name.replace(old, new)
        if name != path.name:
            renames.append((path, path.with_name(name)))
    return edits, renames


# ------------------------------------------------------------
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    also = [
        Path(sys.argv[i + 1])
        for i, a in enumerate(sys.argv)
        if a == "--also" and i + 1 < len(sys.argv)
    ]
    args = [a for a in args if Path(a) not in also]
    if len(args) != 1 or not VALID.match(args[0]):
        sys.exit(
            "usage: python tools/rename.py NEWNAME [--apply] [--also TESTS_FOLDER]\n"
            "the name is letters and digits only, it also gives the file names"
        )
    old, new = current_name(), args[0]
    if old == new:
        sys.exit("that is already the name")
    pairs = spellings(old, new)
    total_edits = total_renames = 0
    for base in [ROOT, *also]:
        edits, renames = plan(base, pairs)
        total_edits += len(edits)
        total_renames += len(renames)
        for path, raw, data in edits:
            print(
                ("edit   " if apply else "would edit   ") + str(path.relative_to(base))
            )
            if apply:
                path.write_bytes(data)
        for src, dst in renames:
            print(
                ("rename " if apply else "would rename ")
                + f"{src.relative_to(base)} -> {dst.name}"
            )
            if apply:
                src.rename(dst)
    print(
        f"{old} -> {new}: {total_edits} files, {total_renames} renames"
        + ("" if apply else " (nothing written, add --apply)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
