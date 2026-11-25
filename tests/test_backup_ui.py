# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the backup window UI: playlist duration counts and the backup and restore dialogs.

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import backup, settings
from djapp.model import Node, Track
from djapp.sources import project
from djapp.ui.backup import BackupPanel
from djapp.ui.details import PlaylistWindow
from djapp.ui.dialogs import About, LogWindow
from djapp.ui.main_window import MainWindow, playlist_duration
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)


# ------------------------------------------------------------
class BackupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "song.mp3"
        self.source.write_bytes(b"source audio")
        self.log = LogWindow()
        self.window = MainWindow(dict(settings.DEFAULTS, auto_load=False), self.log)
        self.window.nodes = [
            Node("playlist", "First", tracks=[Track(path=str(self.source))]),
            Node("playlist", "Unticked", tracks=[Track(path=str(self.source), title="Edited")]),
        ]
        self.window.fill_tree()
        self.window.set_dirty(True)

    def test_playlist_duration_counts_occurrences_and_truncates_only_total(self):
        track = Track(duration=1863.4)
        self.assertEqual(playlist_duration(Node("playlist", "Empty")), "0:00:00")
        self.assertEqual(
            playlist_duration(Node("playlist", "Repeated", tracks=[track, track])), "1:02:06",
        )
        self.assertEqual(
            playlist_duration(Node("playlist", "Fractional",
                                   tracks=[Track(duration=0.6), Track(duration=0.6)])), "0:00:01",
        )

    def test_duration_tree_column_and_edited_refresh(self):
        playlist = Node("playlist", "Timed", tracks=[Track(duration=3726)])
        self.window.nodes = [Node("folder", "Folder", children=[playlist]),
                             Node("playlist", "Empty")]
        self.window.fill_tree()
        folder = self.window.tree.topLevelItem(0)
        item = folder.child(0)
        self.assertEqual(self.window.tree.headerItem().text(2), "Duration")
        self.assertEqual(folder.text(2), "")
        self.assertEqual(item.text(2), "1:02:06")
        self.assertEqual(self.window.tree.topLevelItem(1).text(2), "0:00:00")
        self.window.details(item)
        panel = next(p for p in self.window.findChildren(PlaylistWindow) if p.node is playlist)
        playlist.tracks.append(Track(duration=54))
        panel.edited.emit()
        self.assertEqual((item.text(1), item.text(2)), ("2", "1:03:00"))


    def test_tools_audio_verification_action_opens_panel_with_collection_and_root(self):
        tools = next(action.menu() for action in self.window.menuBar().actions()
                     if action.text().replace("&", "") == "Tools")
        action = next(action for action in tools.actions() if action.text() == "Verify audio files...")
        self.window.root.setText(str(self.root))
        with patch("djapp.ui.main_window.AudioCheck") as panel:
            action.trigger()
            panel.assert_called_once_with(self.window.nodes, str(self.root), self.window)
            panel.return_value.exec.assert_called_once_with()

    def test_unified_open_routes_projects_and_collections_with_separate_filters(self):
        for extension, expected in ((".json", "project"), (".nml", "traktor")):
            with self.subTest(extension=extension):
                path = str(self.root / ("collection" + extension))
                if extension == ".json":
                    project.save(path, self.window.nodes, {})
                with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")) as picker, \
                        patch.object(self.window, "ask_discard", return_value=True) as discard, \
                        patch.object(self.window, "reload") as reload:
                    self.window.open_source()
                self.assertEqual(self.window.src.text(), path)
                discard.assert_called_once_with()
                reload.assert_called_once_with()
                self.assertEqual(picker.call_args.args[1], "Open project / collection")
                filters = picker.call_args.args[3].split(";;")
                self.assertEqual(len(filters), 4)
                self.assertIn("*.json", filters[0])
                self.assertIn("*.nml", filters[0])
                self.assertEqual(filters[1], "Boothbridge projects (*.json)")
                self.assertNotIn("*.json", filters[2])
                from djapp import sources

                self.assertEqual(sources.detect(path), expected)

    def test_unified_open_preserves_source_when_discard_or_picker_cancelled(self):
        original = self.window.src.text()
        for path in (str(self.root / "new.json"), str(self.root / "new.nml")):
            with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")), \
                    patch.object(self.window, "ask_discard", return_value=False), \
                    patch.object(self.window, "reload") as reload:
                self.window.open_source()
            self.assertEqual(self.window.src.text(), original)
            reload.assert_not_called()
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")), \
                patch.object(self.window, "ask_discard") as discard:
            self.window.open_source()
        discard.assert_not_called()
        with patch.object(self.window, "open_source") as open_source:
            self.window.open_project()
        open_source.assert_called_once_with()

    def tearDown(self):
        if self.window.job is not None:
            self.window.job.stop.set()
            self.finish_job()
        self.window.set_dirty(False)
        with patch.object(settings, "save"):
            self.window.close()
        self.window.deleteLater()
        self.window.engine.deleteLater()
        self.log.close()
        self.log.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.tmp.cleanup()

    def finish_job(self):
        deadline = time.monotonic() + 10
        while self.window.job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window.job)

    def test_menu_starts_real_backup_of_every_loaded_playlist_and_keeps_dirty_state(self):
        target = self.root / "project-backup"
        panel = BackupPanel(self.window)
        with patch.object(QMessageBox, "question", side_effect=[QMessageBox.Yes, QMessageBox.No]) as done, \
                patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
                patch.object(QMessageBox, "critical") as failed:
            panel.actions[0].click()
            self.assertIsNotNone(self.window.job)
            self.assertTrue(self.window.stop.isEnabled())
            self.assertFalse(self.window.go.isEnabled())
            self.finish_job()
        failed.assert_not_called()
        self.assertEqual(done.call_count, 2)
        self.assertIn("Verify this backup now?", done.call_args.args[2])
        with zipfile.ZipFile(str(target) + ".zip") as archive:
            self.assertEqual(sum(name.endswith(".m3u8") for name in archive.namelist()), 2)
            self.assertEqual(sum(name.startswith("Audio/") for name in archive.namelist()), 1)
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.project, "")
        self.assertTrue(self.window.go.isEnabled())
        self.assertFalse(self.window.stop.isEnabled())
        self.assertEqual(self.window.bar.value(), 100)

    def test_user_can_decline_or_cancel_file_picker_without_starting_job(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No), \
                patch.object(QFileDialog, "getSaveFileName") as picker:
            self.window.backup_project()
        picker.assert_not_called()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            self.window.backup_project()
        self.assertIsNone(self.window.job)

    def test_missing_audio_failure_is_reported_and_controls_are_restored(self):
        self.source.unlink()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QFileDialog, "getSaveFileName", return_value=(str(self.root / "fail.zip"), "")), \
                patch.object(QMessageBox, "critical") as failed, \
                self.assertLogs("djapp.ui.worker", level="ERROR"):
            self.window.backup_project()
            self.finish_job()
        failed.assert_called_once()
        self.assertFalse((self.root / "fail.zip").exists())
        self.assertTrue(self.window.go.isEnabled())
        self.assertIn("failed", self.window.status.text())

    def test_verify_and_restore_menu_actions_run_real_jobs(self):
        path = str(self.root / "backup.zip")
        backup.create(path, project.dump(self.window.nodes, {}))
        panel = BackupPanel(self.window)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")), \
                patch.object(QMessageBox, "information") as message:
            panel.actions[2].click()
            self.finish_job()
        message.assert_called_once()
        self.assertEqual(self.window.status.text(), "ZIP checksum verified")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")), \
                patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root)), \
                patch("djapp.ui.main_window.QInputDialog.getText", return_value=("Restored", True)), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            panel.actions[1].click()
            self.finish_job()
        restored = self.root / "Restored" / "project.json"
        self.assertTrue(restored.exists())
        nodes = project.load(str(restored))
        self.assertEqual(nodes[1].tracks[0].title, "Edited")
        self.assertTrue(self.window.dirty)
        self.assertEqual(self.window.project, "")

    def test_file_and_tools_group_actions_without_losing_shortcuts(self):
        menus = {action.text().replace("&", ""): action.menu()
                 for action in self.window.menuBar().actions()}
        file_actions = {action.text().replace("&", ""): action for action in menus["File"].actions()
                        if not action.isSeparator()}
        tools = {action.text().replace("&", ""): action for action in menus["Tools"].actions()
                 if not action.isSeparator()}
        self.assertEqual(set(file_actions), {"Open project / collection...", "New playlist...", "Open a music folder...", "Open an Engine DJ library...",
                                            "Open a CDJ USB key...", "Save the project", "Save the project as...",
                                            "Convert", "Exit"})
        self.assertEqual(file_actions["Open project / collection..."].shortcut().toString(), "Ctrl+O")
        self.assertEqual(file_actions["Open a CDJ USB key..."].shortcut().toString(), "Ctrl+Shift+K")
        expected = {"Reload collection": "Ctrl+R", "Check collection...": "Ctrl+K",
                    "Verify audio files...": "",
                    "Verify an export...": "Ctrl+Shift+V", "Backup": "",
                    "Format a USB drive (FAT32)...": "", "Eject the output drive": "Ctrl+E"}
        self.assertEqual(set(tools), set(expected))
        for text, shortcut in expected.items():
            self.assertEqual(tools[text].shortcut().toString(), shortcut)
        self.assertEqual(sum(action.isSeparator() for action in menus["Tools"].actions()), 2)

    def test_about_retains_author_and_license_without_donation_button_or_link(self):
        about = About()
        try:
            self.assertEqual([button.text() for button in about.findChildren(QPushButton)], ["Close"])
            text = " ".join(label.text() for label in about.findChildren(QLabel))
            self.assertIn("Saint-Moulin", text)
            self.assertIn("PolyForm", text)
            self.assertNotIn("ko-fi", text.casefold())
        finally:
            about.close()
            about.deleteLater()

    def test_open_restored_project_preserves_unsaved_work_prompt(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(self.window, "ask_discard", return_value=False) as discard:
            self.window.restore_finished("restored-project.json")
        discard.assert_called_once()
        self.assertFalse(self.window.again)
        self.assertNotEqual(self.window.src.text(), "restored-project.json")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(self.window, "ask_discard", return_value=True), \
                patch.object(self.window, "reload") as reload:
            self.window.restore_finished("restored-project.json")
            self.app.processEvents()
            reload.assert_called_once()
        self.assertFalse(self.window.again)
        self.assertEqual(self.window.src.text(), "restored-project.json")
        self.window.again = False


if __name__ == "__main__":
    unittest.main()
