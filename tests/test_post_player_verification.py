# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the verification of files a player changed, using temporary files modified after the fact.

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import app, export, settings
from djapp.export import manifest
from djapp.export.files import Report
from djapp.ui.main_window import MainWindow
from PySide6.QtWidgets import QApplication, QMessageBox


# ------------------------------------------------------------
class PostPlayerVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = [
            "Contents/demo.flac",
            "PIONEER/rekordbox/export.pdb",
            "PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT",
            "PIONEER/USBANLZ/P001/00000001/ANLZ0000.EXT",
            "PIONEER/USBANLZ/P001/00000001/ANLZ0000.2EX",
            "PIONEER/Artwork/cover.jpg",
        ]
        self.man = manifest.Manifest(str(self.root))
        for rel in self.paths:
            path = self.root.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original")
            self.man.add(str(path))
        self.man.save()

    def change(self, rel, content=b"modified"):
        self.root.joinpath(*rel.split("/")).write_bytes(content)

    def test_unchanged_files_pass_both_modes(self):
        for allow in (False, True):
            result = manifest.verify(str(self.root), allow_player_changes=allow)
            self.assertTrue(result.good)
            self.assertEqual(result.ok, len(self.paths))

    def test_player_files_are_modified_not_certified_or_damaged(self):
        for rel in self.paths[1:5]:
            self.change(rel, b"player updated content with a different size")
        result = manifest.verify(str(self.root), allow_player_changes=True)
        self.assertEqual(set(result.modified), set(self.paths[1:5]))
        self.assertEqual(result.damaged, [])
        self.assertEqual(result.ok, 2)
        self.assertFalse(result.good)
        self.assertIn("integrity unconfirmed", result.summary())
        self.assertEqual(set(manifest.verify(str(self.root)).damaged), set(self.paths[1:5]))

    def test_audio_and_artwork_remain_strict_with_mixed_results(self):
        for rel in self.paths[:3] + self.paths[5:]:
            self.change(rel)
        result = manifest.verify(str(self.root), allow_player_changes=True)
        self.assertEqual(set(result.damaged), {self.paths[0], self.paths[5]})
        self.assertEqual(set(result.modified), {self.paths[1], self.paths[2]})
        self.assertFalse(result.good)

    def test_missing_and_unreadable_player_files_remain_errors(self):
        self.root.joinpath(*self.paths[1].split("/")).unlink()
        original = manifest.sha256_drive

        def read(path, cancel):
            if Path(path).suffix == ".DAT":
                raise OSError("read failed")
            return original(path, cancel)

        with patch.object(manifest, "sha256_drive", side_effect=read):
            result = manifest.verify(str(self.root), allow_player_changes=True)
        self.assertEqual(result.missing, [self.paths[1]])
        self.assertEqual(result.damaged, [self.paths[2]])
        self.assertEqual(result.modified, [])

    def test_player_path_scope_is_narrow_and_case_insensitive(self):
        self.assertTrue(manifest.player_mutable("pioneer/USBANLZ/A/ANLZ0000.dat"))
        for path in ("Contents/ANLZ0000.DAT", "PIONEER/USBANLZ/demo.flac",
                     "PIONEER/USBANLZ/A/other.DAT", "elsewhere/export.pdb",
                     "PIONEER/rekordbox/export.pdb.bak"):
            self.assertFalse(manifest.player_mutable(path), path)

    def test_cancel_and_invalid_manifest_are_not_masked(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(manifest.Cancelled):
            manifest.verify(str(self.root), cancel=cancel, allow_player_changes=True)
        (self.root / manifest.NAME).write_text('{"files": {}}', encoding="utf-8")
        result = manifest.verify(str(self.root), allow_player_changes=True)
        self.assertTrue(result.error)
        self.assertFalse(result.good)

    def test_export_readback_uses_strict_default(self):
        self.change(self.paths[1])
        with patch.object(manifest, "Manifest", return_value=self.man), \
                patch("djapp.export.cdj.usb.export", return_value=Report()), \
                patch.object(self.man, "save"), \
                patch.object(manifest, "verify", wraps=manifest.verify) as verify:
            result = export.run("CDJ/USB", [], str(self.root),
                                {"verify_copy": True, "anlz_processes": 1})
        self.assertNotIn("allow_player_changes", verify.call_args.kwargs)
        self.assertTrue(any(self.paths[1] in error for error in result.errors))

    def test_cli_reports_modified_files_without_success_exit(self):
        self.change(self.paths[1])
        with patch.object(app, "log_line") as output:
            code = app.verify(["program", "--verify", str(self.root)])
        self.assertEqual(code, 1)
        self.assertTrue(any("possible player update" in call.args[0] for call in output.call_args_list))

    def test_ui_standalone_mode_and_modified_guidance(self):
        application = QApplication.instance() or QApplication([])
        main = MainWindow(dict(settings.DEFAULTS, auto_load=False), None)
        self.change(self.paths[1])
        result = manifest.verify(str(self.root), allow_player_changes=True)
        try:
            with patch.object(QMessageBox, "information") as information, \
                    patch.object(QMessageBox, "warning") as warning:
                main.verified(result)
            warning.assert_not_called()
            text = information.call_args.args[2]
            self.assertIn("modified since export", text)
            self.assertIn("updates to its database and analysis files are normal", text)
            self.assertIn("Audio: 1 / 1 verified", text)
            self.assertIn("Artwork: 1 / 1 verified", text)
            self.assertIn("Pioneer database / analysis: 3 / 4 verified", text)
            self.assertNotIn(self.paths[1], text)
            self.assertNotIn("Export again", text)
            self.assertIn("integrity unconfirmed", main.status.text())
            for problem in ("damaged", "missing", "error"):
                mixed = manifest.Check(modified=[self.paths[1]])
                if problem == "error":
                    mixed.error = "Cannot read the checksums"
                else:
                    getattr(mixed, problem).append(self.paths[0])
                with patch.object(QMessageBox, "information") as information, \
                        patch.object(QMessageBox, "warning") as warning:
                    main.verified(mixed)
                information.assert_not_called()
                warning.assert_called_once()
            with patch("djapp.ui.main_window.QFileDialog.getExistingDirectory",
                       return_value=str(self.root)), \
                    patch("djapp.ui.main_window.Job", return_value=Mock()) as job:
                main.verify_export()
            self.assertTrue(job.call_args.kwargs["allow_player_changes"])
        finally:
            main.job = None
            with patch("djapp.ui.main_window.settings.save"):
                main.close()
            main.deleteLater()
            application.processEvents()


if __name__ == "__main__":
    unittest.main()
