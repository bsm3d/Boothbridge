# A seeded random driver on the real windows of the application.
#
# Each step picks a window, a real widget in it and an action (click, type, key, shortcut, drag,
# sort, open a window, close it...). Every step is written down, so a run can be played again
# from its list and shrunk to a shorter list that still fails. Dialogs answer by themselves, files
# stay in a temp folder and the dangerous parts of the application are stubbed (see world.py)

from __future__ import annotations

import contextlib
import copy
import faulthandler
import math
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from djapp.ui import cuehistory
from PySide6.QtCore import (
    QMimeData,
    QPoint,
    QPointF,
    Qt,
    QTimer,
    qInstallMessageHandler,
)
from PySide6.QtGui import (
    QContextMenuEvent,
    QDragMoveEvent,
    QDropEvent,
    QInputMethodEvent,
    QKeySequence,
    QWheelEvent,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QHeaderView,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QScrollBar,
    QTextEdit,
    QTreeWidget,
    QWidget,
)

from . import header, jail, save_run, short
from .world import ODD_STRINGS, OTHER_KEYS, SHORTCUTS, Patches, World, pump

WINDOW_KINDS = [
    ("playlist", "Playlist"), ("cue", "Cue editor"), ("tag", "ID Tag"),
    ("browser", "Navigateur musical"), ("backup", "Backup"), ("options", "Options"), ("audiocheck", "Audio check"),
]

KIND_OF = {"MainWindow": "main", "PlaylistWindow": "playlist", "Timeline": "cue", "CueEdit": "cue",
           "TagEditor": "tag",
           "MusicBrowser": "browser", "BackupPanel": "backup",
           "Options": "options", "AudioCheck": "audiocheck", "FormatDialog": "blocked"}
BLOCKED_TEXT = ("format", "eject", "exit", "quit")
ACTION_KIND = {"backup": "backup", "preferences": "options", "verify audio files": "audiocheck",
               "music browser": "browser"}
# what the offscreen platform says about features it does not have, not about the application
OFFSCREEN_NOISE = ("This plugin does not support", "QTest::keyToAscii Unhandled key code")
IGNORED_CLASSES = {"QTipLabel", "QComboBoxPrivateContainer", "QMenu", "QSplashScreen"}
TEXT_POOL = ODD_STRINGS + ["Night Drive", "house", "120", "a b c", "Ça", "0:30", "Am", "8A", "Warm up", "x.wav"]

ACTIONS = [("click", 30), ("type", 12), ("key", 10), ("shortcut", 10), ("select", 9), ("dbl", 4), ("check", 5),
           ("combo", 3), ("value", 3), ("wave", 8), ("rows", 3), ("sort", 3), ("context", 2), ("menu", 5),
           ("open", 7), ("close", 3), ("cancel", 2), ("resize", 2)]


@dataclass
class Params:
    seed: int = 0
    steps: int = 300
    budget: float = 0.0
    windows: list = field(default_factory=lambda: [k for k, _ in WINDOW_KINDS])
    visible: bool = False
    delay: int = 0
    hang: float = 20.0
    settle: int = 2000
    warn_limit: int = 400
    max_windows: int = 60
    inject: str = ""


def digest(rng: random.Random) -> str:
    return f"{zlib.crc32(repr(rng.getstate()).encode()):08x}"


def class_of(w) -> str:
    return w.metaObject().className()


def visible_ok(w) -> bool:
    return w.isVisible() and w.isEnabled() and w.width() > 2 and w.height() > 2


