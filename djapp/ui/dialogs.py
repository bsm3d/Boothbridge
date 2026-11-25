# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Qt dialogs for options, collection check, log, usage and about, plus the key and player settings choices.

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCursor
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, AUTHOR, SITE, VERSION, WEBSITE, keys
from ..brand import SUPPORT_URL
from .. import i18n
from ..i18n import tr
from ..i18n import trn
from .. import cachebackup
from ..model import Node, Track, playlists
from ..settings import exports
from ..settings import cache_dir
from ..export.cdj import reference
from ..sources import index_folder
from . import formatbox, theme
from .worker import Job

SHOWN = 8

# ----------------------------------------------------------------
def name_lines(names: list[str]) -> str:
    lines = [f"    {name}" for name in names[:SHOWN]]
    if len(names) > SHOWN:
        lines.append("    " + tr("... and {n} more", n=len(names) - SHOWN))
    return "\n".join(lines)

# ----------------------------------------------------------------
# "a and b and c": the word between two names comes from the catalog
def join_and(items: list[str]) -> str:
    text = items[0]
    for item in items[1:]:
        text = tr("{a} and {b}", a=text, b=item)
    return text

# ----------------------------------------------------------------
# what to do with the playlists already on the key. choice is "keep", "replace" or "" (cancel).
# All the text is plain: the names come from files
class KeyChoice(QDialog):
    # ----------------------------------------------------------------
    def __init__(self, summary, replaced: list[str], parent=None, note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr("This key already has playlists"))
        self.choice = ""
        self.readable = bool(getattr(summary, "readable", True))
        names = list(getattr(summary, "names", []))
        if self.readable:
            text = tr(
                "The key holds {playlists} playlists in {folders} folders, {tracks} tracks.",
                playlists=summary.playlists,
                folders=summary.folders,
                tracks=summary.tracks,
            )
            if names:
                text += "\n" + name_lines(names)
            if replaced:
                text += (
                    "\n\n"
                    + tr("Playlists with the same name will be replaced:")
                    + "\n"
                    + name_lines(list(replaced))
                )
            text += "\n\n" + tr(
                "Keep them: the playlists on the key stay and this export is added."
            )
        else:
            text = (
                tr(
                    "The database on this key cannot be read, so the playlists on it cannot be kept."
                )
                + "\n\n"
                + tr("Replace it with this export, or cancel and look at the key first.")
            )
            if note:
                text += "\n\n" + note
        others = list(getattr(summary, "others", []))
        if others:
            text += "\n\n" + tr(
                "This key also holds {names}, written by rekordbox. Boothbridge only "
                "writes export.pdb and leaves them as they are, so a player that reads them instead may not "
                "show the playlists of this export. Test the key on your player.",
                names=join_and(others),
            )
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(460)
        self.again = QCheckBox(tr("Do not ask again"))
        self.again.setVisible(self.readable)
        self.keep_btn = QPushButton(tr("Keep them and add this export"))
        self.replace_btn = QPushButton(
            tr("Replace everything on the key")
            if self.readable
            else tr("Replace (a backup of the old database is kept)")
        )
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.keep_btn.setVisible(self.readable)
        self.keep_btn.clicked.connect(lambda *_: self.pick("keep"))
        self.replace_btn.clicked.connect(lambda *_: self.pick("replace"))
        self.cancel_btn.clicked.connect(self.reject)
        (self.keep_btn if self.readable else self.cancel_btn).setDefault(True)
        row = QHBoxLayout()
        row.addWidget(self.keep_btn)
        row.addWidget(self.replace_btn)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(label)
        lay.addSpacing(6)
        lay.addWidget(self.again)
        lay.addLayout(row)

    @property
    def remember(self) -> bool:
        return self.readable and self.again.isChecked()

    def pick(self, choice: str):
        if choice == "replace" and self.remember and not self.confirm_replace():
            return
        self.choice = choice
        self.accept()

    def confirm_replace(self) -> bool:
        return ask_replace_always(self)

