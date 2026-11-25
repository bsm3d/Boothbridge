# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Drives the whole CDJ USB export: audio copy, export.pdb, analysis files, artwork and settings, in a safe order.

# Layouts come from rekordbox keys compared byte by byte, and crate-digger research. Only ordinary data files are written.
# Order matters: audio first, analysis and pictures next, export.pdb last.
# A player only learns of a track from the database, so a cut in the middle never leaves it pointing to missing files.

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import struct
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field

from ... import tags
from ...model import Node, Track, playlists, prune, unique_tracks
from ...settings import cache_dir
from ...sources import key
from ..files import (
    Cancelled,
    Planner,
    Report,
    copy_all,
    present,
    safe_name,
    same_content,
)
from ..fit import fit
from ..manifest import Manifest, sha256_bytes, write_atomic, write_direct
from . import anlz, keymerge, pdbwrite, reference, settingsread, validate

log = logging.getLogger(__name__)

# bump when the analysis writer changes, it is part of every cache key
ANLZ_VERSION = 9

# rekordbox cuts folder and file names to this length
NAME_LIMIT = 48
PIONEER_DIR = "PIONEER"
DATABASE_DIRS = (PIONEER_DIR, "rekordbox")
DATABASE_FILE = "export.pdb"
# FAT stores modification times with a precision of two seconds
FAT_TIME_SLACK = 2
# more workers do not help: memory bandwidth, and startup costs more than it saves
MAX_ANALYSIS_WORKERS = 8
# progress bar share of copy + analysis, the rest is the database
COPY_ANALYSE_PERCENT = 90
PERCENT_ARTWORK = 92
PERCENT_DATABASE = 96
PERCENT_DONE = 100

# ------------------------------------------------------------
# Contents/<artist>/<album>/<file>. An existing file is reused only if it is the very
# same recording: two untagged track01.wav get different names, so audio already on
# the key is never replaced by another song.
def usb_path(t: Track, plan: Planner, root: str = "") -> str:
    parts = [
        safe_name(t.artist or "Unknown Artist", NAME_LIMIT),
        safe_name(t.album or "Unknown Album", NAME_LIMIT),
        safe_name(os.path.basename(t.path), NAME_LIMIT),
    ]

    def usable(candidate: str) -> bool:
        existing = os.path.join(root, candidate)
        return not os.path.lexists(existing) or same_content(t.path, existing)

    return "/" + plan.claim("Contents/" + "/".join(parts), usable if root else None)

# ------------------------------------------------------------
# a file already inside Contents on this key stays where it is
def place(root: str, t: Track, plan: Planner) -> str:
    current = key.key_path(root, t.path)
    if current.lower().startswith("/contents/"):
        plan.taken.add(current[1:].lower())
        return current
    return usb_path(t, plan, root)

# ------------------------------------------------------------------
# everything that changes the analysis goes into the hash: writer version, file size and mtime, key path, tempo, grid, length, cues
def cache_key(t: Track, usb: str) -> str:
    stat = os.stat(t.path)
    cue_fields = [
        (c.kind, c.start, c.length, c.hotcue, c.name, c.color) for c in t.cues
    ]
    fingerprint = repr(
        (
            ANLZ_VERSION,
            stat.st_size,
            int(stat.st_mtime),
            usb,
            t.bpm,
            t.grid,
            t.duration,
            cue_fields,
        )
    )
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()

# ------------------------------------------------------------
# May a key-only track keep its analysis? Both files must have been read completely and
# the audio must be the one they were made from: size in the database, or when that is
# 0 (rekordbox does it for some files) the audio must not be newer than the analysis.
def unchanged(root: str, t: Track, usb: str, old: key.KeyData) -> bool:
    recorded = old.anlz.get(usb.lower(), "")
    if usb.lower() not in old.intact or not key.ANLZ_PATH.match(recorded):
        return False
    dat_file = os.path.join(root, recorded.lstrip("/"))
    try:
        audio = os.stat(t.path)
        newest_analysis = max(
            os.stat(dat_file).st_mtime, os.stat(dat_file[:-3] + "EXT").st_mtime
        )
    except OSError:
        return False
    if t.size:
        return t.size == audio.st_size
    return audio.st_mtime <= newest_analysis + FAT_TIME_SLACK

