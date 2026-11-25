# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the shipped source tree for denied file names and the rules for text files.

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import djapp

ROOT = Path(djapp.__file__).resolve().parent.parent

# the names that must not be found in any file of the program
DENIED = [
    "traktor" + " bridge", "traktor" + "_bridge", "traktor" + "bridge", "crate" + "ferry", "sera" + "to",
    "github.com/" + "bsm3d", "ev" + "mmc", "djcue" + "bridge", "muta" + "gen",
]
TEXT_SUFFIX = (".py", ".md", ".txt", ".spec", ".bat", ".iss", ".svg", ".html", ".toml", ".cfg", ".ini", ".json")
SKIP_DIRS = {"__pycache__", ".git", ".ruff_cache", ".pytest_cache", "dist", "build", ".venv"}
SKIP_FILES = {"boothbridge.log", "boothbridge.json"}


def shipped() -> list[Path]:
    # the files git would take: tracked or new, not the ones .gitignore keeps out
    try:
        done = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                              capture_output=True, text=True, check=True, timeout=60)
        files = [ROOT / line for line in done.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError):
        files = list(ROOT.rglob("*"))
    return sorted(p for p in files if p.is_file() and not any(s in SKIP_DIRS for s in p.relative_to(ROOT).parts)
                  and p.name not in SKIP_FILES)


def text_files():
    for path in shipped():
        if path.suffix.lower() in TEXT_SUFFIX:
            yield path, path.read_text(encoding="utf-8")


# what is in the source tree is what ships: nothing of the past, nothing personal, nothing of anyone else
class TreeTests(unittest.TestCase):
    def test_no_denied_name_and_no_long_dash_in_any_file(self):
        for path, text in text_files():
            low = text.lower()
            with self.subTest(path=str(path.relative_to(ROOT))):
                if path.relative_to(ROOT).parts[0] != "tests":
                    for word in DENIED:
                        self.assertNotIn(word, low)
                self.assertNotIn("\u2014", text)
                self.assertNotIn("\u2013", text)

    def test_no_personal_or_third_party_file_is_in_the_tree(self):
        names = {p.name.lower() for p in shipped()}
        for bad in (
            "export.pdb", "exportlibrary.db", "master.db", "exportext.pdb", "djprofile.nxs",
        ):
            self.assertNotIn(bad, names)
        for suffix in (".pdb", ".dat", ".ext", ".mp3", ".flac", ".wav", ".aiff", ".nml", ".png"):
            found = [p for p in shipped() if p.suffix.lower() == suffix and p.name != "icon.png"]
            self.assertEqual(found, [], suffix)

    def test_the_gitignore_keeps_the_personal_files_out(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for line in (
            "boothbridge.json",
            "*.log",
        ):
            self.assertIn(line, text)

    def test_no_licence_key_and_no_secret_is_in_the_tree(self):
        for path in shipped():
            self.assertNotEqual(path.suffix.lower(), ".key", str(path))
            self.assertNotIn("licence-private", path.name)
            self.assertNotEqual(path.name.lower(), "boothbridge.key")
            self.assertNotIn("licence_manager", path.name)

    def test_the_old_licence_files_are_not_back(self):
        names = {p.name for p in shipped()}
        for gone in ("locks.py", "ed25519.py", "licence.py", "licence_manager.py", "ui_locked.py"):
            self.assertNotIn(gone, names)

    def test_the_name_of_the_program_comes_from_one_place(self):
        from djapp import brand
        self.assertEqual(brand.SETTINGS_FILE, f"{brand.APP_SLUG}.json")
        for path in sorted((ROOT / "djapp").rglob("*.py")):
            if path.name == "brand.py":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for constant in (brand.SETTINGS_FILE, brand.LOG_FILE):
                    self.assertNotIn(f'"{constant}"', text)

    def test_every_python_file_compiles_and_has_no_leftover_marker(self):
        for path in sorted(ROOT.rglob("*.py")):
            if any(s in SKIP_DIRS for s in path.relative_to(ROOT).parts):
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(ROOT))):
                compile(text, str(path), "exec")
                self.assertNotIn("[feat" + "ure:", text)
                self.assertNotIn("[with" + "out:", text)
                self.assertNotIn("# [ba" + "se]", text)
                self.assertNotIn("# [/ba" + "se]", text)


# a new name is one command, and the tree still runs under it
class RenameTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("rename_tool", ROOT / "tools" / "rename.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_copy_of_the_tree_is_renamed_and_still_compiles(self):
        rename = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            import shutil
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv", "build", "dist", ".ruff_cache", "*.log"))
            edits, renames = rename.plan(tree, rename.spellings("Boothbridge", "Zzname"))
            for path, _raw, data in edits:
                path.write_bytes(data)
            for src, dst in renames:
                src.rename(dst)
            self.assertIn('APP_NAME = "Zzname"', (tree / "djapp" / "brand.py").read_text(encoding="utf-8"))
            self.assertTrue((tree / "zzname.spec").exists())
            for path in sorted(tree.rglob("*")):
                if path.is_file() and path.suffix.lower() in TEXT_SUFFIX and "boothbridge" in path.read_text(encoding="utf-8").lower():
                    self.fail(f"{path.relative_to(tree)} still names the old program")
            env = dict(os.environ, PYTHONPATH=str(tree), QT_QPA_PLATFORM="offscreen")
            done = subprocess.run([sys.executable, str(tree / "main.py"), "--help"], cwd=tree, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("Zzname --export", done.stdout)

    def test_the_new_name_is_checked(self):
        rename = self.load()
        for bad in ("", "a b", "1abc", "x" * 40, "ab-c"):
            self.assertIsNone(rename.VALID.match(bad))
        self.assertIsNotNone(rename.VALID.match("Booth2"))


if __name__ == "__main__":
    unittest.main()
