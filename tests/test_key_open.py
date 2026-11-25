# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks File > Open a CDJ USB key on keys the program wrote: allowed folders, read warnings and Cancel.

import contextlib
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from djapp import export, settings
from djapp.export.cdj import usb
from djapp.model import Node
from djapp.ui import main_window
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow, key_notes, key_root
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from tests.test_key_support import export_to, make_track, tmp


# ----------------------------------------------------------
def wait_for(app, condition, seconds=20):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


class KeyRootTests(unittest.TestCase):
    def test_folders_inside_the_key_give_its_root(self):
        root = os.path.join(os.path.abspath(os.sep), "Volumes", "KEY")
        for inside in ("PIONEER", "PIONEER/rekordbox", "pioneer/USBANLZ/P01A", "PIONEER/Artwork/00001", "Contents",
                       "Contents/Artist/Album", "CONTENTS"):
            with self.subTest(inside):
                self.assertEqual(key_root(os.path.join(root, *inside.split("/"))), root)

    def test_the_root_and_other_folders_are_left_alone(self):
        root = os.path.join(os.path.abspath(os.sep), "Volumes", "KEY")
        for path in (root, os.path.join(root, "Music"), os.path.abspath(os.sep), "relative"):
            with self.subTest(path):
                self.assertEqual(key_root(path), path)

    def test_a_drive_root_stays_a_drive_root(self):
        self.assertEqual(key_root(os.path.join(os.path.abspath(os.sep), "PIONEER")), os.path.abspath(os.sep))

    def test_notes(self):
        self.assertEqual(key_notes({}), "")
        self.assertEqual(key_notes({"key_warnings": 1}), ", 1 warning (see the log)")
        self.assertEqual(key_notes({"key_warnings": 4, "key_unlisted": 2}),
                         ", 4 warnings (see the log), 2 tracks in no playlist not loaded")


@contextlib.contextmanager
def inline(cache: Path):
    with patch.object(usb, "ProcessPoolExecutor", ThreadPoolExecutor), patch.object(usb, "cache_dir", return_value=cache):
        yield


class OpenRealKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        music, cache = self.root / "music", self.root / "cache"
        music.mkdir()
        cache.mkdir()
        self.key = self.root / "key"
        tracks = [make_track(music, f"t{i}", i + 1) for i in range(3)]
        export_to([Node("playlist", "Set", tracks=tracks)], self.key, cache)
        self.boxes = {}
        for name in ("information", "warning", "critical", "question"):
            p = patch.object(QMessageBox, name, return_value=QMessageBox.Ok)
            self.boxes[name] = p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(settings, "save"), patch.object(main_window, "fs_type", lambda path: "FAT32")):
            p.start()
            self.addCleanup(p.stop)
        cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False, key_ask=True, key_mode="keep")
        self.main = MainWindow(cfg, LogWindow())
        self.addCleanup(self.close_main)

    def close_main(self):
        self.main.key_timer.stop()
        wait_for(self.app, lambda: self.main.job is None)
        self.main.close()
        self.main.deleteLater()
        self.app.processEvents()

    def open(self, path):
        self.main.src.setText("before.nml")
        self.main.open_key(str(path))
        wait_for(self.app, lambda: self.main.job is None)
        self.app.processEvents()

    def test_the_root_of_the_key_opens(self):
        self.open(self.key)
        self.assertIn("1 playlists, 3 tracks, read from a CDJ key", self.main.info.text())
        self.assertNotIn("warning", self.main.info.text())

    def test_a_folder_inside_the_key_opens_the_key(self):
        for inside in (("PIONEER",), ("PIONEER", "rekordbox"), ("PIONEER", "USBANLZ"), ("Contents",)):
            with self.subTest(inside):
                self.main.nodes = []
                self.open(self.key.joinpath(*inside))
                self.assertIn("3 tracks, read from a CDJ key", self.main.info.text())
                self.assertEqual(Path(self.main.src.text()), self.key)
        self.boxes["critical"].assert_not_called()

    def test_damaged_analysis_is_said_in_the_window_not_only_in_the_log(self):
        victim = next((self.key / "PIONEER" / "USBANLZ").rglob("ANLZ0000.EXT"))
        victim.write_bytes(b"not an analysis file")
        self.open(self.key)
        self.assertIn("1 warning (see the log)", self.main.info.text())

    def test_a_folder_without_database_has_its_own_title_and_changes_nothing(self):
        self.open(self.root / "music")
        title, message = self.boxes["critical"].call_args.args[1:3]
        self.assertEqual(title, "Cannot read the key")
        self.assertIn("no CDJ database", message)
        self.assertEqual(self.main.src.text(), "before.nml")

    def test_a_collection_that_fails_keeps_its_own_title(self):
        bad = self.root / "bad.nml"
        bad.write_text("<NML><not closed")
        self.main.src.setText(str(bad))
        self.main.reload()
        wait_for(self.app, lambda: self.main.job is None)
        self.assertEqual(self.boxes["critical"].call_args.args[1], "Cannot read the collection")

    def test_cancel_stops_the_check_of_the_key_that_comes_first(self):
        started = threading.Event()

        def slow(path, cancel=None):
            started.set()
            while cancel is not None and not cancel.is_set():
                time.sleep(0.01)
            raise export.manifest.Cancelled()

        with patch.object(export, "key_summary", slow):
            self.main.open_key(str(self.key))
            wait_for(self.app, started.is_set)
            self.main.cancel()
            wait_for(self.app, lambda: self.main.job is None)
        self.assertEqual(self.main.status.text(), "Loading cancelled")

    def test_the_picker_gets_the_key_root_when_a_folder_inside_is_chosen(self):
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.key / "PIONEER" / "rekordbox")):
            self.main.open_key()
            wait_for(self.app, lambda: self.main.job is None)
        self.assertEqual(Path(self.main.src.text()), self.key)


if __name__ == "__main__":
    unittest.main()
