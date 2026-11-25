# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reader for ANLZ analysis files that returns path, frame count, beat grid and cues, with size checks.

# Counterpart of anlz.py: path, frame count, beat grid (tempo and anchor) and cues from the analysis files. Big endian, nothing from Pioneer software.
# Only PPTH, PVBR, PQTZ, PCOB and PCO2 are read, other sections skipped by length. Every length is checked against the file size first: the key can come from anywhere.
# Only AnlzError escapes.

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass, field

# limits against damaged or hostile files
MAX_FILE = 256 << 20  # a ten hour track makes about 16 MB of analysis
MAX_SECTION = 32 << 20  # largest section read in one piece, which is a cue list
MAX_BEATS = 100_000  # ten hours at 166 BPM
MAX_PATH = 8192  # bytes of a PPTH payload
MAX_SECTIONS = 256  # a real file has about ten

# offsets from the start of a section or an entry
SECTION_PREFIX = 12  # name, header length, section length
FILE_HEADER_MIN = 28
VBR_COUNT_BYTES = 4
BEAT_ENTRY_BYTES = 8  # beat in bar (2), tempo x 100 (2), time in ms (4)
BEAT_HEADER_MIN = 24
BEAT_COUNT_AT = 20
PCOB_HEADER_MIN = 24
PCO2_HEADER_MIN = 20
PCOB_COUNT_AT = 18
PCO2_COUNT_AT = 16
PATH_HEADER_MIN = 16

PLAIN_ENTRY_MIN = 0x28
PLAIN_SLOT_AT = 0x0C
PLAIN_KIND_AT = 0x1C
PLAIN_TIMES_AT = 0x20
EXT_ENTRY_MIN = 0x2C
EXT_SLOT_AT = 0x0C
EXT_KIND_AT = 0x10
EXT_TIMES_AT = 0x14
EXT_LABEL_LEN_AT = 0x28
EXT_LABEL_AT = 0x2C
EXT_COLOR_BYTES = 4  # palette code, then red, green, blue

KIND_PLAIN, KIND_LOOP = 1, 2
NO_LOOP_END = 0xFFFFFFFF
LAST_HOT_SLOT = 8

# --------------------------------------------------------
class AnlzError(ValueError):
    pass

# --------------------------------------------------------
@dataclass(slots=True)
class AnlzCue:
    slot: int  # 0 memory cue, 1 to 8 hot cue A to H
    loop: bool
    start: int  # ms
    end: int | None = None  # ms, None unless it is a loop
    name: str = ""
    color: str = ""  # "#RRGGBB" of a hot cue, "" when the file has none
    palette: int = 0  # hot cue palette code, 0 when unknown

@dataclass(slots=True)
class Anlz:
    path: str = ""
    frames: int = 0  # PVBR sample count, 0 when the audio could not be decoded
    bpm: float = 0.0
    grid: float | None = None  # ms, a beat 1 of the grid, None without a beat grid
    beats: int = 0
    cues: list[AnlzCue] = field(default_factory=list)
    detailed: bool = False  # the cues come from PCO2: names and colours are there
    whole: bool = False  # combine: both files of the track were read

# --------------------------------------------------------
class _Window:
    def __init__(self, stream, size: int):
        self.stream = stream
        self.size = size

    def take(self, pos: int, count: int) -> bytes:
        if count < 0 or pos < 0 or pos + count > self.size:
            raise AnlzError("a length points outside the file")
        self.stream.seek(pos)
        got = self.stream.read(count)
        if len(got) != count:
            raise AnlzError("the file is shorter than it says")
        return got

    def number(self, pos: int, code: str) -> int:
        return struct.unpack(code, self.take(pos, struct.calcsize(code)))[0]

# --------------------------------------------------------
# None for what a player does not show (slot above H, type other than cue and loop). A
# loop needs a real end time. Colour = 4 bytes palette code + RGB of a hot cue, all zero
# means none written, memory cues never have one.
def _cue_from_fields(
    slot: int, kind: int, start: int, end: int, name: str, color_bytes: bytes
) -> AnlzCue | None:
    if slot > LAST_HOT_SLOT or kind not in (KIND_PLAIN, KIND_LOOP):
        return None
    is_loop = kind == KIND_LOOP and end != NO_LOOP_END
    has_color = slot and len(color_bytes) == EXT_COLOR_BYTES and any(color_bytes)
    if has_color:
        red, green, blue = color_bytes[1:4]
        color = f"#{red:02X}{green:02X}{blue:02X}"
    else:
        color = ""
    palette = color_bytes[0] if color else 0
    return AnlzCue(slot, is_loop, start, end if is_loop else None, name, color, palette)

