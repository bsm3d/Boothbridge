# the tabs of the test center: Aléatoire, Monkey, Mémoire and Rapports. Every long run is a child
# process (see runproc.py); a tab only builds the command, shows the log and reads the record back

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

from djapp.ui import theme
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import (
    RUNS,
    fuzz,
    list_runs,
    load_run,
    memory,
    monkey,
    new_run_path,
    save_run,
    short,
)
from .curve import SERIES, Curve
from .runproc import RunProcess, child_env

STATE_COLORS = {"ok": theme.GREEN, "echec": theme.RED, "erreur": theme.RED, "timeout": theme.YELLOW,
                "interrompu": theme.YELLOW, "plantage": theme.RED, "blocage": theme.RED, "saute": theme.FG_MUTED,
                "stable": theme.GREEN, "fuite probable": theme.RED}
KIND_NAMES = {"suites": "Suites", "random": "Aléatoire", "monkey": "Monkey", "memory": "Mémoire"}


def mono() -> QFont:
    return QFont("Consolas", 9)


# --------------------------------------------------------
def log_pane(placeholder: str) -> QPlainTextEdit:
    pane = QPlainTextEdit()
    pane.setReadOnly(True)
    pane.setFont(mono())
    pane.setPlaceholderText(placeholder)
    pane.setMaximumBlockCount(20000)
    return pane


def colored(item: QTreeWidgetItem | QTableWidgetItem, state: str):
    color = STATE_COLORS.get(state)
    if color:
        if isinstance(item, QTreeWidgetItem):
            item.setForeground(1, QColor(color))
        else:
            item.setForeground(QColor(color))

def seconds_text(value: float) -> str:
    return f"{value:.1f} s" if value < 120 else f"{value / 60:.1f} min"


# ------------------------------------------------------------------
# common part: a child process, its live log, a Stop button, the record read back at the end
class RunPanel(QWidget):
    KIND = ""
    FLAG = ""
    run_finished = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = RunProcess(self)
        self.proc.line.connect(self.on_line)
        self.proc.done.connect(self.on_done)
        self.out: Path | None = None
        self.last_path: Path | None = None
        self.record: dict = {}
        self.command: list[str] = []
        self.log = log_pane("La sortie du processus apparaîtra ici.")
        self.stop_button = QPushButton("Arrêter")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.status = QLabel("Prêt.")
        self.status.setWordWrap(True)
        self.copy_button = QPushButton("Copier le résultat")
        self.copy_button.clicked.connect(self.copy_result)
        self.timeout_box = QDoubleSpinBox()
        self.timeout_box.setRange(0, 600)
        self.timeout_box.setDecimals(0)
        self.timeout_box.setSuffix(" min")
        self.timeout_box.setSpecialValueText("sans limite")
        self.timeout_box.setValue(20)
        self.timeout_box.setToolTip("Temps limite de la séance : le processus est arrêté passé ce délai.")

    # ---- running

    def running(self) -> bool:
        return self.proc.running()

    def launch(self, args: list[str], visible: bool = False, title: str = ""):
        if self.running():
            return
        self.out = new_run_path(self.KIND)
        self.command = ["tests/test_center.py", self.FLAG, *args, "--out", str(self.out)]
        self.log.clear()
        self.record = {}
        self.set_busy(True)
        self.status.setText(f"{title or KIND_NAMES.get(self.KIND, self.KIND)} en cours...")
        self.log.appendPlainText("> python " + " ".join(self.command))
        self.proc.start(self.command, self.timeout_box.value() * 60, child_env(visible))

    def set_busy(self, busy: bool):
        self.stop_button.setEnabled(busy)
        self.bar.setRange(0, 0 if busy else 1)
        self.bar.setValue(0)
        for widget in self.controls():
            widget.setEnabled(not busy)

    def controls(self) -> list[QWidget]:
        return []

    def stop(self):
        if self.running():
            self.status.setText("Arrêt en cours...")
            self.proc.stop()

    def on_line(self, text: str):
        self.log.appendPlainText(text)

    def on_done(self, code: int, timed_out: bool, stopped: bool):
        self.set_busy(False)
        path = self.out
        record = None
        if path is not None and path.exists():
            try:
                record = load_run(path)
            except (OSError, ValueError):
                record = None
        if record is None or timed_out or stopped:
            record = self.fallback(code, timed_out, stopped, record)
            save_run(path, record)
        self.record, self.last_path = record, path
        self.status.setText(f"{record.get('verdict', '?')} : {record.get('summary', '')}")
        self.show_record(record)
        self.run_finished.emit(str(path))

    # the child could not write its record (killed, crashed, hung): the log is all there is
    def fallback(self, code, timed_out, stopped, record):
        verdict = "timeout" if timed_out else "interrompu" if stopped else "plantage"
        tail = self.log.toPlainText()[-4000:]
        base = record or {"kind": self.KIND, "title": KIND_NAMES.get(self.KIND, self.KIND), "command": self.command,
                          "params": {}, "seed": None, "versions": {}}
        base.update(verdict=verdict, exit_code=code, log_tail=tail,
                    summary={"timeout": "temps limite atteint", "interrompu": "arrêté par l'utilisateur",
                             "plantage": f"le processus s'est arrêté sans résultat (code {code})"}[verdict])
        return base

    # subclasses draw the record
    def show_record(self, record: dict):
        pass

    def copy_result(self):
        text = self.result_text()
        QApplication.clipboard().setText(text or "Aucun résultat pour le moment.")
        self.status.setText("Résultat copié dans le presse-papiers.")

    def result_text(self) -> str:
        return json.dumps(self.record, indent=1, ensure_ascii=False, default=str) if self.record else ""

    # reports calls this with a saved record: same parameters, same command
    def replay(self, record: dict):
        pass

    def wait_stopped(self, ms: int = 4000) -> bool:
        self.proc.stop()
        return self.proc.wait(ms)


