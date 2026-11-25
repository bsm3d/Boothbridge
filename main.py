# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Launcher for the program: checks the compiled core for changes, then starts the application through djapp.app.

import sys
from multiprocessing import freeze_support

if __name__ == "__main__":
    try:
        freeze_support()
    except OSError:
        # a worker whose parent is already gone cannot finish starting (Windows refuses it the handles
        # of the parent): leave quietly, a windowed program would show a dialog about it
        sys.exit(1)
    from djapp import integrity

    problems = integrity.check()
    if problems:
        sys.exit(
            integrity.stop(
                problems,
                quiet=any(
                    a in sys.argv
                    for a in (
                        "--export",
                        "--verify",
                    )
                ),
            )
        )
    from djapp.app import main

    sys.exit(main())
