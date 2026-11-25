# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that the key export manifest holds the hash of the bytes written, never one carried over.

import json
import unittest
from pathlib import Path

from djapp.export import manifest
from djapp.model import Node

from tests.test_key_support import export_to, make_track, tmp


# --------------------------------------------------------------
class KeyManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        music = self.root / "music"
        music.mkdir()
        self.tracks = [make_track(music, f"m{i}", i, title=f"Title {i}") for i in range(4)]
        self.out = self.root / "key"

    def test_two_quick_exports_of_equal_size_keep_the_true_hash(self):
        a = Node("playlist", "A", tracks=self.tracks[:2])
        b = Node("playlist", "B", tracks=self.tracks[2:])
        first = export_to([a], self.out, self.cache, verify_copy=True, key_mode="replace")
        self.assertEqual(first.errors, [])
        pdb = self.out / "PIONEER" / "rekordbox" / "export.pdb"
        size = pdb.stat().st_size
        second = export_to([b], self.out, self.cache, verify_copy=True, key_mode="replace")
        self.assertEqual(pdb.stat().st_size, size, "the two databases must have the same size for this test")
        self.assertEqual(second.errors, [])
        self.assertTrue(manifest.verify(str(self.out)).good)

    def test_every_generated_file_is_listed_with_its_own_hash(self):
        export_to([Node("playlist", "A", tracks=self.tracks)], self.out, self.cache, key_mode="replace")
        files = json.loads((self.out / manifest.NAME).read_text(encoding="utf-8"))["files"]
        for rel in ("PIONEER/rekordbox/export.pdb", "PIONEER/MYSETTING.DAT"):
            self.assertEqual(files[rel]["sha256"], manifest.sha256_file(str(self.out / rel)))
        self.assertFalse([p for p in files if p.endswith(".bak")])
        self.assertIsInstance(Path(self.out), Path)


if __name__ == "__main__":
    unittest.main()