def seed_row(seed_box: QLineEdit, random_box: QCheckBox) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(seed_box)
    row.addWidget(random_box)
    return row


# --------------------------------------------------------
def pick_seed(seed_box: QLineEdit, random_box: QCheckBox) -> int:
    if random_box.isChecked() or not seed_box.text().strip().lstrip("-").isdigit():
        seed = random.SystemRandom().randrange(1_000_000)
        seed_box.setText(str(seed))
        return seed
    return int(seed_box.text())

# --------------------------------------------------------
# Aléatoire
# --------------------------------------------------------

# --------------------------------------------------------
class RandomPanel(RunPanel):
    KIND, FLAG = "random", "--random"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scenarios = QListWidget()
        self.scenarios.setWordWrap(True)
        self.scenarios.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for name, title, _fn in fuzz.SCENARIOS:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.scenarios.addItem(item)
        self.seed = QLineEdit("7")
        self.random_seed = QCheckBox("Graine au hasard")
        self.iterations = QSpinBox()
        self.iterations.setRange(1, 1_000_000)
        self.iterations.setValue(100)
        self.budget = QSpinBox()
        self.budget.setRange(0, 86400)
        self.budget.setSpecialValueText("sans limite")
        self.budget.setSuffix(" s")
        self.run_button = QPushButton("Lancer")
        self.run_button.setObjectName("go")
        self.run_button.clicked.connect(self.run_selected)
        self.replay_button = QPushButton("Rejouer la graine choisie")
        self.replay_button.clicked.connect(self.replay_selected)
        form = QFormLayout()
        form.addRow("Graine", seed_row(self.seed, self.random_seed))
        form.addRow("Cas par scénario", self.iterations)
        form.addRow("Budget de temps", self.budget)
        form.addRow("Temps limite", self.timeout_box)
        box = QGroupBox("Scénarios")
        inner = QVBoxLayout(box)
        inner.addWidget(self.scenarios)
        left = QVBoxLayout()
        left.addWidget(box, 1)
        left.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        left.addLayout(row)
        left.addWidget(self.replay_button)
        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setMaximumWidth(380)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Scénario / échec", "État", "Cas", "Durée", "Détail"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 60)
        self.tree.setColumnWidth(3, 70)
        self.tree.currentItemChanged.connect(self.show_detail)
        self.detail = log_pane("Sélectionne un échec pour voir sa trace et la commande qui le rejoue.")
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.tree)
        right.addWidget(self.detail)
        right.addWidget(self.log)
        right.setSizes([220, 130, 150])
        foot = QHBoxLayout()
        foot.addWidget(self.bar, 1)
        foot.addWidget(self.copy_button)
        rightw = QWidget()
        lay = QVBoxLayout(rightw)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(right, 1)
        lay.addWidget(self.status)
        lay.addLayout(foot)
        split = QSplitter()
        split.addWidget(leftw)
        split.addWidget(rightw)
        split.setSizes([340, 760])
        main = QHBoxLayout(self)
        main.addWidget(split)

    def controls(self):
        return [self.scenarios, self.seed, self.random_seed, self.iterations, self.budget, self.run_button,
                self.replay_button, self.timeout_box]

    def chosen(self) -> list[str]:
        return [self.scenarios.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.scenarios.count())
                if self.scenarios.item(i).checkState() == Qt.CheckState.Checked]

    def run_selected(self):
        names = self.chosen()
        if not names:
            self.status.setText("Coche au moins un scénario.")
            return
        self.start(names, pick_seed(self.seed, self.random_seed), self.iterations.value(), self.budget.value())

    def start(self, names, seed, iterations, budget=0):
        self.tree.clear()
        self.detail.clear()
        args = ["--scenario", ",".join(names), "--seed", str(seed), "--iterations", str(iterations)]
        if budget:
            args += ["--timeout", str(budget)]
        self.launch(args, title="Tests aléatoires")

    def replay_selected(self):
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data:
            self.status.setText("Sélectionne d'abord un échec dans la liste.")
            return
        self.start([data["scenario"]], data["seed"], 1)

    def replay(self, record: dict):
        p = record.get("params", {})
        names = [s for s in str(p.get("scenario", "all")).split(",") if s]
        self.start(names, record.get("seed") or 0, int(p.get("iterations", 100)))

    def show_record(self, record: dict):
        self.tree.clear()
        details = record.get("details") or {}
        for entry in details.get("scenarios", []):
            top = QTreeWidgetItem([f"{entry['name']} : {entry['title']}", entry["status"], str(entry["cases"]),
                                   seconds_text(entry["seconds"]), entry.get("message", "")])
            colored(top, entry["status"])
            self.tree.addTopLevelItem(top)
            for f in entry["failures"]:
                child = QTreeWidgetItem([f"graine {f['seed']} ({f['case']}) x{f['count']}", "échec", "", "",
                                         short(f["message"], 160)])
                child.setData(0, Qt.ItemDataRole.UserRole, f)
                colored(child, "echec")
                top.addChild(child)
            top.setExpanded(bool(entry["failures"]))
        first = next((self.tree.topLevelItem(i).child(0) for i in range(self.tree.topLevelItemCount())
                      if self.tree.topLevelItem(i).childCount()), None)
        if first is not None:
            self.tree.setCurrentItem(first)
        if record.get("summary") and isinstance(record["summary"], str) and not details:
            self.detail.setPlainText(record["summary"] + "\n" + record.get("log_tail", ""))

    def show_detail(self, item, _previous=None):
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data:
            self.detail.clear()
            return
        self.detail.setPlainText(f"{data['message']}\n\nPour rejouer :\n  {data['replay']}\n\n{data.get('detail', '')}")


