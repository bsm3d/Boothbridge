# Benoit Saint-Moulin
# Boothbridge : iTunes and Music reader (the Library.xml that the application writes on request)

# File > Library > Export Library writes a property list in XML: Tracks, a dictionary of tracks by id, and
# Playlists, an array of dictionaries whose Playlist Items are track ids. There is no cue point, no grid and no key
# in it, only the tags and the playlists. The binary libraries (.itl, .musiclibrary) are never opened

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

from ..model import Node, Track
from . import (
    MAX_NODES,
    MAX_REFS,
    MAX_TRACKS,
    Progress,
    Stream,
    check_size,
    num,
    relocator,
    sane,
)
from .rbxml import host_path

MAX_DEPTH = 12
TREE_DEPTH = 32  # folders in folders, as deep as the other readers keep them
SKIPPED_KINDS = ("podcast", "movie", "tv show", "audiobook", "ringtone")


# ------------------------------------------------------------
# one plist element as a Python value. Data is not decoded and a nesting that is too deep is a bad file
def value(el, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ValueError("The iTunes library nests its values too deeply")
    tag = el.tag
    if tag == "dict":
        out = {}
        items = list(el)
        for k, v in zip(items[0::2], items[1::2], strict=False):
            if k.tag == "key" and k.text:
                out[k.text] = value(v, depth + 1)
        return out
    if tag == "array":
        return [value(v, depth + 1) for v in el]
    if tag == "integer":
        text = (el.text or "").strip()
        try:
            return int(text) if len(text) <= 18 else 0
        except ValueError:
            return 0
    if tag == "real":
        return num(el.text)
    if tag == "true":
        return True
    if tag == "false":
        return False
    if tag in ("string", "date"):
        return el.text or ""
    return None


# file://localhost/C:/Music/a.mp3 or file:///Users/me/a.mp3 as a path of this system
def location_path(location: str) -> str:
    try:
        parts = urlparse(location)
    except ValueError:
        return ""
    if parts.scheme != "file":
        return ""
    path = host_path(unquote(parts.netloc), unquote(parts.path))
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return os.path.normpath(path) if path else ""


# ------------------------------------------------------------
def whole(x) -> int:
    v = num(x)
    return int(v) if sane(v) else 0


def read_track(d: dict) -> Track | None:
    if (
        d.get("Protected")
        or d.get("Has Video")
        or str(d.get("Kind", "")).lower().startswith("protected")
    ):
        return None
    if any(word in str(d.get("Kind", "")).lower() for word in SKIPPED_KINDS) or d.get(
        "Podcast"
    ):
        return None
    path = location_path(str(d.get("Location", "")))
    if not path:
        return None  # a track of the cloud has no file
    t = Track(path=path)
    t.title, t.artist, t.album = (
        str(d.get("Name", "")),
        str(d.get("Artist", "")),
        str(d.get("Album", "")),
    )
    t.album_artist, t.genre = str(d.get("Album Artist", "")), str(d.get("Genre", ""))
    t.composer, t.comment = str(d.get("Composer", "")), str(d.get("Comments", ""))
    t.year, t.track_no, t.disc_no = (
        whole(d.get("Year")),
        whole(d.get("Track Number")),
        whole(d.get("Disc Number")),
    )
    t.duration = num(d.get("Total Time")) / 1000.0
    t.bitrate, t.samplerate, t.size = (
        whole(d.get("Bit Rate")),
        whole(d.get("Sample Rate")) or 44100,
        whole(d.get("Size")),
    )
    t.bpm = num(d.get("BPM"))
    t.plays = whole(d.get("Play Count"))
    if not d.get("Rating Computed"):
        t.rating = min(5, max(0, round(num(d.get("Rating")) / 20)))
    t.added = str(d.get("Date Added", ""))[:10]
    t.modified = str(d.get("Date Modified", ""))[:10]
    t.last_played = str(d.get("Play Date UTC", ""))[:10]
    return t


# ------------------------------------------------------------
# The library as a dictionary in one pass over the file. Tracks is read one track at a time and
# Playlist Items one item at a time, so the XML of a big library is never held, only the Tracks
# and, for a playlist, the Tracks it lists. A playlist that comes before Tracks keeps ids instead,
# they are resolved at the end. Every dictionary pairs its keys and values by position, a key that
# is not one drops its value
def read_library(path: str, name: str, cb, found) -> tuple[dict, dict[str, Track]]:
    lib: dict = {}
    raw: dict = {}
    tracks: dict[str, Track] | None = None
    lists: list = []
    top = playlist = items = None
    roles: list[str] = []
    kids: list[int] = []
    last: dict[str, str | None] = {}

    def tick(fraction: float):
        cb(10 + int(50 * fraction), f"Reading {name}...")

    # ------------------------------------------------------------
    # the element being opened is the next key of its dictionary or the value of the last one
    def pair(level: str) -> str:
        index = kids[-2]
        kids[-2] += 1
        if index % 2 == 0:
            return "key"
        return "value" if last.get(level) else "skip"

    with Stream(
        path,
        None,
        tick,
        MAX_DEPTH + 2,
        "The iTunes library nests its values too deeply",
    ) as s:
        for event, el in s:
            if event == "start":
                kids.append(0)
                above = roles[-1] if roles else ""
                if not above:
                    role = "plist"
                elif above == "plist":
                    role = "top" if el.tag == "dict" and top is None else "skip"
                    if role == "top":
                        top = el
                elif above == "top":
                    role = pair("top")
                    if role == "value":
                        if last["top"] == "Tracks" and el.tag == "dict":
                            if tracks is not None:
                                raise ValueError(f"{name} is not an iTunes library")
                            role, raw = "tracks", {}
                        elif last["top"] == "Playlists" and el.tag == "array":
                            role, lists = "playlists", []
                        else:
                            role = "plain"
                    elif role == "key":
                        role = "topkey"
                elif above == "tracks":
                    role = pair("tracks")
                    role = {"key": "trackkey", "value": "track"}.get(role, role)
                elif above == "playlists":
                    role = "skip"
                    if el.tag == "dict":
                        role, playlist = "playlist", {}
                elif above == "playlist":
                    role = pair("playlist")
                    if role == "value":
                        role = "member"
                        if last["playlist"] == "Playlist Items" and el.tag == "array":
                            role, items = "items", []
                    elif role == "key":
                        role = "memberkey"
                elif above == "items":
                    role = "item"
                else:
                    role = "inner"
                roles.append(role)
                continue
            role = roles.pop()
            kids.pop()
            if role == "topkey":
                last["top"] = el.text if el.tag == "key" and el.text else None
            elif role == "plain":
                lib[last["top"]] = value(el, 1)
            elif role == "tracks":
                lib["Tracks"] = raw
                tracks = {}
                for ident, t in raw.values():
                    if t is not None:
                        t.path = found(t.path)
                        tracks[ident] = t
                cb(60, f"{len(tracks)} tracks")
            elif role == "trackkey":
                last["tracks"] = el.text if el.tag == "key" and el.text else None
            elif role == "track":
                entry = value(el, 2)
                key = last["tracks"]
                if isinstance(entry, dict):
                    t = read_track(entry)
                    s.count("tracks", 1, MAX_TRACKS)
                    raw[key] = (str(entry.get("Track ID", key)), t)
                else:
                    raw[key] = ("", None)
            elif role == "playlists":
                lib["Playlists"] = lists
            elif role == "playlist":
                lists.append(playlist)
                s.count("playlists and folders", 1, MAX_NODES)
            elif role == "memberkey":
                last["playlist"] = el.text if el.tag == "key" and el.text else None
            elif role == "member":
                playlist[last["playlist"]] = value(el, 3)
            elif role == "items":
                playlist["Playlist Items"] = items
            elif role == "item":
                entry = value(el, 4)
                if isinstance(entry, dict):
                    ident = str(entry.get("Track ID"))
                    s.count("playlist entries", 1, MAX_REFS)
                    t = tracks.get(ident) if tracks is not None else ident
                    if t is not None:
                        items.append(t)
            if role not in ("plist", "top", "inner"):
                s.release()
    if top is None:
        raise ValueError(f"{name} is not an iTunes library")
    return lib, tracks or {}


# ------------------------------------------------------------
# Parents that lead round in a circle reach no root, and a chain of folders can be deeper than any walk of
# the tree goes. A list no root reaches starts a tree at the top, and what lies below TREE_DEPTH moves up there
def place_all(roots: list[Node], listed: list[tuple[Node, Node | None]]):
    placed: set[int] = set()
    done = 0

    def place():
        nonlocal done
        while done < len(roots):
            todo = [(roots[done], 1)]
            done += 1
            while todo:
                n, depth = todo.pop()
                placed.add(id(n))
                if depth >= TREE_DEPTH and n.children:
                    cut, n.children = n.children, []
                    roots.extend(cut)
                    continue
                todo.extend((c, depth + 1) for c in n.children if id(c) not in placed)

    place()
    for n, parent in listed:
        if id(n) not in placed and parent is not None:
            parent.children.remove(n)
            roots.append(n)
            place()


# ------------------------------------------------------------
def load(
    path: str, music_root: str = "", progress: Progress | None = None
) -> list[Node]:
    cb = progress or (lambda pct, msg: None)
    name = os.path.basename(path)
    check_size(path)
    cb(10, f"Reading {name}...")
    found = relocator(music_root)
    lib, tracks = read_library(path, name, cb, found)
    if not isinstance(lib.get("Tracks"), dict):
        raise ValueError(f"{name} is not an iTunes library")  # noqa: TRY004
    cb(60, f"{len(tracks)} tracks")

    nodes: dict[str, Node] = {}
    parents: dict[str, str] = {}
    order: list[str] = []
    lists = lib.get("Playlists")
    for p in lists if isinstance(lists, list) else []:
        if not isinstance(p, dict) or p.get("Master") or p.get("Distinguished Kind"):
            continue
        pid = str(p.get("Playlist Persistent ID", p.get("Playlist ID", "")))
        if not pid or pid in nodes:
            continue
        if p.get("Folder"):
            nodes[pid] = Node("folder", str(p.get("Name", "")))
        else:
            listed = p.get("Playlist Items")
            items = [
                t
                for t in (
                    i if isinstance(i, Track) else tracks.get(i)
                    for i in (listed if isinstance(listed, list) else [])
                )
                if t is not None
            ]
            if not items:
                continue
            nodes[pid] = Node("playlist", str(p.get("Name", "")), tracks=items)
        parents[pid] = str(p.get("Parent Persistent ID", ""))
        order.append(pid)

    roots: list[Node] = []
    for pid in order:
        parent = nodes.get(parents[pid])
        (
            parent.children
            if parent is not None and parent.is_folder and parent is not nodes[pid]
            else roots
        ).append(nodes[pid])
    place_all(roots, [(nodes[pid], nodes.get(parents[pid])) for pid in order])
    roots = [n for n in roots if not n.is_folder or n.children]
    if tracks:
        roots.append(Node("playlist", "All tracks", tracks=list(tracks.values())))
    cb(100, f"{len(tracks)} tracks")
    return roots
