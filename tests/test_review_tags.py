import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from djapp import sources, tagfile, tagwrite
from djapp.model import unique_tracks

from tests.test_engine import beat_data, make_library, quick_cues, track_data


def atom(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full(kind, payload, version=0):
    return atom(kind, bytes([version, 0, 0, 0]) + payload)


# what an independent reading of the tag and Engine code found
class ReviewTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def trak(self, offsets):
        stco = atom(b"stco", bytes(4) + struct.pack(">I", len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets))
        return atom(b"trak", atom(b"mdia", atom(b"minf", atom(b"stbl", stco))))

    def test_mp4_with_a_media_atom_on_each_side_of_moov_keeps_every_chunk(self):
        ftyp = atom(b"ftyp", b"M4A \0\0\0\0M4A ")
        mdat1 = atom(b"mdat", b"CHUNK00_CHUNK01_")
        mvhd = full(b"mvhd", bytes(8) + struct.pack(">II", 1000, 4000) + bytes(80))

        def moov(o2):
            return atom(b"moov", mvhd + self.trak([len(ftyp) + 8, len(ftyp) + 16]) + self.trak(o2))

        size = len(moov([0, 0]))
        start2 = len(ftyp) + len(mdat1) + size + 8
        path = self.root / "t.m4a"
        path.write_bytes(ftyp + mdat1 + moov([start2, start2 + 8]) + atom(b"mdat", b"CHUNK02_CHUNK03_"))
        tagwrite.write(str(path), {"title": "Hello" * 50})
        raw = path.read_bytes()
        offsets = [struct.unpack(">I", raw[i + 12 + 4 * k : i + 16 + 4 * k])[0] for i in [m for m in range(len(raw)) if raw[m : m + 4] == b"stco"] for k in range(2)]
        self.assertEqual([raw[o : o + 8] for o in offsets], [b"CHUNK00_", b"CHUNK01_", b"CHUNK02_", b"CHUNK03_"])

    def mp4_with(self, udta, mvhd=None):
        mvhd = mvhd or full(b"mvhd", bytes(8) + struct.pack(">II", 1000, 4000) + bytes(80))
        path = self.root / "u.m4a"
        path.write_bytes(atom(b"ftyp", b"M4A \0\0\0\0M4A ") + atom(b"moov", mvhd + udta) + atom(b"mdat", bytes(100)))
        return path

    def item(self, code, text):
        return atom(code, atom(b"data", struct.pack(">II", 1, 0) + text.encode()))

    def test_a_meta_atom_without_its_version_word_is_read_and_written(self):
        hdlr = atom(b"hdlr", bytes(8) + b"mdir" + bytes(13))
        path = self.mp4_with(atom(b"udta", atom(b"meta", hdlr + atom(b"ilst", self.item(b"\xa9nam", "Old")))))
        self.assertEqual(tagfile.open_tagged(str(path)).get("title"), "Old")
        self.assertEqual(tagfile.identify(str(path)).format, "MP4")
        tagwrite.write(str(path), {"title": "New", "artist": "Art"})
        tagged = tagfile.open_tagged(str(path))
        self.assertEqual((tagged.get("title"), tagged.get("artist")), ("New", "Art"))

    def test_the_second_udta_is_left_as_it_is(self):
        first = atom(b"udta", full(b"meta", atom(b"ilst", self.item(b"\xa9nam", "Old"))))
        second = atom(b"udta", full(b"meta", atom(b"ilst", self.item(b"\xa9ART", "Art2"))))
        path = self.mp4_with(first + second)
        tagwrite.write(str(path), {"title": "New"})
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"Art2"), 1)
        self.assertEqual(raw.count(b"ilst"), 2)

    def test_mvhd_of_version_1_that_is_cut_short_is_zero_not_an_exception(self):
        path = self.mp4_with(b"", full(b"mvhd", bytes(16) + struct.pack(">I", 1000), 1))
        self.assertEqual(tagfile.identify(str(path)).length, 0.0)

    def wav(self, riff_size):
        path = self.root / "a.wav"
        sf.write(str(path), (np.sin(np.arange(8000) * 0.1) * 0.3).astype(np.float32), 8000)
        raw = bytearray(path.read_bytes())
        raw[4:8] = struct.pack("<I", riff_size)
        path.write_bytes(bytes(raw))
        return path

    def test_a_wav_whose_riff_size_was_left_at_zero_keeps_its_chunks_in_order(self):
        for size in (0, 4):
            with self.subTest(size=size):
                path = self.wav(size)
                before, _ = sf.read(str(path))
                tagwrite.write(str(path), {"title": "X"})
                chunks = [c[0] for c in tagfile.open_tagged(str(path)).chunks]
                self.assertEqual(chunks[:2], [b"fmt ", b"data"])
                np.testing.assert_array_equal(sf.read(str(path))[0], before)

    def test_wav_length_counts_what_is_there_and_finds_fmt_after_data(self):
        path = self.wav(0xFFFFFFFF)
        raw = bytearray(path.read_bytes())
        i = raw.index(b"data")
        raw[i + 4 : i + 8] = struct.pack("<I", 0xFFFFFFFF)
        path.write_bytes(bytes(raw))
        self.assertAlmostEqual(tagfile.identify(str(path)).length, 1.0, delta=0.01)

    def test_a_vorbis_comment_that_is_not_utf8_or_has_no_equals_comes_back_as_it_was(self):
        path = self.root / "c.flac"
        sf.write(str(path), (np.sin(np.arange(8000) * 0.1) * 0.3).astype(np.float32), 8000)
        tagged = tagfile.open_tagged(str(path))
        tagged.comments.items.append(("artistsort", "Beyonc\udce9"))
        tagged.comments.items.append(("foo~bar", None))
        tagged.save()
        tagwrite.write(str(path), {"title": "T"})
        raw = path.read_bytes()
        self.assertIn(b"artistsort=Beyonc\xe9", raw)
        self.assertIn(b"foo~bar", raw)

    def test_a_backup_is_whole_or_not_there(self):
        path = self.root / "b.wav"
        sf.write(str(path), (np.sin(np.arange(8000) * 0.1) * 0.3).astype(np.float32), 8000)
        original = path.read_bytes()
        real_replace = tagwrite.os.replace

        def failing(src, dst):
            if str(dst).endswith(".bb-tags.bak"):
                raise OSError("disk")
            return real_replace(src, dst)

        with patch.object(tagwrite.os, "replace", failing), self.assertRaises(OSError):
            tagwrite.write(str(path), {"title": "X"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["b.wav"])
        result = tagwrite.write(str(path), {"title": "X"})
        self.assertEqual(Path(result.backup).read_bytes(), original)


# ------------------------------------------------------------
class ReviewEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_file_with_the_header_and_garbage_is_not_a_library_and_no_exception(self):
        fake = self.root / "Engine Library" / "Database2" / "m.db"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"SQLite format 3\0" + bytes(range(256)) * 16)
        self.assertEqual(sources.detect(str(fake.parent)), "folder")
        self.assertEqual(sources.detect(str(fake)), "")

    def test_a_track_table_without_a_path_is_a_named_error(self):
        db = self.root / "Engine Library" / "Database2" / "m.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE Information (id INTEGER, uuid TEXT); CREATE TABLE Playlist (id INTEGER, title TEXT);"
                          "CREATE TABLE Track (id INTEGER, title TEXT); INSERT INTO Track VALUES (1, 'x');")
        con.commit()
        con.close()
        with self.assertRaises(ValueError):
            sources.load(str(db))

    def test_lists_without_a_proper_id_do_not_break_the_order(self):
        db = make_library(self.root)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO PlaylistEntity (listId, trackId, databaseUuid, nextEntityId) VALUES (13, 1, NULL, NULL)")
        con.commit()
        con.close()
        sources.load(str(db))

    def test_a_streaming_source_of_zero_is_a_file_and_a_text_is_not(self):
        db = make_library(self.root)
        con = sqlite3.connect(db)
        con.execute("UPDATE Track SET streamingSource = 0 WHERE id = 1")
        con.commit()
        con.close()
        self.assertIn("One", {t.title for t in unique_tracks(sources.load(str(db)))})

    def test_playlists_that_are_each_others_parents_still_come_out(self):
        db = make_library(self.root)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO Playlist (id, title, parentListId, isPersisted, nextListId) VALUES (20, 'Loop A', 21, 1, 0)")
        con.execute("INSERT INTO Playlist (id, title, parentListId, isPersisted, nextListId) VALUES (21, 'Loop B', 20, 1, 0)")
        con.execute("INSERT INTO PlaylistEntity (listId, trackId, databaseUuid, nextEntityId) VALUES (20, 1, '11111111-2222-3333-4444-555555555555', 0)")
        con.commit()
        con.close()
        names = {n.name for n in sources.load(str(db))}
        self.assertIn("Loop A", names)

    def test_without_the_rate_of_the_track_there_is_no_cue_and_the_grid_stays(self):
        db = make_library(self.root)
        con = sqlite3.connect(db)
        con.execute("UPDATE Track SET trackData = NULL, beatData = ?, quickCues = ? WHERE id = 1",
                    (beat_data(rate=48000.0, bpm=120.0), quick_cues({0: ("a", 48000.0, (1, 2, 3))})))
        con.commit()
        con.close()
        one = next(t for t in unique_tracks(sources.load(str(db))) if t.title == "One")
        self.assertEqual([c for c in one.cues if c.hotcue >= 0], [])
        self.assertAlmostEqual(one.bpm, 120.0, places=2)
        self.assertEqual(track_data(48000.0)[:4], track_data(48000.0)[:4])


if __name__ == "__main__":
    unittest.main()
