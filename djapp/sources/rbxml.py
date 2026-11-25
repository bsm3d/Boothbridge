# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : rekordbox XML (DJ_PLAYLISTS) reader, with its tracks, cues, playlists and colour mapping.

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

import defusedxml.ElementTree as ET

from .. import keys
from ..model import CUE, FADE_IN, FADE_OUT, LOAD, LOOP, Cue, Node, Track
from . import (
    MAX_CUES,
    MAX_NODES,
    MAX_REFS,
    MAX_TRACKS,
    Progress,
    Stream,
    check_size,
    index_folder,
    num,
    position,
    relocate,
)

# POSITION_MARK Type
_KINDS = {0: CUE, 1: FADE_IN, 2: FADE_OUT, 3: LOAD, 4: LOOP}

# the 8 rekordbox track colours as written in Colour="0x......"
RB_COLORS = [
    "FF007F",
    "FF0000",
    "FFA500",
    "FFFF00",
    "00FF00",
    "25FDE9",
    "0000FF",
    "660099",
]

# ------------------------------------------------------------
# without its host file://nas/music/a.mp3 is a folder of this disk instead of the share \\nas\music,
# and file://C:/a.mp3 (one slash too few) loses its drive
def host_path(host: str, p: str) -> str:
    if host.lower() in ("", "localhost"):
        return p
    return "/" + host + p if len(host) == 2 and host[1] == ":" else "//" + host + p

def url_to_path(loc: str) -> str:
    if not loc.startswith("file:"):
        return loc
    try:
        u = urlparse(loc)
        p = unquote(u.path)
    except ValueError:  # file://[::1/x is not an address, and one bad track does not make a bad collection
        return loc
    p = host_path(unquote(u.netloc), p)
    if len(p) > 2 and p[2] == ":":
        p = os.path.normpath(p[1:])
    return p

def rb_date(s: str) -> str:
    return s[:10] if len(s) >= 10 and s[4] == "-" else ""

def read_track(el: ET.Element) -> Track:
    g = el.get
    t = Track(
        title=g("Name", ""),
        artist=g("Artist", ""),
        album=g("Album", ""),
        genre=g("Genre", ""),
        label=g("Label", ""),
        comment=g("Comments", ""),
        remixer=g("Remixer", ""),
        composer=g("Composer", ""),
    )
    t.path = url_to_path(g("Location", ""))
    t.size = int(num(g("Size")))
    t.bitrate = int(num(g("BitRate")))
    t.samplerate = int(num(g("SampleRate"), 44100))
    t.duration = num(g("TotalTime"))
    t.bpm = num(g("AverageBpm"))
    t.key = keys.parse(g("Tonality", ""))
    t.rating = int(num(g("Rating"))) // 51
    t.plays = int(num(g("PlayCount")))
    t.year = int(num(g("Year")))
    t.track_no = int(num(g("TrackNumber")))
    t.disc_no = int(num(g("DiscNumber")))
    t.added = rb_date(g("DateAdded", ""))
    col = g("Colour", "").upper().replace("0X", "")
    if col in RB_COLORS:
        t.color = RB_COLORS.index(col) + 1

    tempo = el.find("TEMPO")
    if tempo is not None:
        first = position(tempo.get("Inizio"))
        if first is not None:
            t.grid = first * 1000

    for pm in el.findall("POSITION_MARK"):
        start = position(pm.get("Start"))
        if start is None:
            continue
        start *= 1000
        stop = num(pm.get("End"), None) if pm.get("End") else None
        c = Cue(
            name=pm.get("Name", ""),
            kind=_KINDS.get(int(num(pm.get("Type"))), CUE),
            start=start,
            length=stop * 1000 - start if stop is not None else 0.0,
            hotcue=int(num(pm.get("Num"), -1)),
        )
        r, gr, b = pm.get("Red"), pm.get("Green"), pm.get("Blue")
        if r is not None and gr is not None and b is not None:
            c.color = f"#{int(num(r)):02X}{int(num(gr)):02X}{int(num(b)):02X}"
        t.cues.append(c)
    return t

# ------------------------------------------------------------------
def read_nodes(
    parent: ET.Element, by_id: dict[str, Track], by_loc: dict[str, Track], keyed: dict
) -> list[Node]:
    out = []
    for n in parent.findall("NODE"):
        name = n.get("Name", "")
        if n.get("Type") == "0":
            kids = read_nodes(n, by_id, by_loc, keyed)
            if kids:
                out.append(Node("folder", name, children=kids))
            continue
        # KeyType 1 means the playlist references tracks by Location instead of TrackID
        ref = by_loc if n.get("KeyType") == "1" else by_id
        node = Node("playlist", name)
        for tr in keyed.get(n, ()):
            t = tr if isinstance(tr, Track) else ref.get(tr)
            if t is not None:
                node.tracks.append(t)
        if node.tracks:
            out.append(node)
    return out

# --------------------------------------------------------------
# One pass over the file. A collection track becomes a Track as soon as it is read; a playlist track becomes a reference to it.
# Only folders and playlists are kept (keyed: what each playlist lists). A playlist read before the collection keeps keys, resolved at the end.
def load(
    path: str, music_root: str = "", progress: Progress | None = None
) -> list[Node]:
    cb = progress or (lambda pct, msg: None)
    check_size(path)
    found = index_folder(music_root) if music_root else {}

    by_id: dict[str, Track] = {}
    by_loc: dict[str, Track] = {}
    keyed: dict = {}
    col = root = None
    ready = False

    def tick(fraction: float):
        cb(10 + int(80 * fraction), f"Reading {len(by_id)} tracks")

    with Stream(path, None, tick) as s:
        for event, el in s:
            tags = s.tags
            if event == "start":
                if len(tags) == 1:
                    root = el
                elif len(tags) == 2 and tags[1] == "COLLECTION" and col is None:
                    col = el
                elif tags[1] == "PLAYLISTS" and tags[-1] == "NODE":
                    s.count("playlists and folders", 1, MAX_NODES)
                continue
            if len(tags) == 3 and tags[1] == "COLLECTION" and tags[2] == "TRACK":
                if s.stack[1] is col:
                    t = read_track(el)
                    s.count("tracks", 1, MAX_TRACKS)
                    s.count("cue points", len(t.cues), MAX_CUES)
                    t.path = relocate(t.path, found)
                    by_id[el.get("TrackID", "")] = t
                    by_loc[el.get("Location", "")] = t
                s.release()
            elif len(tags) == 2 and el is col:
                ready = True
            elif (
                len(tags) > 3
                and tags[1] == "PLAYLISTS"
                and tags[-1] == "TRACK"
                and tags[-2] == "NODE"
            ):
                node = s.stack[-2]
                if node.get("Type") != "0":
                    s.count("playlist entries", 1, MAX_REFS)
                    key = el.get("Key", "")
                    ref = by_loc if node.get("KeyType") == "1" else by_id
                    t = ref.get(key) if ready else key
                    if t is not None:
                        keyed.setdefault(node, []).append(t)
                s.release()

    top = root.find("PLAYLISTS/NODE")
    nodes = read_nodes(top, by_id, by_loc, keyed) if top is not None else []
    cb(100, f"{len(by_id)} tracks")
    return nodes
