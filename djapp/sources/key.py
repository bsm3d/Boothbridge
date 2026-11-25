# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reader for a CDJ USB key that turns export.pdb and ANLZ files back into playlists, writing nothing to the key.

# the inverse of the CDJ export: the playlist tree of PIONEER/rekordbox/export.pdb, every track
# row, and the beat grid and cues of its ANLZ files. A track's path is the audio file on the key.
# Nothing here writes to the key

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .. import keys
from ..model import CUE, LOOP, Cue, Node, Track
from . import Progress, check_size, relocator

log = logging.getLogger(__name__)

# the database of a key with a hundred thousand tracks is tens of MB
MAX_DB = 256 << 20
MAX_DEPTH = 32
DB = ("PIONEER", "rekordbox", "export.pdb")
# the folder the exporter derives from a track path, a file outside of it is not read
ANLZ_PATH = re.compile(
    r"^/PIONEER/USBANLZ/P[0-9A-F]{3}/[0-9A-F]{8}/ANLZ0000\.DAT$", re.IGNORECASE
)

# ------------------------------------------------------------
# the key has a database, but it cannot be read
class KeyReadError(ValueError):
    pass

@dataclass
class KeyData:
    root: str  # the key
    nodes: list[Node]  # the playlist tree
    warnings: list[str] = field(default_factory=list)
    # path on the key, lower case -> its ANLZ0000.DAT
    anlz: dict[str, str] = field(default_factory=dict)
    unlisted: int = 0  # tracks of the database that are in no playlist
    # path on the key, lower case -> its artwork number
    art: dict[str, int] = field(default_factory=dict)
    # paths (lower case) whose DAT and EXT were both read whole
    intact: set[str] = field(default_factory=set)
    # the tracks that are in no playlist, read like the others so a new database can keep them
    extra: list[Track] = field(default_factory=list)

# finding the database

# ------------------------------------------------------------
# (root of the key, export.pdb) for the root of a key or for the export.pdb file, None
# for anything else
def locate(path: str) -> tuple[str, str] | None:
    p = os.path.abspath(path)
    if os.path.isfile(p):
        if os.path.basename(p).lower() != "export.pdb":
            return None
        rek = os.path.dirname(p)
        pio = os.path.dirname(rek)
        if (
            os.path.basename(rek).lower() == "rekordbox"
            and os.path.basename(pio).lower() == "pioneer"
        ):
            return os.path.dirname(pio), p
        return rek, p
    db = os.path.join(p, *DB)
    return (p, db) if os.path.isfile(db) else None

def detect(path: str) -> bool:
    return locate(path) is not None

# ------------------------------------------------------------
# the path of a file as the database writes it ("/Contents/..."), "" for a file that is
# not inside root
def key_path(root: str, path: str) -> str:
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # another drive
        return ""
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return ""
    return "/" + rel.replace("\\", "/")

# the file a database path stands for, "" when the path is not a plain path inside the key
def on_disk(root: str, usb: str) -> str:
    parts = usb.split("/")
    if not usb.startswith("/") or any(
        p in ("", ".", "..") or ":" in p or "\\" in p or any(c < " " for c in p)
        for p in parts[1:]
    ):
        return ""
    return os.path.join(root, *parts[1:])

# reading

# ------------------------------------------------------------
# the rows of the playlist tree in display order, depth first, as (row, depth), and the
# number of rows that could not be placed: a parent that does not exist, a loop, a repeated
# id, or a folder nested past MAX_DEPTH
def outline(tree) -> tuple[list[tuple[object, int]], int]:
    kids: dict[int, list[tuple[int, int, object]]] = {}
    for i, n in enumerate(tree):
        kids.setdefault(n.parent, []).append((n.sort, i, n))
    for v in kids.values():
        v.sort(key=lambda x: x[:2])
    out, seen = [], set()
    todo = [(iter(kids.get(0, ())), 0)]
    while todo:
        it, depth = todo[-1]
        nxt = next(it, None)
        if nxt is None:
            todo.pop()
            continue
        n = nxt[2]
        if n.id in seen or depth > MAX_DEPTH:
            continue
        seen.add(n.id)
        out.append((n, depth))
        if n.folder:
            todo.append((iter(kids.get(n.id, ())), depth + 1))
    return out, len(tree) - len(out)

