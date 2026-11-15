# the key reader gives back what the CDJ export wrote

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import soundfile
from djapp import sources
from djapp.export.cdj import anlz, anlzread, pdbwrite
from djapp.model import CUE, LOOP, Cue, Node, Track, unique_tracks
from djapp.sources import key

from tests.test_key_support import (
    cue_view,
    export_to,
    make_track,
    random_library,
    tmp,
    tree_view,
)


# --------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.music = self.root / "music"
        self.music.mkdir()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.out = self.root / "key"

    def export(self, nodes, out=None, **extra):
        rep = export_to(nodes, out or self.out, self.cache, **extra)
        self.assertEqual(rep.errors, [])
        return rep

# ------------------------------------------------------------
class RoundTripTests(Base):
    def test_random_libraries_come_back_field_for_field(self):
        for seed in range(14):
            with self.subTest(seed=seed):
                nodes = random_library(seed, self.music, 8)
                out = self.root / f"key{seed}"
                self.export(nodes, out)
                got = key.read(str(out))
                self.assertEqual(got.warnings, [], f"seed {seed}")
                self.assertEqual(tree_view(got.nodes), tree_view(nodes), f"seed {seed}")

    def test_paths_are_the_audio_on_the_key_and_sizes_are_real(self):
        nodes = random_library(3, self.music, 6)
        self.export(nodes)
        got = key.read(str(self.out)).nodes
        for t in unique_tracks(got):
            self.assertTrue(Path(t.path).is_file(), t.path)
            self.assertEqual(Path(t.path).parent.parent.parent.name, "Contents")
            self.assertEqual(t.size, os.path.getsize(t.path))
            self.assertEqual(t.duration, soundfile.info(t.path).duration)

    def test_a_track_shared_by_playlists_is_one_object(self):
        t = make_track(self.music, "shared", 1)
        a = Node("playlist", "A", tracks=[t, t])
        b = Node("folder", "F", children=[Node("playlist", "B", tracks=[t])])
        self.export([a, b])
        got = key.load(str(self.out))
        self.assertEqual(len(unique_tracks(got)), 1)
        self.assertIs(got[0].tracks[0], got[0].tracks[1])
        self.assertIs(got[0].tracks[0], got[1].children[0].tracks[0])

    def test_folders_and_empty_playlists_keep_their_order(self):
        t = make_track(self.music, "one", 2)
        nodes = [Node("folder", "Z folder", children=[
                    Node("folder", "Inner", children=[Node("playlist", "Deep", tracks=[t])]),
                    Node("playlist", "Empty")]),
                 Node("playlist", "A", tracks=[t]), Node("folder", "Empty folder")]
        self.export(nodes)
        got = key.load(str(self.out))

        def shape(ns):
            return [(n.kind, n.name, shape(n.children)) for n in ns]

        self.assertEqual(shape(got), shape(nodes))

    def test_what_the_writer_cannot_keep_is_stated(self):
        t = make_track(self.music, "lossy", 3, album="", album_artist="Someone", added="", bpm=120.456)
        t.cues = [Cue(kind=LOOP, start=1000.4, length=500.2, hotcue=2, color="#E02823"),
                  Cue(start=300.5, hotcue=-1, color="#FF0000", name=" memory ")]
        self.export([Node("playlist", "P", tracks=[t])])
        got = key.load(str(self.out))[0].tracks[0]
        self.assertEqual(got.album_artist, "")  # an album artist lives in the album row
        self.assertEqual(got.bpm, 120.46)  # tempo x 100, rounded
        self.assertEqual([cue_view(c) for c in got.cues], [cue_view(c) for c in t.hot_cues() + t.memory_cues()])
        self.assertEqual(got.cues[1].color, "")  # memory cues are not coloured
        self.assertEqual(got.cues[0].kind, LOOP)
        self.assertEqual((got.cues[0].start, got.cues[0].length), (1000.0, 501.0))  # the end is rounded on its own
        self.assertEqual(got.cues[1].name, "memory")

    def test_load_kind_cues_come_back_as_plain_cues(self):
        t = make_track(self.music, "load", 4)
        t.cues = [Cue(kind=3, start=100, hotcue=0), Cue(kind=1, start=200, hotcue=1)]  # LOAD, FADE_IN
        self.export([Node("playlist", "P", tracks=[t])])
        got = key.load(str(self.out))[0].tracks[0]
        self.assertEqual([(c.kind, c.hotcue) for c in got.cues], [(CUE, 0)])


