# Benoit Saint-Moulin
# Boothbridge : writer for the tables of export.pdb (tracks, lookups, playlists)

# Turns tracks and the playlist tree into export.pdb rows, devicesql.py lays them out
# in pages. Row layouts come from a byte level study of rekordbox 6 exports and the
# crate-digger notes. Field names are
# crate-digger's when known, "constant" = same value in every export compared.

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from ... import keys, native
from ...model import Node, Track
from ..fit import text
from .devicesql import RowBuffer, build_py

(TRACKS, GENRES, ARTISTS, ALBUMS, LABELS, KEYS, COLORS, PL_TREE, PL_ENTRIES) = range(9)
ARTWORK, COLUMNS, T17, T18, HISTORY = 13, 16, 17, 18, 19

# rows carry a growing "index shift" in their second u16
SHIFTED_TABLES = {TRACKS, ARTISTS, ALBUMS}

# ids 1 to 8
COLOR_NAMES = ["Pink", "Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple"]

# extension to the file type code of a track row
FILE_TYPES = {
    ".mp3": 1,
    ".m4a": 4,
    ".mp4": 4,
    ".aac": 4,
    ".flac": 5,
    ".wav": 11,
    ".aif": 12,
    ".aiff": 12,
}
DEFAULT_FILE_TYPE = 1
DEFAULT_SAMPLE_RATE = 44100

TRACK_ROW_BYTES = 0x88
TRACK_SUBTYPE = 0x24
TRACK_BITMASK = 0x000C0700
TRACK_CONSTANT_A = 0xC25930BF
TRACK_SAMPLE_DEPTH = 16
TRACK_CONSTANT_B = 41
TRACK_CONSTANT_C = 3
MAX_RATING = 5

# first of the 21 u16 string offsets ending the fixed part
TRACK_STRING_OFFSETS = 0x5E

PLAYLIST_NAME_LIMIT = 255
ARTWORK_PATH = "/PIONEER/Artwork/00001/a{}.jpg"


# ------------------------------------------------------------
# a track as it lands on the key
@dataclass(slots=True)
class Item:
    track: Track
    id: int
    path: str  # /Contents/..., as the player sees it
    anlz: str  # /PIONEER/USBANLZ/P.../ANLZ0000.DAT
    size: int
    art: int = 0


# ------------------------------------------------------------
# C++ core first, Python path as reference and fallback. Returns the bytes, the manifest keeps them.
def write(
    items: list[Item],
    nodes: list[Node],
    path: str | os.PathLike,
    today: str = "",
    static: dict[int, bytes] | None = None,
) -> bytes:
    today = today or datetime.now().astimezone().date().isoformat()
    # menu tables and empty history are identical in every export, see reference.py
    if static is None:
        from . import reference

        static = reference.pages()
    data = None
    if native.available():
        tracks, tree, entries = native_inputs(items, nodes)
        data = native.pdb_write(tracks, tree, entries, today, static)
    if data is None:
        data = build_py_file(items, nodes, today, static)
    from .validate import pdb, write_verified

    write_verified(str(path), data, pdb)
    return data


# ----------------------------------------------------------------
def build_py_file(
    items: list[Item], nodes: list[Node], today: str, static: dict[int, bytes]
) -> bytes:
    lookups = _Lookups()
    track_rows = [_track_row(item, today, lookups.link(item.track)) for item in items]

    tables = {
        TRACKS: track_rows,
        GENRES: [_id_name_row(i, n) for n, i in lookups.genres.ids.items()],
        ARTISTS: [_artist_row(i, n) for n, i in lookups.artists.ids.items()],
        ALBUMS: [_album_row(i, n, a) for (n, a), i in lookups.albums.ids.items()],
        LABELS: [_id_name_row(i, n) for n, i in lookups.labels.ids.items()],
        KEYS: [_key_row(i, n) for n, i in lookups.keys.ids.items()],
        COLORS: [_color_row(i + 1, n) for i, n in enumerate(COLOR_NAMES)],
        ARTWORK: [
            _id_name_row(art, ARTWORK_PATH.format(art))
            for art in sorted({item.art for item in items if item.art})
        ],
    }

    folders, entries = _flatten_playlists(nodes, {i.track.uid: i.id for i in items})
    tables[PL_TREE] = [tree_row(*folder) for folder in folders]
    tables[PL_ENTRIES] = [struct.pack("<III", *entry) for entry in entries]

    return build_py(tables, static, SHIFTED_TABLES)


