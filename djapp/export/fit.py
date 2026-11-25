# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Clamps a track's values (text lengths, ratings, colours) into ranges the export writers can store, without changing the original.

# A damaged collection can hand the model a rating of -1, a colour of 99, a 70 000
# character comment or a title with half a surrogate pair. fit() gives the writers a copy
# of the track with every value in a range they can write, the track itself is left alone

from __future__ import annotations

import math
import re
from dataclasses import replace

from ..model import Track

_SURROGATES = re.compile("[\ud800-\udfff]")

# characters of a title, artist, album... a page of the database holds about 4 KB
NAME = 255
# and a track row holds its title, its comment and its paths, at 2 bytes a letter
COMMENT = 1000
TEXTS = {
    "title": NAME,
    "artist": NAME,
    "album": NAME,
    "album_artist": NAME,
    "genre": NAME,
    "label": NAME,
    "remixer": NAME,
    "composer": NAME,
    "comment": COMMENT,
    "added": 32,
    "modified": 32,
    "last_played": 32,
}
INTS = {
    "bitrate": (0, 100_000),
    "samplerate": (0, 1_000_000),
    "size": (0, 2**32 - 1),
    "rating": (0, 5),
    "plays": (0, 65_535),
    "color": (0, 8),
    "year": (0, 9999),
    "track_no": (0, 9999),
    "disc_no": (0, 9999),
}
SECONDS = 65_535  # the database counts the seconds of a track in 16 bits
BPM = 655.35  # and the beat grid keeps the tempo times 100 in 16 bits
# a cue position or a loop end is a count of milliseconds in 32 bits in the analysis files
CUE_MS = 2**32 - 1

# ------------------------------------------------------------
# text a file can hold: no lone surrogate, no NUL, not longer than limit
def text(value, limit: int = NAME) -> str:
    s = value if isinstance(value, str) else str(value)
    if s.isascii() and len(s) <= limit and "\x00" not in s:
        return s
    return _SURROGATES.sub("�", s).replace("\x00", "")[:limit]

# ------------------------------------------------------------
def whole(value, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return low
    return min(high, max(low, n))

def real(value, low: float, high: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return low
    return min(high, max(low, x)) if math.isfinite(x) else low

# ------------------------------------------------------------
def fit(t: Track) -> Track:
    change: dict = {}
    for name, limit in TEXTS.items():
        v = getattr(t, name)
        if (fixed := text(v, limit)) != v:
            change[name] = fixed
    for name, (low, high) in INTS.items():
        v = getattr(t, name)
        if (fixed := whole(v, low, high)) != v or type(v) is not int:
            change[name] = fixed
    if not (isinstance(t.duration, float | int) and 0 <= t.duration <= SECONDS):
        change["duration"] = real(t.duration, 0.0, SECONDS)
    if not (isinstance(t.bpm, float | int) and 0 <= t.bpm <= BPM):
        change["bpm"] = real(t.bpm, 0.0, BPM)
    if t.key is not None and not (isinstance(t.key, int) and 0 <= t.key <= 23):
        change["key"] = None
    if t.grid is not None and not (
        isinstance(t.grid, float | int) and math.isfinite(t.grid)
    ):
        change["grid"] = None
    if not (isinstance(t.gain, float | int) and math.isfinite(t.gain)):
        change["gain"] = 0.0
    cues = [
        c
        for c in t.cues
        if isinstance(c.start, float | int)
        and math.isfinite(c.start)
        and isinstance(c.length, float | int)
        and math.isfinite(c.length)
        and abs(c.start) <= CUE_MS
        and c.start + max(0, c.length) <= CUE_MS
        and (c.length <= 0 or c.start + c.length >= 0)
    ]
    named = [
        replace(c, name=fixed) if (fixed := text(c.name, NAME)) != c.name else c
        for c in cues
    ]
    if len(cues) != len(t.cues) or any(a is not b for a, b in zip(named, cues, strict=True)):
        change["cues"] = named
    return replace(t, **change) if change else t
