# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Shared helpers for the key tests: synthetic WAV libraries, seeded models and comparison with key output.

import contextlib
import random
import struct
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile
from djapp import export, settings
from djapp.export.cdj import anlz, usb
from djapp.model import CUE, LOAD, LOOP, Cue, Node, Track, playlists

RATE = 11025
TODAY = time.strftime("%Y-%m-%d")

TITLES = ["Plain title", "Café del Mar", "Ünïcödé \u2606 naïve", "日本語のタイトル", "Emoji \U0001F3B5 track",
          "A" * 140, "Très long titre accentué " * 8, "Quote \" and 'apostrophe'", "  padded  ", "x"]
NAMES = ["Artist One", "Zoë Ångström", "山田太郎", "DJ \U0001F525", "", "B" * 60, "Dvořák"]
ALBUMS = ["Album", "Épopée", "", "Greatest Hits Vol. 2", "C" * 70]
GENRES = ["House", "Drum & Bass", "", "Électro", "Techno"]
LABELS = ["Label X", "", "Néo Records"]
CUE_NAMES = ["", "Intro", "Drop", "Café", "Brèak", "日本", "L" * 40, "  spaced  "]
COLORS = ["", "#E02823", "#305AFF", "#28E214", "#FFA000", "#123456", "#FFFFFF"]

def cfg(**extra) -> dict:
    return {**settings.DEFAULTS, "copy_music": True, "verify_copy": False, "anlz_processes": 1, **extra}


# the analysis in threads instead of processes, and a cache folder of its own: the same
# code runs, a test of a hundred exports does not spawn a hundred pools
@contextlib.contextmanager
def inline(cache: Path):
    with patch.object(usb, "ProcessPoolExecutor", ThreadPoolExecutor), \
         patch.object(usb, "cache_dir", return_value=cache):
        yield


# ----------------------------------------------------------------------
# synthetic audio in the format of the file name (FLAC, what people play in a set)
def write_audio(path: Path, seconds: float, seed: int):
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    y = 0.4 * np.sin(2 * np.pi * (110 + 37 * (seed % 11)) * t) + 0.05 * rng.standard_normal(n)
    soundfile.write(str(path), y, RATE, subtype="PCM_16")


# ----------------------------------------------------------------------
def make_track(folder: Path, name: str, seed: int, **fields) -> Track:
    path = folder / f"{name}.flac"
    write_audio(path, 2.0 + (seed % 5) * 0.37, seed)
    t = Track(title=name, artist="Artist", path=str(path), samplerate=RATE)
    t.duration = soundfile.info(str(path)).duration
    for k, v in fields.items():
        setattr(t, k, v)
    return t


# ----------------------------------------------------------------------
def random_cue(rng: random.Random, dur_ms: float, hot: int) -> Cue:
    start = rng.uniform(0, dur_ms * 0.8)
    loop = rng.random() < 0.3
    kind = LOOP if loop else rng.choice((CUE, CUE, LOAD))
    return Cue(name=rng.choice(CUE_NAMES), kind=kind, start=round(start, 3),
               length=round(rng.uniform(100, 900), 3) if loop else 0.0, hotcue=hot,
               color=rng.choice(COLORS) if hot >= 0 else "")


# ----------------------------------------
def random_track(rng: random.Random, folder: Path, index: int, seed: int) -> Track:
    t = make_track(folder, f"t{index}_{seed}", rng.randrange(1 << 30))
    t.title = rng.choice(TITLES) + (f" {index}" if rng.random() < 0.5 else "")
    t.artist, t.album = rng.choice(NAMES), rng.choice(ALBUMS)
    # the album artist is stored in the album row: a track without an album has nowhere to keep it
    t.album_artist = rng.choice(NAMES + [""] * 3) if t.album else ""
    t.genre, t.label = rng.choice(GENRES), rng.choice(LABELS)
    t.remixer, t.composer = rng.choice(NAMES), rng.choice(NAMES)
    t.comment = rng.choice(["", "A comment", "Commentaire accentué é", "c" * 200])
    t.year = rng.choice([0, 1987, 2024, 2026])
    t.track_no, t.disc_no, t.plays = rng.randrange(0, 40), rng.randrange(0, 4), rng.randrange(0, 500)
    t.rating, t.color = rng.randrange(0, 6), rng.randrange(0, 9)
    t.bitrate = rng.choice([0, 128, 320, 1411])
    t.key = rng.choice([None] + list(range(24)))
    t.added = rng.choice(["", "2024-05-17", "2026-01-02"])
    dur = t.duration * 1000
    if rng.random() < 0.7:
        t.bpm = round(rng.uniform(70, 175), 2)
        t.grid = round(rng.uniform(0, dur / 2), 3) if rng.random() < 0.8 else None
    else:
        t.bpm = round(rng.uniform(70, 175), 2) if rng.random() < 0.3 else 0.0
    slots = rng.sample(range(8), rng.randrange(0, 9))
    t.cues = [random_cue(rng, dur, s) for s in slots] + [random_cue(rng, dur, -1) for _ in range(rng.randrange(0, 5))]
    rng.shuffle(t.cues)
    return t


