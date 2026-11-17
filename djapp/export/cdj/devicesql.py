# Benoit Saint-Moulin
# Boothbridge : writer for the page container of export.pdb (DeviceSQL)

# Container format of export.pdb: 4096 byte pages, rows, string encoding. The layout
# comes from a byte level study of rekordbox 6 USB exports and from the public notes
# of Deep Symmetry crate-digger.
#
# The players depend on three rules: each table owns an index page (flags 0x64) then
# its data pages, the header sequence is higher than any page sequence, and the free
# size of a data page is exact. A CDJ-2000NXS2 shows the first screens of a file
# that breaks them, then freezes on the next one.

from __future__ import annotations

import struct

from ... import native

PAGE = 4096
HEAD = 0x28
N_TABLES = 20

HEAP = PAGE - HEAD

# page header offsets
OFF_PAGE_NUMBER = 0x04
OFF_TABLE_KIND = 0x08
OFF_NEXT_PAGE = 0x0C
OFF_SEQUENCE = 0x10
OFF_PACKED_COUNTS = 0x18
OFF_FLAGS = 0x1B
OFF_FREE_SIZE = 0x1C
OFF_USED_SIZE = 0x1E
OFF_ROW_COUNT_A = 0x20
OFF_ROW_COUNT_B = 0x22

# page flag bytes
FLAG_INDEX_PAGE = 0x64
FLAG_DATA_PAGE = 0x24
FLAG_DATA_PAGE_LISTED = 0x34

# Tables whose index page lists their data pages (their data pages use the 0x34 flag)
LISTED_TABLES = (0, 19)

# Row index at the end of a data page: groups of 16 u16 offsets stored backwards, plus two u16 bit masks
ROWS_PER_GROUP = 16
GROUP_BYTES = 36
GROUP_TRAILER = 4

NONE_MARKER = 0x1FFF

# index page: 20 byte head, 4 byte entries, 20 byte tail
INDEX_HEAD_BYTES = 20
INDEX_TAIL_BYTES = 20
INDEX_SLOTS = (PAGE - HEAD - 40) // 4
EMPTY_INDEX_ENTRY = b"\xf8\xff\xff\x1f"

# Row start: u16 subtype, u16 "index shift" (0x20 per row on the shifted tables)
INDEX_SHIFT_OFFSET = 2
INDEX_SHIFT_STEP = 0x20

# string type bytes, see dstr
LONG_ASCII = 0x40
LONG_UTF16 = 0x90
EMPTY_STRING = b"\x03"
SHORT_ASCII_LIMIT = 126


# --------------------------------------------------------
# the C++ core builds it when loaded, build_py is the reference and the fallback
def build(
    tables: dict[int, list[bytes]], static: dict[int, bytes], shifted: set[int]
) -> bytes:
    return native.pdb_build(tables, static, shifted) or build_py(
        tables, static, shifted
    )


# ---------------------------------------------------------------
# page 0 = file header, then 2 pages per table (index, first data). Extra data pages
# of a big table go after those 2 * N_TABLES pages.
def _index_page_of(table: int) -> int:
    return 1 + 2 * table


def _first_data_page_of(table: int) -> int:
    return 2 + 2 * table


def _page_groups(
    tables: dict[int, list[bytes]], static: dict[int, bytes]
) -> dict[int, list]:
    # no rows, no data page. A static table is one finished page.
    groups: dict[int, list] = {}
    for table in range(N_TABLES):
        if table in static:
            groups[table] = [static[table]]
        elif tables.get(table):
            groups[table] = split_into_pages(tables[table])
    return groups


def _number_pages(groups: dict[int, list]) -> tuple[dict[int, list[int]], int]:
    numbers: dict[int, list[int]] = {}
    highest = 2 * N_TABLES
    for table, pages in groups.items():
        table_numbers = [_first_data_page_of(table)]
        for _ in pages[1:]:
            highest += 1
            table_numbers.append(highest)
        numbers[table] = table_numbers
    return numbers, highest