# ----------------------------------------------------------------
def ask_replace_always(parent) -> bool:
    r = QMessageBox.question(
        parent,
        tr("Replace every time"),
        tr("Always replace what is on the key?")
        + "\n\n"
        + tr(
            "The existing playlists will be removed from the key, a backup of the old database is kept."
        ),
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return r == QMessageBox.Yes

# ----------------------------------------------------------------
# A message in plain text, because the names in it come from files. Gives back the label of
# the button that was clicked, or "" when it was closed. A folder adds an Open the folder button
def box(
    parent,
    icon,
    title: str,
    text: str,
    *labels: str,
    folder: str = "",
    default: str = "",
) -> str:
    m = QMessageBox(icon, title, text, QMessageBox.NoButton, parent)
    m.setTextFormat(Qt.PlainText)
    labels = labels or (tr("OK"),)
    for label in labels:
        b = m.addButton(
            label,
            QMessageBox.RejectRole if label == tr("Cancel") else QMessageBox.AcceptRole,
        )
        if label == (default or labels[0]):
            m.setDefaultButton(b)
    opener = (
        m.addButton(tr("Open the folder"), QMessageBox.ActionRole) if folder else None
    )
    m.exec()
    hit = m.clickedButton()
    if hit is None:
        return ""
    if hit is opener:
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    return hit.text()

# ----------------------------------------------------------------
class PlayerSettings(QDialog):
    def __init__(self, chosen: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Player settings"))
        names = {
            9: tr("On air display"),
            11: tr("Quantize"),
            16: tr("Jog ring indicator"),
            17: tr("Slip flashing"),
            23: tr("Sync"),
            26: tr("Hot cue auto load"),
            27: tr("Hot cue colour"),
            35: tr("Auto cue"),
            36: tr("Master tempo"),
        }
        have = reference.parse_choices(chosen)
        form = QFormLayout()
        self.boxes: dict[int, QComboBox] = {}
        for position in reference.SWITCHES:
            box = QComboBox()
            box.addItem(tr("Leave as it is"), None)
            box.addItem(tr("On"), reference.ON)
            box.addItem(tr("Off"), reference.OFF)
            box.setCurrentIndex(max(0, box.findData(have.get(position))))
            self.boxes[position] = box
            form.addRow(names[position], box)
        note = QLabel(
            tr(
                "Written to MYSETTING.DAT on the key at each CDJ/USB export. Only the settings "
                "you change here are written, the others stay as the key has them."
            )
        )
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(buttons)

    # the choices in the form the settings file keeps
    def chosen(self) -> str:
        return reference.format_choices(
            {p: box.currentData() for p, box in self.boxes.items() if box.currentData() is not None}
        )

class Options(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(tr("{app} - Options", app=APP_NAME))
        self.resize(520, 420)
        tabs = QTabWidget()

        ex = QWidget()
        f = QFormLayout(ex)
        self.fmt = QComboBox()
        formatbox.fill(self.fmt, exports(), cfg["export_format"])
        self.keyfmt = QComboBox()
        self.keyfmt.addItems(keys.NOTATIONS)
        self.keyfmt.setCurrentText(cfg["key_format"])
        self.copy = QCheckBox(tr("Copy audio files with the export"))
        self.copy.setChecked(cfg["copy_music"])
        self.verify = QCheckBox(tr("Read the whole export back and check it"))
        self.verify.setChecked(cfg["verify_copy"])
        self.on_key = QComboBox()
        self.on_key.addItems(
            [tr("Ask me"), tr("Keep them and add"), tr("Replace them")]
        )
        self.on_key.setCurrentIndex(
            0
            if cfg.get("key_ask", True)
            else 2
            if cfg.get("key_mode") == "replace"
            else 1
        )
        f.addRow(tr("Export format"), self.fmt)
        f.addRow(tr("Key notation"), self.keyfmt)
        f.addRow(self.copy)
        f.addRow(self.verify)
        f.addRow(tr("When the key already has playlists"), self.on_key)
        f.addRow(
            QLabel(
                tr("CDJ/USB always copies the audio: the players only read the key.")
            )
        )
        tabs.addTab(ex, tr("Export"))

        cdj = QWidget()
        f = QFormLayout(cdj)
        self.procs = QSpinBox()
        self.procs.setRange(0, 32)
        self.procs.setSpecialValueText(tr("Auto (up to 8)"))
        self.procs.setValue(int(cfg["anlz_processes"]))
        self.job = None
        self.count = 0
        group = QGroupBox(tr("Analysis cache"))
        grid = QGridLayout(group)
        self.cache_lbl = QLabel("")
        self.open_btn = QPushButton(tr("Open the cache folder"))
        self.backup_btn = QPushButton(tr("Back up the analyses..."))
        self.restore_btn = QPushButton(tr("Restore analyses..."))
        self.clear_btn = QPushButton(tr("Clear analysis cache"))
        self.open_btn.clicked.connect(self.open_cache)
        self.backup_btn.clicked.connect(lambda *_: self.back_up())
        self.restore_btn.clicked.connect(self.restore)
        self.clear_btn.clicked.connect(self.clear_cache)
        hint = QLabel(
            tr("Deleting the cache only costs analysis time at the next export.")
        )
        hint.setWordWrap(True)
        grid.addWidget(self.cache_lbl, 0, 0, 1, 2)
        grid.addWidget(self.open_btn, 1, 0)
        grid.addWidget(self.backup_btn, 1, 1)
        grid.addWidget(self.restore_btn, 2, 0)
        grid.addWidget(self.clear_btn, 2, 1)
        grid.addWidget(hint, 3, 0, 1, 2)
        f.addRow(tr("Analysis processes"), self.procs)
        self.player = str(cfg.get("player_settings", ""))
        self.player_btn = QPushButton(tr("Player settings..."))
        self.player_btn.clicked.connect(self.pick_player)
        f.addRow(tr("Settings the players read from the key"), self.player_btn)
        f.addRow(group)
        f.addRow(
            QLabel(
                tr(
                    "Target: CDJ-2000NXS2 and compatible players,\n"
                    "FAT32 or exFAT key (NTFS is not read by the players)."
                )
            )
        )
        tabs.addTab(cdj, "CDJ")

        pl = QWidget()
        f = QFormLayout(pl)
        self.wave = QComboBox()
        # the setting keeps the English name, the combo shows it in the language
        for name, label in (("RGB", "RGB"), ("Blue", tr("Blue")), ("IR", "IR")):
            self.wave.addItem(label, name)
        self.wave.setCurrentIndex(max(0, self.wave.findData(cfg["waveform_color"])))
        self.fade = QSpinBox()
        self.fade.setRange(0, 10)
        self.fade.setSuffix(" s")
        self.fade.setValue(int(cfg["crossfade"]))
        self.vol = QSpinBox()
        self.vol.setRange(0, 100)
        self.vol.setSuffix(" %")
        self.vol.setValue(int(cfg["volume"]))
        f.addRow(tr("Waveform colours"), self.wave)
        f.addRow(tr("Crossfade (continue mode)"), self.fade)
        f.addRow(tr("Preview volume"), self.vol)
        tabs.addTab(pl, tr("Player"))

        self.tabs = tabs
        library = QWidget()
        f = QFormLayout(library)
        self.library_root = QLineEdit(cfg.get("library_root", ""))
        self.library_root.setPlaceholderText(
            tr("Folder containing your music (including subfolders)")
        )
        self.library_root.setAccessibleName(tr("Music library folder"))
        choose_library = QPushButton(tr("Browse..."))
        choose_library.clicked.connect(self.pick_library)
        row = QHBoxLayout()
        row.addWidget(self.library_root, 1)
        row.addWidget(choose_library)
        f.addRow(tr("Music library folder"), row)
        note = QLabel(
            tr(
                "The Music browser reads this folder and its subfolders.\n"
                "It does not change the Music folder used to find missing files.\n"
                "Refresh finds new music. Scan duplicates compares exact file copies\n"
                "and never deletes audio."
            )
        )
        note.setWordWrap(True)
        f.addRow(note)
        self.library_tab = library
        tabs.addTab(library, tr("Library"))

        app = QWidget()
        f = QFormLayout(app)
        self.auto = QCheckBox(tr("Load the last collection on startup"))
        self.auto.setChecked(cfg["auto_load"])
        self.confirm = QCheckBox(tr("Ask before quitting"))
        self.confirm.setChecked(cfg["confirm_exit"])
        self.level = QComboBox()
        self.level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.level.setCurrentText(cfg["log_level"])
        self.language = QComboBox()
        for code, name in i18n.LANGUAGES.items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(cfg["language"])))
        f.addRow(self.auto)
        f.addRow(self.confirm)
        f.addRow(tr("Language"), self.language)
        f.addRow(tr("Log level"), self.level)
        tabs.addTab(app, tr("Application"))

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)
        self.show_cache()

    # ----------------------------------------------------------------
    def show_cache(self):
        # a scandir with the sizes it already holds, no extra call per file
        self.count, size = cachebackup.stats(cache_dir())
        self.cache_lbl.setText(
            trn(
                self.count,
                "{n} file, {size} MB",
                "{n} files, {size} MB",
                size=f"{size / 1e6:.1f}",
            )
        )
        idle = self.job is None
        self.open_btn.setToolTip(str(cache_dir()))
        self.restore_btn.setToolTip(
            tr("Put the files of a backup back in the cache. Files already there are kept.")
        )
        self.restore_btn.setEnabled(idle)
        for btn, text, empty in (
            (
                self.backup_btn,
                tr("Save the cache files in a ZIP file."),
                tr("The cache is empty, there is nothing to back up."),
            ),
            (
                self.clear_btn,
                tr("Delete the cache files. The next export analyses again."),
                tr("The cache is empty, there is nothing to clear."),
            ),
        ):
            btn.setEnabled(idle and bool(self.count))
            btn.setToolTip(text if self.count else empty)

    def open_cache(self):
        folder = cache_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            box(
                self,
                QMessageBox.Warning,
                tr("Analysis cache"),
                tr(
                    "The cache folder cannot be created:\n{folder}\n\n{error}",
                    folder=folder,
                    error=e,
                ),
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            box(
                self,
                QMessageBox.Warning,
                tr("Analysis cache"),
                tr("The folder could not be opened:\n{folder}", folder=folder),
            )

    # ----------------------------------------------------------------
    # where the last backup went, else Documents
    def last_dir(self) -> str:
        last = self.cfg.get("cache_backup_dir", "")
        if last and os.path.isdir(last):
            return last
        return QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or str(
            Path.home()
        )

    # ----------------------------------------------------------------
    # runs fn on a Job behind a progress dialog with a Cancel button. done(result) runs here
    # when it ends well, note is added to the message when it is cancelled or fails
    def work(self, fn, args, title: str, label: str, done, note: str = ""):
        bar = QProgressDialog(label, tr("Cancel"), 0, 100, self)
        bar.setWindowTitle(title)
        bar.setWindowModality(Qt.WindowModal)
        bar.setMinimumDuration(0)
        bar.setAutoClose(False)
        bar.setAutoReset(False)
        job = Job(fn, *args)
        bar.canceled.connect(job.stop.set)
        job.progress.connect(
            lambda pct=None, text=None, *_: (bar.setValue(pct), bar.setLabelText(text))
        )
        job.done.connect(lambda res=None, *_: self.over(bar, lambda: done(res)))
        job.failed.connect(
            lambda msg=None, *_: self.over(
                bar,
                lambda: box(
                    self, QMessageBox.Critical, title, f"{msg}\n\n{note}".strip()
                ),
            )
        )
        job.cancelled.connect(
            lambda *_: self.over(
                bar,
                lambda: box(
                    self,
                    QMessageBox.Information,
                    title,
                    f"{tr('Cancelled.')}\n\n{note}".strip(),
                ),
            )
        )
        self.job = job
        self.show_cache()
        bar.show()
        job.start()

    def over(self, bar, then):
        bar.close()
        bar.deleteLater()
        self.job = None
        self.show_cache()
        then()

    # ----------------------------------------------------------------
    def back_up(self, then_clear: bool = False):
        if self.job is not None or not self.count:
            return
        name = f"Boothbridge-analyses-{datetime.now().astimezone():%Y-%m-%d}.zip"
        dest, _ = QFileDialog.getSaveFileName(
            self,
            tr("Back up the analyses"),
            str(Path(self.last_dir()) / name),
            "Zip (*.zip)",
        )
        if not dest:
            if then_clear:
                box(
                    self,
                    QMessageBox.Information,
                    tr("Clear analysis cache"),
                    tr("No backup was made, so nothing was cleared."),
                )
            return
        if not Path(dest).suffix:
            dest += ".zip"
        note = tr("The cache was not cleared.") if then_clear else ""
        self.work(
            cachebackup.create,
            (cache_dir(), dest),
            tr("Back up the analyses"),
            tr("Saving the analyses"),
            lambda res: self.backed_up(res, dest, then_clear),
            note,
        )

    # ----------------------------------------------------------------
    def backed_up(self, res, dest: str, then_clear: bool):
        self.cfg["cache_backup_dir"] = os.path.dirname(dest)
        text = tr(
            "{files} files saved ({size} MB) in\n{dest}",
            files=res.files,
            size=f"{res.size / 1e6:.1f}",
            dest=dest,
        )
        text += "\n\n" + tr(
            "Its checksum is in {name}.sha256, next to it.", name=os.path.basename(dest)
        )
        if res.failed:
            text += (
                "\n\n"
                + tr(
                    "{n} files could not be read and are not in the backup:",
                    n=len(res.failed),
                )
                + "\n"
            )
            text += "\n".join(cachebackup.shown(line) for line in res.failed[:5])
            if len(res.failed) > 5:
                text += "\n..."
        folder = os.path.dirname(dest)
        if not then_clear:
            box(
                self,
                QMessageBox.Warning if res.failed else QMessageBox.Information,
                tr("Back up the analyses"),
                text,
                tr("OK"),
                folder=folder,
            )
        elif res.failed:
            box(
                self,
                QMessageBox.Warning,
                tr("Back up the analyses"),
                text
                + "\n\n"
                + tr("The cache was not cleared, the backup is not complete."),
                tr("OK"),
                folder=folder,
            )
        else:
            self.clear_now(text, folder)

    def clear_cache(self):
        if self.job is not None or not self.count:
            return
        first, second = tr("Back up, then clear"), tr("Clear without a backup")
        pick = box(
            self,
            QMessageBox.Question,
            tr("Clear analysis cache"),
            tr("Back up the analyses before clearing them?")
            + "\n\n"
            + tr(
                "The cache holds {n} files. Without them, the next export analyses the tracks again.",
                n=self.count,
            ),
            first,
            second,
            tr("Cancel"),
        )
        if pick == first:
            self.back_up(then_clear=True)
        elif pick == second:
            self.clear_now()

    def clear_now(self, saved: str = "", folder: str = ""):
        self.work(
            cachebackup.clear,
            (cache_dir(),),
            tr("Clear analysis cache"),
            tr("Clearing the cache"),
            lambda res: self.cleared(res, saved, folder),
        )

    def cleared(self, res, saved: str, folder: str):
        text = tr("{n} files deleted.", n=res.files)
        if res.failed:
            text += (
                "\n\n"
                + tr("{n} files could not be deleted:", n=len(res.failed))
                + "\n"
            )
            text += "\n".join(cachebackup.shown(line) for line in res.failed[:5])
        if saved:
            box(
                self,
                QMessageBox.Warning if res.failed else QMessageBox.Information,
                tr("Clear analysis cache"),
                f"{saved}\n\n{text}",
                tr("OK"),
                folder=folder,
            )
        elif res.failed:
            box(self, QMessageBox.Warning, tr("Clear analysis cache"), text)

    # ----------------------------------------------------------------
    def restore(self):
        if self.job is not None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Restore analyses"), self.last_dir(), "Zip (*.zip)"
        )
        if not path:
            return
        if not os.path.exists(path + ".sha256"):
            cancel, anyway = tr("Cancel"), tr("Restore anyway")
            pick = box(
                self,
                QMessageBox.Warning,
                tr("Restore analyses"),
                tr(
                    "There is no .sha256 file next to this ZIP, so it cannot be checked for damage."
                )
                + "\n\n"
                + tr("Restore it anyway?"),
                cancel,
                anyway,
                default=cancel,
            )
            if pick != anyway:
                return
        self.work(
            cachebackup.restore,
            (path, cache_dir()),
            tr("Restore analyses"),
            tr("Checking the backup"),
            lambda res: self.restored(res, path),
            tr("No file of the cache was replaced."),
        )

    def restored(self, res, path: str):
        self.cfg["cache_backup_dir"] = os.path.dirname(path)
        text = tr(
            "{restored} restored, {present} already there, {skipped} skipped.",
            restored=res.files,
            present=res.present,
            skipped=res.skipped,
        )
        if res.notes:
            text += "\n\n" + "\n".join(res.notes)
        text += "\n\n" + tr("A file that was already in the cache is never replaced.")
        box(self, QMessageBox.Information, tr("Restore analyses"), text)

    # --------------------------------------------
    def accept(self):
        root = self.library_root.text().strip()
        # a folder that was saved earlier may be on a drive that is not plugged in now
        if (
            root
            and root != self.cfg.get("library_root", "").strip()
            and not os.path.isdir(root)
        ):
            QMessageBox.warning(
                self,
                tr("Music library"),
                tr("Choose an existing music folder, or leave it empty."),
            )
            return
        ask = self.on_key.currentIndex() == 0
        already = (
            not self.cfg.get("key_ask", True) and self.cfg.get("key_mode") == "replace"
        )
        if (
            self.on_key.currentIndex() == 2
            and not already
            and not ask_replace_always(self)
        ):
            return
        # set once nothing can stop the dialog: a Cancel after a refused question changes nothing
        self.cfg["library_root"] = root
        if not ask:
            self.cfg["key_mode"] = (
                "replace" if self.on_key.currentIndex() == 2 else "keep"
            )
        self.cfg["key_ask"] = ask
        self.cfg["anlz_processes"] = self.procs.value()
        self.cfg["player_settings"] = self.player
        self.cfg.update(
            export_format=formatbox.name(self.fmt),
            key_format=self.keyfmt.currentText(),
            copy_music=self.copy.isChecked(),
            verify_copy=self.verify.isChecked(),
            waveform_color=self.wave.currentData(),
            crossfade=self.fade.value(),
            volume=self.vol.value(),
            auto_load=self.auto.isChecked(),
            confirm_exit=self.confirm.isChecked(),
            log_level=self.level.currentText(),
        )
        # the windows are built in the language the program started with
        chosen = self.language.currentData()
        if chosen != self.cfg["language"]:
            self.cfg["language"] = chosen
        if chosen != i18n.language():
            QMessageBox.information(
                self,
                tr("Language"),
                tr("The language changes the next time the program starts."),
            )
        super().accept()

    def pick_player(self, *_):
        dlg = PlayerSettings(self.player, self)
        try:
            if dlg.exec():
                self.player = dlg.chosen()
        finally:
            dlg.deleteLater()

    def pick_library(self):
        root = QFileDialog.getExistingDirectory(
            self, tr("Music library folder"), self.library_root.text()
        )
        if root:
            self.library_root.setText(root)

