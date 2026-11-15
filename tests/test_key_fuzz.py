# damaged, cut and scrambled keys: the readers return or raise their own error, nothing else,
# and never hang. Every loop is seeded and prints its seed when a case fails

import os
import random
import shutil
import struct
import threading
import time
import unittest
from pathlib import Path

from djapp import export
from djapp.export.cdj import (
    anlz,
    anlzread,
    devicesql,
    pdbread,
    pdbwrite,
    reference,
)
from djapp.model import Cue, Node, Track
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp

CASES = int(os.environ.get("BB_FUZZ_CASES") or 320)  # raise it for a longer run
LIMIT = 240  # seconds for a whole loop
PAGE = devicesql.PAGE


# ----------------------------------------------------------
class Audio:
    ok = True

    def __init__(self):
        import numpy as np
        self.fine = [np.ones(150)]

    def at(self, n):
        import numpy as np
        from djapp.analysis import Bands
        v = np.ones(n, dtype=np.float32) * .5
        return Bands(44100, v, v, v)


# ------------------------------------------------------------------------
# some bits turned over, in the first zone bytes (the headers) when zone is given
def flips(data: bytes, rng: random.Random, zone: int = 0) -> bytes:
    b = bytearray(data)
    if not b:
        return bytes(b)
    for _ in range(rng.randrange(1, 17)):
        at = rng.randrange(min(len(b), zone) if zone else len(b))
        b[at] ^= 1 << rng.randrange(8)
    return bytes(b)


# blocks of the file swapped or repeated; unit is the block size, random when 0
def scramble(data: bytes, rng: random.Random, unit: int = 0) -> bytes:
    b = bytearray(data)
    size = unit or rng.choice((4, 8, 12, 28, 64, 256))
    blocks = [b[i:i + size] for i in range(0, len(b), size)]
    for _ in range(rng.randrange(1, 6)):
        i, j = rng.randrange(len(blocks)), rng.randrange(len(blocks))
        if rng.random() < 0.7:
            blocks[i], blocks[j] = blocks[j], blocks[i]
        else:
            blocks[i] = blocks[j]
    return b"".join(blocks)


# ----------------------------------------------------------
def mutate(data: bytes, rng: random.Random, unit: int = 0, zone: int = 0) -> tuple[str, bytes]:
    kind = rng.choice(("cut", "flip", "flip", "shuffle", "cut+flip", "insert", "fill"))
    if kind == "cut":
        return kind, data[:rng.randrange(len(data))]
    if kind == "flip":
        return kind, flips(data, rng, zone)
    if kind == "shuffle":
        return kind, scramble(data, rng, unit)
    if kind == "cut+flip":
        return kind, flips(data[:rng.randrange(1, len(data))], rng, zone)
    if kind == "insert":
        at = rng.randrange(len(data))
        return kind, data[:at] + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40))) + data[at:]
    at = rng.randrange(len(data))
    return kind, data[:at] + bytes([rng.choice((0, 255))]) * rng.randrange(1, 300) + data[at:]


# ----------------------------------------------------------
# changes that keep the file a whole number of pages, so the damage reaches the rows
# instead of being turned away at the door, or any of the generic ones
def pdb_mutate(data: bytes, rng: random.Random) -> tuple[str, bytes]:
    pages = len(data) // PAGE
    kind = rng.choice(("fields", "fields", "page flips", "page swap", "whole pages", "generic", "generic"))
    b = bytearray(data)
    if kind == "fields":
        for _ in range(rng.randrange(1, 5)):
            page = rng.randrange(pages)
            at = page * PAGE + rng.choice((rng.randrange(0x28), 0x28 + rng.randrange(1500), PAGE - 1 - rng.randrange(120)))
            width = rng.choice((1, 2, 4))
            at -= at % width
            b[at:at + width] = rng.choice((0, 1, 0xFF, 0x7FFF, 0xFFFF, 0xFFFFFFFF, rng.randrange(1 << 32))).to_bytes(
                4, "little")[:width]
    elif kind == "page flips":
        for _ in range(rng.randrange(1, 9)):
            at = rng.randrange(pages) * PAGE + rng.choice((rng.randrange(1600), PAGE - 1 - rng.randrange(200)))
            b[at] ^= 1 << rng.randrange(8)
    elif kind == "page swap":
        i, j = rng.randrange(pages), rng.randrange(pages)
        b[i * PAGE:(i + 1) * PAGE], b[j * PAGE:(j + 1) * PAGE] = b[j * PAGE:(j + 1) * PAGE], b[i * PAGE:(i + 1) * PAGE]
    elif kind == "whole pages":
        return kind, bytes(b[:rng.randrange(1, pages) * PAGE])
    else:
        return mutate(data, rng, unit=rng.choice((0, PAGE)), zone=rng.choice((0, PAGE * 3, 200)))
    return kind, bytes(b)


