# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that files rekordbox writes next to export.pdb are named in the key summary and left alone.

# files rekordbox writes next to export.pdb (exportExt.pdb, exportLibrary.db): the summary of a
# key names them so the window can warn, and an export leaves them as they are

import unittest

from djapp import export
from djapp.model import Node
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp


# ------------------------------------------------------------
class ForeignFilesTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.root = tmp()
        self.addCleanup(self.dir.cleanup)
        self.music, self.cache, self.out = self.root / "music", self.root / "cache", self.root / "key"
        self.music.mkdir()
        self.cache.mkdir()
        export_to([Node("playlist", "A", tracks=[make_track(self.music, "a", 1)])], self.out, self.cache)
        self.rekordbox = self.out / "PIONEER" / "rekordbox"

    def test_a_key_made_by_this_program_has_none(self):
        self.assertEqual(export.key_summary(str(self.out)).others, [])

    def test_they_are_named_as_they_are_written_on_the_key(self):
        (self.rekordbox / "exportExt.pdb").write_bytes(b"x")
        (self.rekordbox / "EXPORTLIBRARY.DB").write_bytes(b"y")
        (self.rekordbox / "RBFLTR.DAT").write_bytes(b"z")
        self.assertEqual(export.key_summary(str(self.out)).others, ["EXPORTLIBRARY.DB", "exportExt.pdb"])

    def test_an_unreadable_database_still_lists_them(self):
        (self.rekordbox / "exportLibrary.db").write_bytes(b"y")
        self.out.joinpath(*key.DB).write_bytes(b"not a database")
        summary = export.key_summary(str(self.out))
        self.assertFalse(summary.readable)
        self.assertEqual(summary.others, ["exportLibrary.db"])

    def test_an_export_does_not_touch_them_in_either_mode(self):
        (self.rekordbox / "exportExt.pdb").write_bytes(b"ext")
        (self.rekordbox / "exportLibrary.db").write_bytes(b"lib")
        export_to([Node("playlist", "B", tracks=[make_track(self.music, "b", 2)])], self.out, self.cache)
        export_to([Node("playlist", "C", tracks=[make_track(self.music, "c", 3)])], self.out, self.cache, key_mode="replace")
        self.assertEqual((self.rekordbox / "exportExt.pdb").read_bytes(), b"ext")
        self.assertEqual((self.rekordbox / "exportLibrary.db").read_bytes(), b"lib")


if __name__ == "__main__":
    unittest.main()
