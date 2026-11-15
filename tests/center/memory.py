# repeats one scenario of the application for N cycles and watches what grows.
#
# After every cycle the process is measured: memory (resident and private), the Python heap
# (tracemalloc), live widgets and QObjects, threads, open handles and child processes. The first
# cycles are warm-up (caches, lazy imports, first windows) and do not count. A least-squares line
# through the measured cycles says whether something keeps growing

from __future__ import annotations

import contextlib
import gc
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from . import header, save_run

KB = 1024.0


# --------------------------------------------------------
# what counts as growth. Memory is judged per cycle by the slope of the fitted line.
#
# Windows and the allocator move private memory by a few hundred KB from one cycle to the
# next without any leak, so memory needs 256 KB per cycle and 4 MB of net growth before it
# matters (the allocator grows in steps). The Python heap is far quieter (32 KB). A widget, a thread or a child process that is never freed is a
# whole object, so counts are judged on the net growth over the measured cycles (1 for
# widgets, threads and children, 10 handles) together with a slope that proves it keeps
# going. A fit with an R squared under min_r2 is noise and is ignored
@dataclass
class Limits:
    memory_kb: float = 256.0
    memory_net_kb: float = 4096.0
    heap_kb: float = 32.0
    widgets: float = 1.0
    qobjects: float = 5.0
    threads: float = 1.0
    handles: float = 10.0
    children: float = 1.0
    min_r2: float = 0.5
    slope_count: float = 0.1
    handle_slope: float = 0.1


# ------------------------------------------------------------------------
# slope per cycle and R squared of the least-squares line through ys
def fit(ys: list[float]) -> tuple[float, float]:
    n = len(ys)
    if n < 2:
        return 0.0, 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    if syy == 0:
        return slope, 1.0
    return slope, (sxy * sxy) / (sxx * syy)


# --------------------------------------------------------
# live QWidgets (C++ side), then QObjects and QWidgets still referenced from Python
def count_objects() -> tuple[int, int, int]:
    if "PySide6.QtWidgets" not in sys.modules:
        return 0, 0, 0
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication, QWidget
    live = len(QApplication.allWidgets()) if QApplication.instance() else 0
    objects = widgets = 0
    for o in gc.get_objects():
        if isinstance(o, QObject):
            objects += 1
            if isinstance(o, QWidget):
                widgets += 1
    return live, objects, widgets


# --------------------------------------------------------
def sample(proc: psutil.Process, cycle: int) -> dict:
    info = proc.memory_info()
    try:
        private = proc.memory_full_info().uss
    except (psutil.Error, AttributeError):
        private = getattr(info, "private", info.vms)
    handles = proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()
    live, objects, py_widgets = count_objects()
    names = []
    try:
        found = proc.children(recursive=True)
        children = len(found)
        for child in found[:5]:
            with contextlib.suppress(psutil.Error):
                names.append(f"{child.name()} {' '.join(child.cmdline()[1:3])[:60]}")
    except psutil.Error:
        children = 0
    return {"cycle": cycle, "rss": info.rss, "private": private,
            "heap": tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0,
            "widgets": live, "py_widgets": py_widgets, "qobjects": objects,
            "threads": threading.active_count(), "os_threads": proc.num_threads(), "handles": handles,
            "children": children, "child_names": names}


