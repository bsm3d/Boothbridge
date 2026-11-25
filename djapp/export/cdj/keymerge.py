# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Reads the playlists already on a CDJ key and merges new ones into them without losing existing ones.

# An export reads the key first and merges into it: a fresh export.pdb would lose the playlists already there.
# Layout from rekordbox keys and crate-digger research. The merge is pure tree handling with no I/O, so a window can show the outcome before anything is written.

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ...model import Node, Track, playlists
from ...sources import check_size, key
from ..manifest import Cancelled, write_atomic
from . import pdbread

BACKUP = "export.pdb.bak"
MODES = ("keep", "replace")
# block size when the read can be cancelled
CHUNK = 1 << 20
SEP = " / "
# rekordbox files beside export.pdb, never written here
FOREIGN = ("exportext.pdb", "exportlibrary.db")

# ----------------------------------------------------------------
# keep is the settings.DEFAULTS value, anything but "replace" counts as keep. A config
# with no key_mode at all (built by hand) gets the old behaviour: replace.
def keeping(cfg: dict) -> bool:
    if "key_mode" not in cfg:
        return False
    mode = str(cfg["key_mode"]).strip().lower()
    return mode != "replace"

@dataclass
class KeySummary:
    folders: int
    playlists: int
    tracks: int  # rows in the Tracks table
    names: list[str] = field(default_factory=list)
    # False: a database exists but could not be followed
    readable: bool = True
    error: str = ""
    others: list[str] = field(default_factory=list)

# --------------------------------------------------
# path, size, mtime: stat only, enough to know if an earlier summary is still valid
def key_stamp(out: str) -> tuple[str, int, int] | None:
    located = key.locate(out)
    if located is None:
        return None
    db_path = located[1]
    try:
        info = os.stat(db_path)
    except OSError:
        return None
    return db_path, info.st_size, info.st_mtime_ns

# ------------------------------------------------------------
# with a cancel event the file is read by blocks, so a slow drive can be abandoned midway
def read_db(db: str, cancel: threading.Event | None) -> bytes:
    with open(db, "rb") as handle:
        if cancel is None:
            return handle.read()
        blocks = []
        while True:
            if cancel.is_set():
                raise Cancelled()
            block = handle.read(CHUNK)
            if not block:
                return b"".join(blocks)
            blocks.append(block)

# ------------------------------------------------------------
# real names on the key (exportExt.pdb, exportLibrary.db), an export only changes export.pdb
def foreign_files(db: str) -> list[str]:
    try:
        entries = os.listdir(os.path.dirname(db))
    except OSError:
        return []
    return sorted(name for name in entries if name.lower() in FOREIGN)

# ------------------------------------------------------------
# flat display names in player order, Folder / Sub / Name. depth says how many of the folders met still enclose the entry.
def _flatten_outline(placed) -> tuple[int, list[str]]:
    enclosing: list[str] = []
    display_names: list[str] = []
    folder_count = 0
    for node, depth in placed:
        del enclosing[depth:]
        if node.folder:
            folder_count += 1
            enclosing.append(node.name)
        else:
            display_names.append(SEP.join([*enclosing, node.name]))
    return folder_count, display_names

# ------------------------------------------------------------
# None without a database. An unreadable one does not raise: readable=False and the
# reason in error. Cheap on purpose, only tracks and the playlist tree are decoded.
# cancel is checked between file blocks and table pages.
def key_summary(out: str, cancel: threading.Event | None = None) -> KeySummary | None:
    located = key.locate(out)
    if located is None:
        return None
    db_path = located[1]
    try:
        check_size(db_path, key.MAX_DB)
        raw = read_db(db_path, cancel)
        parsed = pdbread.read(raw, {pdbread.TRACKS, pdbread.PL_TREE}, cancel)
        track_rows = len(pdbread.rows(parsed, pdbread.TRACKS))
        placed, _ = key.outline(pdbread.tree(parsed))
    except (OSError, ValueError) as err:  # PdbError is a ValueError
        reason = f"{'/'.join(key.DB)} cannot be read: {err}"
        return KeySummary(0, 0, 0, [], False, reason, foreign_files(db_path))
    folder_count, display_names = _flatten_outline(placed)
    return KeySummary(
        folder_count,
        len(display_names),
        track_rows,
        display_names,
        others=foreign_files(db_path),
    )