def build_py(
    tables: dict[int, list[bytes]], static: dict[int, bytes], shifted: set[int]
) -> bytes:
    # static: kind to a ready made data page (reference.py)
    groups = _page_groups(tables, static)
    numbers, highest_page = _number_pages(groups)

    # per table, the page where the next row would go: past the last page written,
    # or its own first data page when empty
    first_unused = highest_page + 1
    spare_page: dict[int, int] = {}
    unused = first_unused
    for table in range(N_TABLES):
        if table in groups:
            spare_page[table] = unused
            unused += 1
        else:
            spare_page[table] = _first_data_page_of(table)

    pages = [bytes(PAGE)] * (highest_page + 1)
    sequence = 1
    for table in range(N_TABLES):
        table_numbers = numbers.get(table, [])
        for position, content in enumerate(groups.get(table, [])):
            sequence += 1
            number = table_numbers[position]
            if position + 1 < len(table_numbers):
                following = table_numbers[position + 1]
            else:
                following = spare_page[table]
            if table in static:
                pages[number] = renumber_page(content, number, following, sequence)
            else:
                pages[number] = build_data_page(
                    number, table, following, sequence, content, table in shifted
                )

    for table in range(N_TABLES):
        if table in LISTED_TABLES and table in groups:
            entries = [number << 3 for number in numbers[table]]
        else:
            entries = []
        index_sequence = sequence + 1 if entries else 1
        pages[_index_page_of(table)] = build_index_page(
            _index_page_of(table),
            table,
            _first_data_page_of(table),
            index_sequence,
            entries,
        )
    sequence += 2

    pages[0] = _file_header(groups, numbers, spare_page, unused, sequence)
    return b"".join(pages)


def _file_header(
    groups: dict[int, list],
    numbers: dict[int, list[int]],
    spare_page: dict[int, int],
    first_unused: int,
    sequence: int,
) -> bytes:
    header = bytearray(PAGE)
    # the 5 is constant in every export seen
    struct.pack_into("<7I", header, 0, 0, PAGE, N_TABLES, first_unused, 5, sequence, 0)
    pointers_at = 28
    for table in range(N_TABLES):
        last_page = numbers[table][-1] if table in groups else _index_page_of(table)
        struct.pack_into(
            "<4I",
            header,
            pointers_at + 16 * table,
            table,
            spare_page[table],
            _index_page_of(table),
            last_page,
        )
    return bytes(header)


# ------------------------------------------------------
# bytes taken by the row index of `count` rows: full groups, the last one only as big as needed
def index_size(count: int) -> int:
    full_groups, leftover = divmod(count, ROWS_PER_GROUP)
    size = full_groups * GROUP_BYTES
    if leftover:
        size += GROUP_TRAILER + 2 * leftover
    return size


# ------------------------------------------------------------
# greedy fill, a page closes when the next row plus its index space does not fit
def split_into_pages(rows: list[bytes]) -> list[list[bytes]]:
    pages: list[list[bytes]] = []
    current: list[bytes] = []
    used = 0
    for row in rows:
        if len(row) + index_size(1) > HEAP:
            raise ValueError(f"row of {len(row)} bytes does not fit in a page")
        if current and used + len(row) + index_size(len(current) + 1) > HEAP:
            pages.append(current)
            current, used = [], 0
        current.append(row)
        used += len(row)
    if current:
        pages.append(current)
    return pages