# --------------------------------------------------------
# evidence per metric over the measured cycles, and the overall call
def verdict(rows: list[dict], warmup: int, limits: Limits) -> dict:
    kept = [r for r in rows if r["cycle"] >= warmup]
    evidence, table = [], {}
    if len(kept) < 3:
        return {"verdict": "indéterminé", "evidence": ["moins de 3 cycles mesurés"], "metrics": {}, "measured": len(kept)}
    checks = [("private", "mémoire privée", limits.memory_kb * KB, KB, "Ko"),
              ("rss", "mémoire résidente", limits.memory_kb * KB, KB, "Ko"),
              ("heap", "tas Python (tracemalloc)", limits.heap_kb * KB, KB, "Ko"),
              ("widgets", "widgets vivants", limits.slope_count, 1, ""),
              ("qobjects", "QObject en mémoire", limits.slope_count, 1, ""),
              ("threads", "threads Python", limits.slope_count, 1, ""),
              ("os_threads", "threads système", limits.slope_count, 1, ""),
              ("handles", "poignées ouvertes", limits.handle_slope, 1, ""),
              ("children", "processus enfants", 0.05, 1, "")]
    net_limit = {"private": limits.memory_net_kb * KB, "rss": limits.memory_net_kb * KB, "widgets": limits.widgets, "qobjects": limits.qobjects, "threads": limits.threads,
                 "os_threads": limits.threads, "handles": limits.handles, "children": limits.children}
    for key, label, slope_limit, unit, suffix in checks:
        ys = [float(r[key]) for r in kept]
        slope, r2 = fit(ys)
        net = ys[-1] - ys[0]
        table[key] = {"slope": slope, "r2": r2, "net": net, "first": ys[0], "last": ys[-1]}
        growing = slope > slope_limit and r2 >= limits.min_r2
        if key in net_limit:
            growing = growing and net >= net_limit[key]
        if growing:
            evidence.append(f"{label} : +{slope / unit:.1f} {suffix}/cycle (R2 {r2:.2f}), "
                            f"{ys[0] / unit:.0f} -> {ys[-1] / unit:.0f} {suffix}".replace("  ", " "))
    return {"verdict": "fuite probable" if evidence else "stable", "evidence": evidence, "metrics": table,
            "measured": len(kept)}


# ------------------------------------------------------------------------
def top_allocations(first, last, count: int = 10) -> list[dict]:
    if first is None or last is None:
        return []
    out = []
    for stat in last.compare_to(first, "lineno")[:count]:
        frame = stat.traceback[0]
        out.append({"where": f"{Path(frame.filename).name}:{frame.lineno}", "size_diff": stat.size_diff,
                    "count_diff": stat.count_diff, "size": stat.size})
    return out


