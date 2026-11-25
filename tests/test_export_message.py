# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the message the export shows the user after a run, for each target format.

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings
from djapp.export.files import Report
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow
from PySide6.QtWidgets import QApplication, QGroupBox, QMessageBox


# what the window says once an export is done, and what it counts
class ExportMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False, source_path="", output_path="")
        self.window = MainWindow(cfg, LogWindow())
        self.addCleanup(self.window.close)

    def said(self, fmt: str, report: Report) -> str:
        self.window.fmt.setCurrentText(fmt)
        shown = []
        with patch.object(QMessageBox, "warning", lambda *a: shown.append(a[2])), \
                patch.object(QMessageBox, "information", lambda *a: shown.append(a[2])), \
                patch.object(self.window, "offer_eject", create=True):
            self.window.exported(report)
        return shown[-1]

    def test_the_export_format_sits_in_the_destination_with_the_output(self):
        group = self.window.out.parentWidget()
        self.assertIsInstance(group, QGroupBox)
        self.assertEqual(group.title(), "Destination")
        for widget in (self.window.fmt, self.window.fmt_btn, self.window.copy, self.window.verify):
            self.assertIs(widget.parentWidget(), group)
        self.assertIsNot(self.window.src.parentWidget(), group)
        self.assertEqual(self.window.fmt_btn.text(), "Format drive...")

    def test_a_file_format_writes_every_track_and_says_so(self):
        text = self.said("Rekordbox XML", Report(tracks=5, path="out", missing=["a.aiff", "b.aiff"]))
        self.assertIn("5 tracks exported", text)
        self.assertIn("2 of these tracks have no audio file", text)
        self.assertNotIn("skipped", text)

    def test_no_missing_file_no_remark(self):
        text = self.said("M3U", Report(tracks=3, path="out"))
        self.assertIn("3 tracks exported", text)
        self.assertNotIn("no audio file", text)

    def test_a_cdj_key_leaves_the_tracks_without_a_file_out(self):
        if self.window.fmt.findText("CDJ/USB") < 0:
            self.skipTest("no CDJ export in this build")
        text = self.said("CDJ/USB", Report(tracks=5, path="out", missing=["a.aiff", "b.aiff"]))
        self.assertIn("3 tracks exported", text)
        self.assertIn("2 missing files skipped", text)

    def test_the_message_follows_the_run_not_the_combo_changed_meanwhile(self):
        if self.window.fmt.findText("CDJ/USB") < 0:
            self.skipTest("no CDJ export in this build")
        self.window.fmt.setCurrentText("M3U")
        shown = []
        run = ("CDJ/USB", [], "out", dict(self.window.cfg, verify_copy=False))
        with patch.object(QMessageBox, "warning", lambda *a: shown.append(a[2])), \
                patch.object(self.window, "offer_eject", create=True):
            self.window.exported(Report(tracks=5, path="out", missing=["a.aiff", "b.aiff"]), run)
        self.assertIn("3 tracks exported", shown[-1])
        self.assertIn("2 missing files skipped", shown[-1])


if __name__ == "__main__":
    unittest.main()
