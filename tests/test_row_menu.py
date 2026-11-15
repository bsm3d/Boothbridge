# right click on a track of a playlist: Explorer, BPM detection, Cue Editor, insert into another playlist

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import beatdetect, settings
from djapp.model import Node, Track
from djapp.ui import details
from djapp.ui.details import PlaylistWindow
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow
from djapp.ui.player import Engine
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

YES, NO = QMessageBox.Yes, QMessageBox.No


# ------------------------------------------------------------
def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


# ------------------------------------------------------------
class RowMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.tracks = [Track(title=f"Song {i}", artist="Band", path=str(base / f"song{i}.wav"), bpm=100, grid=0)
                       for i in range(3)]
        for t in self.tracks[:2]:
            Path(t.path).touch()
        self.tracks[2].path = str(base / "gone" / "missing.wav")  # no file, no folder
        self.node = Node("playlist", "Source", tracks=list(self.tracks))
        self.other = Node("playlist", "Other", tracks=[])
        self.rnb = Node("playlist", "R&B", tracks=[])
        self.smart = Node("smartlist", "Smart", query="x")
        self.everything = [self.node, self.other, self.rnb, self.smart]
        self.engine = Engine()
        self.loader = Loader()
        with patch.object(self.loader, "prefetch"):
            self.window = PlaylistWindow(self.node, self.engine, self.loader, {}, others=lambda: self.everything)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.finish()
        self.loader.stop()
        self.engine.close()
        self.engine.deleteLater()
        flush(self.app)

    def finish(self):
        deadline = time.monotonic() + 5
        while self.window.detect_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window.detect_job)

    # the menu is read, and an entry optionally clicked, instead of waiting for a click
    def right_click(self, row, choose=None):
        seen = {}
        table = self.window.table
        pos = table.visualItemRect(table.topLevelItem(row)).center()
        real = self.window.build_row_menu

        def fake_exec(menu):
            seen["texts"] = [a.text() for a in menu.actions()]
            seen["enabled"] = {a.text(): a.isEnabled() for a in menu.actions()}
            sub = next(a.menu() for a in menu.actions() if a.menu() is not None)
            seen["sub"] = [a.text() for a in sub.actions()]
            seen["sub_enabled"] = sub.isEnabled()
            if choose:
                every = menu.actions() + sub.actions()
                next(a for a in every if a.text() == choose).trigger()

        def build(t):
            menu = real(t)
            menu.exec = lambda *_: fake_exec(menu)
            return menu

        with patch.object(self.window, "build_row_menu", build):
            self.window.row_menu(pos)
        return seen

    # ---- the menu

    def test_menu_has_the_four_entries_and_lists_the_other_playlists_only(self):
        seen = self.right_click(0)
        self.assertEqual(seen["texts"], ["Show in Explorer", "Detect BPM", "Show Cue Editor", "Insert into"])
        self.assertTrue(all(seen["enabled"].values()))
        self.assertEqual(seen["sub"], ["Other", "R&&B"])  # not itself, not a smart list, & kept as a letter

    def test_right_click_outside_the_selection_selects_that_row_only(self):
        self.window.table.selectAll()
        self.window.table.clearSelection()
        self.right_click(1)
        self.assertEqual(self.window.chosen(), [self.tracks[1]])

    def test_right_click_inside_a_selection_keeps_it_and_names_the_count(self):
        self.window.table.topLevelItem(0).setSelected(True)
        self.window.table.topLevelItem(1).setSelected(True)
        seen = self.right_click(0)
        self.assertEqual(self.window.chosen(), self.tracks[:2])
        self.assertEqual(seen["texts"][1], "Detect BPM (2 tracks)")
        self.assertEqual(seen["texts"][3], "Insert 2 tracks into")

    def test_a_missing_file_disables_what_needs_the_audio(self):
        seen = self.right_click(2)
        self.assertFalse(seen["enabled"]["Show in Explorer"])  # its folder is gone too
        self.assertFalse(seen["enabled"]["Detect BPM"])
        self.assertFalse(seen["enabled"]["Show Cue Editor"])
        self.assertTrue(seen["sub_enabled"])

    def test_a_missing_file_in_an_existing_folder_still_opens_the_folder(self):
        self.tracks[2].path = str(Path(self.tmp.name) / "not there.wav")
        self.window.where.clear()
        self.assertTrue(self.right_click(2)["enabled"]["Show in Explorer"])

    def test_no_other_playlist_disables_insert(self):
        self.everything[:] = [self.node]
        self.assertFalse(self.right_click(0)["sub_enabled"])

    def test_empty_space_opens_nothing(self):
        with patch.object(self.window, "build_row_menu") as build:
            self.window.row_menu(self.window.table.viewport().rect().bottomRight())
        build.assert_not_called()

    def test_the_table_asks_for_this_menu(self):
        self.assertEqual(self.window.table.contextMenuPolicy(), Qt.CustomContextMenu)

    # ---- Show in Explorer

    def test_the_menu_shows_the_resolved_file(self):
        with patch.object(details, "show_in_explorer", return_value=True) as show:
            self.right_click(1, "Show in Explorer")
        show.assert_called_once_with(self.tracks[1].path)

    def test_a_refused_explorer_is_told_in_the_footer(self):
        with patch.object(details, "show_in_explorer", return_value=False):
            self.right_click(1, "Show in Explorer")
        self.assertIn("Cannot open", self.window.count.text())

    @unittest.skipUnless(os.name == "nt", "Explorer is Windows")
    def test_explorer_gets_the_file_with_backslashes(self):
        path = str(Path(self.tracks[0].path)).replace("\\", "/")
        with patch.object(details.subprocess, "Popen") as popen:
            self.assertTrue(details.show_in_explorer(path))
        self.assertEqual(popen.call_args.args[0], ["explorer", "/select,", os.path.normpath(path)])

    def test_a_gone_file_opens_its_folder_and_a_gone_folder_fails(self):
        folder = self.tmp.name
        with patch.object(details.QDesktopServices, "openUrl", return_value=True) as open_url:
            self.assertTrue(details.show_in_explorer(os.path.join(folder, "gone.wav")))
        self.assertEqual(os.path.normpath(open_url.call_args.args[0].toLocalFile()), os.path.normpath(folder))
        with patch.object(details.QDesktopServices, "openUrl", return_value=True) as open_url:
            self.assertFalse(details.show_in_explorer(os.path.join(folder, "nope", "gone.wav")))
        open_url.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Explorer is Windows")
    def test_an_os_error_from_explorer_is_a_failure_not_a_crash(self):
        with patch.object(details.subprocess, "Popen", side_effect=OSError("no explorer")):
            self.assertFalse(details.show_in_explorer(self.tracks[0].path))

    # ---- Cue Editor

    def test_the_cue_editor_opens_once_per_track_until_it_is_closed(self):
        with patch.object(details, "Timeline") as timeline:
            self.right_click(0, "Show Cue Editor")
            self.right_click(0, "Show Cue Editor")
            self.assertEqual(timeline.call_count, 1)
            timeline.return_value.raise_.assert_called_once()
            timeline.return_value.destroyed.connect.call_args.args[0]()  # the editor is gone
            self.right_click(0, "Show Cue Editor")
            self.assertEqual(timeline.call_count, 2)
            self.right_click(1, "Show Cue Editor")
            self.assertEqual(timeline.call_count, 3)

    # ---- Detect BPM

    def test_detect_bpm_runs_without_asking_and_applies_a_reliable_result_as_one_undo(self):
        result = beatdetect.BeatResult(123.0, 50.0, 0.9)
        with patch.object(QMessageBox, "question") as ask, patch.object(beatdetect, "analyze", return_value=result):
            self.right_click(0, "Detect BPM")
            self.finish()
        ask.assert_not_called()
        self.assertEqual((self.tracks[0].bpm, self.tracks[0].grid), (123.0, 50.0))
        self.assertEqual(self.tracks[1].bpm, 100)
        self.window.undo_cues()
        self.assertEqual((self.tracks[0].bpm, self.tracks[0].grid), (100, 0))

    def test_detect_bpm_of_a_selection_analyses_those_tracks_only(self):
        result = beatdetect.BeatResult(124.0, 10.0, 0.9)
        self.window.table.topLevelItem(0).setSelected(True)
        self.window.table.topLevelItem(1).setSelected(True)
        with patch.object(beatdetect, "analyze", return_value=result) as analyze:
            self.right_click(1, "Detect BPM (2 tracks)")
            self.finish()
        self.assertEqual(analyze.call_count, 2)
        self.assertEqual([t.bpm for t in self.tracks], [124.0, 124.0, 100])

    def test_a_doubtful_result_for_one_track_asks_and_a_no_changes_nothing(self):
        weak = beatdetect.BeatResult(90.0, 5.0, 0.05)
        with patch.object(QMessageBox, "question", return_value=NO) as ask, \
                patch.object(beatdetect, "analyze", return_value=weak):
            self.right_click(0, "Detect BPM")
            self.finish()
        ask.assert_called_once()
        self.assertIn("not reliable", ask.call_args.args[2])
        self.assertEqual(ask.call_args.args[4], NO)
        self.assertEqual((self.tracks[0].bpm, self.tracks[0].grid), (100, 0))
        self.assertFalse(self.window.undo_stack)

    def test_a_doubtful_result_for_one_track_is_applied_when_the_user_says_yes(self):
        weak = beatdetect.BeatResult(90.0, 5.0, 0.05)
        with patch.object(QMessageBox, "question", return_value=YES), \
                patch.object(beatdetect, "analyze", return_value=weak):
            self.right_click(0, "Detect BPM")
            self.finish()
        self.assertEqual((self.tracks[0].bpm, self.tracks[0].grid), (90.0, 5.0))
        self.assertEqual(len(self.window.undo_stack), 1)

    def test_doubtful_results_of_several_tracks_are_skipped_without_asking(self):
        weak = beatdetect.BeatResult(90.0, 5.0, 0.05)
        self.window.table.topLevelItem(0).setSelected(True)
        self.window.table.topLevelItem(1).setSelected(True)
        with patch.object(QMessageBox, "question") as ask, patch.object(beatdetect, "analyze", return_value=weak):
            self.right_click(0, "Detect BPM (2 tracks)")
            self.finish()
        ask.assert_not_called()
        self.assertEqual([t.bpm for t in self.tracks], [100, 100, 100])
        self.assertIn("2 skipped", self.window.count.text())

    def test_the_toolbar_button_still_asks_first_and_does_not_ask_about_doubtful_results(self):
        weak = beatdetect.BeatResult(90.0, 5.0, 0.05)
        self.tracks[0].grid = None
        with patch.object(QMessageBox, "question", return_value=YES) as ask, \
                patch.object(beatdetect, "analyze", return_value=weak):
            self.window.detect_grids()
            self.finish()
        ask.assert_called_once()  # the "Analyse 1 track(s)?" question, nothing about the result
        self.assertEqual(self.tracks[0].bpm, 100)
        self.assertFalse(self.window.detect_ask_one)

    def test_detect_bpm_is_off_while_a_detection_runs(self):
        self.window.detect_job = object()
        try:
            self.assertFalse(self.right_click(0)["enabled"]["Detect BPM"])
        finally:
            self.window.detect_job = None

    # ---- Insert into

    def test_insert_sends_the_same_track_objects_and_keeps_the_selection(self):
        sent = []
        self.window.inserted.connect(lambda target, tracks: sent.append((target, tracks)))
        self.window.table.topLevelItem(0).setSelected(True)
        self.window.table.topLevelItem(1).setSelected(True)
        self.right_click(0, "Other")
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0][0], self.other)
        self.assertTrue(all(a is b for a, b in zip(sent[0][1], self.tracks[:2])))
        self.assertEqual(self.window.chosen(), self.tracks[:2])
        self.assertEqual(self.window.count.text(), "Inserted 2 track(s) into Other")

    def test_insert_skips_what_the_target_already_has_by_object_path_or_id(self):
        same_file = Track(title="Other copy", path=self.tracks[0].path)
        same_id = Track(title="Tagged", audio_id="ABC", path="x.wav")
        self.tracks[1].audio_id = "ABC"
        self.other.tracks = [same_file, same_id]
        sent = []
        self.window.inserted.connect(lambda target, tracks: sent.append(tracks))
        for row in range(3):
            self.window.table.topLevelItem(row).setSelected(True)
        self.right_click(0, "Other")
        self.assertEqual(sent, [[self.tracks[2]]])
        self.assertEqual(self.window.count.text(), "Inserted 1 track(s) into Other, 2 already there")

    def test_insert_of_tracks_that_are_all_there_sends_nothing(self):
        self.other.tracks = [self.tracks[0]]
        sent = []
        self.window.inserted.connect(lambda *a: sent.append(a))
        self.right_click(0, "Other")
        self.assertEqual(sent, [])
        self.assertEqual(self.window.count.text(), "Already in Other")


