# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks drive listing: only Windows drives are offered, with the system command output mocked.

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from djapp import drives


# --------------------------------------------
class DriveTests(unittest.TestCase):
    @staticmethod
    def completed(stdout, returncode=0, stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_list_windows_only_returns_writable_removable_data_drives(self):
        disks = [
            {"Number": 1, "Name": "USB", "Size": 64000000000, "Bus": "USB", "System": False,
             "Boot": False, "Style": "GPT", "Sector": 512, "Serial": "A-12 !", "ReadOnly": False,
             "Vols": [{"Letter": "E", "FS": "exFAT", "Label": "MUSIC", "Size": 64000000000}]},
            {"Number": 2, "Name": "System", "Size": 1000000000000, "Bus": "NVMe", "System": True,
             "Boot": True, "Style": "GPT", "Sector": 512, "Serial": "SYS", "ReadOnly": False, "Vols": []},
            {"Number": 3, "Name": "Read only", "Size": 32000000000, "Bus": "USB", "System": False,
             "Boot": False, "Style": "MBR", "Sector": 512, "Serial": "RO", "ReadOnly": True, "Vols": []},
        ]
        with patch.object(drives, "run", return_value=self.completed(json.dumps(disks))):
            found = drives.list_windows()

        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].id, found[0].serial, found[0].mounts), ("1", "A12", ["E:\\"]))
        self.assertEqual(found[0].volumes, ["MUSIC (exFAT, 64.0 GB)"])

    def test_list_windows_reports_command_failure(self):
        with (patch.object(drives, "run", return_value=self.completed("", 1, "denied")),
              self.assertRaisesRegex(OSError, "denied")):
            drives.list_windows()

    def test_list_linux_excludes_system_mount_and_internal_disks(self):
        devices = {"blockdevices": [
            {"type": "disk", "path": "/dev/sda", "tran": "sata", "rm": False, "size": 500000,
             "mountpoint": "/", "log-sec": 512},
            {"type": "disk", "path": "/dev/sdb", "tran": "usb", "rm": False, "size": 64000000000,
             "vendor": "Acme", "model": "Key", "serial": "X-9", "pttype": "gpt", "log-sec": 512,
             "children": [{"mountpoint": "/media/dj", "fstype": "exfat", "label": "MUSIC", "size": 64000000000}]},
            {"type": "disk", "path": "/dev/sdc", "tran": "usb", "rm": False, "size": 32000000000,
             "children": [{"mountpoint": "/home", "size": 32000000000}]},
        ]}
        with patch.object(drives, "run", return_value=self.completed(json.dumps(devices))):
            found = drives.list_linux()

        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].id, found[0].name, found[0].serial), ("/dev/sdb", "Acme Key", "X9"))
        self.assertEqual(found[0].mounts, ["/media/dj"])
        self.assertEqual(found[0].volumes, ["MUSIC (exfat, 64.0 GB)"])

    def test_list_drives_excludes_program_volume(self):
        drive = drives.Drive("E", "USB", 64000000000, mounts=["E:\\"])
        with (patch.object(drives, "list_windows", return_value=[drive]),
              patch.object(drives.sys, "platform", "win32"),
              patch.object(drives, "running_from", return_value=os.path.normcase(os.path.abspath("E:\\App")))):
            self.assertEqual(drives.list_drives(), [])

    def test_drive_for_requires_path_inside_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            mount = os.path.join(folder, "usb")
            os.mkdir(mount)
            drive = drives.Drive("usb", "USB", 64000000000, mounts=[mount])
            with patch.object(drives, "list_windows", return_value=[drive]):
                self.assertIs(drives.drive_for(os.path.join(mount, "music.mp3")), drive)
                self.assertIsNone(drives.drive_for(mount + "-backup\\music.mp3"))

    def test_check_requires_same_id_size_and_serial(self):
        original = drives.Drive("usb", "USB", 64000000000, serial="one")
        with patch.object(drives, "list_drives", return_value=[original]):
            self.assertIs(drives.check(original), original)
        with (patch.object(drives, "list_drives", return_value=[original]),
              self.assertRaisesRegex(OSError, "changed")):
            drives.check(drives.Drive("usb", "USB", 32000000000, serial="one"))
        with (patch.object(drives, "list_drives", return_value=[original]),
              self.assertRaisesRegex(OSError, "changed")):
            drives.check(drives.Drive("usb", "USB", 64000000000, serial="two"))
        with (patch.object(drives, "list_drives", return_value=[]),
              self.assertRaisesRegex(OSError, "no longer there")):
            drives.check(original)

    def test_partition_node_handles_device_name_suffix(self):
        self.assertEqual(drives.partition_node("/dev/sda"), "/dev/sda1")
        self.assertEqual(drives.partition_node("/dev/nvme0n1"), "/dev/nvme0n1p1")

    def test_mac_raw_device_only_accepts_whole_disk_identifiers(self):
        self.assertEqual(drives.mac_raw_device(drives.Drive("/dev/disk4", "USB", 64000000000)),
                         "/dev/rdisk4")
        for identifier in ("/dev/disk4s1", "/dev/rdisk4", "/dev/disk4;rm"):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(OSError, "whole macOS disk"):
                drives.mac_raw_device(drives.Drive(identifier, "USB", 64000000000))

    def test_format_mac_uses_shared_fat32_writer_between_native_mount_steps(self):
        drive = drives.Drive("/dev/disk4", "USB", 64000000000, 512)
        device = MagicMock()
        progress = []

        def format_image(dev, size, bps, label, progress):
            self.assertIs(dev, device)
            self.assertEqual((size, bps, label), (drive.size, drive.bps, "TRAKTOR"))
            progress(100, "Formatted")

        with (patch.object(drives, "run", return_value=self.completed("")) as run,
              patch.object(drives.fat32, "RawDevice", return_value=device) as raw_device,
              patch.object(drives.fat32, "format_disk", side_effect=format_image) as formatter,
              patch.object(drives, "mac_mountpoint", return_value="/Volumes/TRAKTOR")):
            result = drives.format_mac(drive, "TRAKTOR", lambda pct, msg: progress.append((pct, msg)))

        raw_device.assert_called_once_with("/dev/rdisk4")
        formatter.assert_called_once()
        device.close.assert_called_once()
        self.assertEqual(result, "/Volumes/TRAKTOR")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["diskutil", "unmountDisk", drive.id], ["diskutil", "mountDisk", drive.id]],
        )
        self.assertIn((90, "Formatted"), progress)
        self.assertEqual(progress[-1], (92, "Mounting the new volume"))

    def test_format_mac_does_not_open_disk_when_unmount_fails(self):
        drive = drives.Drive("/dev/disk4", "USB", 64000000000)
        failed = self.completed("", 1, "disk busy")
        with (patch.object(drives, "run", return_value=failed),
              patch.object(drives.fat32, "RawDevice") as raw_device,
              self.assertRaisesRegex(OSError, "could not unmount")):
            drives.format_mac(drive, "TRAKTOR", lambda _pct, _msg: None)
        raw_device.assert_not_called()

    def test_mac_elevation_uses_osascript_with_shell_quoted_arguments(self):
        handle = object()
        with (patch.object(drives.sys, "platform", "darwin"),
              patch.object(drives.sys, "executable", "/Applications/Python 3/bin/python3"),
        patch.object(drives.settings, "app_dir", return_value="/Applications/Boothbridge"),
              patch.object(drives.subprocess, "Popen", return_value=handle) as popen):
            result = drives.launch_elevated(["--format-disk", "/dev/disk4", "--label", "DJ's Key"])

        self.assertIs(result, handle)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["osascript", "-e"])
        shell = (
            f"PYTHONPATH={shlex.quote('/Applications/Boothbridge')} exec "
            + shlex.join(["/Applications/Python 3/bin/python3", "-m", "djapp",
                    "--format-disk", "/dev/disk4", "--label", "DJ's Key"])
        )
        self.assertEqual(
            command[2],
            f"do shell script {drives.applescript_string(shell)} with administrator privileges",
        )

    def test_format_drive_uses_elevated_helper_on_unprivileged_macos(self):
        drive = drives.Drive("/dev/disk4", "USB", 64000000000)
        with (patch.object(drives, "check", return_value=drive),
              patch.object(drives.sys, "platform", "darwin"),
              patch.object(drives, "is_admin", return_value=False),
              patch.object(drives, "launch_elevated", return_value=object()) as launch,
              patch.object(drives, "wait_elevated", return_value=0),
              patch.object(drives, "relay", return_value=(0, "done", "/Volumes/TRAKTOR"))):
            result = drives.format_drive(drive, "TRAKTOR")

        self.assertEqual(result, "/Volumes/TRAKTOR")
        self.assertEqual(launch.call_args.args[0][0], drives.HELPER_FLAG)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------
