# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the built-in help: its contents list, anchors, search box, and that it matches the program.

import os
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.ui import theme
from djapp.ui.dialogs import USAGE, Usage, slug, with_contents
from PySide6.QtWidgets import QApplication


# --------------------------------------------
class ContentsTests(unittest.TestCase):
    def test_every_section_is_in_the_contents_and_has_its_anchor(self):
        page = with_contents(USAGE)
        titles = re.findall(r"<h3>(.*?)</h3>", USAGE)
        self.assertGreater(len(titles), 15)
        for title in titles:
            self.assertIn(f'<a href="#{slug(title)}">{title}</a>', page)
            self.assertIn(f'<h3><a name="{slug(title)}"></a>{title}</h3>', page)

    def test_the_contents_comes_before_the_first_section_and_after_the_introduction(self):
        page = with_contents(USAGE)
        self.assertLess(page.index("not replace it"), page.index("<b>In this page</b>"))
        self.assertLess(page.index("<b>In this page</b>"), page.index('<a name="getting-started">'))


    def test_the_anchors_are_unique(self):
        slugs = [slug(t) for t in re.findall(r"<h3>(.*?)</h3>", USAGE)]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_the_original_text_is_left_as_it_was(self):
        self.assertIn("<h3>Keys that already have playlists</h3>", USAGE)
        self.assertNotIn("<b>In this page</b>", USAGE)


# --------------------------------------------------
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = Usage()
        self.dialog.show()
        self.addCleanup(self.dialog.deleteLater)
        self.app.processEvents()

    def test_a_link_of_the_contents_scrolls_to_its_section(self):
        bar = self.dialog.text.verticalScrollBar()
        self.assertEqual(bar.value(), 0)
        self.dialog.text.scrollToAnchor(slug("When a CDJ refuses the key"))
        self.assertGreater(bar.value(), 0)

    def test_the_links_are_internal_and_use_the_accent_colour(self):
        self.assertIn(theme.ACCENT, self.dialog.text.document().defaultStyleSheet())
        self.assertIn('href="#getting-started"', self.dialog.text.toHtml())

    def test_the_search_finds_and_goes_on_to_the_next_match_and_round_the_page(self):
        self.dialog.find.setText("export.pdb")
        first = self.dialog.text.textCursor().selectionStart()
        self.assertEqual(self.dialog.text.textCursor().selectedText(), "export.pdb")
        self.dialog.find.returnPressed.emit()
        second = self.dialog.text.textCursor().selectionStart()
        self.assertGreater(second, first)
        for _ in range(60):
            self.dialog.find.returnPressed.emit()
        self.assertEqual(self.dialog.text.textCursor().selectedText(), "export.pdb")

    def test_typing_again_starts_from_the_top(self):
        self.dialog.find.setText("export.pdb")
        self.dialog.find.returnPressed.emit()
        self.dialog.find.setText("export.pd")
        self.assertEqual(self.dialog.text.textCursor().selectedText(), "export.pd")
        self.assertEqual(self.dialog.text.textCursor().selectionStart(),
                         self.dialog.text.document().find("export.pd").selectionStart())

    def test_clearing_the_box_clears_the_selection(self):
        self.dialog.find.setText("export.pdb")
        self.dialog.find.clear()
        self.assertEqual(self.dialog.text.textCursor().selectedText(), "")

    def test_a_word_that_is_not_there_changes_nothing(self):
        self.dialog.find.setText("zzzzqqq")
        self.assertEqual(self.dialog.text.textCursor().selectedText(), "")


# ----------------------------------------------------
class WordsTests(unittest.TestCase):
    def test_a_track_only_on_the_key_keeps_its_analysis_and_the_old_rewrite_is_gone(self):
        self.assertIn("keeps its analysis while its audio is unchanged", USAGE)
        self.assertNotIn("but their cues and analysis are rewritten", USAGE)

    def test_two_songs_with_one_name_and_the_missing_playlists_are_explained(self):
        self.assertIn("named name (2)", USAGE)
        self.assertIn("Playlists of an earlier export are missing", USAGE)
        self.assertIn("export.pdb.bak", USAGE)

    def test_no_long_dashes_and_no_speed_figures(self):
        self.assertNotIn(chr(0x2014), USAGE)
        self.assertNotIn(chr(0x2013), USAGE)
        self.assertNotIn("faster than", USAGE.lower())


if __name__ == "__main__":
    unittest.main()
