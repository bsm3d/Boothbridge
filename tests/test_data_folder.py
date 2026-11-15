import importlib.util
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from djapp import settings
from djapp.brand import APP_NAME, HOME_ENV, PORTABLE_FLAG, SETTINGS_FILE

ROOT = Path(__file__).resolve().parents[2] / "Boothbridge"
if not (ROOT / "build.py").is_file():
    ROOT = Path(__file__).resolve().parents[1]


def without_home(**extra):
    env = {k: v for k, v in os.environ.items() if k != HOME_ENV}
    env.update(extra)
    return patch.dict(os.environ, env, clear=True)


class LocationTests(unittest.TestCase):
    def test_the_home_folder_is_among_the_data_of_the_author(self):
        with tempfile.TemporaryDirectory() as tmp, without_home(APPDATA=tmp):
            self.assertEqual(settings.home_dir(), Path(tmp) / "BSM" / APP_NAME)

    def test_the_variable_replaces_the_home_folder_and_the_portable_flag(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as program:
            (Path(program) / PORTABLE_FLAG).write_text("x")
            with patch.dict(os.environ, {HOME_ENV: tmp}), patch.object(settings, "app_dir", return_value=Path(program)):
                self.assertEqual(settings.home_dir(), Path(tmp))
                self.assertEqual(settings.data_dir(), Path(tmp))
                self.assertEqual(settings.cache_dir(), Path(tmp) / "cache")

    def test_an_installed_copy_keeps_its_settings_and_its_log_in_the_home_folder(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as program, without_home(APPDATA=tmp):
            with patch.object(settings, "app_dir", return_value=Path(program)):
                self.assertFalse(settings.portable())
                self.assertEqual(settings.data_dir(), Path(tmp) / "BSM" / APP_NAME)
                self.assertTrue((Path(tmp) / "BSM" / APP_NAME).is_dir())

    def test_a_portable_copy_keeps_them_beside_the_program_but_its_cache_goes_to_the_home_folder(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as program, without_home(APPDATA=tmp):
            (Path(program) / PORTABLE_FLAG).write_text("x")
            with patch.object(settings, "app_dir", return_value=Path(program)):
                self.assertTrue(settings.portable())
                self.assertEqual(settings.data_dir(), Path(program))
                self.assertEqual(settings.cache_dir(), Path(tmp) / "BSM" / APP_NAME / "cache")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.program, self.roaming, self.local = base / "program", base / "roaming", base / "local"
        for d in (self.program, self.roaming, self.local):
            d.mkdir()
        self.new_config = self.roaming / "BSM" / APP_NAME / SETTINGS_FILE
        (self.roaming / "BSM" / APP_NAME).mkdir(parents=True)

    def run_migration(self):
        with without_home(APPDATA=str(self.roaming), LOCALAPPDATA=str(self.local)), \
                patch.object(settings, "app_dir", return_value=self.program), patch.object(settings, "CONFIG", self.new_config):
            settings.migrate()

    def test_the_settings_next_to_the_program_are_copied_once(self):
        (self.program / SETTINGS_FILE).write_text('{"language": "fr"}', encoding="utf-8")
        self.run_migration()
        self.assertEqual(self.new_config.read_text(encoding="utf-8"), '{"language": "fr"}')
        self.assertTrue((self.program / SETTINGS_FILE).is_file(), "the old file is not touched")

    def test_settings_already_in_the_home_folder_are_never_replaced(self):
        (self.program / SETTINGS_FILE).write_text('{"language": "fr"}', encoding="utf-8")
        self.new_config.write_text('{"language": "de"}', encoding="utf-8")
        self.run_migration()
        self.assertEqual(self.new_config.read_text(encoding="utf-8"), '{"language": "de"}')

    def test_the_caches_of_the_older_local_folder_are_moved(self):
        older = self.local / APP_NAME
        (older / "anlz").mkdir(parents=True)
        (older / "anlz" / "a.dat").write_bytes(b"1")
        (older / "wave").mkdir()
        (older / "wave" / "w.hi1000").write_bytes(b"2")
        self.run_migration()
        cache = self.roaming / "BSM" / APP_NAME / "cache"
        self.assertEqual((cache / "anlz" / "a.dat").read_bytes(), b"1")
        self.assertEqual((cache / "wave" / "w.hi1000").read_bytes(), b"2")
        self.assertFalse((older / "anlz").exists())

    def test_a_cache_already_in_the_new_place_is_kept_and_the_old_one_stays(self):
        older = self.local / APP_NAME
        (older / "wave").mkdir(parents=True)
        (older / "wave" / "old.bin").write_bytes(b"old")
        new = self.roaming / "BSM" / APP_NAME / "cache" / "wave"
        new.mkdir(parents=True)
        (new / "new.bin").write_bytes(b"new")
        self.run_migration()
        self.assertTrue((new / "new.bin").is_file())
        self.assertFalse((new / "old.bin").exists())
        self.assertTrue((older / "wave" / "old.bin").is_file())

    def test_nothing_is_moved_when_the_home_folder_is_replaced_by_the_variable(self):
        older = self.local / APP_NAME
        (older / "anlz").mkdir(parents=True)
        (older / "anlz" / "a.dat").write_bytes(b"1")
        with patch.dict(os.environ, {HOME_ENV: str(self.roaming / "other"), "LOCALAPPDATA": str(self.local)}):
            settings.migrate()
        self.assertTrue((older / "anlz" / "a.dat").is_file())

    def test_settings_saved_by_notepad_with_a_bom_are_read(self):
        self.new_config.write_bytes(b'\xef\xbb\xbf{"language": "fr", "volume": 40}')
        with patch.object(settings, "CONFIG", self.new_config):
            cfg = settings.load()
        self.assertEqual((cfg["language"], cfg["volume"]), ("fr", 40))


class ZipTests(unittest.TestCase):
    def test_the_portable_zip_carries_the_flag_and_the_installer_folder_does_not(self):
        spec = importlib.util.spec_from_file_location("bb_build", ROOT / "build.py")
        build = importlib.util.module_from_spec(spec)
        # build.py puts its own folder first on sys.path: the analysis processes started by tests that
        # run later would import that tree and not the one under test
        before = list(sys.path)
        try:
            spec.loader.exec_module(build)
        finally:
            sys.path[:] = before
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dist = base / "dist" / "Boothbridge"
            dist.mkdir(parents=True)
            (dist / "Boothbridge.exe").write_bytes(b"MZ")
            with patch.object(build, "ROOT", base), patch.object(build, "DIST", dist):
                z = build.make_zip()
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
            self.assertIn(f"Boothbridge/{PORTABLE_FLAG}", names)
            self.assertIn("Boothbridge/Boothbridge.exe", names)
            self.assertFalse((dist / PORTABLE_FLAG).exists(), "the folder the installer packs has no flag")


if __name__ == "__main__":
    unittest.main()
