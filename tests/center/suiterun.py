# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Runs unittest modules or test ids and prints one machine-readable result line per test.

# runs unittest modules or test ids like `python -m unittest -v`, and adds one machine readable
# line per test after its result: @@BB {"id": ..., "status": ..., "ms": ..., "msg": ...}

from __future__ import annotations

import json
import sys
import time
import unittest

from . import ROOT

MARK = "@@BB "


# ------------------------------------------------
class Result(unittest.TextTestResult):
    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def emit(self, test, status: str, text: str = ""):
        ms = round((time.perf_counter() - getattr(self, "started", time.perf_counter())) * 1000)
        self.stream.writeln(MARK + json.dumps({"id": test.id(), "status": status, "ms": ms, "msg": text[-4000:]}))
        self.stream.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        self.emit(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.emit(test, "echec", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.emit(test, "erreur", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.emit(test, "saute", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.emit(test, "ok", "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.emit(test, "echec", "unexpected success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.emit(subtest, "echec", self._exc_info_to_string(err, test))


# --------------------------------------------------
def main(names: list[str]) -> int:
    sys.path.insert(0, str(ROOT))
    try:
        import tests.conftest  # fonts of the offscreen platform  # noqa: F401
    except ImportError:
        pass
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=Result)
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
