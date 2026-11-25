# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the audio check panel in the window, from starting a job to showing its result.

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import soundfile as sf
from djapp.export.files import Cancelled
from djapp.model import Node, Track
from djapp.ui.audiocheck import AudioCheck
from PySide6.QtWidgets import QApplication, QLabel


# ----------------------------------------------------------------------
class AudioCheckUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_job(self, panel):
        end = time.monotonic() + 5
        while panel.job is not None and time.monotonic() < end:
            time.sleep(0.01)
            self.app.processEvents()
        if panel.job is not None:
            panel.job.stop.set()
            panel.job.wait(5000)
            self.app.processEvents()
            self.fail("Verification did not finish within five seconds")

    def test_both_modes_and_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audio.wav"
            sf.write(path, np.zeros(8000), 8000)
            panel = AudioCheck([Node(kind="playlist", name="Test", tracks=[Track(path=str(path))])])
            self.addCleanup(panel.close)
            self.assertEqual(panel.mode.count(), 2)
            self.assertEqual(panel.mode.currentIndex(), 1)
            self.assertEqual((panel.time_limit.minimum(), panel.time_limit.maximum(),
                              panel.time_limit.value()), (5, 3600, 120))
            self.assertEqual((panel.memory_limit.minimum(), panel.memory_limit.maximum(),
                              panel.memory_limit.value()), (256, 8192, 1024))
            for mode, status in [(0, "Headers readable"), (1, "Decoded")]:
                panel.mode.setCurrentIndex(mode)
                panel.start()
                self.wait_for_job(panel)
                self.assertEqual(panel.table.topLevelItem(0).text(0), status)
                self.assertTrue(panel.start_button.isEnabled())
                self.assertFalse(panel.cancel_button.isEnabled())
                self.assertTrue(panel.time_limit.isEnabled())
                self.assertTrue(panel.memory_limit.isEnabled())
            panel.source.setCurrentIndex(1)
            panel.folder.setText(folder)
            panel.start()
            self.wait_for_job(panel)
            self.assertEqual(panel.table.topLevelItemCount(), 1)

    def test_closing_running_panel_requests_cancel(self):
        def slow(*args, cancel, **kwargs):
            cancel.wait(5)
            if cancel.is_set():
                raise Cancelled()
            return []

        panel = AudioCheck([Node(kind="playlist", name="Test", tracks=[Track(path="test.wav")])])
        self.addCleanup(panel.close)
        panel.show()
        with patch("djapp.ui.audiocheck.audiocheck.scan", slow):
            panel.start()
            self.assertFalse(panel.time_limit.isEnabled())
            self.assertFalse(panel.memory_limit.isEnabled())
            panel.close()
            self.assertTrue(panel.isVisible())
            self.wait_for_job(panel)
        self.assertIn("cancelled", panel.status.text())
        self.assertTrue(panel.start_button.isEnabled())
        panel.close()
        self.assertFalse(panel.isVisible())

    def test_configured_limits_are_passed_to_job_and_clamav_is_detection_only(self):
        panel = None
        with patch("djapp.ui.audiocheck.audiocheck.find_clamav",
                   return_value="C:\\ClamAV\\clamscan.exe"):
            panel = AudioCheck([Node(kind="playlist", name="Test", tracks=[Track(path="test.wav")])])
        self.addCleanup(panel.close)
        text = " ".join(label.text() for label in panel.findChildren(QLabel))
        self.assertIn("ClamAV detected: C:\\ClamAV\\clamscan.exe", text)
        self.assertIn("antivirus scan not performed", text)
        self.assertIn("not a security sandbox", text)
        panel.time_limit.setValue(25)
        panel.memory_limit.setValue(512)
        with patch("djapp.ui.audiocheck.audiocheck.scan", return_value=[]) as scan:
            panel.start()
            self.wait_for_job(panel)
        self.assertEqual(scan.call_args.kwargs["timeout"], 25)
        self.assertEqual(scan.call_args.kwargs["memory_mb"], 512)
        self.assertTrue(panel.time_limit.isEnabled())
        self.assertTrue(panel.memory_limit.isEnabled())

    def test_errors_are_visible_and_controls_restored(self):
        panel = AudioCheck([])
        self.addCleanup(panel.close)
        panel.source.setCurrentIndex(1)
        panel.folder.setText("missing-audio-folder")
        with patch("djapp.ui.audiocheck.QMessageBox.critical") as critical:
            panel.start()
            self.wait_for_job(panel)
            critical.assert_called_once()
        self.assertIn("failed", panel.status.text())
        self.assertTrue(panel.start_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
