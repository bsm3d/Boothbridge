import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import soundfile as sf
from djapp import tags, tagwrite
from djapp.model import LOOP, Cue, Node, Track
from djapp.sources import folder, project
from djapp.tagfile import open_tagged
from djapp.ui import cuehistory, theme
from djapp.ui.details import PlaylistWindow
from djapp.ui.player import Engine
from djapp.ui.tageditor import TagEditor
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox


# ------------------------------------------------------------
class TagEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = str(self.root / "track.wav")
        sf.write(self.path, np.zeros(4410), 44100)
        self.track = Track(title="Original", artist="Artist", path=self.path,
                           duration=0.1, bpm=123.456789, grid=12.5)
        self.loop = Cue(kind=LOOP, start=20, length=30, hotcue=0)
        self.track.cues.append(self.loop)
        self.engine = Engine()
        self.editor = TagEditor(self.track, self.path, "Open Key", engine=self.engine)

    def tearDown(self):
        if self.editor.job is not None:
            self.editor.job.stop.set()
            self.finish_job()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.editor.close()
        self.engine.close()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.tmp.cleanup()

    def finish_job(self):
        deadline = time.monotonic() + 10
        while self.editor.job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.editor.job, "Tag-writing thread did not finish")

    def test_apply_changes_project_only_preserves_timing_and_roundtrips(self):
        original = Path(self.path).read_bytes()
        emitted = []
        self.editor.changed.connect(lambda: emitted.append(True))
        self.editor.fields["title"].setText("Changed")
        self.editor.fields["album_artist"].setText("Album artist")
        self.editor.comment.setPlainText("New comment")
        self.editor.numbers["year"].setValue(2026)
        self.editor.key.setCurrentIndex(self.editor.key.findData(21))
        self.editor.apply_button.click()
        self.assertEqual((self.track.title, self.track.album_artist, self.track.comment, self.track.year),
                         ("Changed", "Album artist", "New comment", 2026))
        self.assertEqual(self.track.key, 21)
        self.assertEqual(self.track.bpm, 123.456789)
        self.assertEqual(self.track.grid, 12.5)
        self.assertIs(self.track.cues[0], self.loop)
        self.assertEqual(Path(self.path).read_bytes(), original)
        self.assertEqual(emitted, [True])
        path = str(self.root / "project.json")
        project.save(path, [Node("playlist", "Test", tracks=[self.track])], {})
        self.assertEqual(project.load(path)[0].tracks[0].title, "Changed")
        self.editor.apply_button.click()
        self.assertEqual(emitted, [True])


    def test_musical_inputs_align_with_number_columns_and_preserve_precision(self):
        self.editor.setStyleSheet(theme.STYLE)
        self.editor.show()
        for width in (1000, 1200):
            self.editor.resize(width, 700)
            QTest.qWait(80)
            self.assertEqual(self.editor.width(), width)
            for input_box, left_name, right_name in (
                    (self.editor.bpm, "year", "track_no"),
                    (self.editor.key, "disc_no", "rating")):
                left_box = self.editor.numbers[left_name]
                right_box = self.editor.numbers[right_name]
                left = input_box.mapTo(self.editor, input_box.rect().topLeft()).x()
                expected_left = left_box.mapTo(self.editor, left_box.rect().topLeft()).x()
                right = input_box.mapTo(self.editor, input_box.rect().bottomRight()).x()
                expected_right = right_box.mapTo(self.editor, right_box.rect().bottomRight()).x()
                self.assertLessEqual(abs(left - expected_left), 1)
                self.assertLessEqual(abs(right - expected_right), 1)
            self.assertEqual(self.editor.bpm.decimals(), 6)
            self.assertEqual(self.editor.bpm.value(), 123.456789)
            self.assertEqual(self.track.grid, 12.5)
            self.assertIs(self.track.cues[0], self.loop)
            for name, maximum in (("year", 9999), ("track_no", 65535),
                                  ("disc_no", 65535), ("rating", 5)):
                self.assertEqual(self.editor.numbers[name].maximum(), maximum)

    def test_discard_and_missing_file_do_not_change_track(self):
        self.editor.fields["title"].setText("Not applied")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.editor.reject()
        self.assertEqual(self.track.title, "Original")
        missing = TagEditor(self.track, "", "Camelot")
        self.assertFalse(missing.write_button.isEnabled())
        missing.close()

    def test_concurrent_edits_are_not_silently_overwritten(self):
        self.editor.fields["title"].setText("My change")
        self.track.title = "External change"
        with patch.object(QMessageBox, "warning") as warning:
            self.editor.apply()
        warning.assert_called_once()
        self.assertEqual(self.track.title, "External change")

    def test_unchanged_fields_keep_external_edits(self):
        self.track.artist = "External artist"
        self.editor.fields["title"].setText("My title")
        self.editor.apply()
        self.assertEqual(self.track.artist, "External artist")
        self.assertEqual(self.editor.fields["artist"].text(), "External artist")

    def test_bpm_edit_uses_shared_undo_without_moving_cues_or_anchor(self):
        self.editor.bpm.setValue(130)
        self.editor.apply()
        self.assertEqual(self.track.bpm, 130)
        self.assertTrue(cuehistory.undo(self.track))
        self.assertEqual(self.track.bpm, 123.456789)
        self.assertEqual(self.track.grid, 12.5)
        self.assertIs(self.track.cues[0], self.loop)

    def test_successful_write_reports_project_conflict_without_overwriting(self):
        write = tagwrite.write

        def external_edit(*args, **kwargs):
            result = write(*args, **kwargs)
            self.track.title = "External title"
            return result

        self.editor.fields["title"].setText("Written title")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch("djapp.tagwrite.write", side_effect=external_edit), \
                patch.object(QMessageBox, "warning") as warning:
            self.editor.write_file()
            self.finish_job()
        warning.assert_called_once()
        self.assertEqual(tags.basic(self.path)["title"], "Written title")
        self.assertEqual(self.track.title, "External title")
        self.assertIn("not applied", self.editor.status.text())

    def test_playlist_reuses_open_editor_for_same_track(self):
        loader = Loader()
        self.addCleanup(loader.stop)
        window = PlaylistWindow(Node("playlist", "Test", tracks=[self.track]), self.engine, loader, {})
        self.addCleanup(window.close)
        with patch.object(TagEditor, "show"), patch.object(TagEditor, "raise_"), \
                patch.object(TagEditor, "activateWindow"):
            window.edit_tags(self.track)
            window.edit_tags(self.track)
        self.assertEqual(len(window.findChildren(TagEditor)), 1)

    def test_write_requires_confirmation_and_preview_stopped(self):
        original = Path(self.path).read_bytes()
        self.editor.fields["title"].setText("New title")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.editor.write_file()
        self.assertIsNone(self.editor.job)
        self.assertEqual(Path(self.path).read_bytes(), original)
        self.assertEqual(self.track.title, "Original")
        self.engine.path = self.path
        with patch.object(QMessageBox, "warning") as warning:
            self.editor.write_file()
        warning.assert_called_once()
        self.assertIsNone(self.editor.job)

    def test_explicit_background_write_updates_audio_project_and_backup(self):
        original = Path(self.path).read_bytes()
        self.editor.fields["title"].setText("Written title")
        self.editor.bpm.setValue(125.123456)
        writes = []
        self.editor.file_written.connect(writes.append)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QMessageBox, "critical") as critical:
            self.editor.write_file()
            self.assertFalse(self.editor.apply_button.isEnabled())
            self.editor.reject()
            self.assertIsNotNone(self.editor.job)
            self.finish_job()
        critical.assert_not_called()
        self.assertEqual(tags.basic(self.path)["title"], "Written title")
        self.assertEqual(self.track.title, "Written title")
        self.assertEqual(self.track.bpm, 125.123456)
        self.assertEqual(self.track.grid, 12.5)
        self.assertIs(self.track.cues[0], self.loop)
        self.assertEqual(Path(self.path + ".bb-tags.bak").read_bytes(), original)
        self.assertEqual(writes, [str(Path(self.path).absolute())])
        self.assertTrue(self.editor.apply_button.isEnabled())

    def test_write_sends_only_edited_fields_and_keeps_unreadable_tags(self):
        audio = open_tagged(self.path)
        for name, value in (("title", "T"), ("artist", "Old"), ("genre", "(17)"),
                            ("bpm", "320"), ("key", "xyz")):
            audio.set(name, value)
        audio.save()
        track = folder.make_track(self.path)
        self.assertEqual((track.genre, track.bpm, track.key), ("", 0, None))
        self.editor.close()
        self.editor = editor = TagEditor(track, self.path, "Open Key")
        editor.fields["title"].setText("Applied")
        editor.apply()
        editor.fields["artist"].setText("")
        sent = []
        write = tagwrite.write

        def spy(path, values, *args, **kwargs):
            sent.append(dict(values))
            return write(path, values, *args, **kwargs)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch("djapp.tagwrite.write", side_effect=spy), \
                patch.object(QMessageBox, "critical") as critical:
            editor.write_file()
            self.finish_job()
        critical.assert_not_called()
        self.assertEqual(sent, [{"title": "Applied", "artist": ""}])
        on_file = tags.basic(self.path)
        self.assertEqual((on_file["title"], on_file["genre"], on_file["bpm"], on_file["key"]),
                         ("Applied", "(17)", "320", "xyz"))
        self.assertNotIn("artist", on_file)

    def test_values_the_widgets_cannot_hold_are_not_edits(self):
        track = Track(title="x", year=20240517, track_no=70000, rating=9, key=30)
        editor = TagEditor(track, "", "Open Key")
        self.addCleanup(editor.close)
        editor.fields["title"].setText("y")
        editor.apply()
        self.assertEqual((track.title, track.year, track.track_no, track.rating, track.key),
                         ("y", 20240517, 70000, 9, 30))
        self.assertEqual(editor.values(), editor.baseline)
        editor.numbers["year"].setValue(2020)
        editor.apply()
        self.assertEqual((track.year, track.track_no), (2020, 70000))

    def test_failed_write_keeps_audio_and_project_and_reports_failure(self):
        original = Path(self.path).read_bytes()
        self.editor.fields["title"].setText("Pending title")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch("djapp.tagwrite.write", side_effect=PermissionError("Access denied")), \
                patch.object(QMessageBox, "critical") as critical, \
                self.assertLogs("djapp.ui.worker", level="ERROR"):
            self.editor.write_file()
            self.finish_job()
        critical.assert_called_once()
        self.assertEqual(self.track.title, "Original")
        self.assertEqual(Path(self.path).read_bytes(), original)
        self.assertEqual(self.editor.fields["title"].text(), "Pending title")
        self.assertTrue(self.editor.apply_button.isEnabled())

    def test_metadata_refreshes_all_open_playlists_without_resetting_cursor_or_loops(self):
        loader = Loader()
        self.addCleanup(loader.stop)
        first = PlaylistWindow(Node("playlist", "First", tracks=[self.track]), self.engine, loader, {})
        second = PlaylistWindow(Node("playlist", "Second", tracks=[self.track]), self.engine, loader, {})
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.player.track = second.player.track = self.track
        first.player.view.set_pos(40)
        self.engine.loop = (self.track, self.loop)
        self.editor.changed.connect(lambda: self.engine.metadata_changed.emit(self.track))
        self.editor.fields["artist"].setText("Changed artist")
        self.editor.fields["title"].setText("Changed title")
        self.editor.apply()
        self.assertEqual(first.table.topLevelItem(0).text(3), "Changed title")
        self.assertEqual(second.table.topLevelItem(0).text(2), "Changed artist")
        self.assertIn("Changed title", first.player.title.text())
        self.assertEqual(first.player.view.needle, 40)
        self.assertIs(self.engine.loop[1], self.loop)


if __name__ == "__main__":
    unittest.main()