def _plain_entry(entry: bytes) -> AnlzCue | None:
    if len(entry) < PLAIN_ENTRY_MIN:
        raise AnlzError("a cue is too short")
    (slot,) = struct.unpack_from(">I", entry, PLAIN_SLOT_AT)
    start, end = struct.unpack_from(">II", entry, PLAIN_TIMES_AT)
    return _cue_from_fields(slot, entry[PLAIN_KIND_AT], start, end, "", b"")

def _extended_entry(entry: bytes) -> AnlzCue | None:
    if len(entry) < EXT_ENTRY_MIN:
        raise AnlzError("a cue is too short")
    (slot,) = struct.unpack_from(">I", entry, EXT_SLOT_AT)
    start, end = struct.unpack_from(">II", entry, EXT_TIMES_AT)
    (label_len,) = struct.unpack_from(">I", entry, EXT_LABEL_LEN_AT)
    label_end = EXT_LABEL_AT + label_len
    if label_end > len(entry):
        raise AnlzError("a cue name runs past its cue")
    label = entry[EXT_LABEL_AT:label_end].decode("utf-16-be", "replace").rstrip("\0")
    color_bytes = entry[label_end : label_end + EXT_COLOR_BYTES]
    return _cue_from_fields(slot, entry[EXT_KIND_AT], start, end, label, color_bytes)

# entry = name (PCPT or PCP2), header length, entry length, which gives the next one
def _read_cue_entries(
    body: bytes, count: int, entry_name: bytes, extended: bool
) -> list[AnlzCue]:
    read_entry = _extended_entry if extended else _plain_entry
    cues = []
    offset = 0
    for _ in range(count):
        if offset + SECTION_PREFIX > len(body):
            raise AnlzError("a cue is cut short")
        head_len, entry_len = struct.unpack_from(">II", body, offset + 4)
        bad_name = body[offset : offset + 4] != entry_name
        if (
            bad_name
            or not SECTION_PREFIX <= head_len <= entry_len
            or offset + entry_len > len(body)
        ):
            raise AnlzError("a cue has an impossible length")
        entry = body[offset : offset + entry_len]
        offset += entry_len
        cue = read_entry(entry)
        if cue is not None:
            cues.append(cue)
    if offset != len(body):
        raise AnlzError("the cue count does not match the section")
    return cues

# --------------------------------------------------------
# Returns (anchor in ms, bpm) from the PQTZ entries.
# The writer rounds every beat to a millisecond, so the middle of that range is taken: writing the grid again gives the same bytes.
# The anchor is always a beat 1 (the first beat if it opens a bar, else the beat 1 before it). An uneven grid keeps the time of its first beat 1.
def _fit_grid(entries: list[tuple[int, int, int]]) -> tuple[float | None, float]:
    tempo = next((t for _, t, _ in entries if t), 0)
    if not entries or not tempo:
        return None, 0.0
    bpm = tempo / 100
    interval = 60000 / bpm
    first_in_bar = entries[0][0]
    beats_before_one = (1 - first_in_bar) % 4
    is_even = 1 <= first_in_bar <= 4 and all(
        pos == (i + first_in_bar - 1) % 4 + 1 and tmp == tempo
        for i, (pos, tmp, _) in enumerate(entries)
    )
    if is_even:
        low, high = 0.0, interval
        for i, (_, _, when) in enumerate(entries):
            low = max(low, when - 0.5 - i * interval)
            high = min(high, when + 0.5 - i * interval)
        if low < high:
            return (low + high) / 2 + beats_before_one * interval, bpm
    first_one = next((when for pos, _, when in entries if pos == 1), entries[0][2])
    return float(first_one), bpm

# --------------------------------------------------------
class _Collected:
    def __init__(self):
        self.out = Anlz()
        self.plain_cues: list[AnlzCue] = []
        self.extended_cues: list[AnlzCue] = []
        self.grid_entries: list[tuple[int, int, int]] = []
        self.saw_extended = False

