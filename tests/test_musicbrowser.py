# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the music browser: its tree, copied data and actions, run offscreen with temporary folders.

import copy
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import keys, library, settings
from djapp.model import Node, Track
from djapp.ui import theme
from djapp.ui.details import PlaylistWindow
from djapp.ui.dialogs import LogWindow, Options
from djapp.ui.main_window import MainWindow
from djapp.ui.musicbrowser import HEADERS, LibraryFilter, MusicBrowser
from djapp.ui.player import Engine
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.test_library import write_wave


# --------------------------------------------------------
class MusicBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_wave(self.root / "one.wav")
        write_wave(self.root / "two.wav")
        self.cfg = dict(settings.DEFAULTS, library_root=str(self.root), auto_load=False)
        self.engine = Engine()
        self.browser = MusicBrowser(self.engine, self.cfg)

    def tearDown(self):
        self.browser.close()
        self.finish()
        self.browser.deleteLater()
        self.engine.close()
        self.engine.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.tmp.cleanup()

    def finish(self):
        deadline = time.monotonic() + 5
        while self.browser.job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.browser.job)

    def scan(self):
        self.browser.refresh()
        self.assertFalse(self.browser.refresh_button.isEnabled())
        self.finish()
        self.assertTrue(self.browser.refresh_button.isEnabled())

    def test_floating_window_can_move_resize_and_restore_geometry(self):
        self.browser.show()
        self.browser.move(80, 70)
        self.browser.resize(760, 510)
        self.browser.close()
        self.assertTrue(self.cfg["library_geometry"])
        restored = MusicBrowser(self.engine, self.cfg)
        self.addCleanup(restored.deleteLater)
        self.assertEqual(restored.size(), self.browser.size())
        self.assertTrue(restored.isWindow())
        self.assertFalse(restored.isModal())

    def test_refresh_reads_files_and_finds_additions_and_removals(self):
        self.scan()
        self.assertEqual(self.browser.model.rowCount(), 2)
        write_wave(self.root / "new.wav")
        (self.root / "one.wav").unlink()
        self.scan()
        self.assertEqual({t.title for t in self.browser.model.tracks}, {"two", "new"})

    def test_filter_and_numeric_sort(self):
        self.scan()
        self.browser.search.setText("two")
        self.browser.filter()
        self.assertEqual(self.browser.proxy.rowCount(), 1)
        self.assertEqual(self.browser.proxy.index(0, 1).data(), "two")
        self.browser.search.clear()
        self.browser.filter()
        self.browser.model.tracks[0].bpm = 90
        self.browser.model.tracks[1].bpm = 120
        self.browser.proxy.sort(4, Qt.DescendingOrder)
        self.assertEqual(self.browser.proxy.index(0, 4).data(), "120.00")

    def test_click_preview_uses_shared_engine_and_reports_missing_audio(self):
        self.scan()
        index = self.browser.proxy.index(0, 0)
        track = index.data(Qt.UserRole)
        with patch.object(self.engine, "play", return_value=True) as play:
            self.browser.table.clicked.emit(index)
        play.assert_called_once_with(track.path, track)
        self.assertIn(track.title, self.browser.now.text())
        Path(track.path).unlink()
        with patch.object(self.engine, "play") as play:
            self.browser.preview(index)
        play.assert_not_called()
        self.assertIn("unavailable", self.browser.status.text())

    def test_close_does_not_stop_a_playlist_preview(self):
        self.scan()
        self.browser.preview_track = self.browser.model.tracks[0]
        self.engine.timing_track = self.browser.model.tracks[1]
        with patch.object(self.engine, "stop") as stop:
            self.browser.close()
        stop.assert_not_called()

    def test_close_stops_its_own_preview(self):
        self.scan()
        track = self.browser.model.tracks[0]
        self.browser.preview_track = self.engine.timing_track = track
        with patch.object(self.engine, "stop") as stop:
            self.browser.close()
        stop.assert_called_once()

    def test_browser_does_not_inherit_playlist_loop_or_automatic_crossfade(self):
        self.scan()
        track = self.browser.model.tracks[0]
        loader = Loader()
        with patch.object(loader, "prefetch"), patch.object(loader, "get", return_value=None):
            playlist = PlaylistWindow(Node("playlist", "Playing", tracks=[copy.copy(track)]),
                                      self.engine, loader, self.cfg)
            playlist.player.load(playlist.node.tracks[0], track.path)
        try:
            self.engine.loop = (playlist.node.tracks[0], object())
            with patch.object(self.engine, "play", return_value=True):
                self.browser.preview(self.browser.proxy.index(0, 0))
            self.assertIsNone(self.engine.loop)
            self.engine.timing_track = track
            self.engine.path = track.path
            playlist.player.fading = 1
            with patch.object(playlist.player, "step_fade") as fade:
                playlist.player.tick()
            fade.assert_not_called()
            self.assertEqual(playlist.player.fading, 0)
            with patch.object(self.engine, "stop") as stop:
                playlist.close()
            stop.assert_not_called()
        finally:
            playlist.close()
            loader.stop()

    def test_styled_browser_fits_a_narrow_desktop_window(self):
        self.browser.setStyleSheet(theme.STYLE)
        self.browser.resize(760, 500)
        self.browser.show()
        self.app.processEvents()
        self.assertLessEqual(self.browser.width(), 760)
        self.assertTrue(self.browser.rect().contains(self.browser.cancel_button.geometry().bottomRight()))

    def test_multi_track_drag_copies_file_urls_to_playlist_with_undo(self):
        self.scan()
        indexes = [self.browser.proxy.index(row, col) for row in range(2) for col in range(3)]
        mime = self.browser.proxy.mimeData(indexes)
        self.assertEqual(len(mime.urls()), 2)
        self.assertEqual(self.browser.model.supportedDragActions(), Qt.CopyAction)
        loader = Loader()
        with patch.object(loader, "prefetch"):
            playlist = PlaylistWindow(Node("playlist", "New"), self.engine, loader, self.cfg)
            try:
                drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
                playlist.table.dropEvent(drop)
                self.assertTrue(drop.isAccepted())
                self.assertEqual(len(playlist.node.tracks), 2)
                playlist.table.dropEvent(QDropEvent(QPointF(10, 10), Qt.CopyAction, mime,
                                                   Qt.LeftButton, Qt.NoModifier))
                self.assertEqual(len(playlist.node.tracks), 2)
                playlist.undo_cues()
                self.assertEqual(playlist.node.tracks, [])
                playlist.undo_cues(True)
                self.assertEqual(len(playlist.node.tracks), 2)
            finally:
                playlist.close()
                loader.stop()
        self.assertTrue(all(Path(t.path).exists() for t in self.browser.model.tracks))

    def test_duplicate_scan_and_filter_never_remove_files_and_refresh_clears_groups(self):
        self.scan()
        self.browser.scan_duplicates()
        self.finish()
        self.assertEqual(len(self.browser.model.groups), 2)
        self.browser.only_duplicates.setChecked(True)
        self.assertEqual(self.browser.proxy.rowCount(), 2)
        self.assertTrue((self.root / "one.wav").exists())
        self.scan()
        self.assertFalse(self.browser.model.groups)
        self.assertFalse(self.browser.only_duplicates.isChecked())

    def test_failed_refresh_preserves_previous_results_and_reports_error(self):
        self.scan()
        self.browser.root = str(self.root / "missing")
        with patch.object(QMessageBox, "warning") as warning, \
                self.assertLogs("djapp.ui.worker", level="ERROR"):
            self.browser.refresh()
            self.finish()
        warning.assert_called_once()
        self.assertEqual(self.browser.model.rowCount(), 2)
        self.assertIn("Previous results kept", self.browser.status.text())

    def test_close_cancels_worker_and_discards_queued_result(self):
        def slow(_root, progress=None, cancel=None):
            cancel.wait(2)
            return library.LibraryScan([])

        self.scan()
        with patch.object(library, "scan", side_effect=slow):
            self.browser.refresh()
            self.browser.close()
            self.finish()
        self.assertEqual(self.browser.model.rowCount(), 2)
        self.assertTrue(self.browser.refresh_button.isEnabled())

    def test_root_change_cancels_old_scan_and_refreshes_the_new_folder(self):
        self.browser.show()
        other = self.root / "other"
        other.mkdir()
        write_wave(other / "new-root.wav")
        real_scan = library.scan

        def scan(root, progress=None, cancel=None):
            if root == str(self.root):
                cancel.wait(2)
                return library.LibraryScan([])
            return real_scan(root, progress=progress, cancel=cancel)

        with patch.object(library, "scan", side_effect=scan):
            self.browser.refresh()
            self.browser.set_root(str(other))
            self.finish()
        self.assertEqual([t.title for t in self.browser.model.tracks], ["new-root"])

    def test_reopening_during_cancel_restarts_initial_scan(self):
        real_scan = library.scan
        calls = []

        def scan(root, progress=None, cancel=None):
            calls.append(root)
            if len(calls) == 1:
                cancel.wait(2)
                return library.LibraryScan([])
            return real_scan(root, progress=progress, cancel=cancel)

        with patch.object(library, "scan", side_effect=scan):
            self.browser.open()
            self.browser.close()
            self.browser.open()
            self.finish()
        self.assertEqual(self.browser.model.rowCount(), 2)

    def test_sort_values_match_the_old_tuple_and_skip_the_per_call_work(self):
        rnd = random.Random(11)
        words = ["Zed", "alpha", "Beta", "", "éclair", "alpha", "ZED"]
        tracks = [Track(artist=rnd.choice(words), title=rnd.choice(words), album=rnd.choice(words),
                        path=os.path.join(rnd.choice(["/m/a", "/m/B", "/m/c"]),
                                          f"t{i}" + rnd.choice([".mp3", ".MP3", ".wav", ".flac"])),
                        duration=rnd.choice([0, 61.5, 200, 200]), bpm=rnd.choice([0, 90, 120.5, 120.5]),
                        key=rnd.choice([None, 0, 5, 12, 23])) for i in range(120)]
        model = self.browser.model
        model.replace(tracks)
        model.set_groups([[t.path for t in tracks[:5]], [t.path for t in tracks[7:9]]])

        def old(track, col):
            group = model.groups.get(track.path, 0)
            return (track.artist.casefold(), track.title.casefold(), track.album.casefold(),
                    track.duration, track.bpm, keys.name(track.key, "Camelot"),
                    os.path.splitext(track.path)[1], os.path.dirname(track.path), group)[col]

        for col in range(len(HEADERS)):
            if col == 5:
                continue
            with self.subTest(column=HEADERS[col]):
                got = [model.index(row, col).data(Qt.UserRole + 1) for row in range(len(tracks))]
                self.assertEqual(got, [old(t, col) for t in tracks])
                self.browser.proxy.sort(-1)  # ties keep the source order
                self.browser.proxy.sort(col, Qt.AscendingOrder)
                order = [self.browser.proxy.mapToSource(self.browser.proxy.index(row, 0)).row()
                         for row in range(len(tracks))]
                self.assertEqual(order, sorted(range(len(tracks)), key=lambda row: old(tracks[row], col)))
        with patch.object(os.path, "splitext", wraps=os.path.splitext) as splitext:
            self.browser.proxy.sort(1, Qt.DescendingOrder)
            self.browser.proxy.sort(0, Qt.AscendingOrder)
        splitext.assert_not_called()

    def test_key_column_sorts_by_key_index_not_by_camelot_text(self):
        tracks = [Track(title=name, key=key) for name, key in
                  (("none", None), ("10A", 23), ("2A", 15), ("8B", 0), ("12B", 4))]
        self.browser.model.replace(tracks)
        self.browser.proxy.sort(5, Qt.AscendingOrder)
        titles = [self.browser.proxy.index(row, 1).data() for row in range(5)]
        self.assertEqual(titles, ["none", "8B", "12B", "2A", "10A"])

    def test_filter_does_not_need_the_qt_6_9_filter_change_api(self):
        self.scan()
        self.browser.search.setText("two")
        def missing(_self):
            raise AttributeError("Qt before 6.9")

        with patch.object(LibraryFilter, "beginFilterChange", property(missing), create=True), \
                patch.object(LibraryFilter, "endFilterChange", property(missing), create=True):
            self.browser.filter()
            self.assertEqual(self.browser.proxy.rowCount(), 1)
            self.assertEqual(self.browser.proxy.index(0, 1).data(), "two")
            self.browser.search.clear()
            self.browser.scan_duplicates()
            self.finish()
            self.browser.only_duplicates.setChecked(True)
            self.assertEqual(self.browser.proxy.rowCount(), 2)
            self.browser.search.setText("one")
            self.browser.filter()
            self.assertEqual(self.browser.proxy.rowCount(), 1)

    # A fake engine start: the browser sees the track as its own, nothing is played
    def preview_spy(self):
        def play(path, track=None):
            self.engine.timing_track = track
            return True

        return (patch.object(self.engine, "play", side_effect=play),
                patch.object(self.engine, "toggle"))


    def test_click_then_double_click_starts_the_preview_once(self):
        self.scan()
        self.browser.show()
        self.app.processEvents()
        point = self.browser.table.visualRect(self.browser.proxy.index(0, 1)).center()
        view = self.browser.table.viewport()
        play, toggle = self.preview_spy()
        with play as play_mock, toggle as toggle_mock:
            QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, point)
            QTest.mouseDClick(view, Qt.LeftButton, Qt.NoModifier, point)
            self.app.processEvents()
        play_mock.assert_called_once()
        toggle_mock.assert_not_called()

    def test_enter_starts_the_preview_once_and_a_later_enter_toggles(self):
        self.scan()
        self.browser.show()
        self.browser.table.setCurrentIndex(self.browser.proxy.index(0, 1))
        self.browser.table.setFocus()
        play, toggle = self.preview_spy()
        with play as play_mock, toggle as toggle_mock:
            QTest.keyClick(self.browser.table, Qt.Key_Return)
            self.app.processEvents()
            play_mock.assert_called_once()
            toggle_mock.assert_not_called()
            QTest.keyClick(self.browser.table, Qt.Key_Return)
            self.app.processEvents()
            toggle_mock.assert_called_once()
            play_mock.assert_called_once()

    def test_enter_after_a_click_is_a_separate_action(self):
        self.scan()
        self.browser.show()
        self.app.processEvents()
        index = self.browser.proxy.index(0, 1)
        play, toggle = self.preview_spy()
        with play as play_mock, toggle as toggle_mock:
            self.browser.table.clicked.emit(index)
            self.app.processEvents()
            self.browser.table.activated.emit(index)
        play_mock.assert_called_once()
        toggle_mock.assert_called_once()

    def test_bad_geometry_in_the_settings_is_ignored(self):
        for bad in (5, "éè", ["ab"], "not hex at all", 1.5, True):
            with self.subTest(value=bad):
                window = MusicBrowser(self.engine, dict(self.cfg, library_geometry=bad))
                window.deleteLater()

    def test_settings_load_keeps_the_library_keys_as_text(self):
        path = self.root / "settings.json"
        for saved, expected in (({"library_root": 5, "library_geometry": 5}, ("", "")),
                                ({"library_root": None, "library_geometry": ["x"]}, ("", "")),
                                ({"library_root": str(self.root), "library_geometry": "ab"},
                                 (str(self.root), "ab"))):
            with self.subTest(saved=saved):
                path.write_text(json.dumps(saved), encoding="utf-8")
                with patch.object(settings, "CONFIG", path):
                    cfg = settings.load()
                self.assertEqual((cfg["library_root"], cfg["library_geometry"]), expected)


    def test_options_and_browser_open_with_non_text_library_settings(self):
        path = self.root / "bad.json"
        for bad in (5, None, ["x"]):
            with self.subTest(value=bad):
                path.write_text(json.dumps({"library_root": bad, "library_geometry": bad}), encoding="utf-8")
                with patch.object(settings, "CONFIG", path):
                    cfg = settings.load()
                window = MusicBrowser(self.engine, cfg)
                window.set_root(cfg["library_root"])
                options = Options(cfg)
                options.deleteLater()
                window.deleteLater()


    def test_preferences_save_when_the_stored_library_drive_is_gone(self):
        gone = str(self.root / "unplugged")
        self.cfg["library_root"] = gone
        options = Options(self.cfg)
        try:
            options.vol.setValue(33)
            with patch.object(QMessageBox, "warning") as warning:
                options.accept()
            warning.assert_not_called()
            self.assertEqual(self.cfg["volume"], 33)
            self.assertEqual(self.cfg["library_root"], gone)
            options.library_root.setText(str(self.root / "typed-by-hand"))
            with patch.object(QMessageBox, "warning") as warning:
                options.accept()
            warning.assert_called_once()
        finally:
            options.deleteLater()

    def test_cancel_after_the_scan_result_was_queued_leaves_a_final_status(self):
        self.scan()
        self.browser.refresh()
        self.browser.job.wait(5000)
        # the result waits for the GUI thread, and Cancel arrives before it is delivered
        self.assertEqual(self.browser.status.text(), "Scanning...")
        self.browser.cancel()
        self.finish()
        self.assertEqual(self.browser.status.text(), "Scan cancelled; previous results kept.")
        self.assertEqual(self.browser.model.rowCount(), 2)

    def test_cancel_after_the_duplicate_result_was_queued_leaves_a_final_status(self):
        self.scan()
        self.browser.scan_duplicates()
        self.browser.job.wait(5000)
        self.browser.cancel()
        self.finish()
        self.assertEqual(self.browser.status.text(), "Scan cancelled; previous results kept.")
        self.assertFalse(self.browser.model.groups)

    def test_test_center_labels_and_manual_steps_include_new_features(self):
        from tests import human_steps
        from tests.test_center import available_suites
        suites = dict(available_suites())
        for name in ("test_beatdetect", "test_timeline_detect", "test_batch_detect", "test_keyboard",
                     "test_library", "test_musicbrowser", "test_regression"):
            self.assertIn(name, suites)
            self.assertFalse(suites[name].startswith("Test "))
        self.assertTrue(all(len(step) == 4 and all(step) for step in human_steps.STEPS))
        self.assertTrue(any(step[0] == "Bibliothèque" for step in human_steps.STEPS))

    def test_preferences_validate_folder_and_keep_relocation_setting_separate(self):
        options = Options(self.cfg)
        try:
            options.library_root.setText(str(self.root / "missing"))
            with patch.object(QMessageBox, "warning") as warning:
                options.accept()
            warning.assert_called_once()
            self.assertEqual(self.cfg["library_root"], str(self.root))
            options.library_root.setText(str(self.root))
            options.accept()
            self.assertEqual(self.cfg["music_root"], "")
            with patch.object(settings, "CONFIG", self.root / "settings.json"):
                settings.save(self.cfg)
                self.assertEqual(settings.load()["library_root"], str(self.root))
        finally:
            options.deleteLater()

    def test_main_menu_reuses_browser_window(self):
        log = LogWindow()
        window = MainWindow(self.cfg, log)
        try:
            action = next(action for menu in window.menuBar().actions()
                          for action in menu.menu().actions() if action.text() == "Music &browser")
            with patch.object(MusicBrowser, "refresh"):
                action.trigger()
                first = window.music_browser
                self.assertTrue(first.isVisible())
                first.close()
                action.trigger()
                self.assertIs(window.music_browser, first)
                self.assertFalse(first.closed)
        finally:
            with patch.object(settings, "save"):
                window.close()
            window.engine.deleteLater()
            window.deleteLater()
            log.deleteLater()


if __name__ == "__main__":
    unittest.main()
