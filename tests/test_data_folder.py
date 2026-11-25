# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks where the program looks for its data folder, including the home folder and overrides.

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


DATAS = tempfile.mkdtemp(prefix="bb_datas_")      # D:\Datas (the analyses): a temporary folder here, the real D: is never touched


def without_home(**extra):
    env = {k: v for k, v in os.environ.items() if k != HOME_ENV}
    env["BSM_DATAS"] = DATAS
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

    def test_a_portable_copy_keeps_them_beside_the_program_but_its_cache_goes_to_the_datas_folder(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as program, without_home(APPDATA=tmp):
            (Path(program) / PORTABLE_FLAG).write_text("x")
            with patch.object(settings, "app_dir", return_value=Path(program)):
                self.assertTrue(settings.portable())
                self.assertEqual(settings.data_dir(), Path(program))
                self.assertEqual(settings.cache_dir(), Path(DATAS) / APP_NAME / "cache")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.program, self.roaming, self.local = base / "program", base / "roaming", base / "local"
        for d in (self.program, self.roaming, self.local):
            d.mkdir()
        self.datas = base / "datas"
        self.new_config = self.roaming / "BSM" / APP_NAME / SETTINGS_FILE
        (self.roaming / "BSM" / APP_NAME).mkdir(parents=True)

    def run_migration(self):
        with without_home(APPDATA=str(self.roaming), LOCALAPPDATA=str(self.local), BSM_DATAS=str(self.datas)), \
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
        cache = self.datas / APP_NAME / "cache"
        self.assertEqual((cache / "anlz" / "a.dat").read_bytes(), b"1")
        self.assertEqual((cache / "wave" / "w.hi1000").read_bytes(), b"2")
        self.assertFalse((older / "anlz").exists())

    def test_a_cache_already_in_the_new_place_is_kept_and_the_old_one_stays(self):
        older = self.local / APP_NAME
        (older / "wave").mkdir(parents=True)
        (older / "wave" / "old.bin").write_bytes(b"old")
        new = self.datas / APP_NAME / "cache" / "wave"
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


class DatasTests(unittest.TestCase):
    """The analyses are on D:\\Datas\\Boothbridge\\cache, the settings and the log stay in the home folder."""

    def test_the_root_is_the_variable_then_the_pointer_then_the_default_folder_if_it_is_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            roaming, default = Path(tmp) / "roaming", Path(tmp) / "Datas"
            (roaming / "BSM").mkdir(parents=True)
            with without_home(APPDATA=str(roaming)), patch.object(settings, "DEFAULT_DATAS", default):
                os.environ.pop("BSM_DATAS")
                self.assertIsNone(settings.datas_root())
                default.mkdir()
                self.assertEqual(settings.datas_root(), default)
                (roaming / "BSM" / "datas.json").write_text('{"root": "%s"}' % str(Path(tmp) / "pointed").replace("\\", "\\\\"), encoding="utf-8")
                self.assertEqual(settings.datas_root(), Path(tmp) / "pointed")
                os.environ["BSM_DATAS"] = str(Path(tmp) / "variable")
                self.assertEqual(settings.datas_root(), Path(tmp) / "variable")

    def test_without_a_data_folder_the_cache_stays_in_the_home_folder(self):
        # a PC with no D: disk: the program starts and nothing is made outside the home folder
        with tempfile.TemporaryDirectory() as tmp, without_home(APPDATA=tmp), patch.object(settings, "DEFAULT_DATAS", Path(tmp) / "none" / "Datas"):
            os.environ.pop("BSM_DATAS")
            d = settings.cache_dir()
            self.assertEqual(d, Path(tmp) / "BSM" / APP_NAME / "cache")
            self.assertTrue(d.is_dir())
            self.assertFalse((Path(tmp) / "none").exists())
            self.assertFalse((Path(tmp) / "BSM" / APP_NAME / settings.DATAS_MARK).exists())

    def test_a_relative_default_is_never_used(self):
        # D:/Datas is a relative path on macOS and Linux
        with tempfile.TemporaryDirectory() as tmp, without_home(APPDATA=tmp), patch.object(settings, "DEFAULT_DATAS", Path(".")):
            os.environ.pop("BSM_DATAS")
            self.assertIsNone(settings.datas_root())

    def test_a_default_folder_used_once_and_then_gone_is_said_not_replaced(self):
        missing = Path("Q:/Datas") if not os.path.exists("Q:\\") else Path("Z:/Datas")
        if os.path.exists(missing.anchor):
            self.skipTest("the test disk exists on this machine")
        with tempfile.TemporaryDirectory() as tmp, without_home(APPDATA=tmp):
            os.environ.pop("BSM_DATAS")
            default = Path(tmp) / "Datas"
            default.mkdir()
            with patch.object(settings, "DEFAULT_DATAS", default), patch.object(settings, "_ADOPTED", []):
                self.assertEqual(settings.cache_dir(), default / APP_NAME / "cache")
            mark = Path(tmp) / "BSM" / APP_NAME / settings.DATAS_MARK
            self.assertEqual(mark.read_text(encoding="utf-8").strip(), str(default))
            # the disk is taken out
            mark.write_text(f"{missing}\n", encoding="utf-8")
            with patch.object(settings, "DEFAULT_DATAS", missing), self.assertRaises(settings.DatasUnavailable):
                settings.cache_dir()
            self.assertFalse((Path(tmp) / "BSM" / APP_NAME / "cache").exists())

    def test_the_variable_leaves_no_mark(self):
        with tempfile.TemporaryDirectory() as tmp, without_home(APPDATA=tmp, BSM_DATAS=str(Path(tmp) / "given")):
            with patch.object(settings, "_ADOPTED", []):
                settings.cache_dir()
            self.assertFalse((Path(tmp) / "BSM" / APP_NAME / settings.DATAS_MARK).exists())

    def test_the_cache_is_copied_once_never_over_what_is_there_and_the_old_one_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            roaming, datas = Path(tmp) / "roaming", Path(tmp) / "datas"
            old = roaming / "BSM" / APP_NAME / "cache"
            (old / "anlz").mkdir(parents=True)
            (old / "anlz" / "a.dat").write_bytes(b"analysis")
            (old / "wave").mkdir()
            (old / "wave" / "w.bin").write_bytes(b"wave")
            (datas / APP_NAME / "cache" / "wave").mkdir(parents=True)
            (datas / APP_NAME / "cache" / "wave" / "kept.bin").write_bytes(b"already there")
            with without_home(APPDATA=str(roaming), BSM_DATAS=str(datas)), patch.object(settings, "_ADOPTED", []):
                d = settings.cache_dir()
            self.assertEqual(d, datas / APP_NAME / "cache")
            self.assertEqual((d / "anlz" / "a.dat").read_bytes(), b"analysis")
            self.assertTrue((d / "wave" / "kept.bin").is_file() and not (d / "wave" / "w.bin").exists(), "an entry already on D: is left alone")
            self.assertTrue((old / "anlz" / "a.dat").is_file(), "the old copy is kept")
            self.assertEqual([p.name for p in d.iterdir() if p.name.endswith(".part")], [])

    def test_a_missing_disk_is_said_and_nothing_is_made(self):
        missing = "Q:\\Datas" if not os.path.exists("Q:\\") else "Z:\\Datas"
        if os.path.exists(missing[:3]):
            self.skipTest("the test disk exists on this machine")
        with without_home(BSM_DATAS=missing):
            with self.assertRaises(settings.DatasUnavailable) as caught:
                settings.cache_dir()
        self.assertIn("Datas", str(caught.exception))
        self.assertIsInstance(caught.exception, OSError)


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
