# Benoit Saint-Moulin
# Boothbridge : writer for the ANLZ0000.DAT and ANLZ0000.EXT analysis files
#
# Two binary files per track (path, beat grid, waveforms, cues), built here as bytes.
# Layout from rekordbox exports compared with the track content, plus the crate-digger
# research. No Pioneer program was disassembled. Undocumented fields carry the value
# seen in the exports and are named "unknown". All numbers big endian.
#
# Left out on purpose: PQT2, PSSI and .2EX (newest players only).

from __future__ import annotations

import math
import struct

import numpy as np

from ... import native
from ...analysis import Audio, Bands
from ...model import Cue, Track
from ...sources.rbxml import RB_COLORS
from ..rbxml import HOT_RGB

# section start: 4 letter name, header length, section length (12 bytes)
SECTION_PREFIX = 12
# the file header is a section too: PMAI, then 6 four byte fields
FILE_HEADER_LEN = 28
FILE_HEADER_TAIL = (1, 0x10000, 0x10000, 0)

# tempo is BPM x 100 in 16 bits
MAX_BPM = 655.35
BEAT_GRID_UNKNOWN = 0x80000

# 400 u32 seek offsets (VBR)
VBR_INDEX_BYTES = 400 * 4

# detail wave: 150 columns per second
DETAIL_PER_SECOND = 150
PREVIEW_COLUMNS = 400
TINY_COLUMNS = 100
COLOR_PREVIEW_COLUMNS = 1200
WAVE_UNKNOWN = 0x10000
COLOR_DETAIL_UNKNOWN = 0x0305

CUE_ENTRY_HEADER = 28
CUE_ENTRY_LEN = 56
CUE_EXT_HEADER = 16
CUE_EXT_FIXED = 0x2C
CUE_EXT_TAIL = 4
CUE_PLAIN, CUE_LOOP = 1, 2
CUE_UNKNOWN_1000 = 1000
NO_LOOP_END = 0xFFFFFFFF
ORDER_NONE = 0xFFFF
MAX_HOT_CUES = 8


# --------------------------------------------------------
# Folder rekordbox picks: hash of the track path, one UTF-16 unit at a time, and the
# players look there first. On a collision the next number is tried, hence `used`.
FOLDER_COUNT = 200003


def _folder_hash(usb_path: str) -> int:
    acc = 0
    for ch in usb_path:
        unit = ord(ch) & 0xFFFF
        acc = ((acc * 0x5BC9 + unit) * 0x93B5 + unit) & 0xFFFFFFFF
    return acc


# the P0xx parent folder is seven bits picked out of the folder number
def _parent_number(num: int) -> int:
    return (
        (num & 1)
        | (num >> 1 & 2)
        | (num >> 4 & 4)
        | (num >> 4 & 8)
        | (num >> 5 & 0x10)
        | (num >> 8 & 0x20)
        | (num >> 10 & 0x40)
    )


def anlz_dir(usb_path: str, used: set[str] | None = None) -> str:
    start = _folder_hash(usb_path)
    for step in range(FOLDER_COUNT):
        num = (start + step) % FOLDER_COUNT
        folder = f"/PIONEER/USBANLZ/P{_parent_number(num):03X}/{num:08X}"
        if used is None:
            return folder
        if folder not in used:
            used.add(folder)
            return folder
    raise ValueError(
        "Too many tracks: Pioneer analysis directory allocation is exhausted"
    )


# --------------------------------------------------------
def _section(name: bytes, head: bytes, payload: bytes = b"") -> bytes:
    head_len = SECTION_PREFIX + len(head)
    return name + struct.pack(">II", head_len, head_len + len(payload)) + head + payload


def pmai(sections: list[bytes]) -> bytes:
    body = b"".join(sections)
    fixed = struct.pack(">II", FILE_HEADER_LEN, FILE_HEADER_LEN + len(body))
    return b"PMAI" + fixed + struct.pack(">IIII", *FILE_HEADER_TAIL) + body


def _slot_number(cue: Cue) -> int:
    # hot cue A = 1, memory cues = 0
    return cue.hotcue + 1 if cue.hotcue >= 0 else 0


def _peak(bands: Bands):
    return np.maximum(np.maximum(bands.low, bands.mid), bands.high)


def _detail_wave(track: Track, audio: Audio):
    if audio.ok:
        columns = len(audio.fine[0])
    else:
        columns = round(track.duration * DETAIL_PER_SECOND)
    return audio.at(columns)


# --------------------------------------------------------
def ppth(usb_path: str) -> bytes:
    raw = usb_path.encode("utf-16-be") + b"\0\0"
    return _section(b"PPTH", struct.pack(">I", len(raw)), raw)


