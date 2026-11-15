import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import beatdetect
from djapp.export.files import Cancelled
from djapp.model import Cue, Node, Track
from djapp.ui.details import PlaylistWindow
from djapp.ui.player import Engine
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox


# ------------------------------------------------------------------
class BatchDetectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracks = [Track(title="First", bpm=100, grid=0, cues=[Cue(start=123)]),
                       Track(title="Second", bpm=0, grid=None)]
        for index, track in enumerate(self.tracks):
            track.path = str(Path(self.tmp.name) / f"{index}.wav")
            Path(track.path).touch()
        self.engine = Engine()
        self.loader = Loader()
        with patch.object(self.loader, "prefetch"):
            self.window = PlaylistWindow(Node("playlist", "Batch", tracks=self.tracks),
                                         self.engine, self.loader, {})

    def tearDown(self):
        self.window.close()
        self.finish()
        self.loader.stop()
        self.engine.close()
        self.engine.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.tmp.cleanup()

    def finish(self):
        deadline = time.monotonic() + 5
        while self.window.detect_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window.detect_job)

    def prepare(self):
        self.window.detect_before = {id(track): (track.bpm, track.grid) for track in self.tracks}

    def test_whole_batch_is_one_undo_and_redo_preserving_cues(self):
        self.prepare()
        self.window.grids_detected([(id(track), beatdetect.BeatResult(128, 50, 0.9))
                                   for track in self.tracks])
        self.assertEqual(len(self.window.undo_stack), 1)
        self.assertTrue(all((t.bpm, t.grid) == (128, 50) for t in self.tracks))
        cue = self.tracks[0].cues[0]
        self.window.undo_cues()
        self.assertEqual([(t.bpm, t.grid) for t in self.tracks], [(100, 0), (0, None)])
        self.assertIs(self.tracks[0].cues[0], cue)
        self.window.undo_cues(True)
        self.assertTrue(all((t.bpm, t.grid) == (128, 50) for t in self.tracks))
        self.assertEqual(self.window.table.topLevelItem(1).text(5), "128.00")

    def test_errors_and_unreliable_results_do_not_change_tracks_or_history(self):
        self.prepare()
        with patch.object(QMessageBox, "warning") as warning:
            self.window.grids_detected([(id(self.tracks[0]), "cannot decode"),
                                       (id(self.tracks[1]), beatdetect.BeatResult(180, 50, 0.1))])
        warning.assert_called_once()
        self.assertIn("cannot decode", warning.call_args.args[2])
        self.assertFalse(self.window.undo_stack)
        self.assertEqual([(t.bpm, t.grid) for t in self.tracks], [(100, 0), (0, None)])

    def test_removed_or_edited_tracks_are_not_overwritten(self):
        self.prepare()
        self.tracks[0].bpm = 101
        self.window.node.tracks = [self.tracks[0]]
        self.window.grids_detected([(id(t), beatdetect.BeatResult(128, 50, 0.9)) for t in self.tracks])
        self.assertEqual(self.tracks[0].bpm, 101)
        self.assertFalse(self.window.undo_stack)

    def test_duplicate_track_references_are_applied_once(self):
        self.prepare()
        entry = (id(self.tracks[0]), beatdetect.BeatResult(128, 50, 0.9))
        self.window.grids_detected([entry, entry])
        self.assertEqual(len(self.window.undo_stack[0][1]), 1)

    def test_real_job_targets_tracks_without_grid_and_restores_controls(self):
        result = beatdetect.BeatResult(124, 90, 0.9)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(beatdetect, "analyze", return_value=result) as analyze:
            self.window.detect_grids()
            self.assertFalse(self.window.detect_button.isEnabled())
            self.assertTrue(self.window.detect_cancel.isEnabled())
            self.assertFalse(self.window.detect_cancel.isHidden())
            self.finish()
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args[0], self.tracks[1].path)
        self.assertEqual((self.tracks[0].bpm, self.tracks[1].bpm), (100, 124))
        self.assertTrue(self.window.detect_button.isEnabled())
        self.assertFalse(self.window.detect_cancel.isEnabled())
        self.assertTrue(self.window.detect_cancel.isHidden())

    def test_selection_limits_detection_even_for_existing_grids(self):
        self.window.table.topLevelItem(0).setSelected(True)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(beatdetect, "analyze", return_value=beatdetect.BeatResult(126, 25, 0.9)):
            self.window.detect_grids()
            self.finish()
        self.assertEqual([(t.bpm, t.grid) for t in self.tracks], [(126, 25), (0, None)])

    def test_cancel_and_close_ignore_queued_results(self):
        def slow(_path, progress=None, cancel=None):
            cancel.wait(2)
            return beatdetect.BeatResult(128, 50, 0.9)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(beatdetect, "analyze", side_effect=slow):
            self.window.detect_grids()
            self.window.cancel_detection()
            self.finish()
        self.assertEqual(self.tracks[1].bpm, 0)
        self.assertFalse(self.window.undo_stack)
        self.prepare()
        self.window.closing = True
        self.window.grids_detected([(id(self.tracks[1]), beatdetect.BeatResult(128, 50, 0.9))])
        self.assertEqual(self.tracks[1].bpm, 0)

    def test_analyze_many_reports_each_failure_and_honours_cancel(self):
        result = beatdetect.BeatResult(128, 50, 0.9)
        with patch.object(beatdetect, "analyze", side_effect=[ValueError("bad audio"), result]):
            self.assertEqual(beatdetect.analyze_many([(1, "bad"), (2, "good")]),
                             [(1, "bad audio"), (2, result)])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            beatdetect.analyze_many([(1, "audio")], cancel=cancel)


if __name__ == "__main__":
    unittest.main()
