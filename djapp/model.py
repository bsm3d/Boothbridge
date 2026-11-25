# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Collection model shared by every source and export: tracks, cues and playlist nodes, with fixed units.

# units are fixed here once, so parsers convert on the way in and exporters on the way
# out: durations in seconds, positions in ms, bitrate in kbps, size in bytes, rating 0-5,
# key as a Traktor key index (0-23, see keys.py), dates as ISO YYYY-MM-DD

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

# Traktor CUE_V2 TYPE values
CUE, FADE_IN, FADE_OUT, LOAD, GRID, LOOP = range(6)
LOOP_BEATS = (1, 2, 4, 8)

# --------------------------------------------------
@dataclass(slots=True)
class Cue:
    name: str = ""
    kind: int = CUE
    start: float = 0.0  # ms
    length: float = 0.0  # ms, a loop when > 0
    hotcue: int = -1  # slot 0-7, -1 = memory cue
    color: str = ""

    @property
    def is_loop(self) -> bool:
        return self.length > 0

# ----------------------------------------------------------------
@dataclass(slots=True, eq=False)
class Track:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    label: str = ""
    comment: str = ""
    remixer: str = ""
    composer: str = ""

    path: str = ""  # absolute source file
    size: int = 0
    bitrate: int = 0
    samplerate: int = 44100
    duration: float = 0.0
    bpm: float = 0.0
    key: int | None = None
    gain: float = 0.0
    energy: float | None = None  # project-only estimated/manual energy, 1..10

    rating: int = 0
    plays: int = 0
    color: int = 0
    year: int = 0
    track_no: int = 0
    disc_no: int = 0

    added: str = ""
    modified: str = ""
    last_played: str = ""

    cues: list[Cue] = field(default_factory=list)
    grid: float | None = None  # first beat, ms
    audio_id: str = ""  # Traktor AUDIO_ID, stable across relocations
    locked: bool = False

    @property
    def uid(self) -> str:
        return self.audio_id or self.path

    def hot_cues(self) -> list[Cue]:
        return sorted(
            (c for c in self.cues if c.hotcue >= 0 and c.kind in (CUE, LOOP, LOAD)),
            key=lambda c: c.hotcue,
        )

    def memory_cues(self) -> list[Cue]:
        return [c for c in self.cues if c.hotcue < 0 and c.kind in (CUE, LOOP, LOAD)]

# ----------------------------------------------------------------
@dataclass(slots=True, eq=False)
class Node:
    kind: str  # 'folder' | 'playlist' | 'smartlist'
    name: str
    tracks: list[Track] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)
    uuid: str = ""
    query: str = ""  # smartlist search expression

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"

# ------------------------------------------------------------
def playlists(nodes: list[Node]) -> Iterator[Node]:
    for n in nodes:
        if n.is_folder:
            yield from playlists(n.children)
        else:
            yield n

def unique_tracks(nodes: list[Node]) -> list[Track]:
    seen, out = set(), []
    for pl in playlists(nodes):
        for t in pl.tracks:
            if t.uid not in seen:
                seen.add(t.uid)
                out.append(t)
    return out

# ----------------------------------------------------------------
# takes node out of the tree, wherever it sits. False when it is not there. Its tracks are
# shared with other playlists and stay as they are
def detach(nodes: list[Node], node: Node) -> bool:
    for i, n in enumerate(nodes):
        if n is node:
            del nodes[i]
            return True
        if n.is_folder and detach(n.children, node):
            return True
    return False

# ----------------------------------------------------------------
# copy of the tree reduced to the playlists whose id() is in keep, folders kept
# only when something survives below them
def prune(nodes: list[Node], keep: set[int]) -> list[Node]:
    out = []
    for n in nodes:
        if n.is_folder:
            sub = prune(n.children, keep)
            if sub:
                out.append(Node("folder", n.name, children=sub, uuid=n.uuid))
        elif id(n) in keep:
            out.append(n)
    return out
