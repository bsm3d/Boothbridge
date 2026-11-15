# Benoit Saint-Moulin
# Boothbridge : what the file exports share (audio copy, XML output)

from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET

from ..model import Node, Track, unique_tracks
from .files import Planner, Report, copy_all, present, safe_name, same_content
from .manifest import Manifest, replace

# 255 chars max on Windows, room left for the (2) of a name already taken
NAME_LIMIT = 200

# the controls XML 1.0 does not allow, half a surrogate pair, and the two noncharacters a wrong byte order makes of a BOM
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


# ---------------------------------------
# ElementTree writes control chars as is, then nothing can read the file back
def clean(s) -> str:
    return _XML_BAD.sub("", str(s))


def write_xml(root: ET.Element, path: str, decl: str):
    ET.indent(root, "  ")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(decl + "\n")
            f.write(ET.tostring(root, encoding="unicode"))
            f.write("\n")
        replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# the directory of a folder node, for the exports that write one file per playlist. A folder
# named like a playlist file ("A.m3u8" beside the playlist "A") took the path of that file,
# and whichever came second failed: a name that ends with the extension gets an underscore
def folder_name(name: str, ext: str) -> str:
    s = safe_name(name, NAME_LIMIT)
    return s + "_" if s.lower().endswith(ext) else s


def start(nodes: list[Node], path: str) -> Report:
    tracks = unique_tracks(nodes)
    return Report(
        tracks=len(tracks),
        path=path,
        missing=[t.path or t.title for t in tracks if not present(t)],
    )


# audio copied under out/Contents/Artist/Album, returns uid -> new path
def copy_tracks(
    nodes: list[Node],
    out: str,
    rep: Report,
    verify: bool,
    progress=None,
    cancel: threading.Event | None = None,
    man: Manifest | None = None,
) -> dict[str, str]:
    plan = Planner()
    moved, jobs = {}, []
    for t in unique_tracks(nodes):
        if not present(t):
            continue

        def fits(rel: str, t: Track = t) -> bool:
            # a second song with the same artist, album and file name used to take the place of
            # the first: a file already there has to be the same recording, else the next name
            there = os.path.join(out, rel)
            return not os.path.lexists(there) or same_content(t.path, there)

        rel = plan.claim(
            os.path.join(
                "Contents",
                safe_name(t.artist or "Unknown Artist", NAME_LIMIT),
                safe_name(t.album or "Unknown Album", NAME_LIMIT),
                safe_name(os.path.basename(t.path), NAME_LIMIT),
            ),
            fits,
        )
        dst = os.path.join(out, rel)
        moved[t.uid] = dst
        jobs.append((t.path, dst))
    rep.errors += copy_all(jobs, verify, cancel, progress, man)
    return moved


def where(t: Track, moved: dict[str, str]) -> str:
    return moved.get(t.uid, t.path)
