# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reader for the Traktor collection.nml file, with defused XML parsing and cleaning of broken text.

from __future__ import annotations

import codecs
import logging
import os
import re

import defusedxml.ElementTree as ET

from .. import keys
from ..model import CUE, GRID, LOOP, Cue, Node, Track
from . import (
    MAX_CUES,
    MAX_NODES,
    MAX_REFS,
    MAX_TRACKS,
    BadEncoding,
    Progress,
    Stream,
    check_size,
    index_folder,
    num,
    position,
    relocate,
)

log = logging.getLogger(__name__)

# Traktor COLOR 1-7 -> rekordbox color id (1 pink ... 8 purple)
COLOR_TO_RB = {1: 2, 2: 3, 3: 4, 4: 5, 5: 7, 6: 8, 7: 1}

_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")

# ------------------------------------------------------------
# A numeric reference to a character XML does not allow (&#0;, half a surrogate pair) is dropped
def _legal_reference(m: re.Match) -> str:
    digits = m.group(1)
    try:
        code = int(digits[1:], 16) if digits[0] == "x" else int(digits)
    except ValueError:
        return ""
    legal = (
        code in (9, 10, 13)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )
    return m.group(0) if legal else ""

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# ------------------------------------------------------------
# old collections sometimes carry control chars in tags, Traktor itself doesn't care
def clean_text(txt: str) -> str:
    return _CHAR_REF.sub(_legal_reference, _CONTROL.sub("", txt))

# ------------------------------------------------------------
# The file as text with clean_text applied, a chunk at a time. A character reference cut in two by the
# end of a chunk is held back for the next one. The text is read as UTF-8 whatever the file declares
class Cleaned:
    def __init__(self, f):
        self.f = f
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.tail = ""
        self.done = False

    def read(self, size: int = -1) -> str:
        while not self.done:
            raw = self.f.read(1 << 16)
            self.done = not raw
            text = self.tail + self.decoder.decode(raw, final=self.done)
            self.tail = ""
            cut = text.rfind("&")
            if (
                not self.done
                and cut >= 0
                and ";" not in text[cut:]
                and len(text) - cut < 40
            ):
                text, self.tail = text[:cut], text[cut:]
            text = clean_text(text)
            if text:
                return text
        return ""

# ------------------------------------------------------------
def read_tree(path: str) -> ET.Element:
    check_size(path)
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return ET.fromstring(raw)
    except (ET.ParseError, LookupError):
        # An encoding label that is not one makes the parser fail too, the text is read as UTF-8
        return ET.fromstring(clean_text(raw.decode("utf-8", errors="replace")))

# '2026/9/28' -> '2026-09-28'
def nml_date(s: str) -> str:
    parts = (s or "").split("/")
    if len(parts) != 3:
        return ""
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"

# the PRIMARYKEY form Traktor uses to reference a track from a playlist
def location_key(loc: ET.Element) -> str:
    return loc.get("VOLUME", "") + loc.get("DIR", "") + loc.get("FILE", "")

# ------------------------------------------------------------
def location_path(loc: ET.Element) -> str:
    vol = loc.get("VOLUME", "")
    rel = loc.get("DIR", "").replace("/:", "/") + loc.get("FILE", "")
    if len(vol) == 2 and vol[1] == ":":
        return os.path.normpath(vol + rel)
    # macOS: VOLUME is the disk name, the boot disk is mounted on /
    ext = f"/Volumes/{vol}{rel}"
    return ext if os.path.exists(ext) else rel

# ------------------------------------------------------------
def read_entry(e: ET.Element) -> Track:
    t = Track(title=e.get("TITLE", ""), artist=e.get("ARTIST", ""))
    t.audio_id = e.get("AUDIO_ID", "")
    t.locked = e.get("LOCK") == "1"
    t.modified = nml_date(e.get("MODIFIED_DATE", ""))

    loc = e.find("LOCATION")
    if loc is not None:
        t.path = location_path(loc)

    alb = e.find("ALBUM")
    if alb is not None:
        t.album = alb.get("TITLE", "")
        t.track_no = int(num(alb.get("TRACK")))

    info = e.find("INFO")
    if info is not None:
        t.genre = info.get("GENRE", "")
        t.label = info.get("LABEL", "")
        t.comment = info.get("COMMENT", "")
        t.remixer = info.get("REMIXER", "") or e.get("REMIXER", "")
        t.composer = info.get("PRODUCER", "")
        t.bitrate = int(num(info.get("BITRATE"))) // 1000
        t.size = int(num(info.get("FILESIZE"))) * 1024  # Traktor stores KB
        t.duration = num(info.get("PLAYTIME_FLOAT")) or num(info.get("PLAYTIME"))
        t.rating = int(num(info.get("RANKING"))) // 51
        t.plays = int(num(info.get("PLAYCOUNT")))
        t.color = COLOR_TO_RB.get(int(num(info.get("COLOR"))), 0)
        t.added = nml_date(info.get("IMPORT_DATE", ""))
        t.last_played = nml_date(info.get("LAST_PLAYED", ""))
        t.year = int(num(info.get("RELEASE_DATE", "").split("/")[0]))
        if t.key is None and info.get("KEY"):
            t.key = keys.parse(info.get("KEY"))

    tempo = e.find("TEMPO")
    if tempo is not None:
        t.bpm = num(tempo.get("BPM"))

    mk = e.find("MUSICAL_KEY")
    if mk is not None:
        t.key = keys.from_traktor(mk.get("VALUE"))

    loud = e.find("LOUDNESS")
    if loud is not None:
        t.gain = num(loud.get("ANALYZED_DB"))

    for c in e.findall("CUE_V2"):
        start = position(c.get("START"))
        if start is None:
            continue
        cue = Cue(
            name=c.get("NAME", ""),
            kind=int(num(c.get("TYPE"))),
            start=start,
            length=num(c.get("LEN")),
            hotcue=int(num(c.get("HOTCUE"), -1)),
            color=c.get("COLOR", ""),
        )
        if cue.kind == GRID:
            if t.grid is None:
                t.grid = cue.start
            continue
        # AutoGrid and friends are not real cues, a LOOP without length is a plain cue
        if cue.kind == LOOP and cue.length <= 0:
            cue.kind = CUE
        t.cues.append(cue)
    return t