# ----------------------------------------------------------------
class _Scan(QThread):
    found = Signal(object)

    def __init__(self, root: str):
        super().__init__()
        self.root = root

    def run(self):
        self.found.emit(index_folder(self.root))

# missing files and their relocation. Tracks are shared between playlists,
# so relocating one entry fixes it everywhere
class CheckCollection(QDialog):
    relocated = Signal()

    # ----------------------------------------------------------------
    def __init__(self, nodes: list[Node], music_root: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Check collection"))
        self.resize(940, 560)
        self.root = music_root
        self.scan = None
        seen, self.items = set(), []
        for pl in playlists(nodes):
            for t in pl.tracks:
                if id(t) not in seen:
                    seen.add(id(t))
                    self.items.append((t, pl.name))

        self.stats = QLabel("")
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Filter by artist, title or path"))
        self.typing = QTimer(self)
        self.typing.setSingleShot(True)
        self.typing.setInterval(150)
        self.typing.timeout.connect(self.fill)
        self.search.textChanged.connect(self.typing.start)
        self.which = QComboBox()
        self.which.addItems([tr("All tracks"), tr("Missing only"), tr("Found only")])
        self.which.setCurrentIndex(1)
        self.which.currentIndexChanged.connect(self.fill)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.which)

        self.table = QTreeWidget()
        self.table.setHeaderLabels(
            ["", tr("Artist"), tr("Title"), tr("Playlist"), tr("File")]
        )
        self.table.setRootIsDecorated(False)
        for i, w in enumerate((28, 170, 220, 130, 360)):
            self.table.setColumnWidth(i, w)

        folder = QHBoxLayout()
        self.folder = QLineEdit(music_root)
        self.folder.setReadOnly(True)
        pick = QPushButton(tr("Browse..."))
        pick.clicked.connect(self.pick)
        folder.addWidget(QLabel(tr("Search folder")))
        folder.addWidget(self.folder, 1)
        folder.addWidget(pick)

        btns = QHBoxLayout()
        for text, fn in (
            (tr("Relocate all missing"), self.auto),
            (tr("Locate selected..."), self.locate),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        btns.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.stats)
        lay.addLayout(bar)
        lay.addWidget(self.table, 1)
        lay.addLayout(folder)
        lay.addLayout(btns)
        self.check()

    def check(self):
        self.ok = {
            id(t): bool(t.path) and os.path.isfile(t.path) for t, _ in self.items
        }
        miss = sum(not v for v in self.ok.values())
        self.stats.setText(
            tr(
                "{n} tracks, {found} found, {missing} missing",
                n=len(self.items),
                found=len(self.items) - miss,
                missing=miss,
            )
        )
        self.fill()

    # ----------------------------------------------------------------
    def fill(self):
        q = self.search.text().lower()
        mode = self.which.currentIndex()
        green, red = QColor(theme.GREEN), QColor(theme.RED)
        self.table.setUpdatesEnabled(False)
        self.table.clear()
        items = []
        for t, pl in self.items:
            ok = self.ok[id(t)]
            if (mode == 1 and ok) or (mode == 2 and not ok):
                continue
            if q and q not in f"{t.artist} {t.title} {t.path}".lower():
                continue
            it = QTreeWidgetItem(["✓" if ok else "✗", t.artist, t.title, pl, t.path])
            it.setForeground(0, green if ok else red)
            if not ok:
                for c in range(1, 5):
                    it.setForeground(c, red)
            it.setToolTip(4, t.path)
            it.setData(0, Qt.UserRole, t)
            items.append(it)
        self.table.addTopLevelItems(items)
        self.table.setUpdatesEnabled(True)

    def pick(self):
        d = QFileDialog.getExistingDirectory(self, tr("Folder to search"), self.root)
        if d:
            self.root = d
            self.folder.setText(d)

    # ----------------------------------------------------------------
    def auto(self):
        if not self.root or not os.path.isdir(self.root):
            QMessageBox.warning(
                self, tr("Relocate"), tr("Pick the folder to search first.")
            )
            return
        missing = [t for t, _ in self.items if not self.ok[id(t)]]
        if not missing:
            QMessageBox.information(self, tr("Relocate"), tr("Nothing is missing."))
            return
        self.stats.setText(tr("Scanning {folder}...", folder=self.root))
        self.setEnabled(False)
        self.scan = _Scan(self.root)
        self.scan.found.connect(lambda idx=None, *_: self.apply(missing, idx))
        self.scan.finished.connect(self.scan_finished)
        self.scan.finished.connect(self.scan.deleteLater)
        self.scan.start()

    def scan_finished(self):
        if self.scan is self.sender():
            self.scan = None

    def done(self, result):
        if self.scan is not None and self.scan.isRunning():
            self.stats.setText(
                tr("Waiting for the folder scan to finish before closing.")
            )
            return
        super().done(result)

    def closeEvent(self, event):
        if self.scan is not None and self.scan.isRunning():
            self.stats.setText(
                tr("Waiting for the folder scan to finish before closing.")
            )
            event.ignore()
            return
        super().closeEvent(event)

    # --------------------------------------------------------------
    def apply(self, missing: list[Track], idx: dict):
        self.setEnabled(True)
        done = skipped = 0
        for t in missing:
            hits = idx.get(os.path.basename(t.path).lower(), [])
            if len(hits) > 1:
                pick, ok = QInputDialog.getItem(
                    self,
                    tr("Several matches"),
                    f"{t.artist} - {t.title}",
                    hits,
                    0,
                    False,
                )
                hits = [pick] if ok else []
            if hits:
                t.path = os.path.normpath(hits[0])
                done += 1
            else:
                skipped += 1
        self.check()
        if done:
            self.relocated.emit()
        QMessageBox.information(
            self,
            tr("Relocate"),
            tr(
                "Relocated: {done}\nNot found or skipped: {skipped}",
                done=done,
                skipped=skipped,
            ),
        )

    # ----------------------------------------------------------------
    def locate(self):
        it = self.table.currentItem()
        if it is None:
            return
        t = it.data(0, Qt.UserRole)
        start = (
            os.path.dirname(t.path)
            if os.path.isdir(os.path.dirname(t.path))
            else self.root
        )
        f, _ = QFileDialog.getOpenFileName(
            self,
            tr("Locate {name}", name=f"{t.artist} - {t.title}"),
            start,
            f"{tr('Audio')} (*.mp3 *.flac *.aiff *.aif *.wav *.aac *.ogg);;{tr('All')} (*)",
        )
        if f:
            t.path = os.path.normpath(f)
            self.check()
            self.relocated.emit()

