# the pictures of a key when an export is merged into it. A track that stays keeps its picture
# and its number, the new ones are numbered after the highest, the pictures nobody uses any more
# go once the new database is written. Until then the old database must still show the right
# picture on every track it lists

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from djapp.export.cdj import pdbread, pdbwrite, usb
from djapp.model import Node
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp

try:
    from PIL import Image

    from tests.witness import FLAC, Picture
except ImportError:  # optional libraries  # pragma: no cover
    Image = None

COLORS = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0)}


# ------------------------------------------------------------
# A FLAC whose embedded picture is one flat colour
def song(folder: Path, name: str, seed: int, color: str):
    base = make_track(folder, name, seed)
    base.artist = name
    audio = FLAC(base.path)
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), COLORS[color]).save(buf, "JPEG")
    pic = Picture()
    pic.data, pic.type, pic.mime = buf.getvalue(), 3, "image/jpeg"
    audio.add_picture(pic)
    audio.save()
    return base


# ------------------------------------------------------------
def colour(out: Path, number: int) -> str:
    f = out / "PIONEER" / "Artwork" / "00001" / f"a{number}.jpg"
    if not f.is_file():
        return "missing"
    px = Image.open(f).convert("RGB").getpixel((40, 40))
    return min(COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(COLORS[c], px, strict=True)))


# ----------------------------------------------------------------
# track name -> (number in the database, colour of that picture)
def pictures(out: Path) -> dict[str, tuple[int, str]]:
    pdb = pdbread.read((out / "PIONEER" / "rekordbox" / "export.pdb").read_bytes())
    found = {}
    for raw in pdbread.rows(pdb, pdbread.TRACKS):
        row = pdbread.track_row(raw)
        found[row.strings[20].split("/")[2]] = (row.art, colour(out, row.art))
    return found


def files(out: Path) -> list[str]:
    return sorted(p.name for p in (out / "PIONEER" / "Artwork" / "00001").glob("*.jpg"))


# --------------------------------------------------------------
@unittest.skipIf(Image is None, "Pillow, the tag witness library or soundfile missing")
class ArtworkMergeTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.root = tmp()
        self.addCleanup(self.dir.cleanup)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.out = self.root / "key"
        music = self.root / "music"
        music.mkdir()
        self.red, self.green, self.blue, self.yellow = (
            song(music, name, i, name) for i, name in enumerate(COLORS, 1))

    def export(self, name, *tracks, **extra):
        return export_to([Node("playlist", name, tracks=list(tracks))], self.out, self.cache, **extra)

    def test_every_track_shows_its_own_picture_after_a_merge(self):
        self.export("P", self.red, self.green)
        self.export("Q", self.blue)
        for name, (_number, shown) in pictures(self.out).items():
            self.assertEqual(shown, name)

    def test_a_track_that_stays_keeps_its_number_and_its_files(self):
        self.export("P", self.red, self.green)
        before = pictures(self.out)
        raw = {n: (self.out / "PIONEER" / "Artwork" / "00001" / n).read_bytes() for n in files(self.out)}
        self.export("Q", self.blue)
        after = pictures(self.out)
        self.assertEqual(after["red"], before["red"])
        self.assertEqual(after["green"], before["green"])
        for n, data in raw.items():
            self.assertEqual((self.out / "PIONEER" / "Artwork" / "00001" / n).read_bytes(), data)

    def test_new_pictures_come_after_the_highest_and_unused_ones_go(self):
        self.export("P", self.red, self.green)
        self.export("Q", self.blue)
        self.export("P", self.yellow)  # P loses red and green
        found = pictures(self.out)
        self.assertEqual(set(found), {"blue", "yellow"})
        self.assertEqual(found["blue"][0], 3)
        self.assertEqual(found["yellow"][0], 4)
        for name, (_number, shown) in found.items():
            self.assertEqual(shown, name)
        self.assertEqual(files(self.out), ["a3.jpg", "a3_m.jpg", "a4.jpg", "a4_m.jpg"])

    def test_the_old_database_still_shows_the_right_pictures_if_the_export_stops_before_it_is_written(self):
        self.export("P", self.red, self.green)
        self.export("Q", self.blue)
        before = pictures(self.out)
        with patch.object(pdbwrite, "write", side_effect=OSError("the key was pulled")), \
                self.assertRaises(OSError):
            self.export("P", self.yellow)
        self.assertEqual(pictures(self.out), before)

    def test_a_track_whose_picture_files_are_gone_gets_a_new_one(self):
        self.export("P", self.red, self.green)
        for n in ("a1.jpg", "a1_m.jpg"):
            (self.out / "PIONEER" / "Artwork" / "00001" / n).unlink()
        self.export("Q", self.blue)
        for name, (number, shown) in pictures(self.out).items():
            self.assertEqual(shown, name, f"{name} has number {number}")

    def test_the_reader_gives_the_numbers(self):
        self.export("P", self.red, self.green)
        data = key.read(str(self.out))
        self.assertEqual(sorted(data.art.values()), [1, 2])

    def test_a_replace_export_numbers_from_one_and_cleans_up(self):
        self.export("P", self.red, self.green)
        self.export("Q", self.blue)
        with patch.object(usb.keymerge, "keeping", return_value=False):
            self.export("R", self.yellow)
        self.assertEqual(pictures(self.out), {"yellow": (1, "yellow")})
        self.assertEqual(files(self.out), ["a1.jpg", "a1_m.jpg"])

    def test_the_manifest_lists_the_pictures_that_stay(self):
        self.export("P", self.red, self.green)
        self.export("Q", self.blue)
        report = self.export("R", self.blue, self.yellow, verify_copy=True)  # blue stays, yellow is new
        self.assertEqual(report.errors, [])
        self.assertGreater(report.verified, 0)


if __name__ == "__main__":
    unittest.main()
