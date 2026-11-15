# Benoit Saint-Moulin
# Boothbridge: sanity checks on the export.pdb and ANLZ bytes before they are published.
#
# Layout as seen in rekordbox exports and in the crate-digger notes, no Pioneer
# software used. Strict on purpose: made for the files this exporter generates, not
# for everything rekordbox could write.

import contextlib
import os
import struct

from ..manifest import replace, settle, sha256_bytes, sha256_drive, write_atomic
from .devicesql import HEAD, N_TABLES, PAGE, index_size

# PMAI file header: tag, header size, total size, 12 more bytes. Sections: tag, header size, total size (big endian)
ANLZ_FILE_HEADER = 28
ANLZ_SECTION_MIN_HEADER = 12

# required sections: .DAT small file, .EXT colour and 3 band waveforms
DAT_SECTIONS = {b"PVBR", b"PQTZ", b"PWAV", b"PWV2", b"PCOB"}
EXT_SECTIONS = {b"PWV3", b"PWV4", b"PWV5", b"PCOB", b"PCO2"}

WAVEFORM_ENTRY_WIDTH = {b"PWAV": 1, b"PWV2": 1, b"PWV3": 1, b"PWV4": 6, b"PWV5": 2}
# these two have a count only, the others width then count
WAVEFORMS_WITHOUT_WIDTH_FIELD = (b"PWAV", b"PWV2")

BEAT_ENTRY_SIZE = 8
VBR_SECTION_SIZE = 1620

# tag -> (header size, offset of the u16 cue count, entry tag)
CUE_LISTS = {
    b"PCOB": (24, 18, b"PCPT"),
    b"PCO2": (20, 16, b"PCP2"),
}

# same page layout as pdbread
PDB_FLAG_NO_ROWS = 0x40
PDB_PAGE_FLAGS_AT = 0x1B
PDB_ROW_COUNT_MASK = 0x1FFF
PDB_ROWS_PER_GROUP = 16
PDB_GROUP_SIZE = 36
PDB_FIRST_OFFSET_AT = 6
PDB_DIRECTORY_AT = 28
PDB_DIRECTORY_ENTRY_SIZE = 16


# ----------------------------------------------------------------
# each checker gets the whole section, its header size and the expected track path
def _check_path(section: bytes, header: int, expected_path: str | None):
    if header != 16:
        raise ValueError("Invalid ANLZ path header")
    declared = struct.unpack_from(">I", section, 12)[0]
    body = section[header:]
    # UTF-16 BE, zero terminated
    if declared != len(body) or declared < 2 or declared % 2 or body[-2:] != b"\0\0":
        raise ValueError("Invalid ANLZ path payload")
    path = body[:-2].decode("utf-16-be")
    if expected_path is not None and path != expected_path:
        raise ValueError("ANLZ path does not match its exported track")


def _check_beat_grid(section: bytes, header: int, expected_path: str | None):
    if (
        header != 24
        or len(section) - header
        != struct.unpack_from(">I", section, 20)[0] * BEAT_ENTRY_SIZE
    ):
        raise ValueError("Invalid ANLZ beat-grid count")


def _waveform_checker(tag: bytes):
    def check(section: bytes, header: int, expected_path: str | None):
        if header < 20:
            raise ValueError("Invalid ANLZ waveform header")
        if tag in WAVEFORMS_WITHOUT_WIDTH_FIELD:
            width, count = 1, struct.unpack_from(">I", section, 12)[0]
        else:
            width, count = struct.unpack_from(">II", section, 12)
        body_size = len(section) - header
        if width != WAVEFORM_ENTRY_WIDTH[tag] or body_size != width * count:
            raise ValueError("Invalid ANLZ waveform count or width")

    return check


def _check_vbr(section: bytes, header: int, expected_path: str | None):
    if header != 16 or len(section) != VBR_SECTION_SIZE:
        raise ValueError("Invalid ANLZ VBR payload")


def _cue_list_checker(tag: bytes):
    list_header, count_at, entry_tag = CUE_LISTS[tag]

    def check(section: bytes, header: int, expected_path: str | None):
        if header != list_header:
            raise ValueError("Invalid ANLZ cue-list header")
        size = len(section)
        count = struct.unpack_from(">H", section, count_at)[0]
        cursor = header
        for _ in range(count):
            if cursor + 12 > size:
                raise ValueError("Truncated ANLZ cue")
            entry_header, entry_size = struct.unpack_from(">II", section, cursor + 4)
            if (
                section[cursor : cursor + 4] != entry_tag
                or not 12 <= entry_header <= entry_size
                or cursor + entry_size > size
            ):
                raise ValueError("Invalid ANLZ cue length")
            cursor += entry_size
        if cursor != size:
            raise ValueError("ANLZ cue count does not match payload")

    return check