# --------------------------------------------------------
# runs cycle(i) cycles times and returns rows, verdict and allocation differences
def measure(cycle, cycles: int, warmup: int = -1, limits: Limits | None = None, log=None, trace: bool = True) -> dict:
    log = log or (lambda text: None)
    limits = limits or Limits()
    if warmup < 0:
        warmup = min(max(1, cycles // 10), 5) if cycles > 3 else 1
    proc = psutil.Process()
    if trace and not tracemalloc.is_tracing():
        tracemalloc.start(1)
    rows, first, error = [], None, ""
    started = time.monotonic()
    for i in range(cycles):
        try:
            cycle(i)
        except Exception as exc:  # a scenario that breaks is a result, with its trace  # noqa: BLE001
            error = f"cycle {i} : {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            break
        gc.collect()
        if "PySide6.QtWidgets" in sys.modules:
            from PySide6.QtCore import QCoreApplication, QEvent
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
        row = sample(proc, i)
        rows.append(row)
        if i == warmup - 1 and tracemalloc.is_tracing():
            first = tracemalloc.take_snapshot()
        log(f"cycle {i + 1:3d}/{cycles}  privé {row['private'] / 1048576:7.1f} Mo  tas {row['heap'] / 1048576:6.2f} Mo  "
            f"widgets {row['widgets']:4d}  threads {row['threads']:2d}  poignées {row['handles']:4d}  "
            f"enfants {row['children']}" + ("  (échauffement)" if i < warmup else "")
            + ("  " + "; ".join(row["child_names"]) if row["child_names"] else ""))
    last = tracemalloc.take_snapshot() if tracemalloc.is_tracing() else None
    result = verdict(rows, warmup, limits)
    result.update(rows=rows, warmup=warmup, limits=asdict(limits), seconds=round(time.monotonic() - started, 2),
                  allocations=top_allocations(first, last), error=error)
    if error:
        result["verdict"] = "erreur"
    return result


# --------------------------------------------------------
# scenarios
# --------------------------------------------------------

# ------------------------------------------------------
# what the scenarios share: a temp folder, the synthetic collection, one main window
class Ctx:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.world = None
        self.col = None
        self.n = 0

    def start_world(self):
        if self.world is None:
            from .world import World
            self.world = World(self.root, 1)
            self.world.start()
            self.col = self.world.col
        return self.world

    def collection(self):
        if self.col is None:
            from . import synth
            self.col = synth.build(self.root / "plain", 1)
        return self.col

    def close(self):
        if self.world is not None:
            self.world.stop()
            self.world = None


def pump(ms: int = 0):
    from .world import pump as run
    run(ms)


def playlist_node(ctx):
    from djapp.model import playlists
    main = ctx.start_world().main
    return next(p for p in playlists(main.nodes) if len(p.tracks) >= 3 and p.name != "Empty")

# --------------------------------------------------------
def open_playlist_window(ctx):
    from djapp.ui.details import PlaylistWindow
    from PySide6.QtCore import Qt
    main = ctx.start_world().main
    node = playlist_node(ctx)
    for item in main.tree.findItems(node.name, Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive):
        if item.data(0, Qt.ItemDataRole.UserRole) is node:
            main.details(item)
            break
    pump(30)
    return next(w for w in main.findChildren(PlaylistWindow) if w.node is node and w.isVisible())

def close_window(window):
    window.close()
    window.deleteLater()
    pump(20)


# --------------------------------------------------------
def open_close_playlist(ctx, i):
    window = open_playlist_window(ctx)
    window.table.setCurrentItem(window.table.topLevelItem(0))
    pump(20)
    close_window(window)


# --------------------------------------------------------
def open_close_cue_editor(ctx, i):
    from djapp.ui import cuehistory
    from djapp.ui.details import C_CUES
    from djapp.ui.timeline import Timeline
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    window = open_playlist_window(ctx)
    item = window.table.topLevelItem(0)
    window.table.setCurrentItem(item)
    window.double(item, C_CUES)
    pump(30)
    timeline = next(w for w in window.findChildren(Timeline) if w.isVisible())
    track = item.data(0, Qt.ItemDataRole.UserRole)
    for k in range(3):
        cuehistory.mark(track)
        track.bpm = 120.0 + k
        track.grid = float(k * 10)
    for _ in range(2):
        cuehistory.undo(track)
    cuehistory.redo(track)
    QTest.keyClick(timeline, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    pump(20)
    timeline.close()
    timeline.deleteLater()
    close_window(window)


# --------------------------------------------------------
def load_collection(ctx, i):
    from djapp.ui.main_window import load_job
    col = ctx.collection()
    for path in (col.nml, col.rbxml, col.project, col.m3u):
        nodes, missing, extra = load_job(str(path), str(col.audio))
        del nodes, missing, extra

# --------------------------------------------------------
def export_cdj(ctx, i):
    from djapp import export, settings
    from djapp.export import manifest
    from djapp.model import Node
    col = ctx.collection()
    cfg = dict(settings.DEFAULTS, verify_copy=True, anlz_processes=1, key_mode="replace", key_ask=False)
    tracks = [t for t in col.nodes[0].tracks if "gone" not in t.path][:3]
    for k, t in enumerate(tracks):
        t.bpm = 120.0 + i * 0.01 + k
        t.grid = 0.0
    out = str(ctx.root / "key")
    report = export.run("CDJ/USB", [Node("playlist", "Test", tracks=tracks)], out, cfg)
    if report.errors:
        raise RuntimeError("export: " + "; ".join(report.errors[:3]))
    check = manifest.verify(out)
    if not check.good:
        raise RuntimeError(f"verify: {check.summary()}")


# the same key again and again in keep mode: merge, check the checksums, read it back. The
# number of playlists must not grow, three names are used in turn
def merge_key(ctx, i):
    from djapp import export, settings
    from djapp.export import manifest
    from djapp.model import Node, playlists
    from djapp.sources import key
    col = ctx.collection()
    cfg = dict(settings.DEFAULTS, verify_copy=True, anlz_processes=1, key_mode="keep", key_ask=False)
    tracks = [t for t in col.nodes[0].tracks if "gone" not in t.path][:3]
    out = str(ctx.root / "mergekey")
    report = export.run("CDJ/USB", [Node("playlist", f"Set {i % 3}", tracks=tracks[i % 3:])], out, cfg)
    if report.errors:
        raise RuntimeError("merge: " + "; ".join(report.errors[:3]))
    check = manifest.verify(out)
    if not check.good:
        raise RuntimeError(f"verify: {check.summary()}")
    summary = export.key_summary(out)
    data = key.read(out)
    names = sorted(p.name for p in playlists(data.nodes))
    if summary.playlists != len(names) or len(names) > 3 or len(set(names)) != len(names):
        raise RuntimeError(f"the key holds {names} after {i + 1} merges")


# back up, check, clear and restore a small analysis cache
def cache_backup(ctx, i):
    import hashlib
    import os

    from djapp import cachebackup
    cache = ctx.root / "cachebackup"
    for sub, ext in (("anlz", "DAT"), ("anlz", "EXT"), ("wave", "bin")):
        (cache / sub).mkdir(parents=True, exist_ok=True)
        for k in range(8):
            name = hashlib.sha1(f"{k}".encode()).hexdigest() + "." + ext
            (cache / sub / name).write_bytes(os.urandom(2000))
    archive = ctx.root / "cache-backup.zip"
    saved = cachebackup.create(cache, archive)
    checked = cachebackup.verify(archive)
    cleared = cachebackup.clear(cache)
    restored = cachebackup.restore(archive, cache)
    archive.unlink()
    Path(str(archive) + ".sha256").unlink(missing_ok=True)
    if not (saved.files == checked.files == cleared.files == restored.files == 24):
        raise RuntimeError(f"cache backup: {saved.files}, {checked.files}, {cleared.files}, {restored.files} files")


# --------------------------------------------------------


# --------------------------------------------------------
def music_browser(ctx, i):
    main = ctx.start_world().main
    main.open_music_browser()
    browser = main.music_browser
    end = time.monotonic() + 15
    while browser.job is not None and time.monotonic() < end:
        pump(20)
    pump(20)
    browser.close()
    pump(20)


# --------------------------------------------------------
def backup_zip(ctx, i):
    from djapp import backup, settings
    from djapp.model import Node
    from djapp.sources import project
    col = ctx.collection()
    tracks = [t for t in col.nodes[0].tracks if "gone" not in t.path][:4]
    doc = project.dump([Node("playlist", "Backup", tracks=tracks)], dict(settings.DEFAULTS, music_root=str(col.audio)))
    folder = ctx.root / "backups"
    folder.mkdir(exist_ok=True)
    archive = folder / f"b{i}.zip"
    backup.create(str(archive), doc)
    backup.verify(str(archive))
    backup.restore(str(archive), str(folder / f"restored{i}"))
    shutil.rmtree(folder / f"restored{i}", ignore_errors=True)
    archive.unlink()
    Path(str(archive) + ".sha256").unlink(missing_ok=True)




# --------------------------------------------------------
def audio_check(ctx, i):
    from djapp import audiocheck
    col = ctx.collection()
    results = audiocheck.scan([str(p) for p in col.files[:3]], "", False)
    if len(results) != 3:
        raise RuntimeError("audio check returned the wrong number of results")


# --------------------------------------------------------
# the small modal windows, opened and closed the way the menus do: About, Usage with a search,
# Options both ways (it starts a job that counts the cache, closing it at once must not leave it
# running) and the log
def modal_basic(ctx, i):
    from djapp.ui.dialogs import About, Options, Usage
    main = ctx.start_world().main
    for cls in (About, Usage):
        d = cls(main)
        d.show()
        pump(20)
        if cls is Usage:
            d.find.setText("export")
            d.search(restart=True)
        d.accept()
        d.deleteLater()
        pump(10)
    for finish in ("reject", "accept", "reject"):
        o = Options(main.cfg, main)
        o.show()
        if finish == "accept":
            pump(60)
        getattr(o, finish)()
        o.deleteLater()
        pump(10)
    main.logwin.show()
    pump(10)
    main.logwin.hide()


# --------------------------------------------------------
# the window that asks what to do with the playlists on a key, answered each way
def key_choice(ctx, i):
    from types import SimpleNamespace

    from djapp.ui.dialogs import KeyChoice
    main = ctx.start_world().main
    readable = SimpleNamespace(folders=1, playlists=3, tracks=40, names=["A", "B", "C / D"], readable=True,
                               others=["exportExt.pdb", "exportLibrary.db"])
    unreadable = SimpleNamespace(readable=False, others=[])
    for summary, replaced, press in ((readable, ["B"], "keep_btn"), (readable, [], "replace_btn"),
                                     (readable, [], "cancel_btn"), (unreadable, [], "replace_btn")):
        d = KeyChoice(summary, replaced, main, "no")
        d.show()
        pump(10)
        getattr(d, press).click()
        d.deleteLater()
        pump(10)


# ------------------------------------------------------------------
# Check collection, with the folder scan that relocates files: closed while it runs, then after
def check_collection(ctx, i):
    from unittest import mock

    from djapp.ui.dialogs import CheckCollection
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    main = ctx.start_world().main
    with mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "warning"), \
            mock.patch.object(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("", False))):
        d = CheckCollection(main.nodes, str(ctx.collection().audio), main)
        d.show()
        pump(20)
        d.auto()
        d.close()  # refused while the scan runs
        d.reject()
        end = time.monotonic() + 15
        while d.scan is not None and time.monotonic() < end:
            pump(10)
        if d.scan is not None:
            raise RuntimeError("the folder scan did not end")
        pump(30)
        d.accept()
        d.deleteLater()
        pump(10)


# Verify audio files: a scan to the end, then a scan cancelled at once. The checks run in their own
# processes, none may be left
def audio_check_panel(ctx, i):
    from djapp.ui.audiocheck import AudioCheck
    main = ctx.start_world().main
    for cancel in (False, True):
        panel = AudioCheck(main.nodes, str(ctx.collection().audio), main)
        panel.show()
        pump(20)
        panel.paths = panel.paths[:4]
        panel.start()
        if cancel:
            pump(50)
            panel.reject()
        end = time.monotonic() + 40
        while panel.job is not None and time.monotonic() < end:
            pump(10)
        if panel.job is not None:
            raise RuntimeError("the audio check did not end")
        panel.accept()
        panel.deleteLater()
        pump(10)

# --------------------------------------------------------
# the USB formatter with the drives and the writing replaced: listed, ticked, started, finished
def format_dialog(ctx, i):
    from unittest import mock

    from djapp import drives
    from djapp.ui.formatter import FormatDialog
    from PySide6.QtWidgets import QMessageBox
    main = ctx.start_world().main
    key = drives.Drive("9", "Test key", 8_000_000_000, mounts=["Z:\\"], volumes=["TRAKTOR (FAT32, 8.0 GB)"])

    def write(d, label, progress=None, cancel=None):
        for pct in (10, 60, 100):
            progress(pct, "Writing")
        return "Z:\\"

    with mock.patch.object(drives, "list_drives", lambda progress=None, cancel=None: [key]), \
            mock.patch.object(drives, "format_drive", write), \
            mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes), \
            mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "critical"):
        d = FormatDialog(main)
        d.show()
        end = time.monotonic() + 10
        while d.busy and time.monotonic() < end:
            pump(10)
        d.sure.setChecked(True)
        d.start()
        while d.busy and time.monotonic() < end:
            pump(10)
        pump(30)
        d.reject()
        d.deleteLater()
        pump(10)