# --------------------------------------------------------
# monkey
# --------------------------------------------------------

# --------------------------------------------------------
class MonkeyPanel(RunPanel):
    KIND, FLAG = "monkey", "--monkey"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.seed = QLineEdit("7")
        self.random_seed = QCheckBox("Graine au hasard")
        self.steps = QSpinBox()
        self.steps.setRange(1, 1_000_000)
        self.steps.setValue(300)
        self.budget = QSpinBox()
        self.budget.setRange(0, 86400)
        self.budget.setSpecialValueText("sans limite")
        self.budget.setSuffix(" s")
        self.delay = QSpinBox()
        self.delay.setRange(0, 5000)
        self.delay.setSuffix(" ms")
        self.hang = QSpinBox()
        self.hang.setRange(3, 600)
        self.hang.setValue(20)
        self.hang.setSuffix(" s")
        self.hang.setToolTip("Au-delà, la fenêtre est considérée comme bloquée et les piles de threads sont relevées.")
        self.visible = QCheckBox("Voir la fenêtre (plateforme Windows réelle)")
        self.visible.setToolTip("Sans cette case, tout se passe hors écran. Avec elle, mets un délai pour suivre.")
        self.windows = QListWidget()
        for kind, label in monkey.WINDOW_KINDS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, kind)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.windows.addItem(item)
        self.windows.setMaximumHeight(190)
        self.run_button = QPushButton("Lancer")
        self.run_button.setObjectName("go")
        self.run_button.clicked.connect(self.run_new)
        self.replay_button = QPushButton("Rejouer le dernier")
        self.replay_button.clicked.connect(self.replay_last)
        self.reduce_button = QPushButton("Réduire")
        self.reduce_button.setToolTip("Cherche une suite de pas plus courte qui provoque le même échec.")
        self.reduce_button.clicked.connect(self.reduce_last)
        form = QFormLayout()
        form.addRow("Graine", seed_row(self.seed, self.random_seed))
        form.addRow("Pas", self.steps)
        form.addRow("Budget de temps", self.budget)
        form.addRow("Délai entre les pas", self.delay)
        form.addRow("Blocage après", self.hang)
        form.addRow("Temps limite", self.timeout_box)
        box = QGroupBox("Fenêtres jouées (la principale l'est toujours)")
        inner = QVBoxLayout(box)
        inner.addWidget(self.windows)
        left = QVBoxLayout()
        left.addLayout(form)
        left.addWidget(self.visible)
        left.addWidget(box)
        row = QHBoxLayout()
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        left.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.replay_button)
        row.addWidget(self.reduce_button)
        left.addLayout(row)
        left.addStretch(1)
        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setMaximumWidth(380)

        self.summary = QLabel("Aucun résultat pour le moment.")
        self.summary.setWordWrap(True)
        self.failures = QTableWidget(0, 4)
        self.failures.setHorizontalHeaderLabels(["Pas", "Type", "Action", "Message"])
        self.failures.horizontalHeader().setStretchLastSection(True)
        self.failures.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.failures.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.failures.verticalHeader().hide()
        self.failures.setColumnWidth(0, 50)
        self.failures.setColumnWidth(1, 90)
        self.failures.setColumnWidth(2, 120)
        self.failures.currentCellChanged.connect(self.show_failure)
        self.stack = log_pane("La pile de l'échec choisi apparaîtra ici.")
        self.steps_table = QTableWidget(0, 5)
        self.steps_table.setHorizontalHeaderLabels(["#", "Action", "Fenêtre", "Widget", "Argument"])
        self.steps_table.horizontalHeader().setStretchLastSection(True)
        self.steps_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.steps_table.verticalHeader().hide()
        self.steps_table.setColumnWidth(0, 50)
        self.steps_table.setColumnWidth(1, 80)
        self.steps_table.setColumnWidth(2, 140)
        self.steps_table.setColumnWidth(3, 220)
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.failures)
        right.addWidget(self.stack)
        right.addWidget(self.steps_table)
        right.addWidget(self.log)
        right.setSizes([120, 120, 170, 120])
        rightw = QWidget()
        lay = QVBoxLayout(rightw)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.summary)
        lay.addWidget(right, 1)
        foot = QHBoxLayout()
        foot.addWidget(self.bar, 1)
        foot.addWidget(self.copy_button)
        lay.addWidget(self.status)
        lay.addLayout(foot)
        split = QSplitter()
        split.addWidget(leftw)
        split.addWidget(rightw)
        split.setSizes([340, 760])
        main = QHBoxLayout(self)
        main.addWidget(split)

    def controls(self):
        return [self.seed, self.random_seed, self.steps, self.budget, self.delay, self.hang, self.visible, self.windows,
                self.run_button, self.replay_button, self.reduce_button, self.timeout_box]

    def chosen(self) -> list[str]:
        return [self.windows.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.windows.count())
                if self.windows.item(i).checkState() == Qt.CheckState.Checked]

    def options(self) -> list[str]:
        windows = self.chosen()
        args = ["--windows", ",".join(windows) if windows else "none", "--delay", str(self.delay.value()),
                "--hang", str(self.hang.value())]
        if self.budget.value():
            args += ["--budget", str(self.budget.value())]
        if self.visible.isChecked():
            args.append("--visible")
        return args

    def run_new(self):
        seed = pick_seed(self.seed, self.random_seed)
        self.launch(["--seed", str(seed), "--steps", str(self.steps.value()), *self.options()],
                    self.visible.isChecked(), "Monkey")

    def replay_last(self):
        if self.last_path is None:
            self.status.setText("Aucun résultat à rejouer : lance d'abord un monkey, ou choisis-en un dans Rapports.")
            return
        self.launch(["--replay", str(self.last_path), "--delay", str(self.delay.value()),
                     *(["--visible"] if self.visible.isChecked() else [])], self.visible.isChecked(), "Rejeu")

    def reduce_last(self):
        data = (self.record.get("details") or {}) if self.record else {}
        if self.last_path is None or not data.get("failures"):
            self.status.setText("Il faut un résultat avec un échec pour le réduire.")
            return
        self.launch(["--reduce", str(self.last_path)], False, "Réduction")

    def replay(self, record: dict):
        path = record.get("_path")
        p = record.get("params", {})
        self.seed.setText(str(record.get("seed", 0)))
        self.random_seed.setChecked(False)
        self.steps.setValue(int(p.get("steps", 300)))
        if path:
            self.last_path = Path(path)
            self.record = record
            self.replay_last()
        else:
            self.run_new()

    def show_record(self, record: dict):
        details = record.get("details") or {}
        self.failures.setRowCount(0)
        self.stack.clear()
        for f in details.get("failures", []):
            row = self.failures.rowCount()
            self.failures.insertRow(row)
            for col, text in enumerate((str(f["step"]), f["type"], f.get("action", ""), f["message"])):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, f)
                self.failures.setItem(row, col, cell)
            colored(self.failures.item(row, 1), "echec")
        if self.failures.rowCount():
            self.failures.selectRow(0)
            self.show_failure(0, 0, 0, 0)
        steps = details.get("steps", [])
        self.steps_table.setRowCount(len(steps))
        for row, s in enumerate(steps):
            values = (str(s.get("i", row)), s.get("act", ""), s.get("win", ""), s.get("label", ""),
                      short(json.dumps(s.get("arg", ""), ensure_ascii=False), 120)
                      + ("  [ignoré : " + s["skipped"] + "]" if s.get("skipped") else ""))
            for col, text in enumerate(values):
                self.steps_table.setItem(row, col, QTableWidgetItem(text))
        warn = details.get("warnings", {})
        text = record.get("summary", "")
        if isinstance(text, str):
            extra = []
            if details.get("reduced_from"):
                extra.append(f"réduit depuis {details['reduced_from']} pas")
            if warn.get("top"):
                extra.append("avertissements : " + " | ".join(f"{n}x {m}" for m, n in warn["top"][:3]))
            if details.get("blocked"):
                extra.append("actions bloquées : " + ", ".join(sorted(set(details["blocked"]))[:4]))
            self.summary.setText(text + ("\n" + "\n".join(extra) if extra else ""))
            self.summary.setStyleSheet(f"color: {STATE_COLORS.get(record.get('verdict', ''), theme.FG)};")

    def show_failure(self, row, _col, _prev_row, _prev_col):
        item = self.failures.item(row, 0) if row >= 0 else None
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.stack.setPlainText(f"{data['message']}\n\n{data.get('stack', '')}" if data else "")

    def result_text(self) -> str:
        details = (self.record.get("details") or {}) if self.record else {}
        if not details:
            return super().result_text()
        lines = [self.record.get("summary", ""), f"graine : {self.record.get('seed')}",
                 f"commande : {' '.join(self.record.get('command', []))}", ""]
        for f in details.get("failures", []):
            lines += [f"pas {f['step']} [{f['type']}] {f['message']}", f.get("stack", ""), ""]
        return "\n".join(lines)


