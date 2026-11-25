# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that two different songs with the same generated name both stay on the key without replacing each other.

# Two different songs with the same generated name (artist, album, file name) must both stay on the key.
# Two untagged track01.flac from different folders ask for the same name. Merging used to replace the first one's audio with the second's.

import hashlib
import os
import unittest
from pathlib import Path

from djapp.export.files import same_content
from djapp.model import Node, playlists
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------
# Playlist name -> (file name on the key, hash of the audio there) for its only track
def playing(out) -> dict[str, tuple[str, str]]:
    found = {}
    for pl in playlists(key.read(str(out), "").nodes):
        t = pl.tracks[0]
        found[pl.name] = (os.path.basename(t.path), digest(t.path))
    return found


# ------------------------------------------------------------
class CollisionTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.root = tmp()
        self.addCleanup(self.dir.cleanup)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.out = self.root / "key"
        self.a, self.b, self.c = self.root / "a", self.root / "b", self.root / "c"
        for d in (self.a, self.b, self.c):
            d.mkdir()

    def song(self, folder, seed, name="track01"):
        return make_track(folder, name, seed=seed, artist="Same", album="")

    def export(self, name, track):
        return export_to([Node("playlist", name, tracks=[track])], self.out, self.cache)

    def test_two_songs_with_the_same_name_both_stay(self):
        one, two = self.song(self.a, 3), self.song(self.b, 4)
        self.export("First", one)
        self.export("Second", two)
        found = playing(self.out)
        self.assertEqual(found["First"][1], digest(one.path))
        self.assertEqual(found["Second"][1], digest(two.path))
        self.assertNotEqual(found["First"][0], found["Second"][0])

    def test_songs_of_the_same_size_are_not_taken_for_one(self):
        one, two = self.song(self.a, 2), self.song(self.b, 12)  # FLAC files of the same size, other sound
        self.assertEqual(os.path.getsize(one.path), os.path.getsize(two.path))
        self.assertFalse(same_content(one.path, two.path))
        self.export("First", one)
        self.export("Second", two)
        found = playing(self.out)
        self.assertEqual(found["First"][1], digest(one.path))
        self.assertEqual(found["Second"][1], digest(two.path))

    def test_the_same_song_from_another_folder_is_not_copied_twice(self):
        one = self.song(self.a, 3)
        again = self.song(self.b, 3)  # same bytes, another file
        self.assertEqual(digest(one.path), digest(again.path))
        self.export("First", one)
        r = self.export("Second", again)
        self.assertEqual(r.errors, [])
        found = playing(self.out)
        self.assertEqual(found["First"], found["Second"])
        self.assertEqual(len(list((self.out / "Contents").rglob("*.flac"))), 1)

    def test_exporting_the_second_song_again_reuses_its_name(self):
        one, two = self.song(self.a, 3), self.song(self.b, 4)
        self.export("First", one)
        self.export("Second", two)
        self.export("Third", self.song(self.c, 4))  # the second song again
        files = sorted(p.name for p in (self.out / "Contents").rglob("*.flac"))
        self.assertEqual(files, ["track01 (2).flac", "track01.flac"])
        found = playing(self.out)
        self.assertEqual(found["Third"], found["Second"])

    def test_a_file_left_on_the_key_by_a_replaced_playlist_is_not_overwritten(self):
        one, two = self.song(self.a, 2), self.song(self.b, 12)  # same size
        self.export("Set", one)
        self.export("Set", two)  # replaces the playlist, one stays as a file
        found = playing(self.out)
        self.assertEqual(found["Set"][1], digest(two.path))
        self.assertTrue(any(digest(p) == digest(one.path) for p in (self.out / "Contents").rglob("*.flac")))


# ------------------------------------------------------------
class SameContentTests(unittest.TestCase):
    def test_comparisons(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        a = root / "a.bin"
        a.write_bytes(bytes(range(256)) * 2000)
        same = root / "same.bin"
        same.write_bytes(a.read_bytes())
        tail = root / "tail.bin"
        data = bytearray(a.read_bytes())
        data[-1] ^= 1
        tail.write_bytes(bytes(data))
        short = root / "short.bin"
        short.write_bytes(a.read_bytes()[:-1])
        self.assertTrue(same_content(str(a), str(same)))
        self.assertTrue(same_content(str(a), str(a)))
        self.assertFalse(same_content(str(a), str(tail)))
        self.assertFalse(same_content(str(a), str(short)))
        self.assertFalse(same_content(str(a), str(root / "missing.bin")))


if __name__ == "__main__":
    unittest.main()
