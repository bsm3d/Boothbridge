# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : iTunes XML export writing a Library.xml property list that Apple Music and iTunes can import.

# A property list written by plistlib: header, Tracks keyed by track id, Playlists as a
# flat array where a folder is a playlist with Folder true and its children point at it
# with Parent Persistent ID. Only tags and playlists fit in it, no cue, loop or grid

from __future__ import annotations

import hashlib
import os
import plistlib
import threading
from datetime import UTC, datetime
from urllib.parse import quote

from ..model import Node, Track, playlists, unique_tracks
from .common import clean, copy_tracks, start, where
from .files import Report
from .fit import fit, text
from .manifest import Cancelled, Manifest, sha256_bytes, write_atomic

NAME = "Library.xml"
APPLICATION_VERSION = "12.13.2.3"

# the words Apple prints in the Kind column, by extension
KINDS = {
    ".mp3": "MPEG audio file",
    ".m4a": "AAC audio file",
    ".aac": "AAC audio file",
    ".wav": "WAV audio file",
    ".aif": "AIFF audio file",
    ".aiff": "AIFF audio file",
    ".flac": "FLAC audio file",
}

def word(s) -> str:
    return clean(text(s, 255))

# 16 hexadecimal digits, the same for the same thing every time so that a second export
# of one library does not create new tracks in a program that matched the first
def persistent(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16].upper()

# file://localhost/C:/Music/a%20b.mp3, and file://localhost/Users/me/a.mp3 on a Mac
def location(path: str) -> str:
    p = text(path, 32767)
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    p = p.replace("\\", "/")
    # a share (//server/share) and a Unix path already start with a slash
    lead = "file://localhost" if p.startswith("/") else "file://localhost/"
    return lead + quote(p, safe="/:")

def date_of(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s[:10])
    except ValueError:
        return None

# ----------------------------------------------------------------
def entry(t: Track, ident: int, path: str) -> dict:
    stem, ext = os.path.splitext(os.path.basename(path))
    d: dict = {"Track ID": ident, "Name": word(t.title or stem)}
    if t.artist:
        d["Artist"] = word(t.artist)
    if t.album_artist:
        d["Album Artist"] = word(t.album_artist)
    if t.composer:
        d["Composer"] = word(t.composer)
    if t.album:
        d["Album"] = word(t.album)
    if t.genre:
        d["Genre"] = word(t.genre)
    d["Kind"] = KINDS.get(ext.lower(), "Audio file")
    size = t.size
    if not size:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    if size:
        d["Size"] = size
    if t.duration > 0:
        d["Total Time"] = round(t.duration * 1000)
    if t.disc_no:
        d["Disc Number"] = t.disc_no
    if t.track_no:
        d["Track Number"] = t.track_no
    if t.year:
        d["Year"] = t.year
    if t.bpm > 0:
        d["BPM"] = round(t.bpm)
    for key, when in (("Date Modified", t.modified), ("Date Added", t.added)):
        if (dt := date_of(when)) is not None:
            d[key] = dt
    if t.bitrate:
        d["Bit Rate"] = t.bitrate
    if t.samplerate:
        d["Sample Rate"] = t.samplerate
    if t.plays:
        d["Play Count"] = t.plays
    if (dt := date_of(t.last_played)) is not None:
        d["Play Date UTC"] = dt
    if t.rating:
        d["Rating"] = t.rating * 20
    if t.comment:
        d["Comments"] = clean(t.comment)
    d["Persistent ID"] = persistent(t.uid)
    d["Track Type"] = "File"
    d["Location"] = location(path)
    return d

# ----------------------------------------------------------------
class Builder:
    def __init__(self, ids: dict[str, int], cancel: threading.Event | None):
        self.ids = ids
        self.cancel = cancel
        self.lists: list[dict] = []
        self.next = 1

    def listed(self, n: Node) -> list[dict]:
        return [{"Track ID": self.ids[t.uid]} for t in n.tracks if t.uid in self.ids]

    # a folder with nothing under it is left out, as the other writers do
    def worth(self, n: Node) -> bool:
        if n.is_folder:
            return any(self.worth(c) for c in n.children)
        return bool(self.listed(n))

    def add(self, nodes: list[Node], parent: str, trail: str):
        for i, n in enumerate(nodes):
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            if not self.worth(n):
                continue
            # the position in the tree keeps two playlists of one name apart
            here = f"{trail}/{i}/{n.name}"
            pid = persistent(here)
            d: dict = {
                "Name": word(n.name) or "_",
                "Playlist ID": 1000 + self.next,
                "Playlist Persistent ID": pid,
            }
            self.next += 1
            if parent:
                d["Parent Persistent ID"] = parent
            d["All Items"] = True
            self.lists.append(d)
            if n.is_folder:
                d["Folder"] = True
                self.add(n.children, pid, here)
            else:
                d["Playlist Items"] = self.listed(n)

# ----------------------------
def export(
    nodes: list[Node],
    out: str,
    copy: bool = False,
    verify: bool = False,
    progress=None,
    cancel: threading.Event | None = None,
    man: Manifest | None = None,
) -> Report:
    cb = progress or (lambda pct, msg: None)
    os.makedirs(out, exist_ok=True)
    rep = start(nodes, os.path.join(out, NAME))
    moved = {}
    if copy:
        moved = copy_tracks(
            nodes,
            out,
            rep,
            verify,
            lambda i, n, info="": cb(90 * i // n, f"Copying {i}/{n} {info}".strip()),
            cancel,
            man,
        )

    tracks, ids = {}, {}
    for original in unique_tracks(nodes):
        p = where(original, moved)
        if not p:
            continue  # without a file there is nothing for the program to find
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        ident = 1000 + len(ids)
        ids[original.uid] = ident
        tracks[str(ident)] = entry(fit(original), ident, p)

    build = Builder(ids, cancel)
    build.add(nodes, "", "")
    master = {
        "Name": "Library",
        "Master": True,
        "Playlist ID": 1000,
        "Playlist Persistent ID": persistent("master"),
        "All Items": True,
        "Playlist Items": [{"Track ID": i} for i in ids.values()],
    }
    folder = os.path.join(out, "Contents") if copy else out
    library = {
        "Major Version": 1,
        "Minor Version": 1,
        "Date": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        "Application Version": APPLICATION_VERSION,
        "Features": 5,
        "Show Content Ratings": True,
        "Music Folder": location(folder).rstrip("/") + "/",
        "Library Persistent ID": persistent("library " + os.path.abspath(out)),
        "Tracks": tracks,
        "Playlists": [master, *build.lists],
    }
    data = plistlib.dumps(library, fmt=plistlib.FMT_XML, sort_keys=False)
    write_atomic(rep.path, data)
    if man:
        man.add(rep.path, sha256_bytes(data))
    count = sum(1 for _ in playlists(nodes))
    cb(100, f"{len(tracks)} tracks written in {count} playlists")
    return rep
