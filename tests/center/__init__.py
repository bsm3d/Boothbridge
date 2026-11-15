# tools behind the test center: random tests, monkey driver, memory runs, run records

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

from .. import APP_ROOT

TESTS = Path(__file__).resolve().parent.parent
ROOT = TESTS.parent
RUNS = TESTS / "runs"

for folder in (APP_ROOT, ROOT):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------
# tests/runs/<date-time>-<kind>.json, never an existing name
def new_run_path(kind: str) -> Path:
    base = f"{stamp()}-{kind}"
    path = RUNS / f"{base}.json"
    n = 2
    while path.exists():
        path = RUNS / f"{base}-{n}.json"
        n += 1
    return path


# ----------------------------------------------------------------
def versions() -> dict:
    out = {"python": platform.python_version(), "system": platform.platform()}
    for name, module in (("pyside6", "PySide6"), ("numpy", "numpy"), ("psutil", "psutil")):
        try:
            out[name] = __import__(module).__version__
        except (ImportError, AttributeError):
            out[name] = "absent"
    try:
        from djapp import VERSION
        out["djapp"] = VERSION
    except ImportError:
        out["djapp"] = "?"
    return out


# ------------------------------------------------------------------------
# written through a temporary file so a killed process never leaves half a record
def save_run(path, data: dict):
    from . import jail
    path = Path(path)
    with jail.trusted():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)


def load_run(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ----------------------------------------------------------------
# newest first
def list_runs() -> list[Path]:
    if not RUNS.is_dir():
        return []
    return sorted(RUNS.glob("*.json"), key=lambda p: p.name, reverse=True)


def header(kind: str, title: str, command: list[str], params: dict, seed=None) -> dict:
    return {"kind": kind, "title": title, "started": datetime.now().astimezone().isoformat(timespec="seconds"),
            "command": command, "params": params, "seed": seed, "versions": versions()}


def short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."
