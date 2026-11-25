# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Floating read-only library window: track model with filtering and duplicate marking, drag to playlists.

# floating, read-only library: preview a track, copy file URLs to a playlist

from __future__ import annotations

import os

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QMimeData,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import keys, library
from ..i18n import tr
from . import theme
from .player import Engine, clock
from .worker import Job

HEADERS = (
    "Artist",
    "Title",
    "Album",
    "Time",
    "BPM",
    "Key",
    "Format",
    "Folder",
    "Duplicates",
)


# --------------------------------------------------------
class LibraryModel(QAbstractTableModel):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.tracks = []
        self.folded = []
        self.groups: dict[str, int] = {}

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.tracks)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            # HEADERS stays the English list (the size of the table), the shown names are translated
            return (
                tr("Artist"),
                tr("Title"),
                tr("Album"),
                tr("Time"),
                "BPM",
                tr("Key"),
                tr("Format"),
                tr("Folder"),
                tr("Duplicates"),
            )[section]
        return None

    # ------------------------------------------------------
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        track = self.tracks[row]
        if role == Qt.UserRole + 1:
            return self.sort_value(row, col)
        group = self.groups.get(track.path, 0)
        if role == Qt.UserRole:
            return track
        if role == Qt.ToolTipRole:
            return track.path + (
                "\n" + tr("Exact file copy: group {group}", group=group) if group else ""
            )
        if role == Qt.DisplayRole:
            return (
                track.artist,
                track.title,
                track.album,
                clock(track.duration * 1000)[:5],
                f"{track.bpm:.2f}" if track.bpm else "",
                keys.name(track.key, self.cfg.get("key_format", "Open Key")),
                os.path.splitext(track.path)[1][1:].upper(),
                os.path.dirname(track.path),
                str(group) if group else "",
            )[col]
        return None

    # --------------------------------------------------------
    def sort_value(self, row, col):
        # petite optimisation: Qt asks for this O(n log n) times while sorting, build only the one value that is needed
        track = self.tracks[row]
        if col < 3:
            return self.folded[row][col]
        if col == 3:
            return track.duration
        if col == 4:
            return track.bpm
        if col == 5:
            # the key index, so 2A sorts before 10A whatever the chosen key format
            return track.key if track.key is not None and 0 <= track.key < 24 else -1
        if col == 6:
            return self.folded[row][3]
        if col == 7:
            return self.folded[row][4]
        return self.groups.get(track.path, 0)

    def flags(self, index):
        return (
            super().flags(index) | Qt.ItemIsDragEnabled
            if index.isValid()
            else Qt.NoItemFlags
        )

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        rows = sorted({index.row() for index in indexes if index.isValid()})
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self.tracks[row].path) for row in rows])
        return mime

    def supportedDragActions(self):
        return Qt.CopyAction

    def replace(self, tracks):
        self.beginResetModel()
        self.tracks = tracks
        self.folded = [
            (
                track.artist.casefold(),
                track.title.casefold(),
                track.album.casefold(),
                os.path.splitext(track.path)[1],
                os.path.dirname(track.path),
            )
            for track in tracks
        ]
        self.groups = {}
        self.endResetModel()

    def set_groups(self, groups):
        self.beginResetModel()
        self.groups = {
            path: number for number, paths in enumerate(groups, 1) for path in paths
        }
        self.endResetModel()


# --------------------------------------------------------
class LibraryFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.only_duplicates = False
        self.setSortRole(Qt.UserRole + 1)

    def filterAcceptsRow(self, row, _parent):
        model = self.sourceModel()
        track = model.tracks[row]
        return (not self.only_duplicates or track.path in model.groups) and (
            not self.query
            or self.query
            in f"{track.artist} {track.title} {track.album} {track.path}".casefold()
        )

    def update_filter(self, query, only_duplicates):
        # beginFilterChange needs Qt 6.9, invalidateFilter works on older versions
        newer = hasattr(self, "beginFilterChange")
        if newer:
            self.beginFilterChange()
        self.query, self.only_duplicates = query.strip().casefold(), only_duplicates
        if newer:
            self.endFilterChange()
        else:
            self.invalidateFilter()


