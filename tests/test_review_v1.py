# defects found by the skeptical review before 1.0.0: a hand edited settings file, options of the command line,
# paths that are not drive letters, a relative Mixxx path, an iTunes location that is not a URL

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from djapp import app, beatdetect, settings, sources, tags
from djapp.export.fit import fit
from djapp.export.nml import split_path
from djapp.model import LOOP, Cue, Track
from djapp.sources import itunes


class SettingsTypes(unittest.TestCase):
    def load(self, saved) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(saved), encoding="utf-8")
            with mock.patch.object(settings, "CONFIG", path):
                return settings.load()

    def test_wrong_types_fall_back_to_the_defaults(self):
        cfg = self.load(
            {
                "log_level": None,
                "music_root": 5,
                "anlz_processes": "x",
                "volume": "loud",
                "copy_music": "yes",
                "crossfade": True,
            }
        )
        for key in ("log_level", "music_root", "anlz_processes", "volume", "copy_music", "crossfade"):
            self.assertEqual(cfg[key], settings.DEFAULTS[key], key)

    def test_log_level_is_a_level(self):
        self.assertEqual(self.load({"log_level": "debug"})["log_level"], "DEBUG")
        self.assertEqual(self.load({"log_level": "loud"})["log_level"], "INFO")


class CommandLine(unittest.TestCase):
    def test_missing_output_is_refused(self):
        with mock.patch.object(app, "log_line"):
            self.assertEqual(
                app.headless(["x", "--export", "a.nml", "--format", "M3U"]), 2
            )
            self.assertEqual(
                app.headless(["x", "--export", "a.nml", "out", "--format"]), 2
            )

    def test_unencodable_message_does_not_raise(self):
        class Ascii:
            def write(self, s):
                s.encode("ascii")

            def flush(self):
                pass

        with mock.patch.object(app.sys, "stdout", Ascii()):
            app.log_line("日本")


class Paths(unittest.TestCase):
    def test_unc_path_in_nml(self):
        vol, folder, name = split_path("\\\\nas\\music\\House\\a.mp3")
        self.assertEqual(name, "a.mp3")
        self.assertEqual(folder, "/:House/:")
        self.assertIn("nas", vol)

    def test_itunes_location_that_is_not_a_url(self):
        self.assertEqual(itunes.location_path("file://[::1/x.mp3"), "")

    def test_mixxx_relative_path_and_odd_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Music #1 %20x"
            folder.mkdir()
            db = folder / "mixxxdb.sqlite"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE track_locations (id INTEGER)")
            con.commit()
            con.close()
            self.assertEqual(sources.detect(str(db)), "mixxx")
            old = os.getcwd()
            os.chdir(folder)
            try:
                self.assertEqual(sources.detect("mixxxdb.sqlite"), "mixxx")
            finally:
                os.chdir(old)


class Cues(unittest.TestCase):
    def test_loop_ending_before_zero_is_dropped_and_names_are_clean(self):
        track = Track(
            cues=[
                Cue("bad", LOOP, -100.0, 50.0, 0),
                Cue("lone\ud800", 0, 10.0, 0.0, 1),
            ]
        )
        cues = fit(track).cues
        self.assertEqual([c.start for c in cues], [10.0])
        cues[0].name.encode("utf-8")


class Beats(unittest.TestCase):
    def test_interpolation_offset_is_bounded(self):
        import numpy as np

        flux = np.zeros(3000)
        flux[::100] = 1.0
        got = beatdetect.candidate_periods(flux, 0.01)
        self.assertTrue(all(p > 0 for p in got), got)


class Id3Unsync(unittest.TestCase):
    def test_v24_unsynchronised_frame(self):
        def sync(n):
            return bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127])

        body = b"\x00ab\xff\x00\xe0cd"
        frame = b"TIT2" + sync(len(body)) + b"\x00\x02" + body
        tag = b"ID3\x04\x00\x00" + sync(len(frame)) + frame
        got = dict(tags.id3_frames(tag))
        self.assertEqual(got[b"TIT2"], b"\x00ab\xff\xe0cd")


if __name__ == "__main__":
    unittest.main()


# the tracks of a key that no playlist lists stay in the database of a keep export
class UnlistedTracks(unittest.TestCase):
    def test_keep_export_keeps_a_track_that_is_in_no_playlist(self):
        from djapp.export.cdj import usb
        from djapp.model import Node
        from djapp.sources import key
        from tests.test_key_merge import Base

        class Case(Base):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        try:
            case.run_export([Node("playlist", "A", tracks=case.tracks[:3])])
            old = key.read(str(case.out))
            hidden = old.nodes[0].tracks.pop()
            old.extra.append(hidden)
            with __import__("tests.test_key_support", fromlist=["inline"]).inline(case.cache):
                rep = usb.export(
                    [Node("playlist", "B", tracks=case.tracks[3:5])],
                    str(case.out),
                    old=old,
                    processes=1,
                )
            self.assertEqual(rep.errors, [])
            got = key.read(str(case.out))
            self.assertEqual(got.unlisted, 1)
            self.assertEqual(len(got.anlz), 5)
        finally:
            case.temp.cleanup()