# ------------------------------------------------------------------
def random_tree(rng: random.Random, tracks: list[Track], depth: int = 0, prefix: str = "") -> list[Node]:
    out = []
    for i in range(rng.randrange(1, 4)):
        if depth < 2 and rng.random() < 0.35:
            kids = random_tree(rng, tracks, depth + 1, f"{prefix}F{i}.")
            out.append(Node("folder", rng.choice(["Dossier é", "Folder", "日本"]) + f" {prefix}{i}", children=kids))
        else:
            n = rng.randrange(0, 6)
            out.append(Node("playlist", rng.choice(["Set", "Warm-up é", "Peak \u2606", "Ünder"]) + f" {prefix}{i}",
                            tracks=[rng.choice(tracks) for _ in range(n)]))
    return out


# ------------------------------------------------------------------
def random_library(seed: int, folder: Path, count: int = 8) -> list[Node]:
    rng = random.Random(seed)
    tracks = [random_track(rng, folder, i, seed) for i in range(count)]
    tree = random_tree(rng, tracks)
    if not any(pl.tracks for pl in playlists(tree)):
        tree.append(Node("playlist", "Last resort", tracks=tracks[:2]))
    return tree


def export_to(nodes, out, cache: Path, **extra):
    with inline(cache):
        return export.run("CDJ/USB", nodes, str(out), cfg(**extra))


# ********************************************
# what a key must give back
# ********************************************

def cue_view(c: Cue):
    loop = c.is_loop
    color = ""
    if c.hotcue >= 0:
        color = f"#{anlz.hot_color(c)[1]:06X}"
    return (c.hotcue, loop, round(max(0.0, c.start)), round(c.start + c.length) if loop else None,
            c.name.strip(), color)


# ----------------------------------------------------------------------
# the fields the writer writes, as the key keeps them
def track_view(t: Track) -> dict:
    album_artist = t.album_artist if t.album and t.album_artist != t.artist else ""
    hot = [cue_view(c) for c in t.hot_cues()[:8]]
    mem = [cue_view(c) for c in t.memory_cues()]
    return {"title": t.title, "artist": t.artist, "album": t.album, "album_artist": album_artist, "genre": t.genre,
            "label": t.label, "remixer": t.remixer, "composer": t.composer, "comment": t.comment, "year": t.year,
            "track_no": t.track_no, "disc_no": t.disc_no, "plays": t.plays, "rating": min(5, t.rating),
            "color": t.color, "tempo": round(t.bpm * 100), "key": t.key, "bitrate": t.bitrate,
            "samplerate": t.samplerate or 44100, "seconds": round(t.duration), "added": t.added or TODAY,
            "beats": anlz.beats(t), "hot": hot, "memory": mem}


def tree_view(nodes: list[Node]):
    return [(n.kind, n.name, tree_view(n.children)) if n.is_folder
            else (n.kind, n.name, [track_view(t) for t in n.tracks]) for n in nodes]


# ----------------------------------------------------------------------
def names_of(nodes: list[Node], prefix: str = "") -> list[str]:
    out = []
    for n in nodes:
        out += names_of(n.children, f"{prefix}{n.name} / ") if n.is_folder else [prefix + n.name]
    return out


def pdb_bytes(root) -> bytes:
    return (Path(root) / "PIONEER" / "rekordbox" / "export.pdb").read_bytes()


# ----------------------------------------------------------------------
# the header sequence is the one field two exports of the same content may not share
def normalised_pdb(data: bytes) -> bytes:
    b = bytearray(data)
    struct.pack_into("<I", b, 0x14, 0)
    return bytes(b)


def tmp():
    t = tempfile.TemporaryDirectory()
    return t, Path(t.name)