# --------------------------------------------------------
# ID Tag: opened, a field changed, applied, then written to a copy of the audio on a Job
def tag_editor(ctx, i):
    import shutil
    from unittest import mock

    from djapp.model import Track
    from djapp.ui.tageditor import TagEditor
    from PySide6.QtWidgets import QMessageBox
    main = ctx.start_world().main
    source = ctx.collection().files[0]
    copy = ctx.root / f"tag{i}{source.suffix}"
    shutil.copy(source, copy)
    track = Track(title="T", artist="A", path=str(copy), duration=1.0)
    with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
            mock.patch.object(QMessageBox, "warning"), mock.patch.object(QMessageBox, "information"), \
            mock.patch.object(QMessageBox, "critical"):
        editor = TagEditor(track, str(copy), "Open Key", main, engine=main.engine)
        editor.show()
        pump(30)
        if editor.write_button.isEnabled():
            editor.write_button.click()
            end = time.monotonic() + 15
            while editor.job is not None and time.monotonic() < end:
                pump(10)
        editor.reject()
        editor.deleteLater()
        pump(20)
    for leftover in ctx.root.glob(f"tag{i}*"):
        leftover.unlink(missing_ok=True)


# ------------------------------------------------------------------
# the shared player: a track played, stopped, played again and the window closed while it plays
def player_engine(ctx, i):
    main = ctx.start_world().main
    paths = [str(p) for p in ctx.collection().files[:2]]
    main.engine.play(paths[0])
    pump(120)
    main.engine.stop()
    main.engine.play(paths[1])
    pump(60)
    window = open_playlist_window(ctx)
    main.engine.play(paths[0])
    pump(60)
    close_window(window)
    main.engine.stop()
    pump(20)