def _vbr_section(frames: int) -> bytes:
    # seek offsets stay zero (the player rebuilds them), only the trailing count matters
    return _section(
        b"PVBR",
        struct.pack(">I", 0),
        bytes(VBR_INDEX_BYTES) + struct.pack(">I", frames),
    )


# --------------------------------------------------------
# (beat in bar 1 to 4, time in ms). The anchor can sit anywhere: beats before it are
# rolled back to time zero and the bar position is counted from there.
def beats(t: Track) -> list[tuple[int, int]]:
    if t.bpm <= 0 or t.grid is None or not t.duration:
        return []
    interval = 60000 / t.bpm
    anchor = t.grid or 0.0
    lead_in = math.floor(anchor / interval)
    first_time = anchor - lead_in * interval
    end_ms = t.duration * 1000
    grid = []
    index = 0
    while first_time + index * interval < end_ms:
        position = ((index - lead_in) % 4) + 1
        grid.append((position, round(first_time + index * interval)))
        index += 1
    return grid


def pqtz(t: Track) -> bytes:
    if t.grid is not None and t.bpm > MAX_BPM:
        raise ValueError("CDJ beat grids support at most 655.35 BPM")
    tempo = round(t.bpm * 100)
    grid = beats(t)
    entries = b"".join(
        struct.pack(">HHI", position, tempo, when) for position, when in grid
    )
    return _section(
        b"PQTZ", struct.pack(">III", 0, BEAT_GRID_UNKNOWN, len(grid)), entries
    )


# --------------------------------------------------------
# mono column byte: bits 0 to 4 height (0 to 31), bits 5 to 7 whiteness (0 to 7), whiter when highs dominate
def _mono_columns(bands: Bands) -> bytes:
    top = _peak(bands)
    ratio = np.divide(bands.high, top, out=np.ones_like(top), where=top > 0.01)
    height = np.rint(31 * top).astype(np.uint8)
    whiteness = np.rint(7 * ratio).astype(np.uint8)
    return (whiteness << 5 | height).astype(np.uint8).tobytes()


def pwav(b: Bands) -> bytes:
    return _section(b"PWAV", struct.pack(">II", len(b), WAVE_UNKNOWN), _mono_columns(b))


# smallest preview, height 0 to 15
def _tiny_wave(bands: Bands) -> bytes:
    return _section(
        b"PWV2",
        struct.pack(">II", len(bands), WAVE_UNKNOWN),
        np.rint(15 * _peak(bands)).astype(np.uint8).tobytes(),
    )


def _detail_mono(bands: Bands) -> bytes:
    head = struct.pack(">IIHH", 1, len(bands), DETAIL_PER_SECOND, 0)
    return _section(b"PWV3", head, _mono_columns(bands))


# two bytes per column: 3 bits each of red (low), green (mid), blue (high), then 5 bits
# of height. Bands are relative to the loudest one so quiet columns keep their hue,
# silence is black.
def _detail_color(bands: Bands) -> bytes:
    top = _peak(bands)
    audible = top > 0.01
    divisor = np.where(audible, top, 1.0)
    red, green, blue = (
        np.where(audible, np.rint(7 * band / divisor), 0).astype(np.uint16)
        for band in (bands.low, bands.mid, bands.high)
    )
    height = np.rint(31 * top).astype(np.uint16)
    word = red << 13 | green << 10 | blue << 7 | height << 2
    head = struct.pack(">IIHH", 2, len(bands), DETAIL_PER_SECOND, COLOR_DETAIL_UNKNOWN)
    return _section(b"PWV5", head, word.astype(">u2").tobytes())


# six bytes per column: the player draws from the last three (low, mid, high as r, g, b), the first three hold the level
def _color_preview(bands: Bands) -> bytes:
    low, mid, high = (
        np.rint(127 * v).astype(np.uint8) for v in (bands.low, bands.mid, bands.high)
    )
    level = np.maximum(np.maximum(low, mid), high)
    columns = np.stack([level, level, level, low, mid, high], axis=1)
    return _section(b"PWV4", struct.pack(">III", 6, len(bands), 0), columns.tobytes())


