# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Qt cue timeline dialog for one track, where cues can be edited, moved and deleted, and the beat grid detected or set.

from __future__ import annotations

import math

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .. import beatdetect, keys
from ..i18n import tr
from ..model import CUE, FADE_IN, FADE_OUT, LOAD, LOOP, LOOP_BEATS, Cue, Track
from . import cuehistory, theme
from .metronome import MetronomeControls
from .waveform import Loader
from .worker import Job
from .zoomwave import (
    ZoomControls,
    ZoomWave,
    clock,
    cue_color,
    hot_key,
    quantize_to_beat,
    snap_key,
    swatch,
)

# one literal tr() per kind, so the catalog can give each its own case and gender
def kind_name(kind: int) -> str:
    if kind == FADE_IN:
        return tr("fade in")
    if kind == FADE_OUT:
        return tr("fade out")
    if kind == LOAD:
        return tr("load")
    if kind == LOOP:
        return tr("loop")
    return tr("cue")

def label(c: Cue) -> str:
    base = kind_name(c.kind)
    return (
        tr("Hot {slot} ({kind})", slot="ABCDEFGH"[c.hotcue], kind=base)
        if 0 <= c.hotcue < 8
        else tr("Memory {kind}", kind=base)
    )

# ------------------------------------------------------------
# 'mm:ss.mmm' or plain seconds, in ms
def parse_clock(s: str) -> float | None:
    s = s.strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            ms = (int(m) * 60 + float(sec)) * 1000
        else:
            ms = float(s) * 1000
    except ValueError:
        return None
    # "inf" and "1e999" are floats too, the clock cannot show them
    return ms if math.isfinite(ms) else None

COMPACT_BUTTON = "QPushButton { padding: 2px 10px; }"

