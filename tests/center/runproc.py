# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : QProcess wrapper that runs a test child process with live output, a time limit and a Stop that kills its tree.

from __future__ import annotations

import os
import sys
import time

import psutil
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from . import ROOT


# ------------------------------------------------------------
# the process and everything it started (spawned workers, pools)
def kill_tree(pid: int):
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    victims = parent.children(recursive=True) + [parent]
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=3)


# ------------------------------------------------------------
class RunProcess(QObject):
    line = Signal(str)
    done = Signal(int, bool, bool)  # exit code, timed out, stopped by the user

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process: QProcess | None = None
        self.buffer = b""
        self.timed_out = False
        self.stopped = False
        self.started = 0.0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.on_timeout)

    def running(self) -> bool:
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def start(self, args: list[str], timeout: float = 0.0, env: dict | None = None, python: str = ""):
        self.timed_out = self.stopped = False
        self.buffer = b""
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.setWorkingDirectory(str(ROOT))
        full = QProcessEnvironment.systemEnvironment()
        for key, value in {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                           **(env or {})}.items():
            full.insert(key, value)
        self.process.setProcessEnvironment(full)
        self.process.readyReadStandardOutput.connect(self.on_output)
        self.process.finished.connect(self.on_finished)
        self.process.errorOccurred.connect(self.on_error)
        self.started = time.monotonic()
        self.process.start(python or sys.executable, ["-u", *args])
        if timeout > 0:
            self.timer.start(int(timeout * 1000))

    def on_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        *lines, self.buffer = self.buffer.split(b"\n")
        for raw in lines:
            self.line.emit(raw.decode("utf-8", "replace").rstrip("\r"))

    def on_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.line.emit(f"Impossible de lancer le processus : {self.process.errorString()}")
            self.timer.stop()
            self.done.emit(-1, False, False)

    def on_finished(self, code, _status):
        self.timer.stop()
        if self.buffer:
            self.line.emit(self.buffer.decode("utf-8", "replace").rstrip("\r"))
            self.buffer = b""
        self.done.emit(code, self.timed_out, self.stopped)

    def on_timeout(self):
        if self.running():
            self.timed_out = True
            self.line.emit("Temps limite atteint, le processus est arrêté.")
            kill_tree(int(self.process.processId()))

    def stop(self):
        if self.running():
            self.stopped = True
            kill_tree(int(self.process.processId()))

    def wait(self, ms: int = 3000) -> bool:
        return self.process is None or self.process.waitForFinished(ms)


# ------------------------------------------------------------
def child_env(visible: bool = False) -> dict:
    from tests import child_env as program_env

    env = {"PYTHONPATH": program_env()["PYTHONPATH"]}
    if not visible:
        env["QT_QPA_PLATFORM"] = "offscreen"
        if os.name == "nt":
            env["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR", "."), "Fonts")
    return env
