# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that the exporters handle extreme track values such as bad ratings, long titles and odd device names.

# Extreme values a damaged collection can hold: rating -1, colour 99, a 100 000 character title, half a surrogate pair.
# Also device names in file names and two songs with one name. Found by pushing extreme tracks through every writer.

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from djapp import export, settings, sources
from djapp.export.files import safe_name
from djapp.export.fit import fit, text
from djapp.model import Cue, Node, Track, playlists
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp

HOSTILE = {
    "color 99": {"color": 99},
    "color -3": {"color": -3},
    "rating -1": {"rating": -1},
    "rating 99": {"rating": 99},
    "surrogate title": {"title": "a\ud800b"},
    "nul in title": {"title": "a\x00b"},
    "huge bitrate": {"bitrate": 10**12},
    "key 99": {"key": 99},
    "key -1": {"key": -1},
    "negative bpm": {"bpm": -5.0},
    "bpm 5000": {"bpm": 5000.0},
    "duration 1e12": {"duration": 1e12},
    "track number huge": {"track_no": 10**15},
    "title of 100000 characters": {"title": "x" * 100000},
    "comment of 70000 characters": {"comment": "lyrics " * 10000},
    "year huge": {"year": 10**9},
    "cue at infinity": {"cues": [Cue(name="a", start=float("inf"), hotcue=0)]},
    "grid at nan": {"grid": float("nan")},
}


# ----------------------------------------------------------
class FitTests(unittest.TestCase):
    def test_a_track_in_range_comes_back_as_it_is(self):
        t = Track(title="T", artist="A", path="/x.mp3", duration=100.0, bpm=120.0, rating=3, color=2)
        self.assertIs(fit(t), t)

    def test_a_copy_is_made_when_something_is_out_of_range_and_the_track_is_not_changed(self):
        t = Track(title="T", path="/x.mp3", rating=-1, color=99, comment="c" * 70000, uid_marker=None) \
            if False else Track(title="T", path="/x.mp3", rating=-1, color=99, comment="c" * 70000)
        fitted = fit(t)
        self.assertIsNot(fitted, t)
        self.assertEqual((fitted.rating, fitted.color, len(fitted.comment)), (0, 8, 1000))
        self.assertEqual((t.rating, t.color, len(t.comment)), (-1, 99, 70000))

    def test_the_uid_never_changes(self):
        t = Track(title="\ud800" * 5, audio_id="\ud800id", path="/x\ud800.mp3", color=99)
        self.assertEqual(fit(t).uid, t.uid)

    def test_text(self):
        self.assertEqual(text("a\ud800b"), "a�b")
        self.assertEqual(text("a\x00b"), "ab")
        self.assertEqual(len(text("x" * 5000, 100)), 100)
        self.assertEqual(text("fine é"), "fine é")

    def test_non_numbers_become_zero(self):
        t = Track(path="/x", bpm=float("nan"), duration=float("inf"), gain=float("inf"))
        fitted = fit(t)
        self.assertEqual((fitted.bpm, fitted.duration, fitted.gain), (0.0, 0.0, 0.0))

    def test_cues_that_cannot_be_placed_are_dropped(self):
        t = Track(path="/x", cues=[Cue(start=1.0), Cue(start=float("nan")), Cue(start=2.0, length=float("inf"))])
        self.assertEqual(len(fit(t).cues), 1)


# ----------------------------------------------------------
class DeviceNameTests(unittest.TestCase):
    def test_names_windows_keeps_for_devices_get_a_prefix(self):
        for name in ("CON", "con", "NUL.mp3", "Aux", "com1", "LPT9.flac", "prn.txt"):
            with self.subTest(name):
                self.assertTrue(safe_name(name).startswith("_"), safe_name(name))

    def test_other_names_are_left_alone(self):
        for name in ("Console", "Contact.mp3", "COM10", "Aux Cord", "nul-1.mp3", "LPT0"):
            with self.subTest(name):
                self.assertEqual(safe_name(name), name)


    def test_lone_surrogates_are_replaced(self):
        self.assertEqual(safe_name("a\ud800b.mp3"), "a_b.mp3")


