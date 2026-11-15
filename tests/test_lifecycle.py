import gc
import os
import tempfile
import threading
import time
import unittest
import weakref
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import backup
from djapp.export.files import Cancelled
from djapp.model import Node, Track
from djapp.sources import project
from djapp.ui import worker
from djapp.ui.details import PlaylistWindow
from djapp.ui.player import Engine
from djapp.ui.timeline import Timeline
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

ERRORS = (OSError, ValueError, zipfile.BadZipFile)


def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

# ------------------------------------------------------------
def wait_for(app, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return condition()


# ------------------------------------------------------------
class JobLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_finished_job_leaves_registry(self):
        job = worker.Job(lambda progress, cancel: 42)
        results = []
        job.done.connect(results.append)
        job.start()
        self.assertTrue(wait_for(self.app, lambda: not job.isRunning() and job not in worker._alive))
        self.assertEqual(results, [42])
        job.deleteLater()
        flush(self.app)

    def test_failed_job_reports_and_leaves_registry(self):
        def boom(progress, cancel):
            raise ValueError("boom")

        job = worker.Job(boom)
        errors = []
        job.failed.connect(errors.append)
        with self.assertLogs("djapp.ui.worker", level="ERROR"):
            job.start()
            self.assertTrue(wait_for(self.app, lambda: errors and job not in worker._alive))
        self.assertEqual(errors, ["boom"])
        job.deleteLater()
        flush(self.app)

    def test_stop_all_cancels_blocked_jobs_and_empties_registry(self):
        started = threading.Event()

        def wait_cancel(progress, cancel):
            started.set()
            while not cancel.is_set():
                time.sleep(0.005)
            raise Cancelled()

        jobs = [worker.Job(wait_cancel) for _ in range(3)]
        cancelled = []
        for job in jobs:
            job.cancelled.connect(lambda: cancelled.append(1))
            job.start()
        self.assertTrue(started.wait(5))
        worker.stop_all()
        self.assertTrue(all(not job.isRunning() for job in jobs))
        self.assertFalse(worker._alive & set(jobs))
        wait_for(self.app, lambda: len(cancelled) == 3)
        self.assertEqual(len(cancelled), 3)
        for job in jobs:
            job.deleteLater()
        flush(self.app)

    def test_running_job_survives_dropped_python_reference(self):
        release = threading.Event()
        job = worker.Job(lambda progress, cancel: release.wait(5))
        ref = weakref.ref(job)
        job.start()
        del job
        gc.collect()
        self.assertIsNotNone(ref())
        release.set()
        self.assertTrue(wait_for(self.app, lambda: ref() is None or not ref().isRunning()))
        worker.stop_all()
        flush(self.app)


# ------------------------------------------------------------
class UiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.engine = Engine()
        self.loader = Loader()

    def tearDown(self):
        self.loader.stop()
        self.engine.close()
        self.engine.deleteLater()
        flush(self.app)

    def test_engine_close_is_idempotent(self):
        engine = Engine()
        engine.close()
        engine.close()
        engine.deleteLater()
        flush(self.app)

    def test_playlist_window_cycles_do_not_leak_or_keep_connections(self):
        refs = []
        for index in range(5):
            node = Node("playlist", f"List {index}", tracks=[Track(title="A"), Track(title="B")])
            window = PlaylistWindow(node, self.engine, self.loader, {})
            window.show()
            self.app.processEvents()
            self.assertTrue(window.close())
            refs.append(weakref.ref(window))
            del window, node
            flush(self.app)
        gc.collect()
        flush(self.app)
        self.assertEqual([ref() for ref in refs], [None] * 5)

    def test_closing_playlist_window_stops_its_timers_and_event_filter(self):
        node = Node("playlist", "List", tracks=[Track(title="A")])
        window = PlaylistWindow(node, self.engine, self.loader, {})
        window.show()
        self.assertTrue(window.player.timer.isActive())
        window.close()
        self.assertFalse(window.player.timer.isActive())
        flush(self.app)

    def test_metadata_signal_after_window_closed_is_harmless(self):
        track = Track(title="A")
        window = PlaylistWindow(Node("playlist", "List", tracks=[track]), self.engine, self.loader, {})
        window.close()
        del window
        flush(self.app)
        gc.collect()
        self.engine.metadata_changed.emit(track)
        flush(self.app)

    def test_timeline_close_stops_follow_timer_and_loader_connections(self):
        track = Track(title="A")
        timeline = Timeline(track, "", self.loader, {}, self.engine)
        timeline.show()
        self.assertTrue(timeline.follow.isActive())
        ref = weakref.ref(timeline)
        timeline.close()
        self.assertFalse(timeline.follow.isActive())
        del timeline
        flush(self.app)
        gc.collect()
        self.assertIsNone(ref())

    def test_metronome_close_releases_timer_and_sink(self):
        metronome = self.engine.metronome
        metronome.set_enabled(False)
        self.engine.close()
        self.assertFalse(metronome.timer.isActive())
        self.assertIsNone(getattr(metronome, "sink", None))


# --------------------------------------------------------
class TemporaryFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio = self.root / "song.mp3"
        self.audio.write_bytes(b"audio")

    def tearDown(self):
        self.tmp.cleanup()

    def leftovers(self):
        return sorted(p.name for p in self.root.iterdir() if p.name.startswith(".bb-"))

    def document(self):
        return project.dump([Node("playlist", "List", tracks=[Track(path=str(self.audio))])], {})

    def test_cancelled_backup_leaves_no_temporary_files_and_no_archive(self):
        cancel = threading.Event()
        cancel.set()
        target = self.root / "out.zip"
        with self.assertRaises(Cancelled):
            backup.create(str(target), self.document(), progress=lambda *_: None, cancel=cancel)
        self.assertFalse(target.exists())
        self.assertEqual(self.leftovers(), [])

    def test_failed_backup_with_missing_audio_leaves_nothing(self):
        document = self.document()
        self.audio.unlink()
        target = self.root / "out.zip"
        with self.assertRaises(ERRORS):
            backup.create(str(target), document)
        self.assertFalse(target.exists())
        self.assertEqual(self.leftovers(), [])

    def test_failed_restore_cleans_staging_folder(self):
        target = self.root / "out.zip"
        backup.create(str(target), self.document())
        data = bytearray(target.read_bytes())
        data[len(data) // 2] ^= 0xFF
        target.write_bytes(bytes(data))
        destination = self.root / "restored"
        with self.assertRaises(ERRORS):
            backup.restore(str(target), str(destination))
        self.assertFalse(destination.exists())
        self.assertEqual(self.leftovers(), [])

    def test_cancelled_restore_cleans_staging_folder(self):
        target = self.root / "out.zip"
        backup.create(str(target), self.document())
        cancel = threading.Event()
        cancel.set()
        destination = self.root / "restored"
        with self.assertRaises(Cancelled):
            backup.restore(str(target), str(destination), progress=lambda *_: None, cancel=cancel)
        self.assertFalse(destination.exists())
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
