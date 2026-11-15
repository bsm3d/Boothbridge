# A small synthetic collection in a temp folder: short WAV files, a Traktor NML, an M3U,
# a rekordbox XML and a project JSON, with a few tracks whose file is missing

from __future__ import annotations

import random
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ROOT  # puts the project on sys.path  # noqa: F401

RATE = 11025
GENRES = ["House", "Techno", "Deep House", "Pop", "Rock", "Disco", "", "Trance", "Italo Disco"]
WORDS = ["Night", "Light", "Echo", "Rain", "Fire", "Ocean", "Drift", "Pulse", "Velvet", "Chrome",
         "Cafe", "Sol", "Neon", "Tide", "Amber"]
ARTISTS = ["Nova", "Kite", "Mira & Co", "DJ O'Neil", "Beta (live)", "Zoe", "Ålesund", "Tom Tom Club"]


# ------------------------------------------------------
@dataclass
class Collection:
    root: Path
    audio: Path
    files: list[Path] = field(default_factory=list)
    nodes: list = field(default_factory=list)
    project: Path = None
    nml: Path = None
    m3u: Path = None
    rbxml: Path = None
    out: Path = None
    restore: Path = None

    def sources(self) -> list[Path]:
        return [p for p in (self.project, self.nml, self.m3u, self.rbxml) if p]


# A kick-like click at the tempo over two sine layers, so the analysis has something to find
def write_wav(path: Path, seconds: float, rng: random.Random, bpm: float = 120.0):
    n = max(1, int(RATE * seconds))
    t = np.arange(n) / RATE
    tone = 0.25 * np.sin(2 * np.pi * rng.choice((110.0, 220.0, 330.0)) * t)
    tone += 0.1 * np.sin(2 * np.pi * rng.uniform(500, 2000) * t)
    beat = 60.0 / bpm
    kick = np.exp(-((t % beat) * 30)) * np.sin(2 * np.pi * 60 * (t % beat))
    data = np.clip(tone + 0.5 * kick, -1, 1)
    pcm = (data * 32000).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm.tobytes())


def make_tracks(audio: Path, rng: random.Random, count: int, missing: int):
    from djapp.model import CUE, LOAD, LOOP, Cue, Track
    tracks = []
    for i in range(count):
        title = f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i}"
        artist = rng.choice(ARTISTS)
        name = f"{artist.replace('/', '-')} - {title}.wav".replace("&", "and")
        gone = i >= count - missing
        path = audio / ("gone" if gone else "") / name
        bpm = float(rng.randint(96, 150))
        seconds = round(rng.uniform(1.5, 4.0), 2)
        if not gone:
            write_wav(path, seconds, rng, bpm)
        t = Track(title=title, artist=artist, album=f"Album {i % 5}", genre=rng.choice(GENRES),
                  path=str(path), size=int(seconds * RATE * 2), bitrate=176, samplerate=RATE,
                  duration=seconds, bpm=bpm, key=rng.choice([None, *range(24)]),
                  energy=rng.choice([None, float(rng.randint(1, 10))]), rating=rng.randint(0, 5),
                  color=rng.randint(0, 8), year=rng.randint(1995, 2026), track_no=i % 12 + 1,
                  grid=round(rng.uniform(0, 400), 1), audio_id=f"synthetic-{i}")
        slots = rng.sample(range(8), rng.randint(0, 4))
        for slot in slots:
            t.cues.append(Cue(name=f"Cue {slot}", kind=CUE, start=round(rng.uniform(0, seconds * 900), 1),
                              hotcue=slot))
        if rng.random() < 0.5:
            t.cues.append(Cue(name="Loop", kind=LOOP, start=round(rng.uniform(0, seconds * 500), 1),
                              length=round(rng.uniform(200, 800), 1)))
        if rng.random() < 0.4:
            t.cues.append(Cue(name="", kind=LOAD, start=0.0))
        tracks.append(t)
    return tracks


def build(root, seed: int = 0, count: int = 36, missing: int = 6) -> Collection:
    from djapp import settings
    from djapp.export import m3u, nml, rbxml
    from djapp.model import Node
    from djapp.sources import project

    rng = random.Random(seed)
    root = Path(root)
    audio = root / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "gone").mkdir(exist_ok=True)
    tracks = make_tracks(audio, rng, count, missing)
    third = len(tracks) // 3
    nodes = [
        Node("playlist", "Warm up", tracks=tracks[:third]),
        Node("playlist", "Peak time", tracks=tracks[third:2 * third + 2]),
        Node("folder", "Sets", children=[
            Node("playlist", "Closing", tracks=tracks[2 * third:]),
            Node("playlist", "Mixed", tracks=rng.sample(tracks, 12)),
        ]),
        Node("playlist", "Empty", tracks=[]),
    ]
    col = Collection(root, audio, [Path(t.path) for t in tracks if "gone" not in t.path], nodes)
    cfg = dict(settings.DEFAULTS, music_root=str(audio))

    col.project = root / "sources" / "project.json"
    col.project.parent.mkdir(exist_ok=True)
    project.save(str(col.project), nodes, cfg)

    folder = root / "sources" / "traktor"
    nml.export(nodes, str(folder), copy=False)
    col.nml = folder / "collection.nml"

    folder = root / "sources" / "rekordbox"
    rbxml.export(nodes, str(folder), copy=False)
    col.rbxml = folder / "rekordbox.xml"

    folder = root / "sources" / "m3u"
    folder.mkdir(exist_ok=True)
    col.m3u = folder / "warm-up.m3u8"
    m3u.write_list(nodes[0], str(col.m3u), {}, False)

    col.out = root / "out"
    col.out.mkdir(exist_ok=True)
    col.restore = root / "restore"
    col.restore.mkdir(exist_ok=True)
    return col