# the tools run with administrator rights are started by full path, so a copy of diskpart.exe or
# powershell.exe next to the program, which CreateProcess would find first, is never run
class ToolPathTests(unittest.TestCase):
    def test_windows_tools_get_their_system_path(self):
        with patch.object(drives.sys, "platform", "win32"), patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            self.assertEqual(drives.tool("diskpart"), os.path.join(r"C:\Windows", "System32", "diskpart.exe"))
            self.assertEqual(drives.tool("powershell"),
                             os.path.join(r"C:\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))

    def test_other_names_and_other_systems_are_left_as_they_are(self):
        with patch.object(drives.sys, "platform", "win32"):
            self.assertEqual(drives.tool("lsblk"), "lsblk")
        with patch.object(drives.sys, "platform", "linux"):
            self.assertEqual(drives.tool("diskpart"), "diskpart")

    def test_run_starts_the_full_path_and_keeps_the_arguments(self):
        seen = []
        with patch.object(drives.sys, "platform", "win32"), patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}), \
                patch.object(drives.subprocess, "run", lambda cmd, **kw: seen.append(cmd)):
            drives.run(["diskpart", "/s", "x.txt"])
            drives.run(["lsblk", "-J"])
        self.assertTrue(seen[0][0].endswith("diskpart.exe") and os.path.isabs(seen[0][0]) or seen[0][0].startswith("C:"))
        self.assertEqual(seen[0][1:], ["/s", "x.txt"])
        self.assertEqual(seen[1], ["lsblk", "-J"])
