# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Lifecycle regression tests from review: event disposal and jobs starting and finishing, with mocks.

import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings
from djapp.model import Cue, Node, Track
from djapp.ui.audiocheck import AudioCheck
from djapp.ui.backup import BackupPanel
from djapp.ui.details import COLS, PlaylistWindow
from djapp.ui.dialogs import About, CheckCollection, Options, Usage
from djapp.ui.main_window import MainWindow
from djapp.ui.timeline import CueEdit, Timeline
from djapp.ui.waveform import Loader
from djapp.ui.worker import Job, has_active_jobs, stop_all
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMenu
from shiboken6 import isValid


def dispose_events():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()


# ------------------------------------------------------------
class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        cfg = dict(settings.DEFAULTS)
        cfg["auto_load"] = False
        self.main = MainWindow(cfg, None)
        self.main.nodes = [Node(kind="playlist", name="Test")]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.main.dirty = False
        with patch("djapp.ui.main_window.settings.save"):
            self.main.close()
        self.main.deleteLater()
        dispose_events()

    def test_repeated_modal_panels_are_destroyed(self):
        with patch.object(AudioCheck, "exec", return_value=0), \
             patch.object(BackupPanel, "exec", return_value=0), \
             patch.object(CheckCollection, "exec", return_value=0), \
             patch.object(Usage, "exec", return_value=0), \
             patch.object(About, "exec", return_value=0), \
             patch.object(Options, "exec", return_value=0):
            for _ in range(4):
                self.main.check_audio()
                self.main.open_backup()
                self.main.options()
                self.main.check()
                self.main.show_dialog(Usage)
                self.main.show_dialog(About)
                dispose_events()
                self.assertFalse(self.main.findChildren(AudioCheck))
                self.assertFalse(self.main.findChildren(BackupPanel))
                self.assertFalse(self.main.findChildren(Options))
                self.assertFalse(self.main.findChildren(CheckCollection))
                self.assertFalse(self.main.findChildren(Usage))
                self.assertFalse(self.main.findChildren(About))
                self.assertIsNone(self.main.backup_panel)

    def test_pad_menus_and_cue_edit_dialogs_are_destroyed(self):
        track = Track(title="Long", duration=7200, bpm=120, grid=0, cues=[Cue(hotcue=0)])
        node = Node(kind="playlist", name="Test", tracks=[track])
        playlist = PlaylistWindow(node, self.main.engine, self.main.loader, self.main.cfg, parent=self.main)
        timeline = Timeline(track, "", self.main.loader, self.main.cfg, engine=self.main.engine,
                            parent=playlist)
        for _ in range(4):
            QTimer.singleShot(0, lambda: self.app.activePopupWidget().close())
            playlist.player.show_pad_menu(0, QPoint())
            QTimer.singleShot(0, lambda: self.app.activePopupWidget().close())
            timeline.show_pad_menu(0, QPoint())
            dispose_events()
            self.assertFalse(playlist.player.findChildren(QMenu))
            self.assertFalse(timeline.findChildren(QMenu))
        with patch.object(CueEdit, "exec", return_value=0):
            for _ in range(4):
                timeline.add_at(100)
                with patch.object(timeline, "selected", return_value=[track.cues[0]]):
                    timeline.edit()
                dispose_events()
                self.assertFalse(timeline.findChildren(CueEdit))
        self.assertEqual(playlist.table.topLevelItem(0).text(COLS.index("Time")), "120:00")
        job = Job(lambda progress, cancel: None)
        playlist.detect_job = timeline.detect_job = job
        with patch.object(job, "wait", return_value=False):
            event = QCloseEvent()
            playlist.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertFalse(playlist.closing)
            timeline.done(0)
            self.assertTrue(timeline.follow.isActive())
        playlist.detect_job = timeline.detect_job = None
        job.deleteLater()
        timeline.close()
        playlist.close()
        dispose_events()

    def test_collection_scan_blocks_close_and_releases_thread(self):
        release = threading.Event()

        def scan(root):
            release.wait(3)
            return {}

        with tempfile.TemporaryDirectory() as root:
            panel = CheckCollection([Node(kind="playlist", name="Test",
                                          tracks=[Track(path="missing.mp3")])], root, self.main)
            panel.show()
            with patch("djapp.ui.dialogs.index_folder", side_effect=scan), \
                 patch.object(panel, "apply"):
                panel.auto()
                thread = panel.scan
                try:
                    panel.reject()
                    self.assertTrue(panel.isVisible())
                    event = QCloseEvent()
                    panel.closeEvent(event)
                    self.assertFalse(event.isAccepted())
                finally:
                    release.set()
                    self.assertTrue(thread.wait(3000))
                self.app.processEvents()
                dispose_events()
                self.assertIsNone(panel.scan)
                self.assertFalse(isValid(thread))
            panel.reject()
            self.assertFalse(panel.isVisible())
            panel.deleteLater()
            dispose_events()

    def test_open_folder_respects_unsaved_project(self):
        self.main.src.setText("existing.json")
        with patch("djapp.ui.main_window.QFileDialog.getExistingDirectory",
                   return_value="new-folder"), \
             patch.object(self.main, "ask_discard", return_value=False), \
             patch.object(self.main, "reload") as reload:
            self.main.open_folder()
            reload.assert_not_called()
            self.assertEqual(self.main.src.text(), "existing.json")

    def test_close_does_not_release_resources_with_running_job(self):
        event = QCloseEvent()
        with patch("djapp.ui.main_window.stop_all", return_value=False), \
             patch.object(self.main.engine, "close") as close, \
             patch.object(self.main.loader, "stop") as stop:
            self.main.closeEvent(event)
            self.assertFalse(event.isAccepted())
            close.assert_not_called()
            stop.assert_not_called()
            self.assertIn("Waiting", self.main.status.text())

    def test_quit_blocks_running_operation_without_cancelling_or_prompting(self):
        release = threading.Event()
        entered = threading.Event()

        def operation(progress, cancel):
            entered.set()
            release.wait(3)

        job = Job(operation)
        job.start()
        self.assertTrue(entered.wait(1))
        try:
            with patch.object(self.main, "ask_discard") as discard, \
                 patch("djapp.ui.main_window.stop_all") as stop, \
                 patch.object(self.main.loader, "stop") as loader_stop, \
                 patch.object(self.main.engine, "close") as close:
                for _ in range(3):
                    event = QCloseEvent()
                    self.main.closeEvent(event)
                    self.assertFalse(event.isAccepted())
                discard.assert_not_called()
                stop.assert_not_called()
                loader_stop.assert_not_called()
                close.assert_not_called()
                self.assertFalse(job.stop.is_set())
                self.assertIn("Cannot quit", self.main.status.text())
        finally:
            release.set()
            job.wait(3000)
            self.app.processEvents()
        job.deleteLater()
        dispose_events()
        event = QCloseEvent()
        with patch.object(self.main, "ask_discard", return_value=True), \
             patch("djapp.ui.main_window.settings.save"):
            self.main.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def test_quit_blocks_running_job_and_collection_scan(self):
        panel = CheckCollection(self.main.nodes, "", self.main)
        sources = [(self.main, "job"), (panel, "scan")]
        try:
            for owner, name in sources:
                with self.subTest(operation=name),                      patch.object(owner, name, Mock()),                      patch.object(self.main, "ask_discard") as discard,                      patch("djapp.ui.main_window.stop_all") as stop:
                    event = QCloseEvent()
                    self.main.closeEvent(event)
                    self.assertFalse(event.isAccepted())
                    discard.assert_not_called()
                    stop.assert_not_called()
        finally:
            panel.deleteLater()
            dispose_events()

    def test_quit_does_not_wait_for_waveform_decoding(self):
        # the loader is stopped by the close itself, it is no reason to refuse
        for name, value in (("th", Mock()), ("hi_th", Mock()),
                            ("queue", ["pending-wave"]), ("hi_queue", ["pending-wave"])):
            with self.subTest(operation=name),                  patch.object(self.main.loader, name, value),                  patch.object(self.main, "ask_discard", return_value=False) as discard:
                event = QCloseEvent()
                self.main.closeEvent(event)
                discard.assert_called_once()
                self.assertFalse(event.isAccepted())

    def test_close_does_not_release_engine_with_running_decoder(self):
        event = QCloseEvent()
        with patch("djapp.ui.main_window.stop_all", return_value=True), \
             patch.object(self.main.loader, "stop", return_value=False), \
             patch.object(self.main.engine, "close") as close:
            self.main.closeEvent(event)
            self.assertFalse(event.isAccepted())
            close.assert_not_called()
            self.assertIn("waveform", self.main.status.text())

    def test_stop_all_reports_slow_worker_then_finishes(self):
        release = threading.Event()

        def slow(progress, cancel):
            release.wait(3)

        job = Job(slow)
        job.start()
        try:
            self.assertFalse(stop_all(1))
            self.assertTrue(job.stop.is_set())
        finally:
            release.set()
            job.wait(3000)
        self.app.processEvents()
        self.assertTrue(stop_all(10))
        job.deleteLater()
        dispose_events()

    def test_stop_all_shares_one_wait_budget(self):
        jobs = [Mock(), Mock(), Mock()]
        for job in jobs:
            job.wait.return_value = False
        with patch("djapp.ui.worker._alive", set(jobs)), \
             patch("djapp.ui.worker.monotonic", side_effect=[0, .002, .004, .006]):
            self.assertFalse(stop_all(5))
        self.assertEqual(sorted(job.wait.call_args.args[0] for job in jobs), [0, 1, 3])
        for job in jobs:
            job.stop.set.assert_called_once()

    def test_quit_blocks_registry_entry_until_finished_signal_is_processed(self):
        job = Mock()
        job.isRunning.return_value = False
        with patch("djapp.ui.worker._alive", {job}), \
                patch.object(self.main, "ask_discard") as discard:
            self.assertTrue(has_active_jobs())
            event = QCloseEvent()
            self.main.closeEvent(event)
            self.assertFalse(event.isAccepted())
            discard.assert_not_called()
        job.stop.set.assert_not_called()

    def test_waveform_requests_do_not_queue_active_path(self):
        loader = Loader()
        loader.th = Mock(paths=["track"])
        loader.hi_th = Mock(path="track")
        with patch("djapp.ui.waveform.load") as load, \
             patch("djapp.ui.waveform.load_hi") as load_hi:
            for _ in range(4):
                self.assertIsNone(loader.get("track"))
                self.assertIsNone(loader.get_hi("track"))
            load.assert_not_called()
            load_hi.assert_not_called()
        self.assertFalse(loader.queue)
        self.assertFalse(loader.hi_queue)
        loader.th = loader.hi_th = None
        loader.deleteLater()
        dispose_events()

    def test_waveform_stop_shares_budget_and_keeps_timed_out_threads(self):
        loader = Loader()
        low, high = Mock(), Mock()
        low.wait.return_value = high.wait.return_value = False
        loader.th, loader.hi_th = low, high
        loader.queue.append("low")
        loader.hi_queue.append("high")
        with patch("djapp.ui.waveform.monotonic", side_effect=[0, .002, .004]):
            self.assertFalse(loader.stop(5))
        low.wait.assert_called_once_with(3)
        high.wait.assert_called_once_with(1)
        self.assertTrue(low.halt)
        self.assertIs(loader.th, low)
        self.assertIs(loader.hi_th, high)
        self.assertFalse(loader.queue)
        self.assertFalse(loader.hi_queue)
        loader.th = loader.hi_th = None
        loader.deleteLater()
        dispose_events()

    def test_waveform_stop_has_bounded_wait_and_preserves_thread(self):
        release = threading.Event()
        loader = Loader()

        def compute(path):
            release.wait(3)
            return [(0., 0., 0.)]

        with patch("djapp.ui.waveform.load", return_value=None), \
             patch("djapp.ui.waveform.compute", side_effect=compute):
            loader.get("track")
            thread = loader.th
            try:
                self.assertFalse(loader.stop(1))
                self.assertIs(loader.th, thread)
                self.assertTrue(thread.halt)
            finally:
                release.set()
                self.assertTrue(thread.wait(3000))
            self.app.processEvents()
            dispose_events()
            self.assertIsNone(loader.th)
            self.assertFalse(isValid(thread))
            self.assertTrue(loader.stop(1))
        loader.deleteLater()
        dispose_events()


if __name__ == "__main__":
    unittest.main()
