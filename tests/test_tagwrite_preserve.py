# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that tag writing only changes the values the user edited and leaves the other tags as they are.

# writing tags only changes what the user changed: a full date, a track total or a key written
# another way stay as they are, a file that already holds the values is not rewritten, and the temporary
# copy of a killed write is not taken for music

import tempfile
import unittest

import pytest
from pathlib import Path

import numpy as np
import soundfile as sf
from djapp import library, tagwrite
from djapp.sources import folder, index_folder, is_audio
witness = pytest.importorskip("tests.witness")
FLAC, ID3, TBPM, TDRC, TKEY, TPOS, TRCK = (
    witness.FLAC, witness.ID3, witness.TBPM, witness.TDRC, witness.TKEY, witness.TPOS, witness.TRCK,
)


# --------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.samples = np.sin(np.arange(4410) * 0.13).astype(np.float32) * 0.2

    def audio(self, extension):
        path = self.root / ("track" + extension)
        sf.write(str(path), self.samples, 44100)
        return path

    def mp3(self, **frames):
        path = self.audio(".mp3")
        tag = ID3()
        for frame in frames.values():
            tag.add(frame)
        tag.save(str(path), v2_version=3)
        return path

    def id3(self, path):
        return ID3(str(path))


# --------------------------------------------------------
class UnchangedFieldsTests(Base):
    def test_a_full_date_and_the_totals_of_the_numbers_survive_a_change_of_title(self):
        path = self.mp3(date=TDRC(encoding=3, text=["2024-05-17"]), track=TRCK(encoding=3, text=["5/12"]),
                        disc=TPOS(encoding=3, text=["1/2"]))
        # what the editor sends: every field, the unchanged ones as the project has them
        tagwrite.write(str(path), {"title": "New", "year": "2024", "track_no": "5", "disc_no": "1"})
        tag = self.id3(path)
        self.assertEqual(str(tag["TIT2"].text[0]), "New")
        self.assertEqual(str(tag["TDRC"].text[0]), "2024-05-17")
        self.assertEqual(str(tag["TRCK"].text[0]), "5/12")
        self.assertEqual(str(tag["TPOS"].text[0]), "1/2")

    def test_a_new_track_number_keeps_the_total(self):
        path = self.mp3(track=TRCK(encoding=3, text=["5/12"]))
        tagwrite.write(str(path), {"track_no": "6"})
        self.assertEqual(str(self.id3(path)["TRCK"].text[0]), "6/12")
        self.assertEqual(tagwrite.get_tags(tagwrite.open_audio(str(path)), ["track_no"]), {"track_no": "6/12"})

    def test_a_number_without_a_total_stays_without_one(self):
        path = self.mp3(track=TRCK(encoding=3, text=["5"]))
        tagwrite.write(str(path), {"track_no": "7"})
        self.assertEqual(str(self.id3(path)["TRCK"].text[0]), "7")

    def test_a_key_written_another_way_and_a_bpm_with_other_decimals_are_left_alone(self):
        path = self.mp3(key=TKEY(encoding=3, text=["8A"]), bpm=TBPM(encoding=3, text=["128"]))
        tagwrite.write(str(path), {"title": "T", "key": "Am", "bpm": "128.000000"})
        tag = self.id3(path)
        self.assertEqual(str(tag["TKEY"].text[0]), "8A")
        self.assertEqual(str(tag["TBPM"].text[0]), "128")

    def test_a_real_change_of_key_or_bpm_is_written(self):
        path = self.mp3(key=TKEY(encoding=3, text=["8A"]), bpm=TBPM(encoding=3, text=["128"]))
        tagwrite.write(str(path), {"key": "Dm", "bpm": "130.5"})
        tag = self.id3(path)
        self.assertEqual(str(tag["TKEY"].text[0]), "Dm")
        self.assertEqual(str(tag["TBPM"].text[0]), "130.5")

    def test_clearing_a_field_still_clears_it(self):
        path = self.mp3(date=TDRC(encoding=3, text=["2024-05-17"]), track=TRCK(encoding=3, text=["5/12"]))
        tagwrite.write(str(path), {"year": "", "track_no": ""})
        tag = self.id3(path)
        self.assertNotIn("TDRC", tag)
        self.assertNotIn("TRCK", tag)

    def test_flac_keeps_the_full_date_and_the_total(self):
        path = self.audio(".flac")
        flac = FLAC(str(path))
        flac["date"] = ["2023-11-02"]
        flac["tracknumber"] = ["3/9"]
        flac.save()
        tagwrite.write(str(path), {"title": "T", "year": "2023", "track_no": "3"})
        after = FLAC(str(path))
        self.assertEqual(after["date"], ["2023-11-02"])
        self.assertEqual(after["tracknumber"], ["3/9"])
        tagwrite.write(str(path), {"track_no": "4"})
        self.assertEqual(FLAC(str(path))["tracknumber"], ["4/9"])


# --------------------------------------------------------
class NothingToWriteTests(Base):
    def test_a_file_that_already_holds_the_values_is_not_touched_and_makes_no_backup(self):
        path = self.mp3(track=TRCK(encoding=3, text=["5/12"]), date=TDRC(encoding=3, text=["2024-05-17"]))
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        messages = []
        result = tagwrite.write(str(path), {"year": "2024", "track_no": "5"}, progress=lambda p, m: messages.append(m))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(result.backup, "")
        self.assertFalse(list(self.root.glob("*.bb-tags.bak")))
        self.assertFalse(list(self.root.glob(".bb-tags-*")))
        self.assertIn("already holds", messages[-1])

    def test_an_existing_backup_is_reported_even_when_nothing_is_written(self):
        path = self.mp3(track=TRCK(encoding=3, text=["5"]))
        first = tagwrite.write(str(path), {"title": "A"})
        again = tagwrite.write(str(path), {"title": "A"})
        self.assertEqual(again.backup, first.backup)


# --------------------------------------------------------
class TextTests(Base):
    def test_text_that_cannot_be_encoded_is_refused_with_a_message(self):
        path = self.audio(".flac")
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Invalid text in title"):
            tagwrite.write(str(path), {"title": "a\ud800b"})
        self.assertEqual(path.read_bytes(), original)


# --------------------------------------------------------
class TemporaryCopyTests(Base):
    def test_the_copy_of_a_killed_write_is_not_music(self):
        self.assertTrue(is_audio("song.mp3"))
        self.assertFalse(is_audio(".bb-tags-abc123.mp3"))
        self.assertFalse(is_audio("notes.txt"))

    def test_scans_skip_it(self):
        (self.root / "sub").mkdir()
        for name in ("a.flac", ".bb-tags-x1.flac", "sub/c.wav", "sub/.bb-tags-x2.wav"):
            sf.write(str(self.root / name), self.samples, 44100, format="FLAC" if name.endswith("flac") else "WAV")
        (self.root / "b.mp3").write_bytes(b"x")
        (self.root / ".bb-tags-x3.mp3").write_bytes(b"x")
        want = ["a.flac", "b.mp3", "c.wav"]
        found = library.scan(str(self.root))
        self.assertEqual(sorted(Path(t.path).name for t in found.tracks), want)
        self.assertEqual(sorted(Path(p).name for paths in index_folder(str(self.root)).values() for p in paths), want)
        self.assertEqual(folder.count_audio(str(self.root)), 3)
        self.assertEqual(sorted(Path(t.path).name for t in folder.tracks_from([str(self.root)])), want)
        self.assertEqual([Path(t.path).name for t in folder.tracks_from([str(self.root / ".bb-tags-x1.flac")])], [])


if __name__ == "__main__":
    unittest.main()
