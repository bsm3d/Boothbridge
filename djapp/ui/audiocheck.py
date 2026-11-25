# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Qt dialog that runs the audio verification over the collection and shows a status for each file.

# audio verification panel, independent of collection relocation

from collections import Counter

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .. import audiocheck
from ..i18n import tr
from ..model import unique_tracks
from . import theme
from .worker import Job


# the status words come from the check in English: shown translated, compared as they are
def status_text(status: str) -> str:
    words = {
        "Not checked": tr("Not checked"),
        "Missing": tr("Missing"),
        "Suspect": tr("Suspect"),
        "Unreadable": tr("Unreadable"),
        "Headers readable": tr("Headers readable"),
        "Decoded": tr("Decoded"),
    }
    return words.get(status, status)


# ------------------------------------------------------------
class AudioCheck(QDialog):
    def __init__(self, nodes, root="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Verify audio files"))
        self.resize(980, 580)
        self.paths = [track.path for track in unique_tracks(nodes)]
        self.job = None
        self.source = QComboBox()
        self.source.addItems(
            [tr("Loaded collection"), tr("Folder (including subfolders)")]
        )
        self.folder = QLineEdit(root)
        self.folder.setReadOnly(True)
        self.browse = QPushButton(tr("Browse..."))
        self.browse.clicked.connect(self.pick_folder)
        self.mode = QComboBox()
        self.mode.addItems(
            [
                tr("Quick - headers and metadata"),
                tr("Full - decode entire audio stream"),
            ]
        )
        self.mode.setCurrentIndex(1)
        self.mode.setAccessibleName(tr("Verification mode"))
        self.source.setAccessibleName(tr("Files to verify"))
        self.source.currentIndexChanged.connect(self.source_changed)
        self.source_changed()
        source_row = QHBoxLayout()
        source_row.addWidget(self.source)
        source_row.addWidget(self.folder, 1)
        source_row.addWidget(self.browse)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(tr("Verification")))
        mode_row.addWidget(self.mode, 1)
        self.time_limit = QSpinBox()
        self.time_limit.setRange(5, 3600)
        self.time_limit.setValue(120)
        self.time_limit.setSuffix(" s")
        self.time_limit.setAccessibleName(tr("Maximum analysis time per file"))
        self.memory_limit = QSpinBox()
        self.memory_limit.setRange(256, 8192)
        self.memory_limit.setValue(1024)
        self.memory_limit.setSuffix(" MB")
        self.memory_limit.setAccessibleName(tr("Maximum analysis process memory"))
        limits = QHBoxLayout()
        limits.addWidget(QLabel(tr("Per-file limits")))
        limits.addWidget(self.time_limit)
        limits.addWidget(self.memory_limit)
        limits.addStretch(1)
        clamav = audiocheck.find_clamav()
        antivirus = QLabel(
            tr(
                "ClamAV detected: {path} - antivirus scan not performed.",
                path=clamav,
            )
            if clamav
            else tr("ClamAV not detected - antivirus scan not performed.")
        )
        antivirus.setWordWrap(True)
        note = QLabel(
            tr(
                "Read-only: original files are never changed. Quick checks do not validate audio data. "
                "Full checks are slower; some formats (notably M4A/ALAC) may not be supported by the decoder. "
                "A readable stream is not a guarantee of integrity: decoders can tolerate some damage."
            )
            + " "
            + tr(
                "Analysis runs in a separate process with monitored time/memory limits. "
                "These limits are not a security sandbox or a malware scan."
            )
        )
        note.setWordWrap(True)
        self.table = QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setHeaderLabels([tr("Status"), tr("File"), tr("Details")])
        self.table.setColumnWidth(0, 145)
        self.table.setColumnWidth(1, 380)
        self.progress = QProgressBar()
        self.status = QLabel(tr("Choose the source and verification mode, then start."))
        self.status.setWordWrap(True)
        self.start_button = QPushButton(tr("Start verification"))
        self.start_button.clicked.connect(self.start)
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addLayout(source_row)
        layout.addLayout(mode_row)
        layout.addLayout(limits)
        layout.addWidget(antivirus)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

    def source_changed(self):
        folder = self.source.currentIndex() == 1
        self.folder.setEnabled(folder)
        self.browse.setEnabled(folder)

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, tr("Audio folder"), self.folder.text()
        )
        if folder:
            self.folder.setText(folder)

    def start(self):
        folder = self.folder.text() if self.source.currentIndex() == 1 else ""
        if (self.source.currentIndex() == 1 and not folder) or (
            not folder and not self.paths
        ):
            QMessageBox.information(
                self,
                tr("Verify audio files"),
                tr("Load a collection or choose an audio folder first."),
            )
            return
        self.table.clear()
        self.progress.setValue(0)
        self.status.setText(tr("Verifying audio files..."))
        for widget in (
            self.source,
            self.folder,
            self.browse,
            self.mode,
            self.start_button,
            self.time_limit,
            self.memory_limit,
        ):
            widget.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.job = Job(
            audiocheck.scan,
            self.paths,
            folder,
            self.mode.currentIndex() == 1,
            timeout=self.time_limit.value(),
            memory_mb=self.memory_limit.value(),
        )
        self.job.progress.connect(self.update_progress)
        self.job.done.connect(self.completed)
        self.job.failed.connect(self.failed)
        self.job.cancelled.connect(
            lambda *_: self.status.setText(
                tr("Verification cancelled; no files changed.")
            )
        )
        self.job.finished.connect(self.job_finished)
        self.job.start()

    def update_progress(self, value, text):
        self.progress.setValue(value)
        self.status.setText(text)

    # ------------------------------------------------------------
    def completed(self, results):
        for result in results:
            item = QTreeWidgetItem(
                [status_text(result.status), result.path, result.detail]
            )
            item.setToolTip(1, result.path)
            item.setToolTip(2, result.detail)
            color = (
                theme.RED
                if result.status in ("Suspect", "Missing", "Unreadable")
                else theme.YELLOW
                if result.status == "Not checked"
                else theme.GREEN
            )
            item.setForeground(0, QColor(color))
            self.table.addTopLevelItem(item)
        counts = Counter(result.status for result in results)
        self.progress.setValue(100)
        self.status.setText(
            tr(
                "{n} files: {counts}",
                n=len(results),
                counts=", ".join(f"{n} {status_text(s)}" for s, n in counts.items()),
            )
            if results
            else tr("No supported audio files found.")
        )

    def failed(self, message):
        self.status.setText(tr("Verification failed; no files changed."))
        QMessageBox.critical(self, tr("Audio verification failed"), message)

    def cancel(self):
        if self.job is not None:
            self.job.stop.set()
            self.cancel_button.setEnabled(False)
            self.status.setText(tr("Cancelling after the current decoder read..."))

    def job_finished(self):
        self.job.deleteLater()
        self.job = None
        for widget in (
            self.source,
            self.mode,
            self.start_button,
            self.time_limit,
            self.memory_limit,
        ):
            widget.setEnabled(True)
        self.source_changed()
        self.cancel_button.setEnabled(False)

    def reject(self):
        if self.job is not None:
            self.cancel()
            return
        super().reject()

    def closeEvent(self, event):
        if self.job is not None:
            self.cancel()
            event.ignore()
        else:
            super().closeEvent(event)
