import ast
import multiprocessing
import os
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
from djapp.audiocheck import (
    _memory_bytes,
    check_file,
    find_clamav,
    isolated_check,
    scan,
)
from djapp.export.files import Cancelled

from tests import APP_ROOT


def _crash_check(connection, path, full):
    os._exit(3)


# ----------------------------------------------------------------
class AudioCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def audio(self, extension, format):
        path = self.root / ("valid" + extension)
        sf.write(path, np.zeros(8000), 8000, format=format)
        return path

    def test_valid_formats_and_read_only(self):
        for extension, format in [(".wav", "WAV"), (".aiff", "AIFF"), (".flac", "FLAC"),
                                  (".mp3", "MP3"), (".ogg", "OGG")]:
            with self.subTest(format=format):
                path = self.audio(extension, format)
                before = path.read_bytes()
                self.assertEqual(check_file(str(path), False).status, "Headers readable")
                self.assertEqual(check_file(str(path), True).status, "Decoded")
                self.assertEqual(path.read_bytes(), before)

    def test_truncated_containers(self):
        for extension, format in [(".wav", "WAV"), (".aiff", "AIFF")]:
            path = self.audio(extension, format)
            path.write_bytes(path.read_bytes()[:-1000])
            for full in [False, True]:
                self.assertEqual(check_file(str(path), full).status, "Suspect")

    def test_missing_empty_and_invalid(self):
        self.assertEqual(check_file(str(self.root / "missing.mp3"), True).status, "Missing")
        empty = self.root / "empty.mp3"
        empty.touch()
        self.assertEqual(check_file(str(empty), True).status, "Suspect")
        invalid = self.root / "invalid.mp3"
        invalid.write_bytes(b"not audio")
        self.assertEqual(check_file(str(invalid), True).status, "Suspect")

    def test_unsupported_decoder_is_not_success(self):
        path = self.audio(".wav", "WAV")
        with patch("djapp.audiocheck.sf.SoundFile",
                   side_effect=sf.LibsndfileError(1)):
            self.assertEqual(check_file(str(path), True).status, "Not checked")

    def test_mp3_estimated_frames_are_not_a_corruption_error(self):
        path = self.audio(".mp3", "MP3")
        for read, expected in [(20787887, 20788128), (18489647, 18489954),
                               (21859247, 21859484)]:
            with self.subTest(read=read):
                decoder = MagicMock()
                decoder.__enter__.return_value = decoder
                decoder.format = "MP3"
                decoder.frames = expected
                decoder.channels = 2
                decoder.samplerate = 44100
                block = MagicMock()
                block.__len__.return_value = read
                decoder.read.side_effect = [block, []]
                with patch("djapp.audiocheck.sf.SoundFile", return_value=decoder):
                    result = check_file(str(path), True)
                self.assertEqual(result.status, "Decoded")
                self.assertIn("header estimate", result.detail)
                self.assertIn(str(read), result.detail)
                self.assertIn(str(expected), result.detail)

    def test_pcm_frame_mismatch_remains_suspect(self):
        path = self.audio(".wav", "WAV")
        decoder = MagicMock()
        decoder.__enter__.return_value = decoder
        decoder.format = "WAV"
        decoder.frames = 8000
        decoder.channels = 1
        decoder.samplerate = 8000
        decoder.read.side_effect = [np.zeros((7900, 1)), []]
        with patch("djapp.audiocheck.sf.SoundFile", return_value=decoder):
            self.assertEqual(check_file(str(path), True).status, "Suspect")

    def test_isolation_time_and_memory_limits(self):
        path = self.audio(".wav", "WAV")
        self.assertEqual(isolated_check(str(path), True).status, "Decoded")
        timed = isolated_check(str(path), True, timeout=0)
        self.assertEqual(timed.status, "Not checked")
        self.assertIn("Time limit", timed.detail)
        with patch("djapp.audiocheck._memory_bytes", return_value=2 * 1024**3):
            limited = isolated_check(str(path), True)
        self.assertEqual(limited.status, "Not checked")
        self.assertIn("Memory limit", limited.detail)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_missing_monitor_and_cancellation_clean_up_child(self):
        path = self.audio(".wav", "WAV")
        with patch("djapp.audiocheck._memory_bytes", side_effect=OSError("monitor unavailable")):
            result = isolated_check(str(path), True)
        self.assertEqual(result.status, "Not checked")
        self.assertIn("Cannot enforce memory monitoring", result.detail)
        cancel = threading.Event()

        def cancel_on_monitor(pid):
            cancel.set()
            return _memory_bytes(pid)

        with patch("djapp.audiocheck._memory_bytes", side_effect=cancel_on_monitor), \
                self.assertRaises(Cancelled):
            isolated_check(str(path), True, cancel=cancel)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_crashed_child_is_not_checked(self):
        with patch("djapp.audiocheck._child_check", _crash_check):
            result = isolated_check(str(self.root / "audio.wav"), True)
        self.assertEqual(result.status, "Not checked")
        self.assertIn("without a result", result.detail)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_start_failure_surfaces_and_closes_process_resources(self):
        context = MagicMock()
        receiver, sender = MagicMock(), MagicMock()
        context.Pipe.return_value = (receiver, sender)
        process = context.Process.return_value
        process.pid = None
        process.start.side_effect = OSError("cannot spawn")
        with patch("djapp.audiocheck.multiprocessing.get_context", return_value=context), \
                self.assertRaisesRegex(OSError, "cannot spawn"):
            isolated_check("test.wav", True)
        receiver.close.assert_called_once_with()
        sender.close.assert_called_once_with()
        process.close.assert_called_once_with()
        process.join.assert_not_called()

    def test_clamav_detection_does_not_execute_it(self):
        with patch("djapp.audiocheck.shutil.which", return_value="C:\\ClamAV\\clamscan.exe"), \
                patch("djapp.audiocheck.subprocess.run") as execute:
            self.assertEqual(find_clamav(), "C:\\ClamAV\\clamscan.exe")
        execute.assert_not_called()
        with patch("djapp.audiocheck.shutil.which", return_value=None), \
             patch("djapp.audiocheck.os.path.isfile", return_value=False):
            self.assertEqual(find_clamav(), "")
        with patch("djapp.audiocheck.shutil.which", return_value=None), \
                patch("djapp.audiocheck.sys.platform", "win32"), \
                patch.dict(os.environ, {"ProgramFiles": "C:\\Programs"}), \
                patch("djapp.audiocheck.os.path.isfile", return_value=True):
            self.assertEqual(find_clamav(), os.path.join("C:\\Programs", "ClamAV", "clamscan.exe"))

    def test_safety_parameters_do_not_allocate_audio(self):
        path = self.audio(".wav", "WAV")
        for channels, rate, frames in [(33, 44100, 100), (2, 1000000, 100),
                                       (2, 44100, 44100 * 86401)]:
            decoder = MagicMock()
            decoder.__enter__.return_value = decoder
            decoder.channels, decoder.samplerate, decoder.frames = channels, rate, frames
            with patch("djapp.audiocheck.sf.SoundFile", return_value=decoder):
                self.assertEqual(check_file(str(path), True).status, "Not checked")
            decoder.read.assert_not_called()

    def test_launcher_calls_freeze_support_before_importing_application(self):
        launcher = APP_ROOT / "main.py"
        with patch("multiprocessing.freeze_support") as freeze:
            runpy.run_path(str(launcher), run_name="__mp_main__")
        freeze.assert_not_called()
        with patch("multiprocessing.freeze_support", side_effect=RuntimeError("spawn handled")) as freeze, \
                self.assertRaisesRegex(RuntimeError, "spawn handled"):
            runpy.run_path(str(launcher), run_name="__main__")
        freeze.assert_called_once_with()

    def test_compiled_module_enumeration_and_windows_monitor_import(self):
        import compile_core

        root = APP_ROOT
        with patch.object(compile_core, "PKG", root / "djapp"):
            modules = compile_core.core_files()
        self.assertIn(root / "djapp" / "audiocheck.py", modules)
        self.assertIn(root / "djapp" / "ui" / "audiocheck.py", modules)
        spec = ast.parse((root / "boothbridge.spec").read_text(encoding="utf-8"))
        imports = next(ast.literal_eval(node.value) for node in spec.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "core_imports"
                               for target in node.targets))
        self.assertIn("multiprocessing", imports)
        self.assertIn("ctypes.wintypes", imports)

    def test_folder_and_duplicate_paths(self):
        path = self.audio(".wav", "WAV")
        child = self.root / "nested"
        child.mkdir()
        (child / "copy.WAV").write_bytes(path.read_bytes())
        (child / "ignore.txt").write_text("not music")
        progress = []
        self.assertEqual(len(scan([], str(self.root), True,
                                  progress=lambda pct, msg: progress.append(pct))), 2)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(len(scan([str(path), str(path)], "", False)), 1)
        with self.assertRaises(OSError):
            scan([], str(self.root / "missing"), False)

    def test_cancel_before_scan_and_during_decode(self):
        path = self.audio(".wav", "WAV")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            scan([str(path)], "", True, cancel=cancel)
        cancel.clear()
        original = sf.SoundFile.read

        def read(audio, *args, **kwargs):
            cancel.set()
            return original(audio, *args, **kwargs)

        with patch.object(sf.SoundFile, "read", read), self.assertRaises(Cancelled):
            check_file(str(path), True, cancel)


if __name__ == "__main__":
    unittest.main()
