# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Entry point for python -m djapp that checks program integrity and then starts the application.

import sys
from multiprocessing import freeze_support

# spawned analysis workers import this module again, they must not start a second GUI
if __name__ == "__main__":
    try:
        freeze_support()
    # bug trouvé et fixé: a worker whose parent is gone showed an Unhandled exception
    # box (WinError 5), leave quietly
    except OSError:
        sys.exit(1)
    from djapp import integrity

    problems = integrity.check()
    if problems:
        sys.exit(
            integrity.stop(
                problems, quiet="--export" in sys.argv or "--verify" in sys.argv
            )
        )
    from djapp.app import main

    sys.exit(main())
