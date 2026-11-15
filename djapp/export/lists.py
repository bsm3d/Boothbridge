# Benoit Saint-Moulin
# Boothbridge : list exports, CSV and JSON (one file) and PLS and XSPF (one file per playlist)

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from urllib.parse import quote

from .. import keys
from ..model import CUE, FADE_IN, FADE_OUT, GRID, LOAD, LOOP, Node, Track
from .common import NAME_LIMIT, clean, copy_tracks, folder_name, start, where
from .files import Cancelled, Planner, Report, safe_name
from .fit import NAME, fit, text, whole
from .manifest import Manifest, sha256_bytes, write_atomic

FORMAT = "boothbridge-playlists"
VERSION = 1
XSPF_NS = "http://xspf.org/ns/0/"
CSV_NAME = "playlists.csv"
JSON_NAME = "playlists.json"

COLUMNS = (
    "playlist",
    "position",
    "artist",
    "title",
    "album",
    "genre",
    "year",
    "bpm",
    "key",
    "rating",
    "energy",
    "duration",
    "path",
)
CUE_TYPES = {
    CUE: "cue",
    FADE_IN: "fade_in",
    FADE_OUT: "fade_out",
    LOAD: "load",
    LOOP: "loop",
}
# a spreadsheet runs a cell that starts with one of these (OWASP lists tab and CR too)
FORMULA = ("=", "+", "-", "@", "\t", "\r")
# some spreadsheets skip these in front of the first real character before they decide
SKIPPED = " \t\r\n\x0b\x0c\xa0\"'"


# ----------------------------------------------------------------
def one_line(s, limit: int = 32767) -> str:
    return text(s, limit).replace("\r", " ").replace("\n", " ")


def num(x, digits: int):
    if not x:
        return None
    r = round(x, digits)
    return int(r) if r == int(r) else r


def energy_of(t: Track):
    e = t.energy
    ok = isinstance(e, (int, float)) and not isinstance(e, bool) and 1 <= e <= 10
    return num(e, 2) if ok else None


# the one place a track becomes the values every list shows, None when it has nothing
def info(t: Track, p: str) -> dict:
    return {
        "artist": t.artist,
        "title": t.title,
        "album": t.album,
        "genre": t.genre,
        "year": t.year or None,
        "bpm": num(t.bpm, 2),
        "key": keys.name(t.key) or None,
        "rating": t.rating,
        "energy": energy_of(t),
        "duration": num(t.duration, 3),
        "path": text(p, 32767),
    }


def label(t: Track, p: str) -> str:
    if t.artist and t.title:
        return f"{t.artist} - {t.title}"
    return t.artist or t.title or os.path.splitext(os.path.basename(p))[0]


# ----------------------------------------------------------------
# every playlist with something in it, with the names of the folders above it
def walk(
    nodes: list[Node], parents: tuple[str, ...] = ()
) -> Iterator[tuple[tuple, Node]]:
    for n in nodes:
        if n.is_folder:
            yield from walk(n.children, (*parents, n.name))
        elif n.tracks:
            yield parents, n


# tracks a player can be given: the ones that have a file
def usable(n: Node, moved: dict[str, str]) -> list[tuple[Track, str]]:
    out = []
    for original in n.tracks:
        p = where(original, moved)
        if p:
            out.append((fit(original), text(p, 32767)))
    return out


def put(path: str, data: bytes, man: Manifest | None):
    write_atomic(path, data)
    if man:
        man.add(path, sha256_bytes(data))


def check(cancel: threading.Event | None):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def begin(nodes, out, copy, verify, cb, cancel, man) -> tuple[Report, dict]:
    os.makedirs(out, exist_ok=True)
    rep = start(nodes, out)
    moved = {}
    if copy:
        moved = copy_tracks(
            nodes,
            out,
            rep,
            verify,
            lambda i, n, note="": cb(90 * i // n, f"Copying {i}/{n} {note}".strip()),
            cancel,
            man,
        )
    return rep, moved


# ----------------------------------------------------------------
def csv_bytes(nodes: list[Node], moved: dict[str, str]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)

    # a quote in front keeps the cell a text, the one fix every spreadsheet understands
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, str) and (
            v.startswith(FORMULA) or v.lstrip(SKIPPED).startswith(FORMULA)
        ):
            return "'" + v
        return v

    for parents, n in walk(nodes):
        name = " / ".join(text(p, 1000) for p in (*parents, n.name))
        for i, original in enumerate(n.tracks, 1):
            t = fit(original)
            row = {"playlist": name, "position": i}
            row.update(info(t, where(original, moved)))
            w.writerow([cell(row[c]) for c in COLUMNS])
    # the BOM is what makes a spreadsheet read the accents as UTF-8
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------
def cue_dict(c) -> dict:
    d = {
        "type": CUE_TYPES.get(c.kind, "cue"),
        "slot": whole(c.hotcue, -1, 63) if c.hotcue >= 0 else None,
        "name": text(c.name, NAME),
        "start": round(c.start, 3),
    }
    if c.is_loop:
        d["end"] = round(c.start + c.length, 3)
    d["color"] = text(c.color, 32)
    return d


def track_dict(t: Track, p: str, position: int) -> dict:
    d = {"position": position, **info(t, p)}
    d["cues"] = [cue_dict(c) for c in t.cues if c.kind != GRID]
    if t.grid is not None:
        d["grid"] = {"anchor": round(t.grid, 3), "bpm": num(t.bpm, 2)}
    return d