# --------------------------------------------------------
# numbered from 1 in order of first use, like rekordbox. Empty name = id 0.
class _IdsByFirstUse:
    def __init__(self):
        self.ids: dict = {}

    def get(self, key) -> int:
        if key in (None, ""):
            return 0
        if key not in self.ids:
            self.ids[key] = len(self.ids) + 1
        return self.ids[key]


class _TrackLinks(NamedTuple):
    artist: int
    album: int
    genre: int
    label: int
    remixer: int
    composer: int
    key: int


class _Lookups:
    def __init__(self):
        self.artists = _IdsByFirstUse()
        self.albums = _IdsByFirstUse()
        self.genres = _IdsByFirstUse()
        self.labels = _IdsByFirstUse()
        self.keys = _IdsByFirstUse()

    # remixer and composer share the artist table, call order decides the ids
    def link(self, track: Track) -> _TrackLinks:
        artist = self.artists.get(track.artist)
        album_artist = self.artists.get(track.album_artist) or artist
        album_key = (track.album, album_artist) if track.album else None
        return _TrackLinks(
            artist,
            self.albums.get(album_key),
            self.genres.get(track.genre),
            self.labels.get(track.label),
            self.artists.get(track.remixer),
            self.artists.get(track.composer),
            self.keys.get(keys.name(track.key)),
        )


# ------------------------------------------------------------
def _track_row(item: Item, today: str, link: _TrackLinks) -> bytes:
    track = item.track
    extension = os.path.splitext(item.path)[1].lower()
    row = RowBuffer(bytes(TRACK_ROW_BYTES))
    buf = row.buf

    # u16 at 2 (index shift) is set at page building
    struct.pack_into(
        "<HHIIIIII",
        buf,
        0x00,
        TRACK_SUBTYPE,
        0,
        TRACK_BITMASK,
        track.samplerate or DEFAULT_SAMPLE_RATE,
        link.composer,
        item.size,
        0,
        TRACK_CONSTANT_A,
    )
    # the third value is original_artist_id, which this export never fills
    struct.pack_into(
        "<IIIIII",
        buf,
        0x1C,
        item.art,
        link.key,
        0,
        link.label,
        link.remixer,
        track.bitrate,
    )
    # bpm * 100
    struct.pack_into(
        "<IIIIII",
        buf,
        0x34,
        track.track_no,
        round(track.bpm * 100),
        link.genre,
        link.album,
        link.artist,
        item.id,
    )
    # duration in whole seconds
    struct.pack_into(
        "<HHHHHH",
        buf,
        0x4C,
        track.disc_no,
        track.plays,
        track.year,
        TRACK_SAMPLE_DEPTH,
        round(track.duration),
        TRACK_CONSTANT_B,
    )
    struct.pack_into(
        "<BBHH",
        buf,
        0x58,
        track.color,
        min(MAX_RATING, track.rating),
        FILE_TYPES.get(extension, DEFAULT_FILE_TYPE),
        TRACK_CONSTANT_C,
    )

    for slot, string in enumerate(_track_strings(item, today)):
        row.set_u16(TRACK_STRING_OFFSETS + 2 * slot, row.add_string(string))
    return row.finish()


