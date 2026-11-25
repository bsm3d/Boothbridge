# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Bounded subprocess probes that decode audio and simulate native crashes, kept away from the test runner process.

import ctypes
import os
import sys


# ------------------------------------------------------------
def crash(connection, path, full):
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        ctypes.CDLL("ucrtbase")._set_abort_behavior(0, 3)
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.abort()


# ----------------------------------------
def isolated_crash():
    import multiprocessing
    from unittest.mock import patch

    from djapp.audiocheck import isolated_check

    context = multiprocessing.get_context("spawn")
    original = context.Process
    children = []

    def process(**kwargs):
        kwargs["target"] = crash
        child = original(**kwargs)
        children.append(child)
        return child

    with patch("djapp.audiocheck.multiprocessing.get_context", return_value=context), \
         patch.object(context, "Process", side_effect=process):
        result = isolated_check("native-crash-probe.wav", True, timeout=10)
    assert result.status == "Not checked", result
    assert "stopped without a result" in result.detail, result
    assert len(children) == 1
    try:
        children[0].is_alive()
    except ValueError:
        pass
    else:
        raise AssertionError("Analysis process was not closed")
    assert not multiprocessing.active_children()
    print("native crash reported; process closed; no active children")


# ------------------------------------------------------------
def qt_decode(path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    loop = QEventLoop()
    player = QMediaPlayer()
    output = QAudioOutput()
    output.setMuted(True)
    player.setAudioOutput(output)
    state = {"decoded": False, "error": ""}

    def status(value):
        if value == QMediaPlayer.EndOfMedia:
            state["decoded"] = True
            loop.quit()

    def error(code, text):
        state["error"] = text or str(code)
        loop.quit()

    player.mediaStatusChanged.connect(status)
    player.errorOccurred.connect(error)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(10000)
    player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
    player.play()
    loop.exec()
    deadline.stop()
    assert not state["error"], state["error"]
    assert state["decoded"], "Qt decoder did not reach EOF within 10 seconds"
    source = player.source()
    player.stop()
    player.setPosition(0)
    state["decoded"] = False
    deadline.start(10000)
    player.play()
    loop.exec()
    deadline.stop()
    assert player.source() == source, "Replaying changed the decoder source"
    player.stop()
    player.setSource(QUrl())
    assert not state["error"], state["error"]
    assert state["decoded"], "Qt decoder did not reach EOF within 10 seconds"
    assert player.duration() == 0
    app.processEvents()
    print("Qt multimedia decoded to EOF and released source")


# ------------------------------------------------------------
# analysis.decode in a process of its own: with no Qt application it makes a core one for the AAC decoder
def decode_file(path):
    from djapp import analysis

    y, rate, frames = analysis.decode(path)
    if y is None:
        print("not decoded")
    else:
        print("decoded", len(y), rate, frames, round(float(abs(y).max()), 3), round(float(abs(y[-1000:]).max()), 3))


if __name__ == "__main__":
    if sys.argv[1] == "crash":
        isolated_crash()
    elif sys.argv[1] == "qt":
        qt_decode(sys.argv[2])
    elif sys.argv[1] == "decode":
        decode_file(sys.argv[2])
    else:
        raise ValueError("Unknown probe")
