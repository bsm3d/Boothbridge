# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the never-decrypt rule for files the program reads, using sqlite and regex checks on test inputs.

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import djapp
from djapp import protected, sources


# the program never opens an encrypted database, and no code that would is allowed back in
class NeverDecryptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make(self, name: str, data: bytes) -> str:
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    def test_the_databases_of_rekordbox_are_refused_by_their_names(self):
        for name in ("master.db", "exportLibrary.db", "MASTER.DB"):
            path = self.make(name, protected.MAGIC + bytes(100))
            with self.subTest(name=name):
                self.assertTrue(protected.encrypted(path))
                with self.assertRaisesRegex(ValueError, "never opens, reads or decrypts"):
                    sources.load(path)

    def test_a_database_file_without_the_sqlite_header_is_refused(self):
        path = self.make("library.db", bytes(range(256)) * 4)
        with self.assertRaisesRegex(ValueError, "encrypted or private library"):
            sources.load(path)

    def test_the_binary_libraries_of_apple_are_refused_and_the_message_points_to_the_export(self):
        for name in ("iTunes Library.itl", "Music Library.musiclibrary"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Export Library"):
                sources.load(self.make(name, b"binary"))

    def test_a_plain_sqlite_database_is_not_refused(self):
        path = str(self.root / "mixxxdb.sqlite")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE t (id INTEGER)")
        con.commit()
        con.close()
        self.assertFalse(protected.encrypted(path))

    def test_other_files_and_missing_files_are_not_refused(self):
        self.assertFalse(protected.encrypted(self.make("list.m3u8", b"#EXTM3U")))
        self.assertFalse(protected.encrypted(self.make("empty.db", b"")))
        self.assertFalse(protected.encrypted(str(self.root / "missing.db")))

    def test_no_source_file_carries_code_to_decrypt_a_database(self):
        forbidden = re.compile(r"sqlcipher|pragma\s+key|cipher_compat|402fd482|master[.]db", re.IGNORECASE)
        package = Path(djapp.__file__).parent
        found = []
        for path in package.rglob("*.py"):
            if path.name == "protected.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if forbidden.search(text):
                found.append(str(path.relative_to(package)))
        self.assertEqual(found, [])

    def test_no_requirement_asks_for_a_decryption_library(self):
        for path in Path(djapp.__file__).parent.parent.glob("requirements*.txt"):
            self.assertNotRegex(path.read_text(encoding="utf-8", errors="replace"), r"(?i)sqlcipher")


if __name__ == "__main__":
    unittest.main()