# --------------------------------------------------------
class DetectTests(Base):
    def setUp(self):
        super().setUp()
        self.export([Node("playlist", "P", tracks=[make_track(self.music, "d", 5)])])

    def test_the_key_root_a_folder_holding_pioneer_and_the_file_itself(self):
        db = self.out / "PIONEER" / "rekordbox" / "export.pdb"
        for path in (self.out, db):
            self.assertEqual(sources.detect(str(path)), "key", path)
            self.assertEqual(sources.load(str(path))[0].name, "P")
        self.assertEqual(sources.detect(str(self.music)), "folder")
        self.assertEqual(sources.detect(str(self.out / "Contents")), "folder")
        self.assertEqual(len(sources.load(str(self.music))), 1)

    def test_a_copy_of_the_key_in_another_folder_is_a_key_too(self):
        import shutil
        shutil.copytree(self.out, self.root / "elsewhere" / "backup")
        self.assertEqual(sources.detect(str(self.root / "elsewhere" / "backup")), "key")
        t = sources.load(str(self.root / "elsewhere" / "backup"))[0].tracks[0]
        self.assertTrue(t.path.startswith(str(self.root / "elsewhere" / "backup")))

    def test_an_export_pdb_that_is_not_a_database_is_a_load_error(self):
        bad = self.root / "bad" / "export.pdb"
        bad.parent.mkdir()
        bad.write_bytes(b"not a database" * 400)
        self.assertEqual(sources.detect(str(bad)), "key")
        with self.assertRaisesRegex(ValueError, "damaged"):
            sources.load(str(bad))
        with self.assertRaisesRegex(ValueError, "not a CDJ USB key"):
            key.read(str(self.music))

    def test_a_database_too_large_is_refused_before_it_is_read(self):
        with patch.object(key, "MAX_DB", 1000), self.assertRaisesRegex(ValueError, "too large"):
            key.read(str(self.out))


