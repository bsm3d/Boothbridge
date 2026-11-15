# the sandbox the monkey plays in: a process level setup, one World per run, and the
# patches that answer dialogs and block the dangerous parts of the application

from __future__ import annotations

import contextlib
import gc
import os
import random
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
)

from . import jail, synth

ODD_STRINGS = [
    "", " ", "a", "x" * 5000, "y" * 40000, "\U0001F3B5\U0001F525" * 30, "مرحبا بالعالم", "שלום עולם", "\u202etxt.exe",
    "a/b\\c", "../..", "..\\..\\x", "C:\\Windows\\System32", "NUL", "CON", "tab\there", "line\nbreak", "\x01\x07\x1b\x7f",
    "%s%d{}", "<b>bold</b>", "'; DROP TABLE x; --", "Z\u0351\u0352\u0353a\u0354l\u0355g\u0356o", "\u00e9\u00e8\u00ea \u00f1 \u00fc",
    "12345678901234567890", "-1", "1e999", "nan", "0", "Ctrl+Z",
]

# keys the application binds, as the author named them
SHORTCUTS = ["A", "B", "C", "D", "E", "F", "G", "H", "Shift+A", "Shift+C", "Space", "P", "Delete", "Ctrl+Z", "Ctrl+Y",
             "Ctrl+F", "Escape", "Return", "Ctrl+I", "Ctrl+T", "Ctrl+D", "Ctrl+B", "S", "Alt+Up", "Alt+Down",
             "Ctrl+Left", "Ctrl+Right", "Ctrl+Shift+D", "Ctrl+M", "Ctrl+R", "Ctrl+K", "Ctrl+L", "F1", "Ctrl+S", "Ctrl+N"]
OTHER_KEYS = ["Tab", "Backtab", "Up", "Down", "Left", "Right", "Home", "End", "PageUp", "PageDown", "Backspace",
              "Enter", "x", "1", "9", "Plus", "Minus", "Shift+Left", "Ctrl+A", "Ctrl+C", "Ctrl+V", "F2", "F5", "Menu"]


# ------------------------------------------------------------
# once per process: jail, isolated settings folder, no real user folders
def process_setup(root: Path):
    root = Path(jail.install(root))
    home = root / "home"
    appdata = root / "appdata"
    for d in (home, appdata, root / "settings"):
        d.mkdir(parents=True, exist_ok=True)
    for name, value in (("LOCALAPPDATA", appdata), ("APPDATA", appdata), ("USERPROFILE", home), ("HOME", home),
                        ("BB_PURE_PYTHON", os.environ.get("BB_PURE_PYTHON", ""))):
        if value != "":
            os.environ[name] = str(value)
    os.chdir(root)
    from djapp import settings
    settings.CONFIG = root / "settings" / "boothbridge.json"
    return root


# ------------------------------------------------------------
# runs the event loop for ms milliseconds (at least once), deferred deletions included
def pump(ms: int = 0):
    end = time.monotonic() + ms / 1000
    while True:
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if time.monotonic() >= end:
            break
        time.sleep(0.002)


# ----------------------------------------
# raised by the stubs of the dangerous application functions
class Blocked(RuntimeError):
    pass


# ------------------------------------------------------------
# nothing may list, eject, check or format a drive, whatever the window asks
def block_drives(stack: contextlib.ExitStack, note):
    from djapp import drives

    def make(name, result):
        def stub(*args, **kwargs):
            note(f"drives.{name}")
            if result is Blocked:
                raise Blocked(f"drives.{name} is blocked in the test center")
            return result
        return stub

    for name, result in (("list_drives", []), ("drive_for", None), ("eject", Blocked), ("check", Blocked),
                         ("format_drive", Blocked), ("do_format", Blocked), ("launch_elevated", Blocked),
                         ("list_windows", []), ("list_linux", []), ("list_mac", []), ("eject_windows", Blocked),
                         ("rescan_windows", Blocked), ("format_windows", Blocked), ("run", Blocked)):
        if hasattr(drives, name):
            stack.enter_context(mock.patch.object(drives, name, make(name, result)))


def block_outside(stack: contextlib.ExitStack, note):
    def open_url(url):
        note(f"openUrl {url.toString() if isinstance(url, QUrl) else url}")
        return True

    stack.enter_context(mock.patch.object(QDesktopServices, "openUrl", staticmethod(open_url)))
    if hasattr(os, "startfile"):
        stack.enter_context(mock.patch.object(os, "startfile", lambda *a, **k: note("os.startfile"), create=True))
    import webbrowser
    stack.enter_context(mock.patch.object(webbrowser, "open", lambda *a, **k: note("webbrowser.open") or True))
    stack.enter_context(mock.patch.object(QApplication, "beep", staticmethod(lambda: None)))


