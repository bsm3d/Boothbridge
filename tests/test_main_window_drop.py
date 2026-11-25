# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks dropping files or folders on the main window, the tree tooltips and the theme checkbox indicators.

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings, sources
from djapp.model import Node
from djapp.ui import main_window, theme
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.test_key_support import export_to, make_track, tmp


# ------------------------------------------------------------
def wait_for(app, condition, seconds=20):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


# ------------------------------------------------------------
def drop(window, *paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.dropEvent(event)
    return event

class DropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        for name in ("information", "warning", "critical", "question"):
            p = patch.object(QMessageBox, name, return_value=QMessageBox.Yes)
            p.start()
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

    def settle(self):
        wait_for(self.app, lambda: self.main.job is None)
        self.app.processEvents()

    def test_the_window_takes_drops(self):
        self.assertTrue(self.main.acceptDrops())

    def test_a_collection_file_opens_like_open_project_collection(self):
        m3u = self.root / "set.m3u"
        audio = self.root / "a.wav"
        audio.write_bytes(b"RIFF")
        m3u.write_text(f"#EXTM3U\n{audio}\n", encoding="utf-8")
        event = drop(self.main, m3u)
        self.settle()
        self.assertTrue(event.isAccepted())
        self.assertEqual(Path(self.main.src.text()), m3u)
        self.assertTrue(self.main.nodes)

    def test_a_cdj_key_opens_as_a_key_and_so_does_a_folder_inside_it(self):
        music, cache, key = self.root / "music", self.root / "cache", self.root / "key"
        music.mkdir()
        cache.mkdir()
        export_to([Node("playlist", "Set", tracks=[make_track(music, "a", 1)])], key, cache)
        for dropped in (key, key / "PIONEER" / "rekordbox"):
            with self.subTest(dropped=str(dropped)):
                self.main.nodes = []
                drop(self.main, dropped)
                self.settle()
                self.assertIn("read from a CDJ key", self.main.info.text())
                self.assertEqual(Path(self.main.src.text()), key)

    def test_a_music_folder_loads_as_a_folder(self):
        (self.root / "folder").mkdir()
        (self.root / "folder" / "a.wav").write_bytes(b"RIFF")
        with patch.object(sources, "load", lambda p, root="", progress=None, notes=None: [Node("playlist", "F")]) as load:
            drop(self.main, self.root / "folder")
            self.settle()
        self.assertEqual(Path(self.main.src.text()), self.root / "folder")
        self.assertTrue(load is not None)

    def test_nothing_happens_while_a_job_runs(self):
        self.main.job = object()
        try:
            drop(self.main, self.root)
        finally:
            self.main.job = None
        self.assertEqual(self.main.src.text(), "")
        self.assertIn("Wait for the current job", self.main.status.text())

    def test_the_drop_asks_before_throwing_away_unsaved_work(self):
        self.main.nodes = [Node("playlist", "Mine")]
        self.main.dirty = True
        with patch.object(self.main, "ask_discard", return_value=False):
            drop(self.main, self.root)
        self.assertEqual(self.main.src.text(), "")

    def test_something_that_is_not_a_local_file_is_ignored(self):
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/x.nml")])
        event = QDropEvent(QPointF(1, 1), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.main.dropEvent(event)
        self.assertEqual(self.main.src.text(), "")


# ------------------------------------------------------------
class WordsAndLookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(settings, "save"):
            self.main = MainWindow(dict(settings.DEFAULTS, auto_load=False, confirm_exit=False), LogWindow())
        self.addCleanup(self.main.deleteLater)
        self.addCleanup(self.main.key_timer.stop)

    def test_the_tree_says_that_nothing_ticked_exports_everything(self):
        tip = self.main.tree.toolTip()
        self.assertIn("With none ticked, all of them are exported", tip)
        self.assertIn("right click", tip)

    def test_the_tooltip_of_the_check_makes_no_claim_about_time(self):
        tip = self.main.verify.toolTip()
        self.assertIn("SHA-256", tip)
        self.assertNotIn("as long", tip)
        self.assertNotIn("minutes", tip)

    def test_checkboxes_and_tree_indicators_are_drawn_on_the_dark_theme(self):
        for part in ("QCheckBox::indicator", "QAbstractItemView::indicator", ":checked", ":indeterminate",
                     "check.svg", "partial.svg"):
            self.assertIn(part, theme.STYLE)
        for name in ("check.svg", "partial.svg"):
            self.assertTrue((Path(theme.ICONS) / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
