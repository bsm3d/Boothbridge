import os
import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings
from djapp.export.cdj import anlz, usb, validate
from djapp.model import Cue, Node, Track
from djapp.ui import waveform
from djapp.ui.details import PlaylistWindow
from djapp.ui.main_window import MainWindow
from djapp.ui.worker import Job
from PySide6.QtWidgets import QApplication, QMessageBox


# ------------------------------------------------------------
class ReleaseReviewFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_colliding_tracks_keep_distinct_valid_analysis_files(self):
        paths = ["/Contents/Artist 02/Album 02/Track 0002.wav",
                 "/Contents/Artist 02/Album 09/Track 0102.wav"]
        self.assertEqual(anlz.anlz_dir(paths[0]), anlz.anlz_dir(paths[1]))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tracks = []
            for number, album in ((2, "Album 02"), (102, "Album 09")):
                source = root / f"Track {number:04}.wav"
                with wave.open(str(source), "wb") as stream:
                    stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                    stream.writeframes(b"\x00\x10" * 8000)
                tracks.append(Track(path=str(source), artist="Artist 02", album=album,
                                    title=f"Track {number}", bpm=120, duration=1))
            cache = root / "cache"
            cache.mkdir()
            output = root / "export"
            with patch.object(usb, "cache_dir", return_value=cache), \
                    patch.object(usb, "ProcessPoolExecutor", ThreadPoolExecutor):
                report = usb.export([Node(kind="playlist", name="Demo", tracks=tracks)],
                                    str(output), verify=True, processes=2)
            self.assertFalse(report.errors)
            files = list((output / "PIONEER" / "USBANLZ").rglob("ANLZ0000.DAT"))
            self.assertEqual(len(files), 2)
            for source in paths:
                matches = 0
                for file in files:
                    try:
                        validate.anlz(file.read_bytes(), source, "DAT")
                    except ValueError:
                        continue
                    matches += 1
                self.assertEqual(matches, 1)

    def test_hotcue_colour_changes_analysis_cache_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.wav"
            path.write_bytes(b"demo")
            cue = Cue(hotcue=0, color="#FF0000")
            track = Track(path=str(path), cues=[cue])
            before = usb.cache_key(track, "/Contents/demo.wav")
            cue.color = "#0000FF"
            self.assertNotEqual(before, usb.cache_key(track, "/Contents/demo.wav"))

    def test_analysis_directory_space_exhaustion_is_explicit(self):
        used = Mock()
        used.__contains__ = Mock(return_value=True)
        with self.assertRaisesRegex(ValueError, "exhausted"):
            anlz.anlz_dir("/Contents/demo.wav", used)
        used.add.assert_not_called()

    def main_window(self):
        main = MainWindow(dict(settings.DEFAULTS, auto_load=False), None)

        def close():
            main.dirty = False
            with patch.object(settings, "save"):
                main.close()
            main.deleteLater()
            self.app.processEvents()

        self.addCleanup(close)
        return main

    def test_duplicate_shared_reference_keeps_first_occurrence_and_undo_restores(self):
        main = self.main_window()
        track = Track(title="Demo", audio_id="demo")
        node = Node(kind="playlist", name="Test", tracks=[track, track])
        window = PlaylistWindow(node, main.engine, main.loader, main.cfg, parent=main)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            window.remove_duplicates()
        self.assertEqual(node.tracks, [track])
        window.undo_cues()
        self.assertEqual(node.tracks, [track, track])
        window.close()

    def test_existing_editor_tracks_unsaved_changes_after_tree_rebuild(self):
        main = self.main_window()
        node = Node(kind="playlist", name="Demo", tracks=[Track(title="Demo")])
        main.nodes = [node]
        main.fill_tree()
        main.details(main.tree.topLevelItem(0))
        window = main.findChildren(PlaylistWindow)[0]
        main.nodes.append(Node(kind="playlist", name="Other"))
        main.fill_tree()
        main.set_dirty(False)
        node.tracks.clear()
        with patch("sys.excepthook") as errors:
            window.edited.emit()
            errors.assert_not_called()
        self.assertTrue(main.dirty)
        self.assertEqual(main.tree.topLevelItem(0).text(1), "0")
        window.close()

    def test_restore_acceptance_after_finished_signal_reloads(self):
        main = self.main_window()
        main.job = Job(lambda progress, cancel: None)

        def answer(*args):
            main.job_over()
            return QMessageBox.Yes

        with patch.object(QMessageBox, "question", side_effect=answer), \
                patch.object(main, "reload") as reload:
            main.restore_finished("restored-project.json")
            self.app.processEvents()
            reload.assert_called_once()
        self.assertEqual(main.src.text(), "restored-project.json")
        self.assertFalse(main.again)

    def test_reload_closes_old_playlist_editor_before_loading(self):
        main = self.main_window()
        node = Node(kind="playlist", name="Old")
        main.nodes = [node]
        main.fill_tree()
        main.details(main.tree.topLevelItem(0))
        editor = main.findChildren(PlaylistWindow)[0]
        editor.show()
        with tempfile.TemporaryDirectory() as folder:
            main.src.setText(folder)
            with patch("djapp.ui.main_window.Job", return_value=Mock()) as job:
                main.reload()
            self.assertFalse(editor.isVisible())
            job.assert_called_once()
            main.job = None

    def test_reload_refused_when_editor_detection_cannot_close(self):
        main = self.main_window()
        editor = PlaylistWindow(Node(kind="playlist", name="Busy"),
                                main.engine, main.loader, main.cfg, parent=main)
        editor.detect_job = Mock()
        editor.detect_job.wait.return_value = False
        with tempfile.TemporaryDirectory() as folder:
            main.src.setText(folder)
            with patch("djapp.ui.main_window.Job") as job:
                main.reload()
            job.assert_not_called()
            self.assertIsNone(main.job)
            self.assertFalse(editor.closing)
            self.assertIn("Wait", main.status.text())
        editor.detect_job = None
        editor.close()

    def test_concurrent_waveform_cache_writes_use_distinct_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            overview, detail = root / "demo.bin", root / "demo.hi1000"
            barrier = threading.Barrier(2)
            replace = os.replace
            names = []

            def interleaved(source, destination):
                names.append(source)
                barrier.wait(timeout=5)
                replace(source, destination)

            with patch.object(waveform.os, "replace", side_effect=interleaved), \
                    ThreadPoolExecutor(2) as pool:
                jobs = [pool.submit(waveform.write_cache, overview, b"overview"),
                        pool.submit(waveform.write_cache, detail, b"detail")]
                for job in jobs:
                    job.result(timeout=10)
            self.assertEqual(len(set(names)), 2)
            self.assertEqual(overview.read_bytes(), b"overview")
            self.assertEqual(detail.read_bytes(), b"detail")
            self.assertEqual(set(root.iterdir()), {overview, detail})

    def test_failed_waveform_replace_preserves_cache_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wave.bin"
            path.write_bytes(b"previous")
            with patch.object(waveform.os, "replace", side_effect=OSError("busy")), \
                    self.assertRaises(OSError):
                waveform.write_cache(path, b"new")
            self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
