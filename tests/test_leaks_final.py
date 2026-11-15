# leaks found by measuring (tests/center/memory.py and the scratch runs of the final pass): what is
# kept after a window, a load, an export, an analysis or a write is gone, caches have a ceiling,
# threads and files do not pile up. Few cycles and wide margins, a leak here is a steady growth

import gc
import os
import random
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from djapp import cachebackup, export, settings, tagwrite
from djapp.export import files
from djapp.export.manifest import Cancelled
from djapp.model import Node, Track, unique_tracks
from djapp.ui import cuehistory, main_window, worker, zoomwave
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow, load_job
from djapp.ui.waveform import KEEP, KEEP_HI, Loader
from PySide6.QtCore import QCoreApplication, QEvent, QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from tests.center import synth
from tests.test_key_support import tmp


# ------------------------------------------------------------
def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def collection(root: Path):
    return synth.build(root, 3, count=12, missing=2)


def tracks_alive() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if type(o) is Track)


def part_files(root: Path) -> list[str]:
    return sorted(
        p.name
        for p in root.rglob("*")
        if p.is_file() and (p.suffix in (".part", ".tmp") or p.name.startswith(".bb-"))
    )


# ------------------------------------------------------------
# the window that is left open at the end: it has no parent, and a visible window without a
# parent keeps the application running when the main window is gone, invisible, holding its files
class QuitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_closing_the_main_window_closes_the_log_window(self):
        cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False)
        log = LogWindow()
        with (
            patch.object(settings, "save"),
            patch.object(main_window, "fs_type", lambda path: "FAT32"),
        ):
            main = MainWindow(cfg, log)
            main.show()
            main.show_log()
            flush(self.app)
            self.assertTrue(log.isVisible())
            self.assertTrue(main.close())
            flush(self.app)
        self.assertFalse(log.isVisible())
        self.assertFalse(main.isVisible())
        main.deleteLater()
        log.deleteLater()
        flush(self.app)