def cue_of(c) -> Cue:
    loop = c.loop and c.end is not None and c.end > c.start
    return Cue(
        name=c.name,
        kind=LOOP if loop else CUE,
        start=float(c.start),
        length=float(c.end - c.start) if loop else 0.0,
        hotcue=c.slot - 1,
        color=c.color,
    )

# ------------------------------------------------------------
# the beat grid and cues of one track, None (and a warning) when its files are missing
# or damaged
def analysis(root: str, usb: str, anlz_path: str, warn):
    from ..export.cdj import anlzread

    dat = on_disk(root, anlz_path) if ANLZ_PATH.match(anlz_path) else ""
    if not dat:
        warn(f"{usb}: no usable analysis path, loaded without cues")
        return None
    got = []
    for f in (dat, dat[:-3] + "EXT"):
        try:
            got.append(anlzread.read(f))
        except OSError:
            warn(f"{usb}: {os.path.basename(f)} is missing or unreadable")
            got.append(None)
        except anlzread.AnlzError as e:
            warn(f"{usb}: {os.path.basename(f)} is damaged ({e}), loaded without it")
            got.append(None)
    if got == [None, None]:
        return None
    a = anlzread.combine(*got)
    if a.path and a.path != usb:
        warn(f"{usb}: its analysis files belong to {a.path}, loaded without them")
        return None
    return a

