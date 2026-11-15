# the last thing a user sees before a drive is erased, and that only the drive that was picked is
# formatted

import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import drives
from djapp.ui.formatter import FormatDialog
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox


def wait_for(app, condition, seconds=10):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


KEY = drives.Drive("3", "SanDisk Ultra", 62_000_000_000, mounts=["E:\\"], volumes=["TRAKTOR (FAT32, 62.0 GB)"])
DISK = drives.Drive("4", "My Passport", 2_000_000_000_000, mounts=["F:\\", "G:\\"],
                    volumes=["Music (NTFS, 1.5 TB)", "Backups (NTFS, 0.5 TB)"])


# ------------------------------------------------------------
class QuestionTests(unittest.TestCase):
    def test_it_names_the_drive_what_is_on_it_and_the_label(self):
        text = FormatDialog.question(KEY, "TRAKTOR")
        for part in ("SanDisk Ultra", "E:\\", "62.0 GB", "TRAKTOR (FAT32, 62.0 GB)", "FAT32 \"TRAKTOR\"",
                     "All its data will be lost"):
            self.assertIn(part, text)
        self.assertNotIn("make sure it is the one you mean", text)

    def test_a_big_drive_is_called_out(self):
        text = FormatDialog.question(DISK, "X")
        self.assertIn("2.00 TB", text)
        self.assertIn("make sure it is the one you mean", text)
        self.assertIn("Music (NTFS, 1.5 TB); Backups (NTFS, 0.5 TB)", text)

    def test_a_drive_without_a_readable_partition_says_so(self):
        text = FormatDialog.question(drives.Drive("5", "Blank", 8_000_000_000), "X")
        self.assertIn("no partition it can read", text)

    def test_markup_and_control_characters_in_names_are_not_shown_as_such(self):
        evil = drives.Drive("6", "<b>KEY</b> & co\x07", 8_000_000_000, volumes=["<img src=x> (FAT32, 8 GB)"])
        text = FormatDialog.question(evil, "X")
        self.assertNotIn("<", text)
        self.assertNotIn("&", text)
        self.assertNotIn("\x07", text)


# ------------------------------------------------------------
class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.formatted = []
        self.asked = []
        self.answer = QMessageBox.Cancel

        def warning(parent, title, text, *rest):
            self.asked.append((title, text))
            return self.answer

        for p in (patch.object(drives, "list_drives", lambda progress=None, cancel=None: [KEY, DISK]),
                  patch.object(drives, "format_drive", lambda d, label, progress=None, cancel=None:
                               self.formatted.append((d.id, label)) or "E:\\"),
                  patch.object(QMessageBox, "warning", warning),
                  patch.object(QMessageBox, "information"), patch.object(QMessageBox, "critical")):
            p.start()
            self.addCleanup(p.stop)
        self.dialog = FormatDialog()
        wait_for(self.app, lambda: self.dialog.drives)
        self.addCleanup(self.dialog.deleteLater)

    def pick(self, index):
        self.dialog.combo.setCurrentIndex(index)
        self.dialog.sure.setChecked(True)

    def test_the_button_stays_off_until_the_box_is_ticked(self):
        self.assertFalse(self.dialog.go.isEnabled())
        self.dialog.sure.setChecked(True)
        self.assertTrue(self.dialog.go.isEnabled())

    def test_cancelling_the_confirmation_formats_nothing(self):
        self.pick(0)
        self.dialog.start()
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.formatted, [])

    def test_only_the_drive_that_was_picked_is_formatted_with_the_label_that_was_shown(self):
        self.pick(1)
        self.dialog.label.setText("my key")
        self.answer = QMessageBox.Yes
        self.dialog.start()
        wait_for(self.app, lambda: self.formatted)
        wait_for(self.app, lambda: not self.dialog.working)
        self.assertEqual(self.formatted, [("4", "MY KEY")])
        self.assertIn("MY KEY", self.asked[0][1])
        self.assertIn("My Passport", self.asked[0][1])

    def test_the_details_are_plain_text(self):
        self.assertEqual(self.dialog.details.textFormat(), Qt.PlainText)
        self.assertEqual(self.dialog.status.textFormat(), Qt.PlainText)


if __name__ == "__main__":
    unittest.main()