# ----------------------------------------------------------
class Loop(unittest.TestCase):
    # case(rng) does one thing, allowed are the exceptions it may raise. A thread runs
    # the loop so that a case that never ends fails the test instead of freezing it
    def run_cases(self, name, seeds, case, allowed):
        failure: list[str] = []
        done = threading.Event()

        def work():
            try:
                for seed in seeds:
                    for i in range(CASES):
                        rng = random.Random(seed * 1_000_003 + i)
                        t0 = time.monotonic()
                        try:
                            case(rng)
                        except allowed:
                            pass
                        except BaseException as e:  # any other error is the failure  # noqa: BLE001
                            failure.append(f"{name}: seed={seed} case={i} raised {type(e).__name__}: {e}")
                            return
                        if time.monotonic() - t0 > 5:
                            failure.append(f"{name}: seed={seed} case={i} took {time.monotonic() - t0:.1f}s")
                            return
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(LIMIT):
            self.fail(f"{name}: did not finish in {LIMIT}s")
        self.assertEqual(failure, [])


# ----------------------------------------------------------
class FuzzTests(Loop):
    @classmethod
    def setUpClass(cls):
        cls.temp, cls.root = tmp()
        music = cls.root / "music"
        music.mkdir()
        cache = cls.root / "cache"
        cache.mkdir()
        tracks = [make_track(music, f"f{i}", i, title=f"Fuzz é {i}", artist=f"Artist {i % 2}", album="Alb",
                             bpm=125.0 + i, grid=33.0 + i, key=i, genre="House", label="L",
                             cues=[Cue(name="Intro", start=300, hotcue=0), Cue(kind=5, start=900, length=400, hotcue=-1)])
                  for i in range(4)]
        tree = [Node("folder", "Folder", children=[Node("playlist", "One", tracks=tracks[:3])]),
                Node("playlist", "Two", tracks=tracks[1:])]
        cls.good = cls.root / "key"
        export_to(tree, cls.good, cache)
        cls.db = (cls.good / "PIONEER" / "rekordbox" / "export.pdb").read_bytes()
        cls.anlz_files = sorted((cls.good / "PIONEER" / "USBANLZ").rglob("ANLZ0000.*"))
        t = Track(bpm=128.0, grid=95.0, duration=9, cues=[Cue(name="Café", start=1500, hotcue=1),
                                                         Cue(kind=5, start=3000, length=1000, hotcue=-1)])
        cls.dat, cls.ext = anlz.build_py(t, "/Contents/A/B/x.wav", Audio())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = self.root / "work"
        shutil.rmtree(self.work, ignore_errors=True)
        shutil.copytree(self.good, self.work)
        self.db_path = self.work / "PIONEER" / "rekordbox" / "export.pdb"

    def test_a_damaged_anlz_file_is_an_anlz_error_or_a_result(self):
        for name, data in (("DAT", self.dat), ("EXT", self.ext)):
            def case(rng, data=data):
                got = anlzread.parse(mutate(data, rng, zone=rng.choice((0, 0, 400)))[1])
                self.assertIsInstance(got.cues, list)

            self.run_cases(f"anlz {name}", (1, 2), case, anlzread.AnlzError)

    def test_a_damaged_database_is_a_load_error_or_a_result(self):
        def case(rng):
            self.db_path.write_bytes(pdb_mutate(self.db, rng)[1])
            got = key.read(str(self.work))
            self.assertIsInstance(got.nodes, list)

        self.run_cases("export.pdb", (1, 2), case, ValueError)

    def test_what_the_load_error_says_names_the_file(self):
        self.db_path.write_bytes(self.db[:5000])
        with self.assertRaisesRegex(key.KeyReadError, "export.pdb is damaged"):
            key.read(str(self.work))

    def test_the_summary_of_a_damaged_database_is_a_summary_either_way(self):
        def case(rng):
            self.db_path.write_bytes(pdb_mutate(self.db, rng)[1])
            s = export.key_summary(str(self.work))
            self.assertEqual(s.readable, not s.error)

        self.run_cases("summary", (3,), case, ())

    def test_a_damaged_analysis_file_on_the_key_never_stops_the_load(self):
        files = [self.work / p.relative_to(self.good) for p in self.anlz_files]

        def case(rng):
            for f in files:
                f.write_bytes((self.good / f.relative_to(self.work)).read_bytes())
            for f in rng.sample(files, rng.randrange(1, 4)):
                f.write_bytes(mutate(f.read_bytes(), rng, zone=rng.choice((0, 400)))[1])
            got = key.read(str(self.work))
            self.assertEqual(len(got.nodes), 2)

        self.run_cases("anlz on the key", (4,), case, ())

    def test_keep_on_a_damaged_database_refuses_and_touches_nothing_or_exports_fully(self):
        music = self.root / "music"
        node = Node("playlist", "Extra", tracks=[make_track(music, "extra", 99)])
        cache = self.root / "cache"
        rng = random.Random(5)
        for i in range(12):
            data = pdb_mutate(self.db, rng)[1]
            shutil.rmtree(self.work)
            shutil.copytree(self.good, self.work)
            self.db_path.write_bytes(data)
            before = {p: p.read_bytes() for p in self.work.rglob("*") if p.is_file()}
            try:
                rep = export_to([node], self.work, cache)
            except ValueError:
                self.assertEqual({p: p.read_bytes() for p in self.work.rglob("*") if p.is_file()}, before, f"case {i}")
            else:
                self.assertEqual(rep.errors, [], f"case {i}")
                self.assertIn("Extra", [n.name for n in key.read(str(self.work)).nodes])

