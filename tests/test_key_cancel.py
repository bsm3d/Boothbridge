# cancelling the read of the database on a key: key_summary and the page reader stop between
# pages, the stamp tells whether a summary is still current

import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from djapp import export
from djapp.export.cdj import keymerge, pdbread
from djapp.export.manifest import Cancelled
from djapp.model import Node
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp


# ------------------------------------------------------------
# counts the pages pdbread reads
class Pages:
    def __init__(self):
        self.count = 0
        self.real = pdbread.read_page

    def __call__(self, data, idx):
        self.count += 1
        return self.real(data, idx)


# ------------------------------------------------------------
class CancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp, cls.root = tmp()
        music, cache = cls.root / "music", cls.root / "cache"
        music.mkdir()
        cache.mkdir()
        tracks = [make_track(music, f"t{i}", i, title=f"Title {i} " + "x" * 40) for i in range(120)]
        cls.key = cls.root / "key"
        export_to([Node("playlist", "Big", tracks=tracks)], cls.key, cache)
        cls.db = cls.key.joinpath(*key.DB)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def pages(self, **kw):
        spy = Pages()
        with patch.object(pdbread, "read_page", spy):
            try:
                keymerge.key_summary(str(self.key), **kw)
            except Cancelled:
                pass
        return spy.count

    def test_the_database_has_many_pages_to_read(self):
        self.assertGreater(self.pages(), 8)


    def test_a_set_event_reads_no_page(self):
        stop = threading.Event()
        stop.set()
        spy = Pages()
        with patch.object(pdbread, "read_page", spy), self.assertRaises(Cancelled):
            keymerge.key_summary(str(self.key), stop)
        self.assertEqual(spy.count, 0)

    def test_it_stops_in_the_middle_of_the_tables(self):
        stop = threading.Event()
        spy = Pages()

        def after_three(data, idx):
            if spy.count == 3:
                stop.set()
            return spy(data, idx)
        with patch.object(pdbread, "read_page", after_three), self.assertRaises(Cancelled):
            keymerge.key_summary(str(self.key), stop)
        self.assertEqual(spy.count, 4)
        self.assertLess(spy.count, self.pages())

    def test_an_event_that_is_not_set_changes_nothing(self):
        plain = keymerge.key_summary(str(self.key))
        self.assertEqual(keymerge.key_summary(str(self.key), threading.Event()), plain)
        self.assertEqual(export.key_summary(str(self.key), threading.Event()), plain)
        self.assertEqual((plain.playlists, plain.tracks), (1, 120))

    def test_the_file_is_read_in_pieces_that_can_be_stopped(self):
        stop = threading.Event()
        seen = []
        real = keymerge.read_db

        def spy(db, cancel):
            seen.append(cancel)
            return real(db, cancel)
        with patch.object(keymerge, "CHUNK", 4096), patch.object(keymerge, "read_db", spy):
            self.assertEqual(keymerge.key_summary(str(self.key), stop).tracks, 120)
        self.assertEqual(seen, [stop])
        with patch.object(keymerge, "CHUNK", 4096):
            self.assertEqual(keymerge.read_db(str(self.db), threading.Event()), self.db.read_bytes())
            stop.set()
            with self.assertRaises(Cancelled):
                keymerge.read_db(str(self.db), stop)

    def test_a_database_that_cannot_be_read_is_still_a_summary_not_a_cancel(self):
        broken = self.root / "broken"
        target = broken.joinpath(*key.DB)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not a database")
        got = keymerge.key_summary(str(broken), threading.Event())
        self.assertFalse(got.readable)

    def test_the_page_reader_checks_before_each_page(self):
        data = self.db.read_bytes()
        stop = threading.Event()
        self.assertTrue(pdbread.read(data, None, stop).pages)
        stop.set()
        spy = Pages()
        with patch.object(pdbread, "read_page", spy), self.assertRaises(Cancelled):
            pdbread.read(data, None, stop)
        self.assertEqual(spy.count, 0)


# ------------------------------------------------------------
class StampTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.db = self.root.joinpath("k", *key.DB)
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"a" * 100)
        self.out = str(self.root / "k")

    def test_none_without_a_database(self):
        self.assertIsNone(keymerge.key_stamp(str(self.root)))
        self.assertIsNone(export.key_stamp(str(self.root / "nowhere")))

    def test_it_names_the_file_with_its_size_and_time(self):
        st = os.stat(self.db)
        self.assertEqual(export.key_stamp(self.out), (str(self.db), 100, st.st_mtime_ns))

    def test_a_new_size_or_a_new_time_changes_it(self):
        first = keymerge.key_stamp(self.out)
        self.db.write_bytes(b"a" * 101)
        second = keymerge.key_stamp(self.out)
        self.assertNotEqual(first, second)
        st = os.stat(self.db)
        os.utime(self.db, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertNotEqual(keymerge.key_stamp(self.out), second)

    def test_the_stamp_is_the_same_for_the_root_and_the_file(self):
        self.assertEqual(keymerge.key_stamp(self.out), keymerge.key_stamp(str(Path(self.db))))


if __name__ == "__main__":
    unittest.main()