# ------------------------------------------------------------
# A CDJ export with its analysis in worker processes, cancelled in the middle: the pool, the threads
# and the files must be gone afterwards
def export_cancel(ctx, i):
    from djapp import export, settings
    from djapp.export.manifest import Cancelled
    from djapp.model import Node
    col = ctx.collection()
    tracks = [t for t in col.nodes[0].tracks if "gone" not in t.path][:6]
    out = ctx.root / "cancelkey"
    stop, count = threading.Event(), [0]

    def progress(pct, message):
        count[0] += 1
        if count[0] == 6:
            stop.set()

    cfg = dict(settings.DEFAULTS, verify_copy=True, anlz_processes=2, key_mode="replace", key_ask=False)
    try:
        export.run("CDJ/USB", [Node("playlist", "Cancel", tracks=tracks)], str(out), cfg, progress, stop)
    except Cancelled:
        pass
    left = [p.name for p in out.rglob("*") if p.is_file() and (p.suffix in (".tmp", ".part") or p.name.startswith(".bb-"))]
    if left:
        raise RuntimeError(f"files left behind by a cancelled export: {left[:3]}")
    shutil.rmtree(out, ignore_errors=True)

# --------------------------------------------------------
# A key opened from the window (the load job), its summary read next to Output (the probe job), and the
# Output field changed while that runs
def key_open_probe(ctx, i):
    from djapp import export, settings
    from djapp.model import Node
    world = ctx.start_world()
    main = world.main
    col = ctx.collection()
    key = ctx.root / "probekey"
    if not (key / "PIONEER").exists():
        tracks = [t for t in col.nodes[0].tracks if "gone" not in t.path][:3]
        export.run("CDJ/USB", [Node("playlist", "P", tracks=tracks)], str(key),
                   dict(settings.DEFAULTS, anlz_processes=1, key_mode="replace", key_ask=False))
    main.out.setText(str(key))
    pump(30)
    main.out.setText(str(ctx.root))
    main.out.setText(str(key))
    world.wait_idle(15)
    main.dirty = False
    main.open_key(str(key))
    world.wait_idle(15)
    main.dirty = False
    world.load(col.project)