# --------------------------------------------------------
# Mémoire
# --------------------------------------------------------

LIMIT_LABELS = [("memory_kb", "Mémoire : pente (Ko/cycle)", 0, 100000, 0), ("memory_net_kb", "Mémoire : croissance nette (Ko)", 0, 10**7, 0),
                ("heap_kb", "Tas Python : pente (Ko/cycle)", 0, 100000, 0), ("widgets", "Widgets : croissance nette", 0, 1000, 0),
                ("threads", "Threads : croissance nette", 0, 1000, 0), ("handles", "Poignées : croissance nette", 0, 10000, 0),
                ("children", "Processus enfants : croissance nette", 0, 100, 0), ("min_r2", "R2 minimal de la droite", 0, 1, 2)]


# --------------------------------------------------------
class MemoryPanel(RunPanel):
    KIND, FLAG = "memory", "--memory"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scenario = QComboBox()
        for name, title, _fn in memory.SCENARIOS:
            self.scenario.addItem(title, name)
        self.cycles = QSpinBox()
        self.cycles.setRange(4, 100000)
        self.cycles.setValue(30)
        self.warmup = QSpinBox()
        self.warmup.setRange(-1, 1000)
        self.warmup.setValue(-1)
        self.warmup.setSpecialValueText("automatique")
        self.limit_boxes: dict[str, QDoubleSpinBox] = {}
        defaults = memory.Limits()
        thresholds = QFormLayout()
        for key, label, low, high, decimals in LIMIT_LABELS:
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(decimals)
            box.setValue(getattr(defaults, key))
            self.limit_boxes[key] = box
            thresholds.addRow(label, box)
        self.series = QComboBox()
        for key, (label, _u, _s) in SERIES.items():
            self.series.addItem(label, key)
        self.series.currentIndexChanged.connect(lambda: self.curve.set_key(self.series.currentData()))
        self.run_button = QPushButton("Lancer")
        self.run_button.setObjectName("go")
        self.run_button.clicked.connect(self.run_new)
        self.curve = Curve()
        self.verdict = QLabel("Aucun résultat pour le moment.")
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet("font-size: 12pt; font-weight: 600;")
        self.evidence = QLabel("")
        self.evidence.setWordWrap(True)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Mesure", "Début", "Fin", "Pente", "R2", "Écart", "Avis"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 150)
        for col in (1, 2, 3, 5):
            self.table.setColumnWidth(col, 74)
        self.table.setColumnWidth(4, 40)
        self.cycles_table = QTableWidget(0, 9)
        self.cycles_table.setHorizontalHeaderLabels(["Cycle", "Privé Mo", "Résident Mo", "Tas Mo", "Widgets", "QObject",
                                                     "Threads", "Poignées", "Enfants"])
        self.cycles_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cycles_table.verticalHeader().hide()
        for col in range(9):
            self.cycles_table.setColumnWidth(col, 68)
        self.allocations = QTableWidget(0, 3)
        self.allocations.setHorizontalHeaderLabels(["Ligne", "Différence", "Objets"])
        self.allocations.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.allocations.verticalHeader().hide()
        self.allocations.horizontalHeader().setStretchLastSection(True)
        self.allocations.setColumnWidth(0, 170)

        form = QFormLayout()
        form.addRow("Scénario", self.scenario)
        form.addRow("Cycles", self.cycles)
        form.addRow("Échauffement", self.warmup)
        form.addRow("Temps limite", self.timeout_box)
        box = QGroupBox("Seuils du verdict")
        box.setLayout(thresholds)
        left = QVBoxLayout()
        left.addLayout(form)
        left.addWidget(box)
        row = QHBoxLayout()
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        left.addLayout(row)
        left.addStretch(1)
        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setMaximumWidth(400)

        top = QHBoxLayout()
        top.addWidget(self.verdict, 1)
        top.addWidget(QLabel("Courbe"))
        top.addWidget(self.series)
        right = QSplitter(Qt.Orientation.Vertical)
        tables = QSplitter()
        counts = QTabWidget()
        counts.addTab(self.table, "Tendances")
        counts.addTab(self.cycles_table, "Cycles")
        tables.addWidget(counts)
        tables.addWidget(self.allocations)
        tables.setSizes([700, 260])
        right.addWidget(self.curve)
        right.addWidget(tables)
        right.addWidget(self.log)
        right.setSizes([230, 170, 130])
        rightw = QWidget()
        lay = QVBoxLayout(rightw)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.evidence)
        lay.addWidget(right, 1)
        foot = QHBoxLayout()
        foot.addWidget(self.bar, 1)
        foot.addWidget(self.copy_button)
        lay.addWidget(self.status)
        lay.addLayout(foot)
        split = QSplitter()
        split.addWidget(leftw)
        split.addWidget(rightw)
        split.setSizes([360, 740])
        main = QHBoxLayout(self)
        main.addWidget(split)

    def controls(self):
        return [self.scenario, self.cycles, self.warmup, self.run_button, self.timeout_box, *self.limit_boxes.values()]

    def limits_json(self) -> str:
        return json.dumps({key: box.value() for key, box in self.limit_boxes.items()})

    def run_new(self):
        self.start(self.scenario.currentData(), self.cycles.value(), self.warmup.value(), self.limits_json())

    def start(self, name, cycles, warmup, limits):
        self.launch(["--scenario", name, "--cycles", str(cycles), "--warmup", str(warmup), "--limits", limits],
                    title="Mesure mémoire")

    def replay(self, record: dict):
        p = record.get("params", {})
        index = self.scenario.findData(p.get("scenario"))
        if index >= 0:
            self.scenario.setCurrentIndex(index)
        self.cycles.setValue(int(p.get("cycles", 30)))
        limits = (record.get("details") or {}).get("limits")
        for key, value in (limits or {}).items():
            if key in self.limit_boxes:
                self.limit_boxes[key].setValue(value)
        self.run_new()

    def show_record(self, record: dict):
        data = record.get("details") or {}
        rows = data.get("rows", [])
        self.curve.set_data(rows, data.get("warmup", 0))
        verdict = data.get("verdict", record.get("verdict", "?"))
        self.verdict.setText(f"{verdict.capitalize()}  ({data.get('measured', 0)} cycles mesurés, "
                             f"{data.get('warmup', 0)} d'échauffement, {seconds_text(data.get('seconds', 0))})")
        self.verdict.setStyleSheet(f"font-size: 12pt; font-weight: 600; color: {STATE_COLORS.get(verdict, theme.FG)};")
        self.evidence.setText("\n".join(data.get("evidence", [])) or data.get("error", "")[:600])
        metrics = data.get("metrics", {})
        self.table.setRowCount(len(metrics))
        flagged = " ".join(data.get("evidence", []))
        for row, (key, m) in enumerate(metrics.items()):
            label, unit, suffix = SERIES.get(key, (key, 1.0, ""))
            bad = label.split(" (")[0].lower() in flagged.lower()
            values = (label, f"{m['first'] / unit:.1f} {suffix}", f"{m['last'] / unit:.1f} {suffix}",
                      f"{m['slope'] / unit:+.3f} {suffix}", f"{m['r2']:.2f}", f"{m['net'] / unit:+.1f} {suffix}",
                      "croît" if bad else "stable")
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if col == 6:
                    cell.setForeground(QColor(theme.RED if bad else theme.GREEN))
                self.table.setItem(row, col, cell)
        self.cycles_table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            values = (r["cycle"] + 1, f"{r['private'] / 1048576:.1f}", f"{r['rss'] / 1048576:.1f}",
                      f"{r['heap'] / 1048576:.2f}", r["widgets"], r["qobjects"], r["threads"], r["handles"], r["children"])
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if r["cycle"] < data.get("warmup", 0):
                    cell.setForeground(QColor(theme.FG_MUTED))
                self.cycles_table.setItem(row, col, cell)
        allocs = data.get("allocations", [])
        self.allocations.setRowCount(len(allocs))
        for row, a in enumerate(allocs):
            for col, text in enumerate((a["where"], f"{a['size_diff'] / 1024:+.1f} Ko", f"{a['count_diff']:+d}")):
                self.allocations.setItem(row, col, QTableWidgetItem(text))

    def result_text(self) -> str:
        data = (self.record.get("details") or {}) if self.record else {}
        if not data:
            return super().result_text()
        return self.record.get("summary", "") + "\n\n" + memory.trend_text(data)

