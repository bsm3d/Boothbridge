# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reads an export.pdb file back into tables and rows, checking every offset against the buffer.

# Layout worked out from rekordbox USB exports, matches the crate-digger write-up.
# The input is a drive anybody may have written to: every offset and length is checked against its buffer, and only PdbError escapes.

from __future__ import annotations

import struct
import threading
from bisect import bisect_right
from dataclasses import dataclass, field

from ..manifest import Cancelled
from .devicesql import HEAD, PAGE, index_size

(TRACKS, GENRES, ARTISTS, ALBUMS, LABELS, KEYS, COLORS, PL_TREE, PL_ENTRIES) = range(9)

# a real key holds a few hundred thousand rows at most, more is damage
MAX_ROWS = 1_000_000

# file header: 28 bytes of fields, then 16 byte directory entries until the page ends
FILE_HEADER_SIZE = 28
DIRECTORY_ENTRY_SIZE = 16
MAX_DIRECTORY_ENTRIES = (PAGE - FILE_HEADER_SIZE) // DIRECTORY_ENTRY_SIZE

# page header: 3 packed bytes hold the row count in their low 13 bits, then the flags byte
PAGE_PACKED_AT = 0x18
PAGE_FLAGS_AT = 0x1B
PAGE_FREE_USED_AT = 0x1C
ROW_COUNT_MASK = 0x1FFF

# index pages, no rows
FLAG_NO_ROWS = 0x40

# row index at the page end, groups of 16: 16 u16 offsets, a u16 presence mask, 2 bytes padding (36)
ROWS_PER_GROUP = 16
GROUP_SIZE = 36
GROUP_MASK_AT = 4
GROUP_OFFSETS_AT = 6

# short ascii: odd first byte ((length + 1) * 2 + 1). Long forms: even marker then u16 total size
LONG_ASCII_MARKER = 0x40
LONG_UTF16_MARKER = 0x90
LONG_STRING_HEADER = 4

TRACK_ROW_MIN = 0x88
TRACK_STRING_TABLE_AT = 0x5E
TRACK_STRING_COUNT = 21

# --------------------------------------------------
class PdbError(ValueError):
    pass

# ----------------------------------------------------------
@dataclass
class Page:
    idx: int
    kind: int
    nxt: int
    seq: int
    flags: int
    free: int
    used: int
    # present rows only, deleted ones are skipped
    rows: list[bytes] = field(default_factory=list)

# ----------------------------------------------------------
@dataclass
class Pdb:
    next_unused: int
    seq: int
    # table number -> (empty candidate page, first page, last page)
    tables: dict[int, tuple[int, int, int]]
    # table number -> its data pages, in chain order
    pages: dict[int, list[Page]]

# ----------------------------------------------------------
def dstr(buf: bytes, off: int) -> str:
    if not 0 <= off < len(buf):
        raise PdbError("string starts outside its row")
    marker = buf[off]
    if marker & 1:
        length = (marker >> 1) - 1
        if length < 0 or off + 1 + length > len(buf):
            raise PdbError("short string runs past its row")
        return buf[off + 1 : off + 1 + length].decode("ascii", "replace")
    if marker not in (LONG_ASCII_MARKER, LONG_UTF16_MARKER) or (
        off + LONG_STRING_HEADER > len(buf)
    ):
        raise PdbError("unknown string form")
    total = struct.unpack_from("<H", buf, off + 1)[0]
    if total < LONG_STRING_HEADER or off + total > len(buf):
        raise PdbError("long string runs past its row")
    body = buf[off + LONG_STRING_HEADER : off + total]
    codec = "utf-16-le" if marker == LONG_UTF16_MARKER else "ascii"
    return body.decode(codec, "replace")

