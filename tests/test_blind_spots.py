# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks collections written oddly by other programs and files that Windows briefly refuses to open.

import ctypes
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from djapp import sources
from djapp.export import files, manifest
from djapp.export.cdj import pdbwrite, validate
from djapp.model import Node, Track

HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'


def nml(title="T", playlist_key="C:/:Music/:a.mp3", folders=0):
    entry = (f'<ENTRY TITLE="{title}" ARTIST="A"><LOCATION DIR="/:Music/:" FILE="a.mp3" VOLUME="C:"/>'
             '<INFO BITRATE="320000" PLAYTIME="200"/><TEMPO BPM="128"/></ENTRY>')
    leaf = ('<NODE TYPE="PLAYLIST" NAME="P"><PLAYLIST UUID="u">'
            f'<ENTRY><PRIMARYKEY KEY="{playlist_key}"/></ENTRY></PLAYLIST></NODE>')
    for k in range(folders):
        leaf = f'<NODE TYPE="FOLDER" NAME="f{k}"><SUBNODES COUNT="1">{leaf}</SUBNODES></NODE>'
    return (f'{HEAD}<NML VERSION="19"><COLLECTION ENTRIES="1">{entry}</COLLECTION>'
            f'<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">{leaf}</SUBNODES></NODE></PLAYLISTS></NML>')


# --------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load(self, text):
        path = self.root / "collection.nml"
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return sources.load(str(path))

class OddCollectionTests(Base):
    def test_a_reference_to_a_character_xml_forbids_does_not_refuse_the_collection(self):
        for reference in ("&#0;", "&#1;", "&#xD800;", "&#xFFFF;", "&#99999999999999999999999;"):
            nodes = self.load(nml(title=f"a{reference}b"))
            self.assertEqual(nodes[0].children[0].tracks[0].title if nodes[0].children else nodes[0].tracks[0].title,
                             "ab", reference)

    def test_references_to_legal_characters_are_kept_next_to_an_illegal_one(self):
        # the illegal reference sends the file to the fallback, the legal ones must come out of it unchanged
        nodes = self.load(nml(title="a&#0;&#233;&#x41;&#x1F600;&#10;b"))
        track = nodes[0].tracks[0] if nodes[0].tracks else nodes[0].children[0].tracks[0]
        self.assertEqual(track.title, "a\u00e9A\U0001F600\nb")

    def test_damaged_xml_is_reported_as_a_bad_file_not_as_a_parser_error(self):
        for data in (b"", b"hello", nml().encode()[:300]):
            with self.assertRaisesRegex(ValueError, "not a valid XML file"):
                self.load(data)

    def test_folders_nested_beyond_reason_are_refused_with_a_reason(self):
        with self.assertRaisesRegex(ValueError, "too deeply"):
            self.load(nml(folders=6000))
        self.assertTrue(self.load(nml(folders=200)))

    def test_entities_are_refused_with_a_reason(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "x">]><NML><COLLECTION><ENTRY TITLE="&a;"/></COLLECTION></NML>'
        with self.assertRaisesRegex(ValueError, "entities"):
            self.load(bomb)

