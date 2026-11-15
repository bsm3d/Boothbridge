import hashlib
import json
import os
import stat
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from defusedxml.common import DefusedXmlException
from djapp import backup
from djapp.model import Node, Track
from djapp.sources import nml, project, rbxml, vdj


# ------------------------------------------------------------
class UntrustedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "backup.zip"
        self.target = self.root / "restored"

    def seal(self):
        checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        Path(str(self.archive) + ".sha256").write_text(f"{checksum} *{self.archive.name}\n")

    def archive_with(self, extra=(), audio=b"audio", compressed=False, track_path="Audio/test.wav"):
        doc = project.dump([Node(kind="playlist", name="Test", tracks=[Track(path=track_path)])], {})
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("project.json", json.dumps(doc))
            archive.writestr("manifest.json", json.dumps(
                {"sha256": {"Audio/test.wav": hashlib.sha256(audio).hexdigest()}}))
            archive.writestr("Audio/test.wav", audio,
                             compress_type=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED)
            for name, content in extra:
                entry = zipfile.ZipInfo("placeholder")
                entry.filename = entry.orig_filename = name
                archive.writestr(entry, content)
        self.seal()

    def rejected(self, text):
        with self.assertRaisesRegex((ValueError, OSError, zipfile.BadZipFile), text):
            backup.restore(str(self.archive), str(self.target))
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".bb-restore-*")))

    def test_valid_backup_create_restore_roundtrip(self):
        audio = self.root / "track.wav"
        audio.write_bytes(os.urandom(200))
        doc = project.dump([Node(kind="playlist", name="Test", tracks=[Track(path=str(audio))])], {})
        backup.create(str(self.archive), doc)
        restored = backup.restore(str(self.archive), str(self.target))
        nodes, _ = project.read(restored)
        self.assertEqual(Path(nodes[0].tracks[0].path).read_bytes(), audio.read_bytes())

    def test_small_compression_bomb_with_valid_checksum_is_rejected_before_read(self):
        self.archive_with(audio=b"\0" * (1 << 20), compressed=True)
        original = zipfile.ZipFile.open
        opened = []

        def open_entry(archive, name, *args, **kwargs):
            opened.append(name)
            return original(archive, name, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", open_entry):
            self.rejected("compression ratio")
        self.assertFalse(opened)

    def test_entry_and_expanded_size_limits(self):
        self.archive_with()
        with patch.object(backup, "MAX_ENTRIES", 2):
            self.rejected("entry or memory limit")
        with patch.object(backup, "MAX_EXPANDED", 100):
            self.rejected("expanded size")

    def test_approved_limits_and_exact_ratio_boundary(self):
        self.assertEqual((backup.MAX_ENTRIES, backup.MAX_EXPANDED, backup.MAX_DIRECTORY,
                          backup.MAX_RATIO, backup.DISK_RESERVE),
                         (100_000, 1 << 40, 128 << 20, 100, 1 << 30))
        entry = zipfile.ZipInfo("Audio/test.wav")
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.compress_size = 10
        entry.file_size = 1000
        self.assertEqual(backup._entry_limits([entry]), 1000)
        entry.file_size += 1
        with self.assertRaisesRegex(ValueError, "compression ratio"):
            backup._entry_limits([entry])
        entry.file_size = 10
        for method, flags in ((zipfile.ZIP_BZIP2, 0), (zipfile.ZIP_STORED, 1)):
            entry.compress_type, entry.flag_bits = method, flags
            with self.assertRaisesRegex(ValueError, "Encrypted or unsupported"):
                backup._entry_limits([entry])


    def test_directory_limit_before_zipfile_allocation(self):
        self.archive.write_bytes(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0, 0,
                                             backup.MAX_DIRECTORY + 1, 0, 0))
        with patch.object(zipfile, "ZipFile") as archive:
            self.rejected("directory exceeds")
            archive.assert_not_called()

    def test_forged_directory_entry_count_is_rejected_before_allocation(self):
        self.archive_with()
        raw = bytearray(self.archive.read_bytes())
        end = raw.rfind(b"PK\x05\x06")
        struct.pack_into("<2H", raw, end + 8, 1, 1)
        self.archive.write_bytes(raw)
        self.seal()
        with patch.object(zipfile, "ZipFile") as archive:
            self.rejected("count mismatch")
            archive.assert_not_called()

    def test_zip64_directory_limit_before_zipfile_allocation(self):
        record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                             backup.MAX_ENTRIES + 1, backup.MAX_ENTRIES + 1, 0, 0)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
        end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 65535, 65535, 0, 0, 0)
        self.archive.write_bytes(record + locator + end)
        with patch.object(zipfile, "ZipFile") as archive:
            self.rejected("directory exceeds")
            archive.assert_not_called()

    def test_path_traversal_absolute_and_windows_streams(self):
        for name in ("../escaped", "/escaped", "Audio/../escaped", "Audio\\escaped",
                     "Audio/a:stream", "Audio/NUL.wav", "Audio/COM1"):
            with self.subTest(name=name):
                self.archive_with(extra=[(name, b"bad")])
                self.rejected("Unsafe")
        self.assertFalse((self.root / "escaped").exists())

    def test_symlinks_and_case_duplicate_names(self):
        self.archive_with()
        link = zipfile.ZipInfo("Audio/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr(link, "../outside")
        self.seal()
        self.rejected("Unsafe")
        self.archive_with(extra=[("audio/TEST.wav", b"duplicate")])
        self.rejected("Unsafe")

    def test_outside_project_reference_is_rejected(self):
        self.archive_with(track_path="../outside.wav")
        self.rejected("outside the backup")

    def test_disk_reserve_checked_before_and_during_extraction(self):
        self.archive_with()
        with patch.object(backup.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
            self.rejected("free space")
        with patch.object(backup.shutil, "disk_usage",
                          side_effect=[SimpleNamespace(free=10 << 30), SimpleNamespace(free=10)]):
            self.rejected("preserve 1 GiB")

    def test_streamed_bytes_cannot_exceed_header_size(self):
        self.archive_with()
        original = zipfile.ZipFile.infolist

        def smaller(archive):
            entries = original(archive)
            for entry in entries:
                if entry.filename == "Audio/test.wav":
                    entry.file_size = 1
            return entries

        with patch.object(zipfile.ZipFile, "infolist", smaller):
            self.rejected("CRC|expanded size|size mismatch")

    def test_xml_internal_and_external_entity_declarations_are_rejected(self):
        loaders = [(nml.read_tree, "NML"), (rbxml.load, "DJ_PLAYLISTS"),
                   (vdj.load, "VirtualDJ_Database"),
                   (lambda path: vdj.read_vdjfolder(path, {}, {}), "VirtualFolder")]
        for loader, tag in loaders:
            for entity in ('"tiny harmless text"', 'SYSTEM "file:///nonexistent/probe"'):
                with self.subTest(loader=loader, entity=entity):
                    path = self.root / "database.xml"
                    path.write_text(f'<!DOCTYPE {tag} [<!ENTITY payload {entity}>]>'
                                    f'<{tag} Name="&payload;"/>')
                    with self.assertRaises(DefusedXmlException):
                        loader(str(path))

    def test_plain_doctype_and_standard_xml_escapes_remain_supported(self):
        path = self.root / "collection.nml"
        path.write_text('<!DOCTYPE NML><NML Name="A &amp; B"/>')
        self.assertEqual(nml.read_tree(str(path)).get("Name"), "A & B")

    def test_project_command_like_strings_are_data_not_executed(self):
        text = '$(whoami); & calc.exe | <script>alert(1)</script>'
        doc = project.dump([Node(kind="playlist", name=text, tracks=[Track(title=text)])], {})
        doc["__reduce__"] = ["os.system", text]
        path = self.root / "project.json"
        path.write_text(json.dumps(doc))
        with patch("os.system") as system, patch("subprocess.run") as run:
            nodes, _ = project.read(str(path))
        self.assertEqual(nodes[0].name, text)
        self.assertEqual(nodes[0].tracks[0].title, text)
        system.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
