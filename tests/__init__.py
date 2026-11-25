# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Package marker and shared test setup: the program root and the environment helper for child processes.

import os
from pathlib import Path

# the program tree the tests run against: the folder above tests, or APP_ROOT when the tests live elsewhere
APP_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])


# the environment of a child process of the tests: the program tree comes first on its import path
def child_env(**extra) -> dict:
    paths = [str(APP_ROOT)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != str(APP_ROOT)]
    return dict(os.environ, PYTHONPATH=os.pathsep.join(paths), **extra)