# ------------------------------------------------------------------
# the zoomable wave of the track: the needle is the position cues are added at
class Canvas(ZoomWave):
    def __init__(self, t: Track, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.show_track(t, None)
        self.needle = -1.0

# ------------------------------------------------------------
class CueEdit(QDialog):
    def __init__(self, c: Cue, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit cue"))
        f = QFormLayout(self)
        self.time = QLineEdit(clock(c.start).replace("--:--.-", "00:00.0"))
        self.name = QLineEdit(c.name)
        self.slot = QComboBox()
        self.slot.addItems([tr("Memory")] + [tr("Hot {slot}", slot=x) for x in "ABCDEFGH"])
        self.slot.setCurrentIndex(c.hotcue + 1 if 0 <= c.hotcue < 8 else 0)
        self.length = QLineEdit(f"{c.length / 1000:.3f}" if c.is_loop else "")
        self.length.setPlaceholderText(tr("seconds, empty = no loop"))
        f.addRow(tr("Time (mm:ss.s)"), self.time)
        f.addRow(tr("Name"), self.name)
        f.addRow(tr("Slot"), self.slot)
        f.addRow(tr("Loop length"), self.length)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    # ------------------------------------------------------------
    def apply(self, c: Cue, dur_ms: float):
        ms = parse_clock(self.time.text())
        if ms is not None:
            c.start = max(0.0, min(ms, dur_ms or ms))
        c.name = self.name.text()
        c.hotcue = self.slot.currentIndex() - 1
        ln = parse_clock(self.length.text()) if self.length.text().strip() else 0.0
        c.length = max(0.0, ln or 0.0)
        if c.length > 0:
            c.kind = LOOP
        elif c.kind == LOOP:
            c.kind = CUE

class Timeline(QDialog):
    changed = Signal()

    # ------------------------------------------------------------
    def __init__(
        self, t: Track, path: str, loader: Loader, cfg: dict, engine=None, parent=None
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.t, self.path, self.loader, self.engine = t, path, loader, engine
        self._loop: Cue | None = None
        self._shown_loop: Cue | None = None
        self.setWindowTitle(tr("Cues: {artist} - {title}", artist=t.artist, title=t.title))
        self.resize(1280, 540)

        self.head = QLabel()
        self.key_format = cfg.get("key_format", "Open Key")
        self.canvas = Canvas(t)
        self.canvas.mode = cfg.get("waveform_color", "RGB")
        self.canvas.snap_to_beat = bool(engine and engine.snap_to_beat)
        self.canvas.wave = loader.get(path) if path else None
        loader.ready.connect(self.wave_ready)
        loader.hi_ready.connect(self.hi_ready)
        self.canvas.seek.connect(self.seek)
        self.canvas.need_hi.connect(self.want_hi)
        self.canvas.activated.connect(self.add_at)
        self.canvas.cue_moved.connect(lambda _c=None, *_: self.touched())
        self.canvas.grid_moved.connect(self.grid_moved)
        self.follow = QTimer(self)
        self.follow.timeout.connect(self.tick)
        self.follow.start(40)

        self.bpm_box = QDoubleSpinBox()
        self.bpm_box.setRange(0, 1000)
        self.bpm_box.setDecimals(6)
        self.bpm_box.setSingleStep(0.01)
        self.bpm_box.setSuffix(" BPM")
        self.bpm_box.setMinimumWidth(140)
        self.grid_box = QDoubleSpinBox()
        self.grid_box.setRange(-1, max(0, t.duration * 1000, t.grid or 0))
        self.grid_box.setDecimals(3)
        self.grid_box.setSingleStep(1)
        self.grid_box.setSpecialValueText(tr("No beat grid"))
        self.grid_box.setSuffix(tr(" ms"))
        self.grid_box.setMinimumWidth(130)
        self.grid_box.setToolTip(tr("Position of the first beat, in milliseconds"))
        self.set_grid_button = QPushButton(tr("Set at cursor"))
        self.set_grid_button.setToolTip(
            tr("Place the first beat marker at the waveform cursor")
        )
        self.set_grid_button.clicked.connect(self.set_grid_at_cursor)
        self.clear_grid_button = QPushButton(tr("Clear"))
        self.clear_grid_button.setToolTip(
            tr("Remove the beat grid marker; tempo is unchanged")
        )
        self.clear_grid_button.clicked.connect(self.clear_grid)
        self.apply_grid_button = QPushButton(tr("Apply"))
        self.apply_grid_button.setToolTip(tr("Apply the tempo and first beat values"))
        self.apply_grid_button.clicked.connect(self.apply_beatgrid)
        self.detect_button = QPushButton(tr("Detect"))
        self.detect_button.setToolTip(
            tr("Analyse the audio to find the tempo and the first beat (constant tempo)")
        )
        self.detect_button.setEnabled(bool(path))
        self.detect_button.clicked.connect(self.detect_beatgrid)
        self.detect_job = None
        self.nudge_buttons = []
        for text, delta in (("−10", -10), ("−1", -1), ("+1", 1), ("+10", 10)):
            button = QPushButton(text)
            button.setStyleSheet("QPushButton { padding: 5px 6px; }")
            button.setMinimumWidth(max(46, button.sizeHint().width()))
            button.setToolTip(tr("Move first beat marker by {delta:+d} ms", delta=delta))
            button.clicked.connect(lambda _checked=False, *_, d=delta: self.nudge_grid(d))
            self.nudge_buttons.append(button)
        for button in (
            self.set_grid_button,
            self.clear_grid_button,
            self.detect_button,
            self.apply_grid_button,
        ):
            button.setStyleSheet(COMPACT_BUTTON)
        self.refresh_beatgrid()

        toolbar = QHBoxLayout()
        toolbar.setSpacing(5)
        self.play_button = QPushButton(tr("Play / pause"))
        self.play_button.setAccessibleName(tr("Play or pause playback"))
        self.play_button.setToolTip(tr("Start or pause preview playback (Space/P)"))
        self.play_button.setStyleSheet(COMPACT_BUTTON)
        self.play_button.clicked.connect(self.toggle)
        toolbar.addWidget(self.play_button)
        toolbar.addWidget(QLabel(tr("Hot cues")))
        self.cue_pads: list[QPushButton] = []
        for slot, letter in enumerate("ABCDEFGH"):
            button = QPushButton(letter)
            button.setFixedSize(26, 26)
            button.setToolTip(
                tr("Set cue {letter} at the cursor, or jump to it", letter=letter)
            )
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.clicked.connect(
                lambda _checked=False, *_, s=slot: self.pad(s, play=False)
            )
            button.customContextMenuRequested.connect(
                lambda pos=None, *_, s=slot: self.show_pad_menu(s, pos)
            )
            self.cue_pads.append(button)
            toolbar.addWidget(button)
        self.snap_btn = QPushButton("S")
        self.snap_btn.setFixedSize(30, 26)
        self.snap_btn.setAccessibleName(tr("Snap to beat"))
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(bool(engine and engine.snap_to_beat))
        self.snap_btn.setToolTip(
            tr("Snap new and moved cues to the nearest beat (S or Ctrl+B)")
        )
        self.snap_btn.toggled.connect(self.set_snap_to_beat)
        self.set_snap_to_beat(bool(engine and engine.snap_to_beat))
        toolbar.addWidget(self.snap_btn)
        self.metronome_controls = (
            MetronomeControls(engine, lambda: self.t, compact=True) if engine else None
        )
        if self.metronome_controls is not None:
            toolbar.addWidget(self.metronome_controls)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel(tr("Tempo")))
        toolbar.addWidget(self.bpm_box)
        toolbar.addWidget(QLabel(tr("First beat")))
        toolbar.addWidget(self.grid_box)

        self.show_hot = QCheckBox(tr("Hot cues"))
        self.show_hot.setAccessibleName(tr("Show hot cues"))
        self.show_hot.setToolTip(tr("Show or hide hot cues"))
        self.show_mem = QCheckBox(tr("Memory cues"))
        self.show_mem.setAccessibleName(tr("Show memory cues"))
        self.show_mem.setToolTip(tr("Show or hide memory cues"))
        self.show_loop = QCheckBox(tr("Loops"))
        self.show_loop.setAccessibleName(tr("Show loops"))
        self.show_loop.setToolTip(tr("Show or hide loops"))
        for b in (self.show_hot, self.show_mem, self.show_loop):
            b.setChecked(True)
            b.toggled.connect(self.fill)
        self.stats = QLabel("")
        self.stats.setStyleSheet(f"color: {theme.FG_MUTED}; font-size: 8pt;")

        self.table = QTreeWidget()
        self.table.setHeaderLabels([tr("Time"), tr("Type"), tr("Length"), tr("Name")])
        self.table.setRootIsDecorated(False)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setColumnWidth(0, 100)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 90)
        self.table.itemDoubleClicked.connect(self.edit)
        self.table.itemSelectionChanged.connect(self.refresh_action_buttons)

        hint = QLabel(tr("A-H cues · S snap · Space/P play · Ctrl+Z/Y"))
        hint.setToolTip(
            tr(
                "Click/drag the cyan first-beat marker in the wave header to align the beat grid; use Tempo, First beat and Nudge for precision.  |  "
            "Click or drag on the wave: move the cursor (no playback)  |  double click: add a cue  |  drag a cue letter: move it (Snap button or Shift: snap to the beat, Esc or release outside the wave: cancel)  |  Ctrl+Z: undo  |  "
            "A-H: set a hot cue at the cursor, again: play from it, Shift+A-H: delete it  |  "
            "Right-click a pad: set a 1, 2, 4 or 8-beat loop or remove it  |  "
            "Click a loop pad to activate/deactivate it  |  Space: play  |  Ctrl + wheel: zoom  |  "
            "wheel: scroll when zoomed  |  bar below: scrub  |  Ctrl+D: detect tempo and first beat  |  "
            "Alt+Left/Right: nudge the first beat 1 ms (+Shift: 10 ms)  |  S: snap"
            )
        )
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hint.setStyleSheet(f"color: {theme.FG_MUTED}; font-size: 8pt;")
        self.shortcut_hint = hint

        def action(text, fn, tip):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setStyleSheet(COMPACT_BUTTON)
            b.clicked.connect(fn)
            return b

        actions = QHBoxLayout()
        actions.setSpacing(4)
        actions.addWidget(self.set_grid_button)
        actions.addWidget(self.clear_grid_button)
        self.add_button = action(
            tr("Add cue"), self.add, tr("Add a memory cue at the cursor")
        )
        self.add_loop_button = action(
            tr("Add 4-beat loop"),
            self.add_loop,
            tr(
                "Add a 4-beat loop in the first free hot cue slot (memory loop if all eight are used)"
            ),
        )
        actions.addWidget(self.add_button)
        actions.addWidget(self.add_loop_button)
        actions.addWidget(QLabel(tr("Nudge")))
        for button in self.nudge_buttons:
            actions.addWidget(button)
        actions.addWidget(self.detect_button)
        actions.addWidget(self.apply_grid_button)
        actions.addStretch(1)
        for b in (self.show_hot, self.show_mem, self.show_loop):
            actions.addWidget(b)
        btns = QHBoxLayout()
        btns.addWidget(self.stats)
        btns.addSpacing(16)
        btns.addWidget(hint, 1)
        self.edit_button = action(
            tr("Edit"), self.edit, tr("Edit the selected cue")
        )
        self.delete_button = action(
            tr("Delete"), self.delete, tr("Delete the selected cues (Del)")
        )
        self.copy_button = action(
            tr("Copy list"), self.copy, tr("Copy the cue list to the clipboard")
        )
        for button in (self.edit_button, self.delete_button, self.copy_button):
            btns.addWidget(button)
        btns.addSpacing(8)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        btns.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        wave_row = QHBoxLayout()
        wave_row.addWidget(self.canvas, 1)
        self.zoom_controls = ZoomControls(self.canvas)
        wave_row.addWidget(self.zoom_controls)
        lay.addLayout(wave_row, 1)
        lay.addLayout(toolbar)
        lay.addLayout(actions)
        lay.addWidget(self.table, 1)
        lay.addLayout(btns)
        for k in ("Space", "P"):
            QShortcut(QKeySequence(k), self, self.toggle)
        QShortcut(QKeySequence.Delete, self.table, self.delete)
        QShortcut(QKeySequence.Undo, self, self.undo)
        QShortcut(QKeySequence.Redo, self, lambda: self.undo(True))
        for seq, fn in (
            ("Ctrl+D", self.detect_beatgrid),
            ("Ctrl+B", self.snap_btn.toggle),
            ("Alt+Left", lambda: self.nudge_grid(-1)),
            ("Alt+Right", lambda: self.nudge_grid(1)),
            ("Alt+Shift+Left", lambda: self.nudge_grid(-10)),
            ("Alt+Shift+Right", lambda: self.nudge_grid(10)),
        ):
            QShortcut(QKeySequence(seq), self, fn)
        self.detect_button.setToolTip(self.detect_button.toolTip() + " (Ctrl+D)")
        # letters would reach the table's type-ahead: only handled here
        QApplication.instance().installEventFilter(self)
        self.fill()
        self.refresh_action_buttons()

    def eventFilter(self, obj, e):
        if snap_key(e, self):
            self.snap_btn.toggle()
            return True
        hit = hot_key(e, self)
        if hit:
            self.pad(*hit)
            return True
        return super().eventFilter(obj, e)

    def refresh_beatgrid(self):
        t = self.t
        first = tr(
            "first beat {time}",
            time=clock(t.grid) if t.grid is not None else tr("not set"),
        )
        self.head.setText(
            f"<b>{t.artist} - {t.title}</b><br>"
            f"<span style='color:{theme.FG_MUTED}'>"
            f"BPM {t.bpm:.3f} | {clock(t.duration * 1000)} | "
            f"{keys.name(t.key, self.key_format)} | "
            f"{first}</span>"
        )
        if hasattr(self, "bpm_box"):
            self.grid_box.setRange(-1, max(0, t.duration * 1000, t.grid or 0))
            self.bpm_box.setValue(max(0.0, t.bpm))
            self.grid_box.setValue(
                min(t.grid, self.grid_box.maximum()) if t.grid is not None else -1
            )
            self.clear_grid_button.setEnabled(t.grid is not None)
            self.set_grid_button.setEnabled(t.duration > 0)
            for button in self.nudge_buttons:
                button.setEnabled(t.grid is not None)
        self.canvas.update()

    def detect_beatgrid(self):
        if self.detect_job is not None or not self.path:
            return
        job = Job(beatdetect.analyze, self.path)
        job.done.connect(self.detected)
        job.failed.connect(self.detect_failed)
        job.finished.connect(self.detect_finished)
        self.detect_job = job
        self.detect_button.setEnabled(False)
        self.detect_button.setText(tr("Detecting..."))
        job.start()

    def confirm_detected(self, r: beatdetect.BeatResult) -> bool:
        now = tr(
            "{bpm:.3f} BPM, first beat {time}",
            bpm=self.t.bpm,
            time=clock(self.t.grid) if self.t.grid is not None else tr("not set"),
        )
        text = tr(
            "Detected {bpm:.3f} BPM, first beat {time}.\n\nCurrent: {now}.\n\nApply it? You can undo it with Ctrl+Z.",
            bpm=r.bpm,
            time=clock(r.first_beat_ms),
            now=now,
        )
        if not r.reliable:
            text = (
                tr(
                    "The detection is not reliable (no steady beat found, or the tempo changes).\n\n"
                )
                + text
            )
        return (
            QMessageBox.question(
                self,
                tr("Detect beat grid"),
                text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes if r.reliable else QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    def detected(self, r: beatdetect.BeatResult):
        # the editor was closed while the analysis finished: nothing to ask, nothing to apply
        if self.detect_job is not None and self.detect_job.stop.is_set():
            return
        if not self.confirm_detected(r):
            return
        self.bpm_box.setValue(r.bpm)
        self.grid_box.setValue(min(r.first_beat_ms, self.grid_box.maximum()))
        self.apply_beatgrid()

    def detect_failed(self, message: str):
        QMessageBox.warning(self, tr("Detect beat grid"), message)

    def detect_finished(self):
        job, self.detect_job = self.detect_job, None
        if job is not None:
            job.deleteLater()
        self.detect_button.setText(tr("Detect"))
        self.detect_button.setEnabled(bool(self.path))

    def apply_beatgrid(self):
        bpm = self.bpm_box.value()
        grid = self.grid_box.value() if self.grid_box.value() >= 0 else None
        if grid is not None:
            grid = min(grid, self.t.duration * 1000) if self.t.duration > 0 else grid
        if (bpm, grid) == (self.t.bpm, self.t.grid):
            self.refresh_beatgrid()
            return
        cuehistory.mark(self.t)
        self.t.bpm, self.t.grid = bpm, grid
        self.touched()

    def set_grid_at_cursor(self):
        self.grid_box.setValue(min(self.position(), self.grid_box.maximum()))
        self.apply_beatgrid()

    def clear_grid(self):
        self.grid_box.setValue(-1)
        self.apply_beatgrid()

    def nudge_grid(self, delta: float):
        if self.t.grid is None:
            return
        self.grid_box.setValue(
            max(0.0, min(self.grid_box.value() + delta, self.grid_box.maximum()))
        )
        self.apply_beatgrid()

    def grid_moved(self, track: Track):
        if track is self.t:
            self.touched()

    def play_from(self, ms: float, play: bool = True):
        self.canvas.set_pos(ms)
        if self.engine is None or not self.path:
            return
        if self.playing_here():
            self.engine.timing_track = self.t
            self.engine.seek(ms)
            if play and not self.engine.playing:
                self.engine.toggle()
        elif play and self.engine.play(self.path, self.t):
            self.engine.seek(ms)

    # ------------------------------------------------------------------
    # Key A-H: sets the hot cue at the cursor, or plays from it when it exists
    def pad(self, slot: int, delete: bool = False, play: bool = True):
        c = next((x for x in self.t.cues if x.hotcue == slot), None)
        if delete:
            if c is None:
                return
            cuehistory.mark(self.t)
            self.t.cues.remove(c)
            if self.loop is c:
                self.loop = None
        elif c is None:
            cuehistory.mark(self.t)
            self.t.cues.append(
                Cue(kind=CUE, start=self.cue_position(self.position()), hotcue=slot)
            )
        elif c.is_loop:
            self.loop = None if self.loop is c else c
            if self.loop:
                self.play_from(c.start, play)
            self.refresh_cue_pads()
            return
        else:
            self.play_from(c.start, play)
            return
        self.touched()

    # --------------------------------------------
    def pad_menu(self, slot: int) -> QMenu:
        menu = QMenu(self.cue_pads[slot])
        for beats in LOOP_BEATS:
            menu.addAction(
                tr("Set {beats}-beat loop", beats=beats),
                lambda _checked=False, *_, s=slot, n=beats: self.set_loop(s, n),
            )
        if any(c.hotcue == slot for c in self.t.cues):
            menu.addSeparator()
            menu.addAction(
                tr("Remove cue {letter}", letter=chr(ord("A") + slot)),
                lambda _checked=False, *_, s=slot: self.pad(s, delete=True),
            )
        return menu

    def show_pad_menu(self, slot: int, pos):
        menu = self.pad_menu(slot)
        try:
            menu.exec(self.cue_pads[slot].mapToGlobal(pos))
        finally:
            menu.deleteLater()

    def set_loop(self, slot: int, beats: int = 4):
        if beats not in LOOP_BEATS:
            raise ValueError(f"Loop length must be one of {LOOP_BEATS} beats")
        beat = 60000 / self.t.bpm if self.t.bpm else 500
        cuehistory.mark(self.t)
        previous = next((cue for cue in self.t.cues if cue.hotcue == slot), None)
        was_active = previous is not None and self.loop is previous
        self.t.cues = [cue for cue in self.t.cues if cue.hotcue != slot]
        cue = Cue(
            kind=LOOP,
            start=self.cue_position(self.position()),
            length=beats * beat,
            hotcue=slot,
        )
        self.t.cues.append(cue)
        if was_active:
            self.loop = cue
        self.touched()

    # A 4 beat loop at the cursor, in the first free hot slot (a memory loop when all are taken)
    def add_loop(self):
        used = {x.hotcue for x in self.t.cues}
        slot = next((s for s in range(8) if s not in used), -1)
        beat = 60000 / self.t.bpm if self.t.bpm else 500
        cuehistory.mark(self.t)
        self.t.cues.append(
            Cue(
                kind=LOOP,
                start=self.cue_position(self.position()),
                length=4 * beat,
                hotcue=slot,
            )
        )
        self.touched()

    def wave_ready(self, path, wave):
        if path == self.path:
            self.canvas.wave = wave
            self.canvas.update()

    def want_hi(self):
        h = self.loader.get_hi(self.path) if self.path else None
        if h is not None:
            self.canvas.set_hi(h)

    def hi_ready(self, path, arr):
        if path == self.path and arr is not None:
            self.canvas.set_hi(arr)

    def playing_here(self) -> bool:
        return self.engine is not None and self.engine.path == self.path

    def seek(self, ms: float):
        # only the cursor moves (the canvas holds it), the track follows when it already plays
        if self.playing_here():
            self.engine.seek(ms)

    def tick(self):
        if self.metronome_controls is not None:
            self.metronome_controls.refresh()
        if self.engine is not None:
            self.snap_btn.setChecked(self.engine.snap_to_beat)
            self.canvas.snap_to_beat = self.engine.snap_to_beat
        if self.loop is not self._shown_loop:
            self.refresh_cue_pads()
        if self.playing_here():
            pos = float(self.engine.a.position())
            if self.loop and pos >= self.loop.start + self.loop.length:
                self.engine.seek(self.loop.start)
                pos = self.loop.start
            if pos != self.canvas.needle:
                self.canvas.set_pos(pos)

    def toggle(self):
        if self.engine is None or not self.path:
            return
        if self.playing_here():
            self.engine.timing_track = self.t
            self.engine.toggle()
        else:
            self.engine.play(self.path, self.t)
            if self.canvas.needle > 0:
                self.engine.seek(self.canvas.needle)

    def position(self) -> float:
        return self.canvas.needle if self.canvas.needle >= 0 else 0.0

    def set_snap_to_beat(self, enabled: bool):
        if self.engine is not None:
            self.engine.snap_to_beat = enabled
        self.canvas.snap_to_beat = enabled
        self.snap_btn.setText("S")
        self.snap_btn.setStyleSheet(
            "QPushButton { padding: 0; }"
            f"QPushButton:checked {{ background: {theme.ACCENT}; font-weight: bold; }}"
        )

    def cue_position(self, ms: float) -> float:
        snap = (
            self.engine.snap_to_beat
            if self.engine is not None
            else self.snap_btn.isChecked()
        )
        return quantize_to_beat(self.t, ms) if snap else max(0.0, ms)

    def visible(self, c: Cue) -> bool:
        if c.is_loop:
            return self.show_loop.isChecked()
        return self.show_hot.isChecked() if c.hotcue >= 0 else self.show_mem.isChecked()

    def fill(self):
        self.table.clear()
        cues = sorted(self.t.cues, key=lambda c: c.start)
        for c in cues:
            if not self.visible(c):
                continue
            it = QTreeWidgetItem(
                [clock(c.start), label(c), clock(c.length) if c.is_loop else "", c.name]
            )
            it.setIcon(1, swatch(cue_color(c)))
            it.setData(0, Qt.UserRole, c)
            self.table.addTopLevelItem(it)
        hot = sum(c.hotcue >= 0 for c in cues)
        loops = sum(c.is_loop for c in cues)
        fields = {
            "cues": len(cues),
            "hot": hot,
            "memory": len(cues) - hot,
            "loops": loops,
        }
        self.stats.setText(
            tr("{cues} cues, {hot} hot, {memory} memory, {loops} loop", **fields)
            if loops == 1
            else tr("{cues} cues, {hot} hot, {memory} memory, {loops} loops", **fields)
        )
        self.refresh_cue_pads()
        self.canvas.update()

    # ------------------------------------------------------------
    def refresh_cue_pads(self):
        active_loop = self.loop
        if (
            self.engine is not None
            and self.engine.loop is not None
            and self.engine.loop[0] is self.t
            and active_loop is None
        ):
            self.engine.loop = None
        self._shown_loop = active_loop
        if active_loop is not None and not any(c is active_loop for c in self.t.cues):
            self.loop = None
        used = {cue.hotcue: cue for cue in self.t.cues if 0 <= cue.hotcue < 8}
        for slot, button in enumerate(self.cue_pads):
            cue = used.get(slot)
            if cue is None:
                button.setStyleSheet(
                    f"background: {theme.BG_MED}; color: {theme.FG_MUTED}; padding: 0;"
                )
                button.setToolTip(
                    tr(
                        "Click or press {key} to set a cue at the cursor; right-click to create a 1, 2, 4 or 8-beat loop",
                        key=chr(ord("A") + slot),
                    )
                )
            else:
                active = cue.is_loop and active_loop is cue
                border = (
                    " border: 2px solid white;"
                    if active
                    else f" border: 1px solid {theme.FG_MUTED};"
                    if cue.is_loop
                    else ""
                )
                button.setStyleSheet(
                    f"background: {cue_color(cue)}; color: black; font-weight: bold; padding: 0;{border}"
                )
                fields = {
                    "name": cue.name or tr("Cue"),
                    "time": clock(cue.start),
                    "length": clock(cue.length),
                    "key": chr(ord("A") + slot),
                }
                if not cue.is_loop:
                    text = tr(
                        "{name} at {time}; click or press {key} to jump/play; right-click: loop length or remove; Shift+key: delete",
                        **fields,
                    )
                elif active:
                    text = tr(
                        "{name} at {time} ({length}, click to deactivate loop); click or press {key} to jump/play; right-click: loop length or remove; Shift+key: delete",
                        **fields,
                    )
                else:
                    text = tr(
                        "{name} at {time} ({length}, click to activate loop); click or press {key} to jump/play; right-click: loop length or remove; Shift+key: delete",
                        **fields,
                    )
                button.setToolTip(text)

    @property
    def loop(self) -> Cue | None:
        if self.engine is None:
            return self._loop
        state = self.engine.loop
        if state is None or state[0] is not self.t:
            return None
        return state[1]

    @loop.setter
    def loop(self, cue: Cue | None):
        if self.engine is None:
            self._loop = cue
        elif cue is None:
            if self.engine.loop is not None and self.engine.loop[0] is self.t:
                self.engine.loop = None
        else:
            self.engine.loop = (self.t, cue)

    def selected(self) -> list[Cue]:
        return [it.data(0, Qt.UserRole) for it in self.table.selectedItems()]

    def refresh_action_buttons(self):
        selected = bool(self.selected())
        self.edit_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)

    def touched(self):
        self.refresh_beatgrid()
        if self.metronome_controls is not None:
            self.metronome_controls.refresh()
        self.fill()
        self.changed.emit()

    def add(self):
        self.add_at(self.position())

    # ------------------------------------------------------------
    def add_at(self, ms: float):
        c = Cue(kind=CUE, start=self.cue_position(max(0.0, ms)))
        d = CueEdit(c, self)
        if d.exec():
            cuehistory.mark(self.t)
            d.apply(c, self.t.duration * 1000)
            c.start = self.cue_position(c.start)
            if c.hotcue >= 0:
                self.t.cues = [x for x in self.t.cues if x.hotcue != c.hotcue]
            self.t.cues.append(c)
            self.touched()
        d.deleteLater()

    # --------------------------------------------------------------
    def edit(self, *_):
        sel = self.selected()
        if not sel:
            return
        c = sel[0]
        d = CueEdit(c, self)
        if d.exec():
            cuehistory.mark(self.t)
            d.apply(c, self.t.duration * 1000)
            c.start = self.cue_position(c.start)
            if c.hotcue >= 0:
                self.t.cues = [x for x in self.t.cues if x is c or x.hotcue != c.hotcue]
            self.touched()
        d.deleteLater()

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        if (
            QMessageBox.question(
                self, tr("Delete"), tr("Delete {n} cue(s)?", n=len(sel))
            )
            != QMessageBox.Yes
        ):
            return
        cuehistory.mark(self.t)
        gone = {id(c) for c in sel}
        self.t.cues = [c for c in self.t.cues if id(c) not in gone]
        self.touched()

    def undo(self, redo: bool = False):
        if (cuehistory.redo if redo else cuehistory.undo)(self.t):
            self.touched()

    def copy(self):
        lines = [f"{self.t.artist} - {self.t.title}", f"BPM {self.t.bpm:.2f}", ""]
        for c in sorted(self.t.cues, key=lambda c: c.start):
            if self.visible(c):
                ln = "  " + tr("length {time}", time=clock(c.length)) if c.is_loop else ""
                lines.append(f"{clock(c.start)}  {label(c)}{ln}  {c.name}".rstrip())
        QApplication.clipboard().setText("\n".join(lines))

    # ------------------------------------------------------
    def done(self, r):
        # Esc goes through here too, closeEvent would miss it
        if self.detect_job is not None:
            self.detect_job.stop.set()
            if not self.detect_job.wait(5000):
                self.head.setText(
                    tr(
                        "Waiting for beat detection to stop. Try closing again when it finishes."
                    )
                )
                return
        self.follow.stop()
        QApplication.instance().removeEventFilter(self)
        self.loader.ready.disconnect(self.wave_ready)
        self.loader.hi_ready.disconnect(self.hi_ready)
        super().done(r)