# ---- log
LEVEL_COLORS = {
    "DEBUG": theme.FG_MUTED,
    "INFO": theme.FG,
    "WARNING": theme.YELLOW,
    "ERROR": theme.RED,
    "CRITICAL": theme.RED,
}

# ----------------------------------------------------------------
# logging.Handler usable from any thread: records cross into the GUI by signal
class LogBridge(QObject, logging.Handler):
    line = Signal(str, str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, rec):
        try:
            self.line.emit(self.format(rec), rec.levelname)
        except RuntimeError:
            pass  # window already gone at shutdown

# ----------------------------------------------------------------
class LogWindow(QDialog):
    MAX = 5000

    # ----------------------------------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Log"))
        self.resize(820, 500)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
        self.text.document().setMaximumBlockCount(self.MAX)
        btns = QHBoxLayout()
        for label, fn in (
            (tr("Clear"), self.text.clear),
            (tr("Copy all"), self.copy),
            (tr("Save..."), self.save),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.hide)
        btns.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.text)
        lay.addLayout(btns)

    def add(self, msg: str, level: str):
        col = LEVEL_COLORS.get(level, theme.FG)
        t = datetime.now().astimezone().strftime("%H:%M:%S")
        self.text.append(
            f'<span style="color:{col}">[{t}] {level}: {html.escape(msg)}</span>'
        )

    def copy(self):
        QApplication.clipboard().setText(self.text.toPlainText())

    def save(self):
        name = f"boothbridge_log_{datetime.now().astimezone():%Y%m%d_%H%M%S}.txt"
        f, _ = QFileDialog.getSaveFileName(
            self, tr("Save log"), name, f"{tr('Text')} (*.txt)"
        )
        if f:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(
                    f"{APP_NAME} {VERSION} log, {datetime.now().astimezone():%Y-%m-%d %H:%M}\n\n"
                )
                fh.write(self.text.toPlainText())