# ------------------------------------------------------------
# one main window on a fresh synthetic collection, with its own settings and temp folder
class World:
    count = 0

    def __init__(self, root: Path, seed: int):
        World.count += 1
        self.root = Path(root) / f"world{World.count}"
        self.seed = seed
        self.col = None
        self.main = None
        self.logwin = None

    def start(self):
        from djapp import settings
        from djapp.ui import theme
        from djapp.ui.dialogs import LogWindow
        from djapp.ui.main_window import MainWindow
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyleSheet(theme.STYLE)
        self.col = synth.build(self.root, self.seed)
        settings.CONFIG = self.root.parent / "settings" / f"djapp-{World.count}.json"
        settings.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False, library_root=str(self.col.audio),
                   music_root=str(self.col.audio), output_path=str(self.col.out), source_path="")
        self.logwin = LogWindow()
        self.main = MainWindow(cfg, self.logwin)
        self.main.show()
        pump(50)
        self.load(self.col.project)

    def busy(self) -> bool:
        from djapp.ui import worker
        main = self.main
        loader = getattr(main, "loader", None)
        return bool(main is not None and (main.job is not None or worker.has_active_jobs()
                    or (loader is not None and (getattr(loader, "th", None) or getattr(loader, "hi_th", None)
                                                or getattr(loader, "queue", None) or getattr(loader, "hi_queue", None)))))

    def wait_idle(self, seconds: float = 15.0) -> bool:
        end = time.monotonic() + seconds
        while self.busy():
            pump(10)
            if time.monotonic() > end:
                return False
        pump(0)
        return True

    def load(self, path):
        self.main.src.setText(str(path))
        self.main.reload()
        self.wait_idle()

    # closes everything the run opened. Never raises: the next run must be able to start
    def stop(self):
        from djapp.ui import cuehistory, worker
        main = self.main
        if main is None:
            return
        with contextlib.suppress(Exception):
            main.cfg["confirm_exit"] = False
            main.dirty = False
            for _ in range(3):
                for w in list(QApplication.topLevelWidgets()):
                    if w is not main and w.isVisible():
                        w.close()
                pump(20)
            worker.stop_all(5000)
            with contextlib.suppress(Exception):
                main.loader.stop()
            for _ in range(3):
                if main.close():
                    break
                worker.stop_all(3000)
                pump(50)
            main.deleteLater()
            self.logwin.deleteLater()
            pump(30)
        cuehistory._hist.clear()
        cuehistory._edits.clear()
        gc.collect()
        shutil.rmtree(self.root, ignore_errors=True)
        self.main = None


def rng_for(seed, kind: str, title: str, count: int) -> random.Random:
    return random.Random(f"{seed}:{kind}:{title}:{count}")


# ------------------------------------------------------------------
def standard_buttons(buttons) -> list:
    sb = QMessageBox.StandardButton
    order = [sb.Yes, sb.Save, sb.Ok, sb.Open, sb.Apply, sb.YesToAll, sb.No, sb.Discard, sb.Cancel, sb.Close, sb.Abort,
             sb.Retry, sb.Ignore, sb.Help]
    found = []
    for b in order:
        with contextlib.suppress(TypeError):
            if buttons & b:
                found.append(b)
    return found or [sb.Ok]


# --------------------------------------------
# QMenu.exec cannot be replaced, so a menu that shows up is answered from the event loop it starts
class PopupFilter(QObject):
    def __init__(self, monkey):
        super().__init__()
        self.m = monkey

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and isinstance(obj, QMenu) and obj.isWindow():
            QTimer.singleShot(0, lambda: self.answer(obj))
        return False

    def answer(self, menu):
        try:
            if not menu.isVisible():
                return
            actions = [a for a in menu.actions() if a.isEnabled() and not a.isSeparator() and a.menu() is None]
            index = self.m.choose("menu", menu.objectName() or "menu", len(actions) + 1)
            if index < len(actions) and not self.m.action_blocked(actions[index].text()):
                menu.setActiveAction(actions[index])
                QTest.keyClick(menu, Qt.Key.Key_Return)
            if menu.isVisible():
                menu.close()
        except RuntimeError:
            pass


