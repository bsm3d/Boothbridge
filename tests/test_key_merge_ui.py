# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the key merge in the main window: the question before export, the Options setting and the help text.

# main window side of the keys that already have playlists: the question before an export, the
# Options setting, File > Open a CDJ USB key, the line next to Output, and the Help text

import contextlib
import hashlib
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import soundfile
from djapp import export, settings, sources
from djapp.export.cdj import reference, usb
from djapp.export.files import Cancelled, Report
from djapp.model import Node, Track, playlists
from djapp.sources import key
from djapp.ui import main_window, worker
from djapp.ui.dialogs import USAGE, KeyChoice, LogWindow, Options, PlayerSettings
from djapp.ui.main_window import MainWindow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox

YES, NO, CANCEL = QMessageBox.Yes, QMessageBox.No, QMessageBox.Cancel
ENGINE = hasattr(export, "merge_names") and hasattr(export, "key_summary")


@dataclass
class Summary:
    folders: int = 1
    playlists: int = 3
    tracks: int = 42
    names: list = field(default_factory=lambda: ["Warm-up", "Peak", "Folder / Closing"])
    readable: bool = True
    others: list = field(default_factory=list)


# ------------------------------------------------------------
def wait_for(app, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return condition()


# ------------------------------------------------------------
# A running job that only ends when it is told to
def hold_a_job() -> worker.Job:
    job = worker.Job(lambda progress=None, cancel=None: cancel.wait(30))
    job.start()
    return job


def release_job(app, job):
    job.stop.set()
    wait_for(app, lambda: not job.isRunning() and not worker.has_active_jobs())


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------
# A main window whose export, message boxes, settings file and key question are fakes
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = str(Path(self.tmp.name) / "key")
        self.db = Path(self.out, *key.DB)
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b"x" * 64)  # only its presence and stamp count, the summary is a fake
        self.runs = []
        self.run_error = None
        self.error_modes = ("keep", "replace")
        self.report = {}
        self.summary = Summary()
        self.merged = ["Peak"]
        self.dialogs = []
        self.answer = lambda dlg: dlg.keep_btn.click()
        self.boxes = {}
        for name in ("information", "warning", "critical", "question"):
            p = patch.object(QMessageBox, name, return_value=YES if name == "question" else QMessageBox.Ok)
            self.boxes[name] = p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(settings, "save"),
                  patch.object(export, "run", self.fake_run),
                  patch.object(export, "key_summary", lambda path, cancel=None: self.summary),
                  patch.object(export, "merge_names", lambda nodes, summary: list(self.merged), create=True),
                  patch.object(main_window, "fs_type", lambda path: "FAT32"),
                  patch.object(KeyChoice, "exec", lambda dlg: self.fake_exec(dlg))):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False, key_ask=True, key_mode="keep", export_format="CDJ/USB")
        self.main = MainWindow(self.cfg, LogWindow())
        self.main.offer_eject = lambda *a, **k: None
        self.main.nodes = [Node("playlist", "Peak", tracks=[Track(title="T", path="t.wav")])]
        self.main.fill_tree()
        self.main.out.setText(self.out)
        self.addCleanup(self.close_main)

    def close_main(self):
        self.main.key_timer.stop()
        wait_for(self.app, lambda: not worker.has_active_jobs() and self.main.job is None)
        self.main.close()
        self.main.deleteLater()
        self.app.processEvents()

    def fake_run(self, fmt, nodes, out, cfg, progress=None, cancel=None):
        self.runs.append((fmt, out, dict(cfg)))
        if self.run_error is not None and cfg["key_mode"] in self.error_modes:
            raise self.run_error
        return Report(tracks=1, path=out, **self.report)

    def fake_exec(self, dlg):
        label = dlg.findChildren(QLabel)[0]
        self.dialogs.append({
            "title": dlg.windowTitle(), "text": label.text(), "plain": label.textFormat() == Qt.PlainText,
            "keep_hidden": dlg.keep_btn.isHidden(), "again_hidden": dlg.again.isHidden(),
            "keep": dlg.keep_btn.text(), "replace": dlg.replace_btn.text(), "cancel": dlg.cancel_btn.text(),
            "default": [b.text() for b in (dlg.keep_btn, dlg.replace_btn, dlg.cancel_btn) if b.isDefault()]})
        self.answer(dlg)
        return dlg.result()

    def convert(self, runs=1):
        self.main.convert()
        wait_for(self.app, lambda: len(self.runs) >= runs and self.main.job is None and self.main.key_read is None)
        self.app.processEvents()

    def modes(self):
        return [cfg["key_mode"] for _, _, cfg in self.runs]


