# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the tag reader on hand built audio files for each format, including atoms and frames.

import ast
import struct
import tempfile
import unittest

import pytest
from pathlib import Path

import djapp
import numpy as np
import soundfile as sf
from djapp import tagfile, tags, tagwrite
witness = pytest.importorskip("tests.witness")
APIC, FLAC, GEOB, ID3, MP3, OggVorbis, Picture, TXXX, WAVE = (
    witness.APIC, witness.FLAC, witness.GEOB, witness.ID3, witness.MP3, witness.OggVorbis, witness.Picture,
    witness.TXXX, witness.WAVE,
)

ROOT = Path(djapp.__file__).resolve().parent.parent
VALUES = {"title": "Titre éàü", "artist": "Artist", "album": "Album", "album_artist": "Album artist",
          "genre": "House", "label": "Label", "remixer": "Remix", "composer": "Composer",
          "comment": "Line one\nLine two", "year": "2026", "track_no": "3", "disc_no": "2",
          "bpm": "123.5", "key": "Am"}


def atom(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


# the writer and the readers are our own: an independent tag library, only used here, reads back what they wrote
class TagFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.samples = np.sin(np.arange(44100) * 0.13).astype(np.float32) * 0.2

    def audio(self, extension):
        path = self.root / ("track" + extension)
        sf.write(str(path), self.samples, 44100)
        return path

    # -------------------------------------
    def test_id3_version_3_and_4_both_read_back_by_the_witness(self):
        for version in (3, 4):
            with self.subTest(version=version):
                path = self.audio(".mp3")
                audio = MP3(str(path))
                audio.add_tags()
                audio.tags.add(GEOB(encoding=3, mime="application/octet-stream", desc="Opaque", data=b"\x00\xff\x00\xff data"))
                audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="c", data=b"picture"))
                audio.tags.add(TXXX(encoding=3, desc="keep", text=["me"]))
                audio.save(v2_version=version)
                tagwrite.write(str(path), VALUES)
                after = ID3(str(path), translate=False)
                self.assertEqual(after.version[1], version)
                self.assertEqual(str(after["TIT2"]), VALUES["title"])
                self.assertEqual(str(after["TPE1"]), "Artist")
                self.assertEqual(str(after["TDRC"] if version == 4 else after["TYER"]), "2026")
                self.assertEqual(str(after["TRCK"]), "3")
                self.assertEqual(after["GEOB:Opaque"].data, b"\x00\xff\x00\xff data")
                self.assertEqual(after["APIC:c"].data, b"picture")
                self.assertEqual(after["TXXX:keep"].text, ["me"])
                self.assertEqual(after["COMM::eng"].text, ["Line one\nLine two"])
                data, _rate = sf.read(str(path))
                self.assertEqual(len(data), len(self.samples))

    def test_a_tag_made_by_the_witness_is_read_by_us(self):
        path = self.audio(".mp3")
        audio = MP3(str(path))
        audio.add_tags()
        TALB, TBPM, TIT2, TKEY, TPE1, TPOS, TRCK = (
            witness.TALB, witness.TBPM, witness.TIT2, witness.TKEY, witness.TPE1, witness.TPOS, witness.TRCK,
        )
        for frame in (TIT2(encoding=1, text=["Ünïcode ✓"]), TPE1(encoding=3, text=["A"]), TALB(encoding=0, text=["B"]),
                      TRCK(encoding=0, text=["5/12"]), TPOS(encoding=0, text=["1/2"]), TBPM(encoding=0, text=["128"]),
                      TKEY(encoding=0, text=["Cm"])):
            audio.tags.add(frame)
        audio.save(v2_version=3)
        tagged = tagfile.open_tagged(str(path))
        self.assertEqual((tagged.get("title"), tagged.get("artist"), tagged.get("album")), ("Ünïcode ✓", "A", "B"))
        self.assertEqual((tagged.get("track_no"), tagged.get("disc_no"), tagged.get("bpm"), tagged.get("key")), ("5/12", "1/2", "128", "Cm"))

    def test_a_track_number_keeps_its_total_and_an_unsynchronised_tag_is_read(self):
        path = self.audio(".mp3")
        tagwrite.write(str(path), {"track_no": "5"})
        tagged = tagfile.open_tagged(str(path))
        tagged.set("track_no", "5/12")
        tagged.save()
        tagwrite.write(str(path), {"track_no": "6"})
        self.assertEqual(str(ID3(str(path))["TRCK"]), "6/12")
        # a v2.3 tag whose body was unsynchronised
        tag = tagfile.Id3Tag(3)
        tag.set("title", "x\xff")
        blob = bytearray(tag.dump(0))
        body = bytes(blob[10:]).replace(b"\xff", b"\xff\x00")
        blob = b"ID3" + bytes([3, 0, 0x80]) + tagfile.to_syncsafe(len(body)) + body
        parsed = tagfile.Id3Tag.parse(blob)
        self.assertEqual(parsed.get("title"), "x\xff")

    def test_id3_v2_2_is_refused_and_a_damaged_tag_is_an_error(self):
        path = self.audio(".mp3")
        raw = path.read_bytes()
        path.write_bytes(b"ID3\x02\x00\x00" + tagfile.to_syncsafe(10) + bytes(10) + raw)
        with self.assertRaisesRegex(ValueError, "ID3v2.2"):
            tagfile.open_tagged(str(path))
        path.write_bytes(b"ID3\x04\x00\x00" + tagfile.to_syncsafe(1000) + raw[:50])
        with self.assertRaises(ValueError):
            tagfile.open_tagged(str(path))

    def test_an_extended_header_is_read_and_dropped(self):
        frame = b"TIT2" + tagfile.to_syncsafe(8) + b"\0\0" + b"\x03Title"[:7].ljust(8, b"x")
        ext = tagfile.to_syncsafe(6) + b"\x01\x00"
        body = ext + frame
        path = self.audio(".mp3")
        raw = path.read_bytes()
        path.write_bytes(b"ID3\x04\x00\x40" + tagfile.to_syncsafe(len(body)) + body + raw)
        tagged = tagfile.open_tagged(str(path))
        self.assertTrue(tagged.get("title").startswith("Title"))
        tagwrite.write(str(path), {"artist": "A"})
        self.assertEqual(ID3(str(path))["TPE1"].text, ["A"])

    # -------------------------------------
    def test_wav_and_aiff_chunks(self):
        for extension, reader in ((".wav", WAVE), (".aiff", None)):
            with self.subTest(extension=extension):
                path = self.audio(extension)
                before = path.read_bytes()
                tagwrite.write(str(path), VALUES)
                tagwrite.write(str(path), {"title": "Second"})
                tagged = tagfile.open_tagged(str(path))
                self.assertEqual(tagged.get("title"), "Second")
                self.assertEqual(tagged.get("artist"), "Artist")
                after = path.read_bytes()
                order = "little" if extension == ".wav" else "big"
                self.assertEqual(int.from_bytes(after[4:8], order) + 8, len(after))
                self.assertEqual(sum(1 for cid, _o, _n in tagged.chunks if cid.lower() == b"id3 "), 1)
                if reader:
                    self.assertEqual(reader(str(path)).tags["TIT2"].text, ["Second"])
                data, _ = sf.read(str(path))
                self.assertEqual(len(data), len(self.samples))
                self.assertTrue(len(after) > len(before))

    def test_a_wav_with_an_odd_chunk_and_trailing_bytes_keeps_both(self):
        path = self.audio(".wav")
        raw = bytearray(path.read_bytes())
        odd = b"junk" + struct.pack("<I", 3) + b"abc" + b"\0"
        raw[36:36] = odd
        raw[4:8] = struct.pack("<I", len(raw) - 8)
        path.write_bytes(bytes(raw) + b"TRAILING")
        tagwrite.write(str(path), {"title": "T"})
        after = path.read_bytes()
        self.assertIn(odd, after)
        self.assertTrue(after.endswith(b"TRAILING"))
        self.assertEqual(tagfile.open_tagged(str(path)).get("title"), "T")
        data, _ = sf.read(str(path))
        self.assertEqual(len(data), len(self.samples))

    # -------------------------------------
    def test_flac_blocks_grow_and_shrink_and_keep_the_picture(self):
        path = self.audio(".flac")
        audio = FLAC(str(path))
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/png", b"p" * 5000
        audio.add_picture(picture)
        audio["VENDOR_X"] = ["opaque"]
        audio.save()
        tagwrite.write(str(path), {"comment": "long " * 2000, **VALUES})
        after = FLAC(str(path))
        self.assertEqual(after["title"], [VALUES["title"]])
        self.assertEqual(after["VENDOR_X"], ["opaque"])
        self.assertEqual(after.pictures[0].data, b"p" * 5000)
        tagwrite.write(str(path), {"comment": ""})
        self.assertNotIn("comment", FLAC(str(path)))
        data, _ = sf.read(str(path))
        self.assertEqual(len(data), len(self.samples))

    # -------------------------------------
    def test_ogg_the_headers_take_more_pages_and_the_audio_pages_are_renumbered(self):
        path = self.audio(".ogg")
        before, _ = sf.read(str(path))
        pages_before = tagfile.open_tagged(str(path)).pages
        tagwrite.write(str(path), {**VALUES, "comment": "word " * 30000})
        tagged = tagfile.open_tagged(str(path))
        self.assertGreater(tagged.pages, pages_before)
        after, _ = sf.read(str(path))
        np.testing.assert_array_equal(before, after)
        witness = OggVorbis(str(path))
        self.assertEqual(witness["title"], [VALUES["title"]])
        self.assertEqual(len(witness["comment"][0]), 150000)
        tagwrite.write(str(path), {"comment": "short"})
        self.assertEqual(OggVorbis(str(path))["comment"], ["short"])
        np.testing.assert_array_equal(sf.read(str(path))[0], before)

    def test_ogg_with_the_same_page_count_copies_the_audio_untouched(self):
        path = self.audio(".ogg")
        tagwrite.write(str(path), {"title": "A"})
        audio_before = path.read_bytes()[tagfile.open_tagged(str(path)).audio:]
        tagwrite.write(str(path), {"title": "B"})
        self.assertEqual(path.read_bytes()[tagfile.open_tagged(str(path)).audio:], audio_before)

    def test_ogg_a_chained_stream_keeps_its_own_page_numbers(self):
        path = self.audio(".ogg")
        second = path.read_bytes()
        path.write_bytes(second + second.replace(second[14:18], b"\x01\x02\x03\x04"))

        def numbers(raw):
            out, pos = [], 0
            while pos < len(raw):
                count = raw[pos + 26]
                out.append((raw[pos + 14:pos + 18], struct.unpack("<I", raw[pos + 18:pos + 22])[0]))
                pos += 27 + count + sum(raw[pos + 27:pos + 27 + count])
            return out
        tagwrite.write(str(path), {"comment": "word " * 30000})
        pages = numbers(path.read_bytes())
        for serial in {s for s, _n in pages}:
            self.assertEqual([n for s, n in pages if s == serial], list(range(sum(s == serial for s, _n in pages))))

    def test_ogg_an_audio_packet_on_the_last_header_page_is_refused_not_dropped(self):
        path = self.audio(".ogg")
        headers = tagfile.open_tagged(str(path)).audio
        raw = path.read_bytes()
        last = raw.rfind(b"OggS", 0, headers)
        count = raw[last + 26]
        table = raw[last + 27:last + 27 + count]
        # a 1 byte packet added after the setup header, on the same page
        page = bytearray(raw[last:last + 26] + bytes([count + 1]) + table + b"\x01" + raw[last + 27 + count:headers] + b"\x00")
        page[22:26] = bytes(4)
        page[22:26] = struct.pack("<I", tagfile.ogg_crc(bytes(page)))
        path.write_bytes(raw[:last] + page + raw[headers:])
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            tagwrite.write(str(path), {"title": "x"})
        self.assertEqual(path.read_bytes(), before)

    def test_a_comment_in_another_language_is_the_comment_and_clearing_it_removes_it(self):
        path = self.audio(".mp3")
        audio = MP3(str(path))
        audio.add_tags()
        audio.tags.add(witness.COMM(encoding=3, lang="XXX", desc="", text=["old"]))
        audio.tags.add(witness.COMM(encoding=3, lang="eng", desc="iTunNORM", text=["0000"]))
        audio.save(v2_version=3)
        self.assertEqual(tagfile.open_tagged(str(path)).get("comment"), tags.basic(str(path))["comment"])
        tagwrite.write(str(path), {"comment": ""})
        self.assertNotIn("comment", tags.basic(str(path)))
        self.assertEqual([f.HashKey for f in ID3(str(path)).getall("COMM")], ["COMM:iTunNORM:eng"])

    def test_flac_with_two_artists_shows_the_first_and_another_edit_keeps_both(self):
        path = self.audio(".flac")
        audio = FLAC(str(path))
        audio["ARTIST"] = ["First", "Second"]
        audio.save()
        shown = tags.basic(str(path))["artist"]
        self.assertEqual(shown, "First")
        tagwrite.write(str(path), {"artist": shown, "genre": "House"})
        self.assertEqual(FLAC(str(path))["artist"], ["First", "Second"])

    # -------------------------------------
    def mp4(self, moov_first=True):
        payload = b"media payload " * 20
        ftyp = atom(b"ftyp", b"M4A \0\0\0\0M4A ")
        mvhd = atom(b"mvhd", bytes(12) + struct.pack(">II", 1000, 4000) + bytes(80))

        def moov_with(offset):
            stco = atom(b"stco", bytes(4) + struct.pack(">I", 2) + struct.pack(">II", offset, offset + 40))
            co64 = atom(b"co64", bytes(4) + struct.pack(">I", 1) + struct.pack(">Q", offset + 100))
            stbl = atom(b"stbl", stco + co64)
            trak = atom(b"trak", atom(b"mdia", atom(b"minf", stbl)))
            return atom(b"moov", mvhd + trak)
        if moov_first:
            size = len(moov_with(0))
            offset = len(ftyp) + size + 8
            data = ftyp + moov_with(offset) + atom(b"mdat", payload)
        else:
            offset = len(ftyp) + 8
            data = ftyp + atom(b"mdat", payload) + moov_with(offset)
        path = self.root / "t.m4a"
        path.write_bytes(data)
        return path, payload, offset

    def offsets(self, path):
        raw = path.read_bytes()
        i = raw.index(b"stco")
        a, b = struct.unpack(">II", raw[i + 12:i + 20])
        j = raw.index(b"co64")
        c = struct.unpack(">Q", raw[j + 12:j + 20])[0]
        return a, b, c

    def test_mp4_chunk_offsets_follow_a_moov_that_grows(self):
        path, payload, offset = self.mp4(moov_first=True)
        self.assertEqual(self.offsets(path), (offset, offset + 40, offset + 100))
        tagwrite.write(str(path), VALUES)
        raw = path.read_bytes()
        a, b, c = self.offsets(path)
        self.assertEqual(raw[a:a + 14], payload[:14])
        self.assertEqual((b - a, c - a), (40, 100))
        raw_tags = tags.basic(str(path))
        self.assertEqual((raw_tags["title"], raw_tags["artist"], raw_tags["bpm"], raw_tags["track_no"]), (VALUES["title"], "Artist", "123.5", "3"))
        tagwrite.write(str(path), {"title": "", "comment": "x" * 3000})
        raw = path.read_bytes()
        a2, _b2, _c2 = self.offsets(path)
        self.assertEqual(raw[a2:a2 + 14], payload[:14])
        self.assertNotIn("title", tags.basic(str(path)))

    def test_mp4_with_the_moov_behind_the_media_needs_no_offset_change(self):
        path, payload, offset = self.mp4(moov_first=False)
        tagwrite.write(str(path), VALUES)
        self.assertEqual(self.offsets(path), (offset, offset + 40, offset + 100))
        self.assertEqual(tags.basic(str(path))["artist"], "Artist")
        self.assertIn(payload, path.read_bytes())

    def test_mp4_a_fragmented_file_and_a_plain_file_are_refused(self):
        path = self.root / "frag.m4a"
        path.write_bytes(atom(b"ftyp", b"M4A \0\0\0\0M4A ") + atom(b"moov", b"") + atom(b"moof", b""))
        with self.assertRaisesRegex(ValueError, "Fragmented"):
            tagfile.open_tagged(str(path))
        path.write_bytes(b"not an mp4 at all, really")
        with self.assertRaises(ValueError):
            tagfile.open_tagged(str(path))

    # -------------------------------------
    def test_the_length_read_from_the_header(self):
        for extension, kind in ((".wav", "WAV"), (".aiff", "AIFF"), (".flac", "FLAC"), (".ogg", "Ogg Vorbis"), (".mp3", "MP3")):
            with self.subTest(extension=extension):
                path = self.audio(extension)
                info = tagfile.identify(str(path))
                self.assertEqual(info.format, kind)
                self.assertAlmostEqual(info.length, 1.0, delta=0.12)
        path, _payload, _offset = self.mp4()
        info = tagfile.identify(str(path))
        self.assertEqual((info.format, info.length), ("MP4", 4.0))

    def test_garbage_is_not_a_container(self):
        path = self.root / "x.mp3"
        path.write_bytes(b"not audio" * 100)
        self.assertIsNone(tagfile.identify(str(path)))
        with self.assertRaises(ValueError):
            tagfile.open_tagged(str(path))

    def test_a_failed_write_leaves_the_file_alone(self):
        path = self.audio(".flac")
        before = path.read_bytes()
        tagged = tagfile.open_tagged(str(path))
        tagged.set("comment", "x" * (1 << 24))
        with self.assertRaises(ValueError):
            tagged.save()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["track.flac"])


# the tag code is ours and no tag library comes with the program
class NoTagLibraryTests(unittest.TestCase):
    def test_no_module_of_the_program_imports_the_witness_library(self):
        for path in sorted((ROOT / "djapp").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for name in names:
                    with self.subTest(path=path.name):
                        self.assertFalse(name.split(".")[0] == witness.NAME, f"{path.name} imports {name}")

    def test_the_requirements_and_the_spec_do_not_name_it(self):
        for name in ("requirements.txt", "boothbridge.spec", "THIRD_PARTY.md"):
            text = (ROOT / name).read_text(encoding="utf-8").lower()
            with self.subTest(file=name):
                self.assertNotIn(witness.NAME, text.replace("no tag library", ""))


if __name__ == "__main__":
    unittest.main()