def tree(nodes: list[Node], moved: dict[str, str]) -> list[dict]:
    out = []
    for n in nodes:
        if n.is_folder:
            kids = tree(n.children, moved)
            if kids:
                out.append(
                    {"kind": "folder", "name": text(n.name, 1000), "children": kids}
                )
        elif n.tracks:
            d = {"kind": n.kind, "name": text(n.name, 1000)}
            if n.query:
                d["query"] = text(n.query, 1000)
            d["tracks"] = [
                track_dict(fit(t), where(t, moved), i)
                for i, t in enumerate(n.tracks, 1)
            ]
            out.append(d)
    return out


def json_bytes(nodes: list[Node], moved: dict[str, str]) -> bytes:
    doc = {"format": FORMAT, "version": VERSION, "tree": tree(nodes, moved)}
    try:
        s = json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False)
        return s.encode("utf-8")
    except UnicodeEncodeError:
        # same fallback as the project file: escaped, a lone surrogate still gets through
        return json.dumps(doc, indent=2, ensure_ascii=True, allow_nan=False).encode(
            "ascii"
        )


# ----------------------------------------------------------------
def rel(p: str, base: str) -> str:
    try:
        p = os.path.relpath(p, base)
    except ValueError:  # other drive
        pass
    return p.replace("\\", "/")


def pls_bytes(n: Node, path: str, moved: dict[str, str], relative: bool) -> bytes:
    base = os.path.dirname(path)
    out = ["[playlist]"]
    found = usable(n, moved)
    for i, (t, p) in enumerate(found, 1):
        out.append(f"File{i}={one_line(rel(p, base) if relative else p)}")
        out.append(f"Title{i}={one_line(label(t, p), 1000)}")
        out.append(f"Length{i}={round(t.duration) or -1}")
    out += [f"NumberOfEntries={len(found)}", "Version=2"]
    return ("\n".join(out) + "\n").encode("utf-8")


# ------------------------------------------------------------
# file:///C:/Music/a%20b.mp3, file://server/share/a.mp3, file:///home/me/a.mp3 (RFC 8089),
# built by hand so that a Windows path of a collection still gives the right URL elsewhere
def file_url(p: str) -> str:
    s = p.replace("\\", "/")
    if re.match(r"[A-Za-z]:/", s):
        return "file:///" + quote(s, safe="/:")
    if s.startswith("//"):
        host, _, rest = s[2:].partition("/")
        return "file://" + quote(host, safe="") + "/" + quote(rest, safe="/")
    if not s.startswith("/"):
        return file_url(os.path.abspath(p))
    return "file://" + quote(s, safe="/:")


def xml_text(s, limit: int = NAME) -> str:
    return clean(one_line(s, limit))


def xspf_bytes(n: Node, path: str, moved: dict[str, str], relative: bool) -> bytes:
    root = ET.Element("playlist", {"version": "1", "xmlns": XSPF_NS})
    ET.SubElement(root, "title").text = xml_text(n.name)
    tl = ET.SubElement(root, "trackList")
    for t, p in usable(n, moved):
        el = ET.SubElement(tl, "track")
        ET.SubElement(el, "location").text = clean(file_url(p))
        if t.title:
            ET.SubElement(el, "title").text = xml_text(t.title)
        if t.artist:
            ET.SubElement(el, "creator").text = xml_text(t.artist)
        if t.comment:
            ET.SubElement(el, "annotation").text = clean(text(t.comment, 1000))
        if t.album:
            ET.SubElement(el, "album").text = xml_text(t.album)
        if t.duration > 0:
            ET.SubElement(el, "duration").text = str(round(t.duration * 1000))
    ET.indent(root, "  ")
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


# ----------------------------------------------------------------
def single(nodes, out, name, build, copy, verify, progress, cancel, man) -> Report:
    cb = progress or (lambda pct, msg: None)
    rep, moved = begin(nodes, out, copy, verify, cb, cancel, man)
    check(cancel)
    put(os.path.join(out, name), build(nodes, moved), man)
    cb(100, f"{name} written")
    return rep


def each(nodes, out, ext, build, copy, verify, progress, cancel, man) -> Report:
    cb = progress or (lambda pct, msg: None)
    rep, moved = begin(nodes, out, copy, verify, cb, cancel, man)
    plan = Planner()
    found = list(walk(nodes))
    low = 90 if copy else 0
    done = 0
    for parents, n in found:
        check(cancel)
        # a playlist none of whose tracks has a file would be an empty list
        if usable(n, moved):
            folder = os.path.join(
                out, "Playlists", *(folder_name(p, ext) for p in parents)
            )
            os.makedirs(folder, exist_ok=True)
            path = plan.claim(os.path.join(folder, safe_name(n.name, NAME_LIMIT) + ext))
            # relative paths only make sense when the audio travels with the playlists
            put(path, build(n, path, moved, copy), man)
            done += 1
        cb(low + (100 - low) * (done + 1) // (len(found) + 1), f"{done} playlists")
    cb(100, f"{done} playlists written")
    return rep


# ----------------------------
def export_csv(
    nodes, out, copy=False, verify=False, progress=None, cancel=None, man=None
) -> Report:
    return single(nodes, out, CSV_NAME, csv_bytes, copy, verify, progress, cancel, man)


def export_json(
    nodes, out, copy=False, verify=False, progress=None, cancel=None, man=None
) -> Report:
    return single(
        nodes, out, JSON_NAME, json_bytes, copy, verify, progress, cancel, man
    )


def export_pls(
    nodes, out, copy=False, verify=False, progress=None, cancel=None, man=None
) -> Report:
    return each(nodes, out, ".pls", pls_bytes, copy, verify, progress, cancel, man)


def export_xspf(
    nodes, out, copy=False, verify=False, progress=None, cancel=None, man=None
) -> Report:
    return each(nodes, out, ".xspf", xspf_bytes, copy, verify, progress, cancel, man)
