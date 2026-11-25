# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Qt dialog that formats a USB key as FAT32, with a confirmation for large drives and a progress bar.

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .. import drives, fat32
from ..i18n import tr
from . import theme
from .worker import Job

log = logging.getLogger(__name__)

LARGE = 256 * 10**9  # above this the confirmation says that it is a big drive

# --------------------------------------------------
# wipes a whole removable drive (Mac, Linux, NTFS, exFAT... whatever is on it) and
# leaves one FAT32 partition in an MBR table, the layout every DJ player reads
class FormatDialog(QDialog):
    formatted = Signal(str)

    # ------------------------------------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Format a USB drive"))
        self.setMinimumWidth(560)
        self.drives: list[drives.Drive] = []
        # a job is running, set and cleared here, not read from the thread
        self.working = False
        self.formatting = False  # the running job writes to the drive: no closing
        self.relist = False
        self.result_path = ""

        v = QVBoxLayout(self)
        v.addWidget(QLabel(tr("Drive")))
        row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self.show_drive)
        self.refresh_btn = QPushButton(tr("Refresh"))
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_btn)
        v.addLayout(row)

        self.details = QLabel("")
        self.details.setTextFormat(Qt.PlainText)  # the names on a drive are not markup
        self.details.setWordWrap(True)
        self.details.setStyleSheet(f"color: {theme.FG_MUTED};")
        v.addWidget(self.details)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Name")))
        self.label = QLineEdit("TRAKTOR")
        self.label.setMaxLength(11)
        self.label.setPlaceholderText(tr("11 characters max"))
        row.addWidget(self.label, 1)
        v.addLayout(row)

        info = QLabel(
            tr(
                "FAT32 with an MBR partition table: every DJ player reads it. The whole drive is "
                "rewritten, whatever it was before (Mac, Linux, NTFS, exFAT, GPT...), with no 32 GB limit."
            )
        )
        info.setWordWrap(True)
        v.addWidget(info)

        warn = QLabel(
            tr("Everything on the selected drive will be erased. This cannot be undone.")
        )
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.RED}; font-weight: bold;")
        v.addWidget(warn)
        self.sure = QCheckBox(
            tr("I understand: every file and partition on this drive will be lost")
        )
        self.sure.toggled.connect(self.update_buttons)
        v.addWidget(self.sure)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.go = QPushButton(tr("Format drive"))
        self.go.setEnabled(False)
        self.go.clicked.connect(self.start)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.go)
        row.addWidget(self.close_btn)
        v.addLayout(row)

        self.refresh()

    @property
    def busy(self) -> bool:
        return self.working

    def run(self, job: Job, formatting: bool = False):
        self.working = True
        self.formatting = formatting
        job.finished.connect(self.job_over)
        job.start()
        self.update_buttons()

    def job_over(self):
        self.working = False
        self.formatting = False
        if self.relist:
            self.relist = False
            self.refresh()
        else:
            self.update_buttons()

    def current(self) -> drives.Drive | None:
        i = self.combo.currentIndex()
        return self.drives[i] if 0 <= i < len(self.drives) else None

    def update_buttons(self):
        idle = not self.busy
        self.go.setEnabled(
            idle and self.sure.isChecked() and self.current() is not None
        )
        self.combo.setEnabled(idle)
        self.refresh_btn.setEnabled(idle)
        self.label.setEnabled(idle)
        self.sure.setEnabled(idle)
        self.close_btn.setEnabled(not self.formatting)

    # ---- the list

    def refresh(self):
        if self.busy:
            return
        self.status.setText(tr("Looking for removable drives..."))
        self.combo.clear()
        self.drives = []
        self.go.setEnabled(False)
        job = Job(drives.list_drives)
        job.done.connect(self.listed)
        job.failed.connect(self.list_failed)
        self.run(job)

    def list_failed(self, msg: str):
        self.status.setText(tr("Could not list the drives: {msg}", msg=msg))

    def listed(self, found):
        self.drives = list(found)
        self.combo.blockSignals(True)
        self.combo.clear()
        for d in self.drives:
            self.combo.addItem(d.title)
        self.combo.blockSignals(False)
        self.status.setText(
            ""
            if self.drives
            else tr("No removable drive found. Plug the key in and press Refresh.")
        )
        self.show_drive()

    def show_drive(self):
        d = self.current()
        if d is None:
            self.details.setText("")
        else:
            on = (
                "; ".join(d.volumes) if d.volumes else tr("no partition it can read")
            )
            self.details.setText(
                tr(
                    "Partition table: {style}.  On it: {on}.",
                    style=d.style or tr("none"),
                    on=on,
                )
            )
        self.update_buttons()

    # ---- the work

    # ----------------------------------------------------------------
    def start(self):
        d = self.current()
        if d is None or self.busy:
            return
        label = fat32.clean_label(self.label.text()) or "TRAKTOR"
        r = QMessageBox.warning(
            self,
            tr("Erase the drive"),
            self.question(d, label),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if r != QMessageBox.Yes:
            return
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.status.setText(tr("Starting... do not unplug the drive"))
        job = Job(drives.format_drive, d, label)
        job.progress.connect(self.on_progress)
        job.done.connect(self.finished_ok)
        job.failed.connect(self.finished_bad)
        self.run(job, formatting=True)

    # what the last confirmation says: the drive, what is on it and how big it is. The names
    # come from the drive, so nothing that looks like markup is shown as such
    @staticmethod
    def question(d: drives.Drive, label: str) -> str:
        def shown(s: str) -> str:
            return "".join("?" if c in "<>&" or not c.isprintable() else c for c in s)

        on = (
            "; ".join(shown(v) for v in d.volumes)
            if d.volumes
            else tr("no partition it can read")
        )
        text = tr(
            'Erase {title} and format it as FAT32 "{label}"?\n\n'
            "On it: {on}.\n\nAll its data will be lost.",
            title=shown(d.title),
            label=label,
            on=on,
        )
        if d.size > LARGE:
            text += "\n\n" + tr(
                "This drive holds {size}. A USB key for the players is usually "
                "much smaller: make sure it is the one you mean.",
                size=drives.gigabytes(d.size),
            )
        return text

    def on_progress(self, pct: int, msg: str):
        self.bar.setValue(max(0, min(100, pct)))
        self.status.setText(
            tr("{msg}... do not unplug the drive", msg=msg) if msg else ""
        )

    def finished_ok(self, path):
        self.relist = True  # set first: the message boxes below run an event loop
        self.result_path = path or ""
        self.bar.setValue(100)
        self.sure.setChecked(False)
        where = " " + tr("It is available as {path}.", path=path) if path else ""
        self.status.setText(tr("Done.") + where)
        QMessageBox.information(
            self,
            tr("Format done"),
            tr("The drive is now FAT32, ready for your DJ players.") + where,
        )
        if path:
            self.formatted.emit(path)

    def finished_bad(self, msg: str):
        self.relist = True
        self.bar.setVisible(False)
        log.error("format failed: %s", msg)
        self.status.setText(tr("Failed: {msg}", msg=msg))
        QMessageBox.critical(self, tr("Format failed"), msg)

    def reject(self):
        if not self.formatting:
            super().reject()

    def closeEvent(self, e):
        if self.formatting:
            e.ignore()
        else:
            super().closeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.formatting:
            return
        super().keyPressEvent(e)