# ------------------------------------------------------------
# every cache and every history has a ceiling
class CeilingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cue_history_keeps_a_few_tracks_and_a_few_steps(self):
        cuehistory._hist.clear()
        cuehistory._edits.clear()
        tracks = [Track(title=f"T{i}") for i in range(cuehistory.TRACKS + 20)]
        for t in tracks:
            for k in range(cuehistory.DEPTH + 30):
                before = cuehistory.snap(t)
                t.bpm = 100.0 + k
                cuehistory.record(t, before)
        self.assertLessEqual(len(cuehistory._hist), cuehistory.TRACKS)
        self.assertLessEqual(len(cuehistory._edits), cuehistory.TRACKS)
        self.assertTrue(
            all(len(u) <= cuehistory.DEPTH for _, u, _ in cuehistory._hist.values())
        )
        cuehistory._hist.clear()
        cuehistory._edits.clear()

    def test_the_waveform_pixmap_cache_stays_small(self):
        image = QImage(60, 20, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        rng = np.random.default_rng(1)
        for i in range(60):
            wave = rng.random((50, 3)).astype(np.float32)
            zoomwave.paint_wave(painter, QRectF(0, 0, 60, 20), wave, "RGB")
            zoomwave.paint_wave(
                painter, QRectF(0, 0, 60, 20), wave, "RGB", 0.1 * (i % 5), 1.0
            )
            self.assertLessEqual(len(zoomwave._pix), 17)
        painter.end()
        zoomwave._pix.clear()

    def test_the_log_window_drops_old_lines(self):
        window = LogWindow()
        for i in range(window.MAX + 300):
            window.add(f"line {i}", "INFO")
        self.assertEqual(window.text.document().blockCount(), window.MAX)
        window.deleteLater()
        flush(self.app)

    def test_the_waveform_loader_keeps_a_bounded_number_of_waves(self):
        loader = Loader()
        for i in range(KEEP + 40):
            loader.keep(f"p{i}", [(0.1, 0.2, 0.3)])
        for i in range(KEEP_HI + 10):
            loader.keep_hi(f"p{i}", np.zeros((4, 3), dtype=np.float32))
        self.assertEqual(len(loader.mem), KEEP)
        self.assertEqual(len(loader.hi), KEEP_HI)
        loader.deleteLater()
        flush(self.app)


# ------------------------------------------------------------
# a collection read again and again leaves no track behind
class LoadTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.col = collection(self.root)

    def test_loading_a_collection_many_times_does_not_keep_its_tracks(self):
        def load_all():
            for path in (self.col.nml, self.col.rbxml, self.col.project, self.col.m3u):
                nodes, _missing, _extra = load_job(str(path), str(self.col.audio))
                self.assertTrue(unique_tracks(nodes))

        load_all()
        base = tracks_alive()
        for _ in range(8):
            load_all()
        self.assertLess(tracks_alive() - base, 60)


# ------------------------------------------------------------
# the copy, whatever ends it: no .part file, no reader thread, no pool
class CopyTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.col = collection(self.root)
        self.jobs = [
            (str(p), str(self.root / "copy" / f"{k}.wav"))
            for k, p in enumerate(self.col.files[:4])
        ]

    def cycle(self):
        files.copy_pipelined(self.jobs, True)
        stop = threading.Event()
        calls = [0]

        def progress(*_):
            calls[0] += 1
            if calls[0] == 2:
                stop.set()

        for job in self.jobs:
            Path(job[1]).unlink(missing_ok=True)
        with self.assertRaises(Cancelled):
            files.copy_pipelined(self.jobs, True, stop, progress)
        with patch.object(files, "replace", side_effect=OSError("injected")):
            errors = files.copy_pipelined(self.jobs, True)
        self.assertTrue(errors)

    def test_copy_cycles_leave_no_thread_and_no_part_file(self):
        self.cycle()
        before = threading.active_count()
        for _ in range(6):
            self.cycle()
        self.assertEqual(threading.active_count(), before)
        self.assertEqual(part_files(self.root / "copy"), [])

    def test_export_with_failures_and_back_leaves_no_thread_and_no_part_file(self):
        cfg = dict(
            settings.DEFAULTS,
            verify_copy=True,
            copy_music=True,
            anlz_processes=1,
            key_ask=False,
        )
        tracks = [t for t in self.col.nodes[0].tracks if "gone" not in t.path][:3]
        real = files.copy_one
        out = self.root / "export"

        def flaky(*args, **kwargs):
            flaky.n += 1
            if flaky.n % 2 == 0:
                raise OSError("injected")
            return real(*args, **kwargs)

        flaky.n = 0

        def cycle():
            report = export.run(
                "Traktor NML", [Node("playlist", "T", tracks=tracks)], str(out), cfg
            )
            self.assertFalse(report.errors)
            with patch.object(files, "copy_one", flaky):
                export.run(
                    "M3U", [Node("playlist", "T", tracks=tracks)], str(out / "m3u"), cfg
                )

        cycle()
        before = threading.active_count()
        for _ in range(4):
            cycle()
        self.assertEqual(threading.active_count(), before)
        self.assertEqual(part_files(out), [])


# ------------------------------------------------------------
# the writers close what they open: the file can be removed and the handles do not pile up
class HandleTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.col = collection(self.root)

    def handles(self) -> int:
        psutil = __import__("psutil")
        proc = psutil.Process()
        return proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()

    def test_tag_writes_close_their_files(self):
        target = self.root / "tagged.wav"
        target.write_bytes(self.col.files[0].read_bytes())

        def cycle(i):
            tagwrite.write(
                str(target), {"title": f"T{i}", "artist": "A", "year": "2020"}
            )

        cycle(0)
        before = self.handles()
        for i in range(1, 12):
            cycle(i)
        self.assertLess(self.handles() - before, 8)
        self.assertEqual(part_files(self.root), [])
        for leftover in self.root.glob("*.bak"):
            leftover.unlink()
        target.unlink()

    def test_cache_backups_close_their_files(self):
        cache = self.root / "cache"
        for sub, ext in (("anlz", "DAT"), ("anlz", "EXT"), ("wave", "bin")):
            (cache / sub).mkdir(parents=True, exist_ok=True)
            for k in range(4):
                (cache / sub / f"{k:040x}.{ext}").write_bytes(os.urandom(500))
        archive = self.root / "cache.zip"

        def cycle():
            cachebackup.create(cache, archive)
            cachebackup.verify(archive)
            cachebackup.clear(cache)
            cachebackup.restore(archive, cache)
            archive.unlink()
            Path(str(archive) + ".sha256").unlink(missing_ok=True)

        cycle()
        before = self.handles()
        for _ in range(10):
            cycle()
        self.assertLess(self.handles() - before, 8)
        self.assertEqual(part_files(self.root), [])


# ------------------------------------------------------------
# decoding and analysing the same audio again and again keeps no growing buffer
class AnalysisTests(unittest.TestCase):
    def test_repeated_analysis_does_not_grow_the_heap(self):
        from djapp import beatdetect
        from djapp.ui import waveform

        temp, root = tmp()
        self.addCleanup(temp.cleanup)
        audio = root / "long.wav"
        synth.write_wav(audio, 10.0, random.Random(5))

        def cycle():
            waveform.compute(str(audio))
            waveform.compute_hi(str(audio))
            beatdetect.analyze(str(audio))

        with patch.object(waveform, "cache_dir", lambda: root):
            for _ in range(3):
                cycle()
            gc.collect()
            started = not tracemalloc.is_tracing()
            if started:
                tracemalloc.start()
            try:
                base = tracemalloc.get_traced_memory()[0]
                for _ in range(8):
                    cycle()
                gc.collect()
                grown = tracemalloc.get_traced_memory()[0] - base
            finally:
                if started:
                    tracemalloc.stop()
        self.assertLess(grown, 2_000_000)


# ------------------------------------------------------------
class JobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_jobs_that_end_each_way_leave_the_registry_empty(self):
        results = []

        def fine(progress, cancel):
            return 1

        def broken(progress, cancel):
            raise ValueError("injected")

        def stopped(progress, cancel):
            raise Cancelled()

        with self.assertLogs("djapp.ui.worker", level="ERROR"):
            for fn in (fine, broken, stopped) * 4:
                job = worker.Job(fn)
                job.done.connect(results.append)
                job.start()
                end = time.monotonic() + 10
                while (
                    job.isRunning() or job in worker._alive
                ) and time.monotonic() < end:
                    self.app.processEvents()
                    time.sleep(0.002)
                job.deleteLater()
                flush(self.app)
        self.assertEqual(len(results), 4)
        self.assertFalse(worker.has_active_jobs())


if __name__ == "__main__":
    unittest.main()