# ------------------------------------------------------------
def _write_row_index(page: bytearray, offsets: list[int]) -> None:
    for first in range(0, len(offsets), ROWS_PER_GROUP):
        group = offsets[first : first + ROWS_PER_GROUP]
        group_end = PAGE - (first // ROWS_PER_GROUP) * GROUP_BYTES
        # two masks: slots in use, rows touched by the last write (all of them here, so equal)
        present = (1 << len(group)) - 1
        struct.pack_into("<HH", page, group_end - GROUP_TRAILER, present, present)
        for slot, offset in enumerate(group):
            struct.pack_into("<H", page, group_end - 6 - 2 * slot, offset)


def build_data_page(
    page_number: int,
    table: int,
    next_page: int,
    sequence: int,
    rows: list[bytes],
    index_shifts: bool,
) -> bytes:
    page = bytearray(PAGE)
    offsets: list[int] = []
    heap_used = 0
    for number, row in enumerate(rows):
        start = HEAD + heap_used
        page[start : start + len(row)] = row
        if index_shifts:
            struct.pack_into(
                "<H", page, start + INDEX_SHIFT_OFFSET, number * INDEX_SHIFT_STEP
            )
        offsets.append(heap_used)
        heap_used += len(row)
    _write_row_index(page, offsets)

    count = len(rows)
    flags = FLAG_DATA_PAGE_LISTED if table in LISTED_TABLES else FLAG_DATA_PAGE
    struct.pack_into("<IIIIII", page, 0, 0, page_number, table, next_page, sequence, 0)
    # 24 bit field: 13 bits of rows, 13 bits of slots in use (same number on a fresh file)
    packed_counts = count | (count << 13)
    page[OFF_PACKED_COUNTS : OFF_PACKED_COUNTS + 3] = packed_counts.to_bytes(
        3, "little"
    )
    page[OFF_FLAGS] = flags
    free_size = HEAP - heap_used - index_size(count)
    struct.pack_into(
        "<HHHHHH", page, OFF_FREE_SIZE, free_size, heap_used, count, 0, 0, 0
    )
    return bytes(page)


# ------------------------------------------------------------
# index page entry = page number << 3, unused slots get the fixed empty word
def build_index_page(
    page_number: int, table: int, next_page: int, sequence: int, entries: list[int]
) -> bytes:
    # a big table can outgrow the slots, the players follow next-page links anyway
    entries = entries[:INDEX_SLOTS]
    page = bytearray(PAGE)
    struct.pack_into("<IIIII", page, 0, 0, page_number, table, next_page, sequence)
    page[OFF_FLAGS] = FLAG_INDEX_PAGE
    # both row counters hold the "none" marker
    struct.pack_into(
        "<HHHHHH",
        page,
        OFF_FREE_SIZE,
        0,
        0,
        NONE_MARKER,
        NONE_MARKER,
        INDEX_SLOTS,
        1 if entries else 0,
    )
    struct.pack_into(
        "<IIIIHH",
        page,
        HEAD,
        page_number,
        next_page,
        0x03FFFFFF,
        0,
        len(entries),
        NONE_MARKER,
    )
    cursor = HEAD + INDEX_HEAD_BYTES
    for entry in entries:
        struct.pack_into("<I", page, cursor, entry)
        cursor += 4
    while cursor < PAGE - INDEX_TAIL_BYTES:
        page[cursor : cursor + 4] = EMPTY_INDEX_ENTRY
        cursor += 4
    return bytes(page)


# --------------------------------------------------------------------
# moves a finished page: only its number, next link and sequence change
def renumber_page(
    page: bytes, page_number: int, next_page: int, sequence: int
) -> bytes:
    patched = bytearray(page)
    struct.pack_into("<I", patched, OFF_PAGE_NUMBER, page_number)
    struct.pack_into("<II", patched, OFF_NEXT_PAGE, next_page, sequence)
    return bytes(patched)


# --------------------------------------------------------
# Fixed part, then strings pointed to by offset. Rows end on a 4 byte boundary and
# long strings start on one (rekordbox pads, the players expect it).
class RowBuffer:
    def __init__(self, fixed_part: bytes):
        self.buf = bytearray(fixed_part)

    def _pad_to_word(self) -> None:
        while len(self.buf) % 4:
            self.buf.append(0)

    def add_string(self, text: str) -> int:
        encoded = dstr(text)
        if encoded[0] in (LONG_ASCII, LONG_UTF16):
            self._pad_to_word()
        offset = len(self.buf)
        self.buf += encoded
        return offset

    def set_u8(self, offset: int, value: int) -> None:
        self.buf[offset] = value

    def set_u16(self, offset: int, value: int) -> None:
        struct.pack_into("<H", self.buf, offset, value)

    def finish(self) -> bytes:
        self._pad_to_word()
        return bytes(self.buf)


# ------------------------------------------------------------
# DeviceSQL strings:
# - empty: the single byte 0x03
# - short ASCII (up to 126 bytes): one byte ((length + 1) << 1) | 1, then the text
# - long ASCII: 0x40, u16 total length (text + 4), a zero byte, then the text
# - anything else: 0x90, the same header, then the text as UTF-16 little endian
def dstr(text: str) -> bytes:
    if not text:
        return EMPTY_STRING
    if text.isascii():
        raw = text.encode("ascii")
        if len(raw) <= SHORT_ASCII_LIMIT:
            return bytes([((len(raw) + 1) << 1) | 1]) + raw
        return struct.pack("<BHB", LONG_ASCII, len(raw) + 4, 0) + raw
    raw = text.encode("utf-16-le")
    return struct.pack("<BHB", LONG_UTF16, len(raw) + 4, 0) + raw