# ------------------------------------------------------------
# a read failure is re-raised with advice, nothing on the key has been modified at that point
def read_existing(out: str, progress=None) -> key.KeyData | None:
    if key.locate(out) is None:
        return None
    try:
        return key.read(out, "", progress)
    except key.KeyReadError as err:
        raise key.KeyReadError(
            f"{err}. Nothing was written to the key. Choose replace to overwrite "
            f"its database, or restore {BACKUP} if there is one."
        ) from err

# ------------------------------------------------------------
# export.pdb.bak beside it, an older one is overwritten. Atomic writer: whole or absent. Returns "" when there was no database.
def backup(db: str) -> str:
    if not os.path.isfile(db):
        return ""
    with open(db, "rb") as handle:
        content = handle.read()
    copy_path = db + ".bak"
    write_atomic(copy_path, content)
    return copy_path

def count_playlists(nodes: list[Node]) -> int:
    return sum(1 for _ in playlists(nodes))

def _same_entry(a: Node, b: Node) -> bool:
    return a.is_folder == b.is_folder and a.name.casefold() == b.name.casefold()

# ------------------------------------------------------------
# Returns (merged tree, names replaced as Folder / Name). Neither input is modified.
# Same folder path and name (case ignored): the new playlist takes the old place, folders merge level by level. Other new entries go after the existing ones.
# A track is identified by ident(track) (lower case path by default); on a shared identity the new track wins, in the playlists that stay too.
def merge(
    new_nodes: list[Node],
    existing_nodes: list[Node],
    ident: Callable[[Track], str] | None = None,
) -> tuple[list[Node], list[str]]:
    ident = ident or (lambda track: track.path.lower())

    # pass 1: one track per identity, new ones first
    representative: dict[str, Track] = {}
    for tree in (new_nodes, existing_nodes):
        for playlist in playlists(tree):
            for track in playlist.tracks:
                ident_value = ident(track)
                if ident_value:
                    representative.setdefault(ident_value, track)

    def unify(track: Track) -> Track:
        ident_value = ident(track)
        if not ident_value:
            return track
        return representative.get(ident_value, track)

    def duplicate(node: Node) -> Node:
        if node.is_folder:
            return Node(
                node.kind,
                node.name,
                children=[duplicate(child) for child in node.children],
                uuid=node.uuid,
                query=node.query,
            )
        return Node(
            node.kind,
            node.name,
            tracks=[unify(track) for track in node.tracks],
            uuid=node.uuid,
            query=node.query,
        )

    replaced_names: list[str] = []

    # pass 2: one level of both trees, recurse into matching folders
    def merge_level(
        incoming: list[Node], present: list[Node], path: list[str]
    ) -> list[Node]:
        result = [duplicate(node) for node in present]
        used: set[int] = set()
        for node in incoming:
            slot = next(
                (
                    index
                    for index, candidate in enumerate(present)
                    if index not in used and _same_entry(candidate, node)
                ),
                None,
            )
            if slot is None:
                result.append(duplicate(node))
                continue
            used.add(slot)
            match = present[slot]
            if node.is_folder:
                result[slot] = Node(
                    match.kind,
                    match.name,
                    children=merge_level(
                        node.children, match.children, [*path, match.name]
                    ),
                    uuid=match.uuid,
                    query=match.query,
                )
            else:
                replaced_names.append(SEP.join([*path, match.name]))
                result[slot] = duplicate(node)
        return result

    return merge_level(new_nodes, existing_nodes, []), replaced_names

# ------------------------------------------------------------
# which playlists on the key an export would replace, from the summary alone (no second read of the key). Same rule as merge.
def merge_names(nodes: list[Node], summary: KeySummary) -> list[str]:
    by_folded: dict[str, list[str]] = {}
    for name in summary.names:
        by_folded.setdefault(name.casefold(), []).append(name)
    replaced: list[str] = []

    def visit(level: list[Node], path: list[str]):
        for node in level:
            if node.is_folder:
                visit(node.children, [*path, node.name])
                continue
            candidates = by_folded.get(SEP.join([*path, node.name]).casefold())
            if candidates:
                replaced.append(candidates.pop(0))

    visit(nodes, [])
    return replaced
