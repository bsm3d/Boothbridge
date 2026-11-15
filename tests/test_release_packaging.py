import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import build


class ReleasePackagingTests(unittest.TestCase):
    def test_prune_removes_source_files_but_preserves_runtime_binaries(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder) / "Boothbridge"
            runtime = dist / "runtime"
            runtime.mkdir(parents=True)
            for suffix in build.SOURCE_SUFFIXES:
                (runtime / f"module{suffix}").write_text("source", encoding="ascii")
            for name in ("module.pyd", "fastcore.dll", "LICENSE.txt", "icon.svg"):
                (runtime / name).write_bytes(b"runtime")
            with patch.object(build, "DIST", dist):
                self.assertEqual(build.prune(), len(build.SOURCE_SUFFIXES))
            self.assertEqual({p.name for p in runtime.iterdir()},
                             {"module.pyd", "fastcore.dll", "LICENSE.txt", "icon.svg"})

    def test_zip_contains_only_portable_directory_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dist = root / "dist" / "Boothbridge"
            dist.mkdir(parents=True)
            (dist / "Boothbridge.exe").write_bytes(b"binary")
            (root / "private.py").write_text("not included", encoding="ascii")
            with patch.object(build, "ROOT", root), patch.object(build, "DIST", dist):
                path = build.make_zip()
            with zipfile.ZipFile(path) as archive:
                # the program, and the flag that keeps the settings of the portable copy beside it; nothing else
                self.assertEqual(sorted(archive.namelist()), ["Boothbridge/Boothbridge.exe", "Boothbridge/portable.flag"])

    def test_packaging_refuses_sources_remaining_outside_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder) / "dist"
            dist.mkdir()
            (dist / "remaining.PYX").write_text("source", encoding="ascii")
            with patch.object(build, "DIST", dist), \
                    patch.object(build, "run_pyinstaller"), \
                    patch.object(build, "prune", return_value=0), \
                    patch.object(build, "check_seal"), \
                    patch.object(build.shutil, "copyfile"), \
                    patch.object(build, "make_zip") as archive, \
                    self.assertRaisesRegex(SystemExit, "Source files remain"):
                build.main()
            archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