# ------------------------------------------------------------------
class MusicBrowser(QWidget):
    preferences_requested = Signal()

    # --------------------------------------------------------
    def __init__(self, engine: Engine, cfg: dict, parent=None):
        super().__init__(parent, Qt.Window)
        self.engine, self.cfg = engine, cfg
        self.root = cfg.get("library_root", "")
        self.job: Job | None = None
        self.preview_track = None
        self.closed = False
        self.rescan_pending = False
        self.setWindowTitle(tr("Music browser"))
        self.resize(1050, 580)
        geometry = cfg.get("library_geometry", "")
        # a hand-edited value must not stop the window from opening
        if isinstance(geometry, str) and geometry.isascii():
            self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))

        top = QHBoxLayout()
        self.folder = QLabel()
        self.folder.setTextFormat(Qt.PlainText)
        self.folder.setWordWrap(True)
        self.folder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.preferences = QPushButton(tr("Choose folder..."))
        self.preferences.clicked.connect(self.preferences_requested)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.duplicates_button = QPushButton(tr("Scan duplicates"))
        self.duplicates_button.setToolTip(
            tr(
                "Compare file size and SHA-256; report exact copies without deleting anything"
            )
        )
        self.duplicates_button.clicked.connect(self.scan_duplicates)
        top.addWidget(self.folder, 1)
        top.addWidget(self.preferences)
        top.addWidget(self.refresh_button)
        top.addWidget(self.duplicates_button)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search artist, title, album or folder"))
        self.search.setAccessibleName(tr("Search music library"))
        self.only_duplicates = QCheckBox(tr("Duplicates only"))
        filters.addWidget(self.search, 1)
        filters.addWidget(self.only_duplicates)
        self.typing = QTimer(self)
        self.typing.setSingleShot(True)
        self.typing.setInterval(150)
        self.typing.timeout.connect(self.filter)
        self.search.textChanged.connect(self.typing.start)
        self.only_duplicates.toggled.connect(self.filter)

        self.model = LibraryModel(cfg, self)
        self.proxy = LibraryFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAccessibleName(tr("Music library tracks"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.setDefaultDropAction(Qt.CopyAction)
        self.table.verticalHeader().hide()
        for col, width in enumerate((150, 230, 160, 65, 70, 65, 70, 250, 85)):
            self.table.setColumnWidth(col, width)
        self.pointer_event = False
        self.table.clicked.connect(self.row_clicked)
        self.table.doubleClicked.connect(self.mark_pointer)
        self.table.activated.connect(self.row_activated)

        transport = QHBoxLayout()
        self.now = QLabel(
            tr("Click a track to preview; drag selected tracks to a playlist.")
        )
        self.now.setTextFormat(Qt.PlainText)
        self.now.setWordWrap(True)
        self.play_button = QPushButton(tr("Play / pause"))
        self.play_button.clicked.connect(self.toggle)
        self.stop_button = QPushButton(tr("Stop"))
        self.stop_button.clicked.connect(self.stop_preview)
        transport.addWidget(self.now, 1)
        transport.addWidget(self.play_button)
        transport.addWidget(self.stop_button)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.FG_MUTED};")
        bottom = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(180)
        self.bar.hide()
        self.cancel_button = QPushButton(tr("Cancel scan"))
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.bar)
        bottom.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addWidget(self.table, 1)
        layout.addLayout(transport)
        layout.addLayout(bottom)
        QShortcut(QKeySequence.Find, self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+R"), self, self.refresh)
        shortcut = QShortcut(QKeySequence("Space"), self.table, self.toggle)
        shortcut.setContext(Qt.WidgetShortcut)
        for deck, _output in self.engine.decks:
            deck.errorOccurred.connect(self.audio_error)
            deck.playbackStateChanged.connect(self.transport_changed)
        self.transport_changed()
        self.update_folder()

    def update_folder(self):
        self.folder.setText(
            self.root
            or tr("No music folder selected. Choose one in Preferences > Library.")
        )
        self.folder.setToolTip(self.root)

    # ------------------------------------------------------
    def set_root(self, root):
        if root == self.root:
            return
        self.root = root
        self.stop_preview()
        self.model.replace([])
        self.only_duplicates.setChecked(False)
        self.update_folder()
        if self.job is not None:
            self.rescan_pending = True
            self.cancel()
        elif self.isVisible():
            self.refresh()

    def open(self):
        self.closed = False
        self.show()
        self.raise_()
        self.activateWindow()
        if self.job is not None and self.job.stop.is_set():
            self.rescan_pending = True
        if not self.model.tracks and self.job is None:
            self.refresh()

    def refresh(self):
        if self.job is not None:
            return
        if not self.root:
            self.status.setText(
                tr("Choose a music folder in Preferences > Library, then Refresh.")
            )
            return
        self.start_job(Job(library.scan, self.root), self.scanned)

    def scan_duplicates(self):
        if self.job is not None:
            return
        if not self.model.tracks:
            self.status.setText(
                tr("Refresh the music library before scanning duplicates.")
            )
            return
        self.start_job(
            Job(library.duplicates, [track.path for track in self.model.tracks]),
            self.duplicates_scanned,
        )

    # --------------------------------------------------------
    def start_job(self, job, done):
        self.job = job
        job.progress.connect(self.progress)
        job.done.connect(done)
        job.failed.connect(self.failed)
        job.cancelled.connect(self.cancelled)
        job.finished.connect(self.finished)
        self.refresh_button.setEnabled(False)
        self.duplicates_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.bar.setRange(0, 0)
        self.bar.show()
        self.status.setText(tr("Scanning..."))
        job.start()

    def progress(self, pct, message):
        if self.closed:
            return
        if pct:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)
        self.status.setText(message)

    def accept_result(self):
        return not self.closed and self.job is not None and not self.job.stop.is_set()

    def scanned(self, result):
        if not self.accept_result():
            self.cancelled()
            return
        self.stop_preview()
        self.only_duplicates.setChecked(False)
        self.model.replace(result.tracks)
        self.filter()
        self.show_errors(result.errors)

    def duplicates_scanned(self, result):
        if not self.accept_result():
            self.cancelled()
            return
        self.model.set_groups(result.groups)
        self.filter()
        self.status.setText(
            tr(
                "{groups} exact duplicate group(s), {files} files. Nothing was deleted.",
                groups=len(result.groups),
                files=len(self.model.groups),
            )
        )
        self.show_errors(result.errors)

    def show_errors(self, errors):
        if errors:
            self.status.setText(
                self.status.text()
                + " | "
                + tr("{n} file(s) could not be checked.", n=len(errors))
            )
            message = QMessageBox(
                QMessageBox.Warning,
                tr("Music library scan"),
                tr(
                    "{n} file(s) could not be checked. See details; refresh after fixing them.",
                    n=len(errors),
                ),
                QMessageBox.Ok,
                self,
            )
            message.setDetailedText("\n".join(errors))
            message.exec()

    def failed(self, message):
        if not self.closed:
            self.status.setText(
                tr("Scan failed: {message}. Previous results kept.", message=message)
            )
            QMessageBox.warning(self, tr("Music library scan"), message)

    def cancelled(self):
        if not self.closed:
            self.status.setText(tr("Scan cancelled; previous results kept."))

    def finished(self):
        job, self.job = self.job, None
        if job is not None:
            job.deleteLater()
        self.refresh_button.setEnabled(True)
        self.duplicates_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.bar.hide()
        if self.rescan_pending and not self.closed:
            self.rescan_pending = False
            self.refresh()

    def cancel(self):
        if self.job is not None:
            self.job.stop.set()

    def filter(self):
        self.proxy.update_filter(self.search.text(), self.only_duplicates.isChecked())
        self.status.setText(
            tr(
                "{shown} of {total} tracks | {files} exact duplicate files",
                shown=self.proxy.rowCount(),
                total=len(self.model.tracks),
                files=len(self.model.groups),
            )
        )

    def mark_pointer(self, _index=None):
        # attention -> bug = a mouse click is followed by "activated" in the same event, it previewed twice
        self.pointer_event = True
        QTimer.singleShot(0, self.clear_pointer)

    def clear_pointer(self):
        self.pointer_event = False

    def row_clicked(self, index):
        self.mark_pointer()
        self.preview(index)

    def row_activated(self, index):
        if not self.pointer_event:
            self.preview(index)

    # --------------------------------------------------------
    def preview(self, index):
        track = index.data(Qt.UserRole)
        if track is None:
            return
        if not os.path.isfile(track.path):
            self.status.setText(
                tr("File unavailable: {path}. Refresh the library.", path=track.path)
            )
            return
        self.preview_track = track
        if self.engine.timing_track is track:
            self.engine.toggle()
        else:
            self.engine.loop = None
            if not self.engine.play(track.path, track):
                self.status.setText(tr("Cannot preview: {path}", path=track.path))
                return
        self.now.setText(
            tr(
                "Preview: {artist} - {title}", artist=track.artist, title=track.title
            )
            if track.artist
            else tr("Preview: {title}", title=track.title)
        )
        self.transport_changed()

    def toggle(self):
        if (
            self.preview_track is not None
            and self.engine.timing_track is self.preview_track
        ):
            self.engine.toggle()
        else:
            self.preview(self.table.currentIndex())

    def stop_preview(self):
        if (
            self.preview_track is not None
            and self.engine.timing_track is self.preview_track
        ):
            self.engine.stop()
        self.preview_track = None
        self.now.setText(
            tr("Click a track to preview; drag selected tracks to a playlist.")
        )
        self.transport_changed()

    def transport_changed(self, _state=None):
        owns = (
            self.preview_track is not None
            and self.engine.timing_track is self.preview_track
        )
        self.play_button.setText(
            tr("Pause") if owns and self.engine.playing else tr("Play")
        )
        self.stop_button.setEnabled(owns)

    def audio_error(self, _code, message):
        if (
            self.preview_track is not None
            and self.engine.timing_track is self.preview_track
        ):
            self.status.setText(tr("Preview failed: {message}", message=message))

    def closeEvent(self, event):
        self.closed = True
        self.rescan_pending = False
        self.cancel()
        self.typing.stop()
        self.stop_preview()
        self.cfg["library_geometry"] = bytes(self.saveGeometry().toHex()).decode(
            "ascii"
        )
        super().closeEvent(event)
