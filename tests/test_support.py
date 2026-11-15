import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from djapp import brand, settings
from djapp.ui import support

from tests import APP_ROOT

ROOT = APP_ROOT


# ------------------------------------------------------------
class SupportTests(unittest.TestCase):
    def setUp(self):
        QApplication.instance() or QApplication([])

    def test_the_support_page_is_the_authors_ko_fi(self):
        self.assertEqual(brand.SUPPORT_URL, "https://ko-fi.com/bsm3d")

    def test_nothing_is_asked_without_a_link(self):
        cfg = {"support_asked": False}
        self.assertFalse(support.ask_once(None, cfg))
        self.assertFalse(cfg["support_asked"])

    def test_the_thank_you_comes_once_and_opens_the_page_only_on_a_click(self):
        opened = []
        with patch.object(support, "SUPPORT_URL", "https://ko-fi.com/x"), patch.object(
            support, "open_link", lambda: opened.append(1)
        ), patch.object(QMessageBox, "exec", lambda self: 0), patch.object(
            QMessageBox, "clickedButton", lambda self: None
        ):
            cfg = {"support_asked": False}
            self.assertTrue(support.ask_once(None, cfg))
            self.assertTrue(cfg["support_asked"])
            self.assertEqual(opened, [])
            self.assertFalse(support.ask_once(None, cfg))

    def test_a_click_on_support_opens_the_page(self):
        opened = []
        with patch.object(support, "SUPPORT_URL", "https://ko-fi.com/x"), patch.object(
            support, "open_link", lambda: opened.append(1)
        ), patch.object(QMessageBox, "exec", lambda self: 0), patch.object(
            QMessageBox, "clickedButton", lambda self: self.buttons()[0]
        ):
            support.ask_once(None, {"support_asked": False})
        self.assertEqual(opened, [1])

    def test_the_setting_exists_and_defaults_to_not_shown(self):
        self.assertFalse(settings.DEFAULTS["support_asked"])




if __name__ == "__main__":
    unittest.main()
