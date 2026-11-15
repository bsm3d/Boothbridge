# seeded random tests on the application's own modules.
#
# A scenario is a function (seed, iterations, deadline) -> list of Failure. Every case uses its
# own seed (seed + case number), so a failure replays alone with --iterations 1 on that seed.
# To add a scenario, write the function and add one line to SCENARIOS

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import io
import json
import math
import os
import random
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass

from .. import APP_ROOT
from . import ROOT, short


# the scenario cannot run here (a module or a function it needs is missing)
class Skip(Exception):
    pass


# ------------------------------------------------------------
@dataclass
class Failure:
    scenario: str
    seed: int
    case: str
    message: str
    detail: str = ""

    def replay(self) -> str:
        return f"python tests/test_center.py --random --scenario {self.scenario} --seed {self.seed} --iterations 1"

    def signature(self) -> str:
        return self.scenario + ":" + re.sub(r"\d+", "N", self.message.splitlines()[0] if self.message else "")[:120]


# ------------------------------------------------------------
# the module, or Skip when it or one of its attributes does not exist (yet)
def need(module: str, *names: str):
    try:
        mod = importlib.import_module(module)
    except ImportError as error:
        raise Skip(f"module absent : {module} ({error})") from error
    for name in names:
        if not hasattr(mod, name):
            raise Skip(f"fonction absente : {module}.{name}")
    return mod


_cases_done = {"n": 0}


# ------------------------------------------------------------
# (case seed, rng) pairs, stopped early when the time budget is gone
def cases(seed: int, iterations: int, deadline: float):
    for i in range(iterations):
        if time.monotonic() > deadline:
            return
        _cases_done["n"] += 1
        case_seed = seed + i
        yield case_seed, random.Random(case_seed)


# ------------------------------------------------------------
# runs fn in a thread so that a hang is reported instead of freezing the run
def guarded(fn, limit: float = 20.0):
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as error:  # the caller classifies every kind of error  # noqa: BLE001
            box["error"] = error
            box["trace"] = traceback.format_exc()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        return "hang", None, ""
    if "error" in box:
        return "error", box["error"], box["trace"]
    return "ok", box.get("value"), ""


# ------------------------------------------------------------
# random data
# ------------------------------------------------------------

TEXTS = ["", " ", "Night Drive", "Café déjà vu", "Ça va ? Ñandú", "日本語のタイトル", "שלום עולם", "مرحبا بالعالم",
         "\U0001F3B5 emoji \U0001F525", "a/b\\c:d", "<tag> & \"quote\" 'apostrophe'", "x" * 300,
         "tab\there", "line\nbreak", "../../up", "C:\\Windows\\x", "NUL", "\u202eRTL override"]


# --------------------------------------------------------------
def rand_text(rng: random.Random, controls: bool = False) -> str:
    text = rng.choice(TEXTS)
    if controls and rng.random() < 0.3:
        text += chr(rng.choice([0, 1, 7, 8, 11, 12, 27, 31]))
    return text


# --------------------------------------------
def random_cues(rng: random.Random, duration: float):
    from djapp.model import CUE, LOAD, LOOP, Cue
    cues = []
    for slot in rng.sample(range(8), rng.randint(0, 5)):
        cues.append(Cue(name=rand_text(rng), kind=CUE, start=round(rng.uniform(0, duration * 1000), 3),
                        hotcue=slot, color=rng.choice(["", "#FF0000", "#28E214"])))
    for _ in range(rng.randint(0, 3)):
        cues.append(Cue(name=rand_text(rng), kind=rng.choice([CUE, LOAD]),
                        start=round(rng.uniform(0, duration * 1000), 3)))
    for _ in range(rng.randint(0, 2)):
        cues.append(Cue(name="loop", kind=LOOP, start=round(rng.uniform(0, duration * 900), 3),
                        length=round(rng.uniform(1, 4000), 3)))
    rng.shuffle(cues)
    return cues


# ------------------------------------------------------------
def random_track(rng: random.Random, folder: str, index: int, controls: bool = False, path: str = ""):
    from djapp.model import Track
    duration = round(rng.uniform(0.5, 600), 3)
    t = Track(title=rand_text(rng, controls) or f"t{index}", artist=rand_text(rng, controls),
              album=rand_text(rng, controls), genre=rng.choice(["House", "Techno", "", "Pop / Rock", "Deep-House"]),
              label=rand_text(rng), comment=rand_text(rng), remixer=rand_text(rng), composer=rand_text(rng),
              album_artist=rand_text(rng),
              path=path or os.path.join(folder, f"track{index}" + rng.choice([".wav", ".mp3", ".flac", ".m4a"])),
              size=rng.randint(0, 10**9), bitrate=rng.choice([0, 128, 320, 1411]),
              samplerate=rng.choice([22050, 44100, 48000]), duration=duration,
              bpm=rng.choice([0.0, round(rng.uniform(60, 200), 2)]), key=rng.choice([None, *range(24)]),
              gain=round(rng.uniform(-12, 12), 2), energy=rng.choice([None, float(rng.randint(1, 10))]),
              rating=rng.randint(0, 5), plays=rng.randint(0, 99), color=rng.randint(0, 8),
              year=rng.randint(0, 2030), track_no=rng.randint(0, 99), disc_no=rng.randint(0, 3),
              added="2025-01-02", modified="2026-02-03", last_played="", grid=rng.choice([None, round(rng.uniform(0, 500), 3)]),
              audio_id=rng.choice(["", f"id-{index}-{rng.randint(0, 9999)}"]), locked=rng.random() < 0.1)
    t.cues = random_cues(rng, duration)
    return t


# --------------------------------------------------------
# A tree of folders and playlists whose tracks are shared between playlists
def random_nodes(rng: random.Random, folder: str, controls: bool = False, path: str = ""):
    from djapp.model import Node
    pool = [random_track(rng, folder, i, controls, path) for i in range(rng.randint(1, 14))]

    def playlist(name):
        return Node(rng.choice(["playlist", "playlist", "smartlist"]), name,
                    tracks=[rng.choice(pool) for _ in range(rng.randint(0, 8))],
                    uuid=rng.choice(["", f"{rng.getrandbits(64):016x}"]), query="")

    def level(depth):
        out = []
        for i in range(rng.randint(1, 3)):
            if depth < 3 and rng.random() < 0.3:
                out.append(Node("folder", rand_text(rng) or f"folder{i}", children=level(depth + 1)))
            else:
                out.append(playlist(rand_text(rng) or f"list{i}"))
        return out

    return level(0)


# ------------------------------------------------------------
# where two JSON trees differ, empty when they are equal
def same_json(a, b, where="") -> str:
    if type(a) is not type(b):
        return f"{where}: {type(a).__name__} / {type(b).__name__}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{where}/{key}: present on one side only"
            found = same_json(a[key], b[key], f"{where}/{key}")
            if found:
                return found
    elif isinstance(a, list):
        if len(a) != len(b):
            return f"{where}: length {len(a)} / {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            found = same_json(x, y, f"{where}[{i}]")
            if found:
                return found
    elif a != b and not (isinstance(a, float) and math.isnan(a) and math.isnan(b)):
        return f"{where}: {short(repr(a), 60)} / {short(repr(b), 60)}"
    return ""


# ------------------------------------------------------------
# (a) projects
# ------------------------------------------------------------

HOSTILE = [None, True, False, 0, -1, 2**31, 2**63, 10**400, 1e308, -1e308, float("nan"), float("inf"),
           float("-inf"), 5e-324, "", " ", "x" * 200000, "\x00", "../../etc", "C:\\Windows", [], {},
           [None], {"a": 1}, [[1]], "NaN", "\U0001F3B5" * 50, "DEEP"]
DEEP = "\x00DEEP\x00"


# ------------------------------------------------------------
def locations(doc, path=()):
    yield path
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield from locations(value, path + (key,))
    elif isinstance(doc, list):
        for i, value in enumerate(doc[:30]):
            yield from locations(value, path + (i,))