# ---- usage, about
USAGE = (
    f"""
<h2>{APP_NAME}</h2>
<p>A hub that lets DJ programs exchange playlists. It reads Traktor NML, rekordbox XML, VirtualDJ, iTunes or Music <code>Library.xml</code>, Engine DJ (read only), Mixxx, M3U/M3U8, a music folder and a CDJ USB key. It writes rekordbox XML, M3U8, Traktor NML, VirtualDJ lists, Mixxx, iTunes XML, CSV, JSON, PLS, XSPF, a CDJ USB key and ZIP backups. It works alongside your DJ program and does not replace it.</p>
<h3>Getting started</h3>
<ol>
<li><b>File &gt; Open project / collection</b> (Ctrl+O), or drop a file, a music folder, the Engine Library folder (close Engine DJ first) or the root of a CDJ key on the window.</li>
<li>If the audio files moved, set the <b>Music folder</b>. Missing tracks are looked up there by file name.</li>
<li>Tick the playlists to export (nothing ticked = everything).</li>
<li>Pick the format and the output folder, then <b>Convert</b> (Ctrl+Return).</li>
</ol>
<h3>Formats</h3>
<ul>
<li><b>CDJ/USB</b>: a complete Pioneer key (Contents, export.pdb, ANLZ, artwork, player settings). Nothing to import.</li>
<li><b>Rekordbox XML</b>: import it in rekordbox (File &gt; Preferences &gt; Bridge &gt; Imported Library, the playlists then show in the Bridge pane; menu names can differ between versions).</li>
<li><b>M3U</b>: one M3U8 per playlist, folders kept. No cues or grids.</li>
<li><b>Traktor NML</b>: a collection Traktor can import.</li>
<li><b>VirtualDJ</b>: one .vdjfolder per playlist, playlists only, no cues. <b>iTunes XML</b>: a Library.xml with tags and playlists. <b>Mixxx</b>: a new mixxxdb.sqlite with playlists and cues, export to an empty folder, never into the Mixxx profile, and keep a backup of your profile when you use it.</li>
<li><b>CSV, JSON, PLS, XSPF</b>: plain lists to read in a spreadsheet, a script or an audio player. Boothbridge does not read them back.</li>
</ul>
<p>Export CDJ/USB to the root of a FAT32 or exFAT key (not NTFS), or to a folder and copy its content to the key. Tracks already on the key are not copied again. A track that is part of the export has its cues and analysis written again, a track only on the key keeps its analysis while its audio is unchanged. Hot cue colours and hot loops need a CDJ-2000NXS2, a CDJ-3000 or a recent XDJ.</p>
<h3>Keys that already have playlists</h3>
<p>When the key already holds playlists, Boothbridge shows their names and asks.</p>
<ul>
<li><b>Keep them and add this export</b> (default): the playlists on the key stay, the new ones are added, a playlist of the same name in the same folder is replaced. Two different songs with the same name both stay, the second is named name (2).</li>
<li><b>Replace everything on the key</b>: the key holds only this export.</li>
</ul>
<p>The old database is saved first as export.pdb.bak. If it cannot be read, Keep is not offered. A playlist none of whose files is found is not written, the window names it. Tick <b>Do not ask again</b> to remember the answer, change it later under <b>Options &gt; Preferences &gt; Export</b>. On the command line, --replace overwrites the key.</p>
<p>A key made by rekordbox may also hold exportExt.pdb and exportLibrary.db. Boothbridge leaves them as they are, and a player that reads them may not show the new playlists: test the key.</p>
<p><b>File &gt; Open a CDJ USB key...</b> (Ctrl+Shift+K) loads the playlists of a key like a collection. It never writes to the key. Pick the root or any folder inside it. Tracks whose analysis files are missing or damaged come without cues, the log has the details.</p>
<h3>Checking files and exports</h3>
<ul>
<li><b>Tools &gt; Verify audio files...</b> (Quick: headers, Full: decode) never changes the originals and reports Not checked for unsupported files, limit overruns and crashes. It is not a checksum or an antivirus scan.</li>
<li><b>Tools &gt; Format a USB drive (FAT32)...</b> (or <b>Format drive...</b> next to Output) erases a removable drive after confirmation and writes FAT32. System disks are never listed.</li>
<li>After an export to a removable drive, the program offers to eject it. <b>Tools &gt; Eject the output drive</b> (Ctrl+E) does it any time.</li>
<li>Every export writes boothbridge_checksums.json. <b>Tools &gt; Verify an export</b> (Ctrl+Shift+V) and <b>Verify the copy</b> (on by default) compare each file with its SHA-256. Generated Pioneer files are validated and read back anyway. None of this replaces a test on your player.</li>
<li>A CDJ can update export.pdb and the ANLZ files. They are then reported as modified since export, integrity unconfirmed, not as damaged. Back up the key before exporting to it again, because the export replaces what the player changed.</li>
</ul>
<h3>Analysis cache</h3>
<p>Analyses and waveforms are kept in %APPDATA%\\BSM\\Boothbridge\\cache (anlz and wave), or in the data folder when there is one. <b>Options &gt; Preferences &gt; CDJ</b> can open the folder, <b>Back up the analyses...</b> in a ZIP with a .zip.sha256 file, <b>Restore analyses...</b> (never replaces a file already there) and <b>Clear analysis cache</b> (offers a backup first). Deleting the cache only costs analysis time.</p>
<h3>Privacy, ClamAV and quitting</h3>
<p>Nothing leaves your computer: no telemetry, no account, no upload, no automatic log sharing. Check a log for paths and track names before sharing it. Collection XML with entities or external references is rejected, and the packaged program checks its native files against a seal at start (not publisher signing).</p>
<p>The audio check shows whether clamscan is installed and never runs it. Install and update ClamAV yourself and scan outside Boothbridge: <code>clamscan --recursive --infected "D:\\Music"</code> (exit 0 clean, 1 detection, 2 error, nothing deleted). A clean report or a clean decode does not prove a file is safe, and there is no detector for hidden data in MP3 files.</p>
<p>Quit is blocked while Boothbridge is writing, analysing, decoding waveforms, scanning or reading a key. Wait or cancel with the job's own control. Do not unplug a drive while it is being written.</p>
<h3>Shortcuts</h3>
<table cellpadding="3">
<tr><td>Ctrl+O</td><td>Open a project or collection</td></tr>
<tr><td>Ctrl+Shift+O</td><td>Open a music folder</td></tr>
<tr><td>Ctrl+Shift+K</td><td>Open a CDJ USB key</td></tr>
<tr><td>Ctrl+N</td><td>New playlist</td></tr>
<tr><td>Ctrl+S, Ctrl+Shift+S</td><td>Save the project, save as</td></tr>
<tr><td>Ctrl+Return</td><td>Convert</td></tr>
<tr><td>Ctrl+R</td><td>Reload the collection (Refresh in the music browser)</td></tr>
<tr><td>Ctrl+K</td><td>Check collection, relocate missing files</td></tr>
<tr><td>Ctrl+Shift+V</td><td>Verify an export</td></tr>
<tr><td>Ctrl+E</td><td>Eject the output drive</td></tr>
<tr><td>Ctrl+M</td><td>Music browser</td></tr>
<tr><td>Ctrl+,</td><td>Preferences</td></tr>
<tr><td>Ctrl+L</td><td>Log</td></tr>
<tr><td>F1</td><td>This page</td></tr>
<tr><td>Ctrl+Q</td><td>Quit</td></tr>
<tr><td>Ctrl+Z, Ctrl+Y</td><td>Undo, redo (playlist, cue, grid, ID tag; in the Cue Editor, cue changes)</td></tr>
<tr><td>Del</td><td>main window: remove the selected playlist from this session. Playlist window: remove the selected tracks</td></tr>
</table>
<h3>Keyboard in the playlist window</h3>
<p>Everything works without the mouse. Tab goes from the player to the filter, the list and the buttons. Up/Down choose a track, Enter plays it, Space or P pauses. Ctrl+F goes to the filter (Esc empties it and returns to the list, Enter returns without emptying).</p>
<table cellpadding="3">
<tr><td>Alt+Up, Alt+Down</td><td>Move the selected tracks</td></tr>
<tr><td>Ctrl+Left, Ctrl+Right</td><td>Back or forward 5 seconds</td></tr>
<tr><td>Ctrl+I</td><td>ID Tag of the current track</td></tr>
<tr><td>Ctrl+T</td><td>Cue Editor of the current track</td></tr>
<tr><td>Ctrl+Shift+D</td><td>Remove duplicates</td></tr>
<tr><td>A to H, Shift+A to H</td><td>Set or play a hot cue, delete it</td></tr>
<tr><td>S</td><td>Snap on or off (not while typing)</td></tr>
<tr><td>Ctrl+D</td><td>Cue Editor: detect tempo and first beat</td></tr>
<tr><td>Alt+Left, Alt+Right</td><td>Cue Editor: move the first beat by 1 ms (Shift: 10 ms)</td></tr>
<tr><td>Ctrl+B</td><td>Cue Editor: snap, same as S</td></tr>
</table>
<h3>Playlists</h3>
<p>Double click a playlist to open it. Shift+click selects a range, Ctrl+click single tracks. Drag rows to reorder, drop files to add. Double click a row to play it, or its Cues cell to open the Cue Editor. Right click a playlist in the main window to Rename... or Remove from this session... (Del does too): only the program's session changes, never the collection file or the audio. The Also in column counts the other loaded playlists holding the same file. Reload restores the playlist as it was when the window opened and empties its history.</p>
<p>Right click on a track: <b>Show in Explorer</b>, <b>Detect BPM</b> (reliable results replace BPM and first beat, a single doubtful one asks, default No, Ctrl+Z undoes), <b>Show Cue Editor</b>, <b>Insert into</b> (adds the tracks to the end of another loaded playlist, no duplicate, same track object on both sides, one Undo step on the target).</p>
<p><b>Detect grids</b> analyses the selected tracks, or the tracks without a grid, after a confirmation. Only reliable results are applied (a clear pulse and the same tempo in every part). Cancel detection applies nothing, one Ctrl+Z undoes a whole batch. It assumes a constant tempo: check the result on the waveform and with the metronome.</p>
"""
    + """<h3>Music browser</h3>
<p>Set the folder in <b>Options &gt; Preferences &gt; Library</b>, then <b>View &gt; Music browser</b> (Ctrl+M). Separate window, position remembered. Click or Enter or Space to preview (one preview at a time), Ctrl+F filters. Drag selected tracks into an open playlist (audio does not move, one Undo step). <b>Refresh</b> (Ctrl+R) rescans, <b>Scan duplicates</b> finds exact copies by size and SHA-256, <b>Duplicates only</b> filters them, <b>Cancel scan</b> stops. Nothing is deleted. This folder is not the Music folder used to find missing tracks.</p>
<h3>Cues, grid and waveform</h3>
<p>Click or drag on the wave to move the needle without playing. Ctrl+wheel zooms, the strip under the wave scrubs, a white line marks every minute. A to H set a hot cue at the needle (again: play from it), Shift+A to H deletes it. Pads: click jumps or sets, Ctrl+click deletes, right click gives a 1, 2, 4 or 8 beat loop. In the Cue Editor Del deletes the cue, a double click on the wave adds one, <b>Add 4-beat loop</b> adds a loop. Drag the letter or triangle of a cue to move it, Shift snaps to the grid, Esc puts it back.</p>
<p>Type a Tempo and a First beat and click Apply, or use Set at cursor, Clear and Nudge. Changing the grid does not move cues. The metronome has its own volume and an offset from -250 to +250 ms (negative advances the click). Projects, NML and rekordbox XML keep the edited timing, M3U8 carries neither grids nor cues.</p>
<h3>ID Tag</h3>
<p><b>Apply to project</b> leaves the audio alone. <b>Write tags to audio...</b> asks for confirmation, keeps the first .bb-tags.bak backup, keeps the audio and unrelated data, clears empty fields and writes only fields that differ. Stop the file's preview first. Rating stays in the project. Reload and Undo cannot reverse a write to a file.</p>
<h3>Projects and ZIP backup</h3>
<p><b>File &gt; Save the project</b> keeps playlists, folders, order, paths, cues, grids, energy and settings in one JSON file. Closing with unsaved changes asks first.</p>
<p><b>Tools &gt; Backup</b>: <b>Save backup...</b> (all loaded playlists, their audio once per file, the project and an M3U8 per playlist, missing audio prevents it), <b>Verify backup...</b>, <b>Load / restore backup...</b> (into a new folder, never overwrites). Keep the ZIP and its .zip.sha256 together. Restore refuses more than 100,000 entries, more than 1 TiB expanded, a central directory above 128 MiB, a ratio above 100:1, encrypted entries, unsupported compression, unsafe paths and Windows device names, and keeps 1 GiB free. A matching SHA-256 shows the archive is intact, not who made it.</p>
<h3>Menus</h3>
<p><b>File</b>: open, new playlist, music folder, Engine DJ library, CDJ USB key, save, Convert, Exit. <b>Tools</b>: Reload collection, Check collection, Verify audio files, Verify an export, Backup, Format a USB drive, Eject. <b>Options</b>: Preferences. <b>View</b>: Music browser. <b>Help</b>: View log, Usage, About.</p>
<h3>Command line</h3>
<pre>Boothbridge.exe --export COLLECTION OUTPUT [--format FORMAT] [--music FOLDER] [--keep | --replace]
Boothbridge.exe --verify OUTPUT
"""
    + """</pre>
<p>FORMAT: Rekordbox XML (default), M3U, Traktor NML, VirtualDJ, Mixxx, iTunes XML, CSV, JSON, PLS, XSPF, CDJ/USB. Report in boothbridge.log. Exit codes: export 0 when nothing failed, 1 failure, 2 wrong command line. Verify 0 for an exact match, 1 otherwise. The exe has no console: use <code>start /wait</code>.</p>
<h3>When a CDJ refuses the key</h3>
<ul>
<li>"NO DISK" or nothing: the key is NTFS, format it FAT32 or exFAT.</li>
<li>Check the log (Ctrl+L): every skipped track is listed.</li>
<li>Playlists of an earlier export are missing: that export replaced the database. Export them again with Keep them and add this export. The old database is next to the new one as export.pdb.bak.</li>
<li>A new playlist does not show on a recent player and the key was made by rekordbox: exportExt.pdb and exportLibrary.db are not updated.</li>
</ul>
"""
)

