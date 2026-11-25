# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that the format helper process receives the program's language from the environment.

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import drives, fat32, i18n, lang

from tests import APP_ROOT

ROOT = APP_ROOT


# ------------------------------------------------------------
class FormatHelperLanguageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_the_helper_process_gets_the_language_of_the_window(self):
        i18n.set_language("fr")
        args = drives.helper_args(drives.Drive("1", "", 10, serial="S"), "L", "bb_format_x.log")
        self.assertEqual(args[args.index("--lang") + 1], "fr")

    def test_the_helper_writes_its_progress_in_that_language(self):
        drive = drives.Drive("1", "", 10, serial="S")
        seen = []

        def fake_format(d, label, progress):
            progress(2, i18n.tr("Cleaning the drive"))
            return "E:"

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, drives.PROGRESS_PREFIX + "x.log")
            Path(path).write_text("", encoding="utf-8")
            argv = ["x", "--format-disk", "1", "--size", "10", "--serial", "S", "--label", "L",
                    "--progress", path, "--lang", "fr"]
            with patch.object(drives, "check", return_value=drive), patch.object(drives, "do_format", fake_format):
                self.assertEqual(drives.helper_main(argv), 0)
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                seen.append(line.partition("|")[2])
        self.assertIn(lang.PARTS[2].CATALOG["fr"]["Cleaning the drive"], seen)
        self.assertIn(lang.PARTS[2].CATALOG["fr"]["Starting"], seen)

    def test_without_a_language_the_helper_stays_english(self):
        i18n.set_language("fr")
        drive = drives.Drive("1", "", 10, serial="S")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, drives.PROGRESS_PREFIX + "x.log")
            Path(path).write_text("", encoding="utf-8")
            argv = ["x", "--format-disk", "1", "--size", "10", "--progress", path]
            with patch.object(drives, "check", return_value=drive), patch.object(
                drives, "do_format", lambda d, label, progress: "E:"
            ):
                drives.helper_main(argv)
            self.assertIn("pct:2|Starting", Path(path).read_text(encoding="utf-8"))


# ------------------------------------------------------------
class FormatProgressTextsTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def run_format(self, code):
        i18n.set_language(code)
        size = 64 * fat32.MIB
        texts = set()
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "disk.img"
            with image.open("wb") as f:
                f.truncate(size)
            device = fat32.RawDevice(str(image), size)
            try:
                fat32.format_disk(device, size, label="T", progress=lambda pct, msg: texts.add(msg))
            finally:
                device.close()
        return texts

    def test_english_is_unchanged(self):
        texts = self.run_format("en")
        self.assertIn("Erasing the start of the drive", texts)
        self.assertIn("Writing the partition table", texts)
        self.assertIn("Formatted", texts)

    def test_every_progress_text_of_the_writer_is_translated(self):
        english = self.run_format("en")
        for code in ("fr", "de", "es", "zh"):
            with self.subTest(code):
                texts = self.run_format(code)
                self.assertEqual(len(texts), len(english))
                self.assertFalse(texts & english)


# ------------------------------------------------------------
class BuildKeepsQtTranslationsTests(unittest.TestCase):
    def test_the_prune_keeps_the_qt_files_of_the_four_languages(self):
        spec = importlib.util.spec_from_file_location("bb_build_review", ROOT / "build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            tr = dist / "runtime" / "PySide6" / "translations"
            tr.mkdir(parents=True)
            names = ["qtbase_fr.qm", "qtbase_de.qm", "qtbase_es.qm", "qtbase_zh_CN.qm", "qtbase_ru.qm", "qt_fr.qm",
                     "qtmultimedia_fr.qm"]
            for n in names:
                (tr / n).write_bytes(b"x")
            with patch.object(build, "DIST", dist):
                build.prune()
            self.assertEqual(
                sorted(p.name for p in tr.iterdir()),
                ["qtbase_de.qm", "qtbase_es.qm", "qtbase_fr.qm", "qtbase_zh_CN.qm"],
            )


if __name__ == "__main__":
    unittest.main()
