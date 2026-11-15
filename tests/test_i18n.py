import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import i18n, lang, settings

from tests import APP_ROOT

TOOLS = APP_ROOT / "tools"


# ------------------------------------------------------------
class MechanicsTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_the_five_languages_and_english_as_the_source(self):
        self.assertEqual(list(i18n.LANGUAGES), ["en", "fr", "de", "es", "zh"])
        self.assertEqual(i18n.SOURCE, "en")

    def test_english_and_a_missing_translation_show_the_english_text(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("Something that is not in a catalog"), "Something that is not in a catalog")
        i18n.set_language("de")
        self.assertEqual(i18n.tr("Something that is not in a catalog"), "Something that is not in a catalog")

    def test_fields_are_filled_in_the_translated_text(self):
        i18n.set_language("fr")
        part = type("P", (), {"CATALOG": {"fr": {"{n} files in {where}": "{where} : {n} fichiers"}}})
        with patch.object(lang, "PARTS", (part,)):
            i18n.set_language("fr")
            self.assertEqual(i18n.tr("{n} files in {where}", n=3, where="X"), "X : 3 fichiers")

    def test_a_translation_with_a_wrong_field_falls_back_to_the_english_text(self):
        part = type("P", (), {"CATALOG": {"fr": {"{n} files": "{nombre} fichiers"}}})
        with patch.object(lang, "PARTS", (part,)):
            i18n.set_language("fr")
            self.assertEqual(i18n.tr("{n} files", n=2), "2 files")

    def test_one_or_many(self):
        i18n.set_language("en")
        self.assertEqual(i18n.trn(1, "{n} track", "{n} tracks"), "1 track")
        self.assertEqual(i18n.trn(0, "{n} track", "{n} tracks"), "0 tracks")
        self.assertEqual(i18n.trn(5, "{n} track", "{n} tracks"), "5 tracks")

    def test_an_unknown_language_is_english(self):
        i18n.set_language("xx")
        self.assertEqual(i18n.language(), "en")

    def test_the_language_of_the_computer_is_one_the_program_knows(self):
        self.assertIn(i18n.system_language(), i18n.LANGUAGES)
        with patch.object(i18n.locale, "getlocale", return_value=("fr_FR", "UTF-8")), patch.object(i18n.sys, "platform", "linux"):
            self.assertEqual(i18n.system_language(), "fr")
        with patch.object(i18n.locale, "getlocale", return_value=("ja_JP", "UTF-8")), patch.object(i18n.sys, "platform", "linux"):
            self.assertEqual(i18n.system_language(), "en")

    def test_the_setting_keeps_a_known_language_and_empties_the_others(self):
        self.assertEqual(settings.DEFAULTS["language"], "")
        for saved, want in (("fr", "fr"), ("zh", "zh"), ("xx", ""), ("", "")):
            with self.subTest(saved=saved):
                with patch("builtins.open", unittest.mock.mock_open(read_data='{"language": "%s"}' % saved)):
                    self.assertEqual(settings.load()["language"], want)


# ------------------------------------------------------------
class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(TOOLS))
        import i18n_check

        cls.check = i18n_check

    def test_no_text_of_the_interface_is_a_variable(self):
        report, _ = self.check.check()
        self.assertEqual(report["loose"], [])

    def test_every_text_has_a_translation_in_every_language_with_the_same_fields(self):
        report, where = self.check.check()
        for code in report["missing"]:
            with self.subTest(language=code):
                self.assertEqual(report["missing"][code][:5], [])
                self.assertEqual(report["fields"][code][:5], [])

    def test_no_translation_holds_a_long_dash(self):
        for part in lang.PARTS:
            for code, words in part.CATALOG.items():
                for key, text in words.items():
                    self.assertNotIn("\u2014", text)
                    self.assertNotIn("\u2013", text)
                    self.assertNotIn("\u2014", key)
                    self.assertNotIn("\u2013", key)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------
class PreferencesLanguageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def options(self, language):
        from PySide6.QtWidgets import QApplication

        from djapp.ui.dialogs import Options

        QApplication.instance() or QApplication([])
        cfg = dict(settings.DEFAULTS, language=language)
        dlg = Options(cfg)
        self.addCleanup(dlg.close)
        return dlg, cfg

    def test_ok_without_a_change_asks_nothing(self):
        from PySide6.QtWidgets import QMessageBox

        for saved in ("", "en"):
            with self.subTest(saved=saved):
                dlg, cfg = self.options(saved)
                said = []
                with patch.object(QMessageBox, "information", lambda *a: said.append(a)):
                    dlg.accept()
                self.assertEqual(said, [])
                self.assertEqual(cfg["language"], "en")

    def test_another_language_is_saved_and_says_it_waits_for_the_next_start(self):
        from PySide6.QtWidgets import QMessageBox

        dlg, cfg = self.options("en")
        dlg.language.setCurrentIndex(dlg.language.findData("de"))
        said = []
        with patch.object(QMessageBox, "information", lambda *a: said.append(a[2])):
            dlg.accept()
        self.assertEqual(cfg["language"], "de")
        self.assertEqual(len(said), 1)


# ------------------------------------------------------------
class LabelsFollowTheLanguageTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_the_beta_mention_and_the_saved_name_in_every_language(self):
        for code in i18n.LANGUAGES:
            with self.subTest(language=code):
                i18n.set_language(code)
                label = settings.export_label("Mixxx")
                self.assertTrue(label.startswith("Mixxx "))
                self.assertEqual(settings.export_name(label), "Mixxx")
                self.assertEqual(settings.export_label("M3U"), "M3U")
        i18n.set_language("en")
        self.assertEqual(settings.export_label("Mixxx"), "Mixxx (beta)")

    def test_the_id_tag_button_is_not_cut_in_any_language(self):
        from PySide6.QtWidgets import QApplication

        from djapp.model import Node, Track
        from djapp.ui.details import C_TAG, PlaylistWindow
        from djapp.ui.player import Engine
        from djapp.ui.waveform import Loader

        QApplication.instance() or QApplication([])
        engine, loader = Engine(), Loader()
        self.addCleanup(engine.close)
        self.addCleanup(loader.stop)
        for code in i18n.LANGUAGES:
            with self.subTest(language=code):
                i18n.set_language(code)
                window = PlaylistWindow(Node("playlist", "P", tracks=[Track(title="t")]), engine, loader, {})
                button = window.table.itemWidget(window.table.topLevelItem(0), C_TAG)
                self.assertGreaterEqual(window.table.columnWidth(C_TAG), button.sizeHint().width())
                window.close()

    def test_no_text_talks_about_a_lock_or_a_key_of_licence(self):
        for part in lang.PARTS:
            for words in part.CATALOG.values():
                for key, text in words.items():
                    low = (key + text).lower()
                    self.assertNotIn("(locked)", low)
                    self.assertNotIn("machine code", low)
                    self.assertNotIn("licence key", low)
