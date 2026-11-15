# Benoit Saint-Moulin
# Boothbridge : Denon Engine DJ reader (the Engine Library folder of a drive or of a computer)

# The library is a plain SQLite file, Engine Library/Database2/m.db, that Denon documents for third-party tools
# (Engine DJ support article "Support for Third-Party Database Tools"). It is only ever opened read-only, and a
# file that is not SQLite is refused by protected.py before this reader sees it. What is read:
#   Track          the tags, the path (relative to the Engine Library folder), the key (0-23), the rating (0-100)
#   Playlist       the tree, parentListId, the order of the siblings follows nextListId
#   PlaylistEntity the tracks of a list, in the order of nextEntityId. An entry of another database is found
#                  again by the pair (databaseUuid, trackId) against Track.originDatabaseUuid, originTrackId
#   PerformanceData (a view in 2.x, a table in 3.x; in older files the same columns are on Track): zlib blobs
#                  trackData, beatData, quickCues, loops. The layouts come from public documentation
#                  (the Mixxx wiki, Denon article) and not from a file of my own, so every number read from a
#                  blob is checked for sense. A blob that does not make sense leaves the track with its tags
#                  and no cues, never with a wrong cue
# The libraries of Engine Prime 1.x (m.db and p.db beside each other, titles in a MetaData table) are not read:
# Engine DJ 4 and later do not read them either

from __future__ import annotations

import logging
import math
import os
import sqlite3
import struct
import zlib
from pathlib import Path

from .. import keys
from ..model import CUE, LOOP, Cue, Node, Track
from . import Progress, num, relocator, sqlite_uri, unix_date

log = logging.getLogger(__name__)

MAX_DB = 8 << 30
MAX_BLOB = 64 << 20
MAX_TRACKS = 1_000_000
MAX_CUE_NAME = 255
TABLES = ("Track", "Playlist", "Information")
SAMPLE_RATES = range(8000, 384001)

# Engine key index to a key name, in the order of the Camelot wheel starting at 8B
KEY_NAMES = (
    "C",
    "Am",
    "G",
    "Em",
    "D",
    "Bm",
    "A",
    "F#m",
    "E",
    "Dbm",
    "B",
    "Abm",
    "F#",
    "Ebm",
    "Db",
    "Bbm",
    "Ab",
    "Fm",
    "Eb",
    "Cm",
    "Bb",
    "Gm",
    "F",
    "Dm",
)


# the m.db a path stands for: the file itself, the Database2 folder, the Engine Library folder or the drive
def find_db(path: str) -> Path | None:
    p = Path(path)
    try:
        if p.is_file():
            return p if p.name.lower() == "m.db" and is_sqlite(p) else None
        if not p.is_dir():
            return None
        for rel in (
            "m.db",
            "Database2/m.db",
            "Engine Library/Database2/m.db",
            "Engine Library/m.db",
        ):
            candidate = p / rel
            if candidate.is_file() and is_sqlite(candidate):
                return candidate
    except OSError:
        return None
    return None