# ------------------------------------------------------------
class KeyQuestionTests(Base):
    def test_dialog_content(self):
        self.summary = Summary(folders=2, playlists=3, tracks=42)
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        shown = self.dialogs[0]
        self.assertEqual(shown["title"], "This key already has playlists")
        for part in ("3 playlists", "2 folders", "42 tracks", "Warm-up", "Folder / Closing",
                     "Playlists with the same name will be replaced:", "Peak"):
            self.assertIn(part, shown["text"])
        self.assertEqual((shown["keep"], shown["replace"], shown["cancel"]),
                         ("Keep them and add this export", "Replace everything on the key", "Cancel"))
        self.assertEqual(shown["default"], ["Keep them and add this export"])

    def test_files_of_rekordbox_are_announced_and_left_alone(self):
        self.summary = Summary(others=["exportExt.pdb", "exportLibrary.db"])
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        text = self.dialogs[0]["text"]
        self.assertIn("exportExt.pdb and exportLibrary.db, written by rekordbox", text)
        self.assertIn("only writes export.pdb and leaves them as they are", text)
        self.assertIn("Test the key on your player", text)
        self.assertLess(text.index("Keep them: the playlists on the key stay"), text.index("written by rekordbox"))

    def test_no_such_files_no_such_warning(self):
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        self.assertNotIn("rekordbox", self.dialogs[0]["text"])

    def test_the_warning_is_also_there_when_the_database_cannot_be_read(self):
        self.summary = Summary(readable=False, others=["exportLibrary.db"])
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        text = self.dialogs[0]["text"]
        self.assertIn("cannot be read", text)
        self.assertIn("exportLibrary.db, written by rekordbox", text)

    def test_names_are_capped_at_eight(self):
        self.summary = Summary(playlists=12, names=[f"List {i}" for i in range(12)])
        self.merged = [f"New {i}" for i in range(10)]
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        text = self.dialogs[0]["text"]
        self.assertIn("List 7", text)
        self.assertNotIn("List 8", text)
        self.assertIn("... and 4 more", text)
        self.assertIn("New 7", text)
        self.assertNotIn("New 8", text)
        self.assertIn("... and 2 more", text)

    def test_nothing_to_replace_is_not_announced(self):
        self.merged = []
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        self.assertNotIn("same name", self.dialogs[0]["text"])

    def test_names_are_plain_text(self):
        self.summary = Summary(names=["<b>Bold</b> & <i>x</i>"])
        self.merged = ["<h1>Big</h1>"]
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        shown = self.dialogs[0]
        self.assertTrue(shown["plain"])
        self.assertIn("<b>Bold</b> & <i>x</i>", shown["text"])
        self.assertIn("<h1>Big</h1>", shown["text"])

    def test_no_dialog_without_database(self):
        self.summary = None
        self.convert()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.modes(), ["keep"])

    def test_no_dialog_for_other_formats(self):
        for fmt in ("Rekordbox XML", "M3U", "Traktor NML"):
            self.runs.clear()
            self.main.fmt.setCurrentText(fmt)
            self.convert()
            self.assertEqual(len(self.runs), 1, fmt)
        self.assertEqual(self.dialogs, [])

    def test_keep_passes_keep_and_leaves_the_setting(self):
        self.cfg["key_mode"] = "replace"
        self.answer = lambda dlg: dlg.keep_btn.click()
        self.convert()
        self.assertEqual(self.modes(), ["keep"])
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (True, "replace"))

    def test_replace_passes_replace_for_this_run_only(self):
        self.answer = lambda dlg: dlg.replace_btn.click()
        self.convert()
        self.assertEqual(self.modes(), ["replace"])
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (True, "keep"))
        self.boxes["question"].assert_not_called()
        self.assertEqual(len(self.dialogs), 1)
        self.runs.clear()
        self.answer = lambda dlg: dlg.keep_btn.click()
        self.convert()
        self.assertEqual(self.modes(), ["keep"])

    def test_cancel_runs_nothing_and_changes_nothing(self):
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        self.assertEqual(self.runs, [])
        self.assertIsNone(self.main.job)
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (True, "keep"))
        self.assertEqual(settings.save.call_count, 0)

    def test_closing_the_dialog_is_a_cancel(self):
        self.answer = lambda dlg: dlg.reject()
        self.convert(runs=0)
        self.assertEqual(self.runs, [])

    def test_do_not_ask_again_with_keep(self):
        def answer(dlg):
            dlg.again.setChecked(True)
            dlg.keep_btn.click()
        self.cfg["key_mode"] = "replace"
        self.answer = answer
        self.convert()
        self.assertEqual(self.modes(), ["keep"])
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (False, "keep"))
        # and the next export does not ask
        self.runs.clear()
        self.convert()
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.modes(), ["keep"])

    def test_do_not_ask_again_with_replace_needs_a_second_yes(self):
        def answer(dlg):
            dlg.again.setChecked(True)
            dlg.replace_btn.click()
        self.answer = answer
        self.convert()
        self.assertEqual(self.modes(), ["replace"])
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (False, "replace"))
        text = self.boxes["question"].call_args.args[2]
        self.assertIn("The existing playlists will be removed from the key, a backup of the old database is kept.", text)

    def test_declining_the_second_confirmation_is_not_a_choice(self):
        def answer(dlg):
            dlg.again.setChecked(True)
            dlg.replace_btn.click()
            dlg.cancel_btn.click()
        self.boxes["question"].return_value = NO
        self.answer = answer
        self.convert(runs=0)
        self.assertEqual(self.runs, [])
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (True, "keep"))

    def test_unreadable_database_offers_replace_or_cancel(self):
        self.summary = Summary(readable=False, playlists=0, tracks=0, names=[])
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert(runs=0)
        shown = self.dialogs[0]
        self.assertTrue(shown["keep_hidden"])
        self.assertTrue(shown["again_hidden"])
        self.assertIn("cannot be read", shown["text"])
        self.assertEqual(shown["replace"], "Replace (a backup of the old database is kept)")
        self.assertEqual(self.runs, [])
        self.answer = lambda dlg: dlg.replace_btn.click()
        self.convert()
        self.assertEqual(self.modes(), ["replace"])
        self.assertEqual((self.cfg["key_ask"], self.cfg["key_mode"]), (True, "keep"))

    def test_ask_off_uses_the_saved_mode_without_a_dialog(self):
        self.cfg["key_ask"] = False
        for mode in ("keep", "replace"):
            self.runs.clear()
            self.cfg["key_mode"] = mode
            self.convert()
            self.assertEqual(self.modes(), [mode])
        self.assertEqual(self.dialogs, [])

    def test_status_says_how_many_playlists_were_kept(self):
        self.cfg["key_ask"] = False
        self.report = {"kept": 3}
        self.convert()
        self.assertEqual(self.main.status.text(), "Done. Kept 3 playlists already on the key")
        self.assertIn("Kept 3 playlists already on the key", self.boxes["information"].call_args.args[2])

    def test_contents_folder_is_moved_up_before_the_question(self):
        asked = []
        with patch.object(export, "key_summary", lambda path, cancel=None: asked.append(path) or None):
            self.main.out.setText(os.path.join(self.out, "Contents"))
            self.convert()
        self.assertEqual(asked[0], self.out)


