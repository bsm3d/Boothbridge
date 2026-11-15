import struct
import tempfile
import unittest
from pathlib import Path

from djapp import fat32


# ------------------------------------------------------------
class Fat32Tests(unittest.TestCase):
    def test_clean_label_replaces_invalid_characters_and_truncates(self):
        self.assertEqual(fat32.clean_label(' my:usb/name?123 '), "MY_USB_NAME")
        self.assertEqual(fat32.clean_label("long disk label"), "LONG DISK L")

    def test_cluster_size_matches_windows_thresholds(self):
        gib = 1 << 30
        self.assertEqual(fat32.cluster_bytes(8 * gib), 4096)
        self.assertEqual(fat32.cluster_bytes(9 * gib), 8192)
        self.assertEqual(fat32.cluster_bytes(17 * gib), 16384)
        self.assertEqual(fat32.cluster_bytes(33 * gib), 32768)

    def test_layout_aligns_partition_and_data_and_rejects_small_disks(self):
        size = 64 * fat32.MIB
        result = fat32.layout(size, label="test", volid=123)

        self.assertEqual(result.start * result.bps, fat32.MIB)
        self.assertEqual((result.reserved + 2 * result.fat) % result.spc, 0)
        self.assertGreaterEqual(result.clusters, fat32.MIN_CLUSTERS)
        self.assertEqual(result.label, "TEST")
        self.assertEqual(result.volid, 123)
        with self.assertRaisesRegex(ValueError, "too small"):
            fat32.layout(32 * fat32.MIB)

    def test_format_disk_writes_consistent_mbr_fat32_and_reports_progress(self):
        size = 64 * fat32.MIB
        progress = []
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "disk.img"
            with image.open("wb") as f:
                f.truncate(size)
            device = fat32.RawDevice(str(image), size)
            try:
                result = fat32.format_disk(
                    device, size, label="TEST DISK", progress=lambda pct, msg: progress.append((pct, msg))
                )

                table = device.read(0, result.bps)
                self.assertEqual(table[510:512], b"\x55\xAA")
                self.assertEqual(table[450], 0x0C)
                start, sectors = struct.unpack_from("<LL", table, 454)
                self.assertEqual((start, sectors), (result.start, result.total))

                base = result.start * result.bps
                boot = device.read(base, result.bps)
                self.assertEqual(boot[510:512], b"\x55\xAA")
                self.assertEqual(boot[3:11], b"MSDOS5.0")
                self.assertEqual(struct.unpack_from("<H", boot, 11)[0], result.bps)
                self.assertEqual(device.read(base + 6 * result.bps, result.bps), boot)
                self.assertEqual(device.read(base + result.bps, 4), struct.pack("<L", 0x41615252))

                fat_offset = base + result.reserved * result.bps
                entries = struct.unpack("<LLL", device.read(fat_offset, 12))
                self.assertEqual(entries, (0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF))
                root_offset = base + result.data * result.bps
                self.assertEqual(device.read(root_offset, 11), b"TEST DISK  ")
                self.assertEqual(progress[-1], (100, "Formatted"))
            finally:
                device.close()


if __name__ == "__main__":
    unittest.main()
