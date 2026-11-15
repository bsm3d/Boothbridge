# Benoit Saint-Moulin
# Boothbridge : VirtualDJ export, one .vdjfolder per playlist in a tree of folders

# VirtualDJ keeps its lists as small XML files: <VirtualFolder> holding one <song> per
# track, only path is required, size helps it find the track in its own database.
# No database.xml is written: merging one into the database of a user would be a
# gamble, so cues, loops and beat grids stay behind. The bpm attribute is left out too,
# the public page does not say in which unit it is read

from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET

from ..model import Node
from .common import NAME_LIMIT, clean, copy_tracks, folder_name, start, where, write_xml
from .files import Planner, Report, safe_name
from .fit import fit, text
from .manifest import Cancelled, Manifest

# the folder of VirtualDJ 8 that the reader of this program walks as well
ROOT = "Folders"


def attr(s) -> str:
    return clean(text(s, 32767)).replace("\r", " ").replace("\n", " ")


# ----------------------------------------------------------------
def size_of(size: int, path: str) -> int:
    if size:
        return size
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def write_list(n: Node, path: str, moved: dict[str, str], man: Manifest | None):
    root = ET.Element(
        "VirtualFolder", noDuplicates="no", singleDrive="no", ordered="yes"
    )
    idx = 0
    for original in n.tracks:
        p = where(original, moved)
        if not p:
            continue
        t = fit(original)
        song = ET.SubElement(root, "song", path=attr(p))
        size = size_of(t.size, p)
        if size:
            song.set("size", str(size))
        if t.artist:
            song.set("artist", attr(t.artist))
        if t.title:
            song.set("title", attr(t.title))
        if t.duration > 0:
            song.set("songlength", f"{t.duration:.1f}")
        song.set("idx", str(idx))
        idx += 1
    write_xml(root, path, '<?xml version="1.0" encoding="UTF-8"?>')
    if man:
        man.add(path)


def has_audio(n: Node) -> bool:
    return any(t.path for t in n.tracks)


def walk(
    nodes: list[Node],
    folder: str,
    moved: dict[str, str],
    plan: Planner,
    cancel: threading.Event | None,
    man: Manifest | None,
) -> int:
    count = 0
    for n in nodes:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if n.is_folder:
            # two folders of one name share one directory rather than one hiding the other
            count += walk(
                n.children,
                os.path.join(folder, folder_name(n.name, ".vdjfolder")),
                moved,
                plan,
                cancel,
                man,
            )
            continue
        if not has_audio(n):
            continue
        os.makedirs(folder, exist_ok=True)
        path = plan.claim(
            os.path.join(folder, safe_name(n.name, NAME_LIMIT) + ".vdjfolder")
        )
        write_list(n, path, moved, man)
        count += 1
    return count


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
    rep = start(nodes, out)
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
    n = walk(nodes, os.path.join(out, ROOT), moved, Planner(), cancel, man)
    cb(100, f"{n} playlists written")
    return rep
