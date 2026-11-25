# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Mixxx export that writes a standalone mixxxdb.sqlite database at schema version 39, never touching a profile.

# Never touches a Mixxx profile: writes its own mixxxdb.sqlite in the export folder, and refuses a folder that already holds one.
# The user copies it into a backed up profile with Mixxx closed. DDL is Mixxx 2.5 (res/schema.xml, revision 39) after all migrations, so Mixxx migrates it forward.

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time

from ..model import CUE, LOAD, LOOP, Node, Track
from .common import copy_tracks, start, where
from .files import Report
from .fit import NAME, fit, text
from .manifest import Cancelled, Manifest, replace, sha256_file

DB_NAME = "mixxxdb.sqlite"
SCHEMA_VERSION = 39
# every revision of the 2.5 schema is marked min_compatible 3 except the first ones
MIN_COMPATIBLE = 3
# Mixxx stores up to 36 hot cues, slots 0 to 35
MAX_HOTCUES = 36
# Mixxx cue types, as djapp/sources/mixxx.py reads them and src/track/cueinfo.h
HOTCUE, MAINCUE, LOOP_CUE = 1, 2, 4
# default Mixxx hot cue colour (red of its palette) and orange for a cue without a slot
HOT_COLOR, MAIN_COLOR = 0xC50A08, 0xFF8C00
SEP = " / "

_HEX = re.compile(r"#?([0-9a-fA-F]{6})\Z")

# ChromaticKey 1..24 in the library, shown in the traditional notation of Mixxx
KEY_NAMES = (
    "C",
    "Db",
    "D",
    "Eb",
    "E",
    "F",
    "F#",
    "G",
    "Ab",
    "A",
    "Bb",
    "B",
    "Cm",
    "C#m",
    "Dm",
    "Ebm",
    "Em",
    "Fm",
    "F#m",
    "Gm",
    "G#m",
    "Am",
    "Bbm",
    "Bm",
)

