# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Headless command line of the test center for monkey, memory and random runs, with exit codes for pass, fail and broken.

"""Headless command line of the test center (the GUI starts these same commands in a child process).

  python tests/test_center.py --monkey --seed 7 --steps 300
  python tests/test_center.py --memory --scenario open_close_playlist --cycles 60
  python tests/test_center.py --random --scenario all --seed 7 --iterations 200

Exit code 0 when everything passes, 1 when something fails, 2 when the run itself broke.
"""

from __future__ import annotations

import argparse
import importlib.util
import random
import sys
import time
import traceback
from pathlib import Path

from . import header, new_run_path, save_run

MODES = ("--monkey", "--memory", "--random")


def wants_cli(argv: list[str]) -> bool:
    return any(a in MODES for a in argv)


# A python file that registers more scenarios (tests use it for injected failures)
def load_extra(path: str):
    spec = importlib.util.spec_from_file_location("bb_center_extra", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    from . import monkey
    ap = argparse.ArgumentParser(prog="test_center.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    for name in MODES:
        mode.add_argument(name, action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="")
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=-1)
    ap.add_argument("--timeout", type=float, default=0.0)
    ap.add_argument("--limits", default="", help="memory thresholds as a JSON object")
    ap.add_argument("--extra", default="", help="python file registering more scenarios")
    monkey.add_arguments(ap)
    return ap


# ------------------------------------------------------------
def run_random(args, command, out) -> int:
    from . import fuzz
    if args.extra:
        load_extra(args.extra)
    record = header("random", "Aléatoire", command, {"scenario": args.scenario, "iterations": args.iterations}, args.seed)
    wanted = [s for s in args.scenario.split(",") if s]
    started = time.monotonic()
    try:
        data = fuzz.run(wanted, args.seed, args.iterations, args.timeout, log=lambda t: print(t, flush=True))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    record.update(verdict=data["verdict"], summary=data["summary"], details=data,
                  seconds=round(time.monotonic() - started, 2))
    save_run(out, record)
    print(f"graine {args.seed} : {record['summary']}")
    print(f"résultat : {out}")
    return 0 if data["verdict"] == "ok" else 1


# ------------------------------------------------------------
def run_memory(args, command, out) -> int:
    from . import memory
    if args.extra:
        load_extra(args.extra)
    return memory.run_cli(args, command, out)


# --------------------------------------------
def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    if args.seed is None:
        args.seed = random.SystemRandom().randrange(1_000_000)
    kind = "monkey" if args.monkey else "memory" if args.memory else "random"
    out = Path(args.out) if args.out else new_run_path(kind)
    command = [sys.executable, "tests/test_center.py", *argv]
    print(f"graine : {args.seed}", flush=True)
    try:
        if args.monkey:
            from . import monkey
            return monkey.run_cli(args, command, out)
        if args.memory:
            return run_memory(args, command, out)
        return run_random(args, command, out)
    except SystemExit:
        raise
    except Exception:  # the exit code must say the run itself broke  # noqa: BLE001
        traceback.print_exc()
        return 2
