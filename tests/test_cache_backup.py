# back up and restore of the analysis cache: the module without Qt, then the buttons of the
# Options window (CDJ tab) with the file dialogs and the message boxes replaced

import builtins
import hashlib
import json
import os
import random
import stat
import struct
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import VERSION, cachebackup, settings
from djapp.export.cdj.usb import ANLZ_VERSION
from djapp.export.files import Cancelled
from djapp.ui import dialogs, waveform, worker
from djapp.ui.dialogs import USAGE, Options
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QPushButton,
)

KEYS = [hashlib.sha1(str(i).encode()).hexdigest() for i in range(3)]
HOSTILE_KEY = "a" * 40
# what make_cache adds that is not a cache file
FOREIGN = sorted(["anlz/notes.txt", f"wave/{KEYS[0]}.bin.1234.tmp", "elsewhere.DAT"])


# ------------------------------------------------------
# the four kinds of file the program writes, plus files that are not ours. Gives back
# {name in the archive: bytes} for the real ones
def make_cache(cache: Path, keys=KEYS[:2]) -> dict:
    rnd = random.Random(7)
    real = {}
    for key in keys:
        for sub, ext in (("anlz", "DAT"), ("anlz", "EXT"), ("wave", "bin"), ("wave", "hi1000")):
            (cache / sub).mkdir(parents=True, exist_ok=True)
            real[f"{sub}/{key}.{ext}"] = rnd.randbytes(3000 + rnd.randrange(2000))
    for name, data in real.items():
        (cache / name).write_bytes(data)
    (cache / "anlz" / "notes.txt").write_bytes(b"mine")
    (cache / "wave" / f"{KEYS[0]}.bin.1234.tmp").write_bytes(b"half")
    (cache / "wave" / "folder").mkdir()
    (cache / "elsewhere.DAT").write_bytes(b"outside")
    return real


def tree(cache: Path) -> dict:
    return {p.relative_to(cache).as_posix(): p.read_bytes() for p in sorted(cache.rglob("*")) if p.is_file()}


def note_for(count: int, **extra) -> dict:
    return dict({"format": cachebackup.FORMAT, "format_version": 1, "program_version": VERSION,
                 "anlz_version": ANLZ_VERSION, "files": count}, **extra)


# --------------------------------------------------------------
# A ZIP made by hand. items is a list of (name or ZipInfo, bytes), note the manifest as a dict,
# a raw string, or None for no manifest
def write_zip(path: Path, items, note="ok"):
    if note == "ok":
        note = note_for(len(items))
    with warnings.catch_warnings(), zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        warnings.simplefilter("ignore")
        for name, data in items:
            z.writestr(name, data)
        if note is not None:
            z.writestr(cachebackup.MANIFEST, note if isinstance(note, str) else json.dumps(note))


# --------------------------------------------------------------------
# change what the central directory says about one entry, the data stays
def patch_central(path: Path, name: str, size=None, packed=None, flags=None):
    raw = bytearray(path.read_bytes())
    i = 0
    while (i := raw.find(b"PK\x01\x02", i)) >= 0:
        n = struct.unpack_from("<H", raw, i + 28)[0]
        if bytes(raw[i + 46:i + 46 + n]) == name.encode():
            if size is not None:
                struct.pack_into("<L", raw, i + 24, size)
            if packed is not None:
                struct.pack_into("<L", raw, i + 20, packed)
            if flags is not None:
                struct.pack_into("<H", raw, i + 8, flags)
            path.write_bytes(bytes(raw))
            return
        i += 4
    raise AssertionError(name)


# ----------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.out = self.root / "out"
        self.out.mkdir()
        self.dest = self.out / "analyses.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, path=None):
        with zipfile.ZipFile(path or self.dest) as z:
            return sorted(z.namelist())

    def leftovers(self):
        return sorted(p.name for p in self.out.iterdir())