# ----------------------------------------------------------------
# the revisions of res/schema.xml, flattened. Column order follows the migrations
SCHEMA = """
CREATE TABLE settings (
  name TEXT UNIQUE NOT NULL,
  value TEXT,
  locked INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0);
CREATE TABLE track_locations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  location varchar(512) UNIQUE,
  filename varchar(512),
  directory varchar(512),
  filesize INTEGER,
  fs_deleted INTEGER,
  needs_verification INTEGER);
CREATE TABLE LibraryHashes (
  directory_path VARCHAR(256) PRIMARY KEY,
  hash INTEGER,
  directory_deleted INTEGER,
  needs_verification INTEGER DEFAULT 0);
CREATE TABLE library (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artist varchar(64),
  title varchar(64),
  album varchar(64),
  year varchar(16),
  genre varchar(64),
  tracknumber varchar(3),
  location INTEGER REFERENCES track_locations(location),
  comment varchar(256),
  url varchar(256),
  duration FLOAT,
  bitrate INTEGER,
  samplerate INTEGER,
  cuepoint INTEGER,
  bpm FLOAT,
  wavesummaryhex BLOB,
  channels INTEGER,
  datetime_added DEFAULT CURRENT_TIMESTAMP,
  mixxx_deleted INTEGER,
  played INTEGER,
  header_parsed INTEGER DEFAULT 0,
  filetype varchar(8) DEFAULT '?',
  replaygain FLOAT DEFAULT 0,
  timesplayed INTEGER DEFAULT 0,
  rating INTEGER DEFAULT 0,
  key varchar(8) DEFAULT '',
  beats BLOB,
  beats_version TEXT,
  composer varchar(64) DEFAULT '',
  bpm_lock INTEGER DEFAULT 0,
  beats_sub_version TEXT DEFAULT '',
  keys BLOB,
  keys_version TEXT,
  keys_sub_version TEXT,
  key_id INTEGER DEFAULT 0,
  grouping TEXT DEFAULT '',
  album_artist TEXT DEFAULT '',
  coverart_source INTEGER DEFAULT 0,
  coverart_type INTEGER DEFAULT 0,
  coverart_location TEXT DEFAULT '',
  coverart_hash INTEGER DEFAULT 0,
  replaygain_peak REAL DEFAULT -1.0,
  tracktotal TEXT DEFAULT '//',
  color INTEGER,
  coverart_color INTEGER,
  coverart_digest BLOB,
  last_played_at DATETIME DEFAULT NULL,
  source_synchronized_ms INTEGER DEFAULT NULL);
CREATE TABLE Playlists (
  id INTEGER PRIMARY KEY,
  name varchar(48),
  position INTEGER,
  hidden INTEGER DEFAULT 0 NOT NULL,
  date_created datetime,
  date_modified datetime,
  locked INTEGER DEFAULT 0);
CREATE TABLE PlaylistTracks (
  id INTEGER PRIMARY KEY,
  playlist_id INTEGER REFERENCES Playlists(id),
  track_id INTEGER REFERENCES library(id),
  position INTEGER,
  pl_datetime_added);
CREATE TABLE cues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id INTEGER NOT NULL REFERENCES library(id),
  type INTEGER DEFAULT 0 NOT NULL,
  position INTEGER DEFAULT -1 NOT NULL,
  length INTEGER DEFAULT 0 NOT NULL,
  hotcue INTEGER DEFAULT -1 NOT NULL,
  label TEXT DEFAULT '' NOT NULL,
  color INTEGER DEFAULT 4294901760 NOT NULL);
CREATE TABLE crates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name varchar(48) UNIQUE NOT NULL,
  count INTEGER DEFAULT 0,
  show INTEGER DEFAULT 1,
  locked INTEGER DEFAULT 0,
  autodj_source INTEGER DEFAULT 0);
CREATE TABLE crate_tracks (
  crate_id INTEGER NOT NULL REFERENCES crates(id),
  track_id INTEGER NOT NULL REFERENCES library(id),
  UNIQUE (crate_id, track_id));
CREATE TABLE itunes_library (
  id INTEGER PRIMARY KEY,
  artist varchar(48), title varchar(48),
  album varchar(48), year varchar(16),
  genre varchar(32), tracknumber varchar(3),
  location varchar(512),
  comment varchar(60),
  duration INTEGER,
  bitrate INTEGER,
  bpm INTEGER,
  rating INTEGER,
  grouping TEXT DEFAULT '',
  album_artist TEXT DEFAULT '');
CREATE TABLE itunes_playlists (
  id INTEGER PRIMARY KEY,
  name varchar(100) UNIQUE);
CREATE TABLE itunes_playlist_tracks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER REFERENCES itunes_playlist(id),
  track_id INTEGER REFERENCES itunes_library(id),
  position INTEGER DEFAULT 0);
CREATE TABLE traktor_library (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artist varchar(48), title varchar(48),
  album varchar(48), year varchar(16),
  genre varchar(32), tracknumber varchar(3),
  location varchar(512) UNIQUE,
  comment varchar(60),
  duration INTEGER,
  bitrate INTEGER,
  bpm FLOAT,
  key varchar(6),
  rating INTEGER);
CREATE TABLE traktor_playlists (
  id INTEGER PRIMARY KEY,
  name varchar(100) UNIQUE);
CREATE TABLE traktor_playlist_tracks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER REFERENCES traktor_playlist(id),
  track_id INTEGER REFERENCES traktor_library(id),
  position INTEGER DEFAULT 0);
CREATE TABLE rhythmbox_library (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artist varchar(48), title varchar(48),
  album varchar(48), year varchar(16),
  genre varchar(32), tracknumber varchar(3),
  location varchar(512) UNIQUE,
  comment varchar(60),
  duration INTEGER,
  bitrate INTEGER,
  bpm FLOAT,
  key varchar(6),
  rating INTEGER);
CREATE TABLE rhythmbox_playlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name varchar(100) UNIQUE);
CREATE TABLE rhythmbox_playlist_tracks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id INTEGER REFERENCES rhythmbox_playlist(id),
  track_id INTEGER REFERENCES rhythmbox_library(id),
  position INTEGER DEFAULT 0);
CREATE TABLE track_analysis (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id INTEGER NOT NULL REFERENCES track_locations(id),
  type varchar(512),
  description varchar(1024),
  version varchar(512),
  created DEFAULT CURRENT_TIMESTAMP,
  data_checksum varchar(512));
CREATE TABLE directories (
  directory TEXT UNIQUE);
CREATE INDEX track_analysis_track_id_index ON track_analysis (track_id);
CREATE INDEX idx_PlaylistTracks_playlist_id_track_id ON PlaylistTracks (
  playlist_id, track_id);
CREATE INDEX idx_PlaylistTracks_track_id ON PlaylistTracks (track_id);
CREATE INDEX idx_crate_tracks_track_id ON crate_tracks (track_id);
"""

