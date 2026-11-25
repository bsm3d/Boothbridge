# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the Engine library reader and writer against synthetic databases, track data and compressed blobs.

import hashlib
import os
import sqlite3
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from djapp import protected, sources
from djapp.model import LOOP, unique_tracks
from djapp.sources import engine

UUID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"

TRACK_COLUMNS_2 = """id INTEGER PRIMARY KEY AUTOINCREMENT, playOrder INTEGER, length INTEGER, bpm INTEGER, year INTEGER, path TEXT,
 filename TEXT, bitrate INTEGER, bpmAnalyzed REAL, albumArtId INTEGER, fileBytes INTEGER, title TEXT, artist TEXT, album TEXT,
 genre TEXT, comment TEXT, label TEXT, composer TEXT, remixer TEXT, key INTEGER, rating INTEGER, albumArt TEXT,
 timeLastPlayed DATETIME, isPlayed BOOLEAN, fileType TEXT, isAnalyzed BOOLEAN, dateCreated DATETIME, dateAdded DATETIME,
 isAvailable BOOLEAN, streamingSource TEXT, uri TEXT, originDatabaseUuid TEXT, originTrackId INTEGER"""
BLOBS = "trackData BLOB, overviewWaveFormData BLOB, beatData BLOB, quickCues BLOB, loops BLOB"


# ------------------------------------------------------------
# the blobs as I understand the public documentation of the format, written here from scratch
def q_compress(data: bytes, prefix: str = ">") -> bytes:
    return struct.pack(prefix + "I", len(data)) + zlib.compress(data)


def track_data(rate=44100.0, samples=44100 * 180, key=0, little=False) -> bytes:
    e = "<" if little else ">"
    return q_compress(struct.pack(e + "dQdi", rate, samples, 0.5, key))


def beat_data(rate=44100.0, samples=44100 * 180, bpm=120.0, first_beat_ms=250.0, little=False, count_little=False) -> bytes:
    beat = rate * 60.0 / bpm
    first = first_beat_ms / 1000.0 * rate
    he = "<" if little else ">"
    ce = "<" if count_little else ">"
    out = struct.pack(he + "ddB", rate, samples, 1)
    for _ in range(2):
        marks = [(first - 4 * beat, -4), (first + 200 * beat, 200)]
        out += struct.pack(ce + "Q", len(marks))
        for offset, index in marks:
            out += struct.pack("<dqii", offset, index, 196 if index == -4 else 0, 0)
    return q_compress(out)


def quick_cues(cues: dict[int, tuple[str, float, tuple[int, int, int]]], main=None, little=False) -> bytes:
    e = "<" if little else ">"
    out = struct.pack(e + "Q", 8)
    for slot in range(8):
        if slot in cues:
            name, pos, (r, g, b) = cues[slot]
            out += bytes([len(name.encode())]) + name.encode() + struct.pack(e + "d", pos) + bytes([255, r, g, b])
        else:
            out += b"\0" + struct.pack(e + "d", -1.0) + bytes(4)
    out += struct.pack(e + "d", main if main is not None else -1.0) + bytes([1 if main is not None else 0]) + struct.pack(e + "d", 0.0)
    return q_compress(out)


def loops(items: dict[int, tuple[str, float, float]], little=True) -> bytes:
    e = "<" if little else ">"
    out = bytes([8]) + bytes(7)
    for slot in range(8):
        if slot in items:
            name, start, end = items[slot]
            out += bytes([len(name.encode())]) + name.encode() + struct.pack(e + "dd", start, end) + bytes([1, 1]) + bytes([255, 0, 255, 0])
        else:
            out += b"\0" + struct.pack(e + "dd", -1.0, -1.0) + bytes([0, 0]) + bytes(4)
    return out


