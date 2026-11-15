import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.model import Cue, Node, Track
from djapp.ui import details, theme
from djapp.ui.details import (
    C_ALSO,
    C_CUES,
    C_TAG,
    COLS,
    PlaylistWindow,
    track_identity,
)
from djapp.ui.main_window import playlist_duration
from djapp.ui.player import Engine
from djapp.ui.timeline import Timeline
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton


# ------------------------------------------------------------
class PlaylistDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.track = Track(title="Test track", artist="Test artist")
        self.node = Node(kind="playlist", name="Test playlist", tracks=[self.track])
        self.engine = Engine()
        self.window = PlaylistWindow(self.node, self.engine, Loader(), {})

    def tearDown(self):
        self.window.close()
        self.window.loader.stop()
        self.engine.close()
        self.engine.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_last_column_has_id_tag_button_per_track(self):
        self.assertEqual(COLS[-1], "ID Tag")
        item = self.window.table.topLevelItem(0)
        button = self.window.table.itemWidget(item, C_TAG)
        self.assertIsInstance(button, QPushButton)
        self.assertEqual(button.text(), "ID Tag")

        with patch.object(self.window, "edit_tags") as edit_tags:
            button.click()

        edit_tags.assert_called_once_with(self.track)

    def test_footer_duration_matches_tree_and_selected_total_without_truncation(self):
        self.node.tracks = [Track(duration=6700.6), Track(duration=6700.6)]
        self.window.fill()
        self.window.table.clearSelection()
        self.window.summary()
        self.assertEqual(playlist_duration(self.node), "3:43:21")
        self.assertEqual(self.window.count.text(), "2 tracks, 3:43:21")
        self.window.table.selectAll()
        self.window.summary()
        self.assertEqual(self.window.count.text(), "2 of 2 selected, 3:43:21")

    def test_the_hint_is_short_and_the_full_list_is_on_hover(self):
        hint = next(
            label for label in self.window.findChildren(QLabel) if label.text().startswith("Space / P play")
        )
        self.assertIn("hover for all", hint.text())
        self.assertLess(len(hint.text()), 110)
        self.assertIn("Shift+A-H: delete it", hint.toolTip())
        self.assertIn("drop files or folders to add", hint.toolTip())

    def test_the_cancel_button_is_hidden_until_a_detection_runs(self):
        self.assertTrue(self.window.detect_cancel.isHidden())

    def test_detection_controls_do_not_force_an_oversized_window(self):
        self.window.setStyleSheet(theme.STYLE)
        self.window.resize(1200, 650)
        self.window.detect_cancel.show()  # the widest the row gets
        self.window.show()
        self.app.processEvents()
        self.assertLessEqual(self.window.width(), 1200)
        self.assertTrue(self.window.rect().contains(
            self.window.detect_cancel.mapTo(self.window, self.window.detect_cancel.rect().bottomRight()),
        ))

    def test_search_and_private_tools_share_one_row(self):
        layout = self.window.layout()
        self.assertEqual(layout.count(), 4)
        top = self.window.player.toolbar.itemAtPosition(1, 0).widget().layout()
        self.assertIs(top.itemAt(1).widget(), self.window.search)
        self.assertEqual(self.window.search.maximumWidth(), 480)
        tools = top.itemAt(2).layout()
        buttons = [tools.itemAt(i).widget() for i in range(tools.count())]
        self.assertEqual(
            [button.text() for button in buttons],
            ["Reload", "Remove duplicates", "Detect grids", "Cancel detection",
             "Add files..."],
        )
        keys = self.window.player.toolbar.itemAtPosition(1, 1).layout()
        self.assertIsNotNone(keys.itemAt(0).spacerItem())
        self.assertEqual(keys.itemAt(1).widget().text(), "Keys")
        self.assertIs(keys.itemAt(2).widget(), self.window.key_btn)

    def test_add_files_and_offset_right_edges_share_dynamic_grid_column(self):
        self.window.setStyleSheet(theme.STYLE)
        self.window.show()
        self.window.player.timer.stop()
        controls = self.window.player.toolbar.itemAtPosition(1, 0).widget()
        add = next(button for button in controls.findChildren(QPushButton)
                   if button.text() == "Add files...")
        offset = self.window.player.metronome_controls.offset
        for width in (1200, 1480):
            self.window.resize(width, 650)
            for text in ("00:00.0", "99:59.9", "120:00.0"):
                self.window.player.time.setText(text)
                QTest.qWait(80)
                self.assertEqual(self.window.width(), width)
                add_right = add.mapTo(self.window, QPoint(add.width() - 1, 0)).x()
                offset_right = offset.mapTo(self.window, QPoint(offset.width() - 1, 0)).x()
                self.assertLessEqual(abs(add_right - offset_right), 1)
                self.assertLessEqual(self.window.search.width(), 480)

    def test_double_click_plays_track_except_cues_column(self):
        item = self.window.table.topLevelItem(0)
        with patch.object(self.window, "play") as play:
            self.window.double(item, 3)
            play.assert_called_once_with(self.track)

        with patch.object(details, "Timeline") as timeline:
            self.window.double(item, C_CUES)
        timeline.assert_called_once()
        timeline.return_value.changed.connect.assert_called_once_with(self.window.cues_changed)
        timeline.return_value.show.assert_called_once_with()

    def test_real_double_click_on_cues_opens_editor_without_starting_audio(self):
        self.window.show()
        self.app.processEvents()
        table = self.window.table
        item = table.topLevelItem(0)
        table.scrollToItem(item)
        rect = table.visualItemRect(item)
        point = rect.center()
        point.setX(table.header().sectionViewportPosition(C_CUES) + table.columnWidth(C_CUES) // 2)
        with patch.object(self.window, "play") as play, \
                patch.object(self.engine, "play") as engine_play, \
                patch.object(details, "Timeline") as timeline:
            QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, point)
            QTest.mouseDClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, point)
            QTest.mouseRelease(table.viewport(), Qt.LeftButton, Qt.NoModifier, point)
            self.app.processEvents()
        timeline.assert_called_once()
        play.assert_not_called()
        engine_play.assert_not_called()
        self.assertFalse(self.engine.playing)

    def test_row_activation_plays_once_and_cue_activation_never_plays(self):
        item = self.window.table.topLevelItem(0)
        with patch.object(self.window, "play") as play:
            self.window.table.itemActivated.emit(item, 3)
        play.assert_called_once_with(self.track)
        with patch.object(self.window, "play") as play, patch.object(details, "Timeline") as timeline:
            self.window.table.itemActivated.emit(item, C_CUES)
            self.window.table.itemActivated.emit(item, C_TAG)
        timeline.assert_called_once()
        play.assert_not_called()

    def test_id_tag_button_opens_metadata_dialog_not_on_double_click(self):
        item = self.window.table.topLevelItem(0)
        with patch.object(self.window, "findChildren", return_value=[]), \
                patch.object(details, "Properties") as properties:
            self.window.edit_tags(self.track)

        properties.assert_called_once_with(
            self.track, self.window.resolve(self.track), self.window.key_btn.text(), self.window,
            engine=self.engine,
        )
        properties.return_value.show.assert_called_once_with()

        with patch.object(self.window, "play") as play:
            self.window.double(item, C_TAG)
        play.assert_not_called()

    def test_playlist_edits_can_be_undone_and_redone_in_order(self):
        second = Track(title="Second", path="second.mp3")
        third = Track(title="Third", path="third.mp3")
        self.node.tracks.extend([second, third])
        self.window.fill()
        self.assertFalse(self.window.undo_button.isEnabled())

        self.window.table.setCurrentItem(self.window.table.topLevelItem(1))
        self.window.table.topLevelItem(1).setSelected(True)
        self.window.remove()
        self.assertEqual(self.node.tracks, [self.track, third])
        self.window.add_paths([])
        self.assertTrue(self.window.undo_button.isEnabled())

        self.assertTrue(self.window.undo_cues())
        self.assertEqual(self.node.tracks, [self.track, second, third])
        self.assertTrue(self.window.redo_button.isEnabled())
        self.assertEqual(self.window.table.topLevelItemCount(), 3)
        self.assertTrue(self.window.undo_cues(True))
        self.assertEqual(self.node.tracks, [self.track, third])
        self.assertFalse(self.window.undo_cues(True))

        self.window.undo_cues()
        self.assertFalse(self.window.undo_cues())
        self.assertFalse(self.window.undo_button.isEnabled())

    def test_undo_covers_reorder_and_new_edit_clears_redo(self):
        second = Track(title="Second", path="second.mp3")
        self.node.tracks.append(second)
        self.window.fill()
        self.window.change_tracks([second, self.track])
        self.window.undo_cues()
        self.assertEqual(self.node.tracks, [self.track, second])
        self.window.change_tracks([second])
        self.assertFalse(self.window.redo_button.isEnabled())

    def test_undo_follows_cue_edits_and_id_tag_changes_in_one_history(self):
        self.window.player.track = self.track
        self.window.player.load(self.track, "")
        self.track.duration = 10
        self.window.player.view.set_pos(1500)
        self.window.player.pad(0)
        self.assertEqual(len(self.track.cues), 1)
        self.assertEqual(self.window.undo_stack[-1][0], "cue")

        before = self.window.fields_of(self.track)
        self.track.title = "Retitled"
        self.window.push(("fields", self.track, before, self.window.fields_of(self.track)))
        self.window.undo_cues()
        self.assertEqual(self.track.title, "Test track")
        self.assertEqual(len(self.track.cues), 1)

        self.window.undo_cues()
        self.assertEqual(self.track.cues, [])
        self.window.undo_cues(True)
        self.window.undo_cues(True)
        self.assertEqual((self.track.title, len(self.track.cues)), ("Retitled", 1))

    def test_player_undo_does_not_add_a_history_step(self):
        self.window.player.load(self.track, "")
        self.window.player.view.set_pos(800)
        self.window.player.pad(1)
        steps = len(self.window.undo_stack)
        self.window.player.undo()
        self.assertEqual(len(self.window.undo_stack), steps)
        self.assertEqual(self.track.cues, [])

    def test_reload_clears_history(self):
        self.window.change_tracks([])
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.window.reload_playlist()
        self.assertFalse(self.window.undo_stack)
        self.assertFalse(self.window.undo_button.isEnabled())
        self.assertEqual(self.node.tracks, [self.track])

    def test_also_in_lists_other_loaded_playlists_by_id_or_path(self):
        self.track.audio_id = "abc"
        self.track.path = "music\\a.mp3"
        same_id = Track(title="Other copy", audio_id="abc")
        same_path = Track(title="Path copy", path="music\\..\\music\\a.mp3")
        unrelated = Track(title="Unrelated", path="b.mp3")
        first = Node(kind="playlist", name="Warm up", tracks=[same_id, unrelated])
        second = Node(kind="playlist", name="Peak", tracks=[same_path, same_id])
        window = PlaylistWindow(self.node, self.engine, Loader(), {},
                                others=lambda: [self.node, first, second])
        self.addCleanup(window.close)
        self.addCleanup(window.loader.stop)

        self.assertEqual(window.also_in(self.track), ["Warm up", "Peak"])
        item = window.table.topLevelItem(0)
        self.assertEqual(item.text(C_ALSO), "2")
        self.assertIn("Warm up, Peak", item.toolTip(C_ALSO))
        window.table.setCurrentItem(item)
        item.setSelected(True)
        self.assertEqual(window.count.text(), "Also in: Warm up, Peak")

        self.node.tracks.append(Track(title="Alone", path="alone.mp3"))
        window.fill()
        self.assertEqual(window.table.topLevelItem(1).text(C_ALSO), "")
        self.assertEqual(window.also_in(self.node.tracks[1]), [])

    def test_also_in_is_empty_without_other_playlists(self):
        self.assertEqual(self.window.also_in(self.track), [])
        self.assertEqual(self.window.table.topLevelItem(0).text(C_ALSO), "")

    def test_also_in_column_sorts_by_count(self):
        popular = Track(title="Popular", audio_id="p")
        self.node.tracks.append(popular)
        other = Node(kind="playlist", name="Other", tracks=[Track(audio_id="p")])
        window = PlaylistWindow(self.node, self.engine, Loader(), {}, others=lambda: [other])
        self.addCleanup(window.close)
        self.addCleanup(window.loader.stop)
        window.sort_by(C_ALSO)
        window.sort_by(C_ALSO)
        self.assertIs(window.table.topLevelItem(0).data(0, Qt.UserRole), popular)

    def test_remove_duplicates_button_keeps_lowest_playlist_number(self):
        first = Track(title="First copy", audio_id="same-id", path="first.mp3")
        middle = Track(title="Different track", path="other.mp3")
        later = Track(title="Later copy", audio_id="same-id", path="copy.mp3")
        path_first = Track(title="Path copy", path="folder\\path.mp3")
        path_later = Track(title="Path copy duplicate", path="folder\\..\\folder\\path.mp3")
        no_identity = Track(title="Missing path")
        self.node.tracks = [first, middle, later, path_first, path_later, no_identity]
        self.window.fill()

        buttons = self.window.findChildren(QPushButton)
        remove_button = next(button for button in buttons if button.text() == "Remove duplicates")
        self.assertIn("lowest playlist number", remove_button.toolTip())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question:
            remove_button.click()

        self.assertEqual(self.node.tracks, [first, middle, path_first, no_identity])
        question.assert_called_once()
        self.assertIn("lowest playlist number", question.call_args.args[2])
        self.assertEqual(self.window.count.text(), "Removed 2 duplicate track(s)")

    def test_remove_duplicates_can_be_cancelled(self):
        first = Track(title="First copy", audio_id="same-id")
        later = Track(title="Later copy", audio_id="same-id")
        self.node.tracks = [first, later]

        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.window.remove_duplicates()

        self.assertEqual(self.node.tracks, [first, later])

    def test_track_identity_skips_empty_paths_and_normalizes_path(self):
        self.assertIsNone(track_identity(Track(title="No file")))
        self.assertEqual(
            track_identity(Track(path="folder\\..\\song.mp3")),
            track_identity(Track(path=os.path.abspath("song.mp3"))),
        )

    def test_reload_restores_original_tracks_order_and_cues(self):
        second = Track(title="Second track", path="second.mp3")
        self.track.path = "first.mp3"
        self.track.cues.append(Cue(start=1200, hotcue=0))
        self.node.tracks.append(second)
        self.window._initial_tracks = [(track, copy.deepcopy(track))
                                       for track in self.node.tracks]
        self.window.fill()
        self.window.table.setCurrentItem(self.window.table.topLevelItem(0))

        self.track.title = "Changed title"
        self.track.cues[0].start = 5000
        self.node.tracks[:] = [second, Track(title="Added track", path="added.mp3")]
        self.window.where[id(self.track)] = "stale-path"

        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.window.reload_playlist()

        self.assertEqual(self.node.tracks, [self.track, second])
        self.assertEqual((self.track.title, self.track.cues[0].start), ("Test track", 1200))
        self.assertEqual(self.window.current(), self.track)
        self.assertNotEqual(self.window.where[id(self.track)], "stale-path")
        self.assertEqual(self.window.player.track, self.track)
        self.assertFalse(self.engine.path)

    def test_reload_cancel_keeps_current_playlist_changes(self):
        added = Track(title="Added track", path="added.mp3")
        self.node.tracks.append(added)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.window.reload_playlist()

        self.assertEqual(self.node.tracks, [self.track, added])


    def test_playlist_undo_refreshes_timeline_beatgrid_and_player_metadata(self):
        self.track.duration = 10
        self.track.bpm = 120
        self.track.grid = 500
        self.window.player.track = self.track
        timeline = Timeline(self.track, "", self.window.loader, {}, self.engine, self.window)
        timeline.changed.connect(self.window.cues_changed)

        timeline.bpm_box.setValue(124)
        timeline.grid_box.setValue(600)
        timeline.apply_beatgrid()
        self.assertEqual(self.track.bpm, 124)
        self.assertIn("BPM 124", self.window.player.meta.text())

        self.window.undo_cues()

        self.assertEqual((self.track.bpm, self.track.grid), (120, 500))
        self.assertEqual(timeline.bpm_box.value(), 120)
        self.assertEqual(timeline.grid_box.value(), 500)
        self.assertIn("BPM 120", self.window.player.meta.text())
        timeline.close()


if __name__ == "__main__":
    unittest.main()