# ----------------------------------------------------------
# databases built to be awkward, with the structure of a real one
class HostileDatabases(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)

    def key_with(self, tables: dict[int, list[bytes]]) -> str:
        db = self.root / "PIONEER" / "rekordbox"
        db.mkdir(parents=True, exist_ok=True)
        (db / "export.pdb").write_bytes(devicesql.build_py(tables, reference.pages(), {0, 2, 3}))
        return str(self.root)

    def test_loops_and_missing_parents_in_the_playlist_tree(self):
        rows = [pdbwrite.tree_row(2, 0, 1, True, "a"), pdbwrite.tree_row(1, 0, 2, True, "b"),
                pdbwrite.tree_row(77, 0, 3, False, "orphan"), pdbwrite.tree_row(0, 0, 4, False, "fine"),
                pdbwrite.tree_row(0, 1, 4, False, "same id twice")]
        got = key.read(self.key_with({pdbread.PL_TREE: rows}))
        self.assertEqual([n.name for n in got.nodes], ["fine"])
        self.assertTrue(any("could not be placed" in w for w in got.warnings))

    def test_a_tree_thousands_of_levels_deep_is_cut_not_followed(self):
        rows = [pdbwrite.tree_row(i, 0, i + 1, True, f"f{i}") for i in range(4000)]
        got = key.read(self.key_with({pdbread.PL_TREE: rows}))
        depth, level = 0, got.nodes
        while level:
            depth += 1
            level = level[0].children
        self.assertEqual(depth, key.MAX_DEPTH + 1)

    def test_entries_that_point_nowhere(self):
        rows = [pdbwrite.tree_row(0, 0, 1, False, "P")]
        ents = [struct.pack("<III", 1, 999, 1), struct.pack("<III", 1, 1, 555)]
        got = key.read(self.key_with({pdbread.PL_TREE: rows, pdbread.PL_ENTRIES: ents}))
        self.assertEqual(got.nodes[0].tracks, [])
        self.assertTrue(any("refer to tracks" in w for w in got.warnings))

    def test_rows_that_are_too_short_or_have_strings_outside_themselves(self):
        for row in (bytes(0x40), bytes(0x88)[:0x5e] + struct.pack("<21H", *([0xFFF0] * 21)) + bytes(4)):
            with self.subTest(len=len(row)), self.assertRaises(key.KeyReadError):
                key.read(self.key_with({pdbread.TRACKS: [row]}))
        long_string = bytes([0x90]) + struct.pack("<H", 60000) + b"\0" + b"ab"
        with self.assertRaises(key.KeyReadError):
            key.read(self.key_with({pdbread.PL_TREE: [struct.pack("<5I", 0, 0, 0, 1, 0) + long_string]}))

    def test_a_page_that_claims_thousands_of_rows(self):
        path = Path(self.key_with({pdbread.PL_TREE: [pdbwrite.tree_row(0, 0, 1, False, "P")]}))
        db = path / "PIONEER" / "rekordbox" / "export.pdb"
        data = bytearray(db.read_bytes())
        page = 2 + 2 * pdbread.PL_TREE
        data[page * PAGE + 0x18:page * PAGE + 0x1b] = (8191).to_bytes(3, "little")
        db.write_bytes(bytes(data))
        with self.assertRaises(key.KeyReadError):
            key.read(str(path))

    def test_two_rows_at_one_place_are_refused(self):
        path = Path(self.key_with({pdbread.PL_TREE: [pdbwrite.tree_row(0, 0, 1, False, "P"),
                                                      pdbwrite.tree_row(0, 1, 2, False, "Q")]}))
        db = path / "PIONEER" / "rekordbox" / "export.pdb"
        data = bytearray(db.read_bytes())
        page = (2 + 2 * pdbread.PL_TREE) * PAGE
        data[page + PAGE - 8:page + PAGE - 6] = data[page + PAGE - 6:page + PAGE - 4]
        db.write_bytes(bytes(data))
        with self.assertRaisesRegex(key.KeyReadError, "same place"):
            key.read(str(path))


if __name__ == "__main__":
    unittest.main()
