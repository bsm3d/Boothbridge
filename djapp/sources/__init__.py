# Benoit Saint-Moulin
# Boothbridge : source detection and loading

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .. import protected
from ..model import Node

Progress = Callable[[int, str], None]

# 100 000 tracks is about 100 MB of XML, a real NML of 20 000 tracks 30 to 60 MB. The readers
# go through the file in one pass and keep only the model, the limits below bound what a
# hostile file can make them keep
MAX_FILE = 256 << 20
MAX_DEPTH = 600
MAX_LIVE = 1_000_000
MAX_TRACKS = 400_000
MAX_CUES = 2_000_000
MAX_NODES = 100_000
MAX_REFS = 10_000_000
TICK = 20_000


# ------------------------------------------------------------
# refuses a file too big to be what its name says, before it is read into memory
def check_size(path: str, limit: int = 0):
    size = os.path.getsize(path)
    if size > (limit or MAX_FILE):
        raise ValueError(
            f"{os.path.basename(path)} is too large to be a collection "
            f"({size / 1e6:.0f} MB, the limit is {(limit or MAX_FILE) / 1e6:.0f} MB)"
        )


# An encoding declaration Python does not know is a bad file
class BadEncoding(ValueError):
    pass


# ------------------------------------------------------------
# One pass over an XML file with defusedxml (entities, external data refused as in a plain parse).
# stack holds the open elements and tags their names. release() gives back an element a reader
# is done with so it is not kept. depth and live are bounded, so is every count() a reader makes
class Stream:
    def __init__(
        self, path: str, clean=None, tick=None, depth: int = MAX_DEPTH, deep: str = ""
    ):
        self.path = path
        self.name = os.path.basename(path)
        self.size = max(1, os.path.getsize(path))
        self.clean, self.tick = clean, tick
        self.depth, self.deep = depth, deep
        self.stack: list = []
        self.tags: list[str] = []
        self.marks: list[int] = []
        self.live = 0
        self.counts: dict[str, int] = {}
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "rb")
        return self

    def __exit__(self, *exc):
        self.f.close()

    @property
    def fraction(self) -> float:
        return min(1.0, self.f.tell() / self.size)

    def count(self, what: str, n: int, limit: int):
        total = self.counts.get(what, 0) + n
        self.counts[what] = total
        if total > limit:
            raise ValueError(
                f"{self.name} holds too many {what} to be a collection (more than {limit:,})".replace(
                    ",", " "
                )
            )

    def release(self):
        el = self.stack[-1]
        self.live = self.marks[-1]
        if len(self.stack) > 1:
            self.stack[-2].remove(el)
        el.clear()

    def __iter__(self):
        import defusedxml.ElementTree as ET

        source = self.clean(self.f) if self.clean else self.f
        stack, tags, marks = self.stack, self.tags, self.marks
        seen = 0
        try:
            for event, el in ET.iterparse(source, ("start", "end")):
                seen += 1
                if event == "start":
                    stack.append(el)
                    tags.append(el.tag)
                    marks.append(self.live)
                    self.live += 1
                    if len(stack) > self.depth:
                        raise ValueError(
                            self.deep
                            or f"{self.name} nests its elements too deeply to be a collection"
                        )
                    if self.live > MAX_LIVE:
                        raise ValueError(
                            f"{self.name} holds too many elements to be a collection"
                        )
                    yield event, el
                else:
                    yield event, el
                    stack.pop()
                    tags.pop()
                    marks.pop()
                if self.tick and seen % TICK == 0:
                    self.tick(self.fraction)
        except LookupError as e:
            raise BadEncoding(
                f"{self.name} declares an encoding that is not supported: {e}"
            ) from e


# what can be added to a playlist: no video container (.mp4), no .m4a, one list on purpose
AUDIO_EXT = {
    ".mp3",
    ".wav",
    ".flac",
    ".aiff",
    ".aif",
    ".aac",
    ".ogg",
}
# a track that comes from a collection can still point at one of these, the export copies and checks it
COLLECTION_AUDIO_EXT = AUDIO_EXT | {".m4a", ".mp4", ".alac"}
# the copy that tag writing works on, left behind when the program is killed
TEMP_PREFIX = ".bb-tags-"