# ------------------------------------------------------------
def mutate_doc(rng: random.Random, doc):
    for _ in range(rng.randint(1, 4)):
        spots = [p for p in locations(doc) if p]
        if not spots:
            return doc
        path = rng.choice(spots)
        parent = doc
        for step in path[:-1]:
            parent = parent[step]
        last = path[-1]
        op = rng.choice(["set", "set", "set", "delete", "duplicate", "swap"])
        if op == "set":
            value = rng.choice(HOSTILE)
            parent[last] = DEEP if value == "DEEP" else copy.deepcopy(value)  # HOSTILE itself must stay as it is
        elif op == "delete":
            del parent[last]
        elif op == "duplicate" and isinstance(parent, list):
            parent.insert(last, copy.deepcopy(parent[last]))  # a shared copy could end up inside itself
        else:
            old = parent[last]
            parent[last] = rng.choice([[old], {"v": old}, str(old), [], 0, None, [[old]]])
    return doc


# ------------------------------------------------------------
def to_text(rng: random.Random, doc) -> str:
    text = json.dumps(doc)
    depth = rng.choice([50, 900, 5000, 60000])
    text = text.replace(json.dumps(DEEP), "[" * depth + "]" * depth)
    roll = rng.random()
    if roll < 0.15:
        text = text[:rng.randrange(len(text) + 1)]
    elif roll < 0.25 and text:
        pos = rng.randrange(len(text))
        text = text[:pos] + rng.choice(["}", "]", ",", '"', "\x00", "NaN", "Infinity"]) + text[pos + 1:]
    return text


# ------------------------------------------------------------
# what a loaded project must satisfy whatever the file said; empty when fine
def check_loaded(nodes, cfg, project) -> str:
    from djapp.model import Cue, Node, Track, unique_tracks
    if not isinstance(nodes, list) or not all(isinstance(n, Node) for n in nodes):
        return "the tree is not a list of Node"
    for t in unique_tracks(nodes):
        if not isinstance(t, Track):
            return "a track is not a Track"
        for name, value in ((n, getattr(t, n)) for n in project.TRACK_FIELDS):
            if isinstance(value, float) and not math.isfinite(value):
                return f"track.{name} is not finite"
            if name in ("size", "bitrate", "samplerate", "rating", "plays", "color", "year", "track_no",
                        "disc_no") and (not isinstance(value, int) or isinstance(value, bool)):
                return f"track.{name} is {type(value).__name__}"
            if name in ("title", "artist", "path", "album") and not isinstance(value, str):
                return f"track.{name} is {type(value).__name__}"
        for c in t.cues:
            if not isinstance(c, Cue) or not all(math.isfinite(v) for v in (c.start, c.length)):
                return "a cue has a non finite number"
    if not isinstance(cfg, dict):
        return "settings are not a dict"
    json.dumps(project.dump(nodes, cfg), allow_nan=False)
    return ""


# ------------------------------------------------------------
def project_json(seed, iterations, deadline):
    project = need("djapp.sources.project", "dump", "read")
    failures = []
    try:
        from djapp.ui.main_window import playlist_duration
        from djapp.ui.zoomwave import clock
    except ImportError:
        playlist_duration = clock = None
    with tempfile.TemporaryDirectory(prefix="tbfuzz-") as tmp:
        music = os.path.join(tmp, "music")
        os.makedirs(music)
        path = os.path.join(tmp, "p.json")
        for case_seed, rng in cases(seed, iterations, deadline):
            def fail(case, message, detail="", seed=case_seed):
                failures.append(Failure("project", seed, case, message, detail))

            nodes = random_nodes(rng, music, controls=True)
            cfg = {"music_root": music, "crossfade": rng.randint(0, 9), "key_format": rng.choice(["Camelot", "Open Key"]),
                   "copy_music": rng.random() < 0.5, "export_format": "M3U"}
            doc = project.dump(nodes, cfg)
            text = json.dumps(doc, ensure_ascii=rng.random() < 0.5)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                nodes2, cfg2 = project.read(path)
            except Exception as error:  # any failure to read a valid project is a finding  # noqa: BLE001
                fail("aller-retour", f"a valid project is refused: {type(error).__name__}: {error}",
                     traceback.format_exc())
                continue
            back = project.dump(nodes2, {**cfg, **cfg2})
            for d in (doc, back):
                d.pop("saved", None)
            found = same_json(doc, back)
            if found:
                fail("aller-retour", f"round trip differs at {found}")

            bad = mutate_doc(rng, json.loads(text))
            with open(path, "w", encoding="utf-8") as f:
                f.write(to_text(rng, bad))
            status, value, trace = guarded(lambda: project.read(path), 30)
            if status == "hang":
                fail("mutation", "project.read does not return (30 s)")
            elif status == "error":
                if not isinstance(value, (ValueError, OSError)):
                    fail("mutation", f"{type(value).__name__}: {short(value, 120)}", trace)
            else:
                problem = check_loaded(value[0], value[1], project)
                if problem:
                    fail("mutation", f"loaded project is not sane: {problem}")
                elif playlist_duration is not None:
                    for node in value[0][:5]:
                        try:
                            playlist_duration(node)
                            for t in node.tracks[:5]:
                                clock(t.duration * 1000)
                        except Exception as error:  # noqa: BLE001
                            fail("mutation", f"display of a loaded project fails: {type(error).__name__}: {error}",
                                 traceback.format_exc())
                            break
    return failures


# ------------------------------------------------------------
# (b) readers
# ------------------------------------------------------------

