# the launcher in a worker process: when the parent is gone Windows refuses the worker the handles it
# needs ([WinError 5] in multiprocessing), and a windowed program turned that into a dialog box

import runpy
import unittest
from unittest.mock import patch

from tests import APP_ROOT

ROOT = APP_ROOT


# ------------------------------------------------
class LauncherTests(unittest.TestCase):
    def run_launcher(self, name):
        with patch("multiprocessing.freeze_support", side_effect=PermissionError(5, "Access is denied")),                 self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(ROOT / name), run_name="__main__")
        return caught.exception.code

    def test_main_py_leaves_quietly_with_1(self):
        self.assertEqual(self.run_launcher("main.py"), 1)

    def test_the_package_entry_point_does_too(self):
        with patch("multiprocessing.freeze_support", side_effect=PermissionError(5, "Access is denied")),                 self.assertRaises(SystemExit) as caught:
            runpy.run_module("djapp", run_name="__main__")
        self.assertEqual(caught.exception.code, 1)

    def test_any_other_error_is_not_hidden(self):
        with patch("multiprocessing.freeze_support", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            runpy.run_path(str(ROOT / "main.py"), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