def is_audio(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in AUDIO_EXT and not name.startswith(
        TEMP_PREFIX
    )


KINDS = (
    "traktor",
    "rekordbox",
    "mixxx",
    "m3u",
    "virtualdj",
    "itunes",
    "engine",
    "folder",
    "project",
    "key",
)


# attention -> bug = "inf", "nan" and "1e999" parse as floats and break int() and every sum after them
LIMIT = 1e15


# ------------------------------------------------------------
def sane(x: float) -> bool:
    return abs(x) <= LIMIT


def num(s, default: float = 0.0) -> float:
    try:
        v = float(s)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if sane(v) else default


# ------------------------------------------------------------
# A cue or grid position as written in a file. 0 when the attribute is missing, None when
# it is there but is not a usable number
def position(s) -> float | None:
    return 0.0 if s is None else num(s, None)


def unix_date(s) -> str:
    try:
        return datetime.fromtimestamp(int(s), UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# a symlink or a Windows junction. islink() is False for a junction and os.walk goes into it, a junction back up
# to a parent is a tree without an end, and two of them are one with too many paths to ever finish
def is_link(path: str) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(isjunction and isjunction(path))


# os.walk that does not enter symlinks or junctions
def walk(root: str):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not is_link(os.path.join(d, x))]
        yield d, dirs, files


# every audio file under root, by lowercase filename
def index_folder(root: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d, _, files in walk(root):
        for f in files:
            if is_audio(f):
                out.setdefault(f.lower(), []).append(os.path.join(d, f))
    return out


# a database on a share is file://nas/share/m.db, an authority SQLite refuses: with four slashes it stays a path
def sqlite_uri(path) -> str:
    uri = Path(path).resolve().as_uri()
    return uri if uri.startswith("file:///") else "file:////" + uri[len("file://") :]


def detect(path: str) -> str:
    from . import key

    if key.detect(path):
        return "key"
    from . import engine

    if engine.detect(path):
        return "engine"
    if os.path.isdir(path):
        return "folder"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".vdjfolder":
        return "virtualdj"
    if ext == ".nml":
        return "traktor"
    if ext in (".m3u", ".m3u8"):
        return "m3u"
    if ext in (".sqlite", ".db"):
        try:
            con = sqlite3.connect(sqlite_uri(path) + "?mode=ro", uri=True)
            try:
                ok = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='track_locations'"
                ).fetchone()
            finally:
                con.close()
            return "mixxx" if ok else ""
        except sqlite3.Error:
            return ""
    if ext == ".json":
        from . import project

        return "project" if project.sniff(path) else ""
    if ext == ".xml":
        with open(path, "rb") as f:
            head = f.read(4096)
        if b"<DJ_PLAYLISTS" in head:
            return "rekordbox"
        if b"<VirtualDJ_Database" in head:
            return "virtualdj"
        if b"<plist" in head and (
            b"Application Version" in head or b"Library Persistent ID" in head
        ):
            return "itunes"
        if b"<NML" in head:
            return "traktor"
    return ""


# ------------------------------------------------------------------
# notes is filled by the readers that have something to say besides the playlists
def load(
    path: str,
    music_root: str = "",
    progress: Progress | None = None,
    notes: dict | None = None,
) -> list[Node]:
    from xml.etree.ElementTree import ParseError

    from defusedxml import DefusedXmlException

    name = os.path.basename(path)
    protected.refuse(path)
    try:
        return read_collection(path, music_root, progress, notes)
    except ParseError as e:
        raise ValueError(f"{name} is not a valid XML file ({e})") from e
    except RecursionError:
        raise ValueError(
            f"{name} nests its folders too deeply to be a collection"
        ) from None
    except DefusedXmlException:
        raise ValueError(
            f"{name} defines XML entities or loads external data, which is refused for safety"
        ) from None
    except sqlite3.Error as e:
        raise ValueError(f"{name} cannot be read as a library database ({e})") from e


def read_collection(
    path: str, music_root: str, progress: Progress | None, notes: dict | None
) -> list[Node]:
    kind = detect(path)
    if kind == "traktor":
        from . import nml

        return nml.load(path, music_root, progress)
    if kind == "rekordbox":
        from . import rbxml

        return rbxml.load(path, music_root, progress)
    if kind == "mixxx":
        from . import mixxx

        return mixxx.load(path, music_root, progress)
    if kind == "m3u":
        from . import m3u

        return m3u.load(path, music_root, progress)
    if kind == "virtualdj":
        from . import vdj

        return vdj.load(path, music_root, progress)
    if kind == "itunes":
        from . import itunes

        return itunes.load(path, music_root, progress)
    if kind == "engine":
        from . import engine

        return engine.load(path, music_root, progress)
    if kind == "project":
        from . import project

        return project.load(path, music_root, progress)
    if kind == "key":
        from . import key

        return key.load(path, music_root, progress, notes)
    if kind == "folder":
        from . import folder

        return folder.load(path, music_root, progress)
    raise ValueError(f"Unknown collection format: {os.path.basename(path)}")


# ------------------------------------------------------------
def relocate(path: str, found: dict[str, list[str]]) -> str:
    if not found or os.path.exists(path):
        return path
    alt = found.get(os.path.basename(path).lower())
    return os.path.normpath(alt[0]) if alt else path


# one lazy folder index and one existence lookup per distinct playlist path
def relocator(music_root: str):
    found = None
    resolved: dict[str, str] = {}

    def resolve(path: str) -> str:
        nonlocal found
        if not music_root or not path:
            return path
        if path not in resolved:
            if os.path.exists(path):
                resolved[path] = path
            else:
                if found is None:
                    found = index_folder(music_root)
                hits = found.get(os.path.basename(path).lower())
                resolved[path] = os.path.normpath(hits[0]) if hits else path
        return resolved[path]

    return resolve