# --------------------------------------------------------
# The 62 hot cue colours of a CDJ, in palette grid order. The code in the file is
# index + 1 (0 = old default, green). A colour outside this table is not shown, so the
# nearest entry is used.
HOT_PALETTE = [
    0x305AFF,
    0x5073FF,
    0x508CFF,
    0x50A0FF,
    0x50B4FF,
    0x50B0F2,
    0x50AEE8,
    0x45ACDB,
    0x00E0FF,
    0x19DAF0,
    0x32D2E6,
    0x21B4B9,
    0x20AAA0,
    0x1FA392,
    0x19A08C,
    0x14A584,
    0x14AA7D,
    0x10B176,
    0x30D26E,
    0x37DE5A,
    0x3CEB50,
    0x28E214,
    0x7DC13D,
    0x8CC832,
    0x9BD723,
    0xA5E116,
    0xA5DC0A,
    0xAAD208,
    0xB4C805,
    0xB4BE04,
    0xBAB404,
    0xC3AF04,
    0xE1AA00,
    0xFFA000,
    0xFF9600,
    0xFF8C00,
    0xFF7500,
    0xE0641B,
    0xE0461E,
    0xE0301E,
    0xE02823,
    0xE62828,
    0xFF376F,
    0xFF2D6F,
    0xFF127B,
    0xF51E8C,
    0xEB2DA0,
    0xE637B4,
    0xDE44CF,
    0xDE448D,
    0xE630B4,
    0xE619DC,
    0xE600FF,
    0xDC00FF,
    0xCC00FF,
    0xB432FF,
    0xB93CFF,
    0xC542FF,
    0xAA5AFF,
    0xAA72FF,
    0x8272FF,
    0x6473FF,
]


def _rgb_parts(value: int) -> tuple[int, int, int]:
    return value >> 16 & 255, value >> 8 & 255, value & 255


# squared distance, first wins on a tie
def nearest(rgb: tuple[int, int, int], palette) -> int:
    def distance(index: int) -> int:
        candidate = _rgb_parts(palette[index])
        return sum((a - b) ** 2 for a, b in zip(rgb, candidate, strict=True))

    return min(range(len(palette)), key=distance)


# The RGB rekordbox writes after the code, by code (0 to 64): the device palette of the rbxport
# notes (docs/reference/usb-export-db.md, Cue record tables), a hue ramp. The codes 1 to 62 are the
# ones of HOT_PALETTE in the same order, HOT_PALETTE has the colours rekordbox shows for them.
# attention -> bug = the colours of HOT_PALETTE went after the code, rekordbox writes these
DEVICE_PALETTE = [
    0x000000,
    0x0000FF,
    0x001CFF,
    0x0038FF,
    0x0054FF,
    0x0070FF,
    0x008CFF,
    0x00A8FF,
    0x00C4FF,
    0x00E0FF,
    0x00FFFF,
    0x00FFE8,
    0x00FFD1,
    0x00FFBA,
    0x00FFA3,
    0x00FF8C,
    0x00FF75,
    0x00FF5E,
    0x00FF47,
    0x00FF30,
    0x00FF1A,
    0x00FF00,
    0x1AFF00,
    0x33FF00,
    0x4DFF00,
    0x66FF00,
    0x80FF00,
    0x99FF00,
    0xB3FF00,
    0xCCFF00,
    0xE6FF00,
    0xFFFF00,
    0xFFE800,
    0xFFD100,
    0xFFBA00,
    0xFFA300,
    0xFF8C00,
    0xFF7500,
    0xFF5E00,
    0xFF4700,
    0xFF3000,
    0xFF1A00,
    0xFF0000,
    0xFF0017,
    0xFF002E,
    0xFF0045,
    0xFF005C,
    0xFF0073,
    0xFF008A,
    0xFF00A1,
    0xFF00B8,
    0xFF00CF,
    0xFF00E6,
    0xFF00FF,
    0xE600FF,
    0xCC00FF,
    0xB300FF,
    0x9900FF,
    0x8000FF,
    0x6600FF,
    0x4D00FF,
    0x3300FF,
    0x1A00FF,
    0x000000,
    0xFFFFFF,
]


# (palette code, 0xRRGGBB): the cue own #RRGGBB or the slot default, snapped to the palette
def hot_color(c: Cue) -> tuple[int, int]:
    wanted = HOT_RGB[c.hotcue % 8]
    text = c.color.lstrip("#")
    if len(text) in (6, 8):
        try:
            wanted = _rgb_parts(int(text[-6:], 16))
        except ValueError:
            pass
    # the code whose shown or written colour is the nearest: a colour read back from a key
    # (the written one) finds its own code again
    code = nearest(wanted, HOT_PALETTE + DEVICE_PALETTE[1:63]) % len(HOT_PALETTE) + 1
    return code, DEVICE_PALETTE[code]


# (rekordbox colour id, word = palette code << 24 | 0xRRGGBB), memory cues (0, 0). The native core unpacks the word itself.
def cue_color(c: Cue) -> tuple[int, int]:
    if c.hotcue < 0:
        return 0, 0
    code, rgb = hot_color(c)
    known = [int(h, 16) for h in RB_COLORS]
    # from the colour shown for the code, as before the device palette
    shown = HOT_PALETTE[code - 1] if 1 <= code <= len(HOT_PALETTE) else rgb
    color_id = nearest(_rgb_parts(shown), known) + 1
    return color_id, code << 24 | rgb