# ----------------------------------------------------------------
class CreateTests(Base):
    def test_zip_holds_exactly_the_cache_files_the_manifest_and_a_checksum(self):
        real = make_cache(self.cache)
        res = cachebackup.create(self.cache, self.dest)
        self.assertEqual(self.names(), sorted([*real, cachebackup.MANIFEST]))
        with zipfile.ZipFile(self.dest) as z:
            for name, data in real.items():
                self.assertEqual(z.read(name), data)
                self.assertNotIn("\\", name)
            note = json.loads(z.read(cachebackup.MANIFEST))
        self.assertEqual((note["program_version"], note["anlz_version"], note["files"]),
                         (VERSION, ANLZ_VERSION, len(real)))
        self.assertTrue(note["created"])
        sha = hashlib.sha256(self.dest.read_bytes()).hexdigest()
        side = Path(str(self.dest) + ".sha256")
        self.assertEqual(side.read_text(), f"{sha} *analyses.zip\n")
        self.assertEqual((res.files, res.sha256, res.size, res.failed), (len(real), sha, self.dest.stat().st_size, []))
        self.assertEqual(self.leftovers(), ["analyses.zip", "analyses.zip.sha256"])

    def test_the_sidecar_is_the_one_the_project_backup_reads(self):
        make_cache(self.cache)
        cachebackup.create(self.cache, self.dest)
        from djapp import backup
        self.assertEqual(backup.verify(str(self.dest)), hashlib.sha256(self.dest.read_bytes()).hexdigest())

    def test_a_cancelled_run_leaves_nothing_and_keeps_the_old_backup(self):
        make_cache(self.cache)
        cachebackup.create(self.cache, self.dest)
        before = {n: (self.out / n).read_bytes() for n in self.leftovers()}
        stop = threading.Event()
        seen = []

        def progress(pct, message):
            seen.append(pct)
            if len(seen) == 3:
                stop.set()

        with self.assertRaises(Cancelled):
            cachebackup.create(self.cache, self.dest, progress, stop)
        self.assertEqual({n: (self.out / n).read_bytes() for n in self.leftovers()}, before)

    def test_cancelled_before_it_starts_leaves_nothing(self):
        make_cache(self.cache)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Cancelled):
            cachebackup.create(self.cache, self.dest, cancel=stop)
        self.assertEqual(self.leftovers(), [])

    def test_a_file_that_cannot_be_opened_is_listed_and_the_rest_is_saved(self):
        # decision: the backup is made without it, the caller sees Result.failed and decides
        real = make_cache(self.cache)
        bad = str(self.cache / "wave" / f"{KEYS[0]}.hi1000")
        opener = builtins.open

        def deny(path, *a, **kw):
            if str(path) == bad:
                raise PermissionError(13, "Permission denied", bad)
            return opener(path, *a, **kw)

        with patch.object(cachebackup, "open", deny, create=True):
            res = cachebackup.create(self.cache, self.dest)
        self.assertEqual(res.files, len(real) - 1)
        self.assertEqual(len(res.failed), 1)
        self.assertIn(f"wave/{KEYS[0]}.hi1000", res.failed[0])
        self.assertNotIn(f"wave/{KEYS[0]}.hi1000", self.names())
        self.assertEqual(cachebackup.verify(str(self.dest)).files, len(real) - 1)

    def test_no_readable_file_is_a_failure_and_leaves_nothing(self):
        make_cache(self.cache)
        opener = builtins.open

        def deny(path, *a, **kw):
            if str(path).startswith(str(self.cache)):
                raise PermissionError(13, "Permission denied", str(path))
            return opener(path, *a, **kw)

        with patch.object(cachebackup, "open", deny, create=True), self.assertRaises(OSError):
            cachebackup.create(self.cache, self.dest)
        self.assertEqual(self.leftovers(), [])

    def test_a_read_error_in_the_middle_of_a_file_stops_everything(self):
        make_cache(self.cache)
        opener = builtins.open

        class Breaking:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def read(self, n=-1):
                raise OSError(5, "Input/output error")

        def flaky(path, *a, **kw):
            f = opener(path, *a, **kw)
            return Breaking(f) if str(path).endswith(".hi1000") else f

        with patch.object(cachebackup, "open", flaky, create=True), self.assertRaises(OSError):
            cachebackup.create(self.cache, self.dest)
        self.assertEqual(self.leftovers(), [])

    def test_an_empty_or_missing_cache_has_nothing_to_back_up(self):
        for case in ("missing", "empty", "foreign only"):
            with self.subTest(case):
                shutil_cache = self.root / case.replace(" ", "_")
                if case != "missing":
                    (shutil_cache / "anlz").mkdir(parents=True)
                if case == "foreign only":
                    (shutil_cache / "anlz" / "readme.txt").write_text("x")
                with self.assertRaises(ValueError):
                    cachebackup.create(shutil_cache, self.dest)
                self.assertEqual(self.leftovers(), [])

    def test_a_symlink_in_the_cache_is_not_followed(self):
        make_cache(self.cache)
        target = self.root / "secret.bin"
        target.write_bytes(b"secret")
        link = self.cache / "wave" / f"{KEYS[2]}.bin"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("no symbolic links here")
        cachebackup.create(self.cache, self.dest)
        self.assertNotIn(f"wave/{KEYS[2]}.bin", self.names())

    def test_the_destination_folder_must_exist(self):
        make_cache(self.cache)
        with self.assertRaises(OSError):
            cachebackup.create(self.cache, self.out / "nope" / "a.zip")

    def test_the_layout_follows_the_zoomed_waveform_rate(self):
        # if the rate changes, the extension of the detail file changes with it
        key = "a" * 40
        self.assertTrue(cachebackup.LAYOUT["wave"].fullmatch(f"{key}.hi{waveform.HI_PER_SEC}"))

    def test_the_names_of_real_cache_keys_are_accepted(self):
        from djapp.export.cdj import usb
        from djapp.model import Track
        audio = self.root / "a.mp3"
        audio.write_bytes(b"x")
        ckey = usb.cache_key(Track(path=str(audio)), "/Contents/a.mp3")
        self.assertTrue(cachebackup.LAYOUT["anlz"].fullmatch(f"{ckey}.DAT"))
        with patch.object(waveform, "cache_dir", return_value=self.root):
            self.assertTrue(cachebackup.LAYOUT["wave"].fullmatch(waveform.hi_file(str(audio)).name))
            self.assertTrue(cachebackup.LAYOUT["wave"].fullmatch(waveform.cache_file(str(audio)).name))