# 21 strings in offset slot order, names from crate-digger
def _track_strings(item: Item, today: str) -> list[str]:
    track = item.track
    return [
        "",  # isrc
        "",  # texter
        "2",  # unknown, always this text in rekordbox exports
        "2",  # unknown, same
        "",  # unknown
        "",  # message
        "ON",  # kuvo_public
        "ON",  # autoload_hotcues
        "",  # unknown
        "",  # unknown
        track.added or today,  # date_added
        "",  # release_date
        "",  # mix_name
        "",  # unknown
        item.anlz,  # analyze_path
        today,  # analyze_date
        track.comment,
        track.title,
        "",  # unknown
        os.path.basename(item.path),  # filename
        item.path,  # file_path
    ]


# genre, label, artwork: u32 id then name
def _id_name_row(ident: int, name: str) -> bytes:
    row = RowBuffer(struct.pack("<I", ident))
    row.add_string(name)
    return row.finish()


def _artist_row(ident: int, name: str) -> bytes:
    # subtype 0x60: artist row, name offset is one byte at 9
    row = RowBuffer(struct.pack("<HHIBB", 0x60, 0, ident, 3, 0))
    row.set_u8(9, row.add_string(name))
    return row.finish()


def _album_row(ident: int, name: str, artist: int) -> bytes:
    row = RowBuffer(struct.pack("<HHIIIIBB", 0x80, 0, 0, artist, ident, 0, 3, 0))
    row.set_u8(21, row.add_string(name))
    return row.finish()


def _key_row(ident: int, name: str) -> bytes:
    # id repeated, as in the exports
    row = RowBuffer(struct.pack("<II", ident, ident))
    row.add_string(name)
    return row.finish()


def _color_row(ident: int, name: str) -> bytes:
    row = RowBuffer(struct.pack("<IBHB", 0, ident, ident, 0))
    row.add_string(name)
    return row.finish()


# public, the tests build damaged files with it
def tree_row(parent: int, sort: int, ident: int, is_folder: bool, name: str) -> bytes:
    row = RowBuffer(struct.pack("<IIIII", parent, 0, sort, ident, int(is_folder)))
    row.add_string(name)
    return row.finish()


# ------------------------------------------------------------
# depth first, nodes numbered 1, 2, 3... as met. Returns tree_row arguments per node and
# (position from 1, track id, playlist id) per entry. A track without an id is skipped
# and takes no position.
def _flatten_playlists(
    nodes: list[Node], track_ids: dict[str, int]
) -> tuple[list[tuple], list[tuple]]:
    folders: list[tuple] = []
    entries: list[tuple] = []
    next_id = iter(range(1, 1 << 31))

    def visit(level: list[Node], parent: int) -> None:
        for sort, node in enumerate(level):
            node_id = next(next_id)
            name = text(node.name, PLAYLIST_NAME_LIMIT)
            folders.append((parent, sort, node_id, int(node.is_folder), name))
            if node.is_folder:
                visit(node.children, node_id)
                continue
            position = 0
            for track in node.tracks:
                track_id = track_ids.get(track.uid)
                if track_id:
                    position += 1
                    entries.append((position, track_id, node_id))

    visit(nodes, 0)
    return folders, entries


# ------------------------------------------------------------
# same data as build_py_file, as plain values for the native core
def native_inputs(items: list[Item], nodes: list[Node]):
    tracks = []
    for item in items:
        track = item.track
        extension = os.path.splitext(item.path)[1].lower()
        tracks.append(
            (
                item.id,
                item.art,
                item.size,
                track.samplerate or DEFAULT_SAMPLE_RATE,
                track.bitrate,
                track.track_no,
                track.disc_no,
                track.plays,
                track.year,
                track.color,
                track.rating,
                FILE_TYPES.get(extension, DEFAULT_FILE_TYPE),
                track.bpm,
                track.duration,
                [
                    track.title,
                    track.artist,
                    track.album_artist,
                    track.album,
                    track.genre,
                    track.label,
                    track.remixer,
                    track.composer,
                    track.comment,
                    keys.name(track.key),
                    track.added,
                    item.path,
                    item.anlz,
                    os.path.basename(item.path),
                ],
            )
        )
    folders, entries = _flatten_playlists(nodes, {i.track.uid: i.id for i in items})
    return tracks, folders, entries