# ------------------------------------------------------------
# answers every modal question on its own with seeded choices, and keeps files inside the sandbox
class Patches:
    def __init__(self, monkey):
        self.m = monkey
        self.stack = contextlib.ExitStack()

    def __enter__(self):
        m = self.m
        patch = self.stack.enter_context
        for name, default in (("question", "Yes|No"), ("information", "Ok"), ("warning", "Ok"), ("critical", "Ok")):
            patch(mock.patch.object(QMessageBox, name, staticmethod(self.message(name, default))))
        # plain functions, so that Python binds them as methods of the widget
        patch(mock.patch.object(QMessageBox, "exec", lambda box: self.box_exec(box)))
        patch(mock.patch.object(QDialog, "exec", lambda dialog: self.dialog_exec(dialog)))
        patch(mock.patch.object(QFileDialog, "getOpenFileName", staticmethod(self.open_file)))
        patch(mock.patch.object(QFileDialog, "getOpenFileNames", staticmethod(self.open_files)))
        patch(mock.patch.object(QFileDialog, "getSaveFileName", staticmethod(self.save_file)))
        patch(mock.patch.object(QFileDialog, "getExistingDirectory", staticmethod(self.folder)))
        patch(mock.patch.object(QInputDialog, "getText", staticmethod(self.get_text)))
        patch(mock.patch.object(QInputDialog, "getItem", staticmethod(self.get_item)))
        patch(mock.patch.object(QInputDialog, "getInt", staticmethod(self.get_int)))
        patch(mock.patch.object(QInputDialog, "getDouble", staticmethod(self.get_double)))
        self.popups = PopupFilter(m)
        QApplication.instance().installEventFilter(self.popups)
        self.stack.callback(lambda: QApplication.instance().removeEventFilter(self.popups))
        block_drives(self.stack, m.note_blocked)
        block_outside(self.stack, m.note_blocked)
        return self

    def __exit__(self, *exc):
        self.stack.close()

    # ---- message boxes

    def message(self, name, default):
        def ask(parent=None, title="", text="", buttons=None, default_button=None, *rest, **kw):
            if buttons is None:
                buttons = QMessageBox.Yes | QMessageBox.No if name == "question" else QMessageBox.Ok
            options = standard_buttons(buttons)
            return options[self.m.choose("box", title, len(options), favor=True)]
        return ask

    def box_exec(self, box):
        options = standard_buttons(box.standardButtons())
        choice = options[self.m.choose("box", box.windowTitle(), len(options), favor=True)]
        return int(choice.value)

    def dialog_exec(self, dialog):
        return self.m.exec_dialog(dialog)

    # ---- files and folders, always inside the sandbox

    def open_file(self, parent=None, caption="", directory="", filter="", *rest, **kw):
        col = self.m.world.col
        options = [str(p) for p in col.sources()] + [str(col.files[0]), str(col.files[-1]), ""]
        path = options[self.m.choose("openfile", caption, len(options))]
        return path, ""

    def open_files(self, parent=None, caption="", directory="", filter="", *rest, **kw):
        col = self.m.world.col
        count = self.m.choose("openfiles", caption, 5)
        start = self.m.choose("openfiles-start", caption, max(1, len(col.files)))
        return [str(p) for p in col.files[start:start + count]], ""

    def save_file(self, parent=None, caption="", directory="", filter="", *rest, **kw):
        col = self.m.world.col
        names = ["", "set.json", "list.m3u8", "p.nml", "backup.zip", "é ü & co.json", "same.json", "x" * 40 + ".zip"]
        name = names[self.m.choose("savefile", caption, len(names))]
        return (str(col.out / name) if name else ""), ""

    def folder(self, parent=None, caption="", directory="", *rest, **kw):
        col = self.m.world.col
        extra = col.root / f"new{self.m.counter('folder')}"
        options = [str(col.out), str(col.audio), str(col.restore), str(extra), ""]
        return options[self.m.choose("folder", caption, len(options))]

    # ---- input dialogs

    def get_text(self, parent=None, title="", label="", echo=None, text="", *rest, **kw):
        options = [*ODD_STRINGS, text or "name"]
        index = self.m.choose("text", title, len(options) + 2)
        if index >= len(options):
            return (text, index == len(options))
        return options[index], self.m.choose("textok", title, 6) != 0

    def get_item(self, parent=None, title="", label="", items=(), current=0, editable=True, *rest, **kw):
        items = list(items) or [""]
        index = self.m.choose("item", title, len(items))
        return items[index], self.m.choose("itemok", title, 6) != 0

    def get_int(self, parent=None, title="", label="", value=0, minimum=-2147483647, maximum=2147483647, *rest, **kw):
        options = [value, minimum, maximum, 0, 1]
        return options[self.m.choose("int", title, len(options))], True

    def get_double(self, parent=None, title="", label="", value=0.0, minimum=-2147483647.0, maximum=2147483647.0,
                   *rest, **kw):
        options = [value, minimum, maximum, 0.0, 1.5]
        return options[self.m.choose("double", title, len(options))], True


def wait_threads(limit: float = 5.0):
    end = time.monotonic() + limit
    while threading.active_count() > 1 and time.monotonic() < end:
        time.sleep(0.05)