# ------------------------------------------------------------
# Convert looks at the key before it asks, away from the GUI thread, or from the status line
class ConvertReadsTheKeyTests(Base):
    def setUp(self):
        super().setUp()
        self.main.key_timer.stop()
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.threads = []
        self.calls = []
        self.gate = None
        patcher = patch.object(export, "key_summary", self.spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spy(self, path, cancel=None):
        self.threads.append(threading.get_ident())
        self.calls.append(path)
        if self.gate is not None:
            self.gate.wait(10)
        return self.summary

    def probe(self):
        self.main.probe_key()
        wait_for(self.app, lambda: self.calls and self.main.key_job is None)
        self.app.processEvents()

    def test_the_key_is_read_off_the_gui_thread(self):
        self.convert(runs=0)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(threading.get_ident(), self.threads)
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("3 playlists", self.dialogs[0]["text"])

    def test_the_answer_goes_on_with_the_conversion(self):
        self.answer = lambda dlg: dlg.replace_btn.click()
        self.convert()
        self.assertEqual(self.modes(), ["replace"])
        self.assertIsNone(self.main.key_read)
        self.assertTrue(self.main.go.isEnabled())

    def test_a_fresh_probe_is_reused(self):
        self.probe()
        self.assertEqual(len(self.calls), 1)
        self.convert(runs=0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("3 playlists", self.dialogs[0]["text"])

    def test_a_reused_probe_needs_no_wait(self):
        self.probe()
        self.main.convert()
        self.assertIsNone(self.main.key_read)
        self.assertEqual(len(self.dialogs), 1)

    def test_a_probe_of_another_folder_is_not_reused(self):
        other = Path(self.tmp.name, "other", *key.DB)
        other.parent.mkdir(parents=True)
        other.write_bytes(self.db.read_bytes())
        self.probe()
        self.main.out.setText(str(other.parents[2]))
        self.main.key_timer.stop()
        self.convert(runs=0)
        self.assertEqual(self.calls, [self.out, str(other.parents[2])])

    def test_a_probe_of_a_file_that_changed_size_is_read_again(self):
        self.probe()
        self.db.write_bytes(b"x" * 4096)
        self.convert(runs=0)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn(threading.get_ident(), self.threads)

    def test_a_probe_of_a_file_that_changed_time_is_read_again(self):
        self.probe()
        st = self.db.stat()
        os.utime(self.db, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
        self.convert(runs=0)
        self.assertEqual(len(self.calls), 2)

    def test_a_probe_that_found_no_database_is_not_a_fresh_one(self):
        self.summary = None
        self.probe()
        self.summary = Summary()
        self.convert(runs=0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.dialogs), 1)

    def test_the_result_of_the_read_feeds_the_status_line(self):
        self.summary = Summary(playlists=8, tracks=80)
        self.convert(runs=0)
        self.assertEqual(self.main.key_label.text(), "Key: 8 playlists, 80 tracks")
        self.assertEqual(len(self.calls), 1)
        self.convert(runs=0)
        self.assertEqual(len(self.calls), 1)

    def test_it_says_so_and_cannot_start_twice_while_it_reads(self):
        self.gate = threading.Event()
        self.main.convert()
        wait_for(self.app, lambda: self.calls)
        self.assertEqual(self.main.status.text(), "Reading the key...")
        self.assertFalse(self.main.go.isEnabled())
        self.assertTrue(self.main.stop.isEnabled())
        self.assertIsNotNone(self.main.key_read)
        self.main.convert()
        self.main.go.click()
        self.assertEqual(self.dialogs, [])
        self.gate.set()
        wait_for(self.app, lambda: self.main.key_read is None)
        self.app.processEvents()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.dialogs), 1)
        self.assertTrue(self.main.go.isEnabled())

    def test_it_cannot_be_started_again_before_the_read_even_runs(self):
        self.gate = threading.Event()
        self.main.convert()
        self.main.convert()
        wait_for(self.app, lambda: self.calls)
        self.gate.set()
        wait_for(self.app, lambda: self.main.key_read is None)
        self.app.processEvents()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.dialogs), 1)

    def test_cancel_during_the_read_gives_up_quietly(self):
        started = threading.Event()

        def waits(path, cancel=None):
            started.set()
            cancel.wait(10)
            raise Cancelled()
        with patch.object(export, "key_summary", waits):
            self.main.convert()
            wait_for(self.app, started.is_set)
            self.main.cancel()
            self.assertTrue(wait_for(self.app, lambda: self.main.key_read is None))
        self.assertEqual(self.main.status.text(), "Cancelled")
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.runs, [])
        self.assertTrue(self.main.go.isEnabled())
        self.assertFalse(self.main.stop.isEnabled())

    def test_a_cancel_that_comes_after_the_answer_still_wins(self):
        self.gate = threading.Event()
        self.main.convert()
        wait_for(self.app, lambda: self.calls)
        self.main.cancel()
        self.gate.set()
        wait_for(self.app, lambda: self.main.key_read is None)
        self.app.processEvents()
        self.assertEqual((self.dialogs, self.runs), ([], []))
        self.assertEqual(self.main.status.text(), "Cancelled")

    def test_no_database_is_instant_and_never_reads(self):
        self.db.unlink()
        self.cfg["key_ask"] = True
        self.main.convert()
        self.assertIsNone(self.main.key_read)
        self.assertIsNotNone(self.main.job)
        self.assertEqual(self.calls, [])
        wait_for(self.app, lambda: self.runs and self.main.job is None)
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.modes(), ["keep"])

    def test_nothing_is_read_when_the_answer_is_not_needed(self):
        self.cfg["key_ask"] = False
        self.main.convert()
        self.assertIsNone(self.main.key_read)
        self.assertEqual(self.calls, [])  # the probe after the export has not run yet
        self.assertIsNotNone(self.main.job)
        wait_for(self.app, lambda: self.runs and self.main.job is None)
        self.assertEqual(self.modes(), ["keep"])

    def test_a_read_that_fails_goes_on_with_the_saved_mode(self):
        def broken(path, cancel=None):
            raise OSError("the drive went away")
        self.cfg["key_mode"] = "replace"
        with patch.object(export, "key_summary", broken):
            self.convert()
        self.assertEqual(self.modes(), ["replace"])
        self.assertEqual(self.dialogs, [])
        self.assertTrue(self.main.go.isEnabled())

    def test_quit_while_the_read_runs_cancels_it(self):
        started = threading.Event()

        def waits(path, cancel=None):
            started.set()
            cancel.wait(10)
            raise Cancelled()
        with patch.object(export, "key_summary", waits):
            self.main.convert()
            wait_for(self.app, started.is_set)
            began = time.monotonic()
            self.assertTrue(self.main.close())
            self.assertLess(time.monotonic() - began, 2.5)
        self.assertFalse(worker.has_active_jobs())
        self.assertEqual((self.dialogs, self.runs), ([], []))

    def test_the_read_stops_the_probe_that_would_read_the_same_thing(self):
        self.gate = threading.Event()
        self.main.probe_key()
        wait_for(self.app, lambda: self.calls)
        probe = self.main.key_job
        self.main.convert()
        self.assertTrue(probe.stop.is_set())
        self.gate.set()
        wait_for(self.app, lambda: self.main.key_read is None and self.main.key_job is None)
        self.app.processEvents()
        self.assertEqual(len(self.dialogs), 1)