def _read_path(win: _Window, got: _Collected, pos: int, head_len: int, size: int):
    if head_len < PATH_HEADER_MIN or not 2 <= size - head_len <= MAX_PATH:
        raise AnlzError("the path section is not usable")
    raw = win.take(pos + head_len, size - head_len)
    even = raw[: len(raw) // 2 * 2]
    got.out.path = even.decode("utf-16-be", "replace").rstrip("\0")

def _read_vbr(win: _Window, got: _Collected, pos: int, head_len: int, size: int):
    # frame count = last 4 bytes, after the seek table
    if size >= head_len + VBR_COUNT_BYTES:
        got.out.frames = win.number(pos + size - VBR_COUNT_BYTES, ">I")

def _read_beat_grid(win: _Window, got: _Collected, pos: int, head_len: int, size: int):
    if head_len < BEAT_HEADER_MIN:
        raise AnlzError("the beat grid header is too short")
    count = win.number(pos + BEAT_COUNT_AT, ">I")
    if size - head_len != BEAT_ENTRY_BYTES * count:
        raise AnlzError("the beat count does not match the section")
    got.out.beats = count
    raw = win.take(pos + head_len, BEAT_ENTRY_BYTES * min(count, MAX_BEATS))
    got.grid_entries = list(struct.iter_unpack(">HHI", raw))

def _read_cue_list(
    win: _Window, got: _Collected, pos: int, head_len: int, size: int, extended: bool
):
    least_header = PCO2_HEADER_MIN if extended else PCOB_HEADER_MIN
    if head_len < least_header or size - head_len > MAX_SECTION:
        raise AnlzError("the cue list is not usable")
    count_at = PCO2_COUNT_AT if extended else PCOB_COUNT_AT
    count = win.number(pos + count_at, ">H")
    cues = _read_cue_entries(
        win.take(pos + head_len, size - head_len),
        count,
        b"PCP2" if extended else b"PCPT",
        extended,
    )
    if extended:
        got.saw_extended = True
        got.extended_cues += cues
    else:
        got.plain_cues += cues

def _parse_stream(stream, size: int) -> Anlz:
    if size > MAX_FILE:
        raise AnlzError("the file is too large")
    win = _Window(stream, size)
    if size < FILE_HEADER_MIN or win.take(0, 4) != b"PMAI":
        raise AnlzError("not an ANLZ file")
    file_head_len, total = struct.unpack(">II", win.take(4, 8))
    if not SECTION_PREFIX <= file_head_len <= total <= size:
        raise AnlzError("the file header has an impossible length")
    got = _Collected()
    pos = file_head_len
    for _ in range(MAX_SECTIONS):
        if pos >= total:
            break
        if pos + SECTION_PREFIX > total:
            raise AnlzError("a section header is cut short")
        prefix = win.take(pos, SECTION_PREFIX)
        name = prefix[:4]
        head_len, section_len = struct.unpack(">II", prefix[4:])
        if not SECTION_PREFIX <= head_len <= section_len or pos + section_len > total:
            raise AnlzError(f"section {name!r} has an impossible length")
        if name == b"PPTH":
            _read_path(win, got, pos, head_len, section_len)
        elif name == b"PVBR":
            _read_vbr(win, got, pos, head_len, section_len)
        elif name == b"PQTZ":
            _read_beat_grid(win, got, pos, head_len, section_len)
        elif name in (b"PCOB", b"PCO2"):
            _read_cue_list(win, got, pos, head_len, section_len, name == b"PCO2")
        pos += section_len
    else:
        raise AnlzError("too many sections")
    out = got.out
    out.grid, out.bpm = _fit_grid(got.grid_entries)
    out.detailed = got.saw_extended
    out.cues = got.extended_cues if got.saw_extended else got.plain_cues
    return out

# --------------------------------------------------------
def parse(data: bytes) -> Anlz:
    return _parse_stream(io.BytesIO(data), len(data))

# OSError if it cannot be opened, AnlzError if it is not ANLZ
def read(path: str) -> Anlz:
    with open(path, "rb") as f:
        return _parse_stream(f, os.fstat(f.fileno()).st_size)

# DAT first for path, frames and grid. Cues from the EXT when it has named and coloured
# ones, else from whichever file has any (an empty PCO2 next to a filled PCOB).
def combine(dat: Anlz | None, ext: Anlz | None) -> Anlz:
    merged = Anlz(whole=dat is not None and ext is not None)
    for part in (dat, ext):
        if part is None:
            continue
        merged.path = merged.path or part.path
        merged.frames = merged.frames or part.frames
        if merged.grid is None and part.grid is not None:
            merged.grid, merged.bpm, merged.beats = part.grid, part.bpm, part.beats
    if ext is not None and ext.detailed and ext.cues:
        merged.cues, merged.detailed = ext.cues, True
    else:
        with_cues = (p.cues for p in (dat, ext) if p is not None and p.cues)
        merged.cues = next(with_cues, [])
    return merged
