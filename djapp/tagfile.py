# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reads and writes audio tags (ID3v2, FLAC, Ogg, MP4, RIFF, AIFF) from the public format specifications, with no tag library.

from __future__ import annotations

import os
import re
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

BLOCK = 4 << 20
PADDING = 1024

class TagError(ValueError):
    pass

@dataclass(frozen=True)
class Info:
    format: str
    length: float

# ------------------------------------------------------------
# the fields the editor knows, and the name each format gives them
ID3_NAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "album_artist": "TPE2",
    "genre": "TCON",
    "label": "TPUB",
    "remixer": "TPE4",
    "composer": "TCOM",
    "year": "TDRC",
    "track_no": "TRCK",
    "disc_no": "TPOS",
    "bpm": "TBPM",
    "key": "TKEY",
}
VORBIS_NAMES = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "albumartist",
    "genre": "genre",
    "label": "organization",
    "remixer": "remixer",
    "composer": "composer",
    "comment": "comment",
    "year": "date",
    "track_no": "tracknumber",
    "disc_no": "discnumber",
    "bpm": "bpm",
    "key": "initialkey",
}
MP4_NAMES = {
    "title": b"\xa9nam",
    "artist": b"\xa9ART",
    "album": b"\xa9alb",
    "album_artist": b"aART",
    "genre": b"\xa9gen",
    "composer": b"\xa9wrt",
    "comment": b"\xa9cmt",
    "year": b"\xa9day",
}
MP4_CUSTOM = {"label": "LABEL", "remixer": "REMIXER", "bpm": "BPM", "key": "INITIALKEY"}
MP4_NUMBERS = {"track_no": b"trkn", "disc_no": b"disk"}
MP4_MEAN = "com.apple.iTunes"
NUMBER_FIELDS = ("track_no", "disc_no")

# A new track or disc number keeps the "/12" the file had after the old one
def with_total(value: str, old: str) -> str:
    total = old.partition("/")[2].strip()
    return f"{value}/{total}" if total.isdecimal() and "/" not in value else value

# ------------------------------------------------------------
def rewrite(path: str, produce):
    # the new content goes to a file next to the old one, then takes its place
    folder = os.path.dirname(os.path.abspath(path))
    fd, name = tempfile.mkstemp(prefix=".bb-tagfile-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out:
            produce(out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    except BaseException:
        if os.path.exists(name):
            os.unlink(name)
        raise

def copy_range(src, out, start: int, end: int):
    src.seek(start)
    left = end - start
    while left > 0:
        block = src.read(min(BLOCK, left))
        if not block:
            raise TagError("The audio file ends before its declared size")
        out.write(block)
        left -= len(block)

def syncsafe(b: bytes) -> int:
    return (b[0] & 127) << 21 | (b[1] & 127) << 14 | (b[2] & 127) << 7 | (b[3] & 127)

def to_syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127])

# ------------------------------------------------------------
# ID3v2
ENCODINGS = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}
FRAME_ID = re.compile(rb"^[A-Z0-9]{4}$")

def terminated(raw: bytes, enc: int) -> tuple[bytes, bytes]:
    if enc in (1, 2):
        i = 0
        while i + 1 < len(raw):
            if raw[i] == 0 and raw[i + 1] == 0:
                return raw[:i], raw[i + 2 :]
            i += 2
        return raw, b""
    i = raw.find(b"\0")
    return (raw, b"") if i < 0 else (raw[:i], raw[i + 1 :])

def decode_string(raw: bytes, enc: int) -> str:
    codec = ENCODINGS.get(enc, "latin-1")
    return raw.decode(codec, "replace").lstrip("﻿")

def text_of(body: bytes) -> str:
    if not body:
        return ""
    return decode_string(body[1:], body[0]).split("\0")[0]

def encode_text(value: str, version: int) -> tuple[int, bytes]:
    if version >= 4:
        return 3, value.encode("utf-8")
    try:
        return 0, value.encode("latin-1")
    except UnicodeEncodeError:
        return 1, b"\xff\xfe" + value.encode("utf-16-le")

class Frame:
    def __init__(self, fid: str, flags: int, data: bytes):
        self.id, self.flags, self.data = fid, flags, data