# --------------------------------------------------------
def _cue_start(cue: Cue) -> int:
    return round(max(0.0, cue.start))


# PCOB entry: position and loop only
def _plain_cue_entry(c: Cue) -> bytes:
    is_loop = c.is_loop
    loop_end = round(c.start + c.length) if is_loop else NO_LOOP_END
    fixed = struct.pack(
        ">IIIIIHHBBHII",
        CUE_ENTRY_HEADER,
        CUE_ENTRY_LEN,
        _slot_number(c),
        0,
        WAVE_UNKNOWN,
        ORDER_NONE,
        ORDER_NONE,
        CUE_LOOP if is_loop else CUE_PLAIN,
        0,
        CUE_UNKNOWN_1000,
        _cue_start(c),
        loop_end,
    )
    return b"PCPT" + fixed + bytes(16)


# PCO2 entry: adds name and colour
def _extended_cue_entry(c: Cue) -> bytes:
    is_loop = c.is_loop
    label = c.name.strip()
    label_bytes = label.encode("utf-16-be") + b"\0\0" if label else b""
    entry_len = CUE_EXT_FIXED + len(label_bytes) + CUE_EXT_TAIL
    color_id, word = cue_color(c)
    palette_code, rgb = word >> 24, word & 0xFFFFFF
    fixed = struct.pack(
        ">IIIBBHII",
        CUE_EXT_HEADER,
        entry_len,
        _slot_number(c),
        CUE_LOOP if is_loop else CUE_PLAIN,
        0,
        CUE_UNKNOWN_1000,
        _cue_start(c),
        round(c.start + c.length) if is_loop else 0,
    )
    return (
        b"PCP2"
        + fixed
        + bytes((color_id,))
        + bytes(7)
        + struct.pack(">HHI", 0, 0, len(label_bytes))
        + label_bytes
        + bytes((palette_code, *_rgb_parts(rgb)))
    )


# the player has pads A to H only: a slot past H has nowhere to go, and the key reader drops it
def hot_cues(t: Track) -> list[Cue]:
    return [c for c in t.hot_cues() if c.hotcue < MAX_HOT_CUES][:MAX_HOT_CUES]


# hot cues (kind 1, 8 slots max) then memory cues (kind 0)
def cue_lists(t: Track, ext: bool) -> list[bytes]:
    lists = []
    for kind, cues in ((1, hot_cues(t)), (0, t.memory_cues())):
        if ext:
            head = struct.pack(">IHH", kind, len(cues), 0)
            body = b"".join(map(_extended_cue_entry, cues))
            lists.append(_section(b"PCO2", head, body))
        else:
            head = struct.pack(">IHHI", kind, 0, len(cues), 0xFFFFFFFF)
            body = b"".join(map(_plain_cue_entry, cues))
            lists.append(_section(b"PCOB", head, body))
    return lists


# --------------------------------------------------------
# reference writer, the tests compare it with the compiled core
def build_py(t: Track, usb_path: str, audio: Audio) -> tuple[bytes, bytes]:
    detail = _detail_wave(t, audio)
    dat = pmai(
        [
            ppth(usb_path),
            _vbr_section(detail.frames),
            pqtz(t),
            pwav(audio.at(PREVIEW_COLUMNS)),
            _tiny_wave(audio.at(TINY_COLUMNS)),
            *cue_lists(t, False),
        ]
    )
    ext = pmai(
        [
            ppth(usb_path),
            _detail_mono(detail),
            *cue_lists(t, False),
            *cue_lists(t, True),
            _detail_color(detail),
            _color_preview(audio.at(COLOR_PREVIEW_COLUMNS)),
        ]
    )
    return dat, ext


def _native_cue(c: Cue):
    return c.hotcue, c.start, c.length, c.name.strip(), *cue_color(c)


# (DAT, EXT). Native core when it answers, else build_py.
def build(t: Track, usb_path: str, audio: Audio) -> tuple[bytes, bytes]:
    if t.grid is not None and t.bpm > MAX_BPM:
        raise ValueError("CDJ beat grids support at most 655.35 BPM")
    if native.available():
        detail = _detail_wave(t, audio)
        built = native.anlz_build(
            usb_path,
            t.bpm if t.grid is not None else 0.0,
            t.duration,
            t.grid or 0.0,
            detail.frames,
            [_native_cue(c) for c in hot_cues(t)],
            [_native_cue(c) for c in t.memory_cues()],
            [
                detail,
                audio.at(PREVIEW_COLUMNS),
                audio.at(TINY_COLUMNS),
                audio.at(COLOR_PREVIEW_COLUMNS),
            ],
        )
        if built is not None:
            return built
    return build_py(t, usb_path, audio)
