# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Main window of the program with its menus, playlist tree, export and CDJ key actions, and drag and drop of sources.

from __future__ import annotations

import logging
import os
import sys
import types
from functools import partial
from pathlib import Path
from time import monotonic

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, VERSION, backup, export, settings, sources
from ..brand import SUPPORT_URL
from .. import drives
from ..export import manifest
from ..export.files import Cancelled, Report
from ..i18n import tr
from ..i18n import trn
from ..model import Node, detach, playlists, prune, unique_tracks
from ..sources import project
from . import formatbox, support, theme, worker
from .audiocheck import AudioCheck
from .backup import BackupPanel
from .details import PlaylistWindow
from .dialogs import (
    About,
    CheckCollection,
    KeyChoice,
    LogWindow,
    Options,
    Usage,
)
from .formatter import FormatDialog
from .musicbrowser import MusicBrowser
from .player import Engine
from .tageditor import TagEditor
from .waveform import Loader
from .worker import Job, stop_all
from .zoomwave import duration_text

log = logging.getLogger(__name__)

PROBE_WAIT = 2000  # ms a read of the key gets to stop when the window closes

def playlist_duration(node: Node) -> str:
    return duration_text(sum(track.duration for track in node.tracks))

def tree_name(node: Node) -> str:
    return node.name + ("  " + tr("(smart)") if node.kind == "smartlist" else "")

# ------------------------------------------------
# File system of the drive holding path, Windows only, '' elsewhere
def fs_type(path: str) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes

    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    buf = ctypes.create_unicode_buffer(32)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        root, None, 0, None, None, None, buf, 32
    )
    return buf.value if ok else ""

# ------------------------------------------------
# contents and PIONEER must sit side by side at the key root
def cdj_root(out: str) -> str:
    if os.path.basename(os.path.normpath(out)).lower() in ("contents", "pioneer"):
        return os.path.dirname(os.path.normpath(out))
    return out

# ------------------------------------------------
# the root of the key for a folder picked inside it: PIONEER or anything below it
# (rekordbox, USBANLZ...), or Contents. Any other folder is returned as it is
def key_root(path: str) -> str:
    parts = Path(os.path.normpath(path)).parts
    lower = [p.lower() for p in parts]
    if "pioneer" in lower:
        cut = len(lower) - 1 - lower[::-1].index("pioneer")
    elif "contents" in lower:
        cut = lower.index("contents")
    else:
        return path
    return str(Path(*parts[:cut])) if cut else path

# what the info line adds when a key was read with trouble: the playlists are all there, but
# some analysis files were missing or damaged (those tracks have no cues), or the database holds
# tracks that no playlist lists (they are not loaded)
def key_notes(saved: dict) -> str:
    out = ""
    n = saved.get("key_warnings", 0)
    if n:
        out += trn(n, ", {n} warning (see the log)", ", {n} warnings (see the log)")
    if saved.get("key_unlisted"):
        out += tr(", {n} tracks in no playlist not loaded", n=saved["key_unlisted"])
    return out

# what the export job hands back when keep mode met a database it cannot read
class Unreadable:
    def __init__(self, message: str):
        self.message = message

# ----------------------------------------
def export_job(
    fmt: str, nodes: list[Node], out: str, cfg: dict, progress=None, cancel=None
):
    try:
        return export.run(fmt, nodes, out, cfg, progress=progress, cancel=cancel)
    except ValueError as e:
        # only this case gets a second chance, any other error goes to the error box
        if fmt == "CDJ/USB" and cfg.get("key_mode", "keep") == "keep":
            summary = export.key_summary(out)
            if summary is not None and not summary.readable:
                return Unreadable(str(e))
        raise

# ----------------------------------------
# the summary of the database on a key, away from the GUI: a USB drive can be slow. The
# stamp is taken first, so a file that changes during the read does not pass as fresh
def key_probe(path: str, progress=None, cancel=None) -> tuple[str, object, object]:
    stamp = export.key_stamp(path)
    return path, export.key_summary(path, cancel), stamp

# ------------------------------------------------
# load nodes, missing-file count and project settings off the GUI thread
def load_job(
    path: str, root: str, progress=None, cancel=None, key_only: bool = False
) -> tuple[list[Node], int, dict]:
    def cb(pct, msg):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(pct, msg)

    extra: dict = {}
    kind = sources.detect(path)
    if key_only:
        # a folder without a database would otherwise load as a music folder
        summary = export.key_summary(path, cancel)
        if summary is None:
            raise ValueError(
                tr(
                    "There is no CDJ database in {path}.\nPick the root of the key, the folder that holds PIONEER.",
                    path=path,
                )
            )
        if not summary.readable:
            raise ValueError(tr("The database on {path} cannot be read.", path=path))
        if kind != "key":
            raise ValueError(tr("{path} cannot be opened as a key.", path=path))
    if kind == "project":
        nodes, extra = project.read(path, root, cb)
    elif kind == "key":
        notes: dict = {}
        nodes = sources.load(path, root, cb, notes)
        extra = {"from_key": True, **notes}
    else:
        nodes = sources.load(path, root, cb)
    cb(100, tr("Checking files..."))
    missing = 0
    for i, t in enumerate(unique_tracks(nodes)):
        if i % 500 == 0:
            cb(100, tr("Checking files..."))
        missing += not os.path.isfile(t.path)
    return nodes, missing, extra

