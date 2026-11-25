# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks beat and bar detection on the timeline, with time and patch mocks in place.

import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import soundfile
from djapp import beatdetect
from djapp.model import Track
from djapp.ui.timeline import Timeline
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.test_beatdetect import RATE, click_track


class TimelineDetectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "click.wav")
        soundfile.write(self.path, click_track(126, 75, secs=30), RATE)
        self.track = Track(title="Click", duration=30, bpm=100, grid=10)
        self.loader = Loader()
        self.timeline = Timeline(self.track, self.path, self.loader, {})

    def tearDown(self):
        self.timeline.close()
        self.loader.stop()
        self.timeline.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.folder.cleanup()

    def wait_job(self):
        deadline = time.monotonic() + 15
        while self.timeline.detect_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.timeline.detect_job)

    def test_detect_applies_after_confirmation_and_can_be_undone(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question:
            self.timeline.detect_button.click()
            self.assertFalse(self.timeline.detect_button.isEnabled())
            self.wait_job()
        question.assert_called_once()
        self.assertAlmostEqual(self.track.bpm, 126, delta=0.1)
        self.assertAlmostEqual(self.track.grid, 75, delta=5)
        self.assertTrue(self.timeline.detect_button.isEnabled())

        self.timeline.undo()
        self.assertEqual((self.track.bpm, self.track.grid), (100, 10))

    def test_result_arriving_after_close_is_neither_asked_nor_applied(self):
        # the analysis ends just as the editor closes and never sees the cancel
        def late(path, progress=lambda *_: None, cancel=None):
            time.sleep(0.2)
            return beatdetect.BeatResult(128.0, 250.0, 1.0)

        self.timeline.setAttribute(Qt.WA_DeleteOnClose, False)
        with patch.object(beatdetect, "analyze", late), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question:
            self.timeline.detect_button.click()
            self.timeline.reject()
            self.wait_job()
        question.assert_not_called()
        self.assertEqual((self.track.bpm, self.track.grid), (100, 10))

    def test_detect_refused_keeps_the_grid(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.timeline.detect_button.click()
            self.wait_job()
        self.assertEqual((self.track.bpm, self.track.grid), (100, 10))

    def test_unreliable_result_asks_with_a_warning_and_defaults_to_no(self):
        weak = beatdetect.BeatResult(bpm=120, first_beat_ms=0, confidence=0.1)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No) as question:
            self.assertFalse(self.timeline.confirm_detected(weak))
        self.assertIn("not reliable", question.call_args.args[2])
        self.assertEqual(question.call_args.args[4], QMessageBox.No)

    def test_failure_is_reported_and_button_comes_back(self):
        with patch.object(beatdetect, "analyze", side_effect=ValueError("too short")), \
                patch.object(QMessageBox, "warning") as warning:
            self.timeline.detect_button.click()
            self.wait_job()
        warning.assert_called_once()
        self.assertIn("too short", warning.call_args.args[2])
        self.assertTrue(self.timeline.detect_button.isEnabled())

    def test_detect_is_disabled_without_a_file(self):
        timeline = Timeline(Track(title="x"), "", self.loader, {})
        self.assertFalse(timeline.detect_button.isEnabled())
        timeline.close()
        timeline.deleteLater()


if __name__ == "__main__":
    unittest.main()