class VerifyTests(Base):
    def setUp(self):
        super().setUp()
        self.real = make_cache(self.cache)
        cachebackup.create(self.cache, self.dest)

    def test_a_good_archive_passes(self):
        res = cachebackup.verify(str(self.dest))
        self.assertEqual((res.files, res.sidecar), (len(self.real), True))

    def test_a_damaged_archive_is_caught_by_the_checksum_file(self):
        raw = bytearray(self.dest.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        self.dest.write_bytes(bytes(raw))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            cachebackup.verify(str(self.dest))

    def test_a_damaged_archive_is_caught_by_the_crc_without_a_checksum_file(self):
        Path(str(self.dest) + ".sha256").unlink()
        raw = bytearray(self.dest.read_bytes())
        raw[len(raw) // 3] ^= 0xFF
        self.dest.write_bytes(bytes(raw))
        with self.assertRaises(ValueError):
            cachebackup.verify(str(self.dest))

    def test_a_truncated_archive_is_refused(self):
        Path(str(self.dest) + ".sha256").unlink()
        self.dest.write_bytes(self.dest.read_bytes()[:-500])
        with self.assertRaises(ValueError):
            cachebackup.verify(str(self.dest))

    def test_bytes_missing_at_the_start_are_a_damaged_zip_not_an_os_error(self):
        # found by the random cache archives: the offsets of the entries come out negative, zipfile
        # seeks to them and gets EINVAL
        Path(str(self.dest) + ".sha256").unlink()
        self.dest.write_bytes(self.dest.read_bytes()[40:])
        for call in (cachebackup.verify, lambda path: cachebackup.restore(path, self.root / "cache2")):
            with self.assertRaises(ValueError) as caught:
                call(str(self.dest))
            self.assertIn("damaged", str(caught.exception))
        self.assertEqual(list((self.root / "cache2").rglob("*")), [])  # at most an empty folder

    def test_an_encrypted_manifest_is_refused_with_a_message(self):
        # a RuntimeError from zipfile ("password required") used to come out as it was
        Path(str(self.dest) + ".sha256").unlink()
        raw = bytearray(self.dest.read_bytes())
        for signature, offset in ((b"PK", 6), (b"PK", 8)):
            pos = raw.find(signature)
            while pos >= 0:
                name_at = pos + (30 if signature == b"PK" else 46)
                if bytes(raw[name_at:name_at + 19]) == b"cache_manifest.json":
                    raw[pos + offset] |= 1
                pos = raw.find(signature, pos + 4)
        self.dest.write_bytes(bytes(raw))
        for call in (cachebackup.verify, lambda path: cachebackup.restore(path, self.root / "cache2")):
            with self.assertRaises(ValueError) as caught:
                call(str(self.dest))
            self.assertIn("encrypted", str(caught.exception))

    def test_the_checksum_of_another_name_is_refused(self):
        other = self.out / "renamed.zip"
        self.dest.rename(other)
        Path(str(self.dest) + ".sha256").rename(str(other) + ".sha256")
        with self.assertRaises(ValueError):
            cachebackup.verify(str(other))

    def test_without_a_checksum_file_the_archive_still_verifies(self):
        Path(str(self.dest) + ".sha256").unlink()
        self.assertFalse(cachebackup.verify(str(self.dest)).sidecar)


# ----------------------------------------------------------------
class RestoreTests(Base):
    def setUp(self):
        super().setUp()
        self.real = make_cache(self.cache)
        cachebackup.create(self.cache, self.dest)
        self.fresh = self.root / "fresh"

    def test_round_trip_into_an_empty_cache_gives_the_same_bytes(self):
        res = cachebackup.restore(str(self.dest), self.fresh)
        self.assertEqual(tree(self.fresh), self.real)
        self.assertEqual((res.files, res.present, res.skipped), (len(self.real), 0, 0))
        self.assertEqual(sorted(p.name for p in self.fresh.iterdir()), ["anlz", "wave"])

    def test_it_works_when_the_cache_folder_does_not_exist_yet(self):
        self.assertFalse(self.fresh.exists())
        cachebackup.restore(str(self.dest), self.fresh / "deeper")
        self.assertEqual(tree(self.fresh / "deeper"), self.real)

    def test_an_existing_file_is_never_overwritten(self):
        mine = f"anlz/{KEYS[0]}.DAT"
        (self.fresh / "anlz").mkdir(parents=True)
        (self.fresh / mine).write_bytes(b"newer")
        res = cachebackup.restore(str(self.dest), self.fresh)
        self.assertEqual((self.fresh / mine).read_bytes(), b"newer")
        self.assertEqual((res.files, res.present), (len(self.real) - 1, 1))
        again = cachebackup.restore(str(self.dest), self.fresh)
        self.assertEqual((again.files, again.present), (0, len(self.real)))
        self.assertEqual((self.fresh / mine).read_bytes(), b"newer")

    def test_foreign_files_stay_and_no_work_folder_is_left(self):
        (self.fresh / "anlz").mkdir(parents=True)
        (self.fresh / "anlz" / "notes.txt").write_bytes(b"mine")
        cachebackup.restore(str(self.dest), self.fresh)
        self.assertEqual((self.fresh / "anlz" / "notes.txt").read_bytes(), b"mine")
        self.assertEqual(sorted(p.name for p in self.fresh.iterdir()), ["anlz", "wave"])

    def test_cancelled_restore_changes_nothing(self):
        stop = threading.Event()
        calls = []

        def progress(pct, message):
            calls.append(pct)
            if len(calls) == 3:
                stop.set()

        with self.assertRaises(Cancelled):
            cachebackup.restore(str(self.dest), self.fresh, progress, stop)
        self.assertEqual(list(self.fresh.rglob("*.*")) if self.fresh.exists() else [], [])
        if self.fresh.exists():
            self.assertEqual([p.name for p in self.fresh.iterdir()], [])

    def test_an_analysis_of_another_version_is_skipped_and_explained(self):
        old = self.out / "old.zip"
        with patch.object(cachebackup, "ANLZ_VERSION", ANLZ_VERSION - 1):
            cachebackup.create(self.cache, old)
        res = cachebackup.restore(str(old), self.fresh)
        anlz = [n for n in self.real if n.startswith("anlz/")]
        wave = [n for n in self.real if n.startswith("wave/")]
        self.assertEqual((res.skipped, res.files), (len(anlz), len(wave)))
        self.assertEqual(sorted(tree(self.fresh)), sorted(wave))
        self.assertIn(str(ANLZ_VERSION - 1), res.notes[0])
        self.assertIn(str(ANLZ_VERSION), res.notes[0])

    def test_without_a_checksum_file_it_restores_and_says_so(self):
        Path(str(self.dest) + ".sha256").unlink()
        res = cachebackup.restore(str(self.dest), self.fresh)
        self.assertFalse(res.sidecar)
        self.assertEqual(tree(self.fresh), self.real)

    def test_a_checksum_that_does_not_match_refuses_before_writing(self):
        Path(str(self.dest) + ".sha256").write_text(f"{'0' * 64} *analyses.zip\n")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            cachebackup.restore(str(self.dest), self.fresh)
        self.assertFalse(self.fresh.exists())

    def test_a_cache_folder_that_cannot_be_written_is_an_error_not_a_half_restore(self):
        self.fresh.mkdir()
        with patch("tempfile.mkdtemp", side_effect=PermissionError(13, "Permission denied")), \
                self.assertRaises(OSError):
            cachebackup.restore(str(self.dest), self.fresh)
        self.assertEqual(list(self.fresh.iterdir()), [])

    def test_a_crc_error_found_while_unpacking_leaves_the_cache_alone(self):
        Path(str(self.dest) + ".sha256").unlink()
        raw = bytearray(self.dest.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        self.dest.write_bytes(bytes(raw))
        self.fresh.mkdir()
        with self.assertRaises(ValueError):
            cachebackup.restore(str(self.dest), self.fresh)
        self.assertEqual(list(self.fresh.iterdir()), [])

# ----------------------------------------------------------------
# every case is refused before one byte reaches the cache folder, which is not even created
class HostileTests(Base):
    def refused(self, label, match=None):
        with self.subTest(label), self.assertRaisesRegex(ValueError, match or ""):
            cachebackup.restore(str(self.dest), self.cache)
        self.assertFalse(self.cache.exists(), label)

    def good(self, key=HOSTILE_KEY):
        return (f"anlz/{key}.DAT", b"data")

    def test_names_outside_the_layout(self):
        for name in (f"../anlz/{HOSTILE_KEY}.DAT", f"anlz/../wave/{HOSTILE_KEY}.bin", f"/anlz/{HOSTILE_KEY}.DAT",
                     f"C:/anlz/{HOSTILE_KEY}.DAT", f"C:anlz/{HOSTILE_KEY}.DAT", f"//server/share/{HOSTILE_KEY}.DAT",
                     "anlz/CON.DAT", "anlz/NUL", "wave/aux.bin", f"anlz/{HOSTILE_KEY}.DAT.", f"anlz/{HOSTILE_KEY}.DAT ",
                     f"anlz/{HOSTILE_KEY}.dat", f"anlz/{HOSTILE_KEY}.bin", f"wave/{HOSTILE_KEY}.DAT",
                     f"wave/{HOSTILE_KEY}.hi500", f"other/{HOSTILE_KEY}.DAT", f"{HOSTILE_KEY}.DAT",
                     f"anlz/sub/{HOSTILE_KEY}.DAT", f"anlz/{HOSTILE_KEY.upper()}.DAT", "anlz/readme.txt",
                     f"anlz/{HOSTILE_KEY}.DAT:stream", f"ANLZ/{HOSTILE_KEY}.DAT", "project.json",
                     "Audio/000001_song.mp3", f"anlz/{HOSTILE_KEY}\x00.DAT"):
            write_zip(self.dest, [self.good(KEYS[0]), (name, b"x")])
            self.refused(repr(name), "not a cache file")

    def test_a_directory_entry(self):
        write_zip(self.dest, [self.good(), ("anlz/", b"")])
        self.refused("directory")

    def test_a_backslash_trick(self):
        write_zip(self.dest, [(f"anlz/{HOSTILE_KEY}.DAT", b"data")])
        raw = self.dest.read_bytes()
        self.assertEqual(raw.count(f"anlz/{HOSTILE_KEY}".encode()), 2)
        self.dest.write_bytes(raw.replace(f"anlz/{HOSTILE_KEY}".encode(), f"anlz\\{HOSTILE_KEY}".encode()))
        self.refused("backslash")

    def test_the_same_name_twice(self):
        write_zip(self.dest, [self.good(), self.good()])
        self.refused("duplicate", "twice")

    def test_a_symbolic_link_entry(self):
        info = zipfile.ZipInfo(f"anlz/{HOSTILE_KEY}.DAT")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        write_zip(self.dest, [(info, b"../../target")])
        self.refused("symlink", "not a cache file")

    def test_a_device_or_other_special_file_entry(self):
        info = zipfile.ZipInfo(f"anlz/{HOSTILE_KEY}.DAT")
        info.external_attr = (stat.S_IFCHR | 0o666) << 16
        write_zip(self.dest, [(info, b"")])
        self.refused("device")

    def test_an_enormous_declared_size(self):
        write_zip(self.dest, [self.good()])
        patch_central(self.dest, f"anlz/{HOSTILE_KEY}.DAT", size=0x7FFFFFFF)
        self.refused("size", "too large")

    def test_a_compression_ratio_beyond_the_limit(self):
        write_zip(self.dest, [self.good()])
        patch_central(self.dest, f"anlz/{HOSTILE_KEY}.DAT", size=8 << 20, packed=1000)
        self.refused("ratio", "compressed beyond")

    def test_a_small_file_may_compress_well(self):
        # zero padding in a small ANLZ file is not a bomb
        with zipfile.ZipFile(self.dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"anlz/{HOSTILE_KEY}.DAT", b"\0" * 200_000)
            z.writestr(cachebackup.MANIFEST, json.dumps(note_for(1)))
        res = cachebackup.restore(str(self.dest), self.cache)
        self.assertEqual(res.files, 1)

    def test_a_total_size_beyond_the_limit(self):
        write_zip(self.dest, [self.good(KEYS[0]), self.good(KEYS[1]), self.good(KEYS[2])])
        with patch.object(cachebackup, "MAX_TOTAL", 5):
            self.refused("total", "room")

    def test_the_entry_count_is_read_before_the_entries_are_built(self):
        write_zip(self.dest, [self.good(k) for k in KEYS])
        with patch.object(cachebackup, "MAX_FILES", 2),                 patch.object(cachebackup.zipfile, "ZipFile", side_effect=AssertionError("opened")):
            self.refused("count", "too many")
        with patch.object(cachebackup, "MAX_DIRECTORY", 100),                 patch.object(cachebackup.zipfile, "ZipFile", side_effect=AssertionError("opened")):
            self.refused("directory size", "too many")

    def test_too_many_entries(self):
        write_zip(self.dest, [self.good(k) for k in KEYS] + [("wave/" + KEYS[0] + ".bin", b"x")])
        with patch.object(cachebackup, "MAX_FILES", 3):
            self.refused("count", "too many")

    def test_an_encrypted_entry(self):
        write_zip(self.dest, [self.good()])
        patch_central(self.dest, f"anlz/{HOSTILE_KEY}.DAT", flags=1)
        self.refused("encrypted", "encrypted")

    def test_the_count_in_a_zip64_directory_is_read_before_the_entries_are_built(self):
        # 66,000 entries need ZIP64 records, the limit must be seen there as well
        with zipfile.ZipFile(self.dest, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for i in range(66_000):
                z.writestr(f"wave/{i:040x}.bin", b"")
            z.writestr(cachebackup.MANIFEST, json.dumps(note_for(66_000)))
        # refused from the end of the file, before ZipFile builds an object per entry
        with patch.object(cachebackup, "MAX_FILES", 65_000), \
                patch.object(cachebackup.zipfile, "ZipFile", side_effect=AssertionError("opened")):
            self.refused("zip64 count", "too many")
        res = cachebackup.verify(str(self.dest))
        self.assertEqual(res.files, 66_000)

    def test_a_missing_manifest(self):
        write_zip(self.dest, [self.good()], note=None)
        self.refused("no manifest", "not a cache backup")

    def test_a_wrong_manifest(self):
        cases = {
            "not json": "{nope",
            "a list": "[1, 2]",
            "a number": "12",
            "other format": note_for(1, format="something-else"),
            "other version": note_for(1, format_version=2),
            "no version": {k: v for k, v in note_for(1).items() if k != "format_version"},
            "text anlz version": note_for(1, anlz_version="7"),
            "bool count": note_for(1, files=True),
            "negative count": note_for(1, files=-1),
            "wrong count": note_for(1, files=5),
            "huge manifest": "{" + " " * (70 << 10) + "}",
        }
        for label, note in cases.items():
            write_zip(self.dest, [self.good()], note=note)
            self.refused(label)

    def test_a_project_backup_is_not_a_cache_backup(self):
        with zipfile.ZipFile(self.dest, "w") as z:
            z.writestr("project.json", "{}")
            z.writestr("manifest.json", json.dumps({"sha256": {}}))
        self.refused("project backup", "not a cache backup")

    def test_files_that_are_not_zip_archives(self):
        for label, raw in (("text", b"hello"), ("empty", b""), ("pk only", b"PK\x05\x06" + b"\0" * 5)):
            self.dest.write_bytes(raw)
            self.refused(label)

    def test_the_manifest_alone_restores_nothing(self):
        write_zip(self.dest, [], note=note_for(0))
        res = cachebackup.restore(str(self.dest), self.cache)
        self.assertEqual((res.files, res.present, res.skipped), (0, 0, 0))

    def test_the_same_checks_apply_to_verify(self):
        write_zip(self.dest, [(f"../x/{HOSTILE_KEY}.DAT", b"x")])
        with self.assertRaises(ValueError):
            cachebackup.verify(str(self.dest))


class MoveInTests(Base):
    def test_a_file_that_appeared_meanwhile_is_not_replaced(self):
        src, dst = self.out / "src", self.out / "dst"
        src.write_bytes(b"restored")
        dst.write_bytes(b"already there")
        self.assertFalse(cachebackup.move_in(src, dst))
        self.assertEqual(dst.read_bytes(), b"already there")
        dst.unlink()
        self.assertTrue(cachebackup.move_in(src, dst))
        self.assertEqual((dst.read_bytes(), src.exists()), (b"restored", False))

# ----------------------------------------------------------------
class ClearTests(Base):
    def test_only_the_cache_files_are_deleted(self):
        real = make_cache(self.cache)
        res = cachebackup.clear(self.cache)
        self.assertEqual((res.files, res.failed), (len(real), []))
        self.assertEqual(sorted(tree(self.cache)), FOREIGN)

    def test_a_file_in_use_is_listed_and_the_others_go(self):
        real = make_cache(self.cache)
        stuck = str(self.cache / "anlz" / f"{KEYS[0]}.DAT")
        unlink = os.unlink

        def fail(path, *a, **kw):
            if str(path) == stuck:
                raise PermissionError(13, "Permission denied", stuck)
            return unlink(path, *a, **kw)

        with patch("os.unlink", fail):
            res = cachebackup.clear(self.cache)
        self.assertEqual((res.files, len(res.failed)), (len(real) - 1, 1))
        self.assertTrue(Path(stuck).exists())

    def test_a_missing_cache_is_fine(self):
        res = cachebackup.clear(self.cache)
        self.assertEqual((res.files, res.failed), (0, []))

    def test_stats_count_the_cache_files_only(self):
        real = make_cache(self.cache)
        self.assertEqual(cachebackup.stats(self.cache), (len(real), sum(len(v) for v in real.values())))
        self.assertEqual(cachebackup.stats(self.root / "missing"), (0, 0))

# ----------------------------------------------------------------
# The Options window
# ----------------------------------------------------------------

# ----------------------------------------------------------------
def wait_for(app, condition, seconds=15):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return condition()


# ------------------------------------------------------
# replaces exec of every message box: records the box and clicks the button named for its title
class Boxes:
    def __init__(self, **answers):
        self.answers = {k.replace("_", " "): v for k, v in answers.items()}
        self.seen = []

    def __enter__(self):
        self.patch = patch.object(QMessageBox, "exec", lambda box: self.run(box))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def run(self, box):
        labels = [b.text() for b in box.buttons()]
        self.seen.append((box.windowTitle(), box.text(), labels))
        want = self.answers.get(box.windowTitle())
        for b in box.buttons():
            if b.text() == want:
                b.click()
        return 0

    def titled(self, title):
        return [s for s in self.seen if s[0] == title]


# ----------------------------------------------------------------
class OptionsBase(Base):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        patcher = patch.object(dialogs, "cache_dir", return_value=self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = dict(settings.DEFAULTS)
        self.dlg = Options(self.cfg)
        self.addCleanup(self.finish)

    def finish(self):
        wait_for(self.app, lambda: not worker.has_active_jobs())
        self.dlg.deleteLater()

    def settle(self):
        self.assertTrue(wait_for(self.app, lambda: self.dlg.job is None and not worker.has_active_jobs()))
        self.app.processEvents()

    def fill(self, **kw):
        real = make_cache(self.cache, **kw)
        self.dlg.show_cache()
        return real

    def save_as(self, path):
        return patch.object(QFileDialog, "getSaveFileName", return_value=(str(path), "Zip (*.zip)"))

    def open_file(self, path):
        return patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "Zip (*.zip)"))


# ------------------------------------------------------
class OpenAndSizeTests(OptionsBase):
    def test_open_creates_the_folder_and_opens_a_local_url(self):
        self.assertFalse(self.cache.exists())
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.dlg.open_btn.click()
        self.assertTrue(self.cache.is_dir())
        url = opened.call_args[0][0]
        self.assertTrue(url.isLocalFile())
        self.assertEqual(Path(url.toLocalFile()), self.cache)

    def test_open_tells_when_the_folder_cannot_be_opened(self):
        with patch.object(QDesktopServices, "openUrl", return_value=False), Boxes() as boxes:
            self.dlg.open_btn.click()
        self.assertIn("could not be opened", boxes.seen[0][1])

    def test_open_tells_when_the_folder_cannot_be_created(self):
        blocker = self.root / "file"
        blocker.write_text("x")
        with patch.object(dialogs, "cache_dir", return_value=blocker / "inside"), \
                patch.object(QDesktopServices, "openUrl") as opened, Boxes() as boxes:
            self.dlg.open_btn.click()
        opened.assert_not_called()
        self.assertIn("cannot be created", boxes.seen[0][1])

    def test_size_line(self):
        self.assertEqual(self.dlg.cache_lbl.text(), "0 files, 0.0 MB")
        (self.cache / "anlz").mkdir(parents=True)
        (self.cache / "anlz" / f"{KEYS[0]}.DAT").write_bytes(b"x" * 1500)
        (self.cache / "anlz" / "notes.txt").write_bytes(b"x" * 5_000_000)
        self.dlg.show_cache()
        self.assertEqual(self.dlg.cache_lbl.text(), "1 file, 0.0 MB")
        (self.cache / "wave").mkdir()
        (self.cache / "wave" / f"{KEYS[0]}.hi1000").write_bytes(b"x" * 2_998_500)
        self.dlg.show_cache()
        self.assertEqual(self.dlg.cache_lbl.text(), "2 files, 3.0 MB")

    def test_buttons_are_disabled_when_the_cache_is_empty(self):
        for btn in (self.dlg.backup_btn, self.dlg.clear_btn):
            self.assertFalse(btn.isEnabled())
            self.assertIn("empty", btn.toolTip())
        self.assertTrue(self.dlg.open_btn.isEnabled())
        self.assertTrue(self.dlg.restore_btn.isEnabled())
        self.fill()
        for btn in (self.dlg.backup_btn, self.dlg.clear_btn):
            self.assertTrue(btn.isEnabled())
            self.assertNotIn("empty", btn.toolTip())

    def test_the_buttons_do_nothing_on_an_empty_cache_even_when_called(self):
        with self.save_as(self.dest) as ask, Boxes() as boxes:
            self.dlg.back_up()
            self.dlg.clear_cache()
        ask.assert_not_called()
        self.assertEqual(boxes.seen, [])

    def test_the_buttons_fit_in_the_window(self):
        self.fill()
        self.dlg.tabs.setCurrentIndex(1)
        self.dlg.show()
        self.app.processEvents()
        for btn in (self.dlg.open_btn, self.dlg.backup_btn, self.dlg.restore_btn, self.dlg.clear_btn):
            self.assertGreaterEqual(btn.width(), btn.sizeHint().width(), btn.text())
        self.assertGreaterEqual(self.dlg.cache_lbl.width(), self.dlg.cache_lbl.sizeHint().width())
        self.dlg.hide()


# ------------------------------------------------------
class BackUpTests(OptionsBase):
    def test_back_up_runs_the_job_and_offers_the_folder(self):
        real = self.fill()
        with self.save_as(self.dest) as ask, Boxes(Back_up_the_analyses="Open the folder") as boxes, \
                patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.dlg.backup_btn.click()
            self.settle()
        name, start, filt = ask.call_args[0][1], ask.call_args[0][2], ask.call_args[0][3]
        self.assertEqual((name, filt), ("Back up the analyses", "Zip (*.zip)"))
        self.assertEqual(Path(start).name, f"Boothbridge-analyses-{datetime.now().astimezone():%Y-%m-%d}.zip")
        self.assertEqual(cachebackup.verify(str(self.dest)).files, len(real))
        _title, text, labels = boxes.titled("Back up the analyses")[0]
        self.assertIn(str(self.dest), text)
        self.assertIn(f"{len(real)} files", text)
        self.assertIn("analyses.zip.sha256", text)
        self.assertIn("Open the folder", labels)
        self.assertEqual(Path(opened.call_args[0][0].toLocalFile()), self.out)
        # the cache is still there
        self.assertTrue(tree(self.cache).keys() >= real.keys())

    def test_the_next_save_dialog_starts_in_the_folder_of_the_last_backup(self):
        self.fill()
        with self.save_as(self.dest), Boxes():
            self.dlg.back_up()
            self.settle()
        self.assertEqual(self.cfg["cache_backup_dir"], str(self.out))
        with self.save_as("") as ask, Boxes():
            self.dlg.back_up()
        self.assertEqual(Path(ask.call_args[0][2]).parent, self.out)

    def test_a_cancelled_save_dialog_does_nothing(self):
        self.fill()
        with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")), Boxes() as boxes:
            self.dlg.back_up()
        self.assertIsNone(self.dlg.job)
        self.assertEqual(boxes.seen, [])
        self.assertEqual(self.leftovers(), [])

    def test_a_name_without_extension_gets_zip(self):
        self.fill()
        with self.save_as(self.out / "mine"), Boxes():
            self.dlg.back_up()
            self.settle()
        self.assertEqual(self.leftovers(), ["mine.zip", "mine.zip.sha256"])

    def test_files_that_could_not_be_read_are_reported(self):
        self.fill()
        res = cachebackup.Result(path=str(self.dest), files=7, size=10, failed=["anlz/x.DAT: Permission denied"])
        with self.save_as(self.dest), patch.object(cachebackup, "create", return_value=res), Boxes() as boxes:
            self.dlg.back_up()
            self.settle()
        text = boxes.titled("Back up the analyses")[0][1]
        self.assertIn("1 files could not be read", text)
        self.assertIn("anlz/x.DAT", text)

    def test_cancel_in_the_progress_dialog_stops_the_job_and_leaves_nothing(self):
        self.fill()
        started = threading.Event()

        def slow(cache, dest, progress=None, cancel=None):
            started.set()
            while not cancel.is_set():
                time.sleep(0.005)
            raise Cancelled()

        with self.save_as(self.dest), patch.object(cachebackup, "create", slow), Boxes() as boxes:
            self.dlg.back_up()
            self.assertTrue(wait_for(self.app, started.is_set))
            self.assertTrue(worker.has_active_jobs())
            bar = self.dlg.findChild(QProgressDialog)
            self.assertIsNotNone(bar)
            self.assertFalse(self.dlg.backup_btn.isEnabled())
            bar.findChild(QPushButton).click()
            self.settle()
        self.assertIn("Cancelled", boxes.titled("Back up the analyses")[0][1])
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_backup_tells_why(self):
        self.fill()
        with self.save_as(self.dest), patch.object(cachebackup, "create", side_effect=OSError("disk full")), \
                Boxes() as boxes:
            self.dlg.back_up()
            self.settle()
        self.assertIn("disk full", boxes.titled("Back up the analyses")[0][1])
        self.assertTrue(self.dlg.backup_btn.isEnabled())

    def test_the_text_of_a_message_is_plain(self):
        self.fill()
        res = cachebackup.Result(path=str(self.dest), files=1, size=1, failed=["<b>x</b>\nanlz/a.DAT: no"])
        seen = []
        with patch.object(QMessageBox, "exec", lambda b: seen.append(b.textFormat())), \
                self.save_as(self.dest), patch.object(cachebackup, "create", return_value=res):
            self.dlg.back_up()
            self.settle()
        self.assertEqual(seen, [dialogs.Qt.PlainText])


# ----------------------------------------------------------------
class ClearQuestionTests(OptionsBase):
    TITLE = "Clear analysis cache"

    def test_the_question_offers_three_choices(self):
        self.fill()
        with Boxes() as boxes:
            self.dlg.clear_btn.click()
        title, text, labels = boxes.seen[0]
        self.assertEqual(title, self.TITLE)
        self.assertTrue(text.startswith("Back up the analyses before clearing them?"))
        self.assertEqual(sorted(labels), sorted(["Back up, then clear", "Clear without a backup", "Cancel"]))

    def test_cancel_clears_nothing(self):
        real = self.fill()
        with Boxes(Clear_analysis_cache="Cancel"), self.save_as(self.dest) as ask:
            self.dlg.clear_btn.click()
        ask.assert_not_called()
        self.assertTrue(real.keys() <= tree(self.cache).keys())
        self.assertIsNone(self.dlg.job)

    def test_closing_the_question_clears_nothing(self):
        real = self.fill()
        with Boxes():
            self.dlg.clear_btn.click()
        self.assertTrue(real.keys() <= tree(self.cache).keys())

    def test_clear_without_a_backup_clears_the_cache_files_only(self):
        self.fill()
        with Boxes(Clear_analysis_cache="Clear without a backup"), self.save_as(self.dest) as ask:
            self.dlg.clear_btn.click()
            self.settle()
        ask.assert_not_called()
        self.assertEqual(sorted(tree(self.cache)), FOREIGN)
        self.assertEqual(self.dlg.cache_lbl.text(), "0 files, 0.0 MB")
        self.assertFalse(self.dlg.clear_btn.isEnabled())

    def test_back_up_then_clear_makes_the_backup_first(self):
        real = self.fill()
        with Boxes(Clear_analysis_cache="Back up, then clear") as boxes, self.save_as(self.dest):
            self.dlg.clear_btn.click()
            self.settle()
        self.assertEqual(cachebackup.verify(str(self.dest)).files, len(real))
        self.assertEqual(cachebackup.stats(self.cache), (0, 0))
        text = boxes.titled(self.TITLE)[-1][1]
        self.assertIn(str(self.dest), text)
        self.assertIn(f"{len(real)} files deleted", text)
        # the backup can bring everything back
        cachebackup.restore(str(self.dest), self.cache)
        self.assertTrue(real.items() <= tree(self.cache).items())

    def test_a_cancelled_save_dialog_clears_nothing_and_says_so(self):
        real = self.fill()
        with Boxes(Clear_analysis_cache="Back up, then clear") as boxes, \
                patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            self.dlg.clear_btn.click()
        self.assertTrue(real.keys() <= tree(self.cache).keys())
        self.assertIn("nothing was cleared", boxes.seen[-1][1])
        self.assertIsNone(self.dlg.job)

    def test_a_failed_backup_clears_nothing(self):
        real = self.fill()
        with Boxes(Clear_analysis_cache="Back up, then clear") as boxes, self.save_as(self.dest), \
                patch.object(cachebackup, "create", side_effect=OSError("disk full")):
            self.dlg.clear_btn.click()
            self.settle()
        self.assertTrue(real.keys() <= tree(self.cache).keys())
        text = boxes.seen[-1][1]
        self.assertIn("disk full", text)
        self.assertIn("not cleared", text)

    def test_a_cancelled_backup_clears_nothing(self):
        real = self.fill()

        def cancelled(cache, dest, progress=None, cancel=None):
            raise Cancelled()

        with Boxes(Clear_analysis_cache="Back up, then clear") as boxes, self.save_as(self.dest), \
                patch.object(cachebackup, "create", cancelled):
            self.dlg.clear_btn.click()
            self.settle()
        self.assertTrue(real.keys() <= tree(self.cache).keys())
        self.assertIn("not cleared", boxes.seen[-1][1])

    def test_a_backup_that_missed_a_file_clears_nothing(self):
        real = self.fill()
        res = cachebackup.Result(path=str(self.dest), files=7, size=10, failed=["anlz/x.DAT: Permission denied"])
        with Boxes(Clear_analysis_cache="Back up, then clear") as boxes, self.save_as(self.dest), \
                patch.object(cachebackup, "create", return_value=res):
            self.dlg.clear_btn.click()
            self.settle()
        self.assertTrue(real.keys() <= tree(self.cache).keys())
        self.assertIn("not cleared", boxes.seen[-1][1])

    def test_files_that_cannot_be_deleted_are_reported(self):
        self.fill()
        res = cachebackup.Result(files=3, failed=["anlz/x.DAT: Permission denied"])
        with Boxes(Clear_analysis_cache="Clear without a backup") as boxes, \
                patch.object(cachebackup, "clear", return_value=res):
            self.dlg.clear_btn.click()
            self.settle()
        self.assertIn("1 files could not be deleted", boxes.seen[-1][1])


# ----------------------------------------------------------------
class RestoreButtonTests(OptionsBase):
    TITLE = "Restore analyses"

    def backup(self):
        real = make_cache(self.cache)
        cachebackup.create(self.cache, self.dest)
        cachebackup.clear(self.cache)
        self.dlg.show_cache()
        return real

    def test_restore_reports_the_three_counts(self):
        real = self.backup()
        mine = self.cache / "anlz" / f"{KEYS[0]}.DAT"
        mine.write_bytes(b"newer")
        with self.open_file(self.dest), Boxes() as boxes:
            self.dlg.restore_btn.click()
            self.settle()
        text = boxes.titled(self.TITLE)[0][1]
        self.assertIn(f"{len(real) - 1} restored, 1 already there, 0 skipped.", text)
        self.assertEqual(mine.read_bytes(), b"newer")
        self.assertEqual(self.dlg.cache_lbl.text().split(",")[0], f"{len(real)} files")


    def test_skipped_entries_are_explained(self):
        real = make_cache(self.cache)
        with patch.object(cachebackup, "ANLZ_VERSION", ANLZ_VERSION - 1):
            cachebackup.create(self.cache, self.dest)
        cachebackup.clear(self.cache)
        with self.open_file(self.dest), Boxes() as boxes:
            self.dlg.restore()
            self.settle()
        text = boxes.titled(self.TITLE)[0][1]
        n = len([k for k in real if k.startswith("anlz/")])
        self.assertIn(f"{n} skipped", text)
        self.assertIn("calculated again", text)

    def test_a_missing_checksum_file_asks_first(self):
        self.backup()
        Path(str(self.dest) + ".sha256").unlink()
        with self.open_file(self.dest), Boxes(Restore_analyses="Cancel") as boxes:
            self.dlg.restore()
        self.assertIsNone(self.dlg.job)
        self.assertEqual(cachebackup.stats(self.cache), (0, 0))
        self.assertIn("no .sha256 file", boxes.seen[0][1])
        with self.open_file(self.dest), Boxes(Restore_analyses="Restore anyway"):
            self.dlg.restore()
            self.settle()
        self.assertEqual(cachebackup.stats(self.cache)[0], 8)

    def test_a_checksum_that_does_not_match_is_refused(self):
        self.backup()
        Path(str(self.dest) + ".sha256").write_text(f"{'1' * 64} *analyses.zip\n")
        with self.open_file(self.dest), Boxes() as boxes:
            self.dlg.restore()
            self.settle()
        self.assertEqual(cachebackup.stats(self.cache), (0, 0))
        self.assertIn("mismatch", boxes.titled(self.TITLE)[0][1])
        self.assertIn("No file of the cache was replaced", boxes.titled(self.TITLE)[0][1])

    def test_a_zip_that_is_not_a_cache_backup_is_refused(self):
        other = self.out / "project.zip"
        with zipfile.ZipFile(other, "w") as z:
            z.writestr("project.json", "{}")
        with self.open_file(other), Boxes(Restore_analyses="Restore anyway") as boxes:
            self.dlg.restore()
            self.settle()
        self.assertIn("not a cache backup", boxes.titled(self.TITLE)[-1][1])
        self.assertFalse(self.cache.exists())

    def test_a_cancelled_open_dialog_does_nothing(self):
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")), Boxes() as boxes:
            self.dlg.restore()
        self.assertEqual(boxes.seen, [])
        self.assertIsNone(self.dlg.job)


# ----------------------------------------------------------------
class HelpTests(unittest.TestCase):
    def test_the_help_describes_the_cache(self):
        for part in ("<h3>Analysis cache</h3>", "%APPDATA%\\BSM\\Boothbridge\\cache",
                     "Back up the analyses...", "Restore analyses...", "Clear analysis cache",
                     "only costs analysis time", "never replaces a file already there"):
            self.assertIn(part, USAGE)

    def test_the_help_has_no_long_dashes(self):
        self.assertNotIn("\u2014", USAGE)
        self.assertNotIn("\u2013", USAGE)


if __name__ == "__main__":
    unittest.main()