# ----------------------------------------------------------------
class UnreadableAfterTheFactTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg["key_ask"] = False
        self.summary = Summary(readable=False, playlists=0, tracks=0, names=[])
        self.run_error = ValueError("Cannot read export.pdb on the key")
        self.error_modes = ("keep",)

    def test_replace_runs_the_export_again_in_replace_mode(self):
        self.answer = lambda dlg: dlg.replace_btn.click()
        self.convert(runs=2)
        self.assertEqual(self.modes(), ["keep", "replace"])
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn("Cannot read export.pdb on the key", self.dialogs[0]["text"])
        self.assertTrue(self.dialogs[0]["keep_hidden"])
        self.boxes["critical"].assert_not_called()
        self.assertEqual(self.cfg["key_mode"], "keep")

    def test_cancel_stops_there(self):
        self.answer = lambda dlg: dlg.cancel_btn.click()
        self.convert()
        self.assertEqual(self.modes(), ["keep"])
        self.assertEqual(self.main.status.text(), "Cancelled")
        self.boxes["critical"].assert_not_called()
        self.assertIsNone(self.main.job)

    def test_other_errors_keep_the_plain_error_box(self):
        self.summary = Summary()
        self.convert()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.boxes["critical"].call_args.args[1], "Export failed")

    def test_replace_mode_never_gets_a_second_chance(self):
        self.cfg["key_mode"] = "replace"
        self.error_modes = ("keep", "replace")
        self.convert()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.boxes["critical"].call_args.args[1], "Export failed")


