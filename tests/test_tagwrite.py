import stat
import struct
import tempfile
import threading
import unittest

import pytest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from djapp import tags, tagwrite
from djapp.export.manifest import Cancelled
from djapp.sources.folder import make_track
witness = pytest.importorskip("tests.witness")
APIC, COMM, FLAC, GEOB, ID3, MP3, MP4, MP4Cover, MP4FreeForm, Picture, TXXX = (
    witness.APIC, witness.COMM, witness.FLAC, witness.GEOB, witness.ID3, witness.MP3, witness.MP4,
    witness.MP4Cover, witness.MP4FreeForm, witness.Picture, witness.TXXX,
)


def atom(name, data):
    return struct.pack(">I4s", len(data) + 8, name) + data

# ------------------------------------------------------------
class TagWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.values = {"title": "New title", "artist": "New artist", "album": "Album",
                       "album_artist": "Album artist", "genre": "House", "label": "Label",
                       "remixer": "Remix", "composer": "Composer", "comment": "Line one\nLine two",
                       "year": "2026", "track_no": "3", "disc_no": "2", "bpm": "123.456789", "key": "Am"}
        self.samples = np.sin(np.arange(4410) * 0.13).astype(np.float32) * 0.2

    def audio(self, extension):
        path = self.root / ("track" + extension)
        sf.write(str(path), self.samples, 44100)
        return path

    def test_wav_aiff_mp3_flac_ogg_preserve_audio_and_read_back_all_tags(self):
        for extension in (".wav", ".aiff", ".mp3", ".flac", ".ogg"):
            with self.subTest(extension=extension):
                path = self.audio(extension)
                original = path.read_bytes()
                before, rate = sf.read(str(path))
                result = tagwrite.write(str(path), self.values)
                self.assertEqual(Path(result.backup).read_bytes(), original)
                self.assertEqual(result.size, path.stat().st_size)
                self.assertEqual(tagwrite.get_tags(tagwrite.open_audio(str(path)), self.values), self.values)
                after, after_rate = sf.read(str(path))
                np.testing.assert_array_equal(before, after)
                self.assertEqual(rate, after_rate)
                raw = tags.basic(str(path))
                for field, value in self.values.items():
                    self.assertEqual(raw[field], value)
                track = make_track(str(path))
                self.assertEqual((track.label, track.remixer, track.composer, track.disc_no),
                                 ("Label", "Remix", "Composer", 2))

    def test_id3_artwork_dj_software_objects_and_named_comments_survive(self):
        path = self.audio(".mp3")
        audio = MP3(str(path))
        audio.add_tags()
        audio.tags.add(GEOB(encoding=3, mime="application/octet-stream", desc="Vendor Markers2", data=b"cue data"))
        audio.tags.add(GEOB(encoding=3, mime="application/octet-stream", desc="Vendor BeatGrid", data=b"grid data"))
        audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=b"picture data"))
        audio.tags.add(TXXX(encoding=3, desc="DJ_ANALYSIS", text=["keep"]))
        audio.tags.add(COMM(encoding=3, lang="eng", desc="DJ data", text=["keep comment"]))
        audio.save(v2_version=3)
        tagwrite.write(str(path), self.values)
        after = ID3(str(path))
        self.assertEqual(after.version[1], 3)
        self.assertEqual(after["GEOB:Vendor Markers2"].data, b"cue data")
        self.assertEqual(after["GEOB:Vendor BeatGrid"].data, b"grid data")
        self.assertEqual(after["APIC:cover"].data, b"picture data")
        self.assertEqual(after["TXXX:DJ_ANALYSIS"].text, ["keep"])
        self.assertEqual(after["COMM:DJ data:eng"].text, ["keep comment"])
        self.assertEqual(tags.basic(str(path))["comment"], self.values["comment"])
        tagwrite.write(str(path), {"comment": ""})
        self.assertNotIn("comment", tags.basic(str(path)))
        self.assertEqual(ID3(str(path))["COMM:DJ data:eng"].text, ["keep comment"])

    def test_flac_unknown_comments_and_artwork_survive(self):
        path = self.audio(".flac")
        audio = FLAC(path)
        audio["VENDOR_MARKERS_V2"] = ["opaque cues"]
        audio["VENDOR_BEATGRID"] = ["opaque grid"]
        audio["custom"] = ["first", "second"]
        audio["KEY"], audio["YEAR"] = ["Cm"], ["2001"]
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/png", b"picture data"
        audio.add_picture(picture)
        audio.save()
        tagwrite.write(str(path), self.values)
        after = FLAC(path)
        self.assertEqual(after["VENDOR_MARKERS_V2"], ["opaque cues"])
        self.assertEqual(after["VENDOR_BEATGRID"], ["opaque grid"])
        self.assertEqual(after["custom"], ["first", "second"])
        self.assertEqual(after.pictures[0].data, b"picture data")
        self.assertEqual(tags.basic(str(path))["key"], "Am")
        self.assertEqual(tags.basic(str(path))["year"], "2026")

    def test_mp4_fractional_bpm_artwork_custom_tags_and_track_totals(self):
        # Synthetic ISO-BMFF container with an opaque media payload; no copyrighted audio fixture.
        path = self.root / "test.m4a"
        mvhd = atom(b"mvhd", bytes(12) + struct.pack(">II", 1000, 100))
        payload = b"unchanged media payload"
        path.write_bytes(atom(b"ftyp", b"M4A \0\0\0\0M4A ") + atom(b"moov", mvhd) + atom(b"mdat", payload))
        audio = MP4(path)
        audio.add_tags()
        audio["trkn"], audio["disk"] = [(1, 12)], [(1, 4)]
        audio["covr"] = [MP4Cover(b"picture", imageformat=MP4Cover.FORMAT_PNG)]
        audio["----:com.apple.iTunes:DJ_DATA"] = [MP4FreeForm(b"cue and grid")]
        audio["----:com.apple.iTunes:key"] = [MP4FreeForm(b"Cm")]
        audio["----:com.apple.iTunes:bpm"] = [MP4FreeForm(b"90")]
        audio.save()
        tagwrite.write(str(path), self.values)
        after = MP4(path)
        self.assertEqual(after["trkn"], [(3, 12)])
        self.assertEqual(after["disk"], [(2, 4)])
        self.assertEqual(after["----:com.apple.iTunes:DJ_DATA"], [b"cue and grid"])
        self.assertEqual(after["covr"], [b"picture"])
        self.assertIn(payload, path.read_bytes())
        self.assertEqual(tagwrite.get_tags(tagwrite.open_audio(str(path)), self.values), self.values)
        self.assertEqual(tags.basic(str(path))["bpm"], "123.456789")
        self.assertEqual(tags.basic(str(path))["key"], "Am")
        tagwrite.write(str(path), {"key": "", "bpm": ""})
        raw = tags.basic(str(path))
        self.assertNotIn("key", raw)
        self.assertNotIn("bpm", raw)

    def test_readonly_source_is_rejected_without_leftover_files(self):
        path = self.audio(".wav")
        original = path.read_bytes()
        path.chmod(stat.S_IREAD)
        try:
            with self.assertRaisesRegex(PermissionError, "read-only"):
                tagwrite.write(str(path), self.values)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(self.root.glob(".bb-tags-*")))
            self.assertFalse(Path(str(path) + ".bb-tags.bak").exists())
        finally:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_clearing_selected_fields_keeps_other_fields_and_first_backup(self):
        path = self.audio(".flac")
        original = path.read_bytes()
        first = tagwrite.write(str(path), self.values)
        tagwrite.write(str(path), {"title": "", "bpm": "", "key": ""})
        raw = tagwrite.get_tags(tagwrite.open_audio(str(path)), self.values)
        self.assertEqual((raw["title"], raw["bpm"], raw["key"]), ("", "", ""))
        self.assertEqual(raw["artist"], "New artist")
        self.assertEqual(Path(first.backup).read_bytes(), original)

    def test_errors_cancellation_and_verification_leave_source_untouched(self):
        for mode in ("save", "replace", "verify", "cancel"):
            with self.subTest(mode=mode):
                path = self.audio(".flac")
                original = path.read_bytes()
                if mode == "save":
                    context = patch.object(tagwrite, "set_tags", side_effect=OSError("cannot save"))
                elif mode == "replace":
                    context = patch.object(tagwrite.os, "replace", side_effect=PermissionError("file in use"))
                elif mode == "verify":
                    context = patch.object(tagwrite, "get_tags", return_value={})
                else:
                    context = patch.object(tagwrite, "set_tags", wraps=tagwrite.set_tags)
                cancelled = threading.Event()
                if mode == "cancel":
                    cancelled.set()
                with context, self.assertRaises((OSError, ValueError, Cancelled)):
                    tagwrite.write(str(path), self.values, cancel=cancelled)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(list(self.root.glob(".bb-tags-*")))

    def test_source_changed_while_copying_is_not_overwritten(self):
        path = self.audio(".wav")
        original = path.read_bytes()
        replaced = original + b"external change"

        def progress(_pct, _message):
            path.write_bytes(replaced)

        with self.assertRaisesRegex(OSError, "source audio changed"):
            tagwrite.write(str(path), self.values, progress=progress)
        self.assertEqual(path.read_bytes(), replaced)
        self.assertFalse(list(self.root.glob(".bb-tags-*")))

    def test_invalid_fields_and_damaged_audio_are_rejected(self):
        path = self.audio(".wav")
        original = path.read_bytes()
        for values in ({"grid": "10"}, {"title": "bad\0text"}, {"bpm": "nan"},
                       {"track_no": "-1"}, {"key": "invalid"}):
            with self.assertRaises(ValueError):
                tagwrite.write(str(path), values)
            self.assertEqual(path.read_bytes(), original)
        damaged = self.root / "damaged.mp3"
        damaged.write_bytes(b"not audio")
        with self.assertRaises(ValueError):
            tagwrite.write(str(damaged), self.values)
        self.assertEqual(damaged.read_bytes(), b"not audio")
        self.assertFalse(list(self.root.glob(".bb-tags-*")))


if __name__ == "__main__":
    unittest.main()