class Id3Tag:
    def __init__(self, version: int = 4):
        self.version = version
        self.frames: list[Frame] = []

    @staticmethod
    def total_size(head: bytes) -> int:
        footer = 10 if head[3] == 4 and head[5] & 0x10 else 0
        return 10 + syncsafe(head[6:10]) + footer

    @classmethod
    def parse(cls, blob: bytes) -> Id3Tag:
        if len(blob) < 10 or blob[:3] != b"ID3":
            raise TagError("No ID3 tag")
        major, flags = blob[3], blob[5]
        if major == 2:
            raise ValueError(
                "ID3v2.2 files must be converted to ID3v2.3/2.4 before editing"
            )
        if major not in (3, 4) or any(blob[6 + i] & 0x80 for i in range(4)):
            raise TagError("Unsupported or damaged ID3 tag")
        size = syncsafe(blob[6:10])
        body = blob[10 : 10 + size]
        if len(body) < size:
            raise TagError("The ID3 tag ends before its declared size")
        tag = cls(major)
        if major == 3 and flags & 0x80:
            body = body.replace(b"\xff\x00", b"\xff")
        pos = 0
        if flags & 0x40:  # extended header, dropped on write
            if len(body) < 4:
                raise TagError("Damaged ID3 extended header")
            skip = (
                syncsafe(body[:4])
                if major == 4
                else struct.unpack(">I", body[:4])[0] + 4
            )
            pos = skip
        whole_unsync = major == 4 and flags & 0x80
        while pos + 10 <= len(body):
            fid = body[pos : pos + 4]
            if fid[0] == 0:
                if any(body[pos:]):
                    raise TagError("Damaged ID3 tag: data after the padding")
                break
            if not FRAME_ID.match(fid):
                raise TagError("Damaged ID3 tag: invalid frame name")
            n = (
                syncsafe(body[pos + 4 : pos + 8])
                if major == 4
                else struct.unpack(">I", body[pos + 4 : pos + 8])[0]
            )
            fflags = struct.unpack(">H", body[pos + 8 : pos + 10])[0]
            data = body[pos + 10 : pos + 10 + n]
            if len(data) < n:
                raise TagError("Damaged ID3 tag: a frame ends past the tag")
            if major == 4 and (whole_unsync or fflags & 0x0002):
                lead = 4 if fflags & 0x0001 else 0
                data = data[:lead] + data[lead:].replace(b"\xff\x00", b"\xff")
                fflags &= ~0x0002
            tag.frames.append(Frame(fid.decode("ascii"), fflags, data))
            pos += 10 + n
        return tag

    def dump(self, padding: int = PADDING) -> bytes:
        body = bytearray()
        for f in self.frames:
            size = (
                to_syncsafe(len(f.data))
                if self.version == 4
                else struct.pack(">I", len(f.data))
            )
            body += f.id.encode("ascii") + size + struct.pack(">H", f.flags) + f.data
        body += bytes(padding)
        if len(body) >= 1 << 28:
            raise TagError("The ID3 tag is too large")
        return (
            b"ID3" + bytes([self.version, 0, 0]) + to_syncsafe(len(body)) + bytes(body)
        )

    def find(self, fid: str) -> Frame | None:
        return next((f for f in self.frames if f.id == fid), None)

    def text(self, fid: str) -> str:
        f = self.find(fid)
        return text_of(f.data) if f else ""

    def delete(self, fid: str):
        self.frames = [f for f in self.frames if f.id != fid]

    def set_text(self, fid: str, value: str):
        self.delete(fid)
        if value:
            enc, raw = encode_text(value, self.version)
            self.frames.append(Frame(fid, 0, bytes([enc]) + raw))

    # a comment is the frame with no description, the ones with one are the DJ programs'.
    # eng first, then any language ("XXX" from many taggers): the folder reader shows those too
    def regular_comment(self) -> Frame | None:
        found = [
            f
            for f in self.frames
            if f.id == "COMM"
            and len(f.data) > 4
            and not decode_string(terminated(f.data[4:], f.data[0])[0], f.data[0])
        ]
        return next(
            (f for f in found if f.data[1:4] == b"eng"), found[0] if found else None
        )

    def comment(self) -> str:
        f = self.regular_comment()
        if f is None:
            return ""
        _desc, text = terminated(f.data[4:], f.data[0])
        return decode_string(text, f.data[0]).split("\0")[0]

    def set_comment(self, value: str):
        f = self.regular_comment()
        while f is not None:
            self.frames.remove(f)
            f = self.regular_comment()
        if value:
            enc, raw = encode_text(value, self.version)
            desc = b"\xff\xfe\0\0" if enc == 1 else b"\0"
            self.frames.append(Frame("COMM", 0, bytes([enc]) + b"eng" + desc + raw))

    def get(self, field: str) -> str:
        if field == "comment":
            return self.comment()
        if field == "year":
            return self.text("TDRC") or self.text("TYER")
        return self.text(ID3_NAMES[field])

    def set(self, field: str, value: str):
        if field == "comment":
            self.set_comment(value)
        elif field == "year":
            self.delete("TDRC")
            self.delete("TYER")
            self.set_text("TYER" if self.version == 3 else "TDRC", value)
        else:
            self.set_text(ID3_NAMES[field], value)

# ------------------------------------------------------------
class Tagged:
    def __init__(self, path: str):
        self.path = str(path)

    def get(self, field: str) -> str:
        raise NotImplementedError

    def raw_set(self, field: str, value: str):
        raise NotImplementedError

    def set(self, field: str, value: str):
        if field in NUMBER_FIELDS and value:
            value = with_total(value, self.get(field))
        self.raw_set(field, value)

    def save(self):
        raise NotImplementedError

