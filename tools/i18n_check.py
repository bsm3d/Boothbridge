# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reports the interface texts that have no translation in the catalogs of djapp/lang, by reading the code.

# Only plain strings are checked: a variable or an f-string cannot be translated and is reported as such.
# python tools/i18n_check.py [--lang fr] [--files]

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from djapp import i18n, lang

FIELD = re.compile(r"\{(\w*)\}")

# ------------------------------------------------------------
def texts(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys, loose = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.id
            if isinstance(fn, ast.Name)
            else fn.attr
            if isinstance(fn, ast.Attribute)
            else ""
        )
        if name not in ("tr", "trn"):
            continue
        if isinstance(fn, ast.Attribute) and not (
            isinstance(fn.value, ast.Name) and fn.value.id == "i18n"
        ):
            continue
        args = node.args[1:3] if name == "trn" else node.args[:1]
        for a in args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                keys.append(a.value)
            else:
                loose.append((node.lineno, ast.unparse(a)[:60]))
    return keys, loose

def used() -> tuple[dict[str, set[str]], list[tuple[str, int, str]]]:
    where: dict[str, set[str]] = {}
    loose = []
    for path in sorted((ROOT / "djapp").rglob("*.py")):
        if "lang" in path.parts or path.name == "i18n.py":
            continue
        rel = path.relative_to(ROOT).as_posix()
        keys, bad = texts(path)
        for k in keys:
            where.setdefault(k, set()).add(rel)
        loose += [(rel, n, s) for n, s in bad]
    return where, loose

def catalog(code: str) -> dict[str, str]:
    words: dict[str, str] = {}
    for part in lang.PARTS:
        words.update(part.CATALOG.get(code, {}))
    return words

def check(codes=None):
    where, loose = used()
    report = {"loose": loose, "missing": {}, "fields": {}, "unused": {}}
    for code in codes or [c for c in i18n.LANGUAGES if c != i18n.SOURCE]:
        words = catalog(code)
        report["missing"][code] = sorted(k for k in where if not words.get(k))
        report["fields"][code] = sorted(
            k
            for k in where
            if words.get(k)
            and sorted(FIELD.findall(k)) != sorted(FIELD.findall(words[k]))
        )
        report["unused"][code] = sorted(k for k in words if k not in where)
    return report, where

# ------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", action="append")
    ap.add_argument(
        "--files", action="store_true", help="count the missing texts per source file"
    )
    args = ap.parse_args()
    report, where = check(args.lang)
    print(
        f"{len(where)} texts in the code, {len(report['loose'])} that cannot be translated"
    )
    for rel, n, s in report["loose"][:20]:
        print(f"  not a plain string: {rel}:{n} {s}")
    bad = bool(report["loose"])
    for code in report["missing"]:
        m, f, u = (
            report["missing"][code],
            report["fields"][code],
            report["unused"][code],
        )
        print(f"{code}: {len(m)} missing, {len(f)} with other fields, {len(u)} unused")
        bad = bad or bool(m or f)
        if args.files and m:
            count: dict[str, int] = {}
            for k in m:
                for rel in where[k]:
                    count[rel] = count.get(rel, 0) + 1
            for rel, n in sorted(count.items(), key=lambda x: -x[1])[:15]:
                print(f"    {n:4d} {rel}")
    return 1 if bad else 0

if __name__ == "__main__":
    raise SystemExit(main())