class AnalysisFilesTests(Base):
    def setUp(self):
        super().setUp()
        self.t = make_track(self.music, "a", 6, bpm=124.0, grid=210.5)
        self.t.cues = [Cue(name="Intro", start=500, hotcue=0), Cue(start=800, hotcue=-1)]
        self.export([Node("playlist", "P", tracks=[self.t])])
        self.dat = next((self.out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.DAT"))
        self.ext = self.dat.with_suffix(".EXT")

    def loaded(self):
        with self.assertLogs(key.log, level="WARNING") as cm:
            t = key.load(str(self.out))[0].tracks[0]
        return t, cm.output

    def test_missing_files_load_without_cues_and_warn(self):
        self.dat.unlink()
        self.ext.unlink()
        t, logs = self.loaded()
        self.assertEqual((t.cues, t.grid), ([], None))
        self.assertEqual(t.title, self.t.title)
        self.assertEqual(t.bpm, 124.0)
        self.assertTrue(any("missing" in line for line in logs), logs)

    def test_damaged_files_load_without_cues_and_warn(self):
        self.dat.write_bytes(self.dat.read_bytes()[:300])
        self.ext.write_bytes(b"\x00" * 90)
        t, logs = self.loaded()
        self.assertEqual((t.cues, t.grid), ([], None))
        self.assertEqual(len([line for line in logs if "damaged" in line]), 2, logs)
        got = key.read(str(self.out))
        self.assertEqual(len(got.warnings), 2)

    def test_without_the_ext_the_dat_still_gives_grid_and_cues_but_no_names(self):
        self.ext.unlink()
        t, _ = self.loaded()
        self.assertIsNotNone(t.grid)
        self.assertEqual([(c.start, c.hotcue, c.name) for c in t.cues], [(500.0, 0, ""), (800.0, -1, "")])


    def test_analysis_that_belongs_to_another_track_is_not_used(self):
        other = make_track(self.music, "other", 7)
        other.cues = [Cue(start=999, hotcue=3)]
        self.export([Node("playlist", "Q", tracks=[other])], self.root / "key2")
        theirs = next((self.root / "key2" / "PIONEER" / "USBANLZ").rglob("ANLZ0000.DAT"))
        self.dat.write_bytes(theirs.read_bytes())
        self.ext.write_bytes(theirs.with_suffix(".EXT").read_bytes())
        t, logs = self.loaded()
        self.assertEqual(t.cues, [])
        self.assertTrue(any("belong" in line for line in logs), logs)


# ------------------------------------------------------------
# A key is data someone else may have written: nothing leaves the key
class HostilePathTests(Base):
    count = 0

    def build(self, usb_path, anlz_path):
        t = Track(title="x", artist="a", path="x.wav")
        HostilePathTests.count += 1
        root = self.root / f"evil{self.count}"
        db = root / "PIONEER" / "rekordbox"
        db.mkdir(parents=True)
        pdbwrite.write([pdbwrite.Item(t, 1, usb_path, anlz_path, 10)], [Node("playlist", "P", tracks=[t])],
                       db / "export.pdb")
        return root

    def test_paths_that_climb_out_of_the_key_are_dropped(self):
        outside = self.root / "outside.wav"
        outside.write_bytes(b"x")
        for evil in ("/Contents/../../outside.wav", "/..", "C:/Windows/x.wav", "/Contents\\x.wav",
                     "Contents/x.wav", "/Contents//x.wav", "/Contents/\x01.wav"):
            with self.subTest(path=evil):
                root = self.build(evil, "/PIONEER/USBANLZ/P000/00000000/ANLZ0000.DAT")
                got = key.read(str(root))
                self.assertEqual(got.nodes[0].tracks[0].path, "")
                self.assertTrue(got.warnings)

    def test_an_analysis_path_outside_the_scheme_is_not_opened(self):
        outside = self.root / "outside" / "ANLZ0000.DAT"
        outside.parent.mkdir()
        outside.write_bytes(b"PMAI")
        root = self.build("/Contents/x.wav", "/PIONEER/../../outside/ANLZ0000.DAT")
        got = key.read(str(root))
        self.assertEqual(got.nodes[0].tracks[0].cues, [])
        self.assertTrue(any("analysis path" in w for w in got.warnings), got.warnings)


# ----------------------------------------------------------------
class SummaryTests(Base):
    def test_counts_names_and_no_analysis_read(self):
        t = [make_track(self.music, f"s{i}", i) for i in range(3)]
        nodes = [Node("folder", "Sets", children=[Node("playlist", "One", tracks=t[:2]),
                                                  Node("folder", "Deep", children=[Node("playlist", "Two", tracks=t[1:])])]),
                 Node("playlist", "Top", tracks=t[:1])]
        self.export(nodes)
        from djapp import export
        with patch.object(anlzread, "read", side_effect=AssertionError("analysis read")):
            s = export.key_summary(str(self.out))
        self.assertEqual((s.folders, s.playlists, s.tracks), (2, 3, 3))
        self.assertEqual(s.names, ["Sets / One", "Sets / Deep / Two", "Top"])
        self.assertTrue(s.readable)
        self.assertIsNone(export.key_summary(str(self.music)))
        self.assertIsNone(export.key_summary(str(self.root / "nowhere")))

    def test_a_database_that_cannot_be_read_is_a_summary_that_says_so(self):
        db = self.out / "PIONEER" / "rekordbox"
        db.mkdir(parents=True)
        (db / "export.pdb").write_bytes(b"junk")
        from djapp import export
        s = export.key_summary(str(self.out))
        self.assertFalse(s.readable)
        self.assertIn("cannot be read", s.error)
        self.assertEqual((s.playlists, s.tracks, s.names), (0, 0, []))

    def test_merge_names_from_the_summary_alone(self):
        from djapp import export
        t = make_track(self.music, "mn", 1)
        self.export([Node("folder", "Sets", children=[Node("playlist", "One", tracks=[t])]),
                     Node("playlist", "Top", tracks=[t]), Node("playlist", "Top", tracks=[t])])
        s = export.key_summary(str(self.out))
        new = [Node("playlist", "top", tracks=[t]), Node("playlist", "One"),
               Node("folder", "SETS", children=[Node("playlist", "one"), Node("playlist", "Two")])]
        self.assertEqual(export.merge_names(new, s), ["Top", "Sets / One"])
        self.assertEqual(export.merge(new, key.load(str(self.out)))[1], export.merge_names(new, s))

# ------------------------------------------------------------
class AnlzReaderTests(unittest.TestCase):
    class Audio:
        ok = True

        def __init__(self):
            import numpy as np
            self.fine = [np.ones(150)]

        def at(self, n):
            import numpy as np
            from djapp.analysis import Bands
            v = np.ones(n, dtype=np.float32) * .5
            return Bands(44100, v, v, v)

    def files(self, t):
        return anlz.build_py(t, "/Contents/Ä \u2606/x.wav", self.Audio())

    def test_both_files_are_read_and_waveforms_are_stepped_over(self):
        t = Track(bpm=128.0, grid=95.0, duration=10,
                  cues=[Cue(name="Café", start=1500.4, hotcue=1, color="#E02823"),
                        Cue(kind=LOOP, start=3000, length=1000, hotcue=-1), Cue(start=77, hotcue=-1)])
        dat, ext = (anlzread.parse(x) for x in self.files(t))
        self.assertEqual((dat.path, ext.path), ("/Contents/Ä \u2606/x.wav",) * 2)
        self.assertEqual((dat.bpm, dat.beats), (128.0, len(anlz.beats(t))))
        self.assertEqual(dat.frames, 44100)
        self.assertFalse(dat.detailed)
        self.assertTrue(ext.detailed)
        both = anlzread.combine(dat, ext)
        self.assertEqual([(c.slot, c.loop, c.start, c.end, c.name) for c in both.cues],
                         [(2, False, 1500, None, "Café"), (0, True, 3000, 4000, ""), (0, False, 77, None, "")])
        self.assertEqual(both.cues[0].color, f"#{anlz.hot_color(t.cues[0])[1]:06X}")
        self.assertEqual(anlzread.combine(dat, None).cues[0].name, "")
        self.assertEqual(anlzread.combine(None, None).cues, [])

    def test_no_beat_grid_is_none(self):
        dat, _ = self.files(Track(bpm=0, grid=None, duration=5))
        got = anlzread.parse(dat)
        self.assertEqual((got.grid, got.beats), (None, 0))

    def test_the_grid_laid_again_gives_the_same_beats(self):
        import random
        rng = random.Random(7)
        for _ in range(300):
            t = Track(bpm=round(rng.uniform(60, 190), 2), grid=rng.uniform(0, 5000), duration=rng.uniform(5, 400))
            before = anlz.beats(t)
            got = anlzread.parse(self.files(t)[0])
            again = Track(bpm=got.bpm, grid=got.grid, duration=t.duration)
            self.assertEqual(anlz.beats(again), before, (t.bpm, t.grid, t.duration))

    def test_what_is_not_an_anlz_file_is_one_error(self):
        dat, _ = self.files(Track(bpm=120, grid=0, duration=3))
        bad = [b"", b"PMAI", b"XXXX" + dat[4:], dat[:40], dat[:-1], dat + b"\0",
               dat[:8] + b"\xff\xff\xff\xff" + dat[12:], dat[:4] + b"\0\0\0\x05" + dat[8:]]
        for data in bad[:5] + bad[6:]:
            with self.assertRaises(anlzread.AnlzError, msg=data[:20]):
                anlzread.parse(data)
        self.assertEqual(anlzread.parse(bad[5]).path, "/Contents/Ä \u2606/x.wav")  # bytes after the end are ignored
        self.assertTrue(issubclass(anlzread.AnlzError, ValueError))

    def test_a_cue_count_that_lies_is_an_error(self):
        t = Track(bpm=120, grid=0, duration=3, cues=[Cue(start=5, hotcue=0)])
        dat, _ = self.files(t)
        at = dat.index(b"PCOB")
        broken = bytearray(dat)
        broken[at + 18:at + 20] = b"\x00\x09"
        with self.assertRaises(anlzread.AnlzError):
            anlzread.parse(bytes(broken))


if __name__ == "__main__":
    unittest.main()
