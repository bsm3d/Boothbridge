# Benoit Saint-Moulin
# Boothbridge : the fixed files a CDJ key holds besides tracks, produced by code

# Besides tracks, a key carries four player settings files and four small export.pdb
# tables (browse columns, two menu tables, play history). All rebuilt from the values
# below, no Pioneer file is embedded. Values were read off rekordbox 6 exports (black
# box study). Settings layout (header, one byte per setting, CRC16) follows the notes
# of pyrekordbox and rekordcrate, the rows follow devicesql.py.

from __future__ import annotations

import binascii
import os
import struct
import time
from functools import lru_cache

from ..manifest import write_atomic
from . import devicesql as ds

SETTINGS = ("MYSETTING.DAT", "MYSETTING2.DAT", "DJMMYSETTING.DAT", "DEVSETTING.DAT")

PAGES = {16: "columns", 17: "t17", 18: "t18", 19: "history"}


def names() -> list[str]:
    return list(SETTINGS) + [f"{n}.page" for n in PAGES.values()]


# first bytes of the body, the players use them to tell the files apart
PLAYER_BODY_MAGIC = bytes.fromhex("7856341202000000")
MIXER_BODY_MAGIC = bytes.fromhex("785634120100000020000000")
DEVICE_BODY_MAGIC = bytes.fromhex("7856341201000000")

# File: 96 byte header (u32 size, three 32 byte texts: brand, "rekordbox", version), u32 body size, body, CRC16, 2 zero bytes
HEADER_SIZE = 0x60
CHECKSUM_FILLER = 0

# position in the body (from 1, like the format notes) to value. Choices count from 0x80, 0x81 = second choice.
PLAYER_SETTINGS = {
    9: 0x81,  # on air display: on
    10: 0x83,  # LCD brightness: three
    11: 0x81,  # quantize: on
    12: 0x88,  # auto cue level: memory
    13: 0x80,  # language: english
    14: 0x01,
    15: 0x82,  # jog ring brightness: bright
    16: 0x81,  # jog ring indicator: on
    17: 0x81,  # slip flashing: on
    18: 0x01,
    19: 0x01,
    20: 0x01,
    21: 0x82,  # disc slot illumination: bright
    22: 0x80,  # eject lock: unlock
    23: 0x80,  # sync: off
    24: 0x80,  # play mode: continue
    25: 0x80,  # quantize beat value: one
    26: 0x81,  # hot cue auto load: on
    27: 0x81,  # hot cue colour: on
    30: 0x80,  # needle lock: unlock
    33: 0x80,  # time mode: elapsed
    34: 0x80,  # jog mode: cdj
    35: 0x81,  # auto cue: on
    36: 0x80,  # master tempo: off
    37: 0x81,  # tempo range: ten
    38: 0x80,  # phase meter: type 1
}
PLAYER_SETTINGS_2 = {
    1: 0x81,  # vinyl speed adjust: touch
    2: 0x80,  # jog display mode: auto
    3: 0x83,  # pad / button brightness: three
    4: 0x83,  # jog LCD brightness: three
    5: 0x81,  # waveform divisions: phrase
    11: 0x80,  # waveform: waveform
    12: 0x81,
    13: 0x85,  # beat jump beat value: sixteen
}
MIXER_SETTINGS = {
    13: 0x81,  # channel fader curve: linear
    14: 0x82,  # cross fader curve: fast cut
    15: 0x80,  # headphones: post EQ
    16: 0x80,  # headphones: stereo
    17: 0x81,  # beat FX quantize: on
    18: 0x80,  # mic low cut: off
    19: 0x80,  # talk over mode: advanced
    20: 0x82,  # talk over level: minus 12 dB
    21: 0x80,  # MIDI channel: one
    22: 0x80,  # MIDI button type: toggle
    23: 0x85,  # display brightness: five
    24: 0x82,  # indicator brightness: three
    25: 0x81,  # channel fader curve: smooth
}

PLAYER_BODY_SIZE = 40
MIXER_BODY_SIZE = 52

# The player settings the user may choose, all plain on / off switches of MYSETTING.DAT. Every
# on / off pair seen in the exports is off = 0x80 and on = 0x81. The default of each is above.
SWITCHES = (9, 11, 16, 17, 23, 26, 27, 35, 36)
OFF, ON = 0x80, 0x81


# The choices as kept in the settings file: "11:1,23:0" = position 11 on, position 23 off.
# Anything that is not a known switch with 0 or 1 is dropped.
def parse_choices(text: object) -> dict[int, int]:
    chosen: dict[int, int] = {}
    if not isinstance(text, str):
        return chosen
    for part in text.split(","):
        position, _, state = part.strip().partition(":")
        if (
            position.isascii()
            and position.isdecimal()
            and int(position) in SWITCHES
            and state in ("0", "1")
        ):
            chosen[int(position)] = ON if state == "1" else OFF
    return chosen


