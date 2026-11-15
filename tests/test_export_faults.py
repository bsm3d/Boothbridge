# what a key looks like after an export that failed or was cancelled at any step: the database is
# the old one or the new one, never half of each, it can always be read, every track it lists has its
# audio, and no temporary file is left behind. Found by failing every write in turn

import builtins
import errno
import os
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from djapp import export
from djapp.export import manifest
from djapp.export.cdj import usb
from djapp.export.manifest import Cancelled, write_atomic
from djapp.model import Node, playlists
from djapp.sources import key

from tests.test_key_support import cfg, inline, make_track, tmp


# ------------------------------------------------
# raises ENOSPC at the Nth write, rename or replace under a folder
class Fault:
    def __init__(self, root, at):
        self.root, self.at, self.n, self.fired = str(root).lower(), at, 0, False
        self.lock = threading.Lock()

    def hit(self, path):
        try:
            p = os.path.abspath(os.fspath(path)).lower()
        except TypeError:
            return
        if not p.startswith(self.root):
            return
        with self.lock:
            self.n += 1
            if self.n == self.at:
                self.fired = True
                raise OSError(errno.ENOSPC, "No space left on device (injected)")


def leftovers(out: Path) -> list[str]:
    return [p.name for p in out.rglob("*") if p.is_file() and (p.suffix in (".tmp", ".part") or p.name.startswith(".bb-"))]


def names(out: Path) -> list[str]:
    return sorted(p.name for p in playlists(key.read(str(out)).nodes))


# ------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.music, self.cache = self.root / "music", self.root / "cache"
        self.music.mkdir()
        self.cache.mkdir()
        self.old = [make_track(self.music, f"a{i}", i + 1, artist="A") for i in range(3)]
        self.new = [make_track(self.music, f"b{i}", i + 7, artist="B") for i in range(2)]
        self.base = self.root / "base"
        with inline(self.cache):
            export.run("CDJ/USB", [Node("playlist", "Old", tracks=self.old)], str(self.base), cfg())
        self.db = Path("PIONEER") / "rekordbox" / "export.pdb"
        self.before = (self.base / self.db).read_bytes()

    def merge(self, out, **extra):
        with inline(self.cache):
            return export.run("CDJ/USB", [Node("playlist", "New", tracks=self.new)], str(out), cfg(**extra))


# ------------------------------------------------
class EveryWriteTests(Base):
    def test_a_failed_write_never_leaves_half_a_database_or_a_missing_track(self):
        problems = []
        at = 0
        while True:
            at += 1
            out = self.root / f"run{at}"
            shutil.copytree(self.base, out)
            fault = Fault(out, at)
            real_open, real_replace, real_rename = builtins.open, os.replace, os.rename

            def fopen(file, mode="r", *args, fault=fault, real_open=real_open, **kw):
                if any(c in mode for c in "wxa+") and isinstance(file, str | os.PathLike):
                    fault.hit(file)
                return real_open(file, mode, *args, **kw)

            def freplace(src, dst, *args, fault=fault, real_replace=real_replace, **kw):
                fault.hit(dst)
                return real_replace(src, dst, *args, **kw)

            def frename(src, dst, *args, fault=fault, real_rename=real_rename, **kw):
                fault.hit(dst)
                return real_rename(src, dst, *args, **kw)

            with patch.object(builtins, "open", fopen), patch.object(os, "replace", freplace), \
                    patch.object(os, "rename", frename):
                try:
                    self.merge(out)
                except (OSError, Cancelled, ValueError):
                    pass
            if not fault.fired:
                break
            data = key.read(str(out))
            now = sorted(p.name for p in playlists(data.nodes))
            if now == ["Old"]:
                if (out / self.db).read_bytes() != self.before:
                    problems.append((at, "same playlists, other database"))
            elif now != ["New", "Old"]:
                problems.append((at, f"half a database: {now}"))
            problems += [(at, f"audio missing {os.path.basename(t.path)}") for p in playlists(data.nodes)
                         for t in p.tracks if not os.path.isfile(t.path)]
            problems += [(at, w) for w in data.warnings[:1]]
            if leftovers(out):
                problems.append((at, f"left behind: {leftovers(out)[:2]}"))
        self.assertGreater(at, 15, "the export wrote too few files for this test to mean anything")
        self.assertEqual(problems, [])

