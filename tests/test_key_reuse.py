# A merge keeps the analysis files of the tracks that stay on the key, and Cancel works while
# the key is read. The analysis of a track nobody touched is not made again: it costs a decode of
# the whole audio from the drive, and a key made by rekordbox holds sections this program does not
# write (phrases, a second beat grid) that a new file would lose

import os
import struct
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from djapp import export
from djapp.export.cdj import usb
from djapp.export.manifest import Cancelled
from djapp.model import Cue, Node, playlists
from djapp.sources import key

from tests.test_key_support import cfg, export_to, inline, make_track, tmp


# ----------------------------------------------------
# every ANLZ file of the key: path -> (bytes, modification time in ns)
def analysis_files(out: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(out)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted((out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.*"))}


# ----------------------------------------------------
# what rekordbox adds and this program does not write: a PSSI section at the end of an EXT file
def with_phrases(path: Path):
    raw = bytearray(path.read_bytes())
    extra = b"PSSI" + struct.pack(">II", 12, 20) + b"\x01\x02\x03\x04\x05\x06\x07\x08"
    struct.pack_into(">I", raw, 8, struct.unpack_from(">I", raw, 8)[0] + len(extra))
    path.write_bytes(bytes(raw) + extra)


# ------------------------------------------------
class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.root = tmp()
        self.addCleanup(self.dir.cleanup)
        self.music, self.cache, self.out = self.root / "music", self.root / "cache", self.root / "key"
        self.music.mkdir()
        self.cache.mkdir()
        self.old = [make_track(self.music, f"old{i}", i + 1, artist=f"Artist {i}",
                               cues=[Cue(name="Drop", start=1000.0 + i, hotcue=0, color="#E02823")]) for i in range(3)]
        self.new = make_track(self.music, "fresh", 9, artist="Fresh")

    def first(self):
        export_to([Node("playlist", "Old", tracks=self.old)], self.out, self.cache)

    def merge(self, **extra):
        calls = []
        real = usb.anlz_job

        def counting(t, path):
            calls.append(os.path.basename(t.path))
            return real(t, path)

        with patch.object(usb, "anlz_job", counting):
            rep = export_to([Node("playlist", "New", tracks=[self.new])], self.out, self.cache, **extra)
        return rep, calls

    def test_the_tracks_that_stay_keep_their_files_byte_for_byte(self):
        self.first()
        for p in (self.out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.EXT"):
            with_phrases(p)
        before = analysis_files(self.out)
        rep, calls = self.merge()
        after = analysis_files(self.out)
        for name, (data, mtime) in before.items():
            self.assertEqual(after[name], (data, mtime), name)
        self.assertEqual(rep.reused, 3)
        self.assertEqual(calls, ["fresh.flac"])
        self.assertEqual(len(after), len(before) + 2)  # the new track has its pair

    def test_the_tracks_that_stay_are_still_in_the_checksums_and_verify(self):
        self.first()
        self.merge(verify_copy=True)
        chk = export.manifest.verify(str(self.out))
        self.assertTrue(chk.good, chk.summary())
        listed = export.manifest.read(str(self.out))["files"]
        self.assertEqual(len([p for p in listed if p.endswith("ANLZ0000.EXT")]), 4)

    def test_cues_and_grid_of_a_track_that_stays_come_back_the_same(self):
        self.first()
        before = {t.title: [(c.name, c.start, c.hotcue, c.color) for c in t.cues]
                  for pl in playlists(key.read(str(self.out)).nodes) for t in pl.tracks}
        self.merge()
        after = {t.title: [(c.name, c.start, c.hotcue, c.color) for c in t.cues]
                 for pl in playlists(key.read(str(self.out)).nodes) for t in pl.tracks}
        for title, cues in before.items():
            self.assertEqual(after[title], cues)

    def test_audio_that_changed_on_the_key_is_analysed_again(self):
        self.first()
        changed = next(self.out.joinpath("Contents").rglob("old1.flac"))
        changed.write_bytes(Path(self.old[2].path).read_bytes())  # another song, another size
        before = analysis_files(self.out)
        rep, calls = self.merge()
        after = analysis_files(self.out)
        self.assertEqual(rep.reused, 2)
        self.assertIn("old1.flac", calls)
        moved = [n for n in before if after[n] != before[n]]
        self.assertEqual(len(moved), 2)  # its DAT and its EXT

    def test_a_track_with_a_missing_analysis_file_is_analysed_again(self):
        self.first()
        victim = next((self.out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.EXT"))
        victim.unlink()
        rep, calls = self.merge()
        self.assertEqual(rep.reused, 2)
        self.assertEqual(len(calls), 2)  # the new track and the damaged one
        self.assertTrue(victim.is_file())

    def test_replace_mode_analyses_everything(self):
        self.first()
        rep, calls = self.merge(key_mode="replace")
        self.assertEqual(rep.reused, 0)
        self.assertEqual(len(calls), 1)  # only the new track is in the export

    def test_a_track_that_is_part_of_the_export_is_not_kept_as_it_was(self):
        self.first()
        calls = []
        real = usb.anlz_job
        with patch.object(usb, "anlz_job", lambda t, p: (calls.append(1), real(t, p))[1]):
            rep = export_to([Node("playlist", "Other", tracks=[self.old[0]])], self.out, self.cache)
        # old0 comes from the export, its cues may have changed: it goes through the analysis cache
        self.assertEqual(rep.reused, 2)
        self.assertEqual(rep.cached, 3)
        self.assertEqual(calls, [])

    def test_unchanged_trusts_the_time_stamp_only_when_the_database_has_no_size(self):
        self.first()
        old = key.read(str(self.out))
        track = old.nodes[0].tracks[0]
        usb_path = key.key_path(str(self.out), track.path)
        self.assertTrue(usb.unchanged(str(self.out), track, usb_path, old))
        track.size = 0
        self.assertTrue(usb.unchanged(str(self.out), track, usb_path, old))  # not newer than its analysis
        later = Path(track.path).stat().st_mtime + 3600
        os.utime(track.path, (later, later))
        self.assertFalse(usb.unchanged(str(self.out), track, usb_path, old))
        track.size = 1
        self.assertFalse(usb.unchanged(str(self.out), track, usb_path, old))


# ----------------------------------------------------
class CancelWhileReadingTests(unittest.TestCase):
    def test_cancel_during_the_read_of_the_key_stops_before_anything_is_written(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        music, cache, out = root / "music", root / "cache", root / "key"
        music.mkdir()
        cache.mkdir()
        tracks = [make_track(music, f"t{i}", i + 1) for i in range(20)]
        export_to([Node("playlist", "Big", tracks=tracks)], out, cache)
        db = out.joinpath(*key.DB)
        before = (db.read_bytes(), (out / export.manifest.NAME).read_bytes(), sorted(p.name for p in out.rglob("*")))
        stop = threading.Event()
        seen = []

        def progress(pct, message):
            seen.append(message)
            if message.startswith("Reading the key 5/"):
                stop.set()

        new = make_track(music, "new", 99)
        with inline(cache), self.assertRaises(Cancelled):
            export.run("CDJ/USB", [Node("playlist", "New", tracks=[new])], str(out), cfg(), progress, stop)
        self.assertTrue(any(m.startswith("Reading the key 5/") for m in seen))
        self.assertFalse(any(m.startswith("Reading the key 10/") for m in seen))  # it stopped at the next check
        self.assertEqual((db.read_bytes(), (out / export.manifest.NAME).read_bytes(),
                          sorted(p.name for p in out.rglob("*"))), before)

    def test_a_cancel_that_is_not_set_changes_nothing(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        music, cache, out = root / "music", root / "cache", root / "key"
        music.mkdir()
        cache.mkdir()
        export_to([Node("playlist", "A", tracks=[make_track(music, "a", 1)])], out, cache)
        with inline(cache):
            rep = export.run("CDJ/USB", [Node("playlist", "B", tracks=[make_track(music, "b", 2)])], str(out),
                             cfg(), None, threading.Event())
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.kept, 1)


if __name__ == "__main__":
    unittest.main()