# ------------------------------------------------------------
class OptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def options(self, **cfg):
        return Options(dict(settings.DEFAULTS, **cfg))

    def test_combo_shows_the_saved_values(self):
        for ask, mode, index in ((True, "keep", 0), (True, "replace", 0), (False, "keep", 1), (False, "replace", 2)):
            d = self.options(key_ask=ask, key_mode=mode)
            self.assertEqual([d.on_key.itemText(i) for i in range(3)], ["Ask me", "Keep them and add", "Replace them"])
            self.assertEqual(d.on_key.currentIndex(), index)

    def test_combo_round_trips(self):
        for index, ask, mode in ((0, True, "replace"), (1, False, "keep"), (2, False, "replace")):
            d = self.options(key_ask=True, key_mode="replace" if index == 0 else "keep")
            d.on_key.setCurrentIndex(index)
            with patch("djapp.ui.dialogs.ask_replace_always", return_value=True):
                d.accept()
            self.assertEqual((d.cfg["key_ask"], d.cfg["key_mode"]), (ask, mode))
            again = Options(d.cfg)
            self.assertEqual(again.on_key.currentIndex(), index if index else 0)

    def test_replacing_everything_is_asked_and_can_be_refused(self):
        d = self.options(key_ask=True, key_mode="keep")
        d.on_key.setCurrentIndex(2)
        with patch("djapp.ui.dialogs.ask_replace_always", return_value=False) as ask:
            d.accept()
        ask.assert_called_once()
        self.assertEqual((d.cfg["key_ask"], d.cfg["key_mode"]), (True, "keep"))

    def test_refused_replace_then_cancel_keeps_the_library_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            d = self.options(key_ask=True, key_mode="keep", library_root="")
            d.library_root.setText(folder)
            d.on_key.setCurrentIndex(2)
            with patch("djapp.ui.dialogs.ask_replace_always", return_value=False):
                d.accept()
            d.reject()
            self.assertEqual(d.cfg["library_root"], "")

    def test_replace_already_chosen_is_not_asked_again(self):
        d = self.options(key_ask=False, key_mode="replace")
        self.assertEqual(d.on_key.currentIndex(), 2)
        with patch("djapp.ui.dialogs.ask_replace_always") as ask:
            d.accept()
        ask.assert_not_called()

    def test_the_other_options_are_saved_as_before(self):
        cfg = dict(settings.DEFAULTS, copy_music=False, verify_copy=False, volume=33, crossfade=5)
        d = Options(cfg)
        d.accept()
        self.assertEqual((cfg["copy_music"], cfg["verify_copy"], cfg["volume"], cfg["crossfade"]),
                         (False, False, 33, 5))

    def test_player_settings_are_chosen_in_their_own_window_and_saved(self):
        cfg = dict(settings.DEFAULTS)
        d = Options(cfg)
        dlg = PlayerSettings(d.player)
        self.assertEqual({b.currentData() for b in dlg.boxes.values()}, {None})
        dlg.boxes[11].setCurrentIndex(dlg.boxes[11].findData(reference.OFF))
        dlg.boxes[36].setCurrentIndex(dlg.boxes[36].findData(reference.ON))
        self.assertEqual(dlg.chosen(), "11:0,36:1")
        with patch.object(PlayerSettings, "exec", return_value=True), patch.object(
            PlayerSettings, "chosen", return_value="11:0,36:1"
        ):
            d.pick_player()
        d.accept()
        self.assertEqual(cfg["player_settings"], "11:0,36:1")
        again = PlayerSettings(Options(cfg).player)
        self.assertEqual((again.boxes[11].currentData(), again.boxes[36].currentData()), (reference.OFF, reference.ON))
        self.assertIsNone(again.boxes[23].currentData())

    def test_cancelling_the_player_settings_keeps_what_was_saved(self):
        cfg = dict(settings.DEFAULTS, player_settings="23:1")
        d = Options(cfg)
        with patch.object(PlayerSettings, "exec", return_value=False):
            d.pick_player()
        d.accept()
        self.assertEqual(cfg["player_settings"], "23:1")

    def test_a_hand_edited_value_that_is_not_text_falls_back_to_none(self):
        path = Path(tempfile.mkdtemp()) / "cfg.json"
        path.write_text('{"player_settings": 5}', encoding="utf-8")
        with patch.object(settings, "CONFIG", path):
            self.assertEqual(settings.load()["player_settings"], "")

    def test_cfg_without_the_new_keys_still_opens(self):
        cfg = {k: v for k, v in settings.DEFAULTS.items() if k not in ("key_ask", "key_mode")}
        d = Options(cfg)
        self.assertEqual(d.on_key.currentIndex(), 0)
        d.accept()
        self.assertTrue(cfg["key_ask"])