_TRACK = (
    "INSERT INTO library (artist, title, album, year, genre, tracknumber, location, "
    "comment, duration, bitrate, samplerate, bpm, channels, datetime_added, "
    "mixxx_deleted, played, header_parsed, filetype, timesplayed, rating, key, "
    "composer, album_artist, key_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, 0, 0, 1, ?, ?, ?, ?, ?, ?, ?)"
)

def flat(s: str) -> str:
    return text(s, NAME).replace("\r", " ").replace("\n", " ")

# ----------------------------------------------------------------
# forward slashes, as Mixxx writes the location of a file on Windows too
def location(path: str) -> str:
    return os.path.abspath(text(path, 32767)).replace("\\", "/")

def color_of(c, default: int) -> int:
    m = _HEX.match(c.color.strip()) if isinstance(c.color, str) else None
    # black is how Mixxx recognises an empty hot cue it throws away
    return int(m.group(1), 16) or default if m else default

# ----------------------------------------------------------------
# hot cues and loops. Cues without a slot, other than a loop or the one main cue, have no
# place in Mixxx and are left out
def cue_rows(t: Track) -> list[tuple]:
    per_ms = 2 * (t.samplerate or 44100) / 1000
    rows, slots, main = [], set(), False
    for c in sorted(t.cues, key=lambda c: (c.hotcue < 0, c.hotcue, c.start)):
        if c.kind not in (CUE, LOAD, LOOP):
            continue
        if c.hotcue >= MAX_HOTCUES:
            continue
        hot = max(-1, c.hotcue)
        label = text(c.name, NAME).replace("\x00", "")
        pos = max(0.0, c.start) * per_ms
        if c.is_loop:
            if hot >= 0 and hot in slots:
                continue
            slots.add(hot)
            rows.append(
                (LOOP_CUE, pos, c.length * per_ms, hot, label, color_of(c, HOT_COLOR))
            )
        elif hot >= 0:
            if hot in slots:
                continue
            slots.add(hot)
            rows.append((HOTCUE, pos, 0.0, hot, label, color_of(c, HOT_COLOR)))
        elif c.kind == LOAD and not main:
            main = True
            rows.append((MAINCUE, pos, 0.0, -1, label, MAIN_COLOR))
    return rows

# ----------------------------------------------------------------
def flatten(nodes: list[Node], prefix: str = "") -> list[tuple[str, Node]]:
    out = []
    for n in nodes:
        name = flat(n.name) or "Playlist"
        if n.is_folder:
            out += flatten(n.children, prefix + name + SEP)
        elif n.tracks:
            out.append((prefix + name, n))
    return out

def unique(name: str, taken: set[str]) -> str:
    base, k = name, 2
    while name.casefold() in taken:
        name = f"{base} ({k})"
        k += 1
    taken.add(name.casefold())
    return name

# ----------------------------------------------------------------
def add_track(
    con: sqlite3.Connection, t: Track, path: str, ids: dict[str, int], stamp: str
) -> int | None:
    if not path:
        return None
    loc = location(path)
    if loc in ids:
        return ids[loc]
    f = fit(t)
    size = f.size
    try:
        size = os.path.getsize(path) or size
    except OSError:
        pass
    cur = con.execute(
        "INSERT INTO track_locations (location, filename, directory, filesize, "
        "fs_deleted, needs_verification) VALUES (?, ?, ?, ?, 0, 0)",
        (loc, os.path.basename(loc), os.path.dirname(loc), size),
    )
    ext = os.path.splitext(loc)[1].lstrip(".").lower()[:8] or "?"
    ext = "aiff" if ext == "aif" else ext
    key = f.key
    added = f"{f.added} 00:00:00" if len(f.added) == 10 else stamp
    cur = con.execute(
        _TRACK,
        (
            flat(f.artist),
            flat(f.title),
            flat(f.album),
            str(f.year) if f.year else "",
            flat(f.genre),
            str(f.track_no) if f.track_no else "",
            cur.lastrowid,
            text(f.comment, 1000).replace("\x00", ""),
            f.duration,
            f.bitrate,
            f.samplerate or 44100,
            f.bpm,
            added,
            ext,
            f.plays,
            f.rating,
            KEY_NAMES[key] if key is not None else "",
            flat(f.composer),
            flat(f.album_artist),
            key + 1 if key is not None else 0,
        ),
    )
    tid = cur.lastrowid
    for row in cue_rows(f):
        con.execute(
            "INSERT INTO cues (track_id, type, position, length, hotcue, label, color) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, *row),
        )
    ids[loc] = tid
    return tid