# ----------------------------------------------------------
def _row_slots(page: bytes, count: int) -> list[tuple[int, bool]]:
    slots = []
    for first in range(0, count, ROWS_PER_GROUP):
        group_end = PAGE - (first // ROWS_PER_GROUP) * GROUP_SIZE
        mask = struct.unpack_from("<H", page, group_end - GROUP_MASK_AT)[0]
        in_group = min(ROWS_PER_GROUP, count - first)
        for bit in range(in_group):
            at = group_end - GROUP_OFFSETS_AT - 2 * bit
            offset = struct.unpack_from("<H", page, at)[0]
            slots.append((offset, bool(mask >> bit & 1)))
    return slots

def read_page(data: bytes, idx: int) -> Page:
    page = data[idx * PAGE : (idx + 1) * PAGE]
    if len(page) != PAGE:
        raise PdbError(f"page {idx} is outside the file")
    _, own_index, kind, following, seq = struct.unpack_from("<5I", page, 0)
    packed = int.from_bytes(page[PAGE_PACKED_AT:PAGE_FLAGS_AT], "little")
    free, used = struct.unpack_from("<HH", page, PAGE_FREE_USED_AT)
    flags = page[PAGE_FLAGS_AT]
    result = Page(own_index, kind, following, seq, flags, free, used)
    if flags & FLAG_NO_ROWS:
        return result

    count = packed & ROW_COUNT_MASK
    if used > PAGE - HEAD or HEAD + index_size(count) > PAGE:
        raise PdbError(f"page {idx} has an impossible row index")
    slots = _row_slots(page, count)

    # a row ends at the next offset in the heap, the last one at the used size
    boundaries = sorted({offset for offset, _ in slots} | {used})
    live = [offset for offset, present in slots if present]
    if len(set(live)) != len(live):
        raise PdbError(f"page {idx} has two rows at the same place")
    for offset in live:
        if offset >= used:
            raise PdbError(f"page {idx} has a row outside its heap")
        stop = boundaries[bisect_right(boundaries, offset)]
        result.rows.append(page[HEAD + offset : HEAD + stop])
    return result

# ----------------------------------------------------------
def _read_chain(
    data: bytes,
    kind: int,
    first: int,
    last: int,
    cancel: threading.Event | None,
) -> list[Page]:
    page_total = len(data) // PAGE
    chain = []
    visited = set()
    row_total = 0
    number = first
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if not 0 < number < page_total or number in visited:
            raise PdbError(f"the page chain of table {kind} is broken")
        visited.add(number)
        page = read_page(data, number)
        if page.idx != number or page.kind != kind:
            raise PdbError(f"page {number} does not belong to table {kind}")
        if not page.flags & FLAG_NO_ROWS:
            chain.append(page)
            row_total += len(page.rows)
            if row_total > MAX_ROWS:
                raise PdbError(f"table {kind} has too many rows")
        if number == last:
            return chain
        number = page.nxt

# `only` limits the walk to those tables, the others stay empty. cancel is checked before each page.
def read(
    data: bytes, only: set[int] | None = None, cancel: threading.Event | None = None
) -> Pdb:
    if len(data) < PAGE or len(data) % PAGE:
        raise PdbError("the size is not a whole number of 4096 byte pages")
    _, page_size, table_count, next_unused, _, seq, _ = struct.unpack_from(
        "<7I", data, 0
    )
    if page_size != PAGE or not 1 <= table_count <= MAX_DIRECTORY_ENTRIES:
        raise PdbError("the header is not a database header")

    tables = {}
    pages = {}
    for entry in range(table_count):
        at = FILE_HEADER_SIZE + DIRECTORY_ENTRY_SIZE * entry
        kind, empty_candidate, first, last = struct.unpack_from("<4I", data, at)
        tables[kind] = (empty_candidate, first, last)
        if only is not None and kind not in only:
            continue
        pages[kind] = _read_chain(data, kind, first, last, cancel)
    return Pdb(next_unused, seq, tables, pages)

def rows(pdb: Pdb, kind: int) -> list[bytes]:
    return [row for page in pdb.pages.get(kind, []) for row in page.rows]

# ----------------------------------------------------------
def track_strings(row: bytes) -> list[str]:
    if len(row) < TRACK_ROW_MIN:
        raise PdbError("track row too short")
    offsets = struct.unpack_from(f"<{TRACK_STRING_COUNT}H", row, TRACK_STRING_TABLE_AT)
    return [dstr(row, offset) for offset in offsets]

@dataclass(slots=True)
class TrackRow:
    id: int
    samplerate: int
    size: int
    art: int
    key: int
    label: int
    remixer: int
    composer: int
    bitrate: int
    track_no: int
    tempo: int  # beats per minute times 100
    genre: int
    album: int
    artist: int
    disc: int
    plays: int
    year: int
    seconds: int
    color: int
    rating: int
    kind: int  # file type
    strings: list[str]  # the 21 strings of the row, see track_strings

def track_row(row: bytes) -> TrackRow:
    strings = track_strings(row)
    samplerate, composer, size = struct.unpack_from("<III", row, 0x08)
    art, key = struct.unpack_from("<II", row, 0x1C)
    label, remixer, bitrate, track_no, tempo, genre, album, artist, ident = (
        struct.unpack_from("<9I", row, 0x28)
    )
    disc, plays, year = struct.unpack_from("<HHH", row, 0x4C)
    (seconds,) = struct.unpack_from("<H", row, 0x54)
    color = row[0x58]
    rating = row[0x59]
    (kind,) = struct.unpack_from("<H", row, 0x5A)
    return TrackRow(
        id=ident,
        samplerate=samplerate,
        size=size,
        art=art,
        key=key,
        label=label,
        remixer=remixer,
        composer=composer,
        bitrate=bitrate,
        track_no=track_no,
        tempo=tempo,
        genre=genre,
        album=album,
        artist=artist,
        disc=disc,
        plays=plays,
        year=year,
        seconds=seconds,
        color=color,
        rating=rating,
        kind=kind,
        strings=strings,
    )

# ----------------------------------------------------------
# id to name. Genre and label rows: name at byte 4, key rows: byte 8
def names(pdb: Pdb, kind: int) -> dict[int, str]:
    name_at = 8 if kind == KEYS else 4
    found = {}
    for row in rows(pdb, kind):
        if len(row) <= name_at:
            raise PdbError("name row too short")
        found.setdefault(struct.unpack_from("<I", row, 0)[0], dstr(row, name_at))
    return found

# name offset is one byte, or a u16 when bit 2 of the subtype is set (name too far for one byte)
def near_far(r: bytes, near: int, far: int) -> str:
    if len(r) < far + 2:
        raise PdbError("name row too short")
    subtype = struct.unpack_from("<H", r, 0)[0]
    if subtype & 4:
        return dstr(r, struct.unpack_from("<H", r, far)[0])
    return dstr(r, r[near])

def artists(pdb: Pdb) -> dict[int, str]:
    found = {}
    for row in rows(pdb, ARTISTS):
        if len(row) < 12:
            raise PdbError("artist row too short")
        found.setdefault(struct.unpack_from("<I", row, 4)[0], near_far(row, 9, 10))
    return found

# album id -> (name, album artist id)
def albums(pdb: Pdb) -> dict[int, tuple[str, int]]:
    found = {}
    for row in rows(pdb, ALBUMS):
        if len(row) < 24:
            raise PdbError("album row too short")
        artist, ident = struct.unpack_from("<II", row, 8)
        found.setdefault(ident, (near_far(row, 0x15, 0x16), artist))
    return found

# ----------------------------------------------------------
@dataclass(slots=True)
class TreeRow:
    parent: int
    sort: int
    id: int
    folder: bool
    name: str

def tree(pdb: Pdb) -> list[TreeRow]:
    found = []
    for row in rows(pdb, PL_TREE):
        if len(row) < 0x15:
            raise PdbError("playlist row too short")
        parent, _, sort, ident, folder = struct.unpack_from("<5I", row, 0)
        found.append(TreeRow(parent, sort, ident, bool(folder), dstr(row, 0x14)))
    return found

# (position, track id, playlist id)
def entries(pdb: Pdb) -> list[tuple[int, int, int]]:
    found = []
    for row in rows(pdb, PL_ENTRIES):
        if len(row) < 12:
            raise PdbError("playlist entry too short")
        found.append(struct.unpack_from("<III", row, 0))
    return found
