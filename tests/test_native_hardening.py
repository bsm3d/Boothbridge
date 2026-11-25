# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks native library loading on Windows: dependency search limits and ABI checks.

import importlib
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import compile_core
import numpy as np
from djapp import integrity, native

from tests import APP_ROOT


# ------------------------------------------------------
class NativeHardeningTests(unittest.TestCase):
    def test_windows_loader_limits_dependency_search_and_checks_abi(self):
        dll = Mock()
        dll.q0.return_value = native.VERSION
        with patch.object(native.sys, "platform", "win32"), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(native.ctypes, "CDLL", return_value=dll) as load:
            self.assertIsNotNone(native.load())
        path = Path(load.call_args.args[0])
        self.assertTrue(path.is_absolute())
        self.assertEqual(load.call_args.kwargs["winmode"], 0x900)
        self.assertEqual(dll.q0.argtypes, [])

    def test_incompatible_or_missing_dll_reports_python_fallback(self):
        dll = Mock()
        dll.q0.return_value = native.VERSION + 1
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(native.ctypes, "CDLL", return_value=dll), \
             self.assertLogs(native.log, level="WARNING"):
            self.assertIsNone(native.load())

    def test_fold_rejects_invalid_inputs_before_native_call(self):
        lib = Mock()
        with patch.object(native, "LIB", lib):
            for width in (0, -1, True, 2**31):
                with self.subTest(width=width), self.assertRaises(ValueError):
                    native.fold(np.ones(8), width)
            for array in (np.ones((2, 2)), np.array([np.nan]), np.array([np.inf])):
                with self.assertRaises(ValueError):
                    native.fold(array, 2)
        lib.bb_fold.assert_not_called()

    def test_fold_rejects_unexpected_native_count(self):
        lib = Mock()
        lib.bb_fold.return_value = -1
        with patch.object(native, "LIB", lib), self.assertRaises(RuntimeError):
            native.fold(np.ones(8), 2)

    def test_bands_rejects_invalid_shapes_filters_sizes_and_status(self):
        lib = Mock()
        sos = np.array([1., 0., 0., 1., 0., 0.])
        with patch.object(native, "LIB", lib):
            for cols in (-1, True, native.MAX_OUTPUT):
                with self.assertRaises(ValueError):
                    native.bands(np.ones(8), sos, sos, cols)
            with self.assertRaises(ValueError):
                native.bands(np.ones(8), sos * np.nan, sos, 2)
            with self.assertRaises(ValueError):
                native.bands(np.ones(8), sos * 2, sos, 2)
            lib.bb_bands.assert_not_called()
            lib.bb_bands.return_value = 0
            with self.assertRaises(RuntimeError):
                native.bands(np.ones(8), sos, sos, 2)

    def test_anlz_rejects_mismatched_band_lengths_and_nonfinite_timing(self):
        lib = Mock()
        band = SimpleNamespace(low=np.ones(2), mid=np.ones(1), high=np.ones(2))
        with patch.object(native, "LIB", lib):
            for waves in ([band], [band] * 4):
                with self.assertRaises(ValueError):
                    native.anlz_build("track", 120., 1., 0., 8000, [], [], waves)
            with self.assertRaises(ValueError):
                native.anlz_build("track", float("nan"), 1., 0., 8000, [], [], [band] * 4)
        lib.bb_anlz_build.assert_not_called()

    def test_output_sizes_are_bounded_before_allocation(self):
        for value in (0, -1, native.MAX_OUTPUT + 1):
            with self.assertRaises(ValueError):
                native._output_size(value)
        self.assertEqual(native._output_size(native.MAX_OUTPUT), native.MAX_OUTPUT)
        lib = Mock()
        lib.bb_pdb_build.return_value = native.MAX_OUTPUT + 1
        with patch.object(native, "LIB", lib), self.assertRaises(ValueError):
            native.pdb_build({}, {}, set())
        self.assertEqual(lib.bb_pdb_build.call_count, 1)

    def test_frozen_build_requires_nonempty_valid_integrity_seal(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.dict(sys.modules, {"djapp._seal": None}):
            self.assertIn("missing integrity seal", integrity.check()[0])
        for hashes in ({}, {"module.pyd": "not a digest"}):
            seal = SimpleNamespace(HASHES=hashes)
            with patch.object(sys, "frozen", True, create=True), \
                 patch.dict(sys.modules, {"djapp._seal": seal}):
                self.assertIn("invalid integrity seal", integrity.check()[0])

    def test_integrity_detects_swap_missing_and_extra_native_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dll = root / "fastcore.dll"
            dll.write_bytes(b"original")
            expected = integrity.scan(root)
            self.assertFalse(integrity.problems(root, expected))
            dll.write_bytes(b"replacement")
            self.assertIn("changed: fastcore.dll", integrity.problems(root, expected))
            dll.unlink()
            (root / "injected.pyd").write_bytes(b"extra")
            self.assertEqual(set(integrity.problems(root, expected)),
                             {"missing: fastcore.dll", "unexpected: injected.pyd"})

    def test_launchers_refuse_untrusted_core_before_app_import(self):
        root = APP_ROOT
        for path in (root / "main.py", root / "djapp" / "__main__.py"):
            with self.subTest(launcher=path.name), \
                 patch.object(integrity, "check", return_value=["changed: app.pyd"]), \
                 patch.object(integrity, "stop", return_value=3) as stop, \
                 patch("multiprocessing.freeze_support"), \
                 patch.dict(sys.modules, {"djapp.app": None}), \
                 self.assertRaises(SystemExit) as exit:
                runpy.run_path(str(path), run_name="__main__")
            self.assertEqual(exit.exception.code, 3)
            stop.assert_called_once()
        self.assertIn("integrity.py", compile_core.KEEP)

    def test_windows_build_command_enables_binary_mitigations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def compile(command, **kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"test build output")
                return SimpleNamespace(returncode=0)

            with patch.object(compile_core.sys, "platform", "win32"), \
                 patch.object(compile_core, "find_cxx", return_value=["g++"]), \
                 patch.object(compile_core.subprocess, "run", side_effect=compile) as run:
                compile_core.build_native(root)
            command = run.call_args.args[0]
            for flag in ("-fstack-protector-strong", "-D_FORTIFY_SOURCE=2",
                         "-Wl,--dynamicbase", "-Wl,--nxcompat", "-Wl,--high-entropy-va"):
                self.assertIn(flag, command)

    @unittest.skipUnless(native.available(), "No built fastcore DLL in this source checkout")
    def test_real_dll_fold_and_bands_match_python_reference(self):
        analysis = importlib.import_module("djapp.analysis")
        samples = np.sin(np.arange(4000, dtype=np.float32) * .1)
        np.testing.assert_allclose(native.fold(samples, 4), samples.reshape(-1, 4).mean(axis=1),
                                   atol=1e-6)
        lo, hi = analysis.filters(4000)
        actual = native.bands(samples, lo, hi, 100)
        expected = analysis.Audio.bands_py(samples, lo, hi, 100)
        np.testing.assert_allclose(actual, expected, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
