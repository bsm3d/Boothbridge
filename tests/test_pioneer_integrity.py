# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the integrity of Pioneer export files against the audio they point to, with a mocked audio source.

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from djapp.analysis import Bands
from djapp.export.cdj import anlz, devicesql, pdbwrite, usb, validate
from djapp.model import Cue, Node, Track


# ----------------------------------------------------
class Audio:
    ok = True

    def __init__(self):
        self.fine = [np.ones(150)]

    def at(self, count):
        values = np.ones(count, dtype=np.float32) * .5
        return Bands(44100, values, values, values)


# ----------------------------------------------------
class PioneerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.track = Track(path="song.wav", title="Song", bpm=120, grid=0, duration=1,
                           cues=[Cue(start=100, hotcue=0)])
        self.path = "/Contents/song.wav"

    def test_generated_dat_ext_validate_and_path_matches(self):
        dat, ext = anlz.build_py(self.track, self.path, Audio())
        validate.anlz(dat, self.path)
        validate.anlz(ext, self.path)
        with self.assertRaisesRegex(ValueError, "path"):
            validate.anlz(dat, "/different.wav")
        broken = bytearray(dat)
        struct.pack_into(">I", broken, 28 + 8, len(dat) + 1)
        self.assertFalse(usb.whole(bytes(broken)))

    def test_waveform_and_cue_counts_cannot_silently_disagree(self):
        for raw, kind, field in [(anlz.pwav(Audio().at(400)), b"PWAV", 12),
                                 (anlz.cue_lists(self.track, False)[0], b"PCOB", 18)]:
            broken = bytearray(raw)
            if kind == b"PWAV":
                struct.pack_into(">I", broken, field, 399)
            else:
                struct.pack_into(">H", broken, field, 0)
            data = anlz.pmai([anlz.ppth(self.path), bytes(broken)])
            with self.assertRaises(ValueError):
                validate.anlz(data)

    def test_pdb_valid_pages_and_corrupt_chain_row_offsets(self):
        data = devicesql.build_py({0: [b"row data"]}, {}, set())
        validate.pdb(data)
        broken = bytearray(data)
        struct.pack_into("<I", broken, devicesql.PAGE + 12, 1)
        with self.assertRaisesRegex(ValueError, "circular"):
            validate.pdb(bytes(broken))
        broken = bytearray(data)
        struct.pack_into("<H", broken, 3 * devicesql.PAGE - 6, 4000)
        with self.assertRaisesRegex(ValueError, "offset"):
            validate.pdb(bytes(broken))
        with self.assertRaisesRegex(ValueError, "alignment"):
            validate.pdb(data[:-1])

    def test_full_pdb_writer_validates_reference_static_pages(self):
        item = pdbwrite.Item(self.track, 1, self.path, "/PIONEER/USBANLZ/A/ANLZ0000.DAT", 0)
        path = self.root / "export.pdb"
        pdbwrite.write([item], [Node(kind="playlist", name="Set", tracks=[self.track])], path)
        validate.pdb(path.read_bytes())

    def test_readback_failure_keeps_existing_file_and_removes_staging(self):
        path = self.root / "export.pdb"
        path.write_bytes(b"previous")
        data = devicesql.build_py({}, {}, set())
        with patch.object(validate, "sha256_drive", return_value="bad"), \
             self.assertRaisesRegex(OSError, "read-back"):
            validate.write_verified(str(path), data, validate.pdb)
        self.assertEqual(path.read_bytes(), b"previous")
        self.assertFalse(Path(str(path) + ".verified").exists())

    def test_anlz_pair_invalid_before_either_destination_changes(self):
        item = pdbwrite.Item(self.track, 1, self.path, "/analysis/ANLZ0000.DAT", 0)
        dat, ext = anlz.build_py(self.track, self.path, Audio())
        with self.assertRaises(ValueError):
            usb.put_anlz(str(self.root), item, dat, ext[:-1])
        self.assertFalse((self.root / "analysis").exists())
        usb.put_anlz(str(self.root), item, dat, ext)
        self.assertEqual((self.root / "analysis" / "ANLZ0000.DAT").read_bytes(), dat)

    def test_cache_invalid_sections_or_wrong_track_are_recomputed(self):
        dat, ext = anlz.build_py(self.track, self.path, Audio())
        (self.root / "key.DAT").write_bytes(dat)
        (self.root / "key.EXT").write_bytes(ext)
        self.assertEqual(usb.cached(self.root, "key", self.path), (dat, ext))
        with self.assertLogs(usb.log, level="WARNING"):
            self.assertIsNone(usb.cached(self.root, "key", "/other.wav"))
        (self.root / "key.EXT").write_bytes(anlz.pmai([anlz.ppth(self.path)]))
        with self.assertLogs(usb.log, level="WARNING"):
            self.assertIsNone(usb.cached(self.root, "key", self.path))


if __name__ == "__main__":
    unittest.main()