# ------------------------------------------------------------
# left untouched (DAT, EXT, and the 2EX rekordbox may have written), only recorded in the checksums
def keep_analysis(root: str, it: pdbwrite.Item, man: Manifest | None):
    folder = os.path.dirname(os.path.join(root, it.anlz.lstrip("/")))
    for name in sorted(os.listdir(folder)):
        if man and name.upper().startswith("ANLZ0000."):
            man.keep(os.path.join(folder, name))

# ------------------------------------------------------------
def whole(raw: bytes) -> bool:
    try:
        validate.anlz(raw)
    except (ValueError, UnicodeDecodeError, struct.error):
        return False
    return True

# ------------------------------------------------------------
# an unreadable or invalid pair counts as a miss
def cached(
    store, cache_id: str, expected_path: str | None = None
) -> tuple[bytes, bytes] | None:
    try:
        dat = (store / f"{cache_id}.DAT").read_bytes()
        ext = (store / f"{cache_id}.EXT").read_bytes()
    except OSError:
        return None
    try:
        validate.anlz(dat, expected_path, "DAT")
        validate.anlz(ext, expected_path, "EXT")
    except (ValueError, UnicodeDecodeError, struct.error):
        log.warning("analysis cache %s damaged, analysed again", cache_id)
        return None
    return dat, ext

# runs in a worker process
def anlz_job(t: Track, usb: str) -> tuple[bytes, bytes]:
    from ...analysis import Audio

    return anlz.build(t, usb, Audio(t.path))

# ------------------------------------------------------
def put(path: str, data: bytes, man: Manifest | None):
    write_direct(path, data)
    if man:
        man.add(path, sha256_bytes(data))

# --------------------------------------------------------------
# both validated before anything touches the key, and again after writing
def put_anlz(
    root: str, it: pdbwrite.Item, dat: bytes, ext: bytes, man: Manifest | None = None
):
    dat_file = os.path.join(root, it.anlz.lstrip("/"))
    ext_file = dat_file[:-4] + ".EXT"
    validate.anlz(dat, it.path, "DAT")
    validate.anlz(ext, it.path, "EXT")
    for path, data, kind in ((dat_file, dat, "DAT"), (ext_file, ext, "EXT")):

        def valid(raw: bytes, kind: str = kind):
            return validate.anlz(raw, it.path, kind)

        validate.write_verified(path, data, valid)
        if man:
            man.add(path, sha256_bytes(data))

# refused before decoding its pixels
MAX_PIXELS = 40_000_000
# stored twice, small and medium square JPEG
THUMB_SIZE = 80
MEDIUM_SIZE = 240
JPEG_QUALITY = 90
ARTWORK_SET = "00001"

# a<number>.jpg and a<number>_m.jpg
ART_FILE = re.compile(r"a(\d{1,9})(_m)?\.jpg", re.IGNORECASE)

def art_folder(root: str) -> str:
    return os.path.join(root, PIONEER_DIR, "Artwork", ARTWORK_SET)

# --------------------------------------------
def art_numbers(root: str) -> set[int]:
    try:
        names = os.listdir(art_folder(root))
    except OSError:
        return set()
    numbers = set()
    for name in names:
        match = ART_FILE.fullmatch(name)
        if match:
            numbers.add(int(match.group(1)))
    return numbers

# ------------------------------------------------------------
# kept maps item.id to the picture number in the key database. Files stay as they are so
# the old database still shows the right picture. Vanished files: new picture later.
def keep_artwork(
    items: list[pdbwrite.Item],
    root: str,
    kept: dict[int, int],
    man: Manifest | None = None,
):
    folder = art_folder(root)
    for it in items:
        number = kept.get(it.id, 0)
        if not number:
            continue
        pair = [
            os.path.join(folder, f"a{number}.jpg"),
            os.path.join(folder, f"a{number}_m.jpg"),
        ]
        if not all(os.path.isfile(path) for path in pair):
            continue
        it.art = number
        if man:
            for path in pair:
                man.add(path)

# ------------------------------------------------------------
# after the new database is written. Only files named like rekordbox ones are touched.
def prune_artwork(items: list[pdbwrite.Item], root: str):
    in_use = {it.art for it in items if it.art}
    folder = art_folder(root)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        match = ART_FILE.fullmatch(name)
        if not match or int(match.group(1)) in in_use:
            continue
        try:
            os.remove(os.path.join(folder, name))
        except OSError as err:
            log.debug("artwork %s not removed: %s", name, err)

