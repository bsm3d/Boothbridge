# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Self tests of the test center tools: monkey driver, random scenarios, memory runs and command line.

# The long runs happen in child processes, as they do for the user, and the independent ones run side by side.
# The whole file stays well under half a minute.

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tests import center, test_center
from tests.center import fuzz, memory

ROOT = Path(__file__).resolve().parents[1]
CHILD_ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
if os.name == "nt":
    CHILD_ENV.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", "."), "Fonts"))

EXTRA = textwrap.dedent('''
    from tests.center import fuzz, memory

    LEAK = []

    def boom(seed, iterations, deadline):
        return [fuzz.Failure("boom", seed + 2, "injected", "this scenario always fails", "detail")]

    def leak(ctx, cycle):
        LEAK.append(bytearray(1 << 20))

    def clean(ctx, cycle):
        sum(range(1000))

    fuzz.register("boom", "Scénario qui échoue", boom)
    memory.register("leak", "Fuite injectée", leak)
    memory.register("clean", "Rien", clean)
''')


# ------------------------------------------------------------
def cli(*args, extra=None) -> subprocess.Popen:
    command = [sys.executable, "tests/test_center.py", *map(str, args)]
    if extra:
        command += ["--extra", str(extra)]
    return subprocess.Popen(command, cwd=ROOT, env=CHILD_ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


# ------------------------------------------------------------
def finish(process: subprocess.Popen, limit: float = 150.0) -> tuple[int, str]:
    try:
        out, _ = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        process.kill()
        out, _ = process.communicate()
        return -9, out
    return process.returncode, out


def steps_of(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))["details"]["steps"]
    return [(s["act"], s.get("win"), s.get("path"), json.dumps(s.get("arg"), sort_keys=True)) for s in data]