# ------------------------------------------------------------
class InsertIntoOpenOrClosedPlaylistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.t1, self.t2, self.t3 = (Track(title=f"Track {i}", artist="Someone", path=f"C:/nowhere/t{i}.mp3")
                                     for i in range(1, 4))
        self.alpha = Node("playlist", "Alpha", tracks=[self.t1, self.t2, self.t3], uuid="a")
        self.beta = Node("playlist", "Beta", tracks=[self.t1], uuid="b")
        self.log = LogWindow()
        self.main = MainWindow(dict(settings.DEFAULTS, auto_load=False, confirm_exit=False), self.log)
        self.main.nodes = [self.alpha, self.beta]
        self.main.fill_tree()
        self.main.refresh_info()
        self.main.show()
        self.app.processEvents()

    def tearDown(self):
        for window in self.main.findChildren(PlaylistWindow):
            window.detect_job = None
            window.close()
        self.main.job = None
        self.main.set_dirty(False)
        with patch.object(settings, "save"):
            self.main.close()
        self.main.engine.deleteLater()
        self.main.deleteLater()
        self.log.deleteLater()
        flush(self.app)

    def item(self, node):
        for i in range(self.main.tree.topLevelItemCount()):
            it = self.main.tree.topLevelItem(i)
            if it.data(0, Qt.UserRole) is node:
                return it

    def open_window(self, node):
        self.main.details(self.item(node))
        return next(w for w in self.main.findChildren(PlaylistWindow) if w.node is node and w.isVisible())

    def test_a_closed_target_is_changed_directly_and_the_tree_count_follows(self):
        source = self.open_window(self.alpha)
        source.table.topLevelItem(1).setSelected(True)
        source.table.topLevelItem(2).setSelected(True)
        source.insert_into(self.beta)
        self.assertEqual(self.beta.tracks, [self.t1, self.t2, self.t3])
        self.assertIs(self.beta.tracks[1], self.t2)
        self.assertEqual(self.item(self.beta).text(1), "3")
        self.assertTrue(self.main.dirty)
        self.assertIn('inserted into "Beta"', self.main.status.text())

    def test_the_source_playlist_is_not_changed_and_shows_where_the_track_now_is(self):
        source = self.open_window(self.alpha)
        source.table.topLevelItem(1).setSelected(True)
        source.insert_into(self.beta)
        self.assertEqual(self.alpha.tracks, [self.t1, self.t2, self.t3])
        self.assertEqual(source.table.topLevelItem(1).text(11), "1")  # Also in: Beta
        self.assertEqual(source.chosen(), [self.t2])

    def test_an_open_target_takes_it_as_one_undo_step_and_shows_the_rows(self):
        source = self.open_window(self.alpha)
        target = self.open_window(self.beta)
        source.table.topLevelItem(2).setSelected(True)
        source.insert_into(self.beta)
        self.assertEqual(self.beta.tracks, [self.t1, self.t3])
        self.assertEqual(target.table.topLevelItemCount(), 2)
        self.assertEqual(len(target.undo_stack), 1)
        target.undo_cues()
        self.assertEqual(self.beta.tracks, [self.t1])
        self.assertEqual(self.item(self.beta).text(1), "1")

    def test_a_playlist_removed_meanwhile_is_ignored(self):
        gone = Node("playlist", "Gone", tracks=[])
        self.main.insert_tracks(gone, [self.t1])
        self.assertEqual(gone.tracks, [])


if __name__ == "__main__":
    unittest.main()