# ------------------------------------------------------------
# MPEG audio with an ID3v2 tag in front
MPEG_RATES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
SAMPLE_RATES = {
    1: [44100, 48000, 32000],
    2: [22050, 24000, 16000],
    3: [11025, 12000, 8000],
}

def mpeg_header(h: bytes):
    if len(h) < 4 or h[0] != 0xFF or h[1] & 0xE0 != 0xE0:
        return None
    version = {3: 1, 2: 2, 0: 3}.get((h[1] >> 3) & 3)  # MPEG 1, 2, 2.5
    layer = {3: 1, 2: 2, 1: 3}.get((h[1] >> 1) & 3)
    rate_index, sr_index = h[2] >> 4, (h[2] >> 2) & 3
    if version is None or layer is None or rate_index in (0, 15) or sr_index == 3:
        return None
    family = 1 if version == 1 else 2
    bitrate = MPEG_RATES[(family, layer)][rate_index] * 1000
    rate = SAMPLE_RATES[version][sr_index]
    samples = 384 if layer == 1 else 1152 if layer == 2 or version == 1 else 576
    return version, layer, bitrate, rate, samples, h[3] >> 6 == 3

def find_mpeg(f, start: int, limit: int = 65536):
    f.seek(start)
    block = f.read(limit)
    for i in range(len(block) - 3):
        if block[i] == 0xFF and mpeg_header(block[i : i + 4]) is not None:
            # two frames in a row make a stream, one sync word can be chance
            _v, layer, bitrate, rate, samples, _mono = mpeg_header(block[i : i + 4])
            if layer == 1:
                size = (12 * bitrate // rate) * 4
            else:
                size = (
                    144 * bitrate // rate if samples == 1152 else 72 * bitrate // rate
                )
            nxt = i + size
            if nxt + 5 > len(block) or any(
                mpeg_header(block[n : n + 4]) is not None for n in (nxt, nxt + 1)
            ):
                return start + i
    return None

def mpeg_length(f, first: int, size: int) -> float:
    f.seek(first)
    head = f.read(4)
    _version, _layer, bitrate, rate, samples, _mono = mpeg_header(head)
    frame = f.read(200)
    for tag in (b"Xing", b"Info"):
        i = frame.find(tag)
        if 0 <= i < 40 and len(frame) >= i + 12 and frame[i + 7] & 1:
            frames = struct.unpack(">I", frame[i + 8 : i + 12])[0]
            return frames * samples / rate
    i = frame.find(b"VBRI")
    if 0 <= i < 40 and len(frame) >= i + 18:
        return struct.unpack(">I", frame[i + 14 : i + 18])[0] * samples / rate
    end = size
    f.seek(max(0, size - 128))
    if f.read(3) == b"TAG":
        end -= 128
    return max(0, end - first) * 8 / bitrate if bitrate else 0.0

class Mp3File(Tagged):
    def __init__(self, path: str):
        super().__init__(path)
        with open(path, "rb") as f:
            head = f.read(10)
            self.start = 0
            self.tag = Id3Tag(4)
            if head[:3] == b"ID3":
                if len(head) < 10:
                    raise TagError("Damaged ID3 header")
                total = (
                    Id3Tag.total_size(head)
                    if head[3] in (3, 4)
                    else 10 + syncsafe(head[6:10])
                )
                f.seek(0)
                blob = f.read(total)
                self.tag = Id3Tag.parse(blob)
                self.start = total
            if find_mpeg(f, self.start) is None:
                raise TagError("Can't sync to an MPEG frame")

    def get(self, field: str) -> str:
        return self.tag.get(field)

    def raw_set(self, field: str, value: str):
        self.tag.set(field, value)

    def save(self):
        blob = self.tag.dump()
        size = os.path.getsize(self.path)

        def produce(out):
            out.write(blob)
            with open(self.path, "rb") as src:
                copy_range(src, out, self.start, size)

        rewrite(self.path, produce)
        self.start = len(blob)

# ------------------------------------------------------------
# RIFF (WAV) and IFF (AIFF) chunks, with the tag in an ID3 chunk
class ChunkFile(Tagged):
    def __init__(self, path: str):
        super().__init__(path)
        with open(path, "rb") as f:
            head = f.read(12)
            size = os.path.getsize(path)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                self.order, self.pack = "little", "<I"
            elif head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
                self.order, self.pack = "big", ">I"
            else:
                raise TagError("Not a WAV or AIFF file")
            self.head = head
            self.end = min(size, int.from_bytes(head[4:8], self.order) + 8)
            if self.end < 20:
                self.end = size  # a size field left at 0 or 4 by a streaming writer
            self.chunks: list[tuple[bytes, int, int]] = []
            pos = 12
            while pos + 8 <= self.end:
                f.seek(pos)
                h = f.read(8)
                n = int.from_bytes(h[4:8], self.order)
                if pos + 8 + n > self.end:
                    # some encoders declare a size a byte short, a chunk that still fits the file is accepted
                    if pos + 8 + n > size:
                        raise TagError("A chunk ends past the end of the file")
                    self.end = size
                self.chunks.append((h[:4], pos, n))
                pos += 8 + n + (n & 1)
            self.size = size
            self.tag = Id3Tag(4)
            self.has_tag = False
            for cid, offset, n in self.chunks:
                if cid.lower() == b"id3 ":
                    f.seek(offset + 8)
                    self.tag = Id3Tag.parse(f.read(n))
                    self.has_tag = True
                    break

    def get(self, field: str) -> str:
        return self.tag.get(field)

    def raw_set(self, field: str, value: str):
        self.tag.set(field, value)

    def save(self):
        blob = self.tag.dump()
        chunk_id = b"id3 " if self.order == "little" else b"ID3 "
        new = (
            chunk_id
            + len(blob).to_bytes(4, self.order)
            + blob
            + (b"\0" if len(blob) & 1 else b"")
        )

        def produce(out):
            out.write(self.head)
            wrote = False
            copied = 12
            with open(self.path, "rb") as src:
                for cid, offset, n in self.chunks:
                    after = min(self.size, offset + 8 + n + (n & 1))
                    copied = after
                    if cid.lower() == b"id3 ":
                        if not wrote:
                            out.write(new)
                            wrote = True
                        continue
                    copy_range(src, out, offset, after)
                    # the writer left out the pad byte of an odd last chunk: the next chunk needs it
                    if n & 1 and offset + 8 + n >= self.size:
                        out.write(b"\0")
                if not wrote:
                    out.write(new)
                end = out.tell()
                if copied < self.size:
                    copy_range(src, out, copied, self.size)
            out.seek(4)
            out.write((end - 8).to_bytes(4, self.order))
            out.seek(0, 2)

        rewrite(self.path, produce)

# ------------------------------------------------------------
# Vorbis comments, the same layout in FLAC and in Ogg Vorbis
class Comments:
    def __init__(self, vendor: bytes = b"", items: list[tuple[str, str]] | None = None):
        self.vendor = vendor
        self.items = items or []

    @classmethod
    def parse(cls, body: bytes) -> Comments:
        try:
            n = struct.unpack("<I", body[:4])[0]
            vendor = body[4 : 4 + n]
            pos = 4 + n
            count = struct.unpack("<I", body[pos : pos + 4])[0]
            pos += 4
            items = []
            for _ in range(count):
                n = struct.unpack("<I", body[pos : pos + 4])[0]
                text = body[pos + 4 : pos + 4 + n]
                if len(text) < n:
                    raise TagError("Damaged Vorbis comments")
                pos += 4 + n
                key, sep, value = text.decode("utf-8", "surrogateescape").partition("=")
                items.append((key, value if sep else None))
            return cls(vendor, items)
        except struct.error as e:
            raise TagError("Damaged Vorbis comments") from e

    def dump(self) -> bytes:
        out = bytearray(
            struct.pack("<I", len(self.vendor))
            + self.vendor
            + struct.pack("<I", len(self.items))
        )
        for key, value in self.items:
            raw = (key if value is None else f"{key}={value}").encode(
                "utf-8", "surrogateescape"
            )
            out += struct.pack("<I", len(raw)) + raw
        return bytes(out)

    def first(self, name: str) -> str:
        return next((v for k, v in self.items if k.lower() == name), "")

    def drop(self, *names: str):
        self.items = [(k, v) for k, v in self.items if k.lower() not in names]

    def get(self, field: str) -> str:
        return self.first(VORBIS_NAMES[field])

    def set(self, field: str, value: str):
        name = VORBIS_NAMES[field]
        self.drop(name, *({"key": ("key",), "year": ("year",)}.get(field, ())))
        if value:
            self.items.append((name.upper(), value))

class FlacFile(Tagged):
    def __init__(self, path: str):
        super().__init__(path)
        self.blocks: list[tuple[int, bytes]] = []
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                raise TagError("Not a FLAC file")
            while True:
                h = f.read(4)
                if len(h) < 4:
                    raise TagError("The FLAC metadata ends too early")
                n = int.from_bytes(h[1:4], "big")
                data = f.read(n)
                if len(data) < n:
                    raise TagError("The FLAC metadata ends too early")
                self.blocks.append((h[0] & 0x7F, data))
                if h[0] & 0x80:
                    break
            self.audio = f.tell()
        if not self.blocks or self.blocks[0][0] != 0:
            raise TagError("The FLAC file has no STREAMINFO block")
        self.comments = Comments()
        for kind, data in self.blocks:
            if kind == 4:
                self.comments = Comments.parse(data)
                break

    def get(self, field: str) -> str:
        return self.comments.get(field)

    def raw_set(self, field: str, value: str):
        self.comments.set(field, value)

    def save(self):
        kept = [(k, d) for k, d in self.blocks if k not in (1, 4)]
        body = self.comments.dump()
        blocks = [kept[0], (4, body), *kept[1:]]
        size = os.path.getsize(self.path)

        def produce(out):
            out.write(b"fLaC")
            for i, (kind, data) in enumerate(blocks):
                if len(data) >= 1 << 24:
                    raise TagError("A FLAC metadata block is too large")
                out.write(
                    bytes([kind | (0x80 if i == len(blocks) - 1 else 0)])
                    + len(data).to_bytes(3, "big")
                    + data
                )
            with open(self.path, "rb") as src:
                copy_range(src, out, self.audio, size)

        rewrite(self.path, produce)

# ------------------------------------------------------------
# Ogg pages, for Vorbis: the comment header is the second packet of the stream
OGG_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = (
            ((_c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            if _c & 0x80000000
            else (_c << 1) & 0xFFFFFFFF
        )
    OGG_TABLE.append(_c)

def ogg_crc(data: bytes) -> int:
    crc = 0
    table = OGG_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[(crc >> 24) ^ b]
    return crc

def ogg_page(f):
    head = f.read(27)
    if len(head) == 0:
        return None
    if len(head) < 27 or head[:4] != b"OggS" or head[4] != 0:
        raise TagError("Damaged Ogg page")
    table = f.read(head[26])
    if len(table) < head[26]:
        raise TagError("Damaged Ogg page")
    data = f.read(sum(table))
    if len(data) < sum(table):
        raise TagError("The Ogg file ends inside a page")
    flags, granule, serial, seq = (
        head[5],
        struct.unpack("<q", head[6:14])[0],
        struct.unpack("<I", head[14:18])[0],
        struct.unpack("<I", head[18:22])[0],
    )
    return flags, granule, serial, seq, list(table), data

def build_page(
    flags: int, granule: int, serial: int, seq: int, table: list[int], data: bytes
) -> bytes:
    head = (
        b"OggS\0"
        + bytes([flags])
        + struct.pack("<qII", granule, serial, seq)
        + b"\0\0\0\0"
        + bytes([len(table)])
        + bytes(table)
    )
    page = bytearray(head + data)
    page[22:26] = struct.pack("<I", ogg_crc(bytes(page)))
    return bytes(page)

def ogg_packets(f, count: int):
    # the first count packets, the number of pages they take, the serial number and where the next page starts
    packets, current, pages, serial = [], bytearray(), 0, 0
    while len(packets) < count:
        page = ogg_page(f)
        if page is None:
            raise TagError("The Ogg stream ends inside its headers")
        _flags, _granule, serial, _seq, table, data = page
        pages += 1
        pos = 0
        for lace in table:
            current += data[pos : pos + lace]
            pos += lace
            if lace < 255:
                packets.append(bytes(current))
                current = bytearray()
    # an audio packet on the last header page would be lost when the headers are rewritten
    if current or len(packets) > count:
        raise TagError("The Ogg headers do not end on a page boundary")
    return packets, pages, serial, f.tell()

class OggFile(Tagged):
    def __init__(self, path: str):
        super().__init__(path)
        with open(path, "rb") as f:
            if f.read(4) != b"OggS":
                raise TagError("Not an Ogg file")
            f.seek(0)
            self.packets, self.pages, self.serial, self.audio = ogg_packets(f, 3)
        ident, comment = self.packets[0], self.packets[1]
        if ident[:7] != b"\x01vorbis" or comment[:7] != b"\x03vorbis":
            raise TagError("Only Ogg Vorbis files are supported")
        self.comments = Comments.parse(comment[7:])

    def get(self, field: str) -> str:
        return self.comments.get(field)

    def raw_set(self, field: str, value: str):
        self.comments.set(field, value)

    def save(self):
        ident, setup = self.packets[0], self.packets[2]
        comment = b"\x03vorbis" + self.comments.dump() + b"\x01"
        laces = []
        for packet in (comment, setup):
            laces += [255] * (len(packet) // 255) + [len(packet) % 255]
        body = comment + setup
        pages = [build_page(2, 0, self.serial, 0, self.lace(ident), ident)]
        seq, pos, cont = 1, 0, False
        while laces:
            chunk, laces = laces[:255], laces[255:]
            data = body[pos : pos + sum(chunk)]
            pos += sum(chunk)
            ends = any(lace < 255 for lace in chunk)
            pages.append(
                build_page(
                    1 if cont else 0, 0 if ends else -1, self.serial, seq, chunk, data
                )
            )
            cont = chunk[-1] == 255
            seq += 1
        shift = seq - self.pages
        size = os.path.getsize(self.path)

        def produce(out):
            for page in pages:
                out.write(page)
            with open(self.path, "rb") as src:
                if shift == 0:
                    copy_range(src, out, self.audio, size)
                    return
                src.seek(self.audio)
                while True:
                    page = ogg_page(src)
                    if page is None:
                        break
                    flags, granule, serial, number, table, data = page
                    # a chained file: the next stream counts its pages from 0 on its own
                    if serial == self.serial:
                        number += shift
                    out.write(build_page(flags, granule, serial, number, table, data))

        rewrite(self.path, produce)
        self.audio = sum(len(p) for p in pages)
        self.pages = seq

    @staticmethod
    def lace(packet: bytes) -> list[int]:
        return [255] * (len(packet) // 255) + [len(packet) % 255]

# ------------------------------------------------------------
# MP4 and M4A: atoms, the tags are the items of moov/udta/meta/ilst
def mp4_atoms(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise TagError("Damaged MP4 atom")
        yield kind, data[pos + header : pos + size]
        pos += size

def mp4_pack(kind: bytes, payload: bytes) -> bytes:
    if len(payload) + 8 >= 1 << 32:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload

def mp4_shift(moov: bytes, delta: int, floor: int) -> bytes:
    # the chunk offsets that point behind the old end of moov move with it
    out = bytearray(moov)

    def walk(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", bytes(out[pos : pos + 8]))
            header = 8
            if size == 1:
                size = struct.unpack(">Q", bytes(out[pos + 8 : pos + 16]))[0]
                header = 16
            if size < header or pos + size > end:
                raise TagError("Damaged MP4 atom")
            if kind in (b"trak", b"mdia", b"minf", b"stbl"):
                walk(pos + header, pos + size)
            elif kind in (b"stco", b"co64"):
                wide = kind == b"co64"
                count = struct.unpack(
                    ">I", bytes(out[pos + header + 4 : pos + header + 8])
                )[0]
                fmt, step = (">Q", 8) if wide else (">I", 4)
                at = pos + header + 8
                if at + count * step > pos + size:
                    raise TagError("Damaged MP4 atom")
                for _ in range(count):
                    value = struct.unpack(fmt, bytes(out[at : at + step]))[0]
                    if value >= floor:
                        out[at : at + step] = struct.pack(fmt, value + delta)
                    at += step
            pos += size

    walk(0, len(out))
    return bytes(out)

# a meta atom is a full box (4 bytes of version and flags first), but some files leave them out
def meta_header(meta: bytes) -> int:
    return 0 if meta[4:8] in (b"hdlr", b"ilst", b"keys", b"free") else 4

def mp4_item_value(payload: bytes):
    for kind, body in mp4_atoms(payload):
        if kind == b"data" and len(body) >= 8:
            return int.from_bytes(body[:4], "big") & 0xFFFFFF, body[8:]
    return None

def mp4_data_atom(kind: int, value: bytes) -> bytes:
    return mp4_pack(b"data", kind.to_bytes(4, "big") + bytes(4) + value)

class Mp4File(Tagged):
    def __init__(self, path: str):
        super().__init__(path)
        self.top = []
        self.size = os.path.getsize(path)
        with open(path, "rb") as f:
            pos = 0
            while pos + 8 <= self.size:
                f.seek(pos)
                head = f.read(16)
                n, kind = struct.unpack(">I4s", head[:8])
                header = 8
                if n == 1:
                    if len(head) < 16:
                        raise TagError("Damaged MP4 atom")
                    n = struct.unpack(">Q", head[8:16])[0]
                    header = 16
                elif n == 0:
                    n = self.size - pos
                if n < header or pos + n > self.size:
                    raise TagError("Damaged MP4 atom")
                self.top.append((kind, pos, n))
                pos += n
            kinds = [k for k, _p, _n in self.top]
            if not kinds or kinds[0] != b"ftyp" or b"moov" not in kinds:
                raise TagError("Not an MP4 audio file")
            if b"moof" in kinds:
                raise TagError("Fragmented MP4 files are not supported")
            _k, self.moov_at, self.moov_size = next(
                t for t in self.top if t[0] == b"moov"
            )
            f.seek(self.moov_at)
            raw = f.read(self.moov_size)
        header = 16 if struct.unpack(">I", raw[:4])[0] == 1 else 8
        self.moov = raw[header:]
        self.items = self.read_items()

    # -------------------------------------
    def read_items(self) -> list[tuple[bytes, bytes]]:
        for kind, udta in mp4_atoms(self.moov):
            if kind == b"udta":
                for k2, meta in mp4_atoms(udta):
                    if k2 == b"meta":
                        for k3, ilst in mp4_atoms(meta[meta_header(meta) :]):
                            if k3 == b"ilst":
                                return list(mp4_atoms(ilst))
        return []

    def tag_udta(self) -> int:
        # the udta that holds the ilst, else the first one: the others are copied as they are
        first = None
        for i, (kind, udta) in enumerate(mp4_atoms(self.moov)):
            if kind != b"udta":
                continue
            first = i if first is None else first
            for k2, meta in mp4_atoms(udta):
                if k2 == b"meta" and any(
                    k3 == b"ilst" for k3, _p in mp4_atoms(meta[meta_header(meta) :])
                ):
                    return i
        return -1 if first is None else first

    def freeform(self, payload: bytes) -> tuple[str, str]:
        mean = name = ""
        for kind, body in mp4_atoms(payload):
            if kind == b"mean":
                mean = body[4:].decode("utf-8", "replace")
            elif kind == b"name":
                name = body[4:].decode("utf-8", "replace")
        return mean, name

    def get(self, field: str) -> str:
        if field in MP4_NAMES or field in MP4_NUMBERS:
            code = MP4_NAMES.get(field) or MP4_NUMBERS[field]
            for kind, payload in self.items:
                if kind == code and (v := mp4_item_value(payload)) is not None:
                    _typ, raw = v
                    if field in MP4_NUMBERS:
                        return (
                            str(int.from_bytes(raw[2:4], "big"))
                            if len(raw) >= 4
                            else ""
                        )
                    return raw.decode("utf-8", "replace")
            return ""
        for kind, payload in self.items:
            if kind == b"----" and self.freeform(payload) == (
                MP4_MEAN,
                MP4_CUSTOM[field],
            ):
                v = mp4_item_value(payload)
                return v[1].decode("utf-8", "replace") if v else ""
        return ""

    def total(self, field: str) -> int:
        for kind, payload in self.items:
            if (
                kind == MP4_NUMBERS[field]
                and (v := mp4_item_value(payload)) is not None
                and len(v[1]) >= 6
            ):
                return int.from_bytes(v[1][4:6], "big")
        return 0

    def raw_set(self, field: str, value: str):
        if field in MP4_NUMBERS:
            code = MP4_NUMBERS[field]
            total = self.total(field)
            self.items = [(k, p) for k, p in self.items if k != code]
            if value:
                n = int(value.partition("/")[0])
                raw = (
                    bytes(2)
                    + n.to_bytes(2, "big")
                    + total.to_bytes(2, "big")
                    + (bytes(2) if field == "track_no" else b"")
                )
                self.items.append((code, mp4_data_atom(0, raw)))
            return
        if field in MP4_CUSTOM:
            aliases = ("initialkey", "key") if field == "key" else (field,)
            self.items = [
                (k, p)
                for k, p in self.items
                if not (
                    k == b"----"
                    and self.freeform(p)[0] == MP4_MEAN
                    and self.freeform(p)[1].lower() in aliases
                )
            ]
            if field == "bpm":
                self.items = [(k, p) for k, p in self.items if k != b"tmpo"]
                if value:
                    self.items.append(
                        (
                            b"tmpo",
                            mp4_data_atom(21, round(float(value)).to_bytes(2, "big")),
                        )
                    )
            if value:
                payload = (
                    mp4_pack(b"mean", bytes(4) + MP4_MEAN.encode())
                    + mp4_pack(b"name", bytes(4) + MP4_CUSTOM[field].encode())
                    + mp4_data_atom(1, value.encode("utf-8"))
                )
                self.items.append((b"----", payload))
            return
        code = MP4_NAMES[field]
        self.items = [
            (k, p)
            for k, p in self.items
            if k != code and not (field == "genre" and k == b"gnre")
        ]
        if value:
            self.items.append((code, mp4_data_atom(1, value.encode("utf-8"))))

    # -------------------------------------
    def new_moov(self) -> bytes:
        ilst = mp4_pack(b"ilst", b"".join(mp4_pack(k, p) for k, p in self.items))
        hdlr = mp4_pack(b"hdlr", bytes(8) + b"mdir" + b"appl" + bytes(8) + b"\0")
        kids = list(mp4_atoms(self.moov))
        target = self.tag_udta()
        out, done = [], False
        for index, (kind, payload) in enumerate(kids):
            if kind == b"udta" and index == target:
                parts, meta_done = [], False
                for k2, p2 in mp4_atoms(payload):
                    if k2 == b"meta" and not meta_done:
                        head = meta_header(p2)
                        inner = [
                            (k3, p3) for k3, p3 in mp4_atoms(p2[head:]) if k3 != b"ilst"
                        ]
                        inner.append((b"ilst", ilst[8:]))
                        parts.append(
                            mp4_pack(
                                b"meta",
                                p2[:head] + b"".join(mp4_pack(k, p) for k, p in inner),
                            )
                        )
                        meta_done = True
                    else:
                        parts.append(mp4_pack(k2, p2))
                if not meta_done:
                    parts.append(mp4_pack(b"meta", bytes(4) + hdlr + ilst))
                out.append(mp4_pack(b"udta", b"".join(parts)))
                done = True
            else:
                out.append(mp4_pack(kind, payload))
        if not done:
            out.append(mp4_pack(b"udta", mp4_pack(b"meta", bytes(4) + hdlr + ilst)))
        return b"".join(out)

    def save(self):
        payload = self.new_moov()
        old_end = self.moov_at + self.moov_size
        delta = len(mp4_pack(b"moov", payload)) - self.moov_size
        if delta:
            payload = mp4_shift(payload, delta, old_end)
        new = mp4_pack(b"moov", payload)

        def produce(out):
            with open(self.path, "rb") as src:
                copy_range(src, out, 0, self.moov_at)
                out.write(new)
                copy_range(src, out, old_end, self.size)

        rewrite(self.path, produce)

# ------------------------------------------------------------
EXTENSIONS = {
    ".mp3": Mp3File,
    ".wav": ChunkFile,
    ".aif": ChunkFile,
    ".aiff": ChunkFile,
    ".flac": FlacFile,
    ".m4a": Mp4File,
    ".mp4": Mp4File,
    ".alac": Mp4File,
    ".ogg": OggFile,
}

def open_tagged(path: str) -> Tagged:
    kind = EXTENSIONS.get(Path(path).suffix.lower())
    if kind is None:
        raise ValueError(
            "Unsupported or damaged audio container; tags were not written"
        )
    return kind(str(path))

# the comments of an Ogg Vorbis file, for the readers that do not write
def ogg_comments(path: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in OggFile(path).comments.items:
        if value is not None:
            clean = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
            out.setdefault(key.upper(), []).append(clean)
    return out

# ------------------------------------------------------------
# what the audio check needs from a header: the container, and the length it declares.
# None when the file is none of the containers known here
def identify(path: str) -> Info | None:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] == b"fLaC":
            f.seek(4)
            h = f.read(4)
            info = f.read(34)
            if len(info) < 34 or h[0] & 0x7F != 0:
                raise TagError("Damaged FLAC STREAMINFO")
            rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4)
            samples = ((info[13] & 15) << 32) | struct.unpack(">I", info[14:18])[0]
            return Info("FLAC", samples / rate if rate else 0.0)
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return Info("WAV", wave_length(f, size))
        if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
            return Info("AIFF", aiff_length(f, size))
        if head[:4] == b"OggS":
            return ogg_info(f, size)
        if head[4:8] == b"ftyp":
            return mp4_info(path)
        start = 0
        if head[:3] == b"ID3" and len(head) >= 10:
            start = (
                Id3Tag.total_size(head)
                if head[3] in (3, 4)
                else 10 + syncsafe(head[6:10])
            )
        first = find_mpeg(f, start)
        if first is not None:
            return Info("MP3", mpeg_length(f, first, size))
    return None

def wave_length(f, size: int) -> float:
    pos, byte_rate, data = 12, 0, None
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(8)
        if len(h) < 8:
            break
        n = struct.unpack("<I", h[4:8])[0]
        if h[:4] == b"fmt " and n >= 16:
            fmt = f.read(16)
            if len(fmt) == 16:
                byte_rate = struct.unpack("<I", fmt[8:12])[0]
        elif h[:4] == b"data":
            # a size past the end of the file (streamed, or cut) counts what is there
            data = min(n, size - pos - 8)
        pos += 8 + n + (n & 1)
    return data / byte_rate if data is not None and byte_rate else 0.0

def aiff_length(f, size: int) -> float:
    pos = 12
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(8)
        if len(h) < 8:
            break
        n = struct.unpack(">I", h[4:8])[0]
        if h[:4] == b"COMM" and n >= 18:
            comm = f.read(18)
            if len(comm) < 18:
                return 0.0
            frames = struct.unpack(">I", comm[2:6])[0]
            exponent = struct.unpack(">H", comm[8:10])[0] & 0x7FFF
            mantissa = struct.unpack(">Q", comm[10:18])[0]
            # a sample rate is between a fraction of a Hz and many GHz
            if not 16383 - 20 <= exponent <= 16383 + 40:
                return 0.0
            rate = mantissa * 2.0 ** (exponent - 16383 - 63)
            return frames / rate if rate else 0.0
        pos += 8 + n + (n & 1)
    return 0.0

def ogg_info(f, size: int) -> Info | None:
    f.seek(0)
    page = ogg_page(f)
    if page is None:
        return None
    first = page[5]
    if first[:7] == b"\x01vorbis" and len(first) >= 16:
        rate = struct.unpack("<I", first[12:16])[0]
        name, skip = "Ogg Vorbis", 0
    elif first[:8] == b"OpusHead" and len(first) >= 12:
        rate, name, skip = 48000, "Ogg Opus", struct.unpack("<H", first[10:12])[0]
    else:
        return None
    tail = min(size, 1 << 17)
    f.seek(size - tail)
    block = f.read(tail)
    i = block.rfind(b"OggS")
    if i < 0 or len(block) - i < 14:
        return Info(name, 0.0)
    granule = struct.unpack("<q", block[i + 6 : i + 14])[0]
    return Info(name, max(0, granule - skip) / rate if rate and granule > 0 else 0.0)

def mp4_info(path: str) -> Info | None:
    tagged = Mp4File(path)
    for kind, payload in mp4_atoms(tagged.moov):
        if kind == b"mvhd" and len(payload) >= 20:
            if payload[0] == 1 and len(payload) >= 32:
                scale, duration = struct.unpack(">IQ", payload[20:32])
            elif payload[0] == 0:
                scale, duration = struct.unpack(">II", payload[12:20])
            else:
                return Info("MP4", 0.0)
            return Info("MP4", duration / scale if scale else 0.0)
    return Info("MP4", 0.0)