# --------------------------------------------------------------
class EveryWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.music, self.cache = self.root / "music", self.root / "cache"
        self.music.mkdir()
        self.cache.mkdir()
        self.audio = make_track(self.music, "a", 1)

    def track(self, **fields):
        t = Track(title="T", artist="A", path=self.audio.path, duration=self.audio.duration, bpm=120.0,
                  samplerate=self.audio.samplerate)
        for k, v in fields.items():
            setattr(t, k, v)
        return t

    def cfg(self, **extra):
        return {**settings.DEFAULTS, "copy_music": False, "verify_copy": False, "anlz_processes": 1,
                "key_mode": "replace", **extra}

    def test_no_value_stops_a_file_export_and_the_file_reads_back(self):
        for label, fields in HOSTILE.items():
            for fmt, name in (("Rekordbox XML", "rekordbox.xml"), ("Traktor NML", "collection.nml")):
                with self.subTest(label=label, fmt=fmt):
                    out = self.root / f"{fmt[:3]}{abs(hash(label))}"
                    export.run(fmt, [Node("playlist", "P", tracks=[self.track(**fields)])], str(out), self.cfg())
                    ET.parse(out / name)  # well formed
                    self.assertEqual(len(list(playlists(sources.load(str(out / name))))), 1)

    def test_a_cue_colour_or_a_uuid_with_control_characters_leaves_the_nml_well_formed(self):
        t = self.track(cues=[Cue(name="c", start=1000.0, hotcue=0, color="#\x01FF\x1f00")])
        nodes = [Node("playlist", "P", tracks=[t], uuid="u\x02id"),
                 Node("smartlist", "S", uuid="\x00bad", query="q")]
        out = self.root / "nmlctrl"
        export.run("Traktor NML", nodes, str(out), self.cfg())
        root = ET.parse(out / "collection.nml").getroot()
        self.assertEqual(root.find(".//CUE_V2").get("COLOR"), "#FF00")
        self.assertEqual([n.get("UUID") for n in root.iter() if n.get("UUID") is not None], ["uid", "bad"])

    def test_no_value_stops_an_m3u_export(self):
        for label, fields in HOSTILE.items():
            with self.subTest(label=label):
                out = self.root / f"m3u{abs(hash(label))}"
                export.run("M3U", [Node("playlist", "P", tracks=[self.track(**fields)])], str(out), self.cfg())
                data = (out / "Playlists" / "P.m3u8").read_bytes().decode("utf-8")
                self.assertIn("#EXTM3U", data)

    def test_no_value_stops_a_cdj_export_and_the_key_reads_back(self):
        for label, fields in HOSTILE.items():
            with self.subTest(label=label):
                out = self.root / f"cdj{abs(hash(label))}"
                rep = export_to([Node("playlist", "P", tracks=[self.track(**fields)])], out, self.cache)
                self.assertEqual(rep.errors, [])
                self.assertEqual(len(list(playlists(key.read(str(out)).nodes))), 1)

    def test_a_playlist_with_a_very_long_name_is_written_to_the_key(self):
        out = self.root / "longname"
        export_to([Node("playlist", "N" * 100000, tracks=[self.track()])], out, self.cache)
        names = [p.name for p in playlists(key.read(str(out)).nodes)]
        self.assertEqual(len(names), 1)
        self.assertEqual(len(names[0]), 255)

    def test_an_artist_and_an_album_with_device_names_are_exported_with_their_audio(self):
        # a file called CON cannot even be made on Windows, the folders are what a tag can ask for
        t = self.track(artist="NUL", album="AUX")
        out = self.root / "devout"
        rep = export_to([Node("playlist", "P", tracks=[t])], out, self.cache)
        self.assertEqual(rep.errors, [])
        self.assertTrue((out / "Contents" / "_NUL" / "_AUX" / "a.flac").is_file())

    def test_a_cdj_export_does_not_change_the_tracks_it_was_given(self):
        t = self.track(rating=-1, color=99, comment="c" * 70000)
        export_to([Node("playlist", "P", tracks=[t])], self.root / "same", self.cache)
        self.assertEqual((t.rating, t.color, len(t.comment)), (-1, 99, 70000))


# --------------------------------------------------------
class FileExportNameTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        for name in ("a", "b"):
            (self.root / name).mkdir()

    def song(self, folder, seed):
        t = make_track(self.root / folder, "track01", seed, artist="Same", album="")
        return t

    def export(self, fmt, name, track, out):
        cfg = {**settings.DEFAULTS, "copy_music": True, "verify_copy": False}
        return export.run(fmt, [Node("playlist", name, tracks=[track])], str(out), cfg)

    def test_two_songs_with_one_name_both_stay_in_a_folder_export(self):
        one, two = self.song("a", 2), self.song("b", 12)  # FLAC files of the same size
        out = self.root / "out"
        self.export("Traktor NML", "First", one, out)
        self.export("Traktor NML", "Second", two, out)
        files = sorted(p.name for p in (out / "Contents").rglob("*.flac"))
        self.assertEqual(files, ["track01 (2).flac", "track01.flac"])
        self.assertEqual((out / "Contents" / "Same" / "Unknown Album" / "track01.flac").read_bytes(),
                         Path(one.path).read_bytes())

    def test_the_same_song_exported_again_is_not_copied_twice(self):
        one = self.song("a", 2)
        out = self.root / "out"
        self.export("M3U", "First", one, out)
        self.export("M3U", "Second", one, out)
        self.assertEqual(len(list((out / "Contents").rglob("*.flac"))), 1)


if __name__ == "__main__":
    unittest.main()