# ------------------------------------------------------------
def make_library(root: Path, version: int = 2, **blob_options) -> Path:
    lib = root / "Engine Library" / "Database2"
    lib.mkdir(parents=True)
    db = lib / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE Information (id INTEGER PRIMARY KEY, uuid TEXT, schemaVersionMajor INTEGER, schemaVersionMinor INTEGER, "
                "schemaVersionPatch INTEGER, currentPlayedIndiciator INTEGER)")
    con.execute("INSERT INTO Information VALUES (1, ?, ?, 21, 0, 0)", (UUID, version))
    extra = "" if version == 3 else ", " + BLOBS
    con.execute(f"CREATE TABLE Track ({TRACK_COLUMNS_2}{extra})")
    con.execute("CREATE TABLE Playlist (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, parentListId INTEGER, isPersisted BOOLEAN, "
                "nextListId INTEGER, lastEditTime DATETIME, isExplicitlyExported BOOLEAN)")
    con.execute("CREATE TABLE PlaylistEntity (id INTEGER PRIMARY KEY AUTOINCREMENT, listId INTEGER, trackId INTEGER, databaseUuid TEXT, "
                "nextEntityId INTEGER, membershipReference INTEGER)")
    if version == 3:
        con.execute(f"CREATE TABLE PerformanceData (trackId INTEGER PRIMARY KEY, isAnalyzed BOOLEAN, {BLOBS}, hasExtraValues BOOLEAN)")

    def add(i, title, path, key, rating, **fields):
        blobs = {
            "trackData": track_data(key=key, little=blob_options.get("little", False)),
            "beatData": beat_data(bpm=120.0 + i, little=blob_options.get("little", False), count_little=blob_options.get("little", False)),
            "quickCues": quick_cues({0: ("Intro", 44100.0 * 2, (255, 0, 0)), 3: ("Drop", 44100.0 * 64, (0, 128, 255))}, main=44100.0 * 1.5,
                                    little=blob_options.get("little", False)),
            "loops": loops({1: ("Loop A", 44100.0 * 10, 44100.0 * 14)}),
            "overviewWaveFormData": b"\x01\x02",
        }
        blobs.update(fields.pop("blobs", {}))
        columns = dict(id=i, length=180, bpm=120 + i, year=2020 + i, path=path, filename=path.rsplit("/", 1)[-1], bitrate=320,
                       bpmAnalyzed=120.0 + i, fileBytes=7_000_000, title=title, artist=f"Artist {i}", album="Album", genre="House",
                       comment="note", label="Label", composer="Comp", remixer="Rem", key=key, rating=rating, timeLastPlayed=1_700_000_000,
                       isPlayed=1, dateAdded=1_690_000_000, dateCreated=1_690_000_000, originDatabaseUuid=UUID, originTrackId=i)
        columns.update(fields)
        if version != 3:
            columns.update(blobs)
        con.execute(f"INSERT INTO Track ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})", list(columns.values()))
        if version == 3:
            con.execute("INSERT INTO PerformanceData (trackId, isAnalyzed, trackData, overviewWaveFormData, beatData, quickCues, loops) "
                        "VALUES (?, 1, ?, ?, ?, ?, ?)", (i, blobs["trackData"], blobs["overviewWaveFormData"], blobs["beatData"],
                                                         blobs["quickCues"], blobs["loops"]))

    add(1, "One", "../Music/one.mp3", 1, 100)
    add(2, "Two", "../Music/sub/two.flac", 23, 60)
    add(3, "Three", "D:/Elsewhere/three.wav", 5, 0)
    add(4, "Cloud", "streaming/four", 0, 0, streamingSource="stream")
    add(5, "Five", "../Music/five.mp3", 99, 120, blobs={"quickCues": b"garbage", "beatData": b"x" * 40})
    # the tree: Root folder (id 10) holds A (11) and B (12); A before B through nextListId, C (13) is a list at the top
    for pid, title, parent, nxt in ((10, "Root", 0, 13), (11, "A", 10, 12), (12, "B", 10, 0), (13, "C", 0, 0), (14, "Empty", 0, 0)):
        con.execute("INSERT INTO Playlist (id, title, parentListId, isPersisted, nextListId) VALUES (?, ?, ?, 1, ?)", (pid, title, parent, nxt))
    # A: tracks 2, 1, 3 in that order by nextEntityId (stored out of order). B: one track of another database, found by origin
    entries = ((1, 11, 1, UUID, 3), (2, 11, 2, UUID, 1), (3, 11, 3, UUID, 0), (4, 12, 777, OTHER, 0), (5, 13, 5, UUID, 0), (6, 13, 4, UUID, 0))
    for eid, lid, tid, uuid, nxt in entries:
        con.execute("INSERT INTO PlaylistEntity (id, listId, trackId, databaseUuid, nextEntityId, membershipReference) VALUES (?, ?, ?, ?, ?, 0)",
                    (eid, lid, tid, uuid, nxt))
    con.execute("UPDATE Track SET originDatabaseUuid = ?, originTrackId = 777 WHERE id = 5", (OTHER,))
    con.commit()
    con.close()
    return db


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------
class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def names(self, nodes):
        return [(n.name, [c.name for c in n.children] if n.is_folder else [t.title for t in n.tracks]) for n in nodes]

    def test_the_tree_the_order_of_the_siblings_and_of_the_tracks(self):
        db = make_library(self.root)
        nodes = sources.load(str(db))
        self.assertEqual(self.names(nodes)[0], ("Root", ["A", "B"]))
        root = nodes[0]
        self.assertEqual([t.title for t in root.children[0].tracks], ["Two", "One", "Three"])
        self.assertEqual([t.title for t in root.children[1].tracks], ["Five"])   # found again by (database, id) against the origin
        self.assertEqual([t.title for t in nodes[1].tracks], ["Five"])           # C: the streaming track has no file and is left out
        self.assertEqual(nodes[1].name, "C")
        self.assertNotIn("Empty", [n.name for n in nodes])
        self.assertEqual([n.name for n in nodes], ["Root", "C", "All tracks"])
        self.assertEqual({t.title for t in nodes[-1].tracks}, {"One", "Two", "Three", "Five"})

    def test_the_tags_the_key_the_rating_and_the_paths(self):
        db = make_library(self.root)
        by = {t.title: t for t in unique_tracks(sources.load(str(db)))}
        one = by["One"]
        self.assertEqual((one.artist, one.album, one.genre, one.label, one.composer, one.remixer, one.comment),
                         ("Artist 1", "Album", "House", "Label", "Comp", "Rem", "note"))
        self.assertEqual((one.year, one.bitrate, one.size, one.duration, one.rating), (2021, 320, 7_000_000, 180, 5))
        self.assertEqual(one.path, os.path.normpath(str(self.root / "Engine Library" / "../Music/one.mp3")))
        self.assertEqual(by["Three"].path, os.path.normpath("D:/Elsewhere/three.wav"))
        self.assertEqual((by["One"].rating, by["Two"].rating, by["Three"].rating, by["Five"].rating), (5, 3, 0, 5))
        self.assertIsNotNone(one.key)
        self.assertEqual(one.added, "2023-07-22")

    def test_the_engine_keys_become_the_keys_of_the_program(self):
        from djapp import keys
        for index, name in ((0, "C"), (1, "Am"), (10, "B"), (11, "Abm"), (19, "Cm"), (23, "Dm")):
            self.assertEqual(keys.parse(engine.KEY_NAMES[index]), keys.parse(name))
        self.assertEqual(len(set(keys.parse(n) for n in engine.KEY_NAMES)), 24)

    def check_performance(self, version, **options):
        db = make_library(self.root / f"v{version}{options.get('little', False)}", version, **options)
        by = {t.title: t for t in unique_tracks(sources.load(str(db)))}
        one = by["One"]
        self.assertAlmostEqual(one.bpm, 121.0, places=3)
        self.assertAlmostEqual(one.grid, 250.0, places=2)
        hot = {c.hotcue: c for c in one.cues if c.hotcue >= 0}
        self.assertEqual({k: (c.name, round(c.start), c.color) for k, c in hot.items()},
                         {0: ("Intro", 2000, "#FF0000"), 3: ("Drop", 64000, "#0080FF")})
        memory = [c for c in one.cues if c.hotcue == -1 and not c.is_loop]
        self.assertEqual([round(c.start) for c in memory], [1500])
        saved = [c for c in one.cues if c.kind == LOOP]
        self.assertEqual([(c.name, round(c.start), round(c.length)) for c in saved], [("Loop A", 10000, 4000)])
        self.assertEqual(one.samplerate, 44100)
        return by

    def test_the_blobs_of_a_2_x_library(self):
        self.check_performance(2)

    def test_the_blobs_of_a_3_x_library_where_performance_data_is_a_table(self):
        self.check_performance(3)

    def test_the_same_blobs_written_the_other_way_round_are_read_too(self):
        # the byte order of the blobs is not something I could check on a real file, so the reader tries both
        self.check_performance(2, little=True)

    def test_a_blob_that_makes_no_sense_leaves_the_track_with_its_tags_and_no_cue(self):
        by = self.check_performance(2)
        five = by["Five"]
        self.assertEqual((five.title, five.grid), ("Five", None))
        self.assertFalse([c for c in five.cues if c.hotcue >= 0])
        self.assertAlmostEqual(five.bpm, 125.0)      # the analysed tempo of the Track row

    def test_a_cue_outside_the_track_is_dropped_not_kept_wrong(self):
        db = make_library(self.root)
        con = sqlite3.connect(db)
        bad = quick_cues({0: ("Far", 44100.0 * 99999, (1, 2, 3))})
        con.execute("UPDATE Track SET quickCues = ? WHERE id = 1", (bad,))
        con.commit()
        con.close()
        one = next(t for t in unique_tracks(sources.load(str(db))) if t.title == "One")
        self.assertFalse([c for c in one.cues if c.hotcue >= 0])

    def test_the_file_is_never_written(self):
        db = make_library(self.root)
        before = digest(db)
        before_files = sorted(p.name for p in db.parent.iterdir())
        sources.load(str(db))
        sources.load(str(db.parent.parent))
        self.assertEqual(digest(db), before)
        self.assertEqual(sorted(p.name for p in db.parent.iterdir()), before_files)

    def test_it_is_found_from_the_file_the_folders_and_the_drive(self):
        db = make_library(self.root)
        for path in (db, db.parent, db.parent.parent, self.root):
            with self.subTest(path=str(path)):
                self.assertEqual(sources.detect(str(path)), "engine")
                self.assertEqual(len(unique_tracks(sources.load(str(path)))), 4)

    def test_other_libraries_are_not_taken_for_it(self):
        mixxx = self.root / "mixxxdb.sqlite"
        con = sqlite3.connect(mixxx)
        con.execute("CREATE TABLE track_locations (id INTEGER)")
        con.commit()
        con.close()
        self.assertEqual(sources.detect(str(mixxx)), "mixxx")
        plain = self.root / "other"
        plain.mkdir()
        self.assertEqual(sources.detect(str(plain)), "folder")
        lookalike = self.root / "x" / "m.db"
        lookalike.parent.mkdir()
        con = sqlite3.connect(lookalike)
        con.execute("CREATE TABLE Other (id INTEGER)")
        con.commit()
        con.close()
        self.assertNotEqual(sources.detect(str(lookalike)), "engine")

    def test_a_file_that_is_not_sqlite_is_refused_and_never_decrypted(self):
        fake = self.root / "Engine Library" / "Database2" / "m.db"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(bytes(range(256)) * 8)
        self.assertNotEqual(sources.detect(str(fake)), "engine")
        with self.assertRaises(ValueError):
            sources.load(str(fake))
        self.assertTrue(protected.encrypted(str(fake)))

    def test_an_old_library_of_engine_prime_is_explained(self):
        old = self.root / "Engine Library" / "m.db"
        old.parent.mkdir(parents=True)
        con = sqlite3.connect(old)
        con.execute("CREATE TABLE Information (id INTEGER, uuid TEXT)")
        con.execute("CREATE TABLE Track (id INTEGER, path TEXT)")
        con.execute("CREATE TABLE Playlist (id INTEGER, title TEXT)")
        con.commit()
        con.close()
        with self.assertRaisesRegex(ValueError, "Engine Prime 1.x"):
            sources.load(str(old))

    def test_a_broken_database_is_an_error_with_a_name(self):
        db = make_library(self.root)
        data = bytearray(db.read_bytes())
        data[100:140] = bytes(40)
        data[4096:8192] = bytes(4096)
        db.write_bytes(bytes(data))
        try:
            sources.load(str(db))
        except ValueError as e:
            self.assertIn("m.db", str(e))
        except sqlite3.Error:
            self.fail("a sqlite error leaked out of the reader")

    def test_the_music_folder_finds_files_that_moved(self):
        db = make_library(self.root)
        music = self.root / "moved"
        music.mkdir()
        (music / "one.mp3").write_bytes(b"x")
        by = {t.title: t for t in unique_tracks(sources.load(str(db), str(music)))}
        self.assertEqual(Path(by["One"].path), music / "one.mp3")