# A file Windows refuses for a moment must not stop an export
@unittest.skipUnless(os.name == "nt", "the error codes are Windows ones")
class ShortLockTests(Base):
    def refuse(self, times, real, winerror=32):
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= times:
                raise OSError(13, "The process cannot access the file because it is being used by another process",
                              None, winerror)
            return real(*args, **kwargs)

        return flaky, calls

    def test_replace_waits_for_a_lock_that_ends(self):
        src, dst = self.root / "a", self.root / "b"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        flaky, calls = self.refuse(3, os.replace)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch("os.replace", flaky):
            manifest.replace(str(src), str(dst))
        self.assertEqual(dst.read_bytes(), b"new")
        self.assertEqual(calls["n"], 4)

    def test_replace_gives_up_when_the_lock_does_not_end(self):
        src, dst = self.root / "a", self.root / "b"
        src.write_bytes(b"new")
        flaky, calls = self.refuse(10 ** 6, os.replace)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch("os.replace", flaky), self.assertRaises(PermissionError):
            manifest.replace(str(src), str(dst))
        self.assertEqual(calls["n"], manifest.LOCK_TRIES)

    def test_a_cancel_ends_the_wait(self):
        stop = threading.Event()
        stop.set()
        flaky, _ = self.refuse(10 ** 6, os.replace)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch("os.replace", flaky), self.assertRaises(manifest.Cancelled):
            manifest.replace("a", "b", stop)

    def test_access_denied_and_lock_violation_are_waited_for_too(self):
        for code in (5, 32, 33):
            src, dst = self.root / "a", self.root / "b"
            src.write_bytes(b"new")
            flaky, calls = self.refuse(2, os.replace, code)
            with patch.object(manifest, "LOCK_PAUSE", 0), patch("os.replace", flaky):
                manifest.replace(str(src), str(dst))
            self.assertEqual(calls["n"], 3, code)

    def test_a_refusal_that_is_not_a_lock_is_not_waited_for(self):
        calls = {"n": 0}

        def refused(*args):
            calls["n"] += 1
            raise OSError(13, "A required privilege is not held by the client", None, 1314)

        with patch("os.replace", refused), self.assertRaises(OSError):
            manifest.replace("a", "b")
        self.assertEqual(calls["n"], 1)

    def test_open_reports_a_lock_with_an_errno_and_no_windows_code(self):
        path = self.root / "locked.json"
        path.write_text("{}")
        handle = ctypes.windll.kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        threading.Timer(0.5, ctypes.windll.kernel32.CloseHandle, args=(handle,)).start()
        with self.assertRaises(PermissionError) as seen:
            path.read_bytes()
        self.assertTrue(manifest.busy(seen.exception))
        started = time.monotonic()
        self.assertEqual(manifest.settle(path.read_bytes), b"{}")
        self.assertGreater(time.monotonic() - started, 0.1)  # it really waited for the lock to end


    def test_a_read_only_destination_fails_at_once(self):
        src, dst = self.root / "a", self.root / "b"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        os.chmod(dst, 0o444)
        self.addCleanup(os.chmod, dst, 0o666)
        started = time.monotonic()
        with patch.object(manifest, "LOCK_PAUSE", 5), self.assertRaises(PermissionError):
            manifest.replace(str(src), str(dst))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(dst.read_bytes(), b"old")

    def test_a_folder_in_the_way_fails_at_once(self):
        src, dst = self.root / "a", self.root / "b"
        src.write_bytes(b"new")
        dst.mkdir()
        started = time.monotonic()
        with patch.object(manifest, "LOCK_PAUSE", 5), self.assertRaises(PermissionError):
            manifest.replace(str(src), str(dst))
        self.assertLess(time.monotonic() - started, 2)

    def test_a_failed_read_back_is_what_the_caller_sees_even_when_the_leftover_is_locked(self):
        target = self.root / "x.pdb"
        locked, _ = self.refuse(10 ** 6, os.unlink)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch.object(validate, "sha256_drive", lambda path: "wrong"), \
                patch("os.unlink", locked), self.assertRaisesRegex(OSError, "read-back mismatch"):
            validate.write_verified(str(target), b"data", lambda data: None)

    def test_other_errors_are_not_retried(self):
        calls = {"n": 0}

        def broken(*args):
            calls["n"] += 1
            raise FileNotFoundError(2, "gone")

        with patch("os.replace", broken), self.assertRaises(FileNotFoundError):
            manifest.replace("a", "b")
        self.assertEqual(calls["n"], 1)

    def test_a_verified_write_survives_a_lock_on_the_read_back(self):
        track = Track(title="T", artist="A", path="/Contents/a.mp3", duration=10.0, bitrate=320, samplerate=44100)
        item = pdbwrite.Item(track, 1, "/Contents/a.mp3", "/PIONEER/USBANLZ/P000/00000001/ANLZ0000.DAT", 0)
        pdbwrite.write([item], [Node(kind="playlist", name="Set", tracks=[track])], self.root / "export.pdb")
        data = (self.root / "export.pdb").read_bytes()
        target = self.root / "again.pdb"
        flaky, calls = self.refuse(2, manifest.sha256_drive)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch.object(validate, "sha256_drive", flaky):
            validate.write_verified(str(target), data, validate.pdb)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(calls["n"], 3)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith((".verified", ".tmp"))], [])

    def test_an_audio_copy_survives_a_lock_on_the_part_file(self):
        src = self.root / "in.wav"
        src.write_bytes(b"RIFF" + bytes(2000))
        dst = self.root / "out" / "in.wav"
        dst.parent.mkdir()
        flaky, calls = self.refuse(3, os.replace)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch("os.replace", flaky):
            files.copy_one(str(src), str(dst), False)
        self.assertEqual(dst.read_bytes(), src.read_bytes())
        self.assertEqual(calls["n"], 4)
        self.assertEqual([p.name for p in dst.parent.iterdir()], ["in.wav"])

    def test_verifying_a_key_waits_for_a_file_that_is_locked_for_a_moment(self):
        (self.root / "a.bin").write_bytes(b"data")
        man = manifest.Manifest(str(self.root))
        man.add(str(self.root / "a.bin"), manifest.sha256_bytes(b"data"))
        man.save()
        flaky, calls = self.refuse(2, manifest.sha256_drive)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch.object(manifest, "sha256_drive", flaky):
            result = manifest.verify(str(self.root))
        self.assertEqual((result.ok, result.damaged), (1, []))
        self.assertEqual(calls["n"], 3)

    def test_the_checksum_file_is_read_again_when_it_is_locked_for_a_moment(self):
        (self.root / "a.bin").write_bytes(b"data")
        man = manifest.Manifest(str(self.root))
        man.add(str(self.root / "a.bin"), manifest.sha256_bytes(b"data"))
        man.save()
        flaky, _ = self.refuse(2, manifest.read)
        with patch.object(manifest, "LOCK_PAUSE", 0), patch.object(manifest, "read", flaky):
            result = manifest.verify(str(self.root))
        self.assertEqual((result.error, result.ok), ("", 1))


if __name__ == "__main__":
    unittest.main()