# ------------------------------------------------------
# everything a key holds that the model can keep. ValueError (KeyReadError) when
# there is no database or it cannot be followed, a damaged analysis file is only a warning
def read(path: str, music_root: str = "", progress: Progress | None = None) -> KeyData:
    from ..export.cdj import pdbread

    cb = progress or (lambda pct, msg: None)
    found = locate(path)
    if not found:
        raise KeyReadError(
            f"{os.path.basename(path) or path} is not a CDJ USB key (no {'/'.join(DB)})"
        )
    root, db = found
    check_size(db, MAX_DB)
    try:
        with open(db, "rb") as f:
            data = f.read()
    except OSError as e:
        raise KeyReadError(f"cannot read {'/'.join(DB)}: {e}") from e
    try:
        pdb = pdbread.read(data)
        rows = [pdbread.track_row(r) for r in pdbread.rows(pdb, pdbread.TRACKS)]
        genres, labels, kinds = (
            pdbread.names(pdb, k)
            for k in (pdbread.GENRES, pdbread.LABELS, pdbread.KEYS)
        )
        artists, albums = pdbread.artists(pdb), pdbread.albums(pdb)
        tree, entries = pdbread.tree(pdb), pdbread.entries(pdb)
    except pdbread.PdbError as e:
        raise KeyReadError(f"{'/'.join(DB)} is damaged: {e}") from e

    warnings: list[str] = []

    def warn(msg: str):
        warnings.append(msg)

    resolve = relocator(music_root)
    pitch = {keys.name(i): i for i in range(24)}
    by_id: dict[int, Track] = {}
    usb_of: dict[int, str] = {}
    anlz_of: dict[int, str] = {}
    art_of: dict[int, int] = {}
    twice = 0
    for r in rows:
        if r.id in by_id:
            twice += 1
            continue
        s = r.strings
        t = Track(title=s[17], comment=s[16], added=s[10])
        t.artist = artists.get(r.artist, "") if r.artist else ""
        album, album_artist = albums.get(r.album, ("", 0)) if r.album else ("", 0)
        t.album = album
        t.album_artist = artists.get(album_artist, "") if album_artist else ""
        if t.album_artist == t.artist:
            # the writer gives an empty album artist the artist's id
            t.album_artist = ""
        t.genre = genres.get(r.genre, "") if r.genre else ""
        t.label = labels.get(r.label, "") if r.label else ""
        t.remixer = artists.get(r.remixer, "") if r.remixer else ""
        t.composer = artists.get(r.composer, "") if r.composer else ""
        name = kinds.get(r.key, "") if r.key else ""
        t.key = pitch.get(name, keys.parse(name))
        t.size, t.bitrate, t.samplerate = r.size, r.bitrate, r.samplerate or 44100
        t.duration = float(r.seconds)
        t.bpm = r.tempo / 100
        t.rating, t.plays, t.year = min(r.rating, 5), r.plays, r.year
        t.track_no, t.disc_no = r.track_no, r.disc
        t.color = r.color if r.color <= 8 else 0
        usb = s[20]
        disk = on_disk(root, usb)
        if disk:
            t.path = resolve(disk)
        else:
            warn(f"{usb or 'a track'}: not a path inside the key, left without a file")
        by_id[r.id] = t
        usb_of[r.id] = usb if disk else ""
        anlz_of[r.id] = s[14]
        art_of[r.id] = r.art

    if twice:
        warn(
            f"track ids that appear twice in the database, the first row is used: {twice}"
        )

    members: dict[int, list[tuple[int, int, int]]] = {}
    for i, (pos, tid, pid) in enumerate(entries):
        members.setdefault(pid, []).append((pos, i, tid))
    placed, left = outline(tree)
    nodes: list[Node] = []
    stack = [nodes]
    used: set[int] = set()
    dangling = 0
    for n, depth in placed:
        if n.folder:
            node = Node("folder", n.name)
        else:
            node = Node("playlist", n.name)
            for _, _, tid in sorted(members.get(n.id, ())):
                t = by_id.get(tid)
                if t is None:
                    dangling += 1
                else:
                    node.tracks.append(t)
                    used.add(tid)
        stack[depth].append(node)
        if n.folder:
            del stack[depth + 1 :]
            stack.append(node.children)
    if dangling:
        warn(f"playlist entries that refer to tracks not in the database: {dangling}")
    if left:
        warn(f"playlists that could not be placed in the tree, left out: {left}")

    todo = list(by_id)
    intact: set[str] = set()
    for n, i in enumerate(todo):
        if n % 5 == 0:
            cb(5 + 90 * n // max(1, len(todo)), f"Reading the key {n}/{len(todo)}")
        t, usb = by_id[i], usb_of[i]
        if not usb:
            continue
        a = analysis(root, usb, anlz_of[i], warn)
        if a is None:
            continue
        if a.whole:
            intact.add(usb.lower())
        if a.grid is not None:
            t.grid = a.grid
            t.bpm = t.bpm or a.bpm
        t.cues = [cue_of(c) for c in a.cues]
        if a.frames and t.samplerate:
            exact = a.frames / t.samplerate
            if abs(exact - t.duration) <= 1.0:
                t.duration = exact  # the database only has whole seconds
    anlz = {u.lower(): anlz_of[i] for i, u in usb_of.items() if u}
    art = {u.lower(): art_of[i] for i, u in usb_of.items() if u and art_of[i]}
    extra = [by_id[i] for i in by_id if i not in used and usb_of[i]]
    cb(100, f"{len(used)} tracks")
    return KeyData(
        root, nodes, warnings, anlz, len(by_id) - len(used), art, intact, extra
    )

# ------------------------------------------------------------
# the playlists of a key. What could not be read is logged as a warning. notes, when given,
# receives the number of warnings and of tracks that no playlist lists, for a window to show
def load(
    path: str,
    music_root: str = "",
    progress: Progress | None = None,
    notes: dict | None = None,
) -> list[Node]:
    got = read(path, music_root, progress)
    if notes is not None:
        notes.update(key_warnings=len(got.warnings), key_unlisted=got.unlisted)
    for w in got.warnings[:20]:
        log.warning(w)
    if len(got.warnings) > 20:
        log.warning("and %d more warnings reading the key", len(got.warnings) - 20)
    if got.unlisted:
        log.info(
            "tracks of the database that are in no playlist, not loaded: %d",
            got.unlisted,
        )
    return got.nodes
