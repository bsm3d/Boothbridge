# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reads the CDJ player settings files, checks their envelope and patches single settings without rebuilding them.

# The four files in PIONEER (see reference.py) share one envelope. This module checks that
# envelope and returns the bytes of the body, it never rebuilds a file it cannot read.
# A file that fails a check is reported and left as it is: the owner's key is not ours to repair.

from __future__ import annotations

import binascii
import os
import struct
from dataclasses import dataclass, field

from ..manifest import write_atomic
from . import reference

HEAD = struct.Struct("<I32s32s32sI")
TAIL = struct.Struct("<HH")

# first bytes of each body, and whether the checksum also covers the header
RULES = {
    "MYSETTING.DAT": (reference.PLAYER_BODY_MAGIC, False),
    "MYSETTING2.DAT": (b"", False),
    "DJMMYSETTING.DAT": (reference.MIXER_BODY_MAGIC, True),
    "DEVSETTING.DAT": (reference.DEVICE_BODY_MAGIC, False),
}

# why a file is refused, in the order the checks run
TOO_SHORT, HEADER, APP, SIZE, CHECKSUM, END, MAGIC, LAYOUT = (
    "short",
    "header",
    "app",
    "size",
    "checksum",
    "end",
    "magic",
    "layout",
)

@dataclass
class Settings:
    name: str
    problems: list[str] = field(default_factory=list)
    body: bytes = b""
    brand: str = ""
    version: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    # position counts from 1 like the notes of the format, None past the end of the body
    def value(self, position: int) -> int | None:
        if 1 <= position <= len(self.body):
            return self.body[position - 1]
        return None

def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")

# ----------------------------------------------------------------
# name is the file name, it decides which body magic and which checksum are expected
def read(name: str, data: bytes) -> Settings:
    out = Settings(name)
    if name not in RULES:
        out.problems.append(HEADER)
        return out
    magic, whole = RULES[name]
    if len(data) < HEAD.size + TAIL.size:
        out.problems.append(TOO_SHORT)
        return out
    head_size, brand, app, version, size = HEAD.unpack_from(data)
    out.brand, out.version = _text(brand), _text(version)
    if head_size != reference.HEADER_SIZE:
        out.problems.append(HEADER)
    if _text(app) != "rekordbox":
        out.problems.append(APP)
    if HEAD.size + size + TAIL.size != len(data):
        out.problems.append(SIZE)
        return out
    end = HEAD.size + size
    out.body = data[HEAD.size : end]
    crc, filler = TAIL.unpack_from(data, end)
    if crc != binascii.crc_hqx(data[:end] if whole else out.body, 0):
        out.problems.append(CHECKSUM)
    if filler != reference.CHECKSUM_FILLER:
        out.problems.append(END)
    if not out.body.startswith(magic):
        out.problems.append(MAGIC)
    return out

# None when the file is not there. A file that cannot be read counts as too short.
def read_file(path: str) -> Settings | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 16)
    except OSError:
        data = b""
    return read(os.path.basename(path), data)

# the problems of the files that are on the key, as (file name, reason)
def check(pioneer_dir: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in reference.SETTINGS:
        got = read_file(os.path.join(pioneer_dir, name))
        if got is not None:
            found += [(name, why) for why in got.problems]
    return found

# ----------------------------------------------------------------
# The player file of the key with some bytes changed: {position: value}, only inside the settings
# area of the body (past the magic). None when the file cannot be trusted as it is.
def patched(got: Settings, data: bytes, changes: dict[int, int]) -> bytes | None:
    if not got.ok or len(got.body) != reference.PLAYER_BODY_SIZE:
        return None
    body = bytearray(got.body)
    for position, value in changes.items():
        if not len(reference.PLAYER_BODY_MAGIC) < position <= len(body) or not 0 <= value <= 0xFF:
            return None
        body[position - 1] = value
    content = data[: HEAD.size] + bytes(body)
    return content + TAIL.pack(binascii.crc_hqx(bytes(body), 0), reference.CHECKSUM_FILLER)

# Changes the chosen settings in MYSETTING.DAT when the key already has it. Other bytes stay as the
# owner left them. Returns the problems that kept the file from being changed.
def apply(pioneer_dir: str, changes: dict[int, int]) -> list[tuple[str, str]]:
    name = "MYSETTING.DAT"
    path = os.path.join(pioneer_dir, name)
    got = read_file(path)
    if not changes or got is None or not got.ok:
        return []
    with open(path, "rb") as f:
        data = f.read()
    if len(got.body) != reference.PLAYER_BODY_SIZE:
        return [(name, LAYOUT)]
    new = patched(got, data, changes)
    if new is None:
        return [(name, LAYOUT)]
    if new != data:
        write_atomic(path, new)
    return []