def format_choices(chosen: dict[int, int]) -> str:
    return ",".join(
        f"{position}:{1 if chosen[position] == ON else 0}"
        for position in sorted(chosen)
        if position in SWITCHES and chosen[position] in (OFF, ON)
    )


def _settings_body(magic: bytes, settings: dict[int, int], size: int) -> bytes:
    body = bytearray(size)
    body[: len(magic)] = magic
    for position, value in settings.items():
        body[position - 1] = value
    return bytes(body)


# CRC16 XMODEM (binascii.crc_hqx) over the body only, mixer file: header too
def _settings_file(
    brand: str, version: str, body: bytes, checksum_includes_header: bool = False
) -> bytes:
    header = struct.pack(
        "<I32s32s32sI",
        HEADER_SIZE,
        brand.encode(),
        b"rekordbox",
        version.encode(),
        len(body),
    )
    content = header + body
    checked = content if checksum_includes_header else body
    crc = binascii.crc_hqx(checked, 0)
    return content + struct.pack("<HH", crc, CHECKSUM_FILLER)


def _settings_files(chosen: dict[int, int]) -> dict[str, bytes]:
    device_body = DEVICE_BODY_MAGIC + b"\x01" * 9 + bytes(15)
    return {
        "MYSETTING.DAT": _settings_file(
            "PIONEER",
            "0.001",
            _settings_body(
                PLAYER_BODY_MAGIC, PLAYER_SETTINGS | chosen, PLAYER_BODY_SIZE
            ),
        ),
        "MYSETTING2.DAT": _settings_file(
            "PIONEER",
            "0.001",
            _settings_body(b"", PLAYER_SETTINGS_2, PLAYER_BODY_SIZE),
        ),
        "DJMMYSETTING.DAT": _settings_file(
            "PioneerDJ",
            "1.000",
            _settings_body(MIXER_BODY_MAGIC, MIXER_SETTINGS, MIXER_BODY_SIZE),
            checksum_includes_header=True,
        ),
        "DEVSETTING.DAT": _settings_file("PIONEER DJ", "7.2.11", device_body),
    }


TABLE_COLUMNS, TABLE_MENU_A, TABLE_MENU_B, TABLE_HISTORY = 16, 17, 18, 19

# (id, display code, name), codes are not in id order, as found in the exports
BROWSE_COLUMNS = [
    (1, 0x80, "GENRE"),
    (2, 0x81, "ARTIST"),
    (3, 0x82, "ALBUM"),
    (4, 0x83, "TRACK"),
    (5, 0x85, "BPM"),
    (6, 0x86, "RATING"),
    (7, 0x87, "YEAR"),
    (8, 0x88, "REMIXER"),
    (9, 0x89, "LABEL"),
    (10, 0x8A, "ORIGINAL ARTIST"),
    (11, 0x8B, "KEY"),
    (12, 0x8D, "CUE"),
    (13, 0x8E, "COLOR"),
    (14, 0x92, "TIME"),
    (15, 0x93, "BITRATE"),
    (16, 0x94, "FILE NAME"),
    (17, 0x84, "PLAYLIST"),
    (18, 0x98, "HOT CUE BANK"),
    (19, 0x95, "HISTORY"),
    (20, 0x91, "SEARCH"),
    (21, 0x96, "COMMENTS"),
    (22, 0x8C, "DATE ADDED"),
    (23, 0x97, "DJ PLAY COUNT"),
    (24, 0x90, "FOLDER"),
    (25, 0xA1, "DEFAULT"),
    (26, 0xA2, "ALPHABET"),
    (27, 0xAA, "MATCHING"),
]

# the name sits between U+FFFA and U+FFFB in exported rows
NAME_START_MARK = "￺"
NAME_END_MARK = "￻"

