# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Regression tests for the export review: refused Mixxx keeps its checksums, relative folders, formula safety.

# fixes of the review of the seven writers added with the formats of the second round: a
# refused Mixxx export keeps the checksums, relative output folders, spreadsheet formulas
# behind a space, the format names of the command line and of the settings, the Options dialog

import csv
import io
import os
import sys
from unittest.mock import patch

import pytest

from djapp import app, export, settings
from djapp.export import lists, manifest
from djapp.model import Node, Track


def cfg(**extra) -> dict:
    return {**settings.DEFAULTS, "copy_music": False, "verify_copy": True, **extra}


@pytest.fixture
def nodes(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    p = music / "a.mp3"
    p.write_bytes(b"x" * 500)
    return [Node("playlist", "P", tracks=[Track(title="a", artist="b", path=str(p))])]


# ------------------------------------------------------------
def test_a_refused_mixxx_export_leaves_the_checksums_of_the_first(nodes, tmp_path):
    out = tmp_path / "out"
    export.run("Mixxx", nodes, str(out), cfg(copy_music=True))
    before = (out / manifest.NAME).read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        export.run("Mixxx", nodes, str(out), cfg(copy_music=True))
    assert (out / manifest.NAME).read_bytes() == before
    assert manifest.verify(str(out)).good


# ------------------------------------------------------------
@pytest.mark.parametrize(
    "fmt,where",
    [("VirtualDJ", "Folders/P.vdjfolder"), ("CSV", "playlists.csv"), ("JSON", "playlists.json")],
)
def test_a_relative_output_folder_gives_absolute_audio_paths(nodes, tmp_path, monkeypatch, fmt, where):
    monkeypatch.chdir(tmp_path)
    rep = export.run(fmt, nodes, "rel_out", cfg(copy_music=True))
    assert os.path.isabs(rep.path)
    text = (tmp_path / "rel_out" / where).read_text(encoding="utf-8-sig").replace("\\\\", "\\")
    assert str(tmp_path / "rel_out" / "Contents") in text


# ------------------------------------------------------------
@pytest.mark.parametrize(
    "value", [" =1+1", "  @SUM(A1)", "\t=1", "\"=1", "'=1", "\n=1", "\xa0-2+3", " \t +cmd"]
)
def test_a_formula_behind_blanks_or_quotes_is_a_text_too(nodes, value):
    nodes[0].tracks[0].title = value
    rows = list(csv.reader(io.StringIO(lists.csv_bytes(nodes, {}).decode("utf-8-sig"), newline="")))
    title = rows[1][3]
    assert title.startswith("'")
    assert title[1:] == value


def test_an_ordinary_title_is_left_alone(nodes):
    nodes[0].tracks[0].title = "  Intro - Mix"
    rows = list(csv.reader(io.StringIO(lists.csv_bytes(nodes, {}).decode("utf-8-sig"), newline="")))
    assert rows[1][3] == "  Intro - Mix"


# ------------------------------------------------------------
@pytest.mark.parametrize(
    "typed,name",
    [("mixxx", "Mixxx"), ("MIXXX", "Mixxx"), ("Mixxx (beta)", "Mixxx"), ("itunes xml", "iTunes XML"),
     ("  csv ", "CSV"), ("xspf (BETA)", "XSPF"), ("Rekordbox XML", "Rekordbox XML"), ("nope", ""), ("", "")],
)
def test_export_name_finds_the_format_in_any_case(typed, name):
    assert settings.export_name(typed) == name


def test_export_name_refuses_what_is_not_text():
    assert settings.export_name(None) == ""
    assert settings.export_name(["Mixxx"]) == ""


def test_a_saved_format_in_another_case_or_with_its_label_is_kept(tmp_path):
    for saved in ("mixxx", "Mixxx (beta)", "MIXXX"):
        f = tmp_path / "s.json"
        f.write_text('{"export_format": "%s"}' % saved, encoding="utf-8")
        with patch.object(settings, "CONFIG", f):
            assert settings.load()["export_format"] == "Mixxx"


def test_a_saved_format_that_is_no_text_falls_back(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('{"export_format": ["Mixxx"]}', encoding="utf-8")
    with patch.object(settings, "CONFIG", f):
        assert settings.load()["export_format"] == "Rekordbox XML"


def test_the_command_line_takes_a_format_in_any_case(nodes, tmp_path):
    src = tmp_path / "set.m3u"
    src.write_text("#EXTM3U\n" + nodes[0].tracks[0].path + "\n", encoding="utf-8")
    out = tmp_path / "cli"
    with patch.object(settings, "load", return_value=cfg(verify_copy=False)):
        assert app.headless(["--export", str(src), str(out), "--format", "csv"]) == 0
    assert (out / lists.CSV_NAME).is_file()
    with patch.object(settings, "load", return_value=cfg(verify_copy=False)):
        assert app.headless(["--export", str(src), str(tmp_path / "x"), "--format", "xspf (beta)"]) == 0
        assert app.headless(["--export", str(src), str(tmp_path / "y"), "--format", "nonsense"]) == 2


# ------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32" and not os.environ.get("QT_QPA_PLATFORM"), reason="needs Qt")
def test_the_options_dialog_starts_from_the_format_the_window_shows():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from djapp.ui import formatbox, main_window
    from djapp.ui.dialogs import LogWindow
    from djapp.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    seen = {}

    class Spy:
        def __init__(self, c, parent=None):
            seen.update(c)

        def exec(self):
            return 0

        def deleteLater(self):
            pass

    with patch.object(settings, "save"), patch.object(main_window, "Options", Spy):
        w = MainWindow(dict(settings.DEFAULTS, auto_load=False), LogWindow())
        try:
            formatbox.select(w.fmt, "PLS")
            w.copy.setChecked(False)
            w.options()
        finally:
            w.key_timer.stop()
            w.close()
            w.deleteLater()
    assert seen["export_format"] == "PLS"
    assert seen["copy_music"] is False