def read_file(path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------
def mutate_bytes(rng: random.Random, data: bytes, donors: list[bytes], text: bool) -> bytes:
    for _ in range(rng.randint(1, 4)):
        if not data:
            break
        op = rng.choice(["cut", "flip", "dup", "splice", "delete", "insert", "zero", "grow"] + (["number", "deep"] * 2 if text else []))
        n = len(data)
        a = rng.randrange(n)
        b = min(n, a + rng.randint(1, max(1, n // 4)))
        if op == "cut":
            data = data[:a]
        elif op == "flip":
            ba = bytearray(data)
            for _ in range(rng.randint(1, 12)):
                ba[rng.randrange(n)] ^= 1 << rng.randrange(8)
            data = bytes(ba)
        elif op == "dup":
            data = data[:b] + data[a:b] + data[b:]
        elif op == "splice" and donors:
            donor = rng.choice(donors)
            if donor:
                x = rng.randrange(len(donor))
                data = data[:a] + donor[x:x + rng.randint(1, 400)] + data[b:]
        elif op == "delete":
            data = data[:a] + data[b:]
        elif op == "insert":
            data = data[:a] + bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 32))) + data[a:]
        elif op == "zero":
            data = data[:a] + bytes(b - a) + data[b:]
        elif op == "grow":
            data = data[:a] + data[a:b] * rng.randint(2, 40) + data[b:]
        elif op == "number":
            raw = data.decode("utf-8", "replace")
            hits = list(re.finditer(r'(?<=[="])-?\d+(?:\.\d+)?(?=["<,])', raw))
            if hits:
                m = rng.choice(hits)
                raw = raw[:m.start()] + rng.choice(["1e999", "-1e999", "nan", "inf", "-1", "99999999999999999999",
                                                    "0x10", "1e-999", ""]) + raw[m.end():]
                data = raw.encode("utf-8")
        elif op == "deep":
            raw = data.decode("utf-8", "replace")
            cut = raw.find(">", raw.find("<", 1) + 1) + 1
            if cut > 0:
                tag = rng.choice(["<NODE TYPE=\"FOLDER\" NAME=\"x\">", "<NODE Type=\"0\" Name=\"x\">", "<a>"])
                data = (raw[:cut] + tag * rng.choice([200, 3000, 30000]) + raw[cut:]).encode("utf-8")
    return data


# --------------------------------------------------------------
def sample_library(rng, folder):
    nodes = random_nodes(rng, folder)
    for t in {id(t): t for n in nodes for t in (n.tracks if not n.is_folder else [])}.values():
        t.path = os.path.join(folder, os.path.basename(t.path))
    return nodes


# --------------------------------------------------
def make_m3u(rng, folder):
    from djapp.export import m3u
    nodes = [n for n in sample_library(rng, folder) if not n.is_folder and n.tracks] or sample_library(rng, folder)[:1]
    node = next((n for n in nodes if not n.is_folder and n.tracks), None)
    if node is None:
        raise Skip("no playlist to write")
    path = os.path.join(folder, "list.m3u8")
    m3u.write_list(node, path, {}, rng.random() < 0.5)
    return path, [path]


def make_nml(rng, folder):
    from djapp.export import nml
    nml.export(sample_library(rng, folder), folder, copy=False)
    path = os.path.join(folder, "collection.nml")
    return path, [path]


# --------------------------------------------------
def make_rbxml(rng, folder):
    from djapp.export import rbxml
    rbxml.export(sample_library(rng, folder), folder, copy=False)
    path = os.path.join(folder, "rekordbox.xml")
    return path, [path]


# ------------------------------------------------------------
def make_vdj(rng, folder):
    songs, names = [], []
    for i in range(rng.randint(1, 6)):
        p = os.path.join(folder, f"music{i}.mp3")
        names.append(p)
        pois = "".join(
            f'<Poi Pos="{rng.uniform(0, 200):.3f}" Name="c{j}" Num="{j + 1}" Type="{rng.choice(["cue", "loop", "beatgrid"])}" '
            f'Size="{rng.choice([1, 4, 8])}" Color="{rng.choice(["#FF0000", "16711680", ""])}"/>' for j in range(rng.randint(0, 4)))
        songs.append(f'<Song FilePath="{p}" FileSize="{rng.randint(1, 10**7)}"><Tags Author="A{i}" Title="T{i}" '
                     f'Genre="House" Key="{rng.choice(["8A", "Am", "11B"])}" Stars="{rng.randint(0, 5)}"/>'
                     f'<Infos SongLength="{rng.uniform(60, 400):.2f}" Bitrate="320" PlayCount="{i}" FirstSeen="1700000000"/>'
                     f'<Scan Bpm="{rng.uniform(0.35, 0.6):.4f}" Phase="{rng.uniform(0, 0.5):.4f}"/>{pois}</Song>')
    db = os.path.join(folder, "database.xml")
    with open(db, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?><VirtualDJ_Database Version="8">' + "".join(songs)
                + "</VirtualDJ_Database>")
    os.makedirs(os.path.join(folder, "Playlists"))
    os.makedirs(os.path.join(folder, "Folders", "sub"))
    m3u_path = os.path.join(folder, "Playlists", "list.m3u")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n" + "\n".join(names[:3]) + "\n")
    vf = os.path.join(folder, "Folders", "sub", "mix.vdjfolder")
    with open(vf, "w", encoding="utf-8") as f:
        f.write("<VirtualDJ_VFolder>" + "".join(f'<song path="{p}" title="x" songlength="200"/>' for p in names) + "</VirtualDJ_VFolder>")
    return db, [db, m3u_path, vf]


def record(tag: str, payload: bytes) -> bytes:
    return tag.encode("latin-1") + struct.pack(">I", len(payload)) + payload


# ------------------------------------------------------------
def make_mixxx(rng, folder):
    path = os.path.join(folder, "mixxxdb.sqlite")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT, filesize INTEGER, fs_deleted INTEGER);
        CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, album TEXT, year TEXT, genre TEXT,
            comment TEXT, composer TEXT, bpm REAL, duration REAL, rating INTEGER, timesplayed INTEGER,
            datetime_added TEXT, bitrate INTEGER, samplerate INTEGER, key_id INTEGER, tracknumber TEXT,
            location INTEGER, mixxx_deleted INTEGER);
        CREATE TABLE cues (track_id INTEGER, type INTEGER, position REAL, length REAL, hotcue INTEGER, label TEXT);
        CREATE TABLE Playlists (id INTEGER PRIMARY KEY, name TEXT, hidden INTEGER, position INTEGER);
        CREATE TABLE PlaylistTracks (playlist_id INTEGER, track_id INTEGER, position INTEGER);
        CREATE TABLE crates (id INTEGER PRIMARY KEY, name TEXT, show INTEGER);
        CREATE TABLE crate_tracks (crate_id INTEGER, track_id INTEGER);
    """)
    count = rng.randint(1, 8)
    for i in range(1, count + 1):
        con.execute("INSERT INTO track_locations VALUES (?,?,?,0)", (i, os.path.join(folder, f"m{i}.mp3"), rng.randint(1, 10**7)))
        con.execute("INSERT INTO library VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (i, f"A{i}", f"T{i}", "Alb", "2020-01-01", "House", "", "", rng.uniform(80, 170), rng.uniform(60, 400),
                     rng.randint(0, 5), i, "2024-01-01 10:00:00", 320, 44100, rng.randint(0, 24), f"{i}/12", i))
        con.execute("INSERT INTO cues VALUES (?,?,?,?,?,?)", (i, rng.choice([1, 2, 4, 5]), rng.uniform(0, 5e6), rng.uniform(0, 1e6), rng.randint(-1, 7), "cue"))
        con.execute("INSERT INTO PlaylistTracks VALUES (1,?,?)", (i, i))
        con.execute("INSERT INTO crate_tracks VALUES (1,?)", (i,))
    con.execute("INSERT INTO Playlists VALUES (1,'Playlist',0,0)")
    con.execute("INSERT INTO crates VALUES (1,'Crate',1)")
    con.commit()
    con.close()
    return path, [path]


def id3_tag(rng) -> bytes:
    def frame(name, text):
        body = b"\x03" + text.encode("utf-8")
        return name + struct.pack(">I", len(body)) + b"\x00\x00" + body

    frames = b"".join(frame(n, t) for n, t in ((b"TIT2", "Title"), (b"TPE1", "Artist"), (b"TALB", "Album"),
                                                (b"TBPM", str(rng.randint(80, 170))), (b"TKEY", "Am"), (b"TCON", "(17)House")))
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 127, (size >> 14) & 127, (size >> 7) & 127, size & 127])
    return b"ID3\x03\x00\x00" + syncsafe + frames


# ------------------------------------------------------------
def flac_file(rng) -> bytes:
    info = bytes(34)
    comment = b"\x00\x00\x00\x00"
    comments = [b"TITLE=Title", b"ARTIST=Artist", b"BPM=" + str(rng.randint(80, 170)).encode()]
    vorbis = struct.pack("<I", 4) + b"tbfz" + struct.pack("<I", len(comments)) + b"".join(
        struct.pack("<I", len(c)) + c for c in comments)
    _ = comment
    head = b"fLaC" + b"\x00" + len(info).to_bytes(3, "big") + info
    return head + b"\x84" + len(vorbis).to_bytes(3, "big") + vorbis


def box(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + name + payload


# ----------------------------------------
def mp4_file(rng) -> bytes:
    data = box(b"data", struct.pack(">II", 1, 0) + b"Title")
    ilst = box(b"ilst", box(b"\xa9nam", data) + box(b"\xa9ART", box(b"data", struct.pack(">II", 1, 0) + b"Artist")))
    meta = box(b"meta", b"\x00\x00\x00\x00" + box(b"hdlr", bytes(24)) + ilst)
    return box(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42") + box(b"moov", box(b"udta", meta))


# ------------------------------------------------------------------
def make_folder(rng, folder):
    from . import synth
    music = os.path.join(folder, "music")
    os.makedirs(os.path.join(music, "sub"))
    paths = []
    p = os.path.join(music, "a.mp3")
    with open(p, "wb") as f:
        f.write(id3_tag(rng) + bytes(rng.getrandbits(8) for _ in range(300)))
    paths.append(p)
    p = os.path.join(music, "b.flac")
    with open(p, "wb") as f:
        f.write(flac_file(rng))
    paths.append(p)
    p = os.path.join(music, "sub", "c.m4a")
    with open(p, "wb") as f:
        f.write(mp4_file(rng))
    paths.append(p)
    p = os.path.join(music, "sub", "d.wav")
    synth.write_wav(__import__("pathlib").Path(p), 0.3, rng)
    paths.append(p)
    return music, paths


READERS = [("m3u", make_m3u, True), ("nml", make_nml, True), ("rbxml", make_rbxml, True),
           ("vdj", make_vdj, True), ("mixxx", make_mixxx, False),
           ("folder", make_folder, False)]


def documented_errors():
    from defusedxml.common import DefusedXmlException
    return (ValueError, OSError, ET.ParseError, sqlite3.Error, zipfile.BadZipFile, DefusedXmlException)


# ------------------------------------------------------------
def check_nodes(nodes) -> str:
    from djapp.model import Node, unique_tracks
    if not isinstance(nodes, list) or not all(isinstance(n, Node) for n in nodes):
        return "the result is not a list of Node"
    for t in unique_tracks(nodes):
        for name in ("duration", "bpm", "gain", "size", "bitrate", "samplerate", "rating", "plays", "year", "track_no",
                     "disc_no", "color"):
            v = getattr(t, name)
            if isinstance(v, float) and not math.isfinite(v):
                return f"track.{name} is {v}"
        for c in t.cues:
            if not (math.isfinite(c.start) and math.isfinite(c.length)):
                return f"a cue has start {c.start} and length {c.length}"
    return ""

# ------------------------------------------------------------
def readers(seed, iterations, deadline):
    sources = need("djapp.sources", "load")
    errors = documented_errors()
    failures = []
    with tempfile.TemporaryDirectory(prefix="tbfuzz-") as tmp:
        for case_seed, rng in cases(seed, iterations, deadline):
            kind, make, text = READERS[case_seed % len(READERS)]  # by absolute seed, so a replay keeps its kind
            folder = os.path.join(tmp, f"{kind}-{case_seed}")
            os.makedirs(folder)

            def fail(case, message, detail="", seed=case_seed):
                failures.append(Failure("readers", seed, case, message, detail))

            try:
                main, files = make(rng, folder)
            except Skip:
                continue
            except Exception as error:  # a sample that cannot be built is a test problem  # noqa: BLE001
                fail(kind, f"sample builder failed: {type(error).__name__}: {error}", traceback.format_exc())
                continue
            status, value, trace = guarded(lambda path=main: sources.load(path), 30)
            if status != "ok":
                fail(f"{kind} valide", f"valid {kind} sample not read: {status} {value}", trace)
                continue
            donors = []
            for _, other, other_text in READERS:
                if other_text == text and rng.random() < 0.5:
                    with contextlib.suppress(Exception):
                        donor_dir = os.path.join(tmp, f"donor-{case_seed}-{other.__name__}")
                        os.makedirs(donor_dir)
                        donors.append(read_file(other(random.Random(case_seed), donor_dir)[0]))
            targets = [p for p in files if os.path.isfile(p)]
            for victim in rng.sample(targets, k=min(len(targets), rng.randint(1, 2))):
                data = read_file(victim)
                with open(victim, "wb") as f:
                    f.write(mutate_bytes(rng, data, donors, text and victim.lower().endswith((".xml", ".nml", ".m3u8", ".m3u", ".vdjfolder"))))
            status, value, trace = guarded(lambda path=main: sources.load(path), 30)
            if status == "hang":
                fail(kind, f"{kind} reader does not return (30 s)")
            elif status == "error":
                if not isinstance(value, errors):
                    fail(kind, f"{kind}: {type(value).__name__}: {short(value, 120)}", trace)
            else:
                problem = check_nodes(value)
                if problem:
                    fail(kind, f"{kind}: {problem}")
    return failures


# ------------------------------------------------------------
# (c) cue editing
# ------------------------------------------------------------

# ------------------------------------------------------------------------
def cue_problem(t) -> str:
    slots = [c.hotcue for c in t.cues if c.hotcue >= 0]
    if len(slots) != len(set(slots)) or any(not 0 <= s <= 7 for s in slots):
        return f"hot slots {sorted(slots)}"
    limit = t.duration * 1000 + 0.1
    for c in t.cues:
        if c.length < 0 or (c.kind == 5 and c.length <= 0):
            return f"loop with length {c.length}"
        if not math.isfinite(c.start) or not 0 <= c.start <= limit:
            return f"start {c.start} outside the track"
    return ""


# one random edit, the way the editors do it. Returns its name
def edit_cues(rng: random.Random, t):
    from djapp.model import CUE, LOOP, Cue
    limit = t.duration * 1000
    op = rng.choice(["add", "add", "move", "delete", "loop", "hot", "clear", "bpm", "grid"])
    cues = t.cues
    if op == "add":
        free = [s for s in range(8) if s not in {c.hotcue for c in cues}]
        slot = rng.choice(free + [-1]) if free else -1
        cues.append(Cue(name="n", kind=CUE, start=round(rng.uniform(0, limit), 1), hotcue=slot))
    elif op == "move" and cues:
        c = rng.choice(cues)
        c.start = round(min(limit, max(0, c.start + rng.uniform(-500, 500))), 1)
    elif op == "delete" and cues:
        cues.remove(rng.choice(cues))
    elif op == "loop" and cues:
        c = rng.choice(cues)
        if c.kind in (CUE, LOOP):
            c.kind, c.length = LOOP, round(rng.uniform(10, 3000), 1)
    elif op == "hot" and cues:
        c = rng.choice([c for c in cues if c.hotcue < 0] or cues)
        free = [s for s in range(8) if s not in {x.hotcue for x in cues if x is not c}]
        if free:
            c.hotcue = rng.choice(free)
    elif op == "clear" and cues:
        rng.choice(cues).hotcue = -1
    elif op == "bpm":
        t.bpm = round(rng.uniform(80, 170), 2)
    elif op == "grid":
        t.grid = round(rng.uniform(0, 500), 1)
    return op


# --------------------------------------------
# one random session of edits on a few tracks, checked against a model of the history
def cue_case(case_seed, rng, history):
    from djapp.model import Track
    failures = []
    many = rng.random() < 0.15
    tracks = [Track(title=f"t{i}", duration=rng.uniform(5, 300), bpm=120.0, grid=0.0)
              for i in range(rng.randint(history.TRACKS + 2, history.TRACKS + 8) if many else rng.randint(1, 3))]
    past = {id(t): [] for t in tracks}
    future = {id(t): [] for t in tracks}
    lru: list[int] = []

    def touch(t):
        if id(t) in lru:
            lru.remove(id(t))
        lru.append(id(t))
        while len(lru) > history.TRACKS:
            gone = lru.pop(0)
            past[gone].clear()
            future[gone].clear()

    def fail(message):
        failures.append(Failure("cues", case_seed, "historique", message))

    def values(t):
        return [(c.name, c.kind, c.start, c.length, c.hotcue, c.color) for c in t.cues]

    for step in range(rng.randint(5, 150 if rng.random() < 0.2 else 40)):
        t = rng.choice(tracks)
        action = rng.choice(["edit", "edit", "edit", "undo", "redo"])
        if action == "edit":
            before = history.snap(t)
            use_mark = rng.random() < 0.3
            if use_mark:
                history.mark(t)
                touch(t)
                if not past[id(t)] or past[id(t)][-1] != before:
                    past[id(t)].append(before)
                    del past[id(t)][:-history.DEPTH]
                future[id(t)].clear()
            op = edit_cues(rng, t)
            if not use_mark:
                history.record(t, before)
                if before != history.snap(t):
                    touch(t)
                    past[id(t)].append(before)
                    del past[id(t)][:-history.DEPTH]
                    future[id(t)].clear()
        else:
            op = action
            touch(t)
            now = history.snap(t)
            got = history.undo(t) if action == "undo" else history.redo(t)
            src, dst = (past, future) if action == "undo" else (future, past)
            expected = bool(src[id(t)])
            if got != expected:
                fail(f"step {step} {action}: returned {got}, expected {expected}")
                break
            if expected:
                dst[id(t)].append(now)
                want = src[id(t)].pop()
                if history.snap(t) != want:
                    fail(f"step {step} {action}: snapshot differs from the one recorded")
                    break
        problem = cue_problem(t)
        if problem:
            fail(f"step {step} after {op}: {problem}")
            break
        # undo then redo must give the same list back
        if past[id(t)] and action == "edit":
            after = history.snap(t)
            kept = values(t)
            ran = history.undo(t)
            touch(t)
            if not ran or not history.redo(t) or history.snap(t) != after:
                fail(f"step {step} after {op}: undo then redo does not restore the list")
                break
            if values(t) != kept:
                fail(f"step {step} after {op}: cue values changed by undo and redo")
                break
    return failures

# ------------------------------------------------------------
def cue_history(seed, iterations, deadline):
    history = need("djapp.ui.cuehistory", "record", "mark", "undo", "redo", "snap", "DEPTH", "TRACKS")
    failures = []
    for case_seed, rng in cases(seed, iterations, deadline):
        history._hist.clear()
        history._edits.clear()
        failures += cue_case(case_seed, rng, history)
    history._hist.clear()
    history._edits.clear()
    return failures


# ------------------------------------------------------------
# (d) CDJ writer and validators
# ------------------------------------------------------------

# ------------------------------------------------------------
# digests of what the writer produces for each case, and the problems the validators found
def cdj_cases(seed, iterations, deadline):
    pdbwrite = need("djapp.export.cdj.pdbwrite", "write", "Item")
    anlz = need("djapp.export.cdj.anlz", "build")
    analysis = need("djapp.analysis", "Audio")
    validate = need("djapp.export.cdj.validate", "pdb", "anlz")
    from pathlib import Path

    from djapp.model import unique_tracks

    from . import synth
    digests, problems = {}, []
    with tempfile.TemporaryDirectory(prefix="tbfuzz-") as tmp:
        wav = Path(tmp) / "a.wav"
        synth.write_wav(wav, 1.0, random.Random(0))
        audio = analysis.Audio(str(wav))
        for case_seed, rng in cases(seed, iterations, deadline):
            nodes = random_nodes(rng, tmp, controls=rng.random() < 0.2, path=str(wav))
            items = []
            for i, t in enumerate(unique_tracks(nodes), 1):
                if t.bpm > 655.35:
                    t.bpm = 200.0
                items.append(pdbwrite.Item(t, i, f"/Contents/Artist/Album/track{i}.wav",
                                           f"/PIONEER/USBANLZ/P{i:03X}/{i:08X}/ANLZ0000.DAT", rng.randint(1000, 10**7)))
            h = hashlib.sha256()
            try:
                out = os.path.join(tmp, f"{case_seed}.pdb")
                pdbwrite.write(items, nodes, out, today="2026-01-01")
                data = read_file(out)
                validate.pdb(data)
                h.update(data)
                for it in items:
                    dat, ext = anlz.build(it.track, it.path, audio)
                    validate.anlz(dat, it.path, "DAT")
                    validate.anlz(ext, it.path, "EXT")
                    h.update(dat)
                    h.update(ext)
            except Exception as error:  # the writer must produce what its own validators accept  # noqa: BLE001
                problems.append([case_seed, f"{type(error).__name__}: {error}", traceback.format_exc()])
                continue
            digests[str(case_seed)] = h.hexdigest()
    return digests, problems


# ------------------------------------------------------------
def cdj_writer(seed, iterations, deadline):
    native = need("djapp.native")
    digests, problems = cdj_cases(seed, iterations, deadline)
    failures = [Failure("cdj", s, "ecriture", m, d) for s, m, d in problems]
    if not digests:
        return failures
    left = max(10.0, deadline - time.monotonic()) if deadline != math.inf else 600.0
    code = (f"import json, sys, time\nsys.path[:0] = [{str(ROOT)!r}, {str(APP_ROOT)!r}]\nfrom tests.center import fuzz\n"
            f"print('@@' + json.dumps(fuzz.cdj_cases({seed}, {iterations}, time.monotonic() + {left})))")
    env = {**os.environ, "BB_PURE_PYTHON": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=left + 60, check=False,
                             cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        failures.append(Failure("cdj", seed, "pur Python", "the pure Python run does not finish"))
        return failures
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("@@")), "")
    if not line:
        failures.append(Failure("cdj", seed, "pur Python", f"no result from the pure Python run: {short(run.stderr, 300)}"))
        return failures
    pure, pure_problems = json.loads(line[2:])
    failures += [Failure("cdj", s, "pur Python", m, d) for s, m, d in pure_problems]
    for case, digest in digests.items():
        if case in pure and pure[case] != digest:
            kind = "native core and pure Python differ" if native.available() else "two runs of pure Python differ"
            failures.append(Failure("cdj", int(case), "comparaison", f"{kind} (files of case {case})"))
    return failures



# ------------------------------------------------------------
# (f) hostile backup archives
# ------------------------------------------------------------

TRICKS = [
    ("traversal", True, lambda: ("../evil.txt", b"x")),
    ("traversal2", True, lambda: ("Audio/../../evil.txt", b"x")),
    ("backslash", True, lambda: ("..\\evil.txt", b"x")),
    ("absolute", True, lambda: ("/abs.txt", b"x")),
    ("drive", True, lambda: ("C:/evil.txt", b"x")),
    ("device", True, lambda: ("Audio/CON", b"x")),
    ("device2", True, lambda: ("Audio/nul.wav", b"x")),
    ("colon", True, lambda: ("Audio/a:stream", b"x")),
    ("trailing", True, lambda: ("Audio/name.", b"x")),
    ("directory", True, lambda: ("Audio/", b"")),
    ("duplicate", True, lambda: ("project.json", b"{}")),
    ("case", True, lambda: ("PROJECT.JSON", b"{}")),
    ("deep", True, lambda: ("Audio/a/b/c.wav", b"x")),
    ("unexpected", True, lambda: ("notes.txt", b"x")),
    ("symlink", True, None),
    ("bomb", True, None),
    ("encrypted", True, None),
    ("huge", True, None),
    ("no_project", False, None),
    ("bad_project", False, None),
    ("bad_manifest", False, None),
    ("corrupt", False, None),
]


# ------------------------------------------------------------
def listing(root: str) -> set[str]:
    out = set()
    for d, dirs, files in os.walk(root):
        out.update(os.path.join(d, x) for x in dirs + files)
    return out


# ----------------------------------------
# rewrites a field of one entry in both its central and its local header
def patch_headers(raw: bytes, entry: str, offset_central: int, offset_local: int, value: bytes) -> bytes:
    data = bytearray(raw)
    name = entry.encode()
    pos = data.find(b"PK\x01\x02")
    while pos >= 0:
        n = struct.unpack_from("<H", data, pos + 28)[0]
        if bytes(data[pos + 46:pos + 46 + n]) == name:
            data[pos + offset_central:pos + offset_central + len(value)] = value
            local = struct.unpack_from("<L", data, pos + 42)[0]
            if data[local:local + 4] == b"PK\x03\x04":
                data[local + offset_local:local + offset_local + len(value)] = value
        pos = data.find(b"PK\x01\x02", pos + 4)
    return bytes(data)


# ------------------------------------------------------------
def zip_backup(seed, iterations, deadline):
    backup = need("djapp.backup", "restore", "create")
    project = need("djapp.sources.project", "dump")
    from djapp.model import Node, Track
    failures = []
    refused = (ValueError, OSError, zipfile.BadZipFile)
    with tempfile.TemporaryDirectory(prefix="tbfuzz-") as tmp:
        audio = os.path.join(tmp, "src.wav")
        with open(audio, "wb") as f:
            f.write(b"RIFFdata" * 40)
        doc = project.dump([Node("playlist", "p", tracks=[Track(title="t", path=audio, duration=3.0)])], {})
        valid = os.path.join(tmp, "valid.zip")
        backup.create(valid, doc)
        with zipfile.ZipFile(valid) as z:
            entries = {n: z.read(n) for n in z.namelist()}
        audio_name = next(n for n in entries if n.startswith("Audio/"))
        try:
            probe = os.path.join(tmp, "probe")
            shutil.copy(valid, probe + ".zip")
            shutil.copy(valid + ".sha256", probe + ".zip.sha256")
            with open(probe + ".zip.sha256", "w") as f:
                f.write(f"{hashlib.sha256(read_file(valid)).hexdigest()} *probe.zip\n")
            backup.restore(probe + ".zip", probe + "-out")
        except OSError as error:
            if "free space" in str(error):
                raise Skip("not enough free disk space for the restore check") from error
            failures.append(Failure("backup", seed, "controle", f"valid backup not restored: {error}"))
            return failures
        except Exception as error:  # noqa: BLE001
            failures.append(Failure("backup", seed, "controle", f"valid backup not restored: {error}"))
            return failures

        for case_seed, rng in cases(seed, iterations, deadline):
            case = os.path.join(tmp, f"case{case_seed}")
            os.makedirs(case)
            canary = os.path.join(case, "canary.txt")
            with open(canary, "w") as f:
                f.write("keep")
            archive = os.path.join(case, "b.zip")
            target = os.path.join(case, "restored")
            chosen = rng.sample(TRICKS, rng.randint(1, 3))
            hostile = any(t[1] for t in chosen)
            files = dict(entries)
            extra = []
            patches = []
            for name, _must, make in chosen:
                if make is not None:
                    n, content = make()
                    extra.append((n, content, 0))
                elif name == "symlink":
                    extra.append(("Audio/link.wav", b"/etc/passwd", 0o120777 << 16))
                elif name == "bomb":
                    extra.append(("Audio/bomb.bin", bytes(3_000_000), 0))
                elif name == "encrypted":
                    patches.append(("encrypted", audio_name))
                elif name == "huge":
                    patches.append(("huge", audio_name))
                elif name == "no_project":
                    files.pop("project.json", None)
                elif name == "bad_project":
                    files["project.json"] = rng.choice([b"[]", b"{", b'{"format": "x"}', b'{"settings": {}, "tracks": 5}', b"\xff\xfe"])
                elif name == "bad_manifest":
                    files["manifest.json"] = rng.choice([b"[]", b"{}", b'{"sha256": 3}', b'{"sha256": {"Audio/x": "zz"}}'])
            with warnings.catch_warnings(), zipfile.ZipFile(archive, "w") as z:
                warnings.simplefilter("ignore", UserWarning)  # a duplicate name is the point of some cases
                for n, content in files.items():
                    z.writestr(n, content)
                for n, content, attr in extra:
                    info = zipfile.ZipInfo("placeholder")
                    info.filename = info.orig_filename = n
                    info.external_attr = attr
                    z.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED if n.endswith("bomb.bin") else zipfile.ZIP_STORED)
            raw = read_file(archive)
            for kind, entry in patches:
                if kind == "encrypted":
                    raw = patch_headers(raw, entry, 8, 6, b"\x01\x00")
                else:
                    raw = patch_headers(raw, entry, 24, 22, struct.pack("<L", rng.choice([0xFFFFFFFF, 1 << 31, 10**9])))
            if any(c[0] == "corrupt" for c in chosen):
                ba = bytearray(raw)
                for _ in range(rng.randint(1, 8)):
                    ba[rng.randrange(len(ba))] ^= 1 << rng.randrange(8)
                raw = bytes(ba)
            with open(archive, "wb") as f:
                f.write(raw)
            with open(archive + ".sha256", "w") as f:
                f.write(f"{hashlib.sha256(raw).hexdigest()} *b.zip\n")
            before = listing(tmp)
            label = "+".join(c[0] for c in chosen)
            status, value, trace = guarded(lambda a=archive, t=target: backup.restore(a, t), 60)
            after = listing(tmp)
            fresh = {p for p in after - before if not p.startswith(target)}
            if fresh:
                failures.append(Failure("backup", case_seed, label,
                                        f"files written outside the target: {sorted(fresh)[:3]}"))
            if status == "hang":
                failures.append(Failure("backup", case_seed, label, "restore does not return (60 s)"))
            elif status == "error":
                if not isinstance(value, refused):
                    failures.append(Failure("backup", case_seed, label, f"{type(value).__name__}: {short(value, 120)}", trace))
                elif os.path.exists(target) or any(".bb-restore-" in p for p in after):
                    failures.append(Failure("backup", case_seed, label, "a refused restore left files behind"))
            elif hostile:
                failures.append(Failure("backup", case_seed, label, "a hostile archive was accepted"))
            if read_file(canary) != b"keep":
                failures.append(Failure("backup", case_seed, label, "canary file changed"))
            shutil.rmtree(case, ignore_errors=True)
    return failures


# ----------------------------------------------------------
# (e2) Backup of the analysis cache: hostile archives
# ----------------------------------------------------------

HEX = "0123456789abcdef"


# ------------------------------------------------------------
# restore and verify of a cache backup that was changed: damaged bytes, names that try to leave
# the cache, links, encrypted entries. Nothing may be written outside the cache folder, a refused
# restore leaves nothing behind, and the only error is ValueError
def cache_zip(seed, iterations, deadline):
    backup = need("djapp.cachebackup", "restore", "create", "verify", "MANIFEST")
    cuts = need("tests.test_key_fuzz", "mutate")
    from pathlib import Path
    failures = []
    with tempfile.TemporaryDirectory(prefix="tbfuzz-") as tmp:
        tmp = Path(tmp)
        source = tmp / "source"
        entries = {}
        for sub_name, ext in (("anlz", "DAT"), ("anlz", "EXT"), ("wave", "bin"), ("wave", "hi1000")):
            (source / sub_name).mkdir(parents=True, exist_ok=True)
            for k in range(3):
                name = hashlib.sha1(f"{sub_name}{ext}{k}".encode()).hexdigest() + "." + ext
                data = bytes((k * 7 + i * 13) % 251 for i in range(300 + k))
                (source / sub_name / name).write_bytes(data)
                entries[f"{sub_name}/{name}"] = data
        valid = tmp / "valid.zip"
        backup.create(source, valid)
        good = valid.read_bytes()
        with zipfile.ZipFile(valid) as z:
            note = json.loads(z.read(backup.MANIFEST))
        key40 = hashlib.sha1(b"x").hexdigest()
        bad_names = [f"../{key40}.DAT", f"anlz/../{key40}.DAT", f"anlz/{key40}.DAT/../x", f"/{key40}.DAT", f"C:/{key40}.DAT",
                     f"anlz\\{key40}.DAT", f"anlz/{key40[:39]}.DAT", f"anlz/{key40}.dat", f"anlz/{key40}.DAT.exe",
                     "anlz/", f"anlz/sub/{key40}.DAT", f"anlz/{key40}.DAT\x00.exe", "evil/" + key40 + ".DAT", "anlz//" + key40 + ".DAT",
                     f"wave/{key40}.DAT", "anlz/CON.DAT", "." + "/" + f"anlz/{key40}.DAT", next(iter(entries))]
        for case_seed, rng in cases(seed, iterations, deadline):
            case = tmp / f"c{case_seed}"
            case.mkdir()
            target = case / "cache"
            canary = case / "canary"
            canary.write_bytes(b"keep")
            archive = case / "b.zip"
            hostile = False
            mode = rng.choice(["bytes", "bytes", "names", "names", "flags"])
            if mode == "bytes":
                label, raw = cuts.mutate(good, rng)
                label = "bytes " + label
            else:
                files = dict(entries)
                chosen = rng.sample(bad_names, rng.randint(1, 3))
                label = mode + " " + "+".join(short(c, 24) for c in chosen)
                hostile = True
                buf = io.BytesIO()
                with warnings.catch_warnings(), zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    warnings.simplefilter("ignore", UserWarning)
                    for n, content in files.items():
                        z.writestr(n, content)
                    for n in chosen:
                        info = zipfile.ZipInfo("placeholder")
                        info.filename = info.orig_filename = n
                        if mode == "flags":
                            info.external_attr = rng.choice([0o120777 << 16, 0o040755 << 16, 0o100000 << 16 | 1])
                        z.writestr(info, b"data")
                    z.writestr(backup.MANIFEST, json.dumps({**note, "files": len(files) + len(chosen)}))
                raw = buf.getvalue()
                if mode == "flags":
                    raw = raw.replace(b"PK\x03\x04\x14\x00\x00\x00", b"PK\x03\x04\x14\x00\x01\x00", rng.randint(1, 2))
                    raw = raw.replace(b"PK\x01\x02\x14\x03\x14\x00\x00\x00", b"PK\x01\x02\x14\x03\x14\x00\x01\x00", rng.randint(1, 2))
            archive.write_bytes(raw)
            before = listing(str(tmp))
            status, value, trace = guarded(lambda a=archive, t=target: backup.restore(a, t), 60)
            after = listing(str(tmp))
            fresh = {x for x in after - before if not x.startswith(str(target))}
            if fresh:
                failures.append(Failure("cachezip", case_seed, label, f"files written outside the cache: {sorted(fresh)[:3]}"))
            if status == "hang":
                failures.append(Failure("cachezip", case_seed, label, "restore does not return (60 s)"))
            elif status == "error":
                if not isinstance(value, ValueError):
                    failures.append(Failure("cachezip", case_seed, label, f"{type(value).__name__}: {short(value, 120)}", trace))
                elif any(".bb-restore-" in x for x in after) or (target.exists() and any(target.rglob("*.*"))):
                    failures.append(Failure("cachezip", case_seed, label, "a refused restore left files behind"))
            elif hostile:
                failures.append(Failure("cachezip", case_seed, label, "a hostile archive was accepted"))
            status, value, trace = guarded(lambda a=archive: backup.verify(a), 60)
            if status == "hang":
                failures.append(Failure("cachezip", case_seed, label, "verify does not return (60 s)"))
            elif status == "error" and not isinstance(value, ValueError):
                failures.append(Failure("cachezip", case_seed, label, f"verify: {type(value).__name__}: {short(value, 120)}", trace))
            if canary.read_bytes() != b"keep":
                failures.append(Failure("cachezip", case_seed, label, "canary file changed"))
            shutil.rmtree(case, ignore_errors=True)
    return failures


# ------------------------------------------------------------
# (f) CDJ/USB key: export, read back, merge, damaged files
# ----------------------------------------------------------------

KEY_GROUP = 50  # cases that share one pair of exports, so a run does a handful of them


# ----------------------------------------
# {folder path and name without case: the tracks of that playlist as the key keeps them}
def playlist_views(sup, nodes, prefix=""):
    out = {}
    for n in nodes:
        if n.is_folder:
            out.update(playlist_views(sup, n.children, f"{prefix}{n.name.casefold()} / "))
        else:
            out[prefix + n.name.casefold()] = json.loads(json.dumps([sup.track_view(t) for t in n.tracks]))
    return out


PALETTE = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
           "white": (255, 255, 255), "black": (0, 0, 0)}


# gives most of the synthetic FLAC files a flat colour as embedded picture, some stay
# without one. Without the tag witness library or Pillow the files stay as they are
def real_files(tracks, rng):
    try:
        from PIL import Image

        from tests.witness import FLAC, Picture
    except ImportError:
        return
    for t in tracks:
        flac = t.path
        if rng.random() < 0.3:
            continue
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), PALETTE[rng.choice(list(PALETTE))]).save(buf, "JPEG")
        audio = FLAC(flac)
        pic = Picture()
        pic.data, pic.type, pic.mime = buf.getvalue(), 3, "image/jpeg"
        audio.add_picture(pic)
        audio.save()

def nearest(pixel):
    return min(PALETTE, key=lambda c: sum((a - b) ** 2 for a, b in zip(PALETTE[c], pixel, strict=True)))


# ------------------------------------------------------------
# on a key just written or merged: a track has a picture number when its audio has a picture,
# that picture is the colour of the one in the audio, and no picture is left that nobody uses
def picture_problems(key, out):
    from pathlib import Path

    from djapp import tags
    from djapp.export.cdj import usb
    from djapp.model import playlists
    from PIL import Image
    data = key.read(str(out))
    problems, used = [], set()
    folder = Path(out) / "PIONEER" / "Artwork" / "00001"
    for pl in playlists(data.nodes):
        for t in pl.tracks:
            embedded = tags.artwork(t.path)
            number = data.art.get(key.key_path(str(out), t.path).lower(), 0)
            if embedded is None:
                if number:
                    problems.append(("pochettes", f"{os.path.basename(t.path)} has no picture but number {number}"))
                continue
            if not number:
                problems.append(("pochettes", f"{os.path.basename(t.path)} has a picture and no number"))
                continue
            used.add(number)
            want = nearest(Image.open(io.BytesIO(embedded)).convert("RGB").getpixel((40, 40)))
            for name in (f"a{number}.jpg", f"a{number}_m.jpg"):
                if not (folder / name).is_file():
                    problems.append(("pochettes", f"{name} is missing"))
                    continue
                shown = nearest(Image.open(folder / name).convert("RGB").getpixel((5, 5)))
                if shown != want:
                    problems.append(("pochettes", f"{os.path.basename(t.path)} shows {shown}, its audio has {want}"))
    left = usb.art_numbers(out) - used
    if left:
        problems.append(("pochettes", f"pictures nobody uses are left on the key: {sorted(left)[:3]}"))
    return problems[:3]


# --------------------------------------------------
# two random libraries exported into one key, the second in keep mode, with what must hold
# after each step. Returns (problems, key folder, nodes of the merged key)
def key_group(sup, key, root, base):
    from pathlib import Path

    from djapp.model import playlists, unique_tracks
    folder = Path(root) / f"g{base}"
    music, cache, out = folder / "music", folder / "cache", folder / "key"
    for d in (music, cache):
        d.mkdir(parents=True)
    problems = []
    lib_a, lib_b = sup.random_library(base, music, 4), sup.random_library(base + 1, music, 4)
    real_files(unique_tracks([*lib_a, *lib_b]), random.Random(base + 7))
    # one playlist of B gets the name of one of A in other letters, so a replacement happens
    tops_a = [n for n in lib_a if not n.is_folder]
    tops_b = [n for n in lib_b if not n.is_folder]
    used = {n.name.casefold() for n in lib_b}
    if tops_a and tops_b and tops_a[0].name.swapcase().casefold() not in used - {tops_b[0].name.casefold()}:
        tops_b[0].name = tops_a[0].name.swapcase()

    def json_view(nodes):
        return json.loads(json.dumps(sup.tree_view(nodes)))

    rep = sup.export_to(lib_a, out, cache)
    if rep.errors:
        return [("export", f"the export of the first library fails: {short(str(rep.errors[0]), 120)}")], out, None
    got = key.read(str(out))
    found = same_json(json_view(lib_a), json_view(got.nodes))
    if found:
        problems.append(("aller-retour", f"round trip differs at {found}"))
    if got.warnings:
        problems.append(("aller-retour", f"warnings on a key just written: {short(got.warnings[0], 120)}"))

    problems += picture_problems(key, out)
    numbers = dict(got.art)
    # the two analysis files of a track with their bytes and time, a copy from the cache has the bytes only
    def stamped(a):
        files = [Path(out) / a.lstrip("/").replace("DAT", ext) for ext in ("DAT", "EXT")]
        return [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]

    analyses = {u: stamped(a) for u, a in got.anlz.items()}
    rep = sup.export_to(lib_b, out, cache)
    if rep.errors:
        return [*problems, ("fusion", f"the keep export fails: {short(str(rep.errors[0]), 120)}")], out, None
    merged = key.read(str(out))
    # a track that is only on the key keeps its picture number
    renewed = {os.path.basename(t.path) for t in unique_tracks(lib_b)}
    for usb_path, number in numbers.items():
        if os.path.basename(usb_path) not in {n.lower() for n in renewed} and usb_path in merged.art \
                and merged.art[usb_path] != number:
            problems.append(("pochettes", f"{usb_path} had picture {number}, it has {merged.art[usb_path]} after the merge"))
            break
    # ... and its analysis files, which are not written again
    for usb_path, was in analyses.items():
        if os.path.basename(usb_path) in {n.lower() for n in renewed} or usb_path not in merged.anlz:
            continue
        if stamped(merged.anlz[usb_path]) != was:
            problems.append(("analyses", f"{usb_path} was analysed again by a merge that did not touch it"))
            break
    if merged.warnings:
        problems.append(("fusion", f"warnings on a merged key: {short(merged.warnings[0], 120)}"))
    views_a, views_b, views = (playlist_views(sup, n) for n in (lib_a, lib_b, merged.nodes))
    want = {**views_a, **views_b}
    if set(views) != set(want):
        missing, extra = sorted(set(want) - set(views))[:2], sorted(set(views) - set(want))[:2]
        problems.append(("fusion", f"playlists missing {missing}, unexpected {extra}"))
    elif len(sup.names_of(merged.nodes)) != len(views):
        problems.append(("fusion", "a playlist is on the key twice"))
    else:
        for name, view in want.items():
            found = same_json(view, views[name], name)
            if found:
                problems.append(("fusion", f"a playlist is not as it was exported: {found}"))
                break
    replaced = len(set(views_a) & set(views_b))
    if len(rep.replaced) != replaced:
        problems.append(("fusion", f"{len(rep.replaced)} playlists replaced, {replaced} share a name"))
    by_path: dict[str, set[int]] = {}
    for pl in playlists(merged.nodes):
        for t in pl.tracks:
            by_path.setdefault(t.path.lower(), set()).add(id(t))
    if any(len(ids) > 1 for ids in by_path.values()):
        problems.append(("fusion", "a track is on the key twice"))
    problems += name_collisions(sup, key, folder, out, cache, base)
    problems += picture_problems(key, out)
    return problems, out, key.read(str(out)).nodes

# seeds of tests/test_key_support.make_track that give FLAC files of the same size
SAME_SIZE = [(2, 12), (18, 73), (42, 52)]

# two different songs that ask for the same place on the key (same artist, no album, same
# file name), sometimes with the same size, then the first again from another folder. Each
# playlist must play its own audio, and a song already on the key keeps its file
def name_collisions(sup, key, folder, out, cache, base):
    import hashlib
    import random
    from pathlib import Path

    from djapp.model import Node, playlists
    rng = random.Random(base)
    name = f"dup{base}"
    first = rng.randrange(1, 40)
    other = first + rng.choice((1, 2, 3, 4, 6, 7))
    if rng.random() < 0.5:
        first, other = rng.choice(SAME_SIZE)  # two different songs, FLAC files of the same size
    sources = {}
    for label, seed in (("a", first), ("b", other), ("c", first)):
        d = Path(folder) / f"col{label}"
        d.mkdir()
        sources[label] = sup.make_track(d, name, seed=seed, artist="Same", album="")
    real_files([sources["a"], sources["b"]], rng)
    again = Path(folder) / "colc" / Path(sources["a"].path).name  # the same file, another folder
    shutil.copyfile(sources["a"].path, again)
    sources["c"].path = str(again)

    def sound(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    problems = []
    for label in "abc":
        rep = sup.export_to([Node("playlist", f"{name} {label}", tracks=[sources[label]])], out, cache)
        if rep.errors:
            return [*problems, ("collision", f"the export {label} fails: {short(str(rep.errors[0]), 120)}")]
    played = {pl.name: pl.tracks[0].path for pl in playlists(key.read(str(out)).nodes) if pl.name.startswith(name)}
    for label in "abc":
        path = played.get(f"{name} {label}")
        if path is None:
            problems.append(("collision", f"the playlist {label} is missing"))
        elif sound(path) != sound(sources[label].path):
            problems.append(("collision", f"the playlist {label} plays another song"))
    if played.get(f"{name} a") and played.get(f"{name} b") and played[f"{name} a"].lower() == played[f"{name} b"].lower():
        problems.append(("collision", "two different songs share one file on the key"))
    if played.get(f"{name} a") and played.get(f"{name} c") and played[f"{name} a"].lower() != played[f"{name} c"].lower():
        problems.append(("collision", "the same song was copied twice"))
    return problems


# ------------------------------------------------------------
def key_usb(seed, iterations, deadline):
    sup = need("tests.test_key_support", "random_library", "export_to", "tree_view", "track_view", "names_of")
    cuts = need("tests.test_key_fuzz", "mutate", "pdb_mutate")
    key = need("djapp.sources.key", "read")
    anlzread = need("djapp.export.cdj.anlzread", "parse", "AnlzError")
    need("djapp.export.cdj.keymerge", "merge")
    failures = []
    groups = {}
    with tempfile.TemporaryDirectory(prefix="tbfuzz-") as tmp:
        for case_seed, rng in cases(seed, iterations, deadline):
            base = case_seed - case_seed % KEY_GROUP
            if base not in groups:
                try:
                    problems, out, nodes = key_group(sup, key, tmp, base)
                except Exception as error:  # a key just written must be readable  # noqa: BLE001
                    problems, out, nodes = [("export", f"{type(error).__name__}: {short(str(error), 120)}")], None, None
                    failures.append(Failure("key", base, "export", problems[0][1], traceback.format_exc()))
                    problems = []
                failures += [Failure("key", base, case, message) for case, message in problems]
                good_pdb = (out / "PIONEER" / "rekordbox" / "export.pdb").read_bytes() if nodes is not None else b""
                anlz_files = [(p, p.read_bytes()) for p in sorted((out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.*"))]                     if nodes is not None else []
                groups[base] = (out, nodes, good_pdb, anlz_files)
            out, nodes, good_pdb, anlz_files = groups[base]
            if nodes is None or not anlz_files:
                continue
            pdb = out / "PIONEER" / "rekordbox" / "export.pdb"
            what = rng.choice(["export.pdb", "export.pdb", "ANLZ file", "ANLZ on the key"])
            path, data = rng.choice(anlz_files)
            if what == "export.pdb":
                label, bad = cuts.pdb_mutate(good_pdb, rng)
            else:
                label, bad = cuts.mutate(data, rng, zone=rng.choice((0, 0, 400)))
            case = f"{what} {label}"
            if what == "ANLZ file":
                status, value, trace = guarded(lambda b=bad: anlzread.parse(b), 20)
                allowed = anlzread.AnlzError
            else:
                target = pdb if what == "export.pdb" else path
                target.write_bytes(bad)
                try:
                    status, value, trace = guarded(lambda folder=out: key.read(str(folder)), 20)
                finally:
                    target.write_bytes(good_pdb if what == "export.pdb" else data)
                allowed = ValueError if what == "export.pdb" else ()
            if status == "hang":
                failures.append(Failure("key", case_seed, case, "the reader does not return (20 s)"))
            elif status == "error":
                if not isinstance(value, allowed):
                    failures.append(Failure("key", case_seed, case, f"{type(value).__name__}: {short(value, 120)}", trace))
            elif what != "ANLZ file":
                problem = check_nodes(value.nodes)
                if not problem and what == "ANLZ on the key" and len(value.nodes) != len(nodes):
                    problem = "a damaged analysis file changed the playlists"
                if problem:
                    failures.append(Failure("key", case_seed, case, problem))
    return failures


# ------------------------------------------------------------
# registry and runner
# ------------------------------------------------------------

SCENARIOS = [
    ("project", "Projet JSON : aller-retour et mutations hostiles", project_json),
    ("readers", "Lecteurs M3U, NML, rekordbox, VirtualDJ, Mixxx, dossier : fichiers abîmés", readers),
    ("cues", "Cues : ajout, déplacement, boucles, hot cues, Undo et Redo", cue_history),
    ("cdj", "Écriture CDJ : validateurs et comparaison pur Python", cdj_writer),
    ("backup", "Restauration de backup : archives ZIP hostiles", zip_backup),
    ("cachezip", "Sauvegarde du cache d'analyses : archives ZIP hostiles", cache_zip),
    ("key", "Clé USB CDJ : aller-retour, fusion et fichiers abîmés", key_usb),
]

# ----------------------------------------
# for scenarios that live in another module (a test file, the key reader later on)
def register(name: str, title: str, fn):
    SCENARIOS[:] = [s for s in SCENARIOS if s[0] != name]
    SCENARIOS.append((name, title, fn))


def names() -> list[str]:
    return [s[0] for s in SCENARIOS]

# ------------------------------------------------------------
# runs the scenarios in turn and returns the full record (see save_run)
def run(wanted: list[str], seed: int, iterations: int, budget: float = 0.0, log=None) -> dict:
    log = log or (lambda text: None)
    chosen = [s for s in SCENARIOS if "all" in wanted or s[0] in wanted]
    unknown = [w for w in wanted if w != "all" and w not in names()]
    if unknown:
        raise ValueError("unknown scenario: " + ", ".join(unknown) + " (known: " + ", ".join(names()) + ")")
    deadline = time.monotonic() + budget if budget else math.inf
    results = []
    for name, title, fn in chosen:
        _cases_done["n"] = 0
        started = time.monotonic()
        log(f"== {name} : {title}")
        entry = {"name": name, "title": title, "status": "ok", "message": "", "failures": [], "cases": 0, "seconds": 0.0}
        try:
            found = fn(seed, iterations, deadline)
        except Skip as skip:
            entry.update(status="saute", message=str(skip))
            found = []
        except ImportError as error:
            entry.update(status="saute", message=f"module absent : {error}")
            found = []
        except AttributeError as error:
            # a function of the application that another version does not have yet
            if "has no attribute" not in str(error):
                raise
            entry.update(status="saute", message=f"fonction absente : {error}")
            found = []
        except Exception as error:  # a broken scenario is reported, the others still run  # noqa: BLE001
            entry.update(status="erreur", message=f"{type(error).__name__}: {error}")
            entry["trace"] = traceback.format_exc()
            found = []
        entry["cases"] = _cases_done["n"]
        entry["seconds"] = round(time.monotonic() - started, 2)
        seen: dict[str, dict] = {}
        for f in found:
            sig = f.signature()
            if sig in seen:
                seen[sig]["count"] += 1
                continue
            seen[sig] = {**asdict(f), "count": 1, "replay": f.replay(), "signature": sig}
        entry["failures"] = list(seen.values())
        if entry["failures"] and entry["status"] == "ok":
            entry["status"] = "echec"
        for f in entry["failures"]:
            log(f"   ECHEC seed {f['seed']} ({f['case']}) x{f['count']} : {f['message']}")
            log(f"   rejouer : {f['replay']}")
        log(f"   {entry['status']} ({entry['cases']} cas, {entry['seconds']} s) {entry['message']}")
        results.append(entry)
    bad = [r for r in results if r["status"] in ("echec", "erreur")]
    return {"scenarios": results, "verdict": "echec" if bad else "ok",
            "summary": f"{len(results)} scénario(s), {sum(len(r['failures']) for r in results)} échec(s) distinct(s)"}
