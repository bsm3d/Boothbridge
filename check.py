# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Local regression gate that runs Ruff, compileall and the test suite on the djapp package.

# fail-fast local regression gate: python check.py

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# the tests are in tests/ next to djapp, or in the Boothbridge-tests folder beside the program folder
def test_place(root: Path) -> tuple[Path, dict]:
    if (root / "tests").is_dir():
        return root, {}
    paths = [str(root), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    env = {"APP_ROOT": str(root), "PYTHONPATH": os.pathsep.join(paths)}
    return root.parent / "Boothbridge-tests", env


def main() -> int:
    root = Path(__file__).resolve().parent
    tests_cwd, tests_env = test_place(root)
    steps = [
        ("Ruff", [sys.executable, "-m", "ruff", "check", "djapp", "check.py"], root, {}),
        (
            "Compile",
            [sys.executable, "-m", "compileall", "-q", "djapp", "check.py"],
            root,
            {},
        ),
        (
            "Tests",
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            tests_cwd,
            tests_env,
        ),
        ("Diff", ["git", "--no-pager", "diff", "--check"], root, {}),
    ]
    for name, command, cwd, extra in steps:
        print(f"\n=== {name} ===", flush=True)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", **extra},
                check=False,
            )
        except OSError as error:
            print(f"Cannot run {name}: {error}", file=sys.stderr)
            return 1
        if result.returncode:
            print(f"{name} failed (exit {result.returncode}).", file=sys.stderr)
            return result.returncode
    print("\nAll regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