# no public description, four u16 per row, copied as observed
MENU_ROWS_A = [
    (1, 1, 0x163, 0),
    (5, 6, 0x105, 0),
    (6, 7, 0x163, 0),
    (7, 8, 0x163, 0),
    (8, 9, 0x163, 0),
    (9, 10, 0x163, 0),
    (10, 11, 0x163, 0),
    (13, 15, 0x163, 0),
    (14, 19, 0x104, 0),
    (15, 20, 0x106, 0),
    (16, 21, 0x163, 0),
    (18, 23, 0x163, 0),
    (2, 2, 2, 1),
    (3, 3, 3, 2),
    (4, 4, 1, 3),
    (11, 12, 0x63, 4),
    (17, 5, 0x63, 5),
    (19, 22, 0x63, 6),
    (20, 18, 0x63, 7),
    (27, 26, 0x263, 8),
    (24, 17, 0x63, 9),
    (22, 27, 0x63, 10),
]
MENU_ROWS_B = [
    (1, 6, 1, 0),
    (21, 7, 1, 0),
    (14, 8, 1, 0),
    (8, 9, 1, 0),
    (9, 10, 1, 0),
    (10, 11, 1, 0),
    (15, 13, 1, 0),
    (13, 15, 1, 0),
    (23, 16, 1, 0),
    (22, 17, 1, 0),
    (25, 0, 0x100, 0),
    (26, 1, 0x200, 0),
    (2, 2, 0x300, 0),
    (3, 3, 0x400, 0),
    (5, 4, 0x500, 0),
    (6, 5, 0x600, 0),
    (11, 12, 0x700, 0),
]

# fresh key: four 40 byte history rows, all dated with the export day
HISTORY_ROWS = 4
HISTORY_ROW_BYTES = 40
HISTORY_SUBTYPE = 0x280
HISTORY_DATE_AT = 12
# bytes after the date, as found
HISTORY_ROW_TAIL = bytes.fromhex("191e0b31303030") + b"\x03"


def _column_rows() -> list[bytes]:
    rows = []
    for ident, code, name in BROWSE_COLUMNS:
        row = ds.RowBuffer(struct.pack("<HH", ident, code))
        row.add_string(NAME_START_MARK + name + NAME_END_MARK)
        rows.append(row.finish())
    return rows


def _menu_rows(table: list[tuple]) -> list[bytes]:
    return [struct.pack("<4H", *entry) for entry in table]


def _history_rows(day: str) -> list[bytes]:
    date_and_tail = ds.dstr(day) + HISTORY_ROW_TAIL
    rows = []
    for number in range(HISTORY_ROWS):
        row = bytearray(HISTORY_ROW_BYTES)
        struct.pack_into("<HHH", row, 0, HISTORY_SUBTYPE, 0, number)
        row[HISTORY_DATE_AT : HISTORY_DATE_AT + len(date_and_tail)] = date_and_tail
        rows.append(bytes(row))
    return rows


def _history_page(day: str) -> bytes:
    page = bytearray(
        ds.build_data_page(0, TABLE_HISTORY, 0, 0, _history_rows(day), True)
    )
    # not a plain fresh page: packed counts read 4 then 1, the counters at 0x20 hold 2,
    # the row index end keeps masks 0x0008 / 0x000C
    page[ds.OFF_PACKED_COUNTS : ds.OFF_PACKED_COUNTS + 3] = bytes((4, 0x20, 0))
    struct.pack_into("<HH", page, ds.OFF_ROW_COUNT_A, 2, 2)
    struct.pack_into("<HH", page, ds.PAGE - 4, 0x0008, 0x000C)
    return bytes(page)


def _table_pages(day: str) -> dict[int, bytes]:
    return {
        TABLE_COLUMNS: ds.build_data_page(
            0, TABLE_COLUMNS, 0, 0, _column_rows(), False
        ),
        TABLE_MENU_A: ds.build_data_page(
            0, TABLE_MENU_A, 0, 0, _menu_rows(MENU_ROWS_A), False
        ),
        TABLE_MENU_B: ds.build_data_page(
            0, TABLE_MENU_B, 0, 0, _menu_rows(MENU_ROWS_B), False
        ),
        TABLE_HISTORY: _history_page(day),
    }


# ------------------------------------------------------------
# built once per run, so the history date is the day the program started. chosen = the player
# settings picked by the user as format_choices writes them, "" keeps the defaults.
@lru_cache(maxsize=4)
def files(chosen: str = "") -> dict[str, bytes]:
    day = time.strftime("%Y-%m-%d")
    built = _settings_files(parse_choices(chosen))
    for table, page in _table_pages(day).items():
        built[f"{PAGES[table]}.page"] = page
    return built


# for devicesql.build_py
def pages() -> dict[int, bytes]:
    built = files()
    return {table: built[f"{name}.page"] for table, name in PAGES.items()}


# existing settings stay, the owner may have changed them. Only the settings the user chose
# in the program are changed in the player file that is already there (settingsread.apply).
def copy_settings(pioneer_dir: str, chosen: str = ""):
    built = files(format_choices(parse_choices(chosen)))
    for name in SETTINGS:
        destination = os.path.join(pioneer_dir, name)
        if not os.path.exists(destination):
            write_atomic(destination, built[name])