# --------------------------------------------------------------
# rapports
# ------------------------------------------------------------

def describe(record: dict) -> str:
    lines = [f"{KIND_NAMES.get(record.get('kind'), record.get('kind'))} : {record.get('title', '')}",
             f"Verdict : {record.get('verdict')}   Graine : {record.get('seed')}", f"Début : {record.get('started')}"]
    summary = record.get("summary")
    if summary:
        lines.append(str(summary))
    lines.append("Commande : " + " ".join(map(str, record.get("command", []))))
    lines.append("Versions : " + ", ".join(f"{k} {v}" for k, v in record.get("versions", {}).items()))
    lines.append("Paramètres : " + json.dumps(record.get("params", {}), ensure_ascii=False))
    details = record.get("details") or {}
    for entry in details.get("scenarios", []):
        lines.append(f"- {entry['name']}: {entry['status']} ({entry['cases']} cas)")
        lines += [f"    graine {f['seed']}: {f['message']}" for f in entry["failures"]]
    for f in details.get("failures", []):
        lines.append(f"- pas {f.get('step')} [{f.get('type')}] {f.get('message')}")
    for suite in details.get("suites", []):
        lines.append(f"- {suite['name']}: code {suite['code']} ({len(suite.get('tests', []))} tests)")
    return "\n".join(lines)