# ------------------------------------------------------------
class MenuAndHelpTests(Base):
    def actions(self):
        found = {}
        for menu_action in self.main.menuBar().actions():
            for action in menu_action.menu().actions():
                found[action.text()] = action
        return found

    def test_menu_entry_follows_the_music_folder_entry(self):
        file_menu = self.main.menuBar().actions()[0].menu()
        texts = [a.text() for a in file_menu.actions()]
        i = texts.index("Open a music &folder...")
        self.assertEqual(texts[i + 2], "Open a CDJ USB &key...")
        self.assertEqual(file_menu.actions()[i + 2].shortcut().toString(), "Ctrl+Shift+K")

    def test_shortcuts_are_unique(self):
        keys = [a.shortcut().toString() for a in self.actions().values() if not a.shortcut().isEmpty()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_help_has_the_new_section(self):
        for part in ("<h3>Keys that already have playlists</h3>", "export.pdb.bak", "--replace", "Do not ask again",
                     "Options &gt; Preferences &gt; Export", "File &gt; Open a CDJ USB key...", "never writes to the key",
                     "<tr><td>Ctrl+Shift+K</td><td>Open a CDJ USB key</td></tr>"):
            self.assertIn(part, USAGE)

    def test_help_has_no_long_dashes(self):
        self.assertNotIn(chr(0x2014), USAGE)
        self.assertNotIn(chr(0x2013), USAGE)

# ------------------------------------------------------------
class OpenKeyTests(Base):
    def setUp(self):
        super().setUp()
        self.key = os.path.join(self.tmp.name, "usbkey")
        os.makedirs(self.key)
        self.main.nodes = []
        self.main.fill_tree()
        self.loaded = [Node("folder", "Crates", children=[Node("playlist", "Warm-up", tracks=[Track(title="W", path="w.wav")])]),
                       Node("playlist", "Peak", tracks=[Track(title="P", path="p.wav")])]
        real_detect = sources.detect
        patches = (patch.object(sources, "detect", lambda p: "key" if p == self.key else real_detect(p)),
                   patch.object(sources, "load", lambda p, root="", progress=None, notes=None: self.loaded))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.picker = patch.object(QFileDialog, "getExistingDirectory", return_value=self.key)
        self.picked = self.picker.start()
        self.addCleanup(self.picker.stop)

    def open_key(self):
        self.main.open_key()
        wait_for(self.app, lambda: self.main.job is None)
        self.app.processEvents()

    def test_the_playlists_appear_in_the_tree_like_a_collection(self):
        self.open_key()
        names = [n.name for n in playlists(self.main.nodes)]
        self.assertEqual(names, ["Warm-up", "Peak"])
        self.assertEqual(self.main.tree.topLevelItemCount(), 2)
        self.assertEqual(self.main.src.text(), self.key)
        self.assertIn("2 playlists", self.main.info.text())
        self.assertIn("read from a CDJ key", self.main.info.text())
        self.assertIn("usbkey", self.main.status.text())
        self.assertIn("key usbkey", self.main.windowTitle())
        self.assertFalse(self.main.dirty)
        self.assertEqual(self.main.project, "")
        self.boxes["critical"].assert_not_called()

    def test_the_dialog_starts_at_the_last_used_folder_without_a_key_output(self):
        self.main.src.setText(self.tmp.name)
        self.open_key()
        self.assertEqual(self.picked.call_args.args[2], self.tmp.name)

    def test_the_dialog_starts_at_the_output_when_it_holds_a_key(self):
        self.main.key_info = (self.out, Summary())
        self.open_key()
        self.assertEqual(self.picked.call_args.args[2], self.out)

    def test_a_folder_without_database_says_so_and_changes_nothing(self):
        with patch.object(export, "key_summary", lambda path, cancel=None: None):
            self.main.src.setText("before.nml")
            self.open_key()
        self.assertEqual(self.main.nodes, [])
        self.assertEqual(self.main.src.text(), "before.nml")
        message = self.boxes["critical"].call_args.args[2]
        self.assertIn("no CDJ database", message)

    def test_an_unreadable_database_is_refused(self):
        self.summary = Summary(readable=False)
        self.open_key()
        self.assertEqual(self.main.nodes, [])
        self.assertIn("cannot be read", self.boxes["critical"].call_args.args[2])

    def test_cancelling_the_picker_does_nothing(self):
        self.picked.return_value = ""
        self.open_key()
        self.assertEqual(self.main.nodes, [])
        self.assertIsNone(self.main.job)

    def test_unsaved_work_is_asked_about_first(self):
        self.main.nodes = [Node("playlist", "Mine")]
        self.main.set_dirty(True)
        self.boxes["question"].return_value = CANCEL
        self.open_key()
        self.assertEqual([n.name for n in self.main.nodes], ["Mine"])
        self.assertEqual(self.main.src.text(), "")

    def test_a_running_job_blocks_it(self):
        self.main.job = object()
        try:
            self.main.open_key()
        finally:
            self.main.job = None
        self.picked.assert_not_called()
        self.assertEqual(self.boxes["information"].call_args.args[2], "Wait for the current job to finish.")

    def test_it_can_be_cancelled_like_a_collection(self):
        started = threading.Event()

        def slow(p, root="", progress=None, notes=None):
            started.set()
            while True:
                progress(5, "Reading the key...")
                time.sleep(0.01)
        with patch.object(sources, "load", slow):
            self.main.open_key()
            wait_for(self.app, started.is_set)
            self.main.cancel()
            wait_for(self.app, lambda: self.main.job is None)
        self.assertEqual(self.main.status.text(), "Loading cancelled")
        self.assertEqual(self.main.nodes, [])

    def test_a_later_plain_load_forgets_the_key(self):
        self.open_key()
        nml = os.path.join(self.tmp.name, "c.nml")
        Path(nml).write_text("<NML></NML>")
        with patch.object(sources, "load", lambda p, root="", progress=None, notes=None: [Node("playlist", "X")]):
            self.main.src.setText(nml)
            self.main.reload()
            wait_for(self.app, lambda: self.main.job is None)
        self.assertEqual(self.main.key_source, "")
        self.assertNotIn("key", self.main.windowTitle())
        self.assertNotIn("CDJ key", self.main.info.text())


# --------------------------------------------------------------
class KeyLabelTests(Base):
    def setUp(self):
        super().setUp()
        self.main.key_timer.stop()
        self.main.key_row.hide()
        self.threads = []
        self.calls = []

    def retype(self, text):
        self.main.out.setText("")
        self.main.out.setText(text)

    def probe(self, summary, expect=True):
        def fake(path, cancel=None):
            self.threads.append(threading.get_ident())
            self.calls.append(path)
            return summary
        with patch.object(export, "key_summary", fake):
            self.main.out.setText("")
            self.main.out.setText(self.out)
            if expect:
                wait_for(self.app, lambda: self.calls and self.main.key_job is None)
            else:
                wait_for(self.app, lambda: False, 0.7)
            self.app.processEvents()

    def test_it_shows_the_summary_computed_off_the_gui_thread(self):
        self.probe(Summary(playlists=5, tracks=120))
        self.assertTrue(self.main.key_row.isVisibleTo(self.main))
        self.assertEqual(self.main.key_label.text(), "Key: 5 playlists, 120 tracks")
        self.assertNotIn(threading.get_ident(), self.threads)

    def test_unreadable_is_in_the_warning_colour(self):
        self.probe(Summary(readable=False))
        self.assertEqual(self.main.key_label.text(), "Key: database cannot be read")
        self.assertIn(main_window.theme.YELLOW, self.main.key_label.styleSheet())

    def test_nothing_without_a_database(self):
        self.probe(None)
        self.assertFalse(self.main.key_row.isVisibleTo(self.main))

    def test_nothing_for_another_format(self):
        self.main.fmt.setCurrentText("M3U")
        self.probe(Summary(), expect=False)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.main.key_row.isVisibleTo(self.main))

    def test_back_to_cdj_shows_it_again(self):
        self.main.fmt.setCurrentText("M3U")
        self.probe(Summary(), expect=False)
        self.assertFalse(self.main.key_row.isVisibleTo(self.main))
        with patch.object(export, "key_summary", lambda path, cancel=None: Summary(playlists=1, tracks=2)):
            self.main.fmt.setCurrentText("CDJ/USB")
            wait_for(self.app, lambda: self.main.key_row.isVisibleTo(self.main))
        self.assertEqual(self.main.key_label.text(), "Key: 1 playlists, 2 tracks")

    def test_typing_is_debounced(self):
        calls = []
        with patch.object(export, "key_summary", lambda path, cancel=None: calls.append(path) or Summary()):
            for i in range(6):
                self.main.out.setText(f"{self.out}{i}")
            wait_for(self.app, lambda: calls and self.main.key_job is None)
            time.sleep(0.05)
            self.app.processEvents()
        self.assertEqual(calls, [f"{self.out}5"])

    def test_a_slow_drive_does_not_block_and_a_late_answer_for_an_old_path_is_dropped(self):
        release = threading.Event()
        first = []

        def slow(path, cancel=None):
            first.append(path)
            if len(first) == 1:
                release.wait(5)
                return Summary(playlists=9, tracks=9)
            return Summary(playlists=1, tracks=1)
        with patch.object(export, "key_summary", slow):
            self.retype(self.out)
            wait_for(self.app, lambda: self.main.key_job is not None)
            began = time.monotonic()
            self.main.out.setText(self.out + "2")
            self.main.probe_key()
            self.assertLess(time.monotonic() - began, 1)
            self.assertTrue(self.main.key_pending)
            release.set()
            wait_for(self.app, lambda: len(first) == 2 and self.main.key_job is None)
            self.app.processEvents()
        self.assertEqual(self.main.key_label.text(), "Key: 1 playlists, 1 tracks")

    def test_it_is_refreshed_after_an_export(self):
        self.probe(Summary(playlists=1, tracks=1))
        with patch.object(export, "key_summary", lambda path, cancel=None: Summary(playlists=2, tracks=7)):
            self.cfg["key_ask"] = False
            self.convert()
            wait_for(self.app, lambda: self.main.key_label.text() == "Key: 2 playlists, 7 tracks")
        self.assertEqual(self.main.key_label.text(), "Key: 2 playlists, 7 tracks")

    def test_the_button_opens_this_key(self):
        self.probe(Summary())
        opened = []
        self.main.open_key = opened.append
        self.main.key_button.click()
        self.assertEqual(opened, [self.out])

    def test_a_new_path_cancels_the_probe_that_reads_the_old_one(self):
        started = threading.Event()
        events = []

        def slow(path, cancel=None):
            events.append((path, cancel))
            started.set()
            if len(events) == 1 and cancel.wait(5):
                raise Cancelled()
            return Summary(playlists=4, tracks=4)
        with patch.object(export, "key_summary", slow):
            self.retype(self.out)
            wait_for(self.app, started.is_set)
            self.main.out.setText(self.out + "2")
            self.assertTrue(wait_for(self.app, lambda: events[0][1].is_set(), 2))
            self.assertTrue(wait_for(self.app, lambda: len(events) == 2 and self.main.key_job is None))
        self.assertEqual(events[1][0], self.out + "2")
        self.assertEqual(self.main.key_label.text(), "Key: 4 playlists, 4 tracks")

    def test_quit_with_only_a_probe_cancels_it_and_closes(self):
        started = threading.Event()
        seen = []

        def slow(path, cancel=None):
            seen.append(cancel)
            started.set()
            cancel.wait(10)
            raise Cancelled()
        with patch.object(export, "key_summary", slow):
            self.retype(self.out)
            wait_for(self.app, started.is_set)
            self.assertTrue(worker.has_active_jobs())
            began = time.monotonic()
            self.assertTrue(self.main.close())
            self.assertLess(time.monotonic() - began, 2.5)
        self.assertTrue(seen[0].is_set())
        self.assertFalse(worker.has_active_jobs())
        self.assertIsNone(self.main.key_job)
        self.assertFalse(self.main.key_timer.isActive())

    def test_quit_does_not_wait_for_a_read_that_ignores_the_cancel(self):
        release = threading.Event()
        started = threading.Event()

        def stuck(path, cancel=None):
            started.set()
            release.wait(10)
            return Summary()
        try:
            with patch.object(export, "key_summary", stuck), patch.object(main_window, "PROBE_WAIT", 200):
                self.retype(self.out)
                wait_for(self.app, started.is_set)
                began = time.monotonic()
                self.assertTrue(self.main.close())
                self.assertLess(time.monotonic() - began, 2)
        finally:
            release.set()
            wait_for(self.app, lambda: not worker.has_active_jobs())

    def test_quit_still_waits_for_a_real_job(self):
        job = hold_a_job()
        self.addCleanup(release_job, self.app, job)
        self.assertFalse(self.main.close())
        self.assertIn("Cannot quit", self.main.status.text())
        self.assertFalse(job.stop.is_set())
        self.assertTrue(worker.has_active_jobs())

    def test_a_probe_next_to_a_real_job_is_left_alone_when_quit_is_refused(self):
        started = threading.Event()

        def slow(path, cancel=None):
            started.set()
            cancel.wait(5)
            raise Cancelled()
        job = hold_a_job()
        self.addCleanup(release_job, self.app, job)
        with patch.object(export, "key_summary", slow):
            self.retype(self.out)
            wait_for(self.app, started.is_set)
            self.assertFalse(self.main.close())
            self.assertFalse(self.main.key_job.stop.is_set())
            self.main.key_job.stop.set()
            wait_for(self.app, lambda: self.main.key_job is None)

    def test_a_running_conversion_blocks_quit(self):
        self.main.job = job = hold_a_job()
        self.addCleanup(release_job, self.app, job)
        try:
            self.assertFalse(self.main.close())
        finally:
            self.main.job = None
        self.assertIn("Cannot quit", self.main.status.text())

    def test_a_probe_still_waiting_to_start_does_not_fire_after_quit(self):
        calls = []
        with patch.object(export, "key_summary", lambda path, cancel=None: calls.append(path) or Summary()):
            self.retype(self.out)
            self.assertTrue(self.main.key_timer.isActive())
            self.assertTrue(self.main.close())
            time.sleep(0.5)
            self.app.processEvents()
        self.assertEqual(calls, [])


