# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Registry of export formats mapped to their writer modules, and the entry point that runs an export.

from __future__ import annotations

import importlib
import os
import sys

from ..model import Node
from . import manifest
from .files import Report

# the writer of each format: module of this package and the function that does the export
WRITERS = {
    "Rekordbox XML": ("rbxml", "export"),
    "M3U": ("m3u", "export"),
    "Traktor NML": ("nml", "export"),
    "VirtualDJ": ("vdj", "export"),
    "Mixxx": ("mixxx", "export"),
    "iTunes XML": ("itunes", "export"),
    "CSV": ("lists", "export_csv"),
    "JSON": ("lists", "export_json"),
    "PLS": ("lists", "export_pls"),
    "XSPF": ("lists", "export_xspf"),
}

# ----------------------------------------------------------------
# what the CDJ database on the key at out holds (folders, playlists, tracks, playlist
# names), None when there is none. With a cancel event it raises Cancelled once it is set.
# All of these live in cdj/keymerge.py
def key_summary(out: str, cancel=None):
    from .cdj import keymerge

    return keymerge.key_summary(out, cancel)

# --------------------------------------------
# the database file of the key at out with its size and modification time, None when
# there is none.
def key_stamp(out: str):
    from .cdj import keymerge

    return keymerge.key_stamp(out)

# the playlists of an export merged into those already on a key: (merged tree, names of
# the playlists that were replaced)
def merge(new_nodes: list[Node], existing_nodes: list[Node], ident=None):
    from .cdj import keymerge

    return keymerge.merge(new_nodes, existing_nodes, ident)

# the playlists on the key that exporting nodes would replace, from key_summary alone
def merge_names(nodes: list[Node], summary) -> list[str]:
    from .cdj import keymerge

    return keymerge.merge_names(nodes, summary)

# ------------------------------------------
# Runs the export, then computes checksums of everything written. With verify_copy the export is read back from the drive and compared.
# A CDJ/USB export keeps the playlists on the key (cfg["key_mode"] "keep", the default) and merges into them, or replaces the database ("replace").
# A key whose database cannot be read is refused in keep mode with a ValueError, before anything is written.
def run(
    fmt: str, nodes: list[Node], out: str, cfg: dict, progress=None, cancel=None
) -> Report:
    # a relative folder would leave relative audio paths in the lists that carry them
    out = os.path.abspath(out)
    if sys.platform == "win32":
        # Windows drops a trailing space or dot when it creates a folder, the path with
        # them no longer led there
        out = out.rstrip(" .") or out
    os.makedirs(out, exist_ok=True)
    if fmt == "Mixxx":
        # before the Manifest below removes the checksums of the export that is there
        from . import mixxx

        mixxx.require_free(out)
    old = None
    if fmt == "CDJ/USB":
        from .cdj import keymerge

        if keymerge.keeping(cfg):
            say = progress or (lambda pct, msg: None)

            def reading(pct, msg):
                # the key is read before anything is written, so Cancel leaves it as it was
                if cancel is not None and cancel.is_set():
                    raise manifest.Cancelled()
                say(0, msg)

            old = keymerge.read_existing(out, reading)
    man = manifest.Manifest(out)
    rep = None
    if fmt == "CDJ/USB":
        from .cdj import usb

        rep = usb.export(
            nodes,
            out,
            cfg["verify_copy"],
            cfg["anlz_processes"],
            progress,
            cancel,
            man,
            old,
            cfg.get("player_settings", ""),
        )
    if rep is None:
        module, name = WRITERS[fmt]
        writer = getattr(importlib.import_module(f".{module}", __package__), name)
        rep = writer(
            nodes, out, cfg["copy_music"], cfg["verify_copy"], progress, cancel, man
        )
    rep.manifest = man.save()
    if cfg["verify_copy"]:
        chk = manifest.verify(out, progress, cancel, lenient=man.carried)
        rep.verified, rep.verified_size = chk.ok, chk.size
        rep.player_changed = chk.modified
        rep.errors += [f"{p}: differs from what was written" for p in chk.damaged]
        rep.errors += [f"{p}: missing after the export" for p in chk.missing]
        if chk.error:
            rep.errors.append(chk.error)
    return rep