# --------------------------------------------------------
class Monkey:
    def __init__(self, params: Params, world: World, replay: list | None = None):
        self.p = params
        self.world = world
        self.rng = random.Random(params.seed)
        self.replaying = replay is not None
        self.script = copy.deepcopy(replay) if replay is not None else []
        self.steps: list[dict] = []
        self.cur: dict = {}
        self.cur_index = -1
        self.depth = 0
        self.sub_count = 0
        self.failures: dict[str, dict] = {}
        self.warnings: dict[str, int] = {}
        self.warn_total = 0
        self.blocked_notes: list[str] = []
        self.counters: dict[str, int] = {}
        self.answer_index = 0
        self.modal_index = 0
        self.stats = {"actions": {}, "skipped": 0, "max_windows": 0, "max_threads": 0, "dirty_steps": 0,
                      "cue_edits": 0, "jobs": 0}
        self.beat_t = time.monotonic()
        self.hang_flag = False
        self.done = False
        self.dump_path = world.root.parent / "hang.txt"
        self.dump_fh = None
        self.clean_fp = None
        self.clean_ident = None
        self.prev_dirty = True
        self.started = time.monotonic()
        self.base_threads = 0
        self.allowed = set(params.windows) | {"main", "other"}
        self.stopped = ""
        self.closing = False

    # ---- bookkeeping

    def beat(self):
        self.beat_t = time.monotonic()
        self.hang_flag = False
        if self.dump_fh is not None:
            # faulthandler's own thread dumps the stacks even when the GIL is stuck in native code
            faulthandler.dump_traceback_later(self.p.hang, file=self.dump_fh)

    def counter(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    def note_blocked(self, text: str):
        if len(self.blocked_notes) < 100:
            self.blocked_notes.append(text)

    def action_blocked(self, text: str) -> bool:
        low = re.sub(r"[&.]", "", text).strip().lower()
        if any(low.startswith(b) or f" {b}" in low for b in BLOCKED_TEXT):
            return True
        kind = next((k for t, k in ACTION_KIND.items() if low.startswith(t)), "")
        return bool(kind) and kind not in self.allowed

    def fail(self, kind: str, message: str, stack: str = "", sig: str = ""):
        key = sig or f"{kind}:{re.sub(r'[0-9]+', 'N', message.splitlines()[0] if message else '')[:100]}"
        known = self.failures.get(key)
        if known:
            known["count"] += 1
            return
        if len(self.failures) >= 25:
            return
        self.failures[key] = {"type": kind, "step": self.cur_index, "message": short(message, 400), "stack": stack,
                              "sig": key, "count": 1, "action": self.cur.get("act", ""),
                              "label": self.cur.get("label", "")}

    # ---- hooks

    def on_exception(self, etype, value, tb):
        stack = "".join(traceback.format_exception(etype, value, tb))
        frame = traceback.extract_tb(tb)[-1] if tb else None
        where = f"{Path(frame.filename).name}:{frame.name}" if frame else ""
        self.fail("exception", f"{etype.__name__}: {value}", stack, f"exception:{etype.__name__}:{where}")

    def on_thread_exception(self, args):
        self.on_exception(args.exc_type, args.exc_value, args.exc_traceback)

    def on_unraisable(self, args):
        self.on_exception(args.exc_type or RuntimeError, args.exc_value, args.exc_traceback)

    def on_qt_message(self, mode, context, message):
        name = getattr(mode, "name", str(mode))
        if name in ("QtWarningMsg", "QtCriticalMsg", "QtFatalMsg") and not message.startswith(OFFSCREEN_NOISE):
            self.warn_total += 1
            key = message[:160]
            self.warnings[key] = self.warnings.get(key, 0) + 1

    def install_hooks(self):
        self.old = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        sys.excepthook = self.on_exception
        threading.excepthook = self.on_thread_exception
        sys.unraisablehook = self.on_unraisable
        qInstallMessageHandler(self.on_qt_message)

    def remove_hooks(self):
        if not hasattr(self, "old"):
            return
        sys.excepthook, threading.excepthook, sys.unraisablehook = self.old
        qInstallMessageHandler(None)

    # ---- answers of the patched dialogs

    # index of the answer, recorded in the current step and read back when replaying
    def choose(self, kind: str, title: str, n: int, favor: bool = False) -> int:
        n = max(1, n)
        if self.closing:
            return 0
        if self.replaying:
            queue = self.cur.get("answers", [])
            if self.answer_index < len(queue):
                value = queue[self.answer_index][2]
                self.answer_index += 1
                return min(value, n - 1)
            self.answer_index += 1
            return 0
        rng = random.Random(f"{self.p.seed}:{kind}:{title}:{self.counter(kind + title)}")
        index = 0 if favor and rng.random() < 0.6 else rng.randrange(n)
        self.cur.setdefault("answers", []).append([kind, short(title, 40), index])
        return index

    # stands for QDialog.exec: shows the dialog, plays a few steps in it, then closes it
    def exec_dialog(self, dialog) -> int:
        kind = KIND_OF.get(type(dialog).__name__, "other")
        if kind == "blocked" or (kind != "other" and kind not in self.allowed) or self.depth >= 3:
            self.note_blocked(f"dialog {type(dialog).__name__} not opened")
            with contextlib.suppress(Exception):
                dialog.reject()
            return 0
        self.depth += 1
        try:
            if self.replaying:
                entries = self.cur.get("modal", [])
                entry = entries[self.modal_index] if self.modal_index < len(entries) else {"steps": [], "end": "reject"}
                self.modal_index += 1
            else:
                entry = {"dialog": type(dialog).__name__, "steps": [], "end": ""}
                self.cur.setdefault("modal", []).append(entry)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            pump(10)
            saved = (self.cur, self.answer_index, self.modal_index)
            wanted = len(entry["steps"]) if self.replaying else self.rng.randint(0, 5)
            for i in range(wanted):
                if not dialog.isVisible():
                    break
                if self.replaying:
                    step = entry["steps"][i]
                else:
                    step = self.plan([dialog])
                    if step is None:
                        break
                    entry["steps"].append(step)
                self.sub_count += 1
                self.run_step(step, sub=True)
            self.cur, self.answer_index, self.modal_index = saved
            end = entry["end"] if self.replaying else self.rng.choice(["accept", "reject", "reject", "escape"])
            entry["end"] = end
            if dialog.isVisible():
                if end == "accept":
                    dialog.accept()
                elif end == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                # a dialog that is busy refuses to close, like the real exec() it waits for it
                for _ in range(100):
                    if not dialog.isVisible():
                        break
                    self.beat()
                    dialog.reject()
                    pump(50)
                if dialog.isVisible():
                    self.note_blocked(f"{type(dialog).__name__} would not close, hidden by force")
                    dialog.hide()
            pump(10)
        finally:
            self.depth -= 1
        return int(dialog.result())

    # ---- windows and widgets

    def windows(self) -> list[QWidget]:
        out = []
        for w in QApplication.topLevelWidgets():
            if not w.isVisible() or not w.isWindow() or isinstance(w, QMenu) or class_of(w) in IGNORED_CLASSES:
                continue
            if KIND_OF.get(type(w).__name__, "other") in self.allowed or type(w).__name__ not in KIND_OF:
                out.append(w)
        out.sort(key=lambda w: (type(w).__name__, w.windowTitle()))
        return out

    def window_id(self, w, scope) -> str:
        name = type(w).__name__
        same = [x for x in scope if type(x).__name__ == name]
        return f"{name}#{same.index(w)}"

    def find_window(self, ident: str, scope=None):
        name, _, index = ident.partition("#")
        same = [w for w in (scope or self.windows()) if type(w).__name__ == name]
        return same[int(index)] if int(index) < len(same) else None

    def path_to(self, w, win) -> str | None:
        parts = []
        cur = w
        while cur is not win:
            parent = cur.parentWidget()
            if parent is None:
                return None
            cls = class_of(cur)
            same = [c for c in parent.children() if isinstance(c, QWidget) and class_of(c) == cls]
            parts.append(f"{cls}[{same.index(cur)}]")
            cur = parent
        return "/".join(reversed(parts))

    def find_widget(self, win, path: str):
        cur = win
        for part in path.split("/"):
            cls, _, rest = part.partition("[")
            index = int(rest.rstrip("]"))
            same = [c for c in cur.children() if isinstance(c, QWidget) and class_of(c) == cls]
            if index >= len(same):
                return None
            cur = same[index]
        return cur

    def label_of(self, w) -> str:
        for getter in ("text", "placeholderText", "accessibleName", "objectName", "toolTip", "windowTitle"):
            fn = getattr(w, getter, None)
            if callable(fn):
                with contextlib.suppress(Exception):
                    text = fn()
                    if isinstance(text, str) and text:
                        return short(text, 40)
        return class_of(w)

    def candidates(self, win) -> dict[str, list]:
        found = {"button": [], "combo": [], "edit": [], "value": [], "view": [], "wave": [], "all": []}
        try:
            from djapp.ui.zoomwave import ZoomWave
        except ImportError:
            ZoomWave = ()
        for w in win.findChildren(QWidget):
            if not visible_ok(w) or isinstance(w, QScrollBar):
                continue
            if ZoomWave and isinstance(w, ZoomWave):
                found["wave"].append(w)
            elif isinstance(w, QAbstractButton):
                if not any(b in self.label_of(w).lower() for b in BLOCKED_TEXT):
                    found["button"].append(w)
            elif isinstance(w, QComboBox):
                found["combo"].append(w)
            elif isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit)):
                if not w.isReadOnly():
                    found["edit"].append(w)
            elif isinstance(w, (QAbstractSpinBox, QAbstractSlider)):
                found["value"].append(w)
            elif isinstance(w, QAbstractItemView) and not isinstance(w, QHeaderView):
                found["view"].append(w)
            found["all"].append(w)
        return found

    def action_list(self, main) -> list[str]:
        out = []

        def walk(menu, prefix):
            for a in menu.actions():
                text = re.sub(r"&", "", a.text())
                if a.menu() is not None:
                    walk(a.menu(), f"{prefix}{text}/")
                elif not a.isSeparator() and a.isEnabled():
                    out.append(f"{prefix}{text}")

        for a in main.menuBar().actions():
            if a.menu() is not None:
                walk(a.menu(), re.sub(r"&", "", a.text()) + "/")
        return [x for x in out if not self.action_blocked(x.rsplit("/", 1)[-1])]

    def find_menu_action(self, main, path: str):
        def walk(menu, prefix):
            for a in menu.actions():
                text = re.sub(r"&", "", a.text())
                if a.menu() is not None:
                    found = walk(a.menu(), f"{prefix}{text}/")
                    if found:
                        return found
                elif f"{prefix}{text}" == path:
                    return a
            return None

        for a in main.menuBar().actions():
            if a.menu() is not None:
                found = walk(a.menu(), re.sub(r"&", "", a.text()) + "/")
                if found:
                    return found
        return None

    # ---- planning

    def plan(self, scope: list | None = None) -> dict | None:
        wins = scope if scope is not None else self.windows()
        if not wins:
            return None
        weights = [3 if type(w).__name__ == "MainWindow" else 2 for w in wins]
        win = self.rng.choices(wins, weights)[0]
        names = [a for a, _ in ACTIONS]
        weights = [x for _, x in ACTIONS]
        if scope is not None:
            weights = [0 if a in ("open", "close", "menu", "resize") else x for a, x in zip(names, weights, strict=True)]
        else:
            # few windows: open more of them, many windows: close some
            crowd = {"open": 4 if len(wins) < 3 else 1, "close": 3 if len(wins) > 5 else 1}
            weights = [x * crowd.get(a, 1) for a, x in zip(names, weights, strict=True)]
        for _ in range(6):
            act = self.rng.choices(names, weights)[0]
            step = self.plan_action(act, win, wins)
            if step is not None:
                step["win"] = self.window_id(step.pop("_win", win), wins)
                step["rng"] = digest(self.rng)
                return step
        return {"act": "key", "win": self.window_id(win, wins), "arg": {"key": "Escape"}, "rng": digest(self.rng)}

    def pick(self, items):
        return items[self.rng.randrange(len(items))] if items else None

    def target(self, win, w) -> dict:
        path = self.path_to(w, win)
        return {"path": path or "", "label": self.label_of(w)}

    def plan_action(self, act: str, win, wins):
        cand = self.candidates(win)
        step: dict = {"act": act}
        rng = self.rng
        if act == "click":
            w = self.pick(cand["button"] or cand["all"])
            if w is None:
                return None
            step.update(self.target(win, w))
            step["arg"] = {"button": "right" if rng.random() < 0.08 else "left"}
        elif act == "type":
            w = self.pick(cand["edit"])
            if w is None:
                return None
            step.update(self.target(win, w))
            step["arg"] = {"text": rng.randrange(len(TEXT_POOL)), "clear": rng.random() < 0.6, "enter": rng.random() < 0.25}
        elif act in ("key", "shortcut"):
            w = self.pick(cand["all"]) if rng.random() < 0.7 else None
            if w is not None:
                step.update(self.target(win, w))
            step["arg"] = {"key": rng.choice(SHORTCUTS if act == "shortcut" else OTHER_KEYS)}
        elif act in ("select", "dbl", "check", "sort", "rows", "context"):
            w = self.pick(cand["view"])
            if w is None:
                return None
            step.update(self.target(win, w))
            step["arg"] = self.plan_item(act, w)
        elif act == "combo":
            w = self.pick(cand["combo"])
            if w is None or w.count() == 0:
                return None
            step.update(self.target(win, w))
            step["arg"] = {"index": rng.randrange(w.count())}
        elif act == "value":
            w = self.pick(cand["value"])
            if w is None:
                return None
            step.update(self.target(win, w))
            step["arg"] = {"fraction": rng.choice([0.0, 1.0, rng.random(), rng.random()])}
        elif act == "wave":
            w = self.pick(cand["wave"])
            if w is None:
                return None
            step.update(self.target(win, w))
            points = [(rng.random(), rng.random()) for _ in range(rng.randint(2, 6))]
            step["arg"] = {"mode": rng.choice(["drag", "drag", "click", "double", "right", "wheel"]),
                           "points": points, "shift": rng.random() < 0.2, "delta": rng.choice([-240, -120, 120, 240])}
        elif act == "menu":
            main = next((x for x in wins if type(x).__name__ == "MainWindow"), None)
            if main is None:
                return None
            options = self.action_list(main)
            if not options:
                return None
            step["arg"] = {"path": rng.choice(options)}
            step["label"] = step["arg"]["path"]
        elif act == "open":
            step["arg"] = {"kind": rng.choice(["playlist", "playlist", "cue", "tag",
                                                "browser", "backup",
                                                "options", "audiocheck"]), "pick": rng.random()}
            if step["arg"]["kind"] not in self.allowed:
                return None
            step["label"] = step["arg"]["kind"]
        elif act == "close":
            others = [w for w in wins if type(w).__name__ != "MainWindow"]
            if not others:
                return None
            step["_win"] = rng.choice(others)
            step["arg"] = {"how": rng.choice(["close", "escape"])}
        elif act == "cancel":
            words = ("cancel", "stop", "annuler")
            options = [w for w in cand["button"] if any(x in self.label_of(w).lower() for x in words)]
            w = self.pick(options)
            if w is None:
                return None
            step.update(self.target(win, w))
        elif act == "resize":
            step["arg"] = {"size": [rng.randint(300, 1500), rng.randint(250, 1000)]}
        return step

    def plan_item(self, act: str, view) -> dict:
        rng = self.rng
        arg: dict = {}
        if isinstance(view, QTreeWidget):
            rows = self.flat_rows(view)
            if rows:
                arg["row"] = rng.choice(rows)
                if act == "rows":
                    arg["to"] = rng.choice(rows)
        else:
            model = view.model()
            count = model.rowCount() if model is not None else 0
            if count:
                arg["row"] = [rng.randrange(count)]
                arg["to"] = [rng.randrange(count)]
        arg["col"] = rng.randrange(max(1, view.model().columnCount() if view.model() else 1))
        arg["mods"] = rng.choice(["", "", "", "ctrl", "shift"])
        arg["section"] = arg["col"]
        arg["checked"] = rng.random() < 0.5
        return arg

    def flat_rows(self, tree, limit: int = 300) -> list[list[int]]:
        out: list[list[int]] = []

        def walk(item, path):
            for i in range(item.childCount()):
                child = item.child(i)
                out.append([*path, i])
                if child.isExpanded() and len(out) < limit:
                    walk(child, [*path, i])

        walk(tree.invisibleRootItem(), [])
        return out[:limit]

    def item_at(self, view, row):
        if isinstance(view, QTreeWidget):
            item = view.invisibleRootItem()
            for i in row:
                if i >= item.childCount():
                    return None
                item = item.child(i)
            return item
        return None

    def index_at(self, view, row, col=0):
        model = view.model()
        if model is None or not row or row[0] >= model.rowCount():
            return None
        return model.index(row[0], min(col, model.columnCount() - 1))

    def point_of(self, view, arg):
        row = arg.get("row")
        if row is None:
            return None
        if isinstance(view, QTreeWidget):
            item = self.item_at(view, row)
            if item is None:
                return None
            rect = view.visualItemRect(item)
        else:
            index = self.index_at(view, row, arg.get("col", 0))
            if index is None:
                return None
            rect = view.visualRect(index)
        if rect.isEmpty() or not view.viewport().rect().intersects(rect):
            return None
        return rect.center()

    # ---- execution

    def run_step(self, st: dict, sub: bool = False):
        previous = (self.cur, self.cur_index, self.answer_index, self.modal_index)
        if not sub:
            self.cur_index = len(self.steps)
            st["i"] = self.cur_index
            self.steps.append(st)
        self.answer_index = self.modal_index = 0
        self.cur = st
        self.beat()
        started = time.monotonic()
        try:
            self.perform(st)
        except Exception as error:  # whatever the step raises is the finding  # noqa: BLE001
            self.fail("exception", f"{type(error).__name__}: {error}", traceback.format_exc(),
                      f"exception:{type(error).__name__}:{short(error, 60)}")
        st["ms"] = round((time.monotonic() - started) * 1000)
        self.stats["actions"][st.get("act", "?")] = self.stats["actions"].get(st.get("act", "?"), 0) + 1
        if sub:
            self.cur, self.cur_index, self.answer_index, self.modal_index = previous
            return
        self.after_step()

    def perform(self, st: dict):
        act = st["act"]
        if self.p.inject and act == self.p.inject:
            raise RuntimeError("injected failure")
        if self.p.inject == f"stall:{act}":
            time.sleep(self.p.hang * 1.6)  # a frozen window, for the watchdog tests
        scope = self.windows() if not self.depth else [w for w in QApplication.topLevelWidgets() if w.isVisible() and isinstance(w, QDialog)]
        win = self.find_window(st.get("win", ""), scope) if st.get("win") else None
        if win is None:
            win = scope[0] if scope else None
        if win is None:
            st["skipped"] = "window"
            self.stats["skipped"] += 1
            return
        w = win
        if st.get("path"):
            w = self.find_widget(win, st["path"])
            if w is None:
                st["skipped"] = "widget"
                self.stats["skipped"] += 1
                return
        if act != "close" and act != "resize":
            win.activateWindow()
        getattr(self, "do_" + act)(st, win, w)

    def do_click(self, st, win, w):
        if not w.isVisible() or not w.isEnabled():
            st["skipped"] = "disabled"
            return
        button = Qt.MouseButton.RightButton if st.get("arg", {}).get("button") == "right" else Qt.MouseButton.LeftButton
        if button == Qt.MouseButton.RightButton and w.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu:
            w.customContextMenuRequested.emit(QPoint(w.width() // 2, w.height() // 2))
            return
        QTest.mouseClick(w, button)

    def do_type(self, st, win, w):
        arg = st["arg"]
        text = TEXT_POOL[arg["text"] % len(TEXT_POOL)]
        w.setFocus()
        if arg["clear"] and isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit)):
            w.selectAll()
        event = QInputMethodEvent()
        event.setCommitString(text)
        QApplication.sendEvent(w, event)
        if arg["enter"]:
            QTest.keyClick(w, Qt.Key.Key_Return)
        # a window that filters after a pause in the typing (the music browser) must have done so before
        # the next step, or the rows that step finds depend on how fast the machine is
        pause = getattr(w.window(), "typing", None)
        while isinstance(pause, QTimer) and pause.isActive():
            QTest.qWait(20)

    def do_key(self, st, win, w):
        combo = QKeySequence(st["arg"]["key"])[0]
        if st.get("path") and w.focusPolicy() != Qt.FocusPolicy.NoFocus:
            w.setFocus()
        target = QApplication.focusWidget() or w
        QTest.keyClick(target, combo.key(), combo.keyboardModifiers())

    do_shortcut = do_key

    def do_select(self, st, win, w):
        arg = st["arg"]
        point = self.point_of(w, arg)
        if point is None:
            st["skipped"] = "row"
            return
        mods = {"ctrl": Qt.KeyboardModifier.ControlModifier, "shift": Qt.KeyboardModifier.ShiftModifier}.get(
            arg.get("mods", ""), Qt.KeyboardModifier.NoModifier)
        QTest.mouseClick(w.viewport(), Qt.MouseButton.LeftButton, mods, point)

    def do_dbl(self, st, win, w):
        point = self.point_of(w, st["arg"])
        if point is None:
            st["skipped"] = "row"
            return
        QTest.mouseDClick(w.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)

    def do_check(self, st, win, w):
        arg = st["arg"]
        item = self.item_at(w, arg.get("row", [])) if isinstance(w, QTreeWidget) else None
        if item is None or not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            st["skipped"] = "check"
            return
        item.setCheckState(0, Qt.CheckState.Checked if arg["checked"] else Qt.CheckState.Unchecked)

    def do_combo(self, st, win, w):
        w.setCurrentIndex(min(st["arg"]["index"], w.count() - 1))

    def do_value(self, st, win, w):
        f = st["arg"]["fraction"]
        if isinstance(w, QAbstractSlider):
            w.setValue(round(w.minimum() + (w.maximum() - w.minimum()) * f))
        elif hasattr(w, "minimum"):
            low, high = w.minimum(), w.maximum()
            if math.isfinite(low) and math.isfinite(high) and high > low:
                w.setValue(type(low)(low + (high - low) * f))

    def do_wave(self, st, win, w):
        arg = st["arg"]
        pts = [QPoint(round(x * (w.width() - 1)), round(y * (w.height() - 1))) for x, y in arg["points"]]
        mods = Qt.KeyboardModifier.ShiftModifier if arg["shift"] else Qt.KeyboardModifier.NoModifier
        left = Qt.MouseButton.LeftButton
        mode = arg["mode"]
        if mode == "drag":
            QTest.mousePress(w, left, mods, pts[0])
            for p in pts[1:]:
                QTest.mouseMove(w, p)
            QTest.mouseRelease(w, left, mods, pts[-1])
        elif mode == "click":
            QTest.mouseClick(w, left, mods, pts[0])
        elif mode == "double":
            QTest.mouseDClick(w, left, mods, pts[0])
        elif mode == "right":
            QTest.mouseClick(w, Qt.MouseButton.RightButton, mods, pts[0])
        else:
            pos = QPointF(pts[0])
            event = QWheelEvent(pos, w.mapToGlobal(pos), QPoint(0, 0), QPoint(0, arg["delta"]), Qt.MouseButton.NoButton,
                                mods, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(w, event)

    # moves rows the way a drop does, without the blocking native drag loop
    def do_rows(self, st, win, w):
        arg = st["arg"]
        src, dst = self.point_of(w, arg), self.point_of(w, {**arg, "row": arg.get("to"), "col": 0})
        if src is None or dst is None:
            st["skipped"] = "row"
            return
        QTest.mouseClick(w.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, src)
        indexes = w.selectedIndexes()
        mime = w.model().mimeData(indexes) if indexes else QMimeData()
        actions = Qt.DropAction.MoveAction
        for event in (QDragMoveEvent(dst, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
                      QDropEvent(QPointF(dst), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)):
            QApplication.sendEvent(w.viewport(), event)

    def do_sort(self, st, win, w):
        head = w.header() if hasattr(w, "header") else None
        if not isinstance(head, QHeaderView) or head.count() == 0:
            st["skipped"] = "header"
            return
        section = st["arg"]["section"] % head.count()
        x = head.sectionViewportPosition(section) + head.sectionSize(section) // 2
        QTest.mouseClick(head.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, head.height() // 2))

    def do_context(self, st, win, w):
        point = self.point_of(w, st["arg"]) or QPoint(5, 5)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, w.viewport().mapToGlobal(point))
        QApplication.sendEvent(w.viewport(), event)

    def do_menu(self, st, win, w):
        main = self.world.main
        action = self.find_menu_action(main, st["arg"]["path"])
        if action is None or not action.isEnabled():
            st["skipped"] = "menu"
            return
        action.trigger()

    def do_open(self, st, win, w):
        kind = st["arg"]["kind"]
        main = self.world.main
        pick = st["arg"]["pick"]
        if kind == "playlist":
            rows = [r for r in self.flat_rows(main.tree) if self.item_at(main.tree, r).data(0, Qt.ItemDataRole.UserRole)
                    and not self.item_at(main.tree, r).data(0, Qt.ItemDataRole.UserRole).is_folder]
            if not rows:
                st["skipped"] = "playlist"
                return
            item = self.item_at(main.tree, rows[int(pick * len(rows)) % len(rows)])
            point = main.tree.visualItemRect(item).center()
            QTest.mouseDClick(main.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
            return
        if kind in ("cue", "tag", "smart"):
            from djapp.ui.details import C_CUES, PlaylistWindow
            wins = [x for x in self.windows() if isinstance(x, PlaylistWindow)]
            if not wins:
                st["skipped"] = "no playlist window"
                return
            pw = wins[int(pick * len(wins)) % len(wins)]
            count = pw.table.topLevelItemCount()
            if count:
                item = pw.table.topLevelItem(int(pick * 1000) % count)
                pw.table.setCurrentItem(item)
                if kind == "cue":
                    pw.double(item, C_CUES)
                else:
                    pw.edit_current_tags()
            return
        text = {"browser": "View/Music browser", "backup": "Tools/Backup", "options": "Options/Preferences...",
                "audiocheck": "Tools/Verify audio files..."}[kind]
        action = self.find_menu_action(main, text)
        if action is None:
            st["skipped"] = "menu"
            return
        action.trigger()

    def do_close(self, st, win, w):
        if type(win).__name__ == "MainWindow":
            st["skipped"] = "main"
            return
        if st["arg"]["how"] == "escape":
            QTest.keyClick(win, Qt.Key.Key_Escape)
        else:
            win.close()

    def do_cancel(self, st, win, w):
        QTest.mouseClick(w, Qt.MouseButton.LeftButton)

    def do_resize(self, st, win, w):
        size = st["arg"]["size"]
        win.resize(size[0], size[1])

    # ---- after each step

    def after_step(self):
        self.close_popups()
        end = time.monotonic() + self.p.settle / 1000
        pump(self.p.delay)
        while self.world.busy() and time.monotonic() < end:
            self.beat()
            pump(15)
        self.close_popups()
        self.beat()
        self.check()

    def close_popups(self):
        for _ in range(3):
            popup = QApplication.activePopupWidget()
            if popup is None:
                return
            if isinstance(popup, QMenu):
                actions = [a for a in popup.actions() if a.isEnabled() and not a.isSeparator() and a.menu() is None]
                index = self.choose("popup", "popup", len(actions) + 1)
                if index < len(actions) and not self.action_blocked(actions[index].text()):
                    actions[index].trigger()
            popup.close()
            pump(5)

    def fingerprint(self) -> str:
        from djapp.sources import project
        doc = project.dump(self.world.main.nodes, {})
        doc.pop("saved", None)
        return str(zlib.crc32(repr(doc).encode()))

    def check(self):
        main = self.world.main
        windows = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
        threads = threading.active_count()
        self.stats["max_windows"] = max(self.stats["max_windows"], len(windows))
        self.stats["max_threads"] = max(self.stats["max_threads"], threads)
        self.stats["dirty_steps"] += bool(main.dirty)
        self.stats["cue_edits"] = sum(cuehistory._edits.values())
        if len(windows) > self.p.max_windows:
            self.fail("invariant", f"{len(windows)} windows are open", sig="invariant:windows")
        if threads > self.base_threads + 80:
            self.fail("invariant", f"{threads} Python threads alive (started with {self.base_threads})",
                      sig="invariant:threads")
        if self.warn_total > self.p.warn_limit:
            top = sorted(self.warnings.items(), key=lambda kv: -kv[1])[:3]
            self.fail("warnings", f"{self.warn_total} Qt warnings: " + " | ".join(f"{n}x {m}" for m, n in top),
                      sig="warnings:flood")
        title_star = main.windowTitle().endswith(" *")
        if title_star != main.dirty:
            self.fail("invariant", f"dirty flag is {main.dirty} but the title is {main.windowTitle()!r}",
                      sig="invariant:dirty-title")
        locked = not main.go.isEnabled() or main.stop.isEnabled() or not main.tree.isEnabled()
        if locked and not self.world.busy():
            self.fail("invariant", "no job is running but Convert/Cancel/the tree are still locked",
                      sig="invariant:buttons-locked")
        try:
            ident = (id(main.nodes), main.project)
            fp = self.fingerprint()
        except Exception as error:  # a dump that fails on loaded data is a finding too  # noqa: BLE001
            self.fail("exception", f"project.dump failed on the loaded playlists: {type(error).__name__}: {error}",
                      traceback.format_exc(), "exception:dump")
            return
        if not main.dirty:
            if (not self.prev_dirty and self.clean_ident == ident and self.clean_fp is not None and fp != self.clean_fp):
                self.fail("invariant", "the playlists changed while the project is marked as saved",
                          sig="invariant:clean-but-modified")
            self.clean_fp, self.clean_ident = fp, ident
        self.prev_dirty = main.dirty

    # ---- watchdog

    def watchdog(self, emergency):
        while not self.done:
            time.sleep(0.25)
            idle = time.monotonic() - self.beat_t
            if idle > self.p.hang and not self.hang_flag:
                self.hang_flag = True
                names = {t.ident: t.name for t in threading.enumerate()}
                stacks = "\n".join(f"-- thread {names.get(i, i)}\n" + "".join(traceback.format_stack(frame))
                                   for i, frame in sys._current_frames().items())
                with contextlib.suppress(OSError), open(self.dump_path, encoding="utf-8", errors="replace") as f:
                    stacks += "\n-- faulthandler\n" + f.read()[-4000:]
                self.fail("hang", f"no progress for {idle:.0f} s during step {self.cur_index} ({self.cur.get('act')} "
                          f"{self.cur.get('label', '')})", stacks, "hang")
            if idle > self.p.hang * 3:
                self.stopped = "blocage"
                emergency()
                os._exit(3)

    # ---- running

    def start(self, emergency):
        with jail.trusted():
            self.dump_fh = open(self.dump_path, "w")  # kept open for faulthandler  # noqa: SIM115
        self.install_hooks()
        self.base_threads = threading.active_count()
        self.thread = threading.Thread(target=self.watchdog, args=(emergency,), daemon=True)
        self.thread.start()

    def finish(self):
        self.done = True
        faulthandler.cancel_dump_traceback_later()
        if self.dump_fh is not None:
            self.dump_fh.close()
            self.dump_fh = None
        self.remove_hooks()

    def run(self):
        self.started = time.monotonic()
        total = len(self.script) if self.replaying else self.p.steps
        for i in range(total):
            if self.p.budget and time.monotonic() - self.started > self.p.budget:
                self.stopped = "budget"
                break
            if self.replaying:
                self.run_step(self.script[i])
            else:
                step = self.plan()
                if step is None:
                    self.stopped = "no window"
                    break
                self.run_step(step)
        return self

    def result(self, title="Monkey") -> dict:
        ordered = sorted(self.failures.values(), key=lambda f: f["step"])
        top = sorted(self.warnings.items(), key=lambda kv: -kv[1])[:15]
        steps = [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.steps]
        data = {"steps": steps, "done": len(self.steps), "sub_steps": self.sub_count, "failures": ordered,
                "warnings": {"total": self.warn_total, "top": top}, "blocked": self.blocked_notes + jail.blocked(),
                "stats": self.stats, "stopped": self.stopped,
                "seconds": round(time.monotonic() - self.started, 2),
                "verdict": "echec" if ordered else "ok"}
        data["summary"] = (f"graine {self.p.seed}, {len(self.steps)} pas, {len(ordered)} problème(s), "
                           f"{self.warn_total} avertissement(s) Qt")
        return data


# ------------------------------------------------------------
# runs
# ------------------------------------------------------------

# ------------------------------------------------------------
# A complete run in a fresh world. The world is closed again whatever happens
def one_run(root: Path, params: Params, replay: list | None = None, emergency=None) -> dict:
    QApplication.instance() or QApplication(sys.argv[:1])
    world = World(root, params.seed)
    monkey = Monkey(params, world, replay)
    monkey.dump_path = Path(root) / f"hang-{world.count}.txt"
    with Patches(monkey):
        try:
            world.start()
            pump(100)
            monkey.start(emergency or (lambda: None))
            monkey.run()
            data = monkey.result()
        finally:
            # closing asks questions too (unsaved edits), the patches must still answer them
            monkey.closing = True
            monkey.cur = {"act": "fermeture"}
            monkey.beat()
            world.stop()
            monkey.finish()
    late = monkey.result()["failures"]
    data["failures"] = late
    data["verdict"] = "echec" if late else "ok"
    return data


def first_signature(data: dict) -> str:
    return data["failures"][0]["sig"] if data["failures"] else ""


# delta debugging: drops chunks of steps while the same failure shows up
def reduce_steps(root: Path, params: Params, steps: list, signature: str, limit_s: float = 120.0, log=None) -> tuple[list, int]:
    log = log or (lambda text: None)
    started = time.monotonic()
    attempts = 0

    def fails(subset) -> bool:
        nonlocal attempts
        attempts += 1
        data = one_run(root, params, replay=[dict(s) for s in subset])
        return any(f["sig"] == signature for f in data["failures"])

    current = list(steps)
    n = 2
    while len(current) >= 2 and time.monotonic() - started < limit_s:
        size = max(1, math.ceil(len(current) / n))
        chunks = [current[i:i + size] for i in range(0, len(current), size)]
        reduced = False
        for i in range(len(chunks)):
            rest = [s for j, c in enumerate(chunks) if j != i for s in c]
            if rest and fails(rest):
                log(f"  {len(current)} -> {len(rest)} pas")
                current, n, reduced = rest, max(n - 1, 2), True
                break
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current, attempts


def renumber(steps: list) -> list:
    for i, s in enumerate(steps):
        s["i"] = i
    return steps


# ------------------------------------------------------------
# command line
# ------------------------------------------------------------

# ------------------------------------------------------------
def add_arguments(ap):
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--budget", type=float, default=0.0)
    ap.add_argument("--windows", default="all")
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--delay", type=int, default=0)
    ap.add_argument("--hang", type=float, default=20.0)
    ap.add_argument("--settle", type=int, default=2000)
    ap.add_argument("--replay", default="")
    ap.add_argument("--reduce", default="")
    ap.add_argument("--inject", default="")


# ------------------------------------------------------------
def params_from(args) -> Params:
    kinds = [k for k, _ in WINDOW_KINDS]
    windows = kinds if args.windows == "all" else [w for w in args.windows.split(",") if w in kinds]
    return Params(seed=args.seed, steps=args.steps, budget=args.budget, windows=windows, visible=args.visible,
                  delay=args.delay, hang=args.hang, settle=args.settle, inject=args.inject)


# ------------------------------------------------------------
# child process entry: plays the run (or the replay, or the reduction) and writes the record
def run_cli(args, command: list[str], out) -> int:
    from . import load_run
    if not args.visible:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        if os.name == "nt":
            os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "."), "Fonts"))
    from . import world as world_module
    root = Path(tempfile.mkdtemp(prefix="tbmonkey-"))
    params = params_from(args)
    record = header("monkey", "Monkey", command, asdict(params), params.seed)
    replay = None
    reduce_from = None
    if args.replay or args.reduce:
        previous = load_run(args.replay or args.reduce)
        saved = previous.get("params", {})
        params = Params(**{**asdict(params), **{k: saved[k] for k in ("seed", "windows", "hang", "settle", "inject") if k in saved}})
        params.visible = args.visible
        params.delay = args.delay
        record["params"] = asdict(params)
        record["seed"] = params.seed
        replay = previous["details"]["steps"] if "details" in previous else previous["steps"]
        reduce_from = bool(args.reduce)
    world_module.process_setup(root)

    def emergency():
        record["verdict"] = "blocage"
        record["summary"] = "la fenêtre ne répond plus, processus arrêté"
        save_run(out, record)

    code = 2
    try:
        if reduce_from:
            first = one_run(root, params, replay=[dict(s) for s in replay], emergency=emergency)
            signature = first_signature(first)
            if not signature:
                record.update(verdict="ok", summary="le rejeu ne reproduit aucun échec, rien à réduire", details=first)
                code = 0
            else:
                failing = first["failures"][0]["step"]
                head = [dict(s) for s in replay[:failing + 1]]
                print(f"réduction de {len(head)} pas, échec : {signature}", flush=True)
                shortest, attempts = reduce_steps(root, params, head, signature, log=lambda t: print(t, flush=True))
                final = one_run(root, params, replay=[dict(s) for s in shortest])
                final["steps"] = renumber(final["steps"])
                final["reduced_from"] = len(replay)
                final["attempts"] = attempts
                record.update(verdict="echec", details=final,
                              summary=f"réduit de {len(replay)} à {len(shortest)} pas ({attempts} essais), échec : {signature}")
                code = 1
        else:
            data = one_run(root, params, replay=replay, emergency=emergency)
            record.update(verdict=data["verdict"], summary=data["summary"], details=data)
            code = 1 if data["failures"] else 0
        print(record["summary"], flush=True)
        for f in record["details"].get("failures", []):
            print(f"  pas {f['step']} [{f['type']}] {f['message']}", flush=True)
    except Exception as error:  # the record must say what happened  # noqa: BLE001
        record.update(verdict="erreur", summary=f"{type(error).__name__}: {error}", trace=traceback.format_exc())
        print(record["trace"], flush=True)
        code = 2
    save_run(out, record)
    print(f"résultat : {out}", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