# ----------------------------------------------------------------
def build(
    path: str,
    nodes: list[Node],
    moved: dict[str, str],
    cb,
    cancel: threading.Event | None,
) -> int:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        rows = [
            ("mixxx.schema.version", str(SCHEMA_VERSION)),
            ("mixxx.schema.last_used_version", str(SCHEMA_VERSION)),
            ("mixxx.schema.min_compatible_version", str(MIN_COMPATIBLE)),
        ]
        con.executemany("INSERT INTO settings (name, value) VALUES (?, ?)", rows)
        lists = flatten(nodes)
        taken: set[str] = set()
        ids: dict[str, int] = {}
        written = 0
        for i, (name, n) in enumerate(lists):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            members = []
            for original in n.tracks:
                tid = add_track(con, original, where(original, moved), ids, stamp)
                if tid is not None:
                    members.append(tid)
            if not members:
                continue
            cur = con.execute(
                "INSERT INTO Playlists (name, position, hidden, date_created, "
                "date_modified, locked) VALUES (?, ?, 0, ?, ?, 0)",
                (unique(name, taken), written, stamp, stamp),
            )
            con.executemany(
                "INSERT INTO PlaylistTracks (playlist_id, track_id, position, "
                "pl_datetime_added) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, tid, p, stamp) for p, tid in enumerate(members, 1)],
            )
            written += 1
            cb(10 + 80 * (i + 1) // len(lists), f"Playlist {i + 1}/{len(lists)}")
        con.commit()
        check(con)
        return written
    finally:
        con.close()

# ----------------------------------------------------------------
# integrity and foreign keys. library.location is declared as a reference to
# track_locations(location) although it holds the id, Mixxx has always done that and runs
# with foreign keys off, so that one declaration is not counted as a fault
def check(con: sqlite3.Connection):
    ok = con.execute("PRAGMA integrity_check").fetchall()
    if ok != [("ok",)]:
        raise ValueError(f"The Mixxx database failed its integrity check: {ok[:3]}")
    bad = [
        r
        for r in con.execute("PRAGMA foreign_key_check")
        if not (r[0] == "library" and r[2] == "track_locations")
    ]
    if bad:
        raise ValueError(f"The Mixxx database has broken references: {bad[:3]}")

# ----------------------------
# a Mixxx library is the user's work: never replaced, never merged into. export.run calls
# this before the checksum list of the folder is removed, so a refusal leaves it
def require_free(out: str):
    final = os.path.join(out, DB_NAME)
    if os.path.lexists(final):
        raise ValueError(
            f"{final} already exists. This export never overwrites a Mixxx database, "
            "choose an empty folder."
        )

def export(
    nodes: list[Node],
    out: str,
    copy: bool = False,
    verify: bool = False,
    progress=None,
    cancel: threading.Event | None = None,
    man: Manifest | None = None,
) -> Report:
    cb = progress or (lambda pct, msg: None)
    os.makedirs(out, exist_ok=True)
    final = os.path.join(out, DB_NAME)
    require_free(out)
    rep = start(nodes, final)
    moved = {}
    if copy:
        moved = copy_tracks(
            nodes,
            out,
            rep,
            verify,
            lambda i, n, info="": cb(10 * i // n, f"Copying {i}/{n} {info}".strip()),
            cancel,
            man,
        )
    tmp = final + ".tmp"
    for stale in (tmp, tmp + "-journal"):
        try:
            os.remove(stale)
        except OSError:
            pass
    try:
        n = build(tmp, nodes, moved, cb, cancel)
        if os.path.lexists(final):
            raise ValueError(f"{final} appeared during the export, left untouched.")
        replace(tmp, final)
    except BaseException:
        for leftover in (tmp, tmp + "-journal"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
    if man:
        man.add(final, sha256_file(final))
    cb(100, f"{n} playlists written")
    return rep
