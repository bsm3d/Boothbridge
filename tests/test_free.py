# the program is free: no licence key, no lock, every export format and the music browser open for everyone

import importlib
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings
from djapp.ui import main_window
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from tests import APP_ROOT


# ------------------------------------------------------------
class NoLicenceMachineryTests(unittest.TestCase):
    def test_the_whole_cdj_format_list_is_open_by_default(self):
        self.assertEqual(settings.exports(), settings.EXPORTS)
        self.assertIn("CDJ/USB", settings.exports())

    def test_there_is_no_module_for_locks_keys_or_signatures(self):
        for name in ("djapp.locks", "djapp.ed25519"):
            with self.subTest(module=name), self.assertRaises(ImportError):
                importlib.import_module(name)

    def test_the_licence_tools_are_gone_from_the_tree(self):
        for path in ("djapp/locks.py", "djapp/ed25519.py", "tools/licence.py", "tools/licence_manager.py"):
            with self.subTest(path=path):
                self.assertFalse((APP_ROOT / path).exists())

    def test_the_branding_has_no_key_file(self):
        from djapp import brand
        self.assertFalse(hasattr(brand, "KEY_FILE"))

    def test_no_source_file_names_a_lock_or_a_licence_key(self):
        words = ("locks.require", "locks.is_open", "locks.current", "Locked(", "machine_code", "CDJ_FORMAT")
        for path in sorted((APP_ROOT / "djapp").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for word in words:
                    self.assertNotIn(word, text)


# ------------------------------------------------------------
class CommandLineTests(unittest.TestCase):
    def run_main(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
        return subprocess.run([sys.executable, str(APP_ROOT / "main.py"), *args], capture_output=True, text=True,
                              cwd=APP_ROOT, env=env, timeout=60, check=False)

    def test_machine_code_is_not_an_option_any_more(self):
        done = self.run_main("--machine-code")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown option: --machine-code", done.stdout)

    def test_the_usage_does_not_talk_about_a_key_or_a_machine_code(self):
        done = self.run_main("--help")
        self.assertEqual(done.returncode, 0)
        for word in ("machine", "licence key", "locked"):
            self.assertNotIn(word, done.stdout.lower())

    def test_the_cdj_format_is_a_plain_export_format_of_the_command_line(self):
        done = self.run_main("--help")
        self.assertIn("CDJ/USB", done.stdout)



# ------------------------------------------------------------
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        for name in ("information", "warning", "critical", "question"):
            p = patch.object(QMessageBox, name, return_value=QMessageBox.Yes)
            p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(settings, "save"), patch.object(main_window, "fs_type", lambda path: "FAT32")):
            p.start()
            self.addCleanup(p.stop)
        cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False)
        self.main = MainWindow(cfg, LogWindow())
        self.addCleanup(self.close_main)

    def close_main(self):
        self.main.key_timer.stop()
        self.main.close()
        self.main.deleteLater()
        self.app.processEvents()

    def test_the_export_list_has_cdj_usb_and_nothing_marked_locked(self):
        shown = [self.main.fmt.itemData(i) for i in range(self.main.fmt.count())]
        self.assertEqual(shown, list(settings.EXPORTS))
        for i in range(self.main.fmt.count()):
            self.assertNotIn("locked", self.main.fmt.itemText(i).lower())

    def test_there_is_no_licence_window_in_the_menus(self):
        titles = [a.text() for menu in self.main.menuBar().findChildren(QMenu) for a in menu.actions()]
        self.assertTrue(titles)
        for text in titles:
            self.assertNotIn("licence", text.lower())
            self.assertNotIn("locked", text.lower())

    def test_the_music_browser_opens_without_any_key(self):
        self.main.open_music_browser()
        self.assertIsNotNone(self.main.music_browser)
        self.assertTrue(self.main.music_browser.isVisible())
        self.main.music_browser.close()


if __name__ == "__main__":
    unittest.main()
