# project metadata first; writing the source audio is a separate, confirmed operation

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import keys, tags, tagwrite
from ..i18n import tr
from ..model import Track
from . import cuehistory, theme
from .worker import Job
from .zoomwave import clock

EDIT_FIELDS = (*tagwrite.FILE_FIELDS, "rating")


# a function and not a constant: the texts are read once the language is chosen
def text_labels():
    return (
        ("title", tr("Title")),
        ("artist", tr("Artist")),
        ("album", tr("Album")),
        ("album_artist", tr("Album artist")),
        ("genre", tr("Genre")),
        ("label", tr("Label")),
        ("remixer", tr("Remixer")),
        ("composer", tr("Composer")),
    )


# --------------------------------------------------------------
class TagEditor(QDialog):
    changed = Signal()
    file_written = Signal(str)

    # --------------------------------------------------------
    def __init__(self, track: Track, path: str, key_fmt: str, parent=None, engine=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.t, self.path, self.engine = track, path, engine
        self.baseline = {field: getattr(track, field) for field in EDIT_FIELDS}
        self.edited = set()
        self.job: Job | None = None
        self.pending = None
        self.setWindowTitle(
            tr("ID Tag: {artist} - {title}", artist=track.artist, title=track.title)
        )
        self.resize(800, 640)
        self.fields = {}
        form = QFormLayout()
        for name, label in text_labels():
            edit = QLineEdit(getattr(track, name))
            edit.setAccessibleName(label)
            self.fields[name] = edit
            form.addRow(label, edit)
        self.comment = QPlainTextEdit(track.comment)
        self.comment.setAccessibleName(tr("Comment"))
        self.comment.setMaximumHeight(72)
        form.addRow(tr("Comment"), self.comment)
        numbers = QGridLayout()
        self.numbers = {}
        for index, (name, label, maximum) in enumerate(
            (
                ("year", tr("Year"), 9999),
                ("track_no", tr("Track"), 65535),
                ("disc_no", tr("Disc"), 65535),
                ("rating", tr("Rating (project)"), 5),
            )
        ):
            box = QSpinBox()
            box.setRange(0, maximum)
            box.setSpecialValueText(tr("Not set"))
            box.setValue(getattr(track, name))
            box.setAccessibleName(label)
            self.numbers[name] = box
            numbers.addWidget(QLabel(label), 0, index * 2)
            numbers.addWidget(box, 0, index * 2 + 1)
            numbers.setColumnStretch(index * 2 + 1, 1)
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(0, max(1000, track.bpm))
        self.bpm.setDecimals(6)
        self.bpm.setSingleStep(0.01)
        self.bpm.setSpecialValueText(tr("Unknown"))
        self.bpm.setValue(track.bpm)
        self.bpm.setAccessibleName("BPM")
        self.key = QComboBox()
        self.key.setAccessibleName(tr("Musical key"))
        self.key.addItem(tr("Unknown"), None)
        for index in range(24):
            self.key.addItem(f"{keys.name(index, key_fmt)} / {keys.name(index)}", index)
        self.key.setCurrentIndex(self.key.findData(track.key))
        numbers.addWidget(QLabel("BPM"), 1, 0)
        numbers.addWidget(self.bpm, 1, 1, 1, 3)
        numbers.addWidget(QLabel(tr("Key")), 1, 4)
        numbers.addWidget(self.key, 1, 5, 1, 3)
        form.addRow(numbers)
        note = QLabel(
            tr(
                "BPM changes the spacing of the beat grid, not existing cue/loop positions. "
                "Edit the first beat in the Cue Editor."
            )
        )
        note.setWordWrap(True)
        form.addRow(note)
        metadata = QWidget()
        metadata.setLayout(form)
        self.tabs = QTabWidget()
        self.tabs.addTab(metadata, tr("Metadata"))
        technical = QWidget()
        info = QFormLayout(technical)
        for label, value in (
            (tr("File"), path or track.path or tr("Missing file")),
            (tr("Duration"), clock(track.duration * 1000)),
            (tr("Bitrate"), f"{track.bitrate} kbps"),
            (tr("Sample rate"), f"{track.samplerate} Hz"),
            (tr("Gain"), f"{track.gain:+.2f} dB"),
            (tr("Plays"), str(track.plays)),
            (tr("Added"), track.added),
            (tr("Cues / loops"), str(len(track.cues))),
            (
                tr("First beat"),
                f"{track.grid:.3f} ms" if track.grid is not None else tr("Not set"),
            ),
        ):
            edit = QLineEdit(value)
            edit.setReadOnly(True)
            edit.setAccessibleName(label)
            info.addRow(label, edit)
        self.tabs.addTab(technical, tr("File information"))
        self.art = QLabel(tr("No artwork"))
        self.art.setFixedSize(180, 180)
        self.art.setAlignment(Qt.AlignCenter)
        self.art.setStyleSheet(f"background: {theme.BG_MED};")
        image = tags.artwork(path) if path else None
        pixmap = QPixmap()
        if image and pixmap.loadFromData(image):
            self.art.setPixmap(
                pixmap.scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        side = QVBoxLayout()
        side.addSpacing(self.tabs.tabBar().sizeHint().height())
        side.addWidget(self.art)
        art_note = QLabel(
            tr("Artwork is preserved.\nCues and beatgrid are not written by this editor.")
        )
        art_note.setWordWrap(True)
        art_note.setMaximumWidth(180)
        side.addWidget(art_note)
        side.addStretch(1)
        body = QHBoxLayout()
        body.addWidget(self.tabs, 1)
        body.addLayout(side)
        self.status = QLabel(
            tr(
                "Apply updates the project and exports only; the source audio stays unchanged."
            )
        )
        self.status.setWordWrap(True)
        buttons = QHBoxLayout()
        self.write_button = QPushButton(tr("Write tags to audio..."))
        self.write_button.setToolTip(
            tr(
                "Write displayed metadata to the source file, with a backup and confirmation"
            )
        )
        self.write_button.clicked.connect(self.write_file)
        self.apply_button = QPushButton(tr("Apply to project"))
        self.apply_button.clicked.connect(self.apply)
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(self.reject)
        for button in (self.write_button, self.apply_button, self.close_button):
            button.setAutoDefault(False)
        buttons.addWidget(self.write_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.remember_shown()
        self.refresh_write_button()

    # a value the widgets cannot hold (year 20240517, a rating of 9, more than 6 decimals) is
    # shown changed: it only becomes an edit once the user moves it
    def remember_shown(self):
        self.shown_bpm = self.bpm.value()
        self.shown = {name: box.value() for name, box in self.numbers.items()}
        self.shown["key"] = self.key.currentIndex()

    def values(self):
        values = {name: widget.text() for name, widget in self.fields.items()}
        values["comment"] = self.comment.toPlainText()
        for name, box in self.numbers.items():
            value = box.value()
            values[name] = self.baseline[name] if value == self.shown[name] else value
        bpm = self.bpm.value()
        values.update(
            bpm=self.baseline["bpm"] if bpm == self.shown_bpm else bpm,
            key=self.baseline["key"]
            if self.key.currentIndex() == self.shown["key"]
            else self.key.currentData(),
        )
        return values

    def check_conflicts(self, values):
        for name, value in values.items():
            current = getattr(self.t, name)
            if (
                value != self.baseline[name]
                and current != self.baseline[name]
                and current != value
            ):
                raise ValueError(
                    tr(
                        "{name} changed in another editor. Close and reopen ID Tag before applying.",
                        name=name,
                    )
                )

    # ------------------------------------------------------
    def update_track(self, values):
        self.check_conflicts(values)
        changed = {
            name: value
            for name, value in values.items()
            if value != self.baseline[name] and value != getattr(self.t, name)
        }
        self.edited.update(changed)
        before = cuehistory.snap(self.t)
        for name, value in changed.items():
            setattr(self.t, name, value)
        if "bpm" in changed:
            cuehistory.record(self.t, before)
        if changed:
            self.setWindowTitle(
                tr(
                    "ID Tag: {artist} - {title}",
                    artist=self.t.artist,
                    title=self.t.title,
                )
            )
            self.changed.emit()
        self.baseline = {name: getattr(self.t, name) for name in EDIT_FIELDS}
        self.reset_fields()

    def reset_fields(self):
        for name, widget in self.fields.items():
            widget.setText(getattr(self.t, name))
        self.comment.setPlainText(self.t.comment)
        for name, widget in self.numbers.items():
            widget.setValue(getattr(self.t, name))
        self.bpm.setValue(self.t.bpm)
        self.key.setCurrentIndex(self.key.findData(self.t.key))
        self.remember_shown()

    def apply(self):
        if self.job is not None:
            return
        try:
            self.update_track(self.values())
        except ValueError as e:
            QMessageBox.warning(self, tr("ID Tag"), str(e))
            return
        self.status.setText(
            tr("Applied to project and exports. The source audio was not modified.")
        )

    def refresh_write_button(self):
        supported = os.path.splitext(self.path)[1].lower() in tagwrite.EXTENSIONS
        self.write_button.setEnabled(
            bool(self.path and os.path.isfile(self.path) and supported)
        )
        if not self.write_button.isEnabled():
            self.write_button.setToolTip(
                tr(
                    "Writing needs an existing MP3, WAV, AIFF, FLAC, MP4/M4A or Ogg Vorbis file"
                )
            )

    # --------------------------------------------------------------
    def write_file(self):
        if self.job is not None:
            return
        if self.engine is not None and os.path.normcase(os.path.abspath(self.path)) in {
            os.path.normcase(os.path.abspath(path))
            for path in (
                self.engine.path,
                self.engine.next_path,
                *(player.source().toLocalFile() for player, _out in self.engine.decks),
            )
            if path
        }:
            QMessageBox.warning(
                self,
                tr("Write audio tags"),
                tr("Stop the preview and release this audio file before writing tags."),
            )
            return
        values = self.values()
        try:
            self.check_conflicts(values)
        except ValueError as e:
            QMessageBox.warning(self, tr("ID Tag"), str(e))
            return
        answer = QMessageBox.question(
            self,
            tr("Write tags to source audio"),
            tr(
                "Write the displayed metadata to this original audio file?\n\n{path}\n\n"
                "Empty fields clear their corresponding tags. Rating is project-only. "
                "Audio, artwork, unrelated tags and DJ cue/grid data are retained.\n\n"
                "A backup is kept at:\n{path}.bb-tags.bak\n"
                "An existing backup is never overwritten. Reload/Undo cannot undo this file write.",
                path=self.path,
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        edited = self.edited | {
            name for name, value in values.items() if value != self.baseline[name]
        }
        values = {
            name: value if value != self.baseline[name] else getattr(self.t, name)
            for name, value in values.items()
        }
        raw = {name: values[name] for name in tagwrite.FILE_FIELDS}
        raw.update(
            {
                name: str(values[name]) if values[name] else ""
                for name in tagwrite.NUMBER_FIELDS
            }
        )
        raw["bpm"] = f"{values['bpm']:.6f}" if values["bpm"] else ""
        raw["key"] = keys.name(values["key"])
        # the track holds simplified values (a "(17)" genre, a 320 BPM or an unreadable key
        # come back empty), so a field the user did not touch would clear its tag
        raw = {name: value for name, value in raw.items() if name in edited}
        self.pending = values
        self.job = Job(tagwrite.write, self.path, raw)
        self.job.progress.connect(lambda _pct=None, message=None, *_: self.status.setText(message))
        self.job.done.connect(self.written)
        self.job.failed.connect(self.write_failed)
        self.job.cancelled.connect(
            lambda *_: self.status.setText(
                tr("Writing cancelled; the source file was not modified.")
            )
        )
        self.job.finished.connect(self.write_finished)
        self.tabs.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.write_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status.setText(tr("Writing tags on a copy; please wait..."))
        self.job.start()

    # --------------------------------------------------------
    def written(self, result: tagwrite.WriteResult):
        self.t.size = result.size
        try:
            for name in tagwrite.FILE_FIELDS:
                current = getattr(self.t, name)
                if current != self.baseline[name] and current != self.pending[name]:
                    raise ValueError(
                        tr("{name} changed in another editor while writing.", name=name)
                    )
            self.update_track(self.pending)
        except ValueError as e:
            QMessageBox.warning(
                self,
                tr("Audio tags written"),
                tr("The file was written, but the project changed:\n{error}", error=e),
            )
            self.status.setText(
                tr(
                    "Audio tags written, but project metadata was not applied: {error}",
                    error=e,
                )
            )
        else:
            self.status.setText(
                tr("Audio tags written. Backup: {backup}", backup=result.backup)
            )
        self.file_written.emit(result.path)

    def write_failed(self, message):
        self.status.setText(tr("Tags were not written: {message}", message=message))
        QMessageBox.critical(self, tr("Write audio tags"), message)

    def write_finished(self):
        job, self.job = self.job, None
        self.pending = None
        if job is not None:
            job.deleteLater()
        self.tabs.setEnabled(True)
        self.apply_button.setEnabled(True)
        self.close_button.setEnabled(True)
        self.refresh_write_button()

    def reject(self):
        if self.job is not None:
            return
        if self.values() != self.baseline:
            answer = QMessageBox.question(
                self,
                tr("Unapplied metadata"),
                tr("Discard unapplied metadata changes?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        super().reject()

    def closeEvent(self, event):
        if self.job is not None:
            event.ignore()
            return
        super().closeEvent(event)