# ------------------------------------------------------------
class ToolsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.extra = cls.root / "extra.py"
        cls.extra.write_text(EXTRA, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()


# --------------------------------------------------------
class MonkeyTests(ToolsCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings = ROOT / "boothbridge.json"
        cls.settings_before = settings.stat().st_mtime_ns if settings.exists() else None
        cls.first = cls.root / "first.json"
        cls.second = cls.root / "second.json"
        cls.broken = cls.root / "broken.json"
        cls.small = cls.root / "reduced.json"
        cls.frozen = cls.root / "frozen.json"
        runs = [cli("--monkey", "--seed", 5, "--steps", 40, "--settle", 4000, "--out", cls.first),
                cli("--monkey", "--seed", 5, "--steps", 40, "--settle", 4000, "--out", cls.second),
                cli("--monkey", "--seed", 5, "--steps", 40, "--settle", 4000, "--inject", "click", "--out", cls.broken),
                cli("--monkey", "--seed", 5, "--steps", 8, "--settle", 4000, "--hang", 3, "--inject", "stall:click",
                    "--out", cls.frozen)]
        cls.codes = [finish(r) for r in runs]
        cls.reduce = finish(cli("--monkey", "--reduce", cls.broken, "--out", cls.small))

    def test_headless_run_writes_a_complete_result_file(self):
        code, text = self.codes[0]
        self.assertIn(code, (0, 1), text[-800:])
        record = json.loads(self.first.read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "monkey")
        self.assertEqual(record["seed"], 5)
        self.assertEqual(len(record["details"]["steps"]), 40)
        self.assertIn(record["verdict"], ("ok", "echec"))
        self.assertIn("pyside6", record["versions"])
        self.assertIn("graine : 5", text)
        step = record["details"]["steps"][0]
        for key in ("act", "win", "rng", "i"):
            self.assertIn(key, step)

    def test_same_seed_gives_the_same_step_list(self):
        self.assertEqual(steps_of(self.first), steps_of(self.second))

    def test_injected_failure_is_found_with_its_stack(self):
        code, text = self.codes[2]
        self.assertEqual(code, 1, text[-800:])
        record = json.loads(self.broken.read_text(encoding="utf-8"))
        failures = record["details"]["failures"]
        self.assertTrue(failures)
        self.assertIn("injected failure", failures[0]["message"])
        self.assertIn("RuntimeError", failures[0]["stack"])
        self.assertEqual(record["verdict"], "echec")


    def test_frozen_window_is_reported_as_a_hang_with_the_stacks(self):
        code, text = self.codes[3]
        self.assertEqual(code, 1, text[-800:])
        record = json.loads(self.frozen.read_text(encoding="utf-8"))
        hangs = [f for f in record["details"]["failures"] if f["type"] == "hang"]
        self.assertTrue(hangs, record["details"]["failures"])
        self.assertIn("sleep", hangs[0]["stack"])
        self.assertIn("-- thread", hangs[0]["stack"])

    def test_reduce_shrinks_the_failing_run(self):
        code, text = self.reduce
        self.assertEqual(code, 1, text[-800:])
        record = json.loads(self.small.read_text(encoding="utf-8"))
        steps = record["details"]["steps"]
        self.assertLessEqual(len(steps), 3)
        self.assertGreaterEqual(record["details"]["reduced_from"], 40)
        self.assertTrue(any("injected failure" in f["message"] for f in record["details"]["failures"]))

    def test_isolation_never_touches_the_real_settings(self):
        settings = ROOT / "boothbridge.json"
        after = settings.stat().st_mtime_ns if settings.exists() else None
        self.assertEqual(after, self.settings_before)


# ------------------------------------------------------------
class RandomTests(ToolsCase):
    def test_every_scenario_runs_a_few_iterations(self):
        for name in fuzz.names():
            with self.subTest(scenario=name):
                result = fuzz.run([name], 3, 3)
                entry = result["scenarios"][0]
                self.assertIn(entry["status"], ("ok", "echec", "saute"), entry.get("message"))
                if entry["status"] != "saute":
                    self.assertGreater(entry["cases"], 0)
                for f in entry["failures"]:
                    self.assertIn("seed", f)
                    self.assertIn("--seed", f["replay"])

    def test_injected_failing_scenario_is_reported_with_its_seed(self):
        def boom(seed, iterations, deadline):
            return [fuzz.Failure("boom", seed + 4, "case", "always fails", "detail")]

        with patch.object(fuzz, "SCENARIOS", [*fuzz.SCENARIOS, ("boom", "Boom", boom)]):
            result = fuzz.run(["boom"], 100, 5)
        entry = result["scenarios"][0]
        self.assertEqual(entry["status"], "echec")
        self.assertEqual(result["verdict"], "echec")
        self.assertEqual(entry["failures"][0]["seed"], 104)
        self.assertIn("--seed 104", entry["failures"][0]["replay"])

    def test_missing_function_skips_with_a_message(self):
        def needs(seed, iterations, deadline):
            fuzz.need("djapp.sources.project", "no_such_function")

        with patch.object(fuzz, "SCENARIOS", [("needs", "Needs", needs)]):
            entry = fuzz.run(["needs"], 1, 1)["scenarios"][0]
        self.assertEqual(entry["status"], "saute")
        self.assertIn("no_such_function", entry["message"])

    def test_a_function_the_application_does_not_have_skips_too(self):
        def reaches(seed, iterations, deadline):
            from djapp.sources import project
            return project.not_there(seed)

        with patch.object(fuzz, "SCENARIOS", [("reaches", "Reaches", reaches)]):
            entry = fuzz.run(["reaches"], 1, 1)["scenarios"][0]
        self.assertEqual(entry["status"], "saute")
        self.assertIn("not_there", entry["message"])

    def test_unknown_scenario_is_an_error(self):
        with self.assertRaises(ValueError):
            fuzz.run(["nope"], 1, 1)

    def test_a_scenario_is_replayable_from_its_seed(self):
        first = fuzz.run(["project"], 11, 3)["scenarios"][0]
        again = fuzz.run(["project"], 11, 3)["scenarios"][0]
        self.assertEqual((first["status"], first["failures"]), (again["status"], again["failures"]))

    def test_cue_model_catches_a_broken_history(self):
        from djapp.ui import cuehistory
        original = cuehistory.undo
        with patch.object(cuehistory, "undo", lambda t: original(t) and False):
            result = fuzz.run(["cues"], 1, 20)
        self.assertEqual(result["scenarios"][0]["status"], "echec")


# ------------------------------------------------------------
class MemoryTests(ToolsCase):
    def measure(self, cycle, **kwargs):
        started = tracemalloc.is_tracing()
        try:
            return memory.measure(cycle, 6, warmup=1, **kwargs)
        finally:
            if not started and tracemalloc.is_tracing():
                tracemalloc.stop()

    def test_injected_leak_is_called_a_leak_and_clean_code_is_stable(self):
        hoard = []
        leak = self.measure(lambda i: hoard.append(bytearray(1 << 20)))
        self.assertEqual(leak["verdict"], "fuite probable")
        self.assertTrue(any("tas Python" in e for e in leak["evidence"]), leak["evidence"])
        self.assertEqual(len(leak["rows"]), 6)
        clean = self.measure(lambda i: sum(range(1000)))
        self.assertEqual(clean["verdict"], "stable", clean["evidence"])

    def test_fit_and_limits(self):
        slope, r2 = memory.fit([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(slope, 1.0)
        self.assertAlmostEqual(r2, 1.0)
        self.assertEqual(memory.fit([5.0, 5.0, 5.0]), (0.0, 1.0))
        rows = [{"cycle": i, "rss": 1, "private": 1, "heap": 0, "widgets": 10 + i, "py_widgets": 0, "qobjects": 0,
                 "threads": 1, "os_threads": 1, "handles": 5, "children": 0} for i in range(8)]
        self.assertEqual(memory.verdict(rows, 2, memory.Limits())["verdict"], "fuite probable")
        self.assertEqual(memory.verdict(rows, 2, memory.Limits(widgets=100))["verdict"], "stable")
        self.assertEqual(memory.verdict(rows[:3], 2, memory.Limits())["verdict"], "indéterminé")

    def test_command_line_exit_codes_and_record(self):
        runs = [cli("--memory", "--scenario", name, "--cycles", 5, "--warmup", 1, "--out", self.root / f"{name}.json",
                    extra=self.extra) for name in ("leak", "clean")]
        (leak_code, leak_text), (clean_code, clean_text) = (finish(r) for r in runs)
        self.assertEqual(leak_code, 1, leak_text[-600:])
        self.assertEqual(clean_code, 0, clean_text[-600:])
        record = json.loads((self.root / "leak.json").read_text(encoding="utf-8"))
        self.assertEqual(record["details"]["verdict"], "fuite probable")
        self.assertEqual(record["params"]["scenario"], "leak")
        self.assertIn("allocations", record["details"])


# ------------------------------------------------------------
class CommandLineTests(ToolsCase):
    def test_random_exit_codes_and_printed_seed(self):
        good = cli("--random", "--scenario", "cues", "--seed", 7, "--iterations", 3, "--out", self.root / "good.json")
        bad = cli("--random", "--scenario", "boom", "--seed", 7, "--iterations", 1, "--out", self.root / "bad.json",
                  extra=self.extra)
        wrong = cli("--random", "--scenario", "nothing", "--seed", 7, "--out", self.root / "wrong.json")
        (code, text), (bad_code, bad_text), (wrong_code, _) = finish(good), finish(bad), finish(wrong)
        self.assertEqual(code, 0, text[-600:])
        self.assertIn("graine : 7", text)
        self.assertEqual(bad_code, 1, bad_text[-600:])
        self.assertIn("graine 7", bad_text)
        self.assertEqual(wrong_code, 2)
        record = json.loads((self.root / "good.json").read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "random")
        self.assertEqual(record["seed"], 7)
        self.assertEqual(record["verdict"], "ok")

    def test_runs_are_listed_newest_first_and_saved_as_json(self):
        with patch.object(center, "RUNS", self.root / "runs"):
            first = center.new_run_path("random")
            center.save_run(first, {"kind": "random", "verdict": "ok"})
            second = center.new_run_path("monkey")
            center.save_run(second, {"kind": "monkey", "verdict": "echec"})
            names = [p.name for p in center.list_runs()]
        self.assertEqual(len(names), 2)
        self.assertEqual(names, sorted(names, reverse=True))
        self.assertTrue(names[0].endswith(".json"))

# --------------------------------------------------------
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(center, "RUNS", Path(self.temp.name) / "runs")
        patcher.start()
        self.addCleanup(patcher.stop)
        report = patch.object(test_center, "REPORT", Path(self.temp.name) / "report.txt")
        report.start()
        self.addCleanup(report.stop)
        self.window = test_center.TestCenter()
        self.addCleanup(self.window.close)

    def test_all_tabs_and_buttons_exist(self):
        w = self.window
        self.assertEqual([w.tabs.tabText(i) for i in range(w.tabs.count())],
                         ["Suites", "Aléatoire", "Monkey", "Mémoire", "Rapports"])
        for button in (w.run_all_button, w.run_selected_button, w.retry_button, w.manual_button, w.open_app_button,
                       w.stop_button, w.copy_button, w.check_all_button, w.check_none_button):
            self.assertTrue(button.text())
        self.assertEqual(w.retry_button.text(), "Relancer les échecs")
        for panel in (w.random, w.monkey, w.memory):
            self.assertTrue(panel.run_button.text())
            self.assertTrue(panel.stop_button.text())
        self.assertTrue(w.monkey.reduce_button.isVisibleTo(w.monkey) or w.monkey.reduce_button.text() == "Réduire")
        self.assertEqual(w.monkey.replay_button.text(), "Rejouer le dernier")
        self.assertEqual(w.reports.replay_button.text(), "Rejouer")
        self.assertTrue(w.reports.open_button.text())
        windows = 7
        self.assertEqual(w.monkey.windows.count(), windows)
        self.assertEqual(w.random.scenarios.count(), len(fuzz.SCENARIOS))
        self.assertEqual(w.memory.scenario.count(), len(memory.SCENARIOS))

    def test_suites_tab_still_discovers_the_unittest_modules(self):
        found = {p.stem for p in Path(test_center.TESTS).glob("test_*.py")} - {"test_center", "test_key_support"}
        self.assertEqual(set(self.window.items), found)
        self.assertIn("test_center_tools", self.window.items)
        self.assertIn("test_regression", self.window.items)
        listed = [(name, label) for name, label in test_center.available_suites()]
        self.assertEqual({n for n, _ in listed}, found)
        self.assertNotIn("test_key_support", self.window.items)  # helpers only, nothing to run
        # every suite has a label in French, none is the module name made readable
        plain = [n for n, label in listed if label == n.removeprefix("test_").replace("_", " ").capitalize()]
        self.assertEqual(plain, [])
        self.assertEqual(self.window.items["test_backup"].text(), "Backup ZIP, SHA-256, vérification et restauration")

    def test_filter_narrows_what_all_runs(self):
        w = self.window
        w.filter.setText("hostile")
        self.assertEqual(w.visible_suites(), ["test_hostile_json"])
        w.filter.setText("")
        self.assertEqual(len(w.visible_suites()), len(w.items))

    def test_suite_run_fills_the_tree_and_the_retry_list(self):
        w = self.window
        w.start_tests([("test_theme", [])])
        end = time.monotonic() + 90
        while w.worker.isRunning() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        top = w.results_tree.topLevelItem(0)
        self.assertEqual(top.text(1), "réussie")
        self.assertGreaterEqual(top.childCount(), 1)
        self.assertEqual(w.counts["echec"] + w.counts["erreur"], 0)
        self.assertIn("réussis", w.summary.text())
        self.assertFalse(w.retry_button.isEnabled())
        saved = list((Path(self.temp.name) / "runs").glob("*-suites.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text(encoding="utf-8"))["kind"], "suites")

    def test_failed_tests_can_be_run_again(self):
        w = self.window
        w.failed_ids = {"test_theme": ["tests.test_theme.ScrollbarThemeTests.test_x"]}
        w.retry_button.setEnabled(True)
        started = []
        with patch.object(w, "start_tests", lambda jobs: started.append(jobs)):
            w.run_failed()
        self.assertEqual(started, [[("test_theme", ["tests.test_theme.ScrollbarThemeTests.test_x"])]])

    def test_a_panel_shows_a_crash_without_a_record(self):
        panel = self.window.monkey
        panel.out = Path(self.temp.name) / "none.json"
        panel.command = ["tests/test_center.py", "--monkey"]
        panel.log.setPlainText("Windows fatal exception: access violation")
        panel.on_done(3221225477, False, False)
        record = json.loads(panel.out.read_text(encoding="utf-8"))
        self.assertEqual(record["verdict"], "plantage")
        self.assertIn("access violation", record["log_tail"])
        panel.on_done(-1, True, False)
        self.assertEqual(json.loads(panel.out.read_text(encoding="utf-8"))["verdict"], "timeout")

    def test_reports_list_and_replay_request(self):
        w = self.window
        path = center.new_run_path("random")
        center.save_run(path, {"kind": "random", "verdict": "echec", "seed": 9, "summary": "x", "params": {
            "scenario": "cues", "iterations": 5}, "started": "2026-01-01T10:00:00+00:00"})
        w.reports.refresh()
        self.assertEqual(w.reports.tree.topLevelItemCount(), 1)
        w.reports.tree.setCurrentItem(w.reports.tree.topLevelItem(0))
        self.assertIn("Graine : 9", w.reports.detail.toPlainText())
        seen = []
        with patch.object(w.random, "replay", lambda record: seen.append(record["seed"])):
            w.reports.replay()
        self.assertEqual(seen, [9])
        self.assertEqual(w.tabs.currentIndex(), 1)

    def test_curve_draws_without_error(self):
        w = self.window
        rows = [{"cycle": i, "rss": 1e8 + i * 1e5, "private": 1e8 + i * 2e5, "heap": 1e6, "widgets": 5, "py_widgets": 5,
                 "qobjects": 9, "threads": 1, "os_threads": 2, "handles": 3, "children": 0, "child_names": []}
                for i in range(10)]
        w.memory.curve.set_data(rows, 2)
        w.memory.curve.resize(500, 250)
        self.assertFalse(w.memory.curve.grab().isNull())
        for i in range(w.memory.series.count()):
            w.memory.series.setCurrentIndex(i)
            self.assertFalse(w.memory.curve.grab().isNull())


if __name__ == "__main__":
    with contextlib.suppress(SystemExit):
        unittest.main()