# ------------------------------------------------
class CancelTests(Base):
    def test_cancelling_at_any_progress_step_leaves_the_old_or_the_new_key(self):
        problems = []
        cancelled = completed = 0
        for at in range(1, 200):
            out = self.root / f"c{at}"
            shutil.copytree(self.base, out)
            stop, count = threading.Event(), [0]

            def progress(pct, message, stop=stop, count=count, at=at):
                count[0] += 1
                if count[0] == at:
                    stop.set()

            try:
                with inline(self.cache):
                    export.run("CDJ/USB", [Node("playlist", "New", tracks=self.new)], str(out),
                               cfg(verify_copy=True), progress, stop)
                completed += 1
                done = True
            except Cancelled:
                cancelled += 1
                done = False
            data = key.read(str(out))
            now = sorted(p.name for p in playlists(data.nodes))
            if done and now != ["New", "Old"]:
                problems.append((at, f"finished with {now}"))
            if not done:
                if now == ["Old"]:
                    if (out / self.db).read_bytes() != self.before:
                        problems.append((at, "cancelled, same playlists, other database"))
                elif now != ["New", "Old"]:
                    problems.append((at, f"cancelled and half done: {now}"))
            problems += [(at, f"audio missing {os.path.basename(t.path)}") for p in playlists(data.nodes)
                         for t in p.tracks if not os.path.isfile(t.path)]
            problems += [(at, w) for w in data.warnings[:1]]
            if leftovers(out):
                problems.append((at, f"left behind: {leftovers(out)[:2]}"))
            if count[0] < at:
                break
        self.assertGreater(cancelled, 10)
        self.assertEqual(problems, [])


class CopyFailureTests(Base):
    def test_a_track_whose_audio_did_not_arrive_is_left_out_of_the_database(self):
        real = usb.copy_all

        def failing(jobs, verify, cancel=None, progress=None, man=None):
            kept = [j for j in jobs if os.path.basename(j[0]) != "b1.flac"]
            errors = real(kept, verify, cancel, progress, man)
            return [*errors, "b1.flac: No space left on device"]

        out = self.root / "partial"
        shutil.copytree(self.base, out)
        with patch.object(usb, "copy_all", failing):
            report = self.merge(out)
        self.assertTrue(any("b1.flac" in e for e in report.errors))
        self.assertTrue(any("left out of the database" in e for e in report.errors))
        data = key.read(str(out))
        self.assertEqual(data.warnings, [])
        listed = {os.path.basename(t.path): p.name for p in playlists(data.nodes) for t in p.tracks}
        self.assertNotIn("b1.flac", listed)
        self.assertEqual(listed["b0.flac"], "New")
        self.assertEqual(report.tracks, 4)  # three old ones and b0
        # exported again to the same place, the missing one arrives
        report = self.merge(out)
        self.assertEqual(report.errors, [])
        self.assertIn("b1.flac", [os.path.basename(t.path) for p in playlists(key.read(str(out)).nodes) for t in p.tracks])


    def test_nothing_is_written_when_no_audio_arrived_in_a_first_export(self):
        def failing(jobs, verify, cancel=None, progress=None, man=None):
            return ["everything: No space left on device"]

        out = self.root / "empty"
        with patch.object(usb, "copy_all", failing):
            report = self.merge(out)
        self.assertEqual(report.tracks, 0)
        self.assertFalse((out / self.db).exists())


# ------------------------------------------------
class TemporaryFileTests(unittest.TestCase):
    def test_write_atomic_removes_its_temporary_file_when_the_replace_fails(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        target = root / "x.json"
        target.write_bytes(b"old")
        with patch.object(manifest.os, "replace", side_effect=OSError(errno.ENOSPC, "full")), \
                self.assertRaises(OSError):
            write_atomic(str(target), b"new")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["x.json"])

    def test_write_atomic_removes_it_when_the_write_fails(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        target = root / "x.json"

        class Full:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, data):
                raise OSError(errno.ENOSPC, "full")

        real = builtins.open
        with patch.object(builtins, "open", lambda f, m="r", *a, **k: Full() if str(f).endswith(".tmp") else real(f, m, *a, **k)), \
                self.assertRaises(OSError):
            write_atomic(str(target), b"new")
        self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------
@unittest.skipUnless(os.name == "nt", "Windows drops the trailing spaces and dots of a folder name")
class OutputNameTests(unittest.TestCase):
    def test_an_output_folder_with_a_trailing_space_or_dot_is_exported_to(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        music, cache = root / "music", root / "cache"
        music.mkdir()
        cache.mkdir()
        track = make_track(music, "a", 1)
        for suffix in (" ", "  ", ".", " . "):
            with self.subTest(suffix=repr(suffix)), inline(cache):
                rep = export.run("CDJ/USB", [Node("playlist", "P", tracks=[track])], str(root / "k") + suffix,
                                 cfg(key_mode="replace"))
                self.assertEqual(rep.errors, [])
                self.assertEqual([p.name for p in playlists(key.read(str(root / "k")).nodes)], ["P"])