SECTION_CHECKS = {
    b"PPTH": _check_path,
    b"PQTZ": _check_beat_grid,
    b"PVBR": _check_vbr,
}
for _tag in WAVEFORM_ENTRY_WIDTH:
    SECTION_CHECKS[_tag] = _waveform_checker(_tag)
for _tag in CUE_LISTS:
    SECTION_CHECKS[_tag] = _cue_list_checker(_tag)


def anlz(data: bytes, expected_path: str | None = None, extension: str | None = None):
    if (
        len(data) < ANLZ_FILE_HEADER
        or data[:4] != b"PMAI"
        or struct.unpack_from(">II", data, 4) != (ANLZ_FILE_HEADER, len(data))
    ):
        raise ValueError("Invalid ANLZ file header or length")

    position = ANLZ_FILE_HEADER
    found = set()
    while position < len(data):
        if position + ANLZ_SECTION_MIN_HEADER > len(data):
            raise ValueError("Truncated ANLZ section header")
        tag = data[position : position + 4]
        header, size = struct.unpack_from(">II", data, position + 4)
        if not ANLZ_SECTION_MIN_HEADER <= header <= size or position + size > len(data):
            raise ValueError(f"Invalid ANLZ section length: {tag!r}")
        check = SECTION_CHECKS.get(tag)
        if check is not None:
            check(data[position : position + size], header, expected_path)
        found.add(tag)
        position += size

    if b"PPTH" not in found:
        raise ValueError("ANLZ has no track path")
    if extension == "DAT":
        needed = DAT_SECTIONS
    elif extension == "EXT":
        needed = EXT_SECTIONS
    else:
        needed = set()
    if not needed <= found:
        raise ValueError(f"ANLZ {extension} is missing required sections")


# ----------------------------------------------------------------
# a page may belong to one table only
def _check_data_page(data: bytes, offset: int):
    packed = data[offset + 0x18 : offset + PDB_PAGE_FLAGS_AT]
    row_count = int.from_bytes(packed, "little") & PDB_ROW_COUNT_MASK
    free, used = struct.unpack_from("<HH", data, offset + 0x1C)
    # header + heap + row index + free gap = whole page
    if HEAD + used + index_size(row_count) + free != PAGE:
        raise ValueError("Invalid PDB row heap or index size")
    for first in range(0, row_count, PDB_ROWS_PER_GROUP):
        group_end = offset + PAGE - (first // PDB_ROWS_PER_GROUP) * PDB_GROUP_SIZE
        in_group = min(PDB_ROWS_PER_GROUP, row_count - first)
        for slot in range(in_group):
            row_start = struct.unpack_from(
                "<H", data, group_end - PDB_FIRST_OFFSET_AT - slot * 2
            )[0]
            if row_start >= used:
                raise ValueError("PDB row offset is outside its heap")


def pdb(data: bytes):
    if len(data) < PAGE or len(data) % PAGE:
        raise ValueError("Invalid PDB page alignment")
    zero, page_size, table_count, next_unused, _, _, _ = struct.unpack_from("<7I", data)
    page_total = len(data) // PAGE
    if zero or page_size != PAGE or table_count != N_TABLES or next_unused < page_total:
        raise ValueError("Invalid PDB header")

    claimed = set()
    for table in range(table_count):
        at = PDB_DIRECTORY_AT + table * PDB_DIRECTORY_ENTRY_SIZE
        kind, _, first, last = struct.unpack_from("<4I", data, at)
        if kind != table:
            raise ValueError("Invalid PDB table directory")
        page = first
        visited = set()
        while True:
            if page <= 0 or page >= page_total or page in visited or page in claimed:
                raise ValueError("Invalid or circular PDB page chain")
            visited.add(page)
            claimed.add(page)
            offset = page * PAGE
            _, own_index, page_kind, following = struct.unpack_from("<4I", data, offset)
            if own_index != page or page_kind != kind:
                raise ValueError("PDB page identity mismatch")
            if not data[offset + PDB_PAGE_FLAGS_AT] & PDB_FLAG_NO_ROWS:
                _check_data_page(data, offset)
            if page == last:
                break
            page = following


# ----------------------------------------------------------------
# validate first so a bad file never reaches the drive, write beside the target, read back, compare hash, swap
def write_verified(path: str, data: bytes, validator):
    validator(data)
    temporary = str(path) + ".verified"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        write_atomic(temporary, data)
        if settle(lambda: sha256_drive(temporary)) != sha256_bytes(data):
            raise OSError(f"Generated file read-back mismatch: {path}")
        replace(temporary, path)
    finally:
        for leftover in (temporary, temporary + ".tmp"):
            if os.path.exists(leftover):
                # Fix erreur: a cleanup that fails must not hide why the write failed
                with contextlib.suppress(OSError):
                    settle(lambda leftover=leftover: os.unlink(leftover))