def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")

# ----------------------------------------------------------------
# the help with a list of its sections before the first one, and an anchor on each of them
def with_contents(page: str) -> str:
    titles = re.findall(r"<h3>(.*?)</h3>", page)
    links = " &nbsp;|&nbsp; ".join(f'<a href="#{slug(t)}">{t}</a>' for t in titles)
    page = page.replace("<h3>", f"<p><b>In this page</b><br>{links}</p>\n<h3>", 1)
    for t in titles:
        page = page.replace(f"<h3>{t}</h3>", f'<h3><a name="{slug(t)}"></a>{t}</h3>', 1)
    return page

# ----------------------------------------------------------------
class Usage(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Usage"))
        self.resize(640, 560)
        self.text = QTextBrowser()
        self.text.document().setDefaultStyleSheet(
            f"a {{ color: {theme.ACCENT}; text-decoration: none; }}"
        )
        self.text.setHtml(with_contents(USAGE))
        self.find = QLineEdit()
        self.find.setPlaceholderText(
            tr("Find in this page (Enter: next, Esc: clear)")
        )
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda *_: self.search(restart=True))
        self.find.returnPressed.connect(self.search)
        QShortcut(QKeySequence.Find, self, self.find.setFocus)
        QShortcut(QKeySequence("Esc"), self.find, self.find.clear).setContext(
            Qt.WidgetShortcut
        )
        b = QPushButton(tr("Close"))
        b.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(self.find)
        lay.addWidget(self.text)
        lay.addWidget(b, 0, Qt.AlignRight)

    # ----------------------------------------------------------------
    # the next match of the search box, from the top when the text changed, round the page
    # when the end is reached
    def search(self, restart: bool = False):
        needle = self.find.text()
        if not needle:
            cursor = self.text.textCursor()
            cursor.clearSelection()
            self.text.setTextCursor(cursor)
            return
        if restart:
            self.text.moveCursor(QTextCursor.Start)
        if not self.text.find(needle):
            self.text.moveCursor(QTextCursor.Start)
            self.text.find(needle)

