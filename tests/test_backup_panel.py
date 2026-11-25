# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the backup panel of the window: its buttons, status text and the jobs it starts.

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import backup, settings
from djapp.export.files import Cancelled
from djapp.model import Node
from djapp.ui.backup import BackupPanel
from djapp.ui.main_window import MainWindow
from djapp.ui.worker import Job
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox


# ------------------------------------------------------------
class BackupPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        save = patch.object(settings, "save")
        save.start()
        self.addCleanup(save.stop)
        cfg = dict(settings.DEFAULTS)
        cfg["auto_load"] = False
        self.main = MainWindow(cfg, None)
        self.main.nodes = [Node(kind="playlist", name="Empty")]
        self.panel = BackupPanel(self.main)
        self.main.backup_panel = self.panel
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.main.job is not None:
            self.main.job.stop.set()
            self.main.job.wait(5000)
            self.app.processEvents()
        self.panel.close()
        self.main.engine.close()
        self.main.dirty = False
        with patch.object(settings, "save"):
            self.main.close()
        self.main.deleteLater()
        self.app.processEvents()

    def wait_for_jobs(self):
        for _ in range(500):
            time.sleep(.01)
            self.app.processEvents()
            if self.main.job is None and not self.main.pending_backup_verify:
                self.app.processEvents()
                if self.main.job is None:
                    return
        self.fail("Backup/verification jobs did not finish")

    def test_menu_has_one_backup_entry_and_opens_modal_panel(self):
        tools = next(action.menu() for action in self.main.menuBar().actions()
                     if action.text() == "&Tools")
        entries = [a for a in tools.actions() if "backup" in a.text().lower()]
        self.assertEqual([a.text() for a in entries], ["Backup"])
        self.assertEqual([b.text() for b in self.panel.actions],
                         ["Save backup...", "Load / restore backup...", "Verify backup..."])
        opened = []

        def close_panel():
            panel = self.main.backup_panel
            opened.append(isinstance(panel, BackupPanel) and panel.isVisible())
            panel.reject()

        QTimer.singleShot(0, close_panel)
        entries[0].trigger()
        self.assertEqual(opened, [True])
        self.assertIsNone(self.main.backup_panel)
        self.main.backup_panel = self.panel

    def test_success_offers_verify_exact_new_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "backup.zip")
            from djapp import backup

            with patch("djapp.ui.main_window.QFileDialog.getSaveFileName",
                       return_value=(path, "")), \
                 patch("djapp.ui.main_window.QMessageBox.question",
                       return_value=QMessageBox.Yes) as question, \
                 patch("djapp.ui.main_window.QMessageBox.information") as information, \
                 patch("djapp.ui.main_window.QFileDialog.getOpenFileName") as picker, \
                 patch("djapp.ui.main_window.backup.verify", wraps=backup.verify) as verify:
                self.panel.actions[0].click()
                self.assertFalse(any(b.isEnabled() for b in self.panel.actions))
                self.assertTrue(self.panel.cancel_button.isEnabled())
                self.wait_for_jobs()
                self.assertTrue(Path(path).is_file())
                self.assertEqual(question.call_count, 2)
                self.assertIn("Verify this backup now?", question.call_args.args[2])
                self.assertEqual(question.call_args.args[-1], QMessageBox.Yes)
                self.assertEqual(verify.call_args.args, (path,))
                picker.assert_not_called()
                self.assertEqual(self.main.status.text(), "ZIP checksum verified")
                self.assertEqual(self.panel.status.text(), self.main.status.text())
                information.assert_called_once()
                self.assertTrue(all(b.isEnabled() for b in self.panel.actions))
                self.assertFalse(self.panel.cancel_button.isEnabled())

    def test_animation_only_runs_during_save_and_renders_moving_blocks(self):
        self.panel.show()
        self.assertFalse(self.panel.animation.isVisible())
        self.assertFalse(self.panel.animation.timer.isActive())
        save = Job(backup.create)
        self.panel.watch(save)
        self.assertTrue(self.panel.animation.isVisible())
        self.assertTrue(self.panel.animation.timer.isActive())
        self.assertEqual(self.panel.animation.timer.interval(), 100)
        deadline = time.monotonic() + 1
        while self.panel.animation.phase == 0 and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertNotEqual(self.panel.animation.phase, 0)
        self.panel.animation.timer.stop()
        self.panel.animation.phase = 0
        first = self.panel.animation.grab().toImage()
        self.panel.animation.advance()
        second = self.panel.animation.grab().toImage()
        self.assertNotEqual(first, second)
        self.panel.job_finished()
        self.assertFalse(self.panel.animation.isVisible())
        self.assertFalse(self.panel.animation.timer.isActive())
        for operation in (backup.verify, backup.restore):
            job = Job(operation)
            self.panel.watch(job)
            self.assertFalse(self.panel.animation.isVisible())
            self.assertFalse(self.panel.animation.timer.isActive())
            self.panel.job_finished()
            job.deleteLater()
        save.deleteLater()

    def test_declining_verify_does_not_start_check(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "backup.zip")
            with patch("djapp.ui.main_window.QFileDialog.getSaveFileName",
                       return_value=(path, "")), \
                 patch("djapp.ui.main_window.QMessageBox.question",
                       side_effect=[QMessageBox.Yes, QMessageBox.No]), \
                 patch("djapp.ui.main_window.backup.verify") as verify:
                self.panel.launch(self.main.backup_project)
                self.wait_for_jobs()
                verify.assert_not_called()
                self.assertEqual(self.main.status.text(), "ZIP backup completed")

    def test_verify_after_worker_finishes_inside_confirmation(self):
        def answer(parent, title, *args):
            if title == "ZIP backup completed":
                # A real modal question processes the worker's finished signal.
                for _ in range(500):
                    time.sleep(.01)
                    self.app.processEvents()
                    if self.main.job is None:
                        break
                self.assertIsNone(self.main.job)
            return QMessageBox.Yes

        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "backup.zip")
            with patch("djapp.ui.main_window.QFileDialog.getSaveFileName",
                       return_value=(path, "")), \
                 patch("djapp.ui.main_window.QMessageBox.question",
                       side_effect=answer), \
                 patch("djapp.ui.main_window.QMessageBox.information") as information, \
                 patch("djapp.ui.main_window.QFileDialog.getOpenFileName") as picker:
                self.panel.launch(self.main.backup_project)
                self.wait_for_jobs()
                picker.assert_not_called()
                information.assert_called_once()
                self.assertEqual(self.main.status.text(), "ZIP checksum verified")
                self.assertEqual(self.panel.status.text(), "ZIP checksum verified")
                self.assertTrue(all(b.isEnabled() for b in self.panel.actions))
                self.assertFalse(self.panel.cancel_button.isEnabled())

    def test_close_running_panel_requests_cancel_and_restores_controls(self):
        def slow(*args, cancel, progress, **kwargs):
            cancel.wait(5)
            if cancel.is_set():
                raise Cancelled()
            raise AssertionError("Cancellation was not requested")

        self.panel.show()
        with patch("djapp.ui.main_window.QFileDialog.getOpenFileName",
                   return_value=("test.zip", "")), \
             patch("djapp.ui.main_window.backup.verify", slow):
            self.panel.actions[2].click()
            self.panel.close()
            self.assertTrue(self.panel.isVisible())
            self.wait_for_jobs()
        self.assertEqual(self.panel.status.text(), "Operation cancelled")
        self.assertTrue(all(b.isEnabled() for b in self.panel.actions))
        self.panel.close()
        self.assertFalse(self.panel.isVisible())


if __name__ == "__main__":
    unittest.main()
