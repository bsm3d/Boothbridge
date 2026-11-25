# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks renaming and removing a playlist in the main window: the tree, open windows and the project.

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings
from djapp.model import Node, Track, detach, playlists
from djapp.sources import project
from djapp.ui.details import PlaylistWindow
from djapp.ui.dialogs import CheckCollection, LogWindow
from djapp.ui.main_window import MainWindow
from djapp.ui.tageditor import TagEditor
from djapp.ui.worker import Job
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

YES = QMessageBox.Yes
NO = QMessageBox.No

def flush(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


# --------------------------------------------------------
class PlaylistTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.t1, self.t2, self.t3, self.t4, self.t5 = (
            Track(title=f"Track {i}", artist="Someone", path=str(base / f"track{i}.mp3"), duration=200 + i)
            for i in range(1, 6))
        # t1 and t2 are in several playlists, the same objects
        self.alpha = Node("playlist", "Alpha", tracks=[self.t1, self.t2, self.t3], uuid="a")
        self.beta = Node("playlist", "Beta", tracks=[self.t2, self.t4], uuid="b")
        self.gamma = Node("playlist", "Gamma", tracks=[self.t1], uuid="g")
        self.delta = Node("playlist", "Delta", tracks=[self.t5], uuid="d")
        self.crates = Node("folder", "Crates", children=[self.alpha, self.beta])
        self.solo = Node("folder", "Solo", children=[self.delta])
        self.log = LogWindow()
        self.main = MainWindow(dict(settings.DEFAULTS, auto_load=False, confirm_exit=False), self.log)
        self.main.nodes = [self.crates, self.gamma, self.solo]
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
        found = []

        def walk(parent):
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.data(0, Qt.UserRole) is node:
                    found.append(child)
                walk(child)

        walk(self.main.tree.invisibleRootItem())
        return found[0] if found else None

    def names(self):
        return [node.name for node in playlists(self.main.nodes)]

    def open_window(self, node):
        self.main.details(self.item(node))
        return next(w for w in self.main.findChildren(PlaylistWindow) if w.node is node and w.isVisible())

    def rename(self, node, text, ok=True):
        with patch.object(QInputDialog, "getText", return_value=(text, ok)) as ask, \
             patch.object(QMessageBox, "warning") as warn:
            self.main.rename_playlist(self.item(node))
        return ask, warn


    def remove(self, node, answer=YES):
        with patch.object(QMessageBox, "question", return_value=answer) as ask, \
             patch.object(QMessageBox, "warning") as warn:
            self.main.remove_playlist(self.item(node))
        return ask, warn

    # ---- model helper

    def test_detach_finds_the_node_in_a_folder_and_leaves_its_tracks(self):
        nodes = [self.crates, self.gamma]
        self.assertTrue(detach(nodes, self.beta))
        self.assertEqual(self.crates.children, [self.alpha])
        self.assertEqual(self.beta.tracks, [self.t2, self.t4])
        self.assertTrue(detach(nodes, self.gamma))
        self.assertEqual(nodes, [self.crates])
        self.assertFalse(detach(nodes, self.gamma))

    # ---- context menu

    def test_menu_has_rename_and_remove_for_a_playlist_only(self):
        menu = self.main.playlist_menu(self.item(self.alpha))
        self.assertEqual([a.text() for a in menu.actions()], ["Rename...", "Remove from this session..."])
        self.assertTrue(all(a.isEnabled() for a in menu.actions()))
        menu.deleteLater()
        self.assertIsNone(self.main.playlist_menu(self.item(self.crates)))
        self.assertIsNone(self.main.playlist_menu(None))


    def test_right_click_opens_the_menu_on_a_playlist_and_not_on_a_folder_or_empty_space(self):
        tree = self.main.tree
        self.assertEqual(tree.contextMenuPolicy(), Qt.CustomContextMenu)
        shown = []
        real = self.main.playlist_menu

        def no_popup(item):
            menu = real(item)
            if menu is not None:
                menu.exec = shown.append  # the real exec would wait for a click
            return menu

        with patch.object(self.main, "playlist_menu", no_popup):
            tree.customContextMenuRequested.emit(tree.visualItemRect(self.item(self.gamma)).center())
            self.assertEqual(len(shown), 1)
            tree.customContextMenuRequested.emit(tree.visualItemRect(self.item(self.crates)).center())
            tree.customContextMenuRequested.emit(tree.viewport().rect().bottomRight())
            self.assertEqual(len(shown), 1)

    def test_menu_actions_run_rename_and_remove_on_that_row(self):
        item = self.item(self.gamma)
        with patch.object(self.main, "rename_playlist") as ren, patch.object(self.main, "remove_playlist") as rem:
            menu = self.main.playlist_menu(item)
            rename, remove = menu.actions()
            rename.trigger()
            remove.trigger()
        ren.assert_called_once_with(item)
        rem.assert_called_once_with(item)
        menu.deleteLater()

    def test_menu_actions_are_off_while_a_job_runs(self):
        self.main.job = Job(lambda progress, cancel: None)
        menu = self.main.playlist_menu(self.item(self.alpha))
        self.assertFalse(any(a.isEnabled() for a in menu.actions()))
        menu.deleteLater()
        self.main.job.deleteLater()

    # ---- rename

    def test_rename_updates_node_item_and_marks_the_project_modified(self):
        self.assertFalse(self.main.dirty)
        self.rename(self.beta, "  Beta two ")
        self.assertEqual(self.beta.name, "Beta two")
        self.assertEqual(self.item(self.beta).text(0), "Beta two")
        self.assertTrue(self.main.dirty)

    def test_rename_asks_with_the_current_name(self):
        ask, _ = self.rename(self.gamma, "Gamma", ok=False)
        self.assertEqual(ask.call_args.kwargs["text"], "Gamma")

    def test_rename_refuses_an_empty_name(self):
        _, warn = self.rename(self.beta, "   ")
        warn.assert_called_once()
        self.assertEqual(self.beta.name, "Beta")
        self.assertFalse(self.main.dirty)

    def test_rename_refuses_a_name_used_by_another_playlist_whatever_the_case(self):
        _, warn = self.rename(self.gamma, "bEtA")
        warn.assert_called_once()
        self.assertEqual(self.gamma.name, "Gamma")
        self.assertEqual(self.item(self.gamma).text(0), "Gamma")
        self.assertFalse(self.main.dirty)

    def test_rename_may_change_only_the_case_of_its_own_name(self):
        _, warn = self.rename(self.gamma, "GAMMA")
        warn.assert_not_called()
        self.assertEqual(self.gamma.name, "GAMMA")

    def test_rename_cancelled_or_unchanged_does_nothing(self):
        self.rename(self.gamma, "Other", ok=False)
        self.rename(self.gamma, "Gamma")
        self.assertEqual(self.gamma.name, "Gamma")
        self.assertFalse(self.main.dirty)

    def test_rename_on_a_folder_does_nothing(self):
        with patch.object(QInputDialog, "getText") as ask:
            self.main.rename_playlist(self.item(self.crates))
        ask.assert_not_called()

    def test_rename_keeps_the_smart_suffix_of_a_smartlist(self):
        smart = Node("smartlist", "Quick", tracks=[self.t1], query="x")
        self.main.nodes.append(smart)
        self.main.fill_tree()
        self.assertEqual(self.item(smart).text(0), "Quick  (smart)")
        self.rename(smart, "Quicker")
        self.assertEqual(self.item(smart).text(0), "Quicker  (smart)")
        self.assertEqual(smart.name, "Quicker")

    def test_rename_updates_the_open_window_and_the_also_in_of_others(self):
        window = self.open_window(self.alpha)
        other = self.open_window(self.gamma)
        self.assertEqual(other.also_in(self.t1), ["Alpha"])
        self.rename(self.alpha, "Opening")
        self.assertEqual(window.windowTitle(), "Details: Opening")
        self.assertEqual(other.windowTitle(), "Details: Gamma")
        self.assertEqual(other.also_in(self.t1), ["Opening"])

    def test_rename_is_refused_while_a_job_runs(self):
        self.main.job = Job(lambda progress, cancel: None)
        with patch.object(QInputDialog, "getText") as ask, patch.object(QMessageBox, "information") as info:
            self.main.rename_playlist(self.item(self.gamma))
        ask.assert_not_called()
        info.assert_called_once()
        self.assertEqual(self.gamma.name, "Gamma")
        self.main.job.deleteLater()

    def test_rename_is_refused_while_a_folder_scan_runs(self):
        panel = CheckCollection(self.main.nodes, "", self.main)
        panel.scan = object()
        try:
            self.assertTrue(self.main.tree_busy())
            with patch.object(QInputDialog, "getText") as ask, patch.object(QMessageBox, "information"):
                self.main.rename_playlist(self.item(self.gamma))
            ask.assert_not_called()
        finally:
            panel.scan = None
            panel.deleteLater()
        self.assertFalse(self.main.tree_busy())

    # ---- remove

    def test_remove_takes_the_node_and_the_item_out_and_updates_the_counts(self):
        self.assertEqual(self.main.info.text(), "4 playlists, 5 tracks")
        self.remove(self.alpha)
        self.assertEqual(self.crates.children, [self.beta])
        self.assertIsNone(self.item(self.alpha))
        self.assertEqual(self.names(), ["Beta", "Gamma", "Delta"])
        self.assertEqual(self.main.tree.topLevelItem(0).childCount(), 1)
        self.assertEqual(self.main.info.text(), "3 playlists, 4 tracks")
        self.assertTrue(self.main.dirty)

    def test_remove_keeps_shared_tracks_in_the_other_playlists(self):
        self.remove(self.alpha)
        self.assertIs(self.beta.tracks[0], self.t2)
        self.assertIs(self.gamma.tracks[0], self.t1)
        self.assertEqual((self.t2.title, self.t2.duration), ("Track 2", 202))
        self.assertEqual(self.alpha.tracks, [self.t1, self.t2, self.t3])

    def test_remove_asks_first_and_says_that_only_the_session_changes(self):
        ask, _ = self.remove(self.gamma, NO)
        self.assertEqual(self.names(), ["Alpha", "Beta", "Gamma", "Delta"])
        self.assertFalse(self.main.dirty)
        text = ask.call_args.args[2]
        self.assertIn('"Gamma"', text)
        self.assertIn("The collection file and the audio files are never touched", text)

    def test_remove_closes_the_window_open_on_that_playlist_and_keeps_the_others(self):
        window = self.open_window(self.alpha)
        other = self.open_window(self.gamma)
        self.remove(self.alpha)
        self.assertFalse(window.isVisible())
        self.assertTrue(other.isVisible())

    def test_remove_closes_every_window_of_the_playlist(self):
        first = self.open_window(self.alpha)
        second = self.open_window(self.alpha)
        self.remove(self.alpha)
        self.assertFalse(first.isVisible() or second.isVisible())

    def test_declined_confirmation_keeps_the_window_open(self):
        window = self.open_window(self.alpha)
        self.remove(self.alpha, NO)
        self.assertTrue(window.isVisible())

    def test_remove_stops_when_the_window_cannot_close(self):
        window = self.open_window(self.alpha)
        job = Job(lambda progress, cancel: None)
        window.detect_job = job
        with patch.object(job, "wait", return_value=False):
            _, warn = self.remove(self.alpha)
        warn.assert_called_once()
        self.assertIn("writing tags or detecting grids", warn.call_args.args[2])
        self.assertIn(self.alpha, self.crates.children)
        self.assertIsNotNone(self.item(self.alpha))
        self.assertEqual(self.main.info.text(), "4 playlists, 5 tracks")
        self.assertFalse(self.main.dirty)
        self.assertTrue(window.isVisible())
        window.detect_job = None
        job.deleteLater()

    def test_remove_is_refused_while_a_job_runs(self):
        self.main.job = Job(lambda progress, cancel: None)
        with patch.object(QMessageBox, "question") as ask, patch.object(QMessageBox, "information") as info:
            self.main.remove_playlist(self.item(self.gamma))
        ask.assert_not_called()
        info.assert_called_once()
        self.assertIn(self.gamma, self.main.nodes)
        self.assertFalse(self.main.dirty)
        self.main.job.deleteLater()

    def test_remove_on_a_folder_does_nothing(self):
        with patch.object(QMessageBox, "question") as ask:
            self.main.remove_playlist(self.item(self.crates))
            self.main.remove_playlist(None)
        ask.assert_not_called()
        self.assertEqual(len(self.main.nodes), 3)

    def test_remove_refreshes_the_also_in_column_of_the_other_windows(self):
        other = self.open_window(self.beta)
        self.assertEqual(other.also_in(self.t2), ["Alpha"])
        self.remove(self.alpha)
        self.assertEqual(other.also_in(self.t2), [])
        self.assertEqual(other.table.topLevelItem(0).text(11), "")

    def test_removed_playlist_is_not_exported_when_nothing_is_ticked(self):
        self.remove(self.gamma)
        everything = self.main.selected()
        self.assertIs(everything, self.main.nodes)
        self.assertNotIn("Gamma", [n.name for n in playlists(everything)])

    def test_selection_goes_to_a_neighbour_then_to_the_folder(self):
        tree = self.main.tree
        self.remove(self.alpha)
        self.assertIs(tree.currentItem().data(0, Qt.UserRole), self.beta)
        self.remove(self.beta)
        self.assertIs(tree.currentItem().data(0, Qt.UserRole), self.crates)
        self.assertEqual(self.crates.children, [])
        self.remove(self.gamma)
        self.assertIs(tree.currentItem().data(0, Qt.UserRole), self.solo)

    def test_removing_the_last_playlist_empties_the_tree(self):
        for node in (self.alpha, self.beta, self.gamma, self.delta):
            self.remove(node)
        self.assertEqual(self.names(), [])
        self.assertEqual(self.main.info.text(), "0 playlists, 0 tracks")

    # ---- the Delete key

    def press_delete(self, widget):
        self.main.activateWindow()
        QTest.qWaitForWindowActive(self.main)
        widget.setFocus()
        self.app.processEvents()
        QTest.keyClick(widget, Qt.Key_Delete)

    def test_delete_key_on_the_tree_removes_the_current_playlist_after_asking(self):
        self.main.tree.setCurrentItem(self.item(self.gamma))
        with patch.object(QMessageBox, "question", return_value=YES) as ask:
            self.press_delete(self.main.tree)
        ask.assert_called_once()
        self.assertNotIn("Gamma", self.names())

    def test_delete_key_on_a_folder_does_nothing(self):
        self.main.tree.setCurrentItem(self.item(self.crates))
        with patch.object(QMessageBox, "question", return_value=YES) as ask:
            self.press_delete(self.main.tree)
        ask.assert_not_called()
        self.assertEqual(len(self.names()), 4)

    def test_delete_key_in_a_text_field_does_not_remove_a_playlist(self):
        self.main.tree.setCurrentItem(self.item(self.gamma))
        self.main.src.setText("abc")
        self.main.src.setCursorPosition(0)
        with patch.object(QMessageBox, "question", return_value=YES) as ask:
            self.press_delete(self.main.src)
        ask.assert_not_called()
        self.assertEqual(self.main.src.text(), "bc")
        self.assertIn("Gamma", self.names())

    def test_delete_key_in_a_playlist_window_still_removes_tracks_only(self):
        window = self.open_window(self.alpha)
        window.table.setCurrentItem(window.table.topLevelItem(0))
        window.table.topLevelItem(0).setSelected(True)
        with patch.object(QMessageBox, "question", return_value=YES) as ask:
            window.activateWindow()
            QTest.qWaitForWindowActive(window)
            window.table.setFocus()
            self.app.processEvents()
            QTest.keyClick(window.table, Qt.Key_Delete)
        ask.assert_not_called()
        self.assertEqual(self.names(), ["Alpha", "Beta", "Gamma", "Delta"])
        self.assertEqual(len(self.alpha.tracks), 2)

    # ---- project

    def save_and_read(self):
        path = os.path.join(self.tmp.name, "saved.json")
        self.main.project = path
        self.assertTrue(self.main.save_project())
        self.assertFalse(self.main.dirty)
        return project.load(path)

    def test_a_new_playlist_cannot_take_a_name_already_used(self):
        with patch.object(QInputDialog, "getText", return_value=("ALPHA", True)),                 patch.object(QMessageBox, "warning") as warn:
            self.main.new_playlist()
        warn.assert_called_once()
        self.assertEqual(self.names(), ["Alpha", "Beta", "Gamma", "Delta"])
        self.assertFalse(self.main.dirty)

    def test_opening_a_playlist_twice_brings_its_window_back(self):
        first = self.open_window(self.alpha)
        self.main.details(self.item(self.alpha))
        self.assertEqual([w for w in self.main.findChildren(PlaylistWindow) if w.isVisible()], [first])

    def test_quit_asks_about_unapplied_id_tag_fields(self):
        window = self.open_window(self.alpha)
        window.edit_tags(self.t1)
        editor = window.findChildren(TagEditor)[0]
        editor.fields["title"].setText("Typed, not applied")
        with patch.object(QMessageBox, "question", return_value=NO) as ask,                 patch.object(settings, "save"):
            self.assertFalse(self.main.close())
        ask.assert_called_once()
        self.assertTrue(editor.isVisible())
        self.assertTrue(window.isVisible())
        self.assertEqual(self.t1.title, "Track 1")
        with patch.object(QMessageBox, "question", return_value=YES):
            self.assertTrue(window.close())
        self.assertFalse(editor.isVisible())

    def test_a_load_keeps_the_path_it_read_when_the_source_changes_meanwhile(self):
        first = os.path.join(self.tmp.name, "first.json")
        second = os.path.join(self.tmp.name, "second.json")
        project.save(first, [Node("playlist", "First")], {})
        project.save(second, [Node("playlist", "Second")], {})
        self.main.src.setText(first)
        self.main.reload()
        self.main.src.setText(second)
        deadline = time.monotonic() + 10
        while self.main.job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.main.job)
        self.assertEqual(self.main.project, first)
        self.assertEqual(self.names(), ["First"])

    def test_a_removed_playlist_is_not_saved_again(self):
        self.remove(self.alpha)
        nodes = self.save_and_read()
        self.assertEqual([n.name for n in playlists(nodes)], ["Beta", "Gamma", "Delta"])
        beta = next(n for n in playlists(nodes) if n.name == "Beta")
        self.assertEqual([t.title for t in beta.tracks], ["Track 2", "Track 4"])
        gamma = next(n for n in playlists(nodes) if n.name == "Gamma")
        self.assertEqual([t.title for t in gamma.tracks], ["Track 1"])

    def test_a_renamed_playlist_is_saved_with_its_new_name(self):
        self.rename(self.beta, "Closing")
        nodes = self.save_and_read()
        self.assertEqual([n.name for n in playlists(nodes)], ["Alpha", "Closing", "Gamma", "Delta"])
        self.assertEqual([n.uuid for n in playlists(nodes)], ["a", "b", "g", "d"])

    def test_removing_the_only_playlist_leaves_nothing_to_save(self):
        self.main.nodes = [self.gamma]
        self.main.fill_tree()
        self.remove(self.gamma)
        with patch.object(QMessageBox, "information") as info:
            self.assertFalse(self.main.save_project())
        info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
