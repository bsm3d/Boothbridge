import copy
import hashlib
import json
import os
import struct
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from djapp import backup
from djapp.export.manifest import Cancelled
from djapp.model import LOOP, Cue, Node, Track
from djapp.sources import project


# ----------------------------------------------------------------
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "music" / "same.mp3"
        self.source.parent.mkdir()
        self.source.write_bytes(b"original audio and tags")
        self.track = Track(title="Edited title", artist="Artist", path=str(self.source),
                           bpm=123.456789, grid=17.125, duration=300,
                           cues=[Cue(kind=LOOP, hotcue=2, start=3000, length=2000)])
        self.nodes = [Node("folder", "Sets", children=[
            Node("playlist", "First/Set", tracks=[self.track, self.track]),
            Node("playlist", "First/Set", tracks=[self.track]),
            Node("playlist", "Empty"),
        ])]
        self.doc = project.dump(self.nodes, {"music_root": str(self.source.parent),
                                            "source_path": "old.nml", "output_path": "old-drive"})
        self.path = self.root / "backup.zip"

    def test_portable_restore_preserves_all_playlists_shared_tracks_and_metadata(self):
        original = copy.deepcopy(self.doc)
        result = backup.create(str(self.path), self.doc)
        self.assertEqual((result["audio"], result["playlists"]), (1, 3))
        checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(result["sha256"], checksum)
        self.assertEqual(Path(result["checksum_path"]).read_text(encoding="utf-8"),
                         f"{checksum} *backup.zip\n")
        self.assertEqual(self.doc, original)
        extracted = self.root / "restored"
        with zipfile.ZipFile(self.path) as archive:
            self.assertIsNone(archive.testzip())
            archive.extractall(extracted)
            audio = [name for name in archive.namelist() if name.startswith("Audio/")]
            self.assertEqual(len(audio), 1)
            self.assertTrue(audio[0].endswith(".mp3"))
            self.assertEqual(archive.read(audio[0]), self.source.read_bytes())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["sha256"][audio[0]], hashlib.sha256(self.source.read_bytes()).hexdigest())
            playlists = [name for name in archive.namelist() if name.endswith(".m3u8")]
            self.assertEqual(len(playlists), 3)
            lines = archive.read(playlists[0]).decode().splitlines()
            self.assertEqual(lines[-1], "../" + audio[0])
        nodes, cfg = project.read(str(extracted / "project.json"))
        first, second, empty = nodes[0].children
        restored = first.tracks[0]
        self.assertIs(restored, first.tracks[1])
        self.assertIs(restored, second.tracks[0])
        self.assertEqual(empty.tracks, [])
        self.assertEqual(restored.title, "Edited title")
        self.assertEqual((restored.bpm, restored.grid), (123.456789, 17.125))
        self.assertEqual(restored.cues[0], self.track.cues[0])
        self.assertEqual(Path(restored.path).read_bytes(), self.source.read_bytes())
        self.assertEqual(cfg["music_root"], str(extracted / "Audio"))
        self.assertEqual((cfg["source_path"], cfg["output_path"]), ("", ""))

    def test_same_basenames_and_distinct_tracks_with_same_source(self):
        other = self.root / "other" / "same.mp3"
        other.parent.mkdir()
        other.write_bytes(b"different audio")
        tracks = [self.track, Track(path=str(self.source), title="Different metadata"), Track(path=str(other))]
        doc = project.dump([Node("playlist", "Set", tracks=tracks)], {})
        result = backup.create(str(self.path), doc)
        self.assertEqual(result["audio"], 2)
        with zipfile.ZipFile(self.path) as archive:
            saved = json.loads(archive.read("project.json"))
            paths = [track["path"] for track in saved["tracks"]]
            self.assertEqual(paths[0], paths[1])
            self.assertNotEqual(paths[0], paths[2])

    def test_relocated_source_is_found_and_missing_audio_prevents_partial_backup(self):
        self.doc["tracks"][0]["path"] = str(self.root / "old" / "same.mp3")
        backup.create(str(self.path), self.doc)
        previous = self.path.read_bytes()
        self.doc["tracks"][0]["path"] = "missing.mp3"
        with self.assertRaisesRegex(FileNotFoundError, "missing audio"):
            backup.create(str(self.path), self.doc)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertFalse(list(self.root.glob(".bb-backup-*")))

    def test_cancellation_copy_verification_and_replace_errors_preserve_destination(self):
        for mode in ("cancel-copy", "cancel-verify", "replace-error", "corrupt"):
            with self.subTest(mode=mode):
                self.path.write_bytes(b"previous backup")
                cancel = threading.Event()

                def progress(_pct, message, mode=mode, cancel=cancel):
                    if (mode == "cancel-copy" and message.startswith("Backing up")) or (
                        mode == "cancel-verify" and message.startswith("Verifying")
                    ):
                        cancel.set()

                context = patch.object(backup.os, "replace", side_effect=PermissionError("access denied")) \
                    if mode == "replace-error" else patch.object(
                        backup.hashlib, "sha256", wraps=hashlib.sha256)
                if mode == "corrupt":
                    real_open = zipfile.ZipFile.open

                    def corrupt(archive, name, mode="r", *args, real_open=real_open, **kwargs):
                        if mode == "r":
                            raise zipfile.BadZipFile("CRC mismatch")
                        return real_open(archive, name, mode, *args, **kwargs)

                    context = patch.object(zipfile.ZipFile, "open", new=corrupt)
                with context, self.assertRaises((Cancelled, OSError, zipfile.BadZipFile)):
                    backup.create(str(self.path), self.doc, progress=progress, cancel=cancel)
                self.assertEqual(self.path.read_bytes(), b"previous backup")
                self.assertFalse(list(self.root.glob(".bb-backup-*")))

    def test_audio_modified_while_copying_is_rejected(self):
        def progress(_pct, message):
            if message.startswith("Backing up"):
                self.source.write_bytes(b"changed by another application")

        with self.assertRaisesRegex(OSError, "Audio changed"):
            backup.create(str(self.path), self.doc, progress=progress)
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.root.glob(".bb-backup-*")))

    @unittest.skipUnless(os.name == "nt", "Windows legacy stat/fstat ctime difference")
    def test_windows_handle_ctime_difference_does_not_reject_unchanged_audio(self):
        real_fstat = os.fstat

        def handle_stat(fd):
            stat = real_fstat(fd)
            return SimpleNamespace(st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns,
                                   st_ctime_ns=stat.st_ctime_ns + 1, st_ino=stat.st_ino)

        with patch.object(backup.os, "fstat", side_effect=handle_stat):
            self.assertEqual(backup.create(str(self.path), self.doc)["audio"], 1)


    def test_empty_playlist_and_unsafe_names_are_valid(self):
        doc = project.dump([Node("playlist", "../CON:\n")], {})
        backup.create(str(self.path), doc)
        with zipfile.ZipFile(self.path) as archive:
            self.assertTrue(all(".." not in name and "\n" not in name for name in archive.namelist()))
            self.assertEqual(len(archive.namelist()), 3)

    def test_destination_cannot_overwrite_audio(self):
        with self.assertRaisesRegex(ValueError, "source audio"):
            backup.create(str(self.source), self.doc)
        self.assertEqual(self.source.read_bytes(), b"original audio and tags")

    def test_checksum_write_failure_reports_that_archive_was_created(self):
        with patch.object(backup, "write_atomic", side_effect=PermissionError("access denied")), \
                self.assertRaisesRegex(OSError, "ZIP was created.*SHA-256"):
            backup.create(str(self.path), self.doc)
        with zipfile.ZipFile(self.path) as archive:
            self.assertIsNone(archive.testzip())

    def test_relative_paths_resolve_from_project_not_working_directory(self):
        local = self.root / "saved"
        local.mkdir()
        audio = local / "song.wav"
        audio.write_bytes(b"audio")
        project.save(str(local / "project.json"),
                     [Node("playlist", "Local", tracks=[Track(path="song.wav")])], {"music_root": "."})
        nodes, cfg = project.read(str(local / "project.json"))
        self.assertEqual(nodes[0].tracks[0].path, str(audio))
        self.assertEqual(cfg["music_root"], str(local))
        self.assertNotEqual(os.getcwd(), str(local))

    def test_verify_and_restore_complete_backup(self):
        result = backup.create(str(self.path), self.doc)
        self.assertEqual(backup.verify(str(self.path)), result["sha256"])
        target = self.root / "restored"
        restored_path = backup.restore(str(self.path), str(target))
        nodes, _cfg = project.read(restored_path)
        track = nodes[0].children[0].tracks[0]
        self.assertEqual(Path(track.path).read_bytes(), self.source.read_bytes())
        self.assertEqual(track.cues, self.track.cues)
        self.assertFalse(list(self.root.glob(".bb-restore-*")))

    def test_corrupt_archive_missing_checksum_and_existing_destination_rejected(self):
        backup.create(str(self.path), self.doc)
        target = self.root / "restored"
        target.mkdir()
        with self.assertRaises(FileExistsError):
            backup.restore(str(self.path), str(target))
        self.path.write_bytes(self.path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            backup.verify(str(self.path))
        with self.assertRaisesRegex(ValueError, "Invalid or multi-volume ZIP directory"):
            backup.restore(str(self.path), str(self.root / "new"))
        self.assertFalse((self.root / "new").exists())
        Path(str(self.path) + ".sha256").unlink()
        with self.assertRaises(FileNotFoundError):
            backup.verify(str(self.path))

    def test_restore_cancellation_removes_staging(self):
        backup.create(str(self.path), self.doc)
        cancel = threading.Event()

        def progress(_pct, message):
            if message.startswith("Restoring"):
                cancel.set()

        with self.assertRaises(Cancelled):
            backup.restore(str(self.path), str(self.root / "restored"), progress=progress, cancel=cancel)
        self.assertFalse((self.root / "restored").exists())
        self.assertFalse(list(self.root.glob(".bb-restore-*")))

    def test_unsafe_zip_entry_rejected_even_with_matching_checksum(self):
        backup.create(str(self.path), self.doc)
        with zipfile.ZipFile(self.path, "a") as archive:
            archive.writestr("../escape.txt", "bad")
        checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        Path(str(self.path) + ".sha256").write_text(f"{checksum} *backup.zip\n")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            backup.restore(str(self.path), str(self.root / "restored"))
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse(list(self.root.glob(".bb-restore-*")))


    # writes data as the archive and a checksum that matches it, so that only its content is judged
    def reseal(self, data: bytes):
        self.path.write_bytes(data)
        Path(str(self.path) + ".sha256").write_text(f"{hashlib.sha256(data).hexdigest()} *backup.zip\n")

    def test_seeds_91_and_114_an_archive_without_project_json_is_refused_with_a_message(self):
        backup.create(str(self.path), self.doc)
        with zipfile.ZipFile(self.path) as archive:
            kept = {name: archive.read(name) for name in archive.namelist() if name != "project.json"}
        self.assertTrue(any(name.startswith("Audio/") for name in kept))
        with zipfile.ZipFile(self.path, "w") as archive:
            for name, data in kept.items():
                archive.writestr(name, data)
        self.reseal(self.path.read_bytes())
        before = sorted(self.root.rglob("*"))
        with self.assertRaisesRegex(ValueError, "project.json is missing"):
            backup.restore(str(self.path), str(self.root / "restored"))
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_an_archive_without_manifest_json_is_refused_with_a_message(self):
        backup.create(str(self.path), self.doc)
        with zipfile.ZipFile(self.path) as archive:
            kept = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
        with zipfile.ZipFile(self.path, "w") as archive:
            for name, data in kept.items():
                archive.writestr(name, data)
        self.reseal(self.path.read_bytes())
        before = sorted(self.root.rglob("*"))
        with self.assertRaisesRegex(ValueError, "manifest.json is missing"):
            backup.restore(str(self.path), str(self.root / "restored"))
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_seed_1439_a_zip_version_python_cannot_read_is_a_value_error(self):
        backup.create(str(self.path), self.doc)
        data = bytearray(self.path.read_bytes())
        at = data.find(b"PK\x01\x02")
        while at >= 0:
            data[at + 6:at + 8] = struct.pack("<H", 148)  # version needed to extract, 14.8
            at = data.find(b"PK\x01\x02", at + 4)
        self.reseal(bytes(data))
        before = sorted(self.root.rglob("*"))
        with self.assertRaisesRegex(ValueError, "ZIP version"):
            backup.restore(str(self.path), str(self.root / "restored"))
        self.assertEqual(sorted(self.root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