class BlobTests(unittest.TestCase):
    def test_a_blob_with_or_without_the_length_in_front_and_garbage(self):
        data = b"hello" * 10
        self.assertEqual(engine.unpack(q_compress(data)), data)
        self.assertEqual(engine.unpack(zlib.compress(data)), data)
        self.assertEqual(engine.unpack(b"not a blob at all"), b"")
        self.assertEqual(engine.unpack(None), b"")
        self.assertEqual(engine.unpack(b""), b"")

    def test_a_bomb_is_not_unpacked(self):
        self.assertEqual(engine.unpack(q_compress(bytes(engine.MAX_BLOB + 1000))), b"")

    def test_the_grid_of_a_track_that_starts_before_zero_comes_back_in_the_first_bar(self):
        blob = beat_data(bpm=128.0, first_beat_ms=-100.0)
        bpm, first = engine.read_beat_data(blob)
        self.assertAlmostEqual(bpm, 128.0, places=3)
        self.assertTrue(0 <= first < 60000 / 128.0)
        self.assertAlmostEqual((first + 100.0) % (60000 / 128.0), 0.0, places=1)

    def test_a_grid_with_one_marker_or_nonsense_is_nothing(self):
        out = struct.pack(">ddB", 44100.0, 441000.0, 1) + struct.pack(">Q", 1) + struct.pack("<dqii", 0.0, 0, 0, 0)
        self.assertIsNone(engine.read_beat_data(q_compress(out)))
        self.assertIsNone(engine.read_beat_data(b""))
        self.assertIsNone(engine.read_beat_data(q_compress(bytes(100))))

    def test_a_database_on_a_share_is_a_path_for_sqlite(self):
        # file://nas/share/m.db was refused by SQLite as an invalid authority, before the file was even looked at
        self.assertEqual(sources.sqlite_uri(r"\\nas\share\m.db"), "file:////nas/share/m.db")
        self.assertTrue(sources.sqlite_uri(r"C:\a#b\m.db").startswith("file:///C:/a%23b/"))

    def test_the_track_data_needs_a_sample_rate_that_exists(self):
        self.assertEqual(engine.read_track_data(track_data(48000.0, 480000)), (48000.0, 480000))
        self.assertEqual(engine.read_track_data(track_data(48000.0, 480000, little=True)), (48000.0, 480000))
        self.assertIsNone(engine.read_track_data(track_data(3.5, 480000)))


if __name__ == "__main__":
    unittest.main()
