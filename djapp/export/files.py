# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Audio copy engine shared by every export: safe file names, pipelined copies with verification and progress.

from __future__ import annotations

import contextlib
import hashlib
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..model import Track
from .manifest import Cancelled, Manifest, replace, sha256_file

# big writes are more sequential for a flash controller and cost fewer syscalls
COPY_BLOCK = 4 << 20
QUEUE = 8

_BAD = re.compile('[<>:"/\\\\|?*\x00-\x1f\ud800-\udfff]')
# Windows keeps these names for devices, with or without an extension, and a FAT key cannot hold them
_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# ------------------------------------------------------------
@dataclass
class Report:
    tracks: int = 0
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cached: int = 0
    reused: int = 0
    path: str = ""
    manifest: str = ""
    verified: int = 0  # files read back from the drive and found identical
    verified_size: int = 0
    kept: int = 0  # CDJ key: playlists already on the key that stay as they were
    replaced: list[str] = field(default_factory=list)
    added: int = 0  # CDJ key: playlists that were not on the key yet
    # CDJ key: where the database this export replaced was saved, "" when there was none
    backup: str = ""
    # CDJ key: playlists none of whose files was found, not written
    left_out: list[str] = field(default_factory=list)
    # CDJ key: kept analysis files a player changed since the last export
    player_changed: list[str] = field(default_factory=list)
    # CDJ key: player settings files already on the key that fail a check, as (file, reason), left as they are
    settings_problems: list[tuple[str, str]] = field(default_factory=list)
    # CDJ key: player settings chosen in the program that were written to the file on the key
    settings_set: int = 0

# ----------------------------------------------------------
# FAT32 friendly name, keeps the accents like rekordbox does
def safe_name(s: str, limit: int = 0) -> str:
    s = _BAD.sub("_", s).strip().rstrip(". ")
    if limit and len(s) > limit:
        stem, ext = os.path.splitext(s)
        s = stem[: max(1, limit - len(ext))].rstrip(". ") + ext
    if s.split(".")[0].rstrip().upper() in _DEVICES:
        s = "_" + s
    return s or "_"

# ----------------------------------------------------------
# hands out destination names, never twice the same (case insensitive, FAT)
class Planner:
    def __init__(self):
        self.taken: set[str] = set()

    # the first name from rel, then rel (2), rel (3)... that nobody took in this export and
    # that fits(name) accepts. fits tells whether a file already sitting there may be used
    def claim(self, rel: str, fits=None) -> str:
        stem, ext = os.path.splitext(rel)
        cand, n = rel, 1
        while cand.lower() in self.taken or (fits is not None and not fits(cand)):
            n += 1
            cand = f"{stem} ({n}){ext}"
        self.taken.add(cand.lower())
        return cand

# ------------------------------------------------------
# USB key or card: one copy at a time, parallel writes fragment the FAT
# and the flash controller serialises them anyway
def removable(path: str) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    return ctypes.windll.kernel32.GetDriveTypeW(root) == 2

# --------------------------------
def same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False

SAMPLE = 64 << 10

