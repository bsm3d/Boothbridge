import os
import random
import shutil
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf
from djapp import tagfile, tagwrite
from djapp.sources import engine

from tests.test_engine import beat_data, loops, make_library, q_compress, quick_cues, track_data

CASES = int(os.environ.get("BB_FUZZ_CASES", "120"))
ALLOWED = (ValueError, OSError)     # TagError is a ValueError, a damaged file is an error with a name and nothing else


def mutate(data: bytes, rng: random.Random) -> bytes:
    data = bytearray(data)
    for _ in range(rng.choice((1, 1, 2, 4, 8))):
        kind = rng.choice(("flip", "set", "cut", "grow", "zero", "dup"))
        if not data:
            break
        at = rng.randrange(len(data))
        if kind == "flip":
            data[at] ^= 1 << rng.randrange(8)
        elif kind == "set":
            data[at] = rng.choice((0, 0xFF, 0x7F, 0x80, rng.randrange(256)))
        elif kind == "cut":
            del data[at:]
        elif kind == "grow":
            data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
        elif kind == "zero":
            data[at:at + rng.randrange(1, 64)] = bytes(rng.randrange(1, 64))
        else:
            data[at:at + 8] = data[:8]
    return bytes(data)


def atom(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


# whatever a file holds, the tag code answers with a value or with an error that has a name, never hangs, and a failed
# write leaves the file as it was
class TagFuzzTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        samples = (np.sin(np.arange(22050) * 0.13) * 0.2).astype(np.float32)
        self.originals = {}
        for ext in (".wav", ".aiff", ".flac", ".ogg", ".mp3"):
            path = self.root / ("seed" + ext)
            sf.write(str(path), samples, 44100)
            tagwrite.write(str(path), {"title": "Seed", "artist": "A", "year": "2020", "track_no": "2"})
            self.originals[ext] = path.read_bytes()
        mvhd = atom(b"mvhd", bytes(12) + struct.pack(">II", 1000, 4000) + bytes(80))
        stco = atom(b"stco", bytes(4) + struct.pack(">II", 1, 4000))
        trak = atom(b"trak", atom(b"mdia", atom(b"minf", atom(b"stbl", stco))))
        self.originals[".m4a"] = atom(b"ftyp", b"M4A \0\0\0\0M4A ") + atom(b"moov", mvhd + trak) + atom(b"mdat", bytes(4000))

    def run_one(self, ext: str, data: bytes):
        path = self.root / f"fuzz{ext}"
        path.write_bytes(data)
        try:
            tagged = tagfile.open_tagged(str(path))
            tagged.get("title")
            tagged.set("title", "New é")
            tagged.set("track_no", "5")
            tagged.set("bpm", "123.5")
            tagged.save()
            tagfile.open_tagged(str(path)).get("title")
        except ALLOWED:
            pass
        try:
            tagfile.identify(str(path))
        except ALLOWED:
            pass
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".bb-tagfile-")]
        self.assertEqual(leftovers, [], f"a temporary file was left by a {ext} write")

    def test_damaged_audio_files_never_raise_anything_but_an_error_with_a_name(self):
        for ext, original in self.originals.items():
            rng = random.Random(f"tags{ext}")
            for i in range(CASES):
                data = mutate(original, rng)
                with self.subTest(ext=ext, case=i):
                    self.run_one(ext, data)

    def test_a_failed_write_never_changes_the_file(self):
        for ext, original in self.originals.items():
            rng = random.Random(f"keep{ext}")
            for i in range(CASES // 2):
                data = mutate(original, rng)
                path = self.root / f"keep{ext}"
                path.write_bytes(data)
                try:
                    tagged = tagfile.open_tagged(str(path))
                    tagged.set("comment", "x" * rng.choice((10, 5000, 70000)))
                    tagged.save()
                except ALLOWED:
                    with self.subTest(ext=ext, case=i):
                        self.assertEqual(path.read_bytes(), data)


# ------------------------------------------------------------
class EngineFuzzTests(unittest.TestCase):
    def test_the_blob_readers_never_raise(self):
        good = [beat_data(), quick_cues({0: ("a", 1000.0, (1, 2, 3))}, main=500.0), q_compress(loops({0: ("l", 10.0, 90.0)})),
                loops({0: ("l", 10.0, 90.0)}), track_data()]
        rng = random.Random("engine")
        readers = (lambda b: engine.read_beat_data(b), lambda b: engine.read_quick_cues(b, 44100.0, 8_000_000),
                   lambda b: engine.read_loops(b, 44100.0, 8_000_000), lambda b: engine.read_track_data(b), engine.unpack)
        for i in range(CASES * 4):
            raw = rng.choice(good)
            # mutate inside the compressed stream too: unpack, change, pack again
            body = engine.unpack(raw) or raw
            blob = mutate(body, rng) if rng.random() < 0.5 else mutate(raw, rng)
            if rng.random() < 0.5:
                blob = q_compress(blob)
            for n, read in enumerate(readers):
                with self.subTest(case=i, reader=n):
                    read(blob)

    def test_random_bytes_are_nothing(self):
        rng = random.Random(7)
        for _ in range(CASES):
            blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
            self.assertIsNone(engine.read_beat_data(blob))
            self.assertEqual(engine.read_quick_cues(blob, 44100.0, 1e7), [])

    def test_a_damaged_database_is_an_error_with_a_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_library(Path(tmp))
            original = db.read_bytes()
            rng = random.Random("db")
            for i in range(CASES):
                data = mutate(original, rng)
                path = Path(tmp) / "case" / "Engine Library" / "Database2" / "m.db"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                with self.subTest(case=i):
                    try:
                        from djapp import sources
                        sources.detect(str(path))
                        sources.load(str(path))
                    except ValueError:
                        pass
                    except sqlite3.Error as e:
                        self.fail(f"a sqlite error leaked out of the reader: {e}")
                shutil.rmtree(path.parent.parent.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
