# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the command line options: help text in every spelling, refused unknown options, and no window opened.

import os
import subprocess
import sys
import unittest

from djapp import app

from tests import APP_ROOT

ROOT = APP_ROOT


class CheckOptionsTests(unittest.TestCase):
    def check(self, *args):
        return app.check_options(["Boothbridge", *args])

    def test_help_in_every_spelling(self):
        for flag in ("--help", "-h", "/?"):
            with self.subTest(flag):
                self.assertEqual(self.check(flag), 0)
                self.assertEqual(self.check("--export", flag), 0)

    def test_an_option_that_does_not_exist_is_refused(self):
        for args in (("--bogus",), ("--export", "a.nml", "--out"), ("--vrify", "x"), ("--keeep",)):
            with self.subTest(args):
                self.assertEqual(self.check(*args), 2)

    def test_the_options_of_export_need_export(self):
        for flag in ("--keep", "--replace", "--format", "--music"):
            with self.subTest(flag):
                self.assertEqual(self.check(flag), 2)
                self.assertIsNone(self.check("--export", "a.nml", "out", flag))

    def test_what_the_program_itself_passes_to_its_helpers_goes_through(self):
        self.assertIsNone(self.check("--format-disk", "3", "--size", "10", "--serial", "S", "--label", "L",
                                     "--progress", "bb_format_x.log", "--owner", "1000"))
        self.assertIsNone(self.check("--multiprocessing-fork", "parent_pid=1", "pipe_handle=2"))

    def test_no_argument_and_the_real_modes_go_on(self):
        self.assertIsNone(self.check())
        self.assertIsNone(self.check("--verify", "key"))
        self.assertIsNone(self.check("--export", "a.nml", "out", "--format", "M3U", "--replace"))


# ------------------------------------------------------
class ProgramTests(unittest.TestCase):
    def run_main(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *args], capture_output=True, text=True,
                              cwd=ROOT, env=env, timeout=60, check=False)

    def test_help_prints_the_usage_and_leaves(self):
        r = self.run_main("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Boothbridge --export COLLECTION OUTPUT", r.stdout)

    def test_an_unknown_option_prints_it_and_leaves_with_2(self):
        r = self.run_main("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option: --bogus", r.stdout)


if __name__ == "__main__":
    unittest.main()
