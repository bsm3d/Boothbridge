# the offscreen Qt platform has no font database on Windows, so every text measures about
# 2.4 times too wide and the layout tests see windows that do not exist for a real user.
# Pointing it at the system fonts gives the widths Segoe UI really has

import os
import tempfile

# no test reads or writes the real data folder of the program (%APPDATA%\BSM\Boothbridge): one temporary folder for the run
os.environ.setdefault("BOOTHBRIDGE_HOME", tempfile.mkdtemp(prefix="bb_home_"))

if os.name == "nt" and os.environ.get("QT_QPA_PLATFORM", "offscreen") == "offscreen":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "."), "Fonts"))


# the thank you for the support page is a box that waits for a click: no test must meet it by chance,
# the tests of the support link set the address themselves
import pytest


@pytest.fixture(autouse=True)
def no_support_link(monkeypatch):
    import djapp.ui.dialogs as dialogs
    import djapp.ui.main_window as main_window
    import djapp.ui.support as support

    for module in (support, main_window, dialogs):
        monkeypatch.setattr(module, "SUPPORT_URL", "")


# a process that has no Qt application would get a core one from the AAC decoder, and the tests that
# build their windows later find that one instead of a QApplication. The decoder is tried in a process of
# its own in test_audio_decoders
@pytest.fixture(autouse=True)
def no_qt_decoder_in_process(monkeypatch):
    import djapp.analysis as analysis

    def unavailable(path):
        raise RuntimeError("The Qt decoder is not used inside the test process")

    monkeypatch.setattr(analysis, "read_qt", unavailable)