# ----------------------------------------------------------
# same size, and the same bytes at the start, in the middle and at the end. 192 KB of
# each, not the whole audio, so a key full of tracks is not read twice. Two different
# songs of the same size do not match in practice
def same_content(a: str, b: str) -> bool:
    try:
        size = os.path.getsize(a)
        if size != os.path.getsize(b):
            return False
        if same_file(a, b):
            return True
        with open(a, "rb") as fa, open(b, "rb") as fb:
            for at in (0, max(0, (size - SAMPLE) // 2), max(0, size - SAMPLE)):
                fa.seek(at)
                fb.seek(at)
                if fa.read(SAMPLE) != fb.read(SAMPLE):
                    return False
    except OSError:
        return False
    return True

# ----------------------------------------------------------
def _todo(jobs) -> int:
    n = 0
    for s, d in jobs:
        try:
            size = os.path.getsize(s)
            if not (os.path.exists(d) and os.path.getsize(d) == size):
                n += size
        except OSError:
            pass
    return n

# ----------------------------------------------------------
# (True, sha) when dst is already a good copy. The sha is None when it was trusted
# without reading, the manifest then keeps what it knows
def _already(src: str, dst: str, size: int, verify: bool) -> tuple[bool, str | None]:
    # attention -> bug = a track already on the key is its own source, opening it for writing empties
    # it. Nothing to copy
    if same_file(src, dst):
        return True, sha256_file(dst) if verify else None
    # same size already there: a previous export, trusted unless verifying
    if os.path.exists(dst) and os.path.getsize(dst) == size:
        if not verify:
            return True, None
        sha = sha256_file(dst)
        if sha256_file(src) == sha:
            return True, sha
    return False, None

# ----------------------------------------------------------
# bytes actually written, 0 when the file was already there. The copy is hashed
# on the way for the manifest, no second read
def copy_one(src: str, dst: str, verify: bool, man: Manifest | None = None) -> int:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    size = os.path.getsize(src)
    hit, sha = _already(src, dst, size, verify)
    if hit:
        if man:
            man.add(dst, man.known(dst) if sha is None else sha)
        return 0
    tmp = dst + ".part"
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(COPY_BLOCK)
                if not b:
                    break
                h.update(b)
                fo.write(b)
        replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    if man:
        man.add(dst, h.hexdigest())
    return size

# ------------------------------------------------------------------
# reads the sources in order, ahead of the writer, into a bounded queue. The
# source drive and the key are busy at the same time instead of taking turns
def _read_ahead(jobs, verify: bool, q: queue.Queue, stop: threading.Event):
    def put(item) -> bool:
        while not stop.is_set():
            try:
                q.put(item, timeout=0.2)
                return True
            except queue.Full:
                pass
        return False

    for i, (src, dst) in enumerate(jobs):
        try:
            size = os.path.getsize(src)
            hit, sha = _already(src, dst, size, verify)
            if hit:
                if not put((i, "skip", sha)):
                    return
                continue
            with open(src, "rb") as fi:
                if not put((i, "start", size)):
                    return
                while True:
                    b = fi.read(COPY_BLOCK)
                    if not b:
                        break
                    if not put((i, "data", b)):
                        return
            if not put((i, "end", None)):
                return
        except OSError as e:
            if not put((i, "err", e)):
                return

# ----------------------------------------------------------
# one writer fed by a reader thread: for a removable drive, where parallel writes
# do not help but waiting for each read before writing does
def copy_pipelined(
    jobs: list[tuple[str, str]],
    verify: bool,
    cancel: threading.Event | None = None,
    progress=None,
    man: Manifest | None = None,
) -> list[str]:
    errors, written = [], 0
    todo = _todo(jobs)
    q: queue.Queue = queue.Queue(maxsize=QUEUE)
    stop = threading.Event()
    reader = threading.Thread(
        target=_read_ahead, args=(jobs, verify, q, stop), daemon=True
    )
    t0 = time.monotonic()
    reader.start()

    fo, h, tmp, failed, size = None, None, "", None, 0

    def drop():
        nonlocal fo
        if fo is not None:
            fo.close()
            fo = None
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    try:
        done = 0
        while done < len(jobs):
            try:
                i, kind, payload = q.get(timeout=0.5)
            except queue.Empty:
                if cancel is not None and cancel.is_set():
                    raise Cancelled() from None
                if not reader.is_alive():
                    raise RuntimeError("read-ahead thread stopped") from None
                continue
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            src, dst = jobs[i]
            finished = True
            if kind == "skip":
                if man:
                    man.add(dst, man.known(dst) if payload is None else payload)
            elif kind == "start":
                finished = False
                size, failed, h, tmp = payload, None, hashlib.sha256(), dst + ".part"
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    fo = open(tmp, "wb")  # noqa: SIM115
                except OSError as e:
                    failed, tmp = e, ""
            elif kind == "data":
                finished = False
                if failed is None:
                    try:
                        h.update(payload)
                        fo.write(payload)
                    except OSError as e:
                        failed = e
                        drop()
            elif kind == "end":
                if failed is None:
                    try:
                        fo.close()
                        fo = None
                        replace(tmp, dst)
                        written += size
                        if man:
                            man.add(dst, h.hexdigest())
                    except OSError as e:
                        failed = e
                        drop()
                tmp = ""
            else:
                failed = payload
                drop()
                tmp = ""
            if finished:
                if failed is not None:
                    errors.append(f"{os.path.basename(src)}: {failed}")
                    failed = None
                done += 1
                if progress:
                    progress(done, len(jobs), eta(written, todo, t0))
    except BaseException:
        drop()
        raise
    finally:
        stop.set()
        reader.join(timeout=5)
    return errors

# ----------------------------------------------------------
def eta(done: int, total: int, t0: float) -> str:
    dt = time.monotonic() - t0
    if done <= 0 or dt < 1:
        return ""
    rate = done / dt
    left = (total - done) / rate
    return f"{rate / 1e6:.1f} MB/s, {int(left // 60)}:{int(left % 60):02d} left"

# ----------------------------------------------------------
# jobs: (src, dst). progress(i, n, info) with the speed and time left.
# Returns the errors, one line each
def copy_all(
    jobs: list[tuple[str, str]],
    verify: bool,
    cancel: threading.Event | None = None,
    progress=None,
    man: Manifest | None = None,
) -> list[str]:
    if jobs and removable(jobs[0][1]):
        return copy_pipelined(jobs, verify, cancel, progress, man)
    errors, done, written = [], 0, 0
    todo = _todo(jobs)
    workers = 4
    t0 = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(copy_one, s, d, verify, man): (s, d) for s, d in jobs}
        for f, (s, _) in futs.items():
            if cancel is not None and cancel.is_set():
                pool.shutdown(cancel_futures=True)
                raise Cancelled()
            try:
                written += f.result()
            except OSError as e:
                errors.append(f"{os.path.basename(s)}: {e}")
            done += 1
            if progress:
                progress(done, len(jobs), eta(written, todo, t0))
    return errors

def present(t: Track) -> bool:
    return bool(t.path) and os.path.isfile(t.path)