def _square_jpeg(picture, side: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    picture.resize((side, side), Image.LANCZOS).save(
        buffer, "JPEG", quality=JPEG_QUALITY
    )
    return buffer.getvalue()

def _decode_picture(raw: bytes, audio_path: str):
    from PIL import Image

    try:
        picture = Image.open(io.BytesIO(raw))
        if picture.width * picture.height > MAX_PIXELS:
            log.debug(
                "artwork of %s: %sx%s too big",
                audio_path,
                picture.width,
                picture.height,
            )
            return None
        return picture.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as err:
        log.debug("artwork of %s: %s", audio_path, err)
        return None

# ------------------------------------------------------------
# one pair per distinct image. Numbered after the highest in use when merging into a
# key, from 1 otherwise. Returns the number of pairs made.
def write_artwork(
    items: list[pdbwrite.Item],
    root: str,
    man: Manifest | None = None,
    merging: bool = False,
) -> int:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        log.info("Pillow missing, no artwork")
        return 0
    folder = art_folder(root)
    on_key = sorted(art_numbers(root)) if merging else [0]
    highest = max([it.art for it in items] + on_key)
    number_by_digest: dict[str, int] = {}
    for it in items:
        if it.art:
            continue
        raw = tags.artwork(it.track.path)
        if not raw:
            continue
        digest = hashlib.sha1(raw).hexdigest()
        if digest in number_by_digest:
            it.art = number_by_digest[digest]
            continue
        picture = _decode_picture(raw, it.track.path)
        if picture is None:
            continue
        number = highest + len(number_by_digest) + 1
        for side, name in (
            (THUMB_SIZE, f"a{number}.jpg"),
            (MEDIUM_SIZE, f"a{number}_m.jpg"),
        ):
            put(os.path.join(folder, name), _square_jpeg(picture, side), man)
        number_by_digest[digest] = it.art = number
    return len(number_by_digest)

# ------------------------------------------------------------------
# a playlist with no findable file would show up empty on the player, so it is left out.
# Returns (nodes kept, names left out, files not found).
def playable(nodes: list[Node]) -> tuple[list[Node], list[str], list[str]]:
    staying: set[int] = set()
    left_out: list[str] = []
    not_found: list[str] = []
    for playlist in playlists(nodes):
        if not playlist.tracks or any(present(t) for t in playlist.tracks):
            staying.add(id(playlist))
        else:
            left_out.append(playlist.name)
            not_found += [t.path or t.title for t in playlist.tracks]
    if not left_out:
        return nodes, [], []
    return prune(nodes, staying), left_out, not_found

@dataclass
class _Analysis:
    item: pdbwrite.Item
    cache_id: str

# state between the steps, which are methods in the order run() calls them
@dataclass
class _Run:
    nodes: list[Node]
    root: str
    verify: bool
    processes: int
    progress: object
    cancel: threading.Event | None
    man: Manifest | None
    old: key.KeyData | None
    rep: Report = field(default_factory=Report)
    target: dict[str, str] = field(default_factory=dict)
    # by lower case audio path
    known_anlz: dict[str, str] = field(default_factory=dict)
    # id() of the tracks read from the key
    from_key: set[int] = field(default_factory=set)
    keep_uids: set[str] = field(default_factory=set)
    # tracks only on the key, not in this export
    key_only_items: set[int] = field(default_factory=set)
    # id() of the items whose analysis failed, left out of the database
    failed: set[int] = field(default_factory=set)
    # tracks of the key that are in no playlist, kept in the database
    extra: list = field(default_factory=list)
    items: list[pdbwrite.Item] = field(default_factory=list)
    copies: list[tuple[str, str]] = field(default_factory=list)
    pending: list[_Analysis] = field(default_factory=list)
    copied: int = 0
    copy_info: str = ""
    analysed: int = 0
    # the player settings chosen in the program, as reference.format_choices writes them
    player: str = ""

    def say(self, percent: int, message: str):
        if self.progress:
            self.progress(percent, message)

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    # ---- step 1
    def plan_targets(self):
        plan = Planner()
        for t in unique_tracks(self.nodes):
            if present(t):
                self.target[t.uid] = place(self.root, t, plan)
        self.nodes, self.rep.left_out, not_found = playable(self.nodes)
        self.rep.missing += not_found

    def merge_with_key(self):
        old = self.old
        if old is None:
            return
        self.known_anlz = old.anlz
        key_tracks = unique_tracks(old.nodes)
        self.from_key = {id(t) for t in key_tracks}
        for t in key_tracks:
            self.target.setdefault(t.uid, key.key_path(self.root, t.path))
        new_count = keymerge.count_playlists(self.nodes)
        self.nodes, self.rep.replaced = keymerge.merge(
            self.nodes, old.nodes, lambda t: self.target.get(t.uid, "").lower()
        )
        # tracks that no playlist lists stay in the database, unless this export brings the same file
        wanted = {self.target.get(t.uid, "").lower() for t in unique_tracks(self.nodes)}
        for t in old.extra:
            where = key.key_path(self.root, t.path)
            if where.lower() not in wanted:
                self.target[t.uid] = where
                self.from_key.add(id(t))
                self.extra.append(t)
        rep = self.rep
        rep.kept = keymerge.count_playlists(old.nodes) - len(rep.replaced)
        rep.added = new_count - len(rep.replaced)
        log.info(
            "kept %d playlists already on the key, replaced %d, added %d",
            rep.kept,
            len(rep.replaced),
            rep.added,
        )

    # ---- step 2
    def build_items(self):
        for t in [*unique_tracks(self.nodes), *self.extra]:
            if not present(t):
                self.rep.missing.append(t.path or t.title)
                continue
            number = len(self.items) + 1
            if self.old is not None and id(t) in self.from_key:
                self.key_only_items.add(number)
                if unchanged(self.root, t, self.target[t.uid], self.old):
                    self.keep_uids.add(t.uid)
            tags.fill(t)
            self.items.append(pdbwrite.Item(fit(t), number, self.target[t.uid], "", 0))
        # a file can be in a playlist left out and in another
        self.rep.missing = list(dict.fromkeys(self.rep.missing))
        # the window shows the gap between this total and what was exported
        self.rep.tracks = len(self.items) + len(self.rep.missing)

    def inherited_artwork(self) -> dict[int, int]:
        return {
            it.id: self.old.art.get(it.path.lower(), 0)
            for it in self.items
            if it.id in self.key_only_items
        }

    # ---- step 3
    def assign_analysis_folders(self):
        taken_dirs: set[str] = set()
        # tracks already on the key keep their folder
        for it in self.items:
            recorded = self.known_anlz.get(it.path.lower(), "")
            if not recorded or not key.ANLZ_PATH.match(recorded):
                continue
            folder = recorded.rsplit("/", 1)[0].upper()
            if folder not in taken_dirs:
                taken_dirs.add(folder)
                it.anlz = recorded
        for it in self.items:
            if not it.anlz:
                it.anlz = anlz.anlz_dir(it.path, taken_dirs) + "/ANLZ0000.DAT"

    def reuse_or_queue(self, store):
        for it in self.items:
            same_place = it.anlz == self.known_anlz.get(it.path.lower())
            if it.track.uid in self.keep_uids and same_place:
                keep_analysis(self.root, it, self.man)
                self.rep.cached += 1
                self.rep.reused += 1
                continue
            cache_id = cache_key(it.track, it.path)
            hit = cached(store, cache_id, it.path)
            if hit:
                put_anlz(self.root, it, *hit, self.man)
                self.rep.cached += 1
            else:
                self.pending.append(_Analysis(it, cache_id))

    # ---- step 4
    # analysis reads the original audio, never the copy, so both run side by side
    def show_progress(self):
        total_work = 2 * len(self.items)
        finished = self.copied + self.analysed
        info = ""
        if self.copy_info and self.copied < len(self.items):
            info = f" ({self.copy_info})"
        count = len(self.items)
        self.say(
            COPY_ANALYSE_PERCENT * finished // total_work,
            f"Copied {self.copied}/{count}{info}, analysed {self.analysed}/{count}",
        )

    def on_copied(self, done, total, info=""):
        self.copied, self.copy_info = done, info
        self.show_progress()

    def worker_count(self) -> int:
        default = min(MAX_ANALYSIS_WORKERS, os.cpu_count() or 1)
        return max(1, min(self.processes or default, len(self.pending) or 1))

    def copy_and_analyse(self, store):
        self.analysed = self.rep.cached
        self.copies = [
            (it.track.path, os.path.join(self.root, it.path.lstrip("/")))
            for it in self.items
        ]
        with ProcessPoolExecutor(self.worker_count()) as pool:
            futures = {
                pool.submit(anlz_job, job.item.track, job.item.path): job
                for job in self.pending
            }
            try:
                self.rep.errors += copy_all(
                    self.copies, self.verify, self.cancel, self.on_copied, self.man
                )
                for future in as_completed(futures):
                    self.check_cancel()
                    self.store_result(future, futures[future], store)
            except BaseException:
                # a failed write ends the export: drop the queued analyses instead of waiting
                pool.shutdown(cancel_futures=True)
                raise

    def store_result(self, future, job: _Analysis, store):
        item = job.item
        try:
            dat, ext = future.result()
        # whatever a worker raised goes in the export report
        except Exception as err:  # noqa: BLE001
            self.rep.errors.append(f"{item.track.title}: analysis failed ({err})")
            self.failed.add(id(item))
            return
        put_anlz(self.root, item, dat, ext, self.man)
        # EXT last, an interruption never leaves half a pair in the cache
        write_atomic(str(store / f"{job.cache_id}.DAT"), dat)
        write_atomic(str(store / f"{job.cache_id}.EXT"), ext)
        self.analysed += 1
        self.show_progress()

    # ---- step 5
    # audio that never reached the key (full drive, unreadable file) must not stay in
    # the database, the player would list a track it cannot load
    def record_sizes_and_drop_lost(self):
        for it, (_, copy_file) in zip(self.items, self.copies, strict=True):
            exists = os.path.exists(copy_file)
            it.size = min(os.path.getsize(copy_file), 0xFFFFFFFF) if exists else 0
        lost = {
            id(it)
            for it, (_, copy_file) in zip(self.items, self.copies, strict=True)
            if not os.path.isfile(copy_file)
        } | {id(it) for it in self.items if id(it) in self.failed}
        if not lost:
            return
        for it in self.items:
            if id(it) in lost:
                name = it.track.title or os.path.basename(it.track.path)
                self.rep.errors.append(
                    f"{name}: its audio is not on the key, it is left out of the database"
                )
        self.items = [it for it in self.items if id(it) not in lost]
        self.rep.tracks = len(self.items) + len(self.rep.missing)

    # ---- step 6
    def write_pictures(self, inherited: dict[int, int]):
        self.say(PERCENT_ARTWORK, "Artwork...")
        keep_artwork(self.items, self.root, inherited, self.man)
        write_artwork(self.items, self.root, self.man, self.old is not None)

    # last, the old one is saved beside it first
    def write_database(self) -> tuple[str, bytes]:
        self.say(PERCENT_DATABASE, "Writing export.pdb...")
        db_folder = os.path.join(self.root, *DATABASE_DIRS)
        os.makedirs(db_folder, exist_ok=True)
        db_file = os.path.join(db_folder, DATABASE_FILE)
        self.rep.backup = keymerge.backup(db_file)
        data = pdbwrite.write(self.items, self.nodes, db_file)
        prune_artwork(self.items, self.root)
        return db_file, data

    def write_settings(self):
        folder = os.path.join(self.root, PIONEER_DIR)
        reference.copy_settings(folder, self.player)
        chosen = reference.parse_choices(self.player)
        # the choices go into a file that was there before, a file written just now has them already
        self.rep.settings_problems = settingsread.apply(folder, chosen)
        self.rep.settings_set = len(chosen)
        for problem in settingsread.check(folder):
            if problem not in self.rep.settings_problems:
                self.rep.settings_problems.append(problem)

    def record_checksums(self, db_file: str, data: bytes):
        if not self.man:
            return
        self.man.add(db_file, sha256_bytes(data))
        for name in reference.SETTINGS:
            self.man.add(os.path.join(self.root, PIONEER_DIR, name))

    def run(self) -> Report:
        self.plan_targets()
        self.merge_with_key()
        self.build_items()
        if not self.items:
            return self.rep
        inherited = self.inherited_artwork()

        store = cache_dir() / "anlz"
        store.mkdir(exist_ok=True)
        self.assign_analysis_folders()
        self.reuse_or_queue(store)
        self.copy_and_analyse(store)
        self.record_sizes_and_drop_lost()
        if not self.items:
            return self.rep

        self.write_pictures(inherited)
        db_file, data = self.write_database()
        self.write_settings()
        self.record_checksums(db_file, data)
        self.say(PERCENT_DONE, f"{len(self.items)} tracks exported")
        return self.rep

# ------------------------------------------------------------------
# old = the key as read before the export, its playlists stay and the new ones merge into them
def export(
    nodes: list[Node],
    root: str,
    verify: bool = False,
    processes: int = 0,
    progress=None,
    cancel: threading.Event | None = None,
    man: Manifest | None = None,
    old: key.KeyData | None = None,
    player: str = "",
) -> Report:
    report = Report(path=root)
    run = _Run(
        nodes, root, verify, processes, progress, cancel, man, old, report, player=player
    )
    return run.run()
