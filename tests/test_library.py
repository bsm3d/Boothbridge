import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from djapp import library
from djapp.export.files import Cancelled
from djapp.sources.folder import natural


# ----------------------------------------------------
def write_wave(path):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)


# ----------------------------------------------------
class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sub").mkdir()
        write_wave(self.root / "song2.wav")
        write_wave(self.root / "sub" / "song10.WAV")
        (self.root / "ignore.txt").write_text("not audio")

    def test_recursive_scan_reads_metadata_and_refresh_finds_new_files(self):
        result = library.scan(str(self.root))
        self.assertEqual([t.title for t in result.tracks], ["song2", "song10"])
        self.assertEqual(result.errors, [])
        self.assertTrue(all(t.duration == 1 for t in result.tracks))
        write_wave(self.root / "new.wav")
        self.assertEqual(len(library.scan(str(self.root)).tracks), 3)
        (self.root / "song2.wav").unlink()
        self.assertEqual(len(library.scan(str(self.root)).tracks), 2)

    def test_exact_duplicates_are_grouped_and_unique_paths_deduplicated(self):
        first = self.root / "song2.wav"
        same = self.root / "sub" / "song10.WAV"
        different = self.root / "different.wav"
        shutil.copyfile(first, different)
        data = different.read_bytes()
        different.write_bytes(data[:-2] + b"\1\0")
        result = library.duplicates([str(first), str(first), str(same), str(different)])
        self.assertEqual(result.groups, [[str(first), str(same)]])
        self.assertEqual(result.errors, [])
        self.assertTrue(all(path.exists() for path in (first, same, different)))

    def make_junction(self, link, target):
        if os.name != "nt":
            self.skipTest("junctions exist only on Windows")
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, check=False)
        if done.returncode != 0:
            self.skipTest("cannot create a junction here")
        self.addCleanup(os.rmdir, link)

    def test_scan_does_not_follow_junctions(self):
        (self.root / "sub" / "a").mkdir()
        self.make_junction(self.root / "sub" / "a" / "loop", self.root)
        outside = Path(self.tmp.name + "_outside")
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside)
        write_wave(outside / "other.wav")
        self.make_junction(self.root / "linked", outside)
        result = library.scan(str(self.root))
        self.assertEqual(sorted(t.title for t in result.tracks), ["song10", "song2"])
        self.assertEqual(result.errors, [])
        # the two real files are byte-identical copies, so exactly one group of two
        groups = library.duplicates([t.path for t in result.tracks]).groups
        self.assertEqual([len(group) for group in groups], [2])

    def test_scan_skips_a_junction_found_by_the_fallback_check(self):
        (self.root / "sub" / "a").mkdir()
        self.make_junction(self.root / "sub" / "a" / "loop", self.root)
        with patch.object(library.os.path, "isjunction", None):
            result = library.scan(str(self.root))
        self.assertEqual(len(result.tracks), 2)

    def test_hardlinks_and_aliases_of_one_file_are_not_duplicates(self):
        first = self.root / "song2.wav"
        link = self.root / "hard.wav"
        try:
            os.link(first, link)
        except OSError:
            self.skipTest("cannot create a hard link here")
        copy = self.root / "copy.wav"
        shutil.copyfile(first, copy)
        self.assertEqual(library.duplicates([str(first), str(link)]).groups, [])
        result = library.duplicates([str(first), str(link), str(copy)])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0]), 2)
        self.assertIn(str(copy), result.groups[0])

    def test_alias_through_a_junction_is_not_a_duplicate(self):
        (self.root / "alias").mkdir()
        self.make_junction(self.root / "alias" / "home", self.root)
        real = str(self.root / "song2.wav")
        alias = str(self.root / "alias" / "home" / "song2.wav")
        self.assertEqual(library.duplicates([real, alias]).groups, [])

    def test_empty_files_are_not_duplicates(self):
        for name in ("empty1.wav", "empty2.wav"):
            (self.root / name).write_bytes(b"")
        result = library.duplicates([str(self.root / "empty1.wav"), str(self.root / "empty2.wav")])
        self.assertEqual(result.groups, [])
        self.assertEqual(result.errors, [])

    def test_natural_order_survives_superscript_digits(self):
        self.assertEqual(natural("1\u00b22"), ["", 1, "\u00b2", 2, ""])
        write_wave(self.root / "take 1\u00b22.wav")
        write_wave(self.root / "take 10.wav")
        result = library.scan(str(self.root))
        self.assertEqual(len(result.tracks), 4)
        self.assertEqual(result.errors, [])

    def test_missing_folder_and_files_are_reported(self):
        with self.assertRaises(FileNotFoundError):
            library.scan(str(self.root / "missing"))
        result = library.duplicates([str(self.root / "gone.wav")])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.groups, [])

    def test_scan_and_duplicate_hashing_are_cancellable(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            library.scan(str(self.root), cancel=cancel)
        with self.assertRaises(Cancelled):
            library.duplicates([], cancel=cancel)
        cancel.clear()
        with self.assertRaises(Cancelled):
            library.duplicates([str(self.root / "song2.wav"), str(self.root / "sub" / "song10.WAV")],
                               progress=lambda *_: cancel.set(), cancel=cancel)

    def test_unreadable_subfolder_is_reported_not_silently_omitted(self):
        def walk(_root, onerror, followlinks):
            onerror(PermissionError("Cannot read protected folder"))
            yield str(self.root), [], ["song2.wav"]

        with patch.object(library.os, "walk", side_effect=walk):
            result = library.scan(str(self.root))
        self.assertEqual(len(result.tracks), 1)
        self.assertIn("protected", result.errors[0])

    def test_file_change_during_hashing_is_not_reported_as_a_duplicate(self):
        first = str(self.root / "song2.wav")
        second = str(self.root / "sub" / "song10.WAV")
        real_hash = library.hashlib.sha256

        def hash_and_change():
            digest = real_hash()
            Path(first).write_bytes(b"changed")
            return digest

        with patch.object(library.hashlib, "sha256", side_effect=hash_and_change):
            result = library.duplicates([first, second])
        self.assertEqual(result.groups, [])
        self.assertTrue(result.errors)


if __name__ == "__main__":
    unittest.main()
