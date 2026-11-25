# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks keyboard shortcuts and key handling of the window and its widgets, run offscreen.

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.model import Node, Track
from djapp.ui.details import C_CUES, C_TAG, PlaylistWindow
from djapp.ui.player import Engine
from djapp.ui.timeline import Timeline
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel, Qt
from PySide6.QtGui import QKeyEvent, QShortcut
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QPushButton


def key(widget, code, mods=Qt.NoModifier):
    for kind in (QEvent.KeyPress, QEvent.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, code, mods))


# ------------------------------------------------------------
class KeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tracks = [Track(title=name, path=f"{name}.mp3") for name in ("One", "Two", "Three", "Four")]
        self.node = Node(kind="playlist", name="List", tracks=list(self.tracks))
        self.engine = Engine()
        self.window = PlaylistWindow(self.node, self.engine, Loader(), {})

    def tearDown(self):
        for timeline in getattr(self, "timelines", []):
            timeline.close()
        self.window.close()
        self.window.loader.stop()
        self.engine.close()
        self.engine.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def select(self, *rows):
        self.window.table.clearSelection()
        for row in rows:
            self.window.table.topLevelItem(row).setSelected(True)
        self.window.table.setCurrentItem(self.window.table.topLevelItem(rows[0]),
                                         0, QItemSelectionModel.NoUpdate)

    def test_shortcuts_have_no_duplicate_key_in_a_window(self):
        for owner in (self.window,):
            seen = {}
            for shortcut in owner.findChildren(QShortcut):
                sequence = shortcut.key().toString()
                scope = shortcut.parent()
                self.assertNotIn((sequence, scope), seen, f"{sequence} is bound twice")
                seen[(sequence, scope)] = shortcut
        sequences = [s.key().toString() for s in self.window.findChildren(QShortcut) if s.parent() is self.window]
        for wanted in ("Alt+Up", "Alt+Down", "Ctrl+I", "Ctrl+T", "Ctrl+Left", "Ctrl+Right", "Ctrl+Shift+D"):
            self.assertIn(wanted, sequences)

    def test_timeline_shortcuts_are_unique(self):
        timeline = Timeline(self.tracks[0], "", self.window.loader, {})
        self.addCleanup(timeline.deleteLater)
        self.addCleanup(timeline.close)
        owned = [s.key().toString() for s in timeline.findChildren(QShortcut) if s.parent() is timeline]
        self.assertEqual(len(owned), len(set(owned)))
        for wanted in ("Ctrl+D", "Ctrl+B", "Alt+Left", "Alt+Right"):
            self.assertIn(wanted, owned)

    def test_timeline_keyboard_nudges_the_grid_and_toggles_snap(self):
        track = self.tracks[0]
        track.duration, track.bpm, track.grid = 10, 120, 100
        timeline = Timeline(track, "", self.window.loader, {})
        self.addCleanup(timeline.deleteLater)
        self.addCleanup(timeline.close)
        before = timeline.snap_btn.isChecked()
        next(s for s in timeline.findChildren(QShortcut) if s.key().toString() == "Ctrl+B").activated.emit()
        self.assertNotEqual(timeline.snap_btn.isChecked(), before)
        next(s for s in timeline.findChildren(QShortcut) if s.key().toString() == "Alt+Shift+Right").activated.emit()
        self.assertEqual(track.grid, 110)
        next(s for s in timeline.findChildren(QShortcut) if s.key().toString() == "Alt+Left").activated.emit()
        self.assertEqual(track.grid, 109)

    def test_alt_arrows_move_the_selection_and_can_be_undone(self):
        self.select(1, 2)
        self.window.move_selected(-1)
        self.assertEqual([t.title for t in self.node.tracks], ["Two", "Three", "One", "Four"])
        selected = [t.title for t in self.window.chosen()]
        self.assertEqual(selected, ["Two", "Three"])
        self.window.move_selected(-1)  # already at the top
        self.assertEqual(len(self.window.undo_stack), 1)
        self.window.move_selected(1)
        self.assertEqual([t.title for t in self.node.tracks], ["One", "Two", "Three", "Four"])
        self.assertEqual(len(self.window.undo_stack), 2)
        self.window.undo_cues()
        self.window.undo_cues()
        self.assertEqual([t.title for t in self.node.tracks], ["One", "Two", "Three", "Four"])
        self.assertFalse(self.window.undo_stack)

    def test_move_is_refused_while_the_list_is_filtered(self):
        self.window.search.setText("T")
        self.window.typing.stop()
        self.window.fill()
        self.select(0)
        self.window.move_selected(1)
        self.assertEqual([t.title for t in self.node.tracks], ["One", "Two", "Three", "Four"])
        self.assertIn("Clear", self.window.count.text())

    def test_enter_plays_the_current_track(self):
        self.select(2)
        with patch.object(self.window, "play") as play:
            self.window.table.itemActivated.emit(self.window.table.topLevelItem(2), 0)
        play.assert_called_once_with(self.tracks[2])

    def test_enter_on_cues_and_tags_plays_once_without_opening_editors(self):
        for column in (C_CUES, C_TAG, 0):
            self.window.table.setCurrentItem(self.window.table.topLevelItem(2), column)
            for code in (Qt.Key_Return, Qt.Key_Enter):
                with patch.object(self.window, "play") as play, \
                        patch("djapp.ui.details.Timeline") as timeline:
                    key(self.window.table, code)
                    play.assert_called_once_with(self.tracks[2])
                    timeline.assert_not_called()
                    QApplication.sendEvent(
                        self.window.table, QKeyEvent(QEvent.KeyPress, code, Qt.NoModifier, "", True),
                    )
                    play.assert_called_once_with(self.tracks[2])

    def test_escape_in_the_filter_clears_it_and_returns_to_the_list(self):
        self.window.show()
        self.window.focus_search()
        self.window.search.setText("two")
        key(self.window.search, Qt.Key_Escape)
        self.assertEqual(self.window.search.text(), "")
        self.assertTrue(self.window.table.hasFocus() or not self.window.isActiveWindow())

    def test_ctrl_i_and_ctrl_t_open_the_current_track_editors(self):
        self.select(1)
        with patch.object(self.window, "edit_tags") as edit:
            self.window.edit_current_tags()
        edit.assert_called_once_with(self.tracks[1])
        with patch("djapp.ui.details.Timeline") as timeline:
            self.window.open_timeline()
        timeline.assert_called_once()

    def test_seek_by_moves_the_needle(self):
        track = self.tracks[0]
        track.duration = 60
        self.window.player.load(track, "")
        self.window.seek_by(5000)
        self.assertEqual(self.window.player.mark, 5000)
        self.window.seek_by(-9000)
        self.assertEqual(self.window.player.mark, 0)

    def test_s_toggles_snap_in_each_editor_without_double_toggling_shared_engine(self):
        track = self.tracks[0]
        self.window.player.load(track, "")
        timeline = Timeline(track, "", self.window.loader, {}, self.engine)
        self.timelines = [timeline]
        for owner, other, button in ((self.window, timeline, self.window.player.snap_btn),
                                     (timeline, self.window, timeline.snap_btn)):
            with patch.object(owner, "isActiveWindow", return_value=True), \
                    patch.object(other, "isActiveWindow", return_value=False), \
                    patch.object(QApplication, "focusWidget", return_value=owner.table):
                before = self.engine.snap_to_beat
                key(owner.table, Qt.Key_S)
                self.assertNotEqual(self.engine.snap_to_beat, before)
                self.assertEqual(button.isChecked(), self.engine.snap_to_beat)
                self.assertIn("S", button.text())
                self.assertIn("Snap", button.accessibleName())
                repeated = QKeyEvent(QEvent.KeyPress, Qt.Key_S, Qt.NoModifier, "s", True)
                QApplication.sendEvent(owner.table, repeated)
                self.assertEqual(self.engine.snap_to_beat, not before)
                key(owner.table, Qt.Key_S)
                self.assertEqual(self.engine.snap_to_beat, before)

    def test_s_is_not_intercepted_in_text_fields_spinboxes_or_with_modifiers(self):
        timeline = Timeline(self.tracks[0], "", self.window.loader, {}, self.engine)
        self.timelines = [timeline]
        line, plain = QLineEdit(), QPlainTextEdit()
        self.addCleanup(line.deleteLater)
        self.addCleanup(plain.deleteLater)
        for owner in (self.window, timeline):
            with patch.object(owner, "isActiveWindow", return_value=True):
                for field in (self.window.search, timeline.bpm_box, self.window.player.fade_box,
                              self.window.player.mode, line, plain):
                    with patch.object(QApplication, "focusWidget", return_value=field):
                        before = self.engine.snap_to_beat
                        event = QKeyEvent(QEvent.KeyPress, Qt.Key_S, Qt.NoModifier, "s")
                        self.assertFalse(owner.eventFilter(field, event))
                        self.assertEqual(self.engine.snap_to_beat, before)
                with patch.object(QApplication, "focusWidget", return_value=owner.table):
                    for modifier in (Qt.ControlModifier, Qt.AltModifier, Qt.ShiftModifier):
                        self.assertFalse(owner.eventFilter(
                            owner.table, QKeyEvent(QEvent.KeyPress, Qt.Key_S, modifier)))

    def test_row_buttons_stay_out_of_the_tab_chain_but_are_named(self):
        item = self.window.table.topLevelItem(0)
        button = self.window.table.itemWidget(item, C_TAG)
        self.assertEqual(button.focusPolicy(), Qt.NoFocus)
        self.assertIn("One", button.accessibleName())
        self.assertTrue(self.window.table.accessibleName())
        self.assertTrue(self.window.search.accessibleName())

    def test_every_bottom_button_is_reachable_by_tab_and_has_a_name(self):
        for button in self.window.findChildren(QPushButton):
            if button.parent() is self.window.table.viewport():
                continue
            self.assertNotEqual(button.focusPolicy(), Qt.NoFocus, button.text())
            self.assertTrue(button.text() or button.accessibleName())


if __name__ == "__main__":
    unittest.main()