# ------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, cfg: dict, logwin: LogWindow):
        super().__init__()
        self.cfg, self.logwin = cfg, logwin
        self.nodes: list[Node] = []
        self.project = ""  # the project file the playlists were opened from or saved to
        self.dirty = False
        self.job: Job | None = None
        self.again = False
        self.loading = ""  # the path the running load reads, the Source field may change meanwhile
        self.pending_backup_verify = ""
        self.rerun: tuple | None = None  # an export to start again once the job is over
        self.key_only = False  # the next load must be a key (File > Open a CDJ USB key)
        self.key_prev = None  # the Source text to put back when that load fails
        self.key_source = ""  # the key the playlists were read from
        self.key_job: Job | None = None
        self.key_read: Job | None = None  # the read Convert waits for, see read_key
        self.key_pending = False
        # the last (folder, summary) the probe gave
        self.key_info: tuple[str, object] | None = None
        self.key_stamp = None  # size and time of the database that summary came from
        self.backup_panel: BackupPanel | None = None
        self.music_browser: MusicBrowser | None = None
        self.engine = Engine()
        self.engine.set_volume(cfg["volume"] / 100)
        self.loader = Loader(self)
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(760, 700)
        self.setAcceptDrops(True)
        self.build_menu()

        paths = QGroupBox(tr("Source"))
        g = QGridLayout(paths)
        self.src = QLineEdit(cfg["source_path"])
        self.src.setPlaceholderText(
            "Traktor .nml, rekordbox .xml, VirtualDJ database.xml, iTunes Library.xml, Engine DJ m.db, Mixxx, M3U"
        )
        self.root = QLineEdit(cfg["music_root"])
        self.root.setPlaceholderText(tr("optional: where the files are now, if they moved"))
        self.out = QLineEdit(cfg["output_path"])
        self.out.setPlaceholderText(tr("export folder or USB key"))
        rows = (
            (self.src, tr("Collection / project"), self.open_source),
            (self.root, tr("Music folder"), self.pick_root),
        )
        for i, (edit, name, fn) in enumerate(rows):
            b = QPushButton(tr("Browse..."))
            b.clicked.connect(fn)
            g.addWidget(QLabel(name), i, 0)
            g.addWidget(edit, i, 1)
            g.addWidget(b, i, 2)
        reload = QPushButton(tr("Reload"))
        reload.clicked.connect(self.reload_asked)
        g.addWidget(reload, 0, 3)
        self.fmt_btn = QPushButton(tr("Format drive..."))
        self.fmt_btn.setToolTip(
            tr("Erase a USB key and format it as FAT32 for the DJ players")
        )
        self.fmt_btn.clicked.connect(self.format_usb)
        self.key_label = QLabel()
        self.key_label.setTextFormat(Qt.PlainText)
        self.key_button = QPushButton(tr("Open this key"))
        self.key_button.clicked.connect(lambda *_: self.open_key(self.key_target()))
        self.key_row = QWidget()
        key_lay = QHBoxLayout(self.key_row)
        key_lay.setContentsMargins(0, 0, 0, 0)
        key_lay.addWidget(self.key_label)
        key_lay.addWidget(self.key_button)
        key_lay.addStretch(1)
        self.key_row.hide()
        self.key_timer = QTimer(self)
        self.key_timer.setSingleShot(True)
        self.key_timer.setInterval(400)
        self.key_timer.timeout.connect(self.probe_key)
        self.out.textChanged.connect(self.out_changed)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Playlists"), tr("Tracks"), tr("Duration")])
        self.tree.setToolTip(
            tr(
                "Tick the playlists to export. With none ticked, all of them are exported.\n"
                "Double-click a playlist to open it, right click to rename or remove it."
            )
        )
        self.tree.setColumnWidth(0, 520)
        self.tree.setColumnWidth(1, 90)
        self.tree.itemDoubleClicked.connect(self.details)
        self.tree.itemChanged.connect(self.selection_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        # tied to the tree: Del in the Source fields keeps editing text
        del_key = QShortcut(
            QKeySequence.Delete,
            self.tree,
            lambda: self.remove_playlist(self.tree.currentItem()),
        )
        del_key.setContext(Qt.WidgetShortcut)
        self.info = QLabel(tr("No collection loaded"))
        self.info.setStyleSheet(f"color: {theme.FG_MUTED};")

        opts = QHBoxLayout()
        self.fmt = QComboBox()
        formatbox.fill(self.fmt, settings.exports(), cfg["export_format"])
        self.fmt.currentIndexChanged.connect(
            lambda _=None, *_rest: self.format_changed(formatbox.name(self.fmt))
        )
        self.copy = QCheckBox(tr("Copy audio"))
        self.copy.setChecked(cfg["copy_music"])
        self.verify = QCheckBox(tr("Verify the copy (SHA-256)"))
        self.verify.setToolTip(
            tr(
                "After the export, read every file back from the USB key (not from the system cache) and compare\n"
                "its SHA-256 to the source: all the tracks are there and intact.\n"
                "Tools > Verify an export does it again later."
            )
        )
        self.verify.setChecked(cfg["verify_copy"])
        opts.addWidget(self.fmt)
        opts.addWidget(self.copy)
        opts.addWidget(self.verify)
        opts.addStretch(1)

        # the format decides what the output is, so it sits with the output
        dest = QGroupBox(tr("Destination"))
        d = QGridLayout(dest)
        out_browse = QPushButton(tr("Browse..."))
        out_browse.clicked.connect(self.pick_out)
        d.addWidget(QLabel(tr("Output")), 0, 0)
        d.addWidget(self.out, 0, 1)
        d.addWidget(out_browse, 0, 2)
        d.addWidget(self.fmt_btn, 0, 3)
        d.addWidget(QLabel(tr("Export")), 1, 0)
        d.addLayout(opts, 1, 1, 1, 3)
        d.addWidget(self.key_row, 2, 1)
        # same widths in both groups so the fields line up
        for col in (0, 3):
            wide = max(
                max(
                    grid.itemAtPosition(r, col).widget().sizeHint().width()
                    for r in range(grid.rowCount())
                    if grid.itemAtPosition(r, col)
                    and grid.itemAtPosition(r, col).widget()
                )
                for grid in (g, d)
            )
            g.setColumnMinimumWidth(col, wide)
            d.setColumnMinimumWidth(col, wide)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.status = QLabel(tr("Ready"))
        self.go = QPushButton(tr("CONVERT"))
        self.go.setObjectName("go")
        self.go.clicked.connect(self.convert)
        self.stop = QPushButton(tr("Cancel"))
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        run = QHBoxLayout()
        run.addWidget(self.status, 1)
        run.addWidget(self.stop)
        run.addWidget(self.go)

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(paths)
        lay.addWidget(dest)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.info)
        lay.addWidget(self.bar)
        lay.addLayout(run)
        self.setCentralWidget(w)
        self.format_changed(formatbox.name(self.fmt))

        if cfg["auto_load"]:
            if not self.src.text():
                self.src.setText(settings.default_collection())
            if self.src.text() and os.path.exists(self.src.text()):
                QTimer.singleShot(150, self.reload)

    # ---- menus

    # --------------------------------------------------
    def build_menu(self):
        mb = self.menuBar()
        items = {
            tr("&File"): [
                (tr("&Open project / collection..."), "Ctrl+O", self.open_source),
                (tr("&New playlist..."), "Ctrl+N", self.new_playlist),
                (tr("Open a music &folder..."), "Ctrl+Shift+O", self.open_folder),
                (tr("Open an &Engine DJ library..."), None, self.open_engine),
                (
                    tr("Open a CDJ USB &key..."),
                    "Ctrl+Shift+K",
                    lambda *_: self.open_key(),
                ),
                None,
                (tr("&Save the project"), "Ctrl+S", self.save_project),
                (
                    tr("Save the project &as..."),
                    "Ctrl+Shift+S",
                    lambda *_: self.save_project(True),
                ),
                None,
                (tr("Convert"), "Ctrl+Return", self.convert),
                None,
                (tr("E&xit"), "Ctrl+Q", self.close),
            ],
            tr("&Tools"): [
                (tr("&Reload collection"), "Ctrl+R", self.reload_asked),
                (tr("Check collection..."), "Ctrl+K", self.check),
                (tr("Verify audio files..."), "", self.check_audio),
                (tr("&Verify an export..."), "Ctrl+Shift+V", self.verify_export),
                None,
                (tr("Backup"), "", self.open_backup),
                None,
                (tr("&Format a USB drive (FAT32)..."), None, self.format_usb),
                (
                    tr("&Eject the output drive"),
                    "Ctrl+E",
                    lambda *_: self.offer_eject(self.out.text().strip(), False),
                ),
            ],
            tr("&Options"): [
                (tr("&Preferences..."), "Ctrl+,", self.options),
            ],
            tr("&View"): [
                (tr("Music &browser"), "Ctrl+M", self.open_music_browser)
            ],
            tr("&Help"): [
                (tr("View &log"), "Ctrl+L", self.show_log),
                (tr("&Usage"), "F1", lambda *_: self.show_dialog(Usage)),
                (tr("&About"), None, lambda *_: self.show_dialog(About)),
                *(
                    [(tr("&Support the project..."), None, support.open_link)]
                    if SUPPORT_URL
                    else []
                ),
            ],
        }
        for title, acts in items.items():
            m = mb.addMenu(title)
            for a in acts:
                if a is None:
                    m.addSeparator()
                    continue
                act = QAction(a[0], self)
                if a[1]:
                    act.setShortcut(QKeySequence(a[1]))
                act.triggered.connect(a[2])
                m.addAction(act)

    # ---- paths

    # ------------------------------------------------
    def open_source(self):
        start = os.path.dirname(self.src.text()) or settings.default_collection()
        f, _ = QFileDialog.getOpenFileName(
            self,
            tr("Open project / collection"),
            start,
            tr("Projects and DJ collections")
            + " (*.json *.nml *.xml *.sqlite *.db *.m3u *.m3u8 *.vdjfolder database*);;"
            + tr("Boothbridge projects")
            + " (*.json);;"
            + tr("DJ collections and playlists")
            + " (*.nml *.xml *.sqlite *.db *.m3u *.m3u8 *.vdjfolder database*);;"
            + tr("All")
            + " (*)",
        )
        if f:
            if not self.ask_discard():
                return
            self.src.setText(f)
            self.reload()

    def dragEnterEvent(self, event):
        if any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    # ------------------------------------------------
    # A collection, a project or a music folder dropped on the window opens like the matching entry of the
    # File menu
    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        event.acceptProposedAction()
        self.open_dropped(paths[0])

    def open_dropped(self, path: str):
        if self.job is not None or self.key_reading():
            self.status.setText(
                tr("Wait for the current job to finish before opening something else.")
            )
            return
        if (
            os.path.isdir(path)
            and sources.detect(key_root(path)) == "key"
        ):
            self.open_key(key_root(path))
            return
        if not os.path.exists(path) or not self.ask_discard():
            return
        self.src.setText(path)
        self.reload()

    def open_engine(self):
        d = QFileDialog.getExistingDirectory(
            self,
            tr("Open an Engine DJ library (the Engine Library folder, or the drive that holds it)"),
            self.src.text() or self.root.text(),
        )
        if not d:
            return
        if sources.detect(d) != "engine":
            QMessageBox.information(
                self,
                tr("Engine DJ library"),
                tr(
                    "There is no Engine DJ library in this folder. Choose the folder "
                    "Engine Library, or the drive or folder that holds it."
                ),
            )
            return
        if self.ask_discard():
            self.src.setText(d)
            self.reload()

    def open_folder(self):
        d = QFileDialog.getExistingDirectory(
            self, tr("Open a music folder"), self.src.text() or self.root.text()
        )
        if d and self.ask_discard():
            self.src.setText(d)
            self.reload()

    # ------------------------------------------------
    # loads the playlists of a CDJ key like a collection. The key is only read
    def open_key(self, path: str = ""):
        if self.job is not None:
            QMessageBox.information(
                self,
                tr("Open a CDJ USB key"),
                tr("Wait for the current job to finish."),
            )
            return
        if not path:
            path = QFileDialog.getExistingDirectory(
                self,
                tr("Open a CDJ USB key (the folder that holds PIONEER)"),
                self.key_start(),
            )
            if not path:
                return
        path = key_root(path)
        if not self.ask_discard():
            return
        self.key_only, self.key_prev = True, self.src.text()
        self.src.setText(path)
        self.reload()
        if self.job is None:
            self.key_failed()

    def key_start(self) -> str:
        out = self.out.text().strip()
        if (
            self.key_info
            and self.key_info[1] is not None
            and self.key_info[0] == self.key_target()
        ):
            return out
        src = self.src.text().strip()
        return (
            src
            if os.path.isdir(src)
            else os.path.dirname(src) or os.path.expanduser("~")
        )

    # ------------------------------------------------
    # the load that was meant to read a key did not happen: the Source field goes back
    def key_failed(self):
        if self.key_prev is not None:
            self.src.setText(self.key_prev)
        self.key_only, self.key_prev = False, None

    # ---- the key next to Output

    def key_target(self) -> str:
        out = self.out.text().strip()
        return cdj_root(out) if out and formatbox.name(self.fmt) == "CDJ/USB" else ""

    def out_changed(self, _text=""):
        self.key_row.hide()
        if self.key_job is not None:
            self.key_job.stop.set()  # it reads the old folder
        self.key_timer.start()

    # --------------------------------------------------
    def probe_key(self):
        path = self.key_target()
        if not path:
            self.key_info = self.key_stamp = None
            self.key_row.hide()
            return
        if self.key_job is not None:
            self.key_pending = True
            return
        job = self.key_job = Job(key_probe, path)
        job.done.connect(self.key_probed)
        job.failed.connect(lambda m=None, *_: log.warning("key summary: %s", m))
        job.finished.connect(lambda *_: self.key_probe_over(job))
        job.start()

    def key_probed(self, res):
        path, summary, stamp = res
        if path != self.key_target():
            return
        self.key_info, self.key_stamp = (path, summary), stamp
        if summary is None:
            self.key_row.hide()
            return
        if getattr(summary, "readable", True):
            self.key_label.setText(
                tr(
                    "Key: {playlists} playlists, {tracks} tracks",
                    playlists=summary.playlists,
                    tracks=summary.tracks,
                )
            )
            self.key_label.setStyleSheet(f"color: {theme.FG_MUTED};")
        else:
            self.key_label.setText(tr("Key: database cannot be read"))
            self.key_label.setStyleSheet(f"color: {theme.YELLOW};")
        self.key_button.setVisible(getattr(summary, "readable", True))
        self.key_row.show()

    def key_probe_over(self, job):
        job.deleteLater()
        if job is self.key_read:
            self.key_read = None
        if job is self.key_job:
            self.key_job = None
            if self.key_pending:
                self.key_pending = False
                self.key_timer.start()

    # ------------------------------------------------
    # calls then(summary) with what the database on the key holds, None when there is no
    # database or it could not be looked at. The summary the status line has is reused while the
    # file is the same, otherwise the key is read away from the GUI: a USB drive can be slow
    def read_key(self, out: str, then):
        try:
            stamp = export.key_stamp(out)
        except (OSError, ValueError) as e:
            log.warning("cannot look at the key: %s", e)
            then(None)
            return
        if stamp is None:
            then(None)
            return
        info = self.key_info
        if (
            info is not None
            and info[0] == out
            and info[1] is not None
            and self.key_stamp == stamp
        ):
            then(info[1])
            return
        if self.key_job is not None:
            self.key_job.stop.set()  # this read feeds the status line too
        self.busy(True)
        self.status.setText(tr("Reading the key..."))
        job = self.key_read = Job(key_probe, out)
        job.done.connect(lambda res=None, *_: self.key_read_done(job, res, then))
        job.failed.connect(lambda m=None, *_: self.key_read_failed(m, then))
        job.cancelled.connect(self.key_read_cancelled)
        job.finished.connect(lambda *_: self.key_probe_over(job))
        job.start()

    def key_read_done(self, job, res, then):
        self.busy(False)
        if job.stop.is_set():
            self.key_read_cancelled()
            return
        self.key_probed(res)
        then(res[1])

    def key_read_failed(self, msg: str, then):
        log.warning("cannot look at the key: %s", msg)
        self.busy(False)
        then(None)

    def key_read_cancelled(self):
        self.busy(False)
        self.status.setText(tr("Cancelled"))

    # ------------------------------------------------
    # cancels the reads of the key and waits a little for them, they only read. Returns the
    # ones that did not stop, a drive that does not answer must not hold the window
    def stop_probes(self) -> list[Job]:
        jobs = [j for j in (self.key_job, self.key_read) if j is not None]
        for j in jobs:
            j.stop.set()
        deadline = monotonic() + PROBE_WAIT / 1000
        for j in jobs:
            j.wait(max(0, int((deadline - monotonic()) * 1000)))
        QApplication.processEvents()  # lets their finished signals through
        return [j for j in jobs if j.isRunning()]

    def key_reading(self) -> bool:
        return self.key_read is not None

    # ------------------------------------------------
    # the jobs that are running and are not a read of the key
    def other_jobs(self) -> list[Job]:
        return [
            j for j in worker._alive if j is not self.key_job and j is not self.key_read
        ]

    def new_playlist(self):
        name, ok = QInputDialog.getText(self, tr("New playlist"), tr("Name"))
        name = name.strip()
        if not ok or not name:
            return
        # same rule as rename_playlist: names that differ only by case would be one file on Windows
        if any(other.name.casefold() == name.casefold() for other in playlists(self.nodes)):
            QMessageBox.warning(
                self,
                tr("New playlist"),
                tr('A playlist named "{name}" already exists.', name=name),
            )
            return
        n = Node("playlist", name)
        self.nodes.append(n)
        self.set_dirty(True)
        self.fill_tree()
        self.refresh_info()
        for it in self.tree.findItems(name, Qt.MatchExactly | Qt.MatchRecursive):
            if it.data(0, Qt.UserRole) is n:
                self.details(it)
                break

    # ---- project

    def set_dirty(self, on: bool):
        self.dirty = on
        name = os.path.basename(self.project) if self.project else ""
        if not name and self.key_source:
            name = tr(
                "key {name}",
                name=os.path.basename(os.path.normpath(self.key_source))
                or self.key_source,
            )
        self.setWindowTitle(
            f"{APP_NAME} {VERSION}"
            + (f" - {name}" if name else "")
            + (" *" if on else "")
        )

    def open_project(self):
        self.open_source()

    # ------------------------------------------------
    def save_project(self, ask: bool = False) -> bool:
        if not self.nodes:
            QMessageBox.information(
                self,
                tr("Save the project"),
                tr(
                    "There is nothing to save yet: open a collection "
                    "or create a playlist first."
                ),
            )
            return False
        path = self.project
        if ask or not path:
            start = path or os.path.join(
                os.path.expanduser("~"), "Documents", "playlists.json"
            )
            path, _ = QFileDialog.getSaveFileName(
                self,
                tr("Save the project"),
                start,
                tr("Boothbridge projects") + " (*.json)",
            )
            if not path:
                return False
            if not path.lower().endswith(".json"):
                path += ".json"
        try:
            project.save(path, self.nodes, self.sync())
        except OSError as e:
            QMessageBox.critical(
                self,
                tr("Save the project"),
                tr("Cannot write {path}:\n{error}", path=path, error=e),
            )
            return False
        self.project = path
        self.set_dirty(False)
        self.status.setText(tr("Project saved: {path}", path=path))
        log.info("Project saved: %s", path)
        return True

    def open_backup(self):
        panel = BackupPanel(self)
        self.backup_panel = panel
        try:
            panel.exec()
        finally:
            self.backup_panel = None
            panel.deleteLater()

    # ------------------------------------------------
    def backup_project(self):
        if self.job is not None:
            QMessageBox.information(
                self, tr("ZIP backup"), tr("Wait for the current job to finish.")
            )
            return
        if not self.nodes:
            QMessageBox.information(
                self,
                tr("ZIP backup"),
                tr("Open a collection or create a playlist first."),
            )
            return
        if any(
            editor.job is not None
            for window in self.findChildren(PlaylistWindow)
            for editor in window.findChildren(TagEditor)
        ):
            QMessageBox.warning(
                self,
                tr("ZIP backup"),
                tr("Wait for audio-tag writing to finish before backing up."),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("ZIP backup"),
            tr(
                "Back up all loaded playlists, current applied project edits and their audio files?\n\n"
                "This includes unticked playlists. Audio files are included once. Unapplied editor fields "
                "are not included. Missing audio prevents a complete backup.\n\n"
                "After extracting the ZIP, open project.json to restore the project. "
                "The backup may need as much disk space as the audio collection."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        name = (
            os.path.splitext(self.project)[0] + "-backup.zip"
            if self.project
            else "Boothbridge-backup.zip"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, tr("ZIP backup"), name, tr("ZIP archives") + " (*.zip)"
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        document = project.dump(self.nodes, self.sync())
        self.busy(True)
        self.status.setText(tr("Preparing ZIP backup..."))
        self.job = Job(backup.create, path, document)
        self.job.progress.connect(self.on_progress)
        self.job.done.connect(self.backup_finished)
        self.job.failed.connect(self.backup_failed)
        self.job.cancelled.connect(
            lambda *_: self.status.setText(tr("Backup cancelled; destination unchanged"))
        )
        self.job.finished.connect(self.job_over)
        self.job.start()

    # --------------------------------------------------------------
    def backup_finished(self, result):
        self.status.setText(tr("ZIP backup completed"))
        answer = QMessageBox.question(
            self,
            tr("ZIP backup completed"),
            tr(
                "{playlists} playlists and {audio} audio files backed up.\n"
                "{size} GB\n\n{path}\n\n"
                "SHA-256: {sha256}\nChecksum file: {checksum_path}\n\n"
                "Verify this backup now?",
                playlists=result["playlists"],
                audio=result["audio"],
                size=f"{result['size'] / 1e9:.2f}",
                path=result["path"],
                sha256=result["sha256"],
                checksum_path=result["checksum_path"],
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            if self.job is None:
                QTimer.singleShot(0, lambda: self.start_backup_verify(result["path"]))
            else:
                self.pending_backup_verify = result["path"]

    def backup_failed(self, message):
        self.status.setText(tr("ZIP backup failed; see error details"))
        QMessageBox.critical(self, tr("ZIP backup failed"), message)

    def verify_backup(self):
        self.backup_action(False)

    def restore_backup(self):
        self.backup_action(True)

    def backup_action(self, restoring, path=""):
        if self.job is not None:
            QMessageBox.information(
                self, tr("ZIP backup"), tr("Wait for the current job to finish.")
            )
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self,
                tr("Restore ZIP backup") if restoring else tr("Verify ZIP backup"),
                "",
                tr("ZIP archives") + " (*.zip)",
            )
        if not path:
            return
        args = (path,)
        if restoring:
            parent = QFileDialog.getExistingDirectory(
                self, tr("Parent folder for restoration")
            )
            if not parent:
                return
            name, ok = QInputDialog.getText(
                self,
                tr("Restoration folder"),
                tr("New folder name:"),
                text=os.path.splitext(os.path.basename(path))[0] + "-restored",
            )
            if not ok:
                return
            if not name or name != backup.safe_name(name) or name in (".", ".."):
                QMessageBox.warning(
                    self,
                    tr("Restoration folder"),
                    tr("Use a simple folder name without path separators."),
                )
                return
            args += (os.path.join(parent, name),)
        self.busy(True)
        self.status.setText(tr("Verifying ZIP backup..."))
        self.job = Job(backup.restore if restoring else backup.verify, *args)
        self.job.progress.connect(self.on_progress)
        self.job.done.connect(
            self.restore_finished if restoring else self.verify_backup_finished
        )
        self.job.failed.connect(self.backup_failed)
        self.job.cancelled.connect(lambda *_: self.status.setText(tr("Operation cancelled")))
        self.job.finished.connect(self.job_over)
        self.job.start()

    def verify_backup_finished(self, checksum):
        self.bar.setValue(100)
        self.status.setText(tr("ZIP checksum verified"))
        QMessageBox.information(
            self,
            tr("ZIP verified"),
            tr("The ZIP matches its SHA-256 checksum.\n\n{checksum}", checksum=checksum),
        )

    # ------------------------------------------------
    def restore_finished(self, path):
        self.status.setText(tr("Backup restored"))
        answer = QMessageBox.question(
            self,
            tr("Backup restored"),
            tr("Project restored at:\n{path}\n\nOpen it now?", path=path),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes and self.ask_discard():
            self.root.clear()
            self.out.clear()
            self.src.setText(path)
            if self.job is None:
                QTimer.singleShot(0, self.reload)
            else:
                self.again = True

    # ------------------------------------------------
    # true when it is fine to replace or close the playlists: saved, or given up
    def ask_discard(self) -> bool:
        if not (self.dirty and self.nodes):
            return True
        r = QMessageBox.question(
            self,
            tr("Unsaved work"),
            tr("The playlists changed since the last save. Save the project?"),
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if r == QMessageBox.Save:
            return self.save_project()
        return r == QMessageBox.Discard

    # ------------------------------------------------
    def apply_project(self, path: str, saved: dict):
        self.project = path
        for key, widget in (("music_root", self.root), ("output_path", self.out)):
            if saved.get(key):
                widget.setText(str(saved[key]))
        if saved.get("export_format") in settings.exports():
            formatbox.select(self.fmt, saved["export_format"])
        for key, box in (("copy_music", self.copy), ("verify_copy", self.verify)):
            if isinstance(saved.get(key), bool) and box.isEnabled():
                box.setChecked(saved[key])
        for key in ("key_format", "waveform_color", "crossfade"):
            if key in saved:
                self.cfg[key] = saved[key]

    def pick_root(self):
        d = QFileDialog.getExistingDirectory(self, tr("Music folder"), self.root.text())
        if d:
            self.root.setText(d)
            if self.nodes:
                self.reload_asked()

    def pick_out(self):
        d = QFileDialog.getExistingDirectory(self, tr("Output folder"), self.out.text())
        if d:
            self.out.setText(d)

    def format_usb(self):
        dlg = FormatDialog(self)
        dlg.formatted.connect(self.use_formatted)
        dlg.exec()
        dlg.deleteLater()

    def use_formatted(self, path: str):
        if self.out.text().strip() != path:
            r = QMessageBox.question(
                self,
                tr("Output"),
                tr("Use {path} as the export folder?", path=path),
            )
            if r == QMessageBox.Yes:
                self.out.setText(path)

    def format_changed(self, fmt: str):
        cdj = fmt == "CDJ/USB"
        self.copy.setEnabled(not cdj)
        if cdj:
            self.copy.setChecked(True)
        self.out_changed()

    # ---- loading

    def busy(self, on: bool):
        self.go.setEnabled(not on)
        self.stop.setEnabled(on)
        self.tree.setEnabled(not on)

    # ----------------------------------------------------
    # from the button, the menu and the music folder: unsaved edits are not thrown away unasked
    def reload_asked(self, *_):
        if self.job is not None or self.key_reading():
            return
        if self.ask_discard():
            self.reload()

    def reload(self):
        path = self.src.text().strip()
        if not path or not os.path.exists(path):
            return
        if self.job is not None:
            # asked while loading (music folder picked from the missing files prompt)
            self.again = True
            return
        for editor in self.findChildren(PlaylistWindow):
            if not editor.close():
                self.status.setText(
                    tr("Wait for playlist editing operations to finish before reloading.")
                )
                return
        self.busy(True)
        self.status.setText(tr("Loading..."))
        self.loading = path
        self.job = Job(load_job, path, self.root.text().strip(), key_only=self.key_only)
        self.key_only = False
        self.job.progress.connect(self.on_progress)
        self.job.done.connect(self.loaded)
        self.job.failed.connect(self.load_failed)
        self.job.cancelled.connect(self.load_cancelled)
        self.job.finished.connect(self.job_over)
        self.job.start()

    # ------------------------------------------------
    def loaded(self, res: tuple[list[Node], int, dict]):
        nodes, missing, saved = res
        self.nodes = nodes
        from_key = False
        self.key_prev = None
        from_key = bool(saved.get("from_key"))
        path = self.loading
        if sources.detect(path) == "project":
            self.apply_project(path, saved)
            from_key = False
        else:
            self.project = ""
        self.key_source = path if from_key else ""
        self.set_dirty(False)
        self.fill_tree()
        tracks = unique_tracks(nodes)
        counts = {"playlists": sum(1 for _ in playlists(nodes)), "tracks": len(tracks)}
        info = (
            tr(
                "{playlists} playlists, {tracks} tracks, {missing} missing files",
                missing=missing,
                **counts,
            )
            if missing
            else tr("{playlists} playlists, {tracks} tracks", **counts)
        )
        info += (tr(", read from a CDJ key") if from_key else "") + (
            key_notes(saved) if from_key else ""
        )
        self.info.setText(info)
        self.status.setText(tr("Ready"))
        if from_key:
            self.status.setText(
                tr("Playlists read from the key {path}", path=self.key_source)
            )
        self.bar.setValue(0)
        log.info(
            "Loaded %s: %d tracks, %d missing", path, len(tracks), missing
        )
        self.cfg["source_path"] = path
        if missing and not self.root.text() and not from_key:
            QTimer.singleShot(0, lambda: self.ask_root(missing, len(tracks)))

    def ask_root(self, missing: int, total: int):
        r = QMessageBox.question(
            self,
            tr("Missing files"),
            tr(
                "{missing} of {total} files are not where the collection says.\n"
                "Pick the folder where your music is now?",
                missing=missing,
                total=total,
            ),
        )
        if r == QMessageBox.Yes:
            self.pick_root()

    def load_failed(self, msg: str):
        self.status.setText(tr("Loading failed"))
        title = tr("Cannot read the collection")
        if self.key_prev is not None:
            title = tr("Cannot read the key")
        self.key_failed()
        QMessageBox.critical(self, title, msg)

    def load_cancelled(self):
        self.status.setText(tr("Loading cancelled"))
        self.key_failed()

    # --------------------------------------------
    def job_over(self):
        self.job.deleteLater()
        self.job = None
        self.busy(False)
        if self.rerun is not None:
            QTimer.singleShot(0, lambda: self.rerun and self.start_export(*self.rerun))
        if self.pending_backup_verify:
            path, self.pending_backup_verify = self.pending_backup_verify, ""
            QTimer.singleShot(0, lambda: self.start_backup_verify(path))
        if self.again:
            self.again = False
            self.reload()

    def start_rerun(self):
        if self.rerun is not None and self.job is None:
            self.start_export(*self.rerun)

    def start_backup_verify(self, path):
        self.backup_action(False, path)
        if self.job is not None and self.backup_panel is not None:
            self.backup_panel.watch(self.job)

    # ------------------------------------------------------------------------
    def fill_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()

        # ------------------------------------------------
        def add(parent, nodes):
            for n in nodes:
                count = "" if n.is_folder else str(len(n.tracks))
                name = tree_name(n)
                duration = "" if n.is_folder else playlist_duration(n)
                it = QTreeWidgetItem([name, count, duration])
                it.setData(0, Qt.UserRole, n)
                it.setCheckState(0, Qt.Unchecked)
                if n.is_folder:
                    it.setFlags(it.flags() | Qt.ItemIsAutoTristate)
                    add(it, n.children)
                if parent is None:
                    self.tree.addTopLevelItem(it)
                else:
                    parent.addChild(it)

        add(None, self.nodes)
        self.tree.expandToDepth(0)
        self.tree.blockSignals(False)

    def selection_changed(self, *_):
        sel = self.selected()
        n = (
            len(unique_tracks(sel))
            if sel is not self.nodes
            else len(unique_tracks(self.nodes))
        )
        self.status.setText(
            tr("{n} tracks selected", n=n)
            if sel is not self.nodes
            else tr("Everything selected")
        )

    # ------------------------------------------------
    def selected(self) -> list[Node]:
        keep = set()

        def walk(it):
            for i in range(it.childCount()):
                c = it.child(i)
                n = c.data(0, Qt.UserRole)
                if not n.is_folder and c.checkState(0) == Qt.Checked:
                    keep.add(id(n))
                walk(c)

        walk(self.tree.invisibleRootItem())
        return prune(self.nodes, keep) if keep else self.nodes

    # ---- windows

    def details(self, it, _col=0):
        n = it.data(0, Qt.UserRole)
        if n.is_folder:
            return
        # one window per playlist: two would each undo to the list they knew and drop the other's edits
        for w in self.findChildren(PlaylistWindow):
            if w.node is n and w.isVisible():
                w.show()
                w.raise_()
                w.activateWindow()
                return
        w = PlaylistWindow(
            n,
            self.engine,
            self.loader,
            self.cfg,
            self.root.text().strip(),
            self,
            others=lambda: list(playlists(self.nodes)),
        )
        w.edited.connect(lambda *_: self.playlist_edited(n))
        w.inserted.connect(self.insert_tracks)
        w.show()

    # ------------------------------------------------
    # tracks sent from a playlist window to another playlist. An open window of the target takes them as
    # one undo step, a closed one is changed directly
    def insert_tracks(self, target, tracks):
        if not any(playlist is target for playlist in playlists(self.nodes)):
            return
        window = next(
            (w for w in self.findChildren(PlaylistWindow) if w.node is target), None
        )
        if window is not None:
            window.change_tracks(window.node.tracks + tracks)
        else:
            target.tracks.extend(tracks)
            self.playlist_edited(target)
        self.refresh_windows(skip=target)
        self.status.setText(
            tr(
                '{n} track(s) inserted into "{name}"',
                n=len(tracks),
                name=target.name,
            )
        )

    def playlist_edited(self, node):
        self.set_dirty(True)
        for item in self.tree.findItems("", Qt.MatchContains | Qt.MatchRecursive):
            if item.data(0, Qt.UserRole) is node:
                item.setText(1, str(len(node.tracks)))
                item.setText(2, playlist_duration(node))
                break


    def refresh_info(self):
        self.info.setText(
            tr(
                "{playlists} playlists, {tracks} tracks",
                playlists=sum(1 for _ in playlists(self.nodes)),
                tracks=len(unique_tracks(self.nodes)),
            )
        )

    # ---- rename and remove

    # ------------------------------------------------
    # A job or a folder scan is working on the playlists
    def tree_busy(self) -> bool:
        return (
            self.job is not None
            or self.key_reading()
            or any(
                panel.scan is not None for panel in self.findChildren(CheckCollection)
            )
        )

    def playlist_of(self, item) -> Node | None:
        node = item.data(0, Qt.UserRole) if item is not None else None
        return None if node is None or node.is_folder else node

    def tree_menu(self, pos):
        menu = self.playlist_menu(self.tree.itemAt(pos))
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            menu.deleteLater()

    # ------------------------------------------------
    # the menu of a playlist row, None on a folder or on empty space
    def playlist_menu(self, item) -> QMenu | None:
        if self.playlist_of(item) is None:
            return None
        menu = QMenu(self)
        for text, fn in (
            (tr("Rename..."), self.rename_playlist),
            (tr("Remove from this session..."), self.remove_playlist),
        ):
            act = menu.addAction(text)
            act.triggered.connect(lambda _checked=False, *_, fn=fn: fn(item))
            act.setEnabled(not self.tree_busy())
        return menu

    # ------------------------------------------------
    def rename_playlist(self, item):
        node = self.playlist_of(item)
        if node is None:
            return
        if self.tree_busy():
            QMessageBox.information(
                self, tr("Rename playlist"), tr("Wait for the current job to finish.")
            )
            return
        name, ok = QInputDialog.getText(
            self, tr("Rename playlist"), tr("Name"), text=node.name
        )
        name = name.strip()
        if not ok or name == node.name:
            return
        if not name:
            QMessageBox.warning(
                self, tr("Rename playlist"), tr("The name cannot be empty.")
            )
            return
        # same rule as add_set: names that differ only by case would be one file on Windows
        if any(
            other is not node and other.name.casefold() == name.casefold()
            for other in playlists(self.nodes)
        ):
            QMessageBox.warning(
                self,
                tr("Rename playlist"),
                tr('A playlist named "{name}" already exists.', name=name),
            )
            return
        node.name = name
        self.tree.blockSignals(True)
        item.setText(0, tree_name(node))
        self.tree.blockSignals(False)
        for window in self.findChildren(PlaylistWindow):
            if window.node is node:
                window.renamed()
        self.refresh_windows(skip=node)
        self.set_dirty(True)

    # ------------------------------------------------
    def remove_playlist(self, item):
        node = self.playlist_of(item)
        if node is None:
            return
        if self.tree_busy():
            QMessageBox.information(
                self,
                tr("Remove from this session"),
                tr("Wait for the current job to finish."),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Remove from this session"),
            tr(
                'Remove "{name}" from this session?\n\n'
                "Only the playlists loaded in Boothbridge change. The collection file and the audio files "
                "are never touched. Unsaved edits of this playlist are lost.",
                name=node.name,
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        # same way as Reload: a window busy writing tags or detecting grids refuses to close
        for window in self.findChildren(PlaylistWindow):
            if window.node is node and not window.close():
                QMessageBox.warning(
                    self,
                    tr("Remove from this session"),
                    tr(
                        '"{name}" is open in a window that is writing tags or detecting grids. '
                        "Wait for it to finish, then try again.",
                        name=node.name,
                    ),
                )
                return
        if not detach(self.nodes, node):
            return
        parent = item.parent() or self.tree.invisibleRootItem()
        row = parent.indexOfChild(item)
        parent.takeChild(row)
        if parent.childCount():
            self.tree.setCurrentItem(parent.child(min(row, parent.childCount() - 1)))
        elif parent is not self.tree.invisibleRootItem():
            self.tree.setCurrentItem(parent)
        self.set_dirty(True)
        self.refresh_info()
        self.refresh_windows(skip=node)
        self.status.setText(tr('"{name}" removed from this session', name=node.name))

    # ------------------------------------------------
    # the open windows list where a track also is by playlist name: they read the tree again
    def refresh_windows(self, skip=None):
        for window in self.findChildren(PlaylistWindow):
            if window.isVisible() and window.node is not skip:
                window.fill()

    def paths_relocated(self, root: str):
        self.root.setText(root)
        self.set_dirty(True)
        for editor in self.findChildren(PlaylistWindow):
            editor.paths_changed()

    def check(self):
        if not self.nodes:
            QMessageBox.information(
                self, tr("Check collection"), tr("Load a collection first.")
            )
            return
        d = CheckCollection(self.nodes, self.root.text().strip(), self)
        d.relocated.connect(lambda *_: self.paths_relocated(d.root))
        try:
            d.exec()
        finally:
            d.deleteLater()

    def check_audio(self):
        panel = AudioCheck(self.nodes, self.root.text().strip(), self)
        try:
            panel.exec()
        finally:
            panel.deleteLater()

    def open_music_browser(self):
        if self.music_browser is None:
            self.music_browser = MusicBrowser(self.engine, self.cfg, self)
            self.music_browser.preferences_requested.connect(
                lambda *_: self.options(library=True)
            )
        self.music_browser.set_root(self.cfg.get("library_root", ""))
        self.music_browser.open()

    # ------------------------------------------------
    def options(self, library=False):
        d = Options(self.sync(), self)
        if library:
            d.tabs.setCurrentWidget(d.library_tab)
        if d.exec():
            formatbox.select(self.fmt, self.cfg["export_format"])
            self.copy.setChecked(self.cfg["copy_music"])
            self.verify.setChecked(self.cfg["verify_copy"])
            self.engine.set_volume(self.cfg["volume"] / 100)
            if self.music_browser is not None:
                self.music_browser.set_root(self.cfg.get("library_root", ""))
                self.music_browser.model.dataChanged.emit(
                    self.music_browser.model.index(0, 0),
                    self.music_browser.model.index(
                        self.music_browser.model.rowCount() - 1, 8
                    ),
                )
            logging.getLogger().setLevel(self.cfg["log_level"])
            settings.save(self.sync())
        d.deleteLater()

    def show_log(self):
        self.logwin.show()
        self.logwin.raise_()

    def show_dialog(self, dialog_type):
        dialog = dialog_type(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    # ---- export

    def sync(self) -> dict:
        self.cfg.update(
            source_path=self.src.text(),
            music_root=self.root.text(),
            output_path=self.out.text(),
            export_format=formatbox.name(self.fmt),
            copy_music=self.copy.isChecked(),
            verify_copy=self.verify.isChecked(),
        )
        return self.cfg

    # ------------------------------------------------
    def convert(self):
        if self.job is not None or self.key_reading():
            return
        if not self.nodes:
            QMessageBox.information(self, tr("Convert"), tr("Load a collection first."))
            return
        out = self.out.text().strip()
        if not out:
            self.pick_out()
            out = self.out.text().strip()
            if not out:
                return
        cfg = self.sync()
        fmt = cfg["export_format"]
        if fmt == "CDJ/USB" and cdj_root(out) != out:
            out = cdj_root(out)
            self.out.setText(out)
            cfg["output_path"] = out
            log.info("output moved up to the key root: %s", out)
        if fmt == "CDJ/USB" and fs_type(out).upper() == "NTFS":
            r = QMessageBox.warning(
                self,
                tr("NTFS drive"),
                tr(
                    "The players don't read NTFS keys (they show NO DISK).\n"
                    "Export anyway? You can copy the result to a FAT32 or exFAT key,\n"
                    "or use Format drive... next to Output to make this key FAT32 (it erases it)."
                ),
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return
        nodes = self.selected()
        run_cfg = dict(cfg)
        if fmt == "CDJ/USB":
            then = partial(self.key_chosen, fmt, nodes, out, cfg, run_cfg)
            if cfg.get("key_ask", True):
                self.read_key(out, then)
            else:
                then(None)  # the saved mode applies, nothing to show
            return
        settings.save(cfg)
        self.start_export(fmt, nodes, out, run_cfg)

    # ------------------------------------------------
    # the second half of Convert for a key, once read_key has what is on it
    def key_chosen(
        self, fmt: str, nodes: list[Node], out: str, cfg: dict, run_cfg: dict, summary
    ):
        mode = self.key_decision(nodes, summary)
        if mode is None:
            return
        run_cfg["key_mode"] = mode  # for this run only, the saved setting is cfg
        settings.save(cfg)
        self.start_export(fmt, nodes, out, run_cfg)

    # ------------------------------------------------
    # key_mode for this export, None when the user cancels. Asks only when the key has a database
    def key_decision(self, nodes: list[Node], summary) -> str | None:
        saved = self.cfg.get("key_mode", "keep")
        if summary is None or not self.cfg.get("key_ask", True):
            return saved
        try:
            replaced = export.merge_names(nodes, summary) if summary.readable else []
        except (OSError, ValueError) as e:
            log.warning("cannot list the playlists to replace: %s", e)
            replaced = []
        dlg = KeyChoice(
            summary,
            replaced,
            self,
            "" if summary.readable else getattr(summary, "error", ""),
        )
        try:
            dlg.exec()
            choice, remember = dlg.choice, dlg.remember
        finally:
            dlg.deleteLater()
        if not choice:
            return None
        if remember:
            self.cfg["key_ask"] = False
            self.cfg["key_mode"] = choice
        return choice

    def start_export(self, fmt: str, nodes: list[Node], out: str, run_cfg: dict):
        log.info("Export %s to %s", fmt, out)
        self.rerun = None
        self.busy(True)
        self.job = Job(export_job, fmt, nodes, out, dict(run_cfg))
        self.job.progress.connect(self.on_progress)
        self.job.done.connect(
            lambda rep=None, *_: self.exported(rep, (fmt, nodes, out, run_cfg))
        )
        self.job.failed.connect(self.export_failed)
        self.job.cancelled.connect(lambda *_: self.status.setText(tr("Cancelled")))
        self.job.finished.connect(self.job_over)
        self.job.start()

    def export_failed(self, msg: str):
        QMessageBox.critical(self, tr("Export failed"), msg)
        self.probe_key()

    def cancel(self):
        job = self.job
        job = job if job is not None else self.key_read
        if job is not None:
            job.stop.set()
            self.status.setText(tr("Cancelling..."))

    def on_progress(self, pct: int, msg: str):
        self.bar.setValue(pct)
        self.status.setText(msg)

    # ------------------------------------------------
    # why a player settings file on the key was refused, the codes are those of settingsread
    @staticmethod
    def settings_reason(code: str) -> str:
        return {
            "short": tr("too short to be a settings file"),
            "header": tr("not a settings file"),
            "app": tr("from another program"),
            "size": tr("cut short, or longer than it says"),
            "checksum": tr("damaged (its checksum does not match)"),
            "end": tr("damaged (the end of the file is wrong)"),
            "magic": tr("not the file its name says"),
            "layout": tr("not laid out the way this program changes it"),
        }.get(code, code)

    def exported(self, rep: Report, run=None):
        if isinstance(rep, Unreadable):
            self.key_unreadable(rep.message, run)
            return
        # a CDJ key leaves a track without its file out, the file formats write every track and only have
        # no audio to copy for it
        # the format and options of this run: the combo and the box can change while it works
        fmt, run_cfg = formatbox.name(self.fmt), self.cfg
        if run is not None:
            fmt, run_cfg = run[0], run[3]
        left_out_missing = fmt == "CDJ/USB"
        for m in rep.missing:
            log.warning(
                "missing, not exported: %s"
                if left_out_missing
                else "missing, written without its audio: %s",
                m,
            )
        for e in rep.errors:
            log.error(e)
        self.bar.setValue(100)
        written = rep.tracks - len(rep.missing) if left_out_missing else rep.tracks
        lines = [tr("{n} tracks exported to\n{path}", n=written, path=rep.path)]
        shown = 8
        kept = 0
        kept = getattr(rep, "kept", 0)
        if kept:
            lines.append(tr("Kept {n} playlists already on the key", n=kept))
        replaced = getattr(rep, "replaced", [])
        if replaced:
            lines.append(
                tr("{n} playlists with the same name were replaced", n=len(replaced))
            )
        reused = getattr(rep, "reused", 0)
        if reused:
            lines.append(
                tr(
                    "{n} tracks kept the analysis they already have on the key",
                    n=reused,
                )
            )
        if rep.cached - reused > 0:
            lines.append(
                tr("{n} analyses reused from the cache", n=rep.cached - reused)
            )
        if rep.missing and left_out_missing:
            lines.append(
                tr("{n} missing files skipped (see the log)", n=len(rep.missing))
            )
        elif rep.missing:
            lines.append(
                tr(
                    "{n} of these tracks have no audio file where the collection says, "
                    "they are written with the path they had (see the log)",
                    n=len(rep.missing),
                )
            )
        left_out = getattr(rep, "left_out", [])
        if left_out:
            lines.append(
                tr(
                    "Not written, none of their files was found: {names}",
                    names=", ".join(left_out),
                )
            )
        changed = getattr(rep, "player_changed", [])
        if changed:
            lines.append(
                tr(
                    "{n} analysis files kept from the earlier export were changed since, "
                    "by a player or another program. They are left as they are.",
                    n=len(changed),
                )
            )
        problems = getattr(rep, "settings_problems", [])
        for name, why in problems:
            lines.append(
                tr(
                    "{name} on the key is {why}. It was left as it is, the players may ignore it.",
                    name=name,
                    why=self.settings_reason(why),
                )
            )
        chosen = getattr(rep, "settings_set", 0)
        if chosen and not problems:
            lines.append(tr("Player settings written to the key: {n}", n=chosen))
        if rep.errors:
            lines.append(
                tr("{n} errors:", n=len(rep.errors))
                + "\n"
                + "\n".join(rep.errors[:shown])
                + (
                    "\n"
                    + tr("... and {n} more (see the log)", n=len(rep.errors) - shown)
                    if len(rep.errors) > shown
                    else ""
                )
                + "\n\n"
                + tr(
                    "Export again to the same place: only the files that differ are written again."
                )
            )
        elif rep.manifest:
            lines.append(
                tr(
                    "Integrity checked: all {n} files read back from the drive match their "
                    "SHA-256 ({size} GB)",
                    n=rep.verified,
                    size=f"{rep.verified_size / 1e9:.2f}",
                )
                if run_cfg["verify_copy"]
                else tr(
                    "Checksums saved, Tools > Verify an export checks the export later"
                )
            )
        self.status.setText(
            tr("Done. Kept {n} playlists already on the key", n=kept)
            if kept
            else tr("Done")
        )
        self.probe_key()
        box = (
            QMessageBox.warning
            if rep.errors or rep.missing or left_out or problems
            else QMessageBox.information
        )
        box(self, tr("Export finished"), "\n\n".join(lines))
        if not rep.errors:
            if support.ask_once(self, self.cfg):
                settings.save(self.sync())
            self.offer_eject(rep.path)

    # ------------------------------------------------
    # Keep mode met a database it cannot read: replace it for this run, or give up
    def key_unreadable(self, message: str, run):
        self.status.setText(tr("The database on the key cannot be read"))
        self.probe_key()
        dlg = KeyChoice(types.SimpleNamespace(readable=False), [], self, message)
        try:
            dlg.exec()
            choice = dlg.choice
        finally:
            dlg.deleteLater()
        if choice == "replace" and run is not None:
            fmt, nodes, out, run_cfg = run
            self.rerun = (fmt, nodes, out, dict(run_cfg, key_mode="replace"))
            # the job that reported it is over by then, or job_over starts it
            QTimer.singleShot(0, self.start_rerun)
            return
        self.status.setText(tr("Cancelled"))

    # ---- eject

    # ------------------------------------------------
    # looks for the removable drive holding path and offers to eject it. From the menu (ask False)
    # it also says so when there is none
    def offer_eject(self, path: str, ask: bool = True):
        if not path:
            return
        self.status.setText(tr("Looking for the drive..."))
        job = Job(drives.drive_for, path)
        job.done.connect(lambda d=None, *_: self.eject_found(d, ask))
        job.failed.connect(lambda m=None, *_: self.eject_failed(m, ask))
        job.start()

    # ------------------------------------------------
    def eject_found(self, d, ask: bool):
        self.status.setText(tr("Done"))
        if d is None:
            if not ask:
                QMessageBox.information(
                    self, tr("Eject"), tr("The output is not on a removable drive.")
                )
            return
        r = QMessageBox.question(
            self,
            tr("Eject the drive"),
            tr("Eject {title} now?\nYou can unplug it afterwards.", title=d.title),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if r != QMessageBox.Yes:
            return
        self.status.setText(tr("Ejecting..."))
        job = Job(drives.eject, d)
        job.done.connect(self.ejected)
        job.failed.connect(lambda m=None, *_: self.eject_failed(m, False))
        job.start()

    def ejected(self, title: str):
        self.status.setText(tr("Ejected"))
        QMessageBox.information(
            self, tr("Eject"), tr("{title} can be unplugged.", title=title)
        )

    def eject_failed(self, msg: str, ask: bool):
        self.status.setText(tr("Done"))
        log.warning("eject: %s", msg)
        if not ask:
            QMessageBox.warning(self, tr("Eject"), msg)

    # ---- verification

    def verify_export(self):
        if self.job is not None:
            return
        start = self.out.text().strip()
        d = QFileDialog.getExistingDirectory(
            self, tr("Export to verify (the key or the output folder)"), start
        )
        if not d:
            return
        if not os.path.isfile(os.path.join(d, manifest.NAME)):
            QMessageBox.information(
                self,
                tr("Verify an export"),
                tr(
                    "No checksums in {path}.\nExport to it once with this version to create them.",
                    path=d,
                ),
            )
            return
        self.busy(True)
        self.job = Job(manifest.verify, d, allow_player_changes=True)
        self.job.progress.connect(self.on_progress)
        self.job.done.connect(self.verified)
        self.job.failed.connect(
            lambda m=None, *_: QMessageBox.critical(self, tr("Verification failed"), m)
        )
        self.job.cancelled.connect(
            lambda *_: self.status.setText(tr("Verification cancelled"))
        )
        self.job.finished.connect(self.job_over)
        self.job.start()

    # ------------------------------------------------
    def verified(self, chk: manifest.Check):
        self.bar.setValue(100)
        log.info("Verify %s: %s", chk.base, chk.summary())
        for p in chk.damaged:
            log.error("damaged: %s", p)
        for p in chk.missing:
            log.error("missing: %s", p)
        for p in chk.modified:
            log.warning("modified since export, integrity unconfirmed: %s", p)
        problems = bool(chk.error or chk.damaged or chk.missing)
        self.status.setText(
            tr("Verified")
            if chk.good
            else tr("Verification found problems")
            if problems
            else tr("Files modified since export; integrity unconfirmed")
        )
        if chk.good:
            QMessageBox.information(self, tr("Verify an export"), chk.report())
            return
        show_report = QMessageBox.warning if problems else QMessageBox.information
        show_report(self, tr("Verify an export"), chk.report())

    # ------------------------------------------------
    def closeEvent(self, e):
        if (
            self.other_jobs()
            or self.job is not None
            or any(
                panel.scan is not None for panel in self.findChildren(CheckCollection)
            )
        ):
            self.status.setText(
                tr(
                    "Cannot quit while writing, analysing or scanning. "
                    "Wait for completion or use the operation's Cancel button."
                )
            )
            e.ignore()
            return
        if not self.ask_discard():
            e.ignore()
            return
        if (
            self.cfg["confirm_exit"]
            and QMessageBox.question(
                self, tr("Quit"), tr("Quit {name}?", name=APP_NAME)
            )
            != QMessageBox.Yes
        ):
            e.ignore()
            return
        # the app quits with this window, the playlist windows would not be asked about their ID Tag fields
        for window in self.findChildren(PlaylistWindow):
            if window.isVisible() and not window.close():
                e.ignore()
                return
        stuck = []
        self.key_timer.stop()
        self.key_pending = False
        stuck = self.stop_probes()
        if stuck:
            log.warning("a read of the key is still running, closing anyway")
        if self.music_browser is not None:
            self.music_browser.close()
        if not stuck and not stop_all():
            self.status.setText(
                tr(
                    "Waiting for background operations to stop. Try closing again when they finish."
                )
            )
            e.ignore()
            return
        if not self.loader.stop():
            self.status.setText(
                tr(
                    "Waiting for waveform decoding to stop. Try closing again when it finishes."
                )
            )
            e.ignore()
            return
        self.engine.close()
        settings.save(self.sync())
        # a Log window left open has no parent and would keep the process alive, invisible, after the main window is gone
        if self.logwin is not None:
            self.logwin.close()
        super().closeEvent(e)