def is_sqlite(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\0"
    except OSError:
        return False


def detect(path: str) -> bool:
    db = find_db(path)
    if db is None:
        return False
    try:
        con = connect(db)
        try:
            have = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return False
    return all(t in have for t in TABLES)


def connect(db: Path) -> sqlite3.Connection:
    uri = sqlite_uri(db)
    for options in ("?mode=ro", "?mode=ro&immutable=1"):
        con = None
        try:
            con = sqlite3.connect(uri + options, uri=True)
            con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error:
            if con is not None:
                con.close()
    raise ValueError(f"{db.name} cannot be opened for reading")


# ------------------------------------------------------------
# a zlib blob, with or without the 4-byte length that Qt puts in front of it. A blob that does not unpack, or that
# unpacks to more than MAX_BLOB, is nothing
def unpack(blob) -> bytes:
    if not isinstance(blob, (bytes, bytearray, memoryview)) or len(blob) < 6:
        return b""
    for start in (4, 0):
        try:
            d = zlib.decompressobj()
            data = d.decompress(bytes(blob[start:]), MAX_BLOB + 1)
            if len(data) <= MAX_BLOB:
                return data
        except zlib.error:
            continue
    return b""


class Reader:
    def __init__(self, data: bytes, little: bool = False):
        self.data, self.pos, self.little = data, 0, little

    def take(self, fmt: str, size: int):
        if self.pos + size > len(self.data):
            raise ValueError("short blob")
        value = struct.unpack_from(
            ("<" if self.little else ">") + fmt, self.data, self.pos
        )[0]
        self.pos += size
        return value

    def double(self):
        return self.take("d", 8)

    def u64(self):
        return self.take("Q", 8)

    def byte(self):
        return self.take("B", 1)

    def raw(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("short blob")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out


def finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


# ------------------------------------------------------------
# trackData: sample rate, number of samples, loudness, key
def read_track_data(blob) -> tuple[float, int] | None:
    data = unpack(blob)
    for little in (False, True):
        try:
            r = Reader(data, little)
            rate, samples = r.double(), r.u64()
        except (ValueError, struct.error):
            return None
        if (
            finite(rate)
            and int(rate) in SAMPLE_RATES
            and rate == int(rate)
            and samples < (1 << 40)
        ):
            return rate, samples
    return None


# beatData: the beat markers of the grid. Returns (bpm, first beat in ms) or None
def read_beat_data(blob) -> tuple[float, float] | None:
    data = unpack(blob)
    if len(data) < 18:
        return None
    for head_little in (False, True):
        try:
            r = Reader(data, head_little)
            rate, samples = r.double(), r.double()
            r.byte()
        except (ValueError, struct.error):
            return None
        if not (
            finite(rate)
            and 8000 <= rate <= 384000
            and finite(samples)
            and 0 < samples < (1 << 42)
        ):
            continue
        grids = []
        for _ in range(2):
            marks = None
            for count_little in (False, True):
                start = r.pos
                try:
                    r.little = count_little
                    count = r.u64()
                    if count < 2 or count > 100_000:
                        raise ValueError("count")
                    marks = read_markers(r, count, rate, samples)
                except (ValueError, struct.error):
                    marks = None
                    r.pos = start
                    continue
                break
            if marks is None:
                break
            grids.append(marks)
        if not grids:
            continue
        marks = grids[-1] if len(grids) > 1 and len(grids[-1]) >= 2 else grids[0]
        first_off, first_idx = marks[0]
        last_off, last_idx = marks[-1]
        beats, span = last_idx - first_idx, last_off - first_off
        if beats <= 0 or span <= 0:
            return None
        bpm = rate * 60.0 * beats / span
        if not 20.0 <= bpm <= 400.0:
            return None
        beat = span / beats
        origin = first_off - first_idx * beat
        origin %= beat
        return bpm, origin / rate * 1000.0
    return None


# markers: sample offset (double), beat index (int64), beats to the next marker and one unknown field (int32 each)
def read_markers(
    r: Reader, count: int, rate: float, samples: float
) -> list[tuple[float, int]]:
    for little in (True, False):
        start = r.pos
        out = []
        try:
            r.little = little
            for _ in range(count):
                offset, index = r.double(), r.take("q", 8)
                r.take("i", 4)
                r.take("i", 4)
                if not (
                    finite(offset)
                    and -rate * 600 <= offset <= samples + rate * 600
                    and abs(index) < 1_000_000
                ):
                    raise ValueError("marker")
                out.append((offset, index))
            if all(
                out[i][0] < out[i + 1][0] and out[i][1] < out[i + 1][1]
                for i in range(len(out) - 1)
            ):
                return out
        except (ValueError, struct.error):
            pass
        r.pos = start
    raise ValueError("markers")


# quickCues: eight hot cues, then the main cue
def read_quick_cues(blob, rate: float, samples: float) -> list[Cue]:
    data = unpack(blob)
    if len(data) < 16:
        return []
    for little in (False, True):
        try:
            r = Reader(data, little)
            if r.u64() != 8:
                continue
            cues = []
            for slot in range(8):
                name = r.raw(r.byte()).decode("utf-8", "replace")[:MAX_CUE_NAME]
                pos = r.double()
                argb = r.raw(4)
                if pos == -1 or not finite(pos):
                    continue
                if not -1 < pos <= samples * 1.01 + rate:
                    raise ValueError("cue outside the track")
                cues.append(Cue(name, CUE, pos / rate * 1000.0, 0.0, slot, rgb(argb)))
            main, overridden = r.double(), r.byte()
            if overridden == 1 and finite(main) and 0 <= main <= samples * 1.01 + rate:
                cues.append(Cue("", CUE, main / rate * 1000.0, 0.0, -1))
            return cues
        except (ValueError, struct.error):
            continue
    return []


# loops: eight slots, positions as doubles in samples
def read_loops(blob, rate: float, samples: float) -> list[Cue]:
    data = unpack(blob) or (
        bytes(blob)
        if isinstance(blob, (bytes, bytearray)) and len(blob) <= MAX_BLOB
        else b""
    )
    if len(data) < 16:
        return []
    for little in (True, False):
        try:
            r = Reader(data, little)
            if r.byte() != 8:
                continue
            r.raw(7)
            loops = []
            for _slot in range(8):
                name = r.raw(r.byte()).decode("utf-8", "replace")[:MAX_CUE_NAME]
                start, end = r.double(), r.double()
                has_start, has_end = r.byte(), r.byte()
                argb = r.raw(4)
                if has_start != 1 or has_end != 1:
                    continue
                if not (
                    finite(start)
                    and finite(end)
                    and 0 <= start < end <= samples * 1.01 + rate
                ):
                    raise ValueError("loop outside the track")
                loops.append(
                    Cue(
                        name,
                        LOOP,
                        start / rate * 1000.0,
                        (end - start) / rate * 1000.0,
                        -1,
                        rgb(argb),
                    )
                )
            return loops
        except (ValueError, struct.error):
            continue
    return []


def whole(x) -> int:
    v = num(x)
    return int(v) if abs(v) < 1e12 else 0


def text(x) -> str:
    return "" if x is None else str(x)


# a column that older or newer schemas may not have
def col(row: sqlite3.Row, name: str, default=None):
    names = row.keys()
    return row[name] if name in names else default


# four bytes, alpha first, as the colour of a cue
def rgb(argb: bytes) -> str:
    return f"#{argb[1]:02X}{argb[2]:02X}{argb[3]:02X}"


def read_date(x) -> str:
    if isinstance(x, (int, float)):
        return unix_date(x)
    s = text(x)
    return unix_date(s) if s.isdigit() else s[:10]


def track_path(base: Path, path: str) -> str:
    p = path.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return os.path.normpath(p)
    return os.path.normpath(str(base / p))


def chain(rows: list[sqlite3.Row], next_key: str) -> list[sqlite3.Row]:
    # a linked list kept in the table: every row names the next one, 0 ends it. What does not link in keeps its id order
    rows = [r for r in rows if isinstance(r["id"], int)]
    by_id = {r["id"]: r for r in rows}
    pointed = {r[next_key] for r in rows if r[next_key] in by_id}
    heads = sorted((r for r in rows if r["id"] not in pointed), key=lambda r: r["id"])
    out, seen = [], set()
    for head in heads:
        row = head
        while row is not None and row["id"] not in seen:
            seen.add(row["id"])
            out.append(row)
            row = by_id.get(row[next_key])
    out += [r for r in sorted(rows, key=lambda r: r["id"]) if r["id"] not in seen]
    return out


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def load(
    path: str, music_root: str = "", progress: Progress | None = None
) -> list[Node]:
    cb = progress or (lambda pct, msg: None)
    db = find_db(path)
    if db is None:
        raise ValueError(f"{os.path.basename(path)} is not an Engine library")
    if db.stat().st_size > MAX_DB:
        raise ValueError(f"{db.name} is too large to be an Engine library")
    cb(5, f"Reading {db.name}...")
    con = connect(db)
    try:
        return read_library(con, db, music_root, cb)
    except sqlite3.DatabaseError as e:
        raise ValueError(f"{db.name} cannot be read as an Engine library ({e})") from e
    finally:
        con.close()


def read_library(
    con: sqlite3.Connection, db: Path, music_root: str, cb: Progress
) -> list[Node]:
    have = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    if not all(t in have for t in TABLES):
        raise ValueError(f"{db.name} is not an Engine library")
    track_cols = columns(con, "Track")
    if not {"id", "path"} <= track_cols:
        raise ValueError(f"{db.name} is not an Engine library")
    if "title" not in track_cols:
        raise ValueError(
            "This is a library of Engine Prime 1.x. Open it in Engine DJ 4 or later, which writes the "
            "Database2 folder, and read that one"
        )
    # the folder the paths of the tracks start from: Engine Library, the parent of Database2
    base = db.parent.parent if db.parent.name.lower() == "database2" else db.parent
    uuid = text(
        (con.execute("SELECT uuid FROM Information LIMIT 1").fetchone() or [""])[0]
    )

    # the performance blobs: PerformanceData where there is one (a view or a table), else the Track columns
    blob_query = ""
    wanted = ("trackData", "beatData", "quickCues", "loops")
    if (
        "PerformanceData" in have
        and set(wanted) <= columns(con, "PerformanceData")
        and "trackId" in columns(con, "PerformanceData")
    ):
        blob_query = "SELECT trackData, beatData, quickCues, loops FROM PerformanceData WHERE trackId = ?"
    elif set(wanted) <= track_cols:
        blob_query = (
            "SELECT trackData, beatData, quickCues, loops FROM Track WHERE id = ?"
        )

    found = relocator(music_root)
    tracks: dict[int, Track] = {}
    by_origin: dict[tuple[str, int], int] = {}
    total = max(1, con.execute("SELECT COUNT(*) FROM Track").fetchone()[0])
    damaged = 0
    for n, row in enumerate(con.execute("SELECT * FROM Track ORDER BY id")):
        if len(tracks) >= MAX_TRACKS:
            break
        if col(row, "streamingSource") not in (None, "", 0, "0") or not text(
            row["path"]
        ):
            continue
        t = Track(path=found(track_path(base, text(row["path"]))))
        t.title, t.artist, t.album, t.genre = (
            text(col(row, name)) for name in ("title", "artist", "album", "genre")
        )
        t.comment, t.label, t.composer, t.remixer = (
            text(col(row, name)) for name in ("comment", "label", "composer", "remixer")
        )
        t.year, t.bitrate, t.size = (
            whole(col(row, "year")),
            whole(col(row, "bitrate")),
            whole(col(row, "fileBytes")),
        )
        t.duration = num(col(row, "length"))
        t.bpm = num(col(row, "bpmAnalyzed")) or num(col(row, "bpm"))
        k = col(row, "key")
        if isinstance(k, int) and 0 <= k < len(KEY_NAMES):
            t.key = keys.parse(KEY_NAMES[k])
        t.rating = min(5, max(0, round(num(col(row, "rating")) / 20)))
        t.added = read_date(col(row, "dateAdded")) or read_date(col(row, "dateCreated"))
        t.last_played = (
            read_date(col(row, "timeLastPlayed")) if col(row, "isPlayed") else ""
        )
        blob = con.execute(blob_query, (row["id"],)).fetchone() if blob_query else None
        if blob is not None:
            try:
                apply_performance(t, blob)
            except (ValueError, struct.error, OverflowError):
                damaged += 1
        tracks[row["id"]] = t
        origin_uuid = text(col(row, "originDatabaseUuid"))
        origin_id = col(row, "originTrackId")
        if origin_uuid and isinstance(origin_id, (int, float)) and finite(origin_id):
            by_origin[(origin_uuid, int(origin_id))] = row["id"]
        if n % 500 == 0:
            cb(10 + int(60 * n / total), f"{n} tracks")
    if damaged:
        log.warning(
            "%d tracks of the Engine library have cue or grid data that could not be read",
            damaged,
        )
    cb(75, f"{len(tracks)} tracks")

    def resolve(database: str, track_id: int) -> Track | None:
        if (database, track_id) in by_origin:
            return tracks.get(by_origin[(database, track_id)])
        return tracks.get(track_id) if not database or database == uuid else None

    members: dict[int, list[Track]] = {}
    if "PlaylistEntity" in have:
        entities = [
            r
            for r in con.execute(
                "SELECT id, listId, trackId, databaseUuid, nextEntityId FROM PlaylistEntity"
            )
        ]
        grouped: dict[int, list[sqlite3.Row]] = {}
        for r in entities:
            grouped.setdefault(r["listId"], []).append(r)
        for list_id, rows in grouped.items():
            tracks_of = (
                resolve(text(r["databaseUuid"]), r["trackId"])
                for r in chain(rows, "nextEntityId")
            )
            members[list_id] = [t for t in tracks_of if t is not None]
    playlists = [
        r
        for r in con.execute("SELECT id, title, parentListId, nextListId FROM Playlist")
    ]
    ids = {r["id"] for r in playlists}
    children: dict[int, list[sqlite3.Row]] = {}
    for r in playlists:
        parent = (
            r["parentListId"]
            if r["parentListId"] in ids and r["parentListId"] != r["id"]
            else 0
        )
        children.setdefault(parent, []).append(r)

    reached: set[int] = set()

    def build(parent: int, depth: int) -> list[Node]:
        out = []
        for r in chain(children.get(parent, []), "nextListId"):
            reached.add(r["id"])
            name = text(r["title"])
            kids = build(r["id"], depth + 1) if depth < 32 else []
            mine = members.get(r["id"], [])
            if kids:
                folder = Node("folder", name, children=kids)
                if mine:
                    folder.children.append(
                        Node("playlist", f"{name} (tracks)", tracks=mine)
                    )
                out.append(folder)
            elif mine:
                out.append(Node("playlist", name, tracks=mine))
        return out

    roots = build(0, 0)
    # lists whose parents lead round in a circle are reachable from no root, they come out flat
    for r in sorted(
        (r for r in playlists if r["id"] not in reached), key=lambda r: str(r["id"])
    ):
        mine = members.get(r["id"], [])
        if mine:
            roots.append(Node("playlist", text(r["title"]), tracks=mine))
    if tracks:
        roots.append(Node("playlist", "All tracks", tracks=list(tracks.values())))
    cb(100, f"{len(tracks)} tracks")
    return roots


def apply_performance(t: Track, blob: sqlite3.Row):
    data = read_track_data(blob["trackData"])
    grid = read_beat_data(blob["beatData"])
    if grid:
        t.bpm, t.grid = grid
    if data:
        rate, samples = data
        t.samplerate = int(rate)
        if not t.duration:
            t.duration = samples / rate
    else:
        # without the rate of the track the positions in samples mean nothing: no cue rather than a wrong one
        return
    t.cues += read_quick_cues(blob["quickCues"], rate, samples)
    t.cues += read_loops(blob["loops"], rate, samples)
