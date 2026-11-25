# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the FAT32 writer against the FAT specification geometry for every drive size, and parses its images.

import random
import struct
import tempfile
import unittest
from pathlib import Path

from djapp import fat32

MIB, GIB, TIB = 1 << 20, 1 << 30, 1 << 40


# ----------------------------------------------------------------
def check_layout(test, size: int, bps: int):
    if bps * 65525 > size - MIB:  # 65 525 clusters of at least one sector do not fit
        with test.assertRaises(ValueError):
            fat32.layout(size, bps, "X", volid=1)
        return
    L = fat32.layout(size, bps, "X", volid=1)
    test.assertEqual(L.start * bps, MIB, "the partition starts at 1 MiB")
    test.assertLessEqual((L.start + L.total) * bps, size, "the partition is inside the drive")
    test.assertLess(L.start + L.total, 2**32 + 1, "an MBR partition is 32 bit")
    test.assertIn(L.spc & -L.spc, (L.spc,), "sectors per cluster is a power of two")
    test.assertLessEqual(L.spc, 128)
    test.assertLessEqual(L.cluster_size, 65536, "64 KB is the most the specification allows")
    if size <= 4 * TIB:
        test.assertLessEqual(L.cluster_size, 32768, "up to 4 TB clusters stay at 32 KB, what the players are used to")
    test.assertGreaterEqual(L.clusters, 65525, "fewer clusters is a FAT16 volume by definition")
    test.assertLessEqual(L.clusters, 0x0FFFFFF5)
    # the FAT holds an entry for every cluster and the two reserved ones
    test.assertGreaterEqual(L.fat * bps, (L.clusters + 2) * 4)
    # and is not wastefully big: the geometry settles on a FAT a little over what it needs, under 2 %
    test.assertLess(L.fat * bps - (L.clusters + 2) * 4, max(4096, L.fat * bps * 2 // 100))
    test.assertEqual((L.reserved + 2 * L.fat) % L.spc, 0, "the data area starts on a cluster boundary")
    test.assertEqual((L.start * bps + L.data * bps) % L.cluster_size, 0, "and so does the byte offset on the disk")
    test.assertGreaterEqual(L.reserved, 32)
    # no sector of the partition is wasted by more than a cluster
    used = L.reserved + 2 * L.fat + L.clusters * L.spc
    test.assertGreaterEqual(L.total, used)
    test.assertLess(L.total - used, L.spc + 1)


class GeometryTests(unittest.TestCase):
    def test_known_sizes(self):
        for size in (35 * MIB, 64 * MIB, 100 * MIB, 513 * MIB, GIB, 4 * GIB, 8 * GIB, 16 * GIB, 31 * GIB,
                     32 * GIB, 33 * GIB, 64 * GIB, 128 * GIB, 256 * GIB, 512 * GIB, TIB, 2 * TIB, 4 * TIB, 16 * TIB):
            for bps in (512, 4096):
                with self.subTest(size=size, bps=bps):
                    check_layout(self, size, bps)

    def test_random_sizes(self):
        rng = random.Random(2026)
        for _ in range(300):
            size = int(10 ** rng.uniform(7.6, 13.2)) // 512 * 512  # 40 MB to 16 TB
            bps = rng.choice((512, 4096))
            size = size // bps * bps
            with self.subTest(size=size, bps=bps):
                check_layout(self, size, bps)

    def test_a_drive_that_is_too_small_is_refused(self):
        for size in (MIB, 8 * MIB, 32 * MIB):
            with self.subTest(size), self.assertRaises(ValueError):
                fat32.layout(size)

    def test_labels(self):
        self.assertEqual(fat32.clean_label("My Key é/1"), "MY KEY _?1".replace("?", "_"))
        self.assertEqual(fat32.clean_label("x" * 30), "X" * 11)
        self.assertEqual(fat32.clean_label(""), "")


# ------------------------------------------------------------
def read_image(path: Path, size: int, bps: int = 512, label: str = "SPEC TEST"):
    with path.open("wb") as f:
        f.truncate(size)
    dev = fat32.RawDevice(str(path), size)
    try:
        L = fat32.format_disk(dev, size, bps, label)
    finally:
        dev.close()
    return L


# ----------------------------------------------------------------
class ImageTests(unittest.TestCase):
    def parse(self, size: int, bps: int = 512, label: str = "SPEC TEST"):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        image = Path(folder.name) / "disk.img"
        L = read_image(image, size, bps, label)
        return L, image.read_bytes() if size <= 600 * MIB else None, image

    def test_the_structures_follow_the_specification(self):
        for size in (40 * MIB, 64 * MIB, 200 * MIB, 513 * MIB):
            with self.subTest(size=size):
                L, raw, _ = self.parse(size)
                bps = 512
                # --- MBR
                self.assertEqual(raw[510:512], b"\x55\xAA")
                self.assertEqual(raw[446], 0)  # not active
                self.assertEqual(raw[450], 0x0C)
                lba, count = struct.unpack_from("<LL", raw, 454)
                self.assertEqual((lba, count), (L.start, L.total))
                self.assertEqual(raw[462:510], bytes(48), "no other partition")
                base = lba * bps
                # --- boot sector and its backup
                boot = raw[base:base + bps]
                self.assertEqual(boot[510:512], b"\x55\xAA")
                self.assertEqual(boot[0], 0xEB)
                self.assertEqual(boot[2], 0x90)
                (bytes_per_sector, spc, reserved, fats, root_entries, sectors16, media, fat16, _spt, _heads,
                 hidden, sectors32) = struct.unpack_from("<HBHBHHBHHHLL", boot, 11)
                self.assertEqual((bytes_per_sector, fats, root_entries, sectors16, fat16), (bps, 2, 0, 0, 0))
                self.assertEqual((spc, reserved, media, hidden, sectors32), (L.spc, L.reserved, 0xF8, lba, count))
                (fat32_size, flags, version, root, fsinfo_sector, backup, _res, _drive, _r, signature, volid,
                 label, kind) = struct.unpack_from("<LHHLHH12sBBBL11s8s", boot, 36)
                self.assertEqual((fat32_size, flags, version, root, fsinfo_sector, backup), (L.fat, 0, 0, 2, 1, 6))
                self.assertEqual((signature, kind), (0x29, b"FAT32   "))
                self.assertEqual(label, b"SPEC TEST  ")
                self.assertEqual(volid, struct.unpack_from("<L", boot, 67)[0])
                self.assertEqual(raw[base + 6 * bps:base + 7 * bps], boot, "the backup boot sector is the same")
                # --- FSInfo and its backup
                for at in (1, 7):
                    info = raw[base + at * bps:base + (at + 1) * bps]
                    self.assertEqual(struct.unpack_from("<L", info, 0)[0], 0x41615252)
                    lead, free, nxt = struct.unpack_from("<LLL", info, 484)
                    self.assertEqual((lead, free, nxt), (0x61417272, L.clusters - 1, 3))
                    self.assertEqual(info[508:512], b"\x00\x00\x55\xAA")
                for at in (2, 8):
                    self.assertEqual(raw[base + at * bps + 508:base + at * bps + 512], b"\x00\x00\x55\xAA")
                # --- the two FATs are equal and hold only the three used entries
                fat_a = raw[base + L.reserved * bps:base + (L.reserved + L.fat) * bps]
                fat_b = raw[base + (L.reserved + L.fat) * bps:base + (L.reserved + 2 * L.fat) * bps]
                self.assertEqual(fat_a, fat_b)
                self.assertEqual(struct.unpack_from("<LLL", fat_a, 0), (0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF))
                self.assertEqual(fat_a[12:], bytes(len(fat_a) - 12), "every other cluster is free")
                # --- the root directory is cluster 2, with the label and nothing else
                root_at = base + L.data * bps
                entry = raw[root_at:root_at + 32]
                self.assertEqual(entry[:11], b"SPEC TEST  ")
                self.assertEqual(entry[11], 0x08)
                self.assertEqual(raw[root_at + 32:root_at + L.cluster_size], bytes(L.cluster_size - 32))
                # --- what was there before is gone
                self.assertEqual(raw[512:base], bytes(base - 512), "nothing between the MBR and the partition")

    def test_no_label_gives_no_label_entry(self):
        L, raw, _ = self.parse(64 * MIB, label="")
        base = L.start * 512
        boot = raw[base:base + 512]
        self.assertEqual(boot[71:82], b"NO NAME    ")
        self.assertEqual(raw[base + L.data * 512:base + L.data * 512 + 32], bytes(32))

    def test_old_signatures_are_wiped_at_both_ends(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        image = Path(folder.name) / "disk.img"
        size = 100 * MIB
        with image.open("wb") as f:
            f.write(b"\xAA" * size)  # a drive full of old data
        dev = fat32.RawDevice(str(image), size)
        try:
            L = fat32.format_disk(dev, size)
        finally:
            dev.close()
        raw = image.read_bytes()
        self.assertEqual(raw[512:L.start * 512], bytes(L.start * 512 - 512))
        self.assertEqual(raw[-8 * MIB:], bytes(8 * MIB), "the last 8 MiB are zero, a GPT backup header cannot survive")
        fat = raw[L.start * 512 + L.reserved * 512:][:L.fat * 512]
        self.assertEqual(fat[12:], bytes(len(fat) - 12))

    def test_the_progress_goes_up_and_ends_at_100(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        image = Path(folder.name) / "disk.img"
        with image.open("wb") as f:
            f.truncate(64 * MIB)
        seen = []
        dev = fat32.RawDevice(str(image), 64 * MIB)
        try:
            fat32.format_disk(dev, 64 * MIB, progress=lambda p, m: seen.append(p))
        finally:
            dev.close()
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 100)
        self.assertLessEqual(max(seen[:-1]), 97)


if __name__ == "__main__":
    unittest.main()