SCENARIOS = [
    ("open_close_playlist", "Ouvrir et fermer une fenêtre playlist", open_close_playlist),
    ("open_close_cue_editor", "Ouvrir et fermer le Cue editor, avec modifications et Undo", open_close_cue_editor),
    ("load_collection", "Charger une collection (NML, rekordbox, projet, M3U) puis l'abandonner", load_collection),
    ("export_cdj", "Exporter quelques morceaux vers une clé CDJ temporaire et la vérifier", export_cdj),
    ("merge_key", "Clé CDJ : fusionner sans cesse dans la même clé, la vérifier et la relire", merge_key),
    ("cache_backup", "Cache d'analyses : sauvegarder, vérifier, vider, restaurer", cache_backup),
    ("music_browser", "Navigateur musical : scanner la bibliothèque puis fermer", music_browser),
    ("backup_zip", "Backup ZIP : créer, vérifier, restaurer", backup_zip),
    ("audio_check", "Vérification des fichiers audio", audio_check),
    ("modal_basic", "Fenêtres modales simples : À propos, Aide, Options, journal", modal_basic),
    ("key_choice", "Question sur les playlists d'une clé, toutes les réponses", key_choice),
    ("check_collection", "Vérifier la collection : analyse de dossier, fermeture pendant le travail", check_collection),
    ("audio_check_panel", "Vérifier les fichiers audio : fenêtre, analyse et annulation", audio_check_panel),
    ("format_dialog", "Formatage USB : liste, confirmation, écriture simulée", format_dialog),
    ("tag_editor", "ID Tag : ouverture, écriture sur une copie, fermeture", tag_editor),
    ("player_engine", "Lecteur : lecture, arrêt, fermeture de la fenêtre qui joue", player_engine),
    ("export_cancel", "Export CDJ annulé au milieu : processus, threads et fichiers", export_cancel),
    ("key_open_probe", "Clé : lecture du résumé et ouverture depuis la fenêtre", key_open_probe),
]