# --------------------------------------------------------------
# the analysis in threads and a cache folder of its own
@contextlib.contextmanager
def inline(cache: Path):
    with patch.object(usb, "ProcessPoolExecutor", ThreadPoolExecutor), patch.object(usb, "cache_dir", return_value=cache):
        yield


def make_track(folder: Path, name: str, seed: int) -> Track:
    rate = 11025
    path = folder / f"{name}.wav"
    t = np.arange(int(2.0 * rate)) / rate
    soundfile.write(str(path), 0.4 * np.sin(2 * np.pi * (110 + 40 * seed) * t), rate, subtype="PCM_16")
    return Track(title=name, artist="Artist", path=str(path), samplerate=rate, duration=2.0)


# ------------------------------------------------------------
# the window code path with the real engine: export A, then B, to one key
@unittest.skipUnless(ENGINE and "key" in sources.KINDS, "the key engine is not there yet")
class RealKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / "music").mkdir()
        self.key = base / "key"
        self.a = Node("playlist", "Set A", tracks=[make_track(base / "music", "a1", 1), make_track(base / "music", "a2", 2)])
        self.b = Node("playlist", "Set B", tracks=[make_track(base / "music", "b1", 3)])
        for p in (patch.object(settings, "save"), patch.object(main_window, "fs_type", lambda path: "FAT32"),
                  patch.object(usb, "ProcessPoolExecutor", ThreadPoolExecutor),
                  patch.object(usb, "cache_dir", return_value=base / "cache"),
                  patch.object(QMessageBox, "information"), patch.object(QMessageBox, "warning"),
                  patch.object(QMessageBox, "critical"), patch.object(QMessageBox, "question", return_value=YES)):
            p.start()
            self.addCleanup(p.stop)
        (base / "cache").mkdir()
        self.cfg = dict(settings.DEFAULTS, auto_load=False, confirm_exit=False, key_ask=False, key_mode="keep",
                        verify_copy=False, anlz_processes=1, export_format="CDJ/USB")
        self.main = MainWindow(self.cfg, LogWindow())
        self.main.offer_eject = lambda *a, **k: None
        self.main.verify.setChecked(False)
        self.main.out.setText(str(self.key))
        self.main.key_timer.stop()
        self.addCleanup(self.close_main)

    def close_main(self):
        self.main.key_timer.stop()
        wait_for(self.app, lambda: not worker.has_active_jobs() and self.main.job is None)
        self.main.close()
        self.main.deleteLater()

    def export_playlist(self, node):
        self.main.nodes = [self.a, self.b]
        self.main.fill_tree()
        for i in range(self.main.tree.topLevelItemCount()):
            item = self.main.tree.topLevelItem(i)
            item.setCheckState(0, Qt.Checked if item.data(0, Qt.UserRole) is node else Qt.Unchecked)
        self.main.convert()
        self.assertTrue(wait_for(self.app, lambda: self.main.job is None and self.main.rerun is None, 120))
        self.app.processEvents()

    def test_a_then_b_keeps_both_and_opening_never_writes(self):
        self.export_playlist(self.a)
        self.export_playlist(self.b)
        names = sorted(n.name for n in playlists(sources.load(str(self.key))))
        self.assertEqual(names, ["Set A", "Set B"])
        self.assertTrue((self.key / "PIONEER" / "rekordbox" / "export.pdb.bak").is_file())
        before = tree_hash(self.key)
        self.main.nodes = []
        self.main.src.setText("")
        self.main.open_key(str(self.key))
        self.assertTrue(wait_for(self.app, lambda: self.main.job is None, 60))
        self.app.processEvents()
        self.assertEqual(sorted(n.name for n in playlists(self.main.nodes)), ["Set A", "Set B"])
        self.assertEqual(self.main.tree.topLevelItemCount(), 2)
        self.assertIn("read from a CDJ key", self.main.info.text())
        self.assertEqual(tree_hash(self.key), before)

    def test_the_label_follows_the_key(self):
        self.export_playlist(self.a)
        self.main.probe_key()
        self.assertTrue(wait_for(self.app, lambda: self.main.key_label.text() == "Key: 1 playlists, 2 tracks"))

    def test_replace_through_the_question_leaves_only_the_new_playlist(self):
        self.export_playlist(self.a)
        self.cfg["key_ask"] = True
        with patch.object(KeyChoice, "exec", lambda dlg: (dlg.replace_btn.click(), dlg.result())[1]):
            self.export_playlist(self.b)
        self.assertEqual([n.name for n in playlists(sources.load(str(self.key)))], ["Set B"])
        self.assertTrue((self.key / "PIONEER" / "rekordbox" / "export.pdb.bak").is_file())

    def test_an_unreadable_database_in_keep_mode_offers_replace(self):
        self.export_playlist(self.a)
        db = self.key / "PIONEER" / "rekordbox" / "export.pdb"
        db.write_bytes(b"not a database")
        with patch.object(KeyChoice, "exec", lambda dlg: (dlg.replace_btn.click(), dlg.result())[1]):
            self.export_playlist(self.b)
        self.assertEqual([n.name for n in playlists(sources.load(str(self.key)))], ["Set B"])


if __name__ == "__main__":
    unittest.main()