# --------------------------------------------------------
class ReportsPanel(QWidget):
    replay_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Date", "Type", "Graine", "Verdict", "Résumé"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 140)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 70)
        self.tree.setColumnWidth(3, 90)
        self.tree.currentItemChanged.connect(self.show_selected)
        self.tree.itemDoubleClicked.connect(lambda *_: self.show_selected(self.tree.currentItem()))
        self.detail = log_pane("Choisis un rapport pour voir son contenu.")
        self.refresh_button = QPushButton("Actualiser")
        self.refresh_button.clicked.connect(self.refresh)
        self.replay_button = QPushButton("Rejouer")
        self.replay_button.setToolTip("Relance la même séance avec les mêmes paramètres (un monkey rejoue ses pas).")
        self.replay_button.clicked.connect(self.replay)
        self.folder_button = QPushButton("Ouvrir le dossier")
        self.folder_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(RUNS))))
        self.copy_button = QPushButton("Copier")
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.detail.toPlainText()))
        self.open_button = QPushButton("Ouvrir le fichier")
        self.open_button.setToolTip("Ouvre le JSON du rapport choisi avec le programme associé.")
        self.open_button.clicked.connect(self.open_file)
        self.status = QLabel("")
        row = QHBoxLayout()
        for b in (self.refresh_button, self.replay_button, self.copy_button, self.open_button, self.folder_button):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.status)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setSizes([280, 300])
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(split, 1)
        self.refresh()

    def refresh(self):
        current = self.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) if self.tree.currentItem() else None
        self.tree.clear()
        for path in list_runs():
            try:
                record = load_run(path)
            except (OSError, ValueError):
                continue
            started = record.get("started", "")
            try:
                started = datetime.fromisoformat(started).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
            summary = record.get("summary", "")
            item = QTreeWidgetItem([started, KIND_NAMES.get(record.get("kind"), record.get("kind", "?")),
                                    str(record.get("seed") if record.get("seed") is not None else ""),
                                    str(record.get("verdict", "")), short(str(summary).splitlines()[0] if summary else "", 140)])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            colored(item, str(record.get("verdict", "")))
            item.setToolTip(4, str(summary))
            self.tree.addTopLevelItem(item)
            if str(path) == current:
                self.tree.setCurrentItem(item)
        self.status.setText(f"{self.tree.topLevelItemCount()} rapport(s) dans {RUNS.name}/")

    def selected_record(self) -> dict | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        path = item.data(0, Qt.ItemDataRole.UserRole)
        try:
            record = load_run(path)
        except (OSError, ValueError):
            return None
        record["_path"] = path
        return record

    def show_selected(self, _item=None, _previous=None):
        record = self.selected_record()
        if record is None:
            self.detail.clear()
            return
        self.detail.setPlainText(describe(record) + "\n\n" + json.dumps(
            {k: v for k, v in record.items() if k != "_path"}, indent=1, ensure_ascii=False, default=str)[:60000])

    def open_file(self):
        item = self.tree.currentItem()
        if item is None:
            self.status.setText("Choisis d'abord un rapport.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(0, Qt.ItemDataRole.UserRole)))

    def replay(self):
        record = self.selected_record()
        if record is None:
            self.status.setText("Choisis d'abord un rapport.")
            return
        self.replay_requested.emit(record)