# ----------------------------------------------------------------
class About(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("About {app}", app=APP_NAME))
        a = f"style='color:{theme.ACCENT}; text-decoration:none'"
        lbl = QLabel(
            f"<h2>{APP_NAME}</h2><p>{tr('Version {version}', version=VERSION)}</p>"
            f"<p>{tr('A hub that lets DJ programs exchange playlists. It works alongside them.')}</p>"
            f"<p>{AUTHOR}<br><a {a} href='https://{WEBSITE}'>{WEBSITE}</a>"
            + (f"<br><a {a} href='https://{SITE}'>{SITE}</a>" if SITE else "")
            + (
                f"<br><a {a} href='{SUPPORT_URL}'>{tr('Support the project')}</a>"
                if SUPPORT_URL
                else ""
            )
            + "</p>"
            f"<p>{tr('Free, open source (PolyForm Noncommercial 1.0.0).')}</p>"
        )
        lbl.setTextFormat(Qt.RichText)
        lbl.setOpenExternalLinks(True)
        lbl.setAlignment(Qt.AlignCenter)
        legal = QLabel(
            f"<span style='color:{theme.FG_MUTED}'>"
            + tr(
                "Provided as is, without warranty: no liability for data loss or other damage, back up your files."
            )
            + " "
            + tr(
                "Not affiliated with AlphaTheta / Pioneer DJ, Native Instruments, VirtualDJ, inMusic / Denon DJ, Apple or Mixxx."
            )
            + " "
            + tr(
                "The CDJ USB key is written by the program itself, rekordbox is not involved."
            )
            + "</span>"
        )
        legal.setWordWrap(True)
        legal.setAlignment(Qt.AlignCenter)
        b = QPushButton(tr("Close"))
        b.clicked.connect(self.accept)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.addWidget(lbl)
        lay.addSpacing(8)
        lay.addWidget(legal)
        lay.addWidget(b, 0, Qt.AlignCenter)