# ------------------------------------------------------------
def read_nodes(parent: ET.Element, by_key: dict[str, Track], keyed: dict) -> list[Node]:
    out = []
    sub = parent.find("SUBNODES")
    for n in (sub if sub is not None else parent).findall("NODE"):
        kind, name = n.get("TYPE", ""), n.get("NAME", "")
        if kind == "FOLDER":
            kids = read_nodes(n, by_key, keyed)
            if kids:
                out.append(Node("folder", name, children=kids))
        elif kind == "PLAYLIST":
            pl = n.find("PLAYLIST")
            if pl is None:
                continue
            node = Node("playlist", name, uuid=pl.get("UUID", ""))
            for ref in keyed.get(pl, ()):
                t = ref if isinstance(ref, Track) else by_key.get(ref)
                if t is not None:
                    node.tracks.append(t)
            if node.tracks:
                out.append(node)
        elif kind == "SMARTLIST":
            sl = n.find("SMARTLIST")
            if sl is None:
                continue
            q = sl.find("SEARCH_EXPRESSION")
            out.append(
                Node(
                    "smartlist",
                    name,
                    uuid=sl.get("UUID", ""),
                    query=q.get("QUERY", "") if q is not None else "",
                )
            )
    return out

# ------------------------------------------------------------
# One pass over the file. A collection entry becomes a Track as soon as it is read; a playlist entry becomes a reference to its Track.
# Only folders and playlists are kept (keyed: what each playlist lists). Traktor writes the collection first; a playlist read before it keeps keys, resolved at the end.
def read_stream(path: str, found: dict, cb, music_root: str, clean) -> list[Node]:
    by_key: dict[str, Track] = {}
    keyed: dict = {}
    moved = 0
    col = root = None
    ready = False

    # ------------------------------------------------------------
    def tick(fraction: float):
        cb(10 + int(80 * fraction), f"Reading {len(by_key)} tracks")

    with Stream(path, clean, tick) as s:
        for event, el in s:
            tags = s.tags
            if event == "start":
                if len(tags) == 1:
                    root = el
                    log.info("NML version %s", el.get("VERSION", "?"))
                elif len(tags) == 2 and tags[1] == "COLLECTION" and col is None:
                    col = el
                elif tags[1] == "PLAYLISTS" and tags[-1] == "NODE":
                    s.count("playlists and folders", 1, MAX_NODES)
                continue
            if len(tags) == 3 and tags[1] == "COLLECTION" and tags[2] == "ENTRY":
                if s.stack[1] is col:
                    loc = el.find("LOCATION")
                    if loc is not None:
                        t = read_entry(el)
                        s.count("tracks", 1, MAX_TRACKS)
                        s.count("cue points", len(t.cues), MAX_CUES)
                        # relocate only what is really gone, a same-named file elsewhere could be another mix
                        p = relocate(t.path, found)
                        if p != t.path:
                            t.path = p
                            moved += 1
                        by_key[location_key(loc)] = t
                s.release()
            elif len(tags) == 2 and el is col:
                ready = True
            elif (
                len(tags) > 2
                and tags[1] == "PLAYLISTS"
                and tags[-1] == "ENTRY"
                and tags[-2] == "PLAYLIST"
            ):
                pk = el.find("PRIMARYKEY")
                if pk is not None:
                    s.count("playlist entries", 1, MAX_REFS)
                    key = pk.get("KEY", "")
                    t = by_key.get(key) if ready else key
                    if t is not None:
                        keyed.setdefault(s.stack[-2], []).append(t)
                s.release()

    if moved:
        log.info("%d tracks relocated from %s", moved, music_root)

    top = root.find("PLAYLISTS")
    if top is None:
        return []
    rootnode = top.find("NODE")
    nodes = read_nodes(rootnode if rootnode is not None else top, by_key, keyed)
    cb(100, f"{len(by_key)} tracks")
    return nodes

# ------------------------------------------------------------
def load(
    path: str, music_root: str = "", progress: Progress | None = None
) -> list[Node]:
    cb = progress or (lambda pct, msg: None)
    check_size(path)
    found = {}
    if music_root:
        cb(5, "Scanning music folder...")
        found = index_folder(music_root)
    try:
        return read_stream(path, found, cb, music_root, None)
    except (ET.ParseError, BadEncoding):
        return read_stream(path, found, cb, music_root, Cleaned)