# fn(ctx, cycle_number). One line in SCENARIOS does the same for a built-in scenario
def register(name: str, title: str, fn):
    SCENARIOS[:] = [s for s in SCENARIOS if s[0] != name]
    SCENARIOS.append((name, title, fn))


def names() -> list[str]:
    return [s[0] for s in SCENARIOS]


def find(name: str):
    for entry in SCENARIOS:
        if entry[0] == name:
            return entry
    raise ValueError("unknown scenario: " + name + " (known: " + ", ".join(names()) + ")")


# --------------------------------------------------------
# command line
# --------------------------------------------------------

# Growth that is known, bounded and not ours, so a scenario may tolerate it without hiding the rest.
SCENARIO_LIMITS = {
    # Cancelling while the analysis process is still starting leaves the duplicate of its pipe end open
    # (CPython multiprocessing, popen_spawn_win32). One handle per cancel, nothing when the check ends.
    "audio_check_panel": {"handle_slope": 1.5, "handles": 40.0},
    # The ffmpeg backend of Qt 6.11 keeps about one handle per source it opened. The windows backend
    # does not, and it is not the one the program ships with.
    "player_engine": {"handle_slope": 3.6, "handles": 120.0},
}


# --------------------------------------------------------
# limits() with the values given as --limits '{"heap_kb": 64}'
def limits_from(args) -> Limits:
    limits = Limits()
    for key, value in SCENARIO_LIMITS.get(getattr(args, "scenario", ""), {}).items():
        setattr(limits, key, float(value))
    for key, value in json.loads(getattr(args, "limits", "") or "{}").items():
        if hasattr(limits, key):
            setattr(limits, key, float(value))
    return limits


# --------------------------------------------
def run_cli(args, command: list[str], out) -> int:
    if os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "."), "Fonts"))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from . import world
    scenario = args.scenario
    name, title, fn = find(scenario)
    root = Path(tempfile.mkdtemp(prefix="tbmemory-"))
    world.process_setup(root)
    record = header("memory", title, command, {"scenario": name, "cycles": args.cycles, "warmup": args.warmup}, args.seed)
    ctx = Ctx(root / "data")
    ctx.root.mkdir(parents=True, exist_ok=True)
    print(f"scénario : {name} ({title})", flush=True)
    data = measure(lambda i: fn(ctx, i), args.cycles, args.warmup, limits_from(args),
                   log=lambda t: print(t, flush=True))
    with contextlib.suppress(Exception):  # closing is not what is measured
        ctx.close()
    bad = data["verdict"] in ("fuite probable", "erreur")
    record.update(verdict="echec" if bad else "ok", details=data,
                  summary=f"{name} : {data['verdict']} sur {data['measured']} cycles mesurés"
                  + ("".join(f"\n  {e}" for e in data["evidence"])))
    save_run(out, record)
    print(record["summary"], flush=True)
    if data["error"]:
        print(data["error"], flush=True)
    for a in data["allocations"][:5]:
        print(f"  alloc {a['where']} {a['size_diff'] / KB:+.1f} Ko ({a['count_diff']:+d})", flush=True)
    print(f"résultat : {out}", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if bad else 0)


# --------------------------------------------------------
def trend_text(data: dict) -> str:
    lines = [f"verdict : {data['verdict']} ({data['measured']} cycles mesurés, {data['warmup']} d'échauffement)"]
    for key, m in data.get("metrics", {}).items():
        if math.isfinite(m["slope"]):
            lines.append(f"{key:11s} pente {m['slope']:12.1f}/cycle  R2 {m['r2']:.2f}  {m['first']:.0f} -> {m['last']:.0f}")
    return "\n".join(lines)
