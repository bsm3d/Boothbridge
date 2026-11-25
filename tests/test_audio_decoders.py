# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that each supported audio format decodes correctly, with the native decoders run in a child process.

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
from djapp import analysis, beatdetect
from djapp.export.files import Cancelled
from djapp.ui import waveform

from tests import APP_ROOT


# ------------------------------------------------------------
class AudioDecoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def audio(self, extension, format):
        rate = 16000
        time = np.arange(rate, dtype=np.float32) / rate
        left = .25 * np.sin(2 * np.pi * 440 * time)
        samples = np.stack((left, left * .5), axis=1)
        path = self.root / ("stereo" + extension)
        sf.write(path, samples, rate, format=format)
        return path, samples, rate

    def test_real_block_decoder_formats_and_read_only(self):
        for extension, format in [(".wav", "WAV"), (".aiff", "AIFF"), (".flac", "FLAC"),
                                  (".mp3", "MP3"), (".ogg", "OGG")]:
            with self.subTest(format=format):
                path, samples, rate = self.audio(extension, format)
                before = path.read_bytes()
                decoded, folded_rate, frames = analysis.decode(str(path))
                self.assertIsNotNone(decoded)
                self.assertEqual(folded_rate, rate // analysis.FOLD)
                self.assertGreater(frames, 0)
                self.assertGreater(len(decoded), 0)
                self.assertTrue(np.isfinite(decoded).all())
                self.assertGreater(float(np.abs(decoded).max()), .05)
                if format in ("WAV", "AIFF", "FLAC"):
                    expected = samples.reshape(-1, analysis.FOLD * 2).mean(axis=1)
                    np.testing.assert_allclose(decoded, expected, atol=4e-5)
                self.assertEqual(path.read_bytes(), before)

    def test_librosa_fallback_preserves_source_rate_and_fold(self):
        samples = np.arange(32, dtype=np.float32) / 32
        librosa = Mock()
        librosa.load.return_value = (samples, 48000)
        with patch.object(analysis, "read_sf", side_effect=sf.LibsndfileError(1)), \
             patch.dict(sys.modules, {"librosa": librosa, "librosa.core": librosa}):
            decoded, rate, frames = analysis.decode("fallback.m4a")
        librosa.load.assert_called_once_with("fallback.m4a", sr=None, mono=True)
        self.assertEqual((rate, frames), (12000, 32))
        np.testing.assert_allclose(decoded, samples.reshape(-1, analysis.FOLD).mean(axis=1))

    def test_both_decoder_failures_are_logged_and_not_valid_audio(self):
        for error in (OSError, RuntimeError, ValueError, ImportError):
            with self.subTest(error=error.__name__), \
                 patch.object(analysis, "read_sf", side_effect=error("primary decoder failed")), \
                 patch.object(analysis, "read_librosa", side_effect=error("fallback failed")), \
                 self.assertLogs(analysis.log, level="WARNING") as logs:
                audio = analysis.Audio("broken.m4a")
                self.assertFalse(audio.ok)
                self.assertEqual(audio.seconds, 0)
                np.testing.assert_array_equal(audio.at(12).low, np.zeros(12))
            self.assertIn("fallback failed", "\n".join(logs.output))

    def cut_reader(self, good_blocks):
        class Cut(sf.SoundFile):
            reads = 0

            def read(self, *args, **kwargs):
                Cut.reads += 1
                if Cut.reads > good_blocks:
                    raise sf.LibsndfileError(1)
                return super().read(*args, **kwargs)
        return Cut

    def test_file_damaged_after_most_of_it_keeps_what_played_and_the_time_scale(self):
        rate, frames = 8000, 4 * analysis.BLOCK
        samples = (.3 * np.sin(2 * np.pi * 220 * np.arange(frames) / rate)).astype(np.float32)
        path = self.root / "cut.wav"
        sf.write(path, samples, rate, format="WAV")
        with patch.object(analysis.sf, "SoundFile", self.cut_reader(3)), \
             self.assertLogs(analysis.log, level="WARNING") as logs:
            decoded, folded_rate, total = analysis.decode(str(path))
        self.assertIn("damaged", "\n".join(logs.output))
        self.assertEqual((folded_rate, total, len(decoded)), (rate // analysis.FOLD, frames, frames // analysis.FOLD))
        kept = 3 * analysis.BLOCK // analysis.FOLD
        self.assertGreater(float(np.abs(decoded[:kept]).max()), .2)
        np.testing.assert_array_equal(decoded[kept:], np.zeros(len(decoded) - kept))

    def test_file_damaged_early_is_refused_not_stretched(self):
        rate, frames = 8000, 4 * analysis.BLOCK
        path = self.root / "early.wav"
        sf.write(path, np.full(frames, .2, dtype=np.float32), rate, format="WAV")
        with patch.object(analysis.sf, "SoundFile", self.cut_reader(1)), \
             patch.object(analysis, "read_librosa", side_effect=RuntimeError("no fallback")), \
             self.assertLogs(analysis.log, level="WARNING"):
            audio = analysis.Audio(str(path))
        self.assertFalse(audio.ok)

    def test_file_with_no_frames_to_judge_by_is_refused_when_it_cannot_be_read(self):
        path = self.root / "tiny.wav"
        sf.write(path, np.full(2, .2, dtype=np.float32), 8000, format="WAV")
        with patch.object(analysis.sf, "SoundFile", self.cut_reader(0)), \
             patch.object(analysis, "read_librosa", side_effect=RuntimeError("no fallback")), \
             self.assertLogs(analysis.log, level="WARNING"):
            self.assertEqual(analysis.decode(str(path)), (None, 0, 0))

    def test_qt_decoder_comes_after_libsndfile_and_before_librosa(self):
        samples = np.zeros(40, dtype=np.float32)
        with patch.object(analysis, "read_sf", side_effect=sf.LibsndfileError(1)), \
             patch.object(analysis, "read_qt", return_value=(samples, 1, 160, 8000)) as qt, \
             patch.object(analysis, "read_librosa") as librosa:
            decoded, rate, frames = analysis.decode("song.m4a")
        qt.assert_called_once_with("song.m4a")
        librosa.assert_not_called()
        self.assertEqual((len(decoded), rate, frames), (40, 2000, 160))

    def test_qt_decoder_failure_falls_through_to_librosa(self):
        samples = np.zeros(40, dtype=np.float32)
        with patch.object(analysis, "read_sf", side_effect=sf.LibsndfileError(1)), \
             patch.object(analysis, "read_qt", side_effect=RuntimeError("no codec")), \
             patch.object(analysis, "read_librosa", return_value=(samples, 1, 160, 8000)) as librosa:
            decoded, rate, frames = analysis.decode("song.m4a")
        librosa.assert_called_once_with("song.m4a")
        self.assertEqual((len(decoded), rate, frames), (40, 2000, 160))

    def aac(self, share=1.0):
        data = (Path(__file__).with_name("media") / "tone.m4a").read_bytes()
        path = self.root / f"tone{int(share * 100)}.m4a"
        path.write_bytes(data[:int(len(data) * share)])
        return path

    def test_real_aac_file_is_decoded_by_the_qt_decoder(self):
        path = self.aac()
        before = path.read_bytes()
        output = self.probe("decode", str(path)).split()
        self.assertEqual(output[0], "decoded", output)
        length, rate, frames, peak = int(output[1]), int(output[2]), int(output[3]), float(output[4])
        self.assertEqual(rate, 44100 // analysis.FOLD)
        # three seconds, the encoder adds a little padding
        self.assertAlmostEqual(frames / 44100, 3.0, delta=.1)
        self.assertEqual(length, frames // analysis.FOLD)
        # a sine of amplitude .0625 (-24 dBFS), a little more at the attack through the lossy codec
        self.assertGreater(peak, .05)
        self.assertLess(peak, .1)
        self.assertEqual(path.read_bytes(), before)

    def test_real_aac_file_cut_after_most_of_it_keeps_the_time_scale(self):
        output = self.probe("decode", str(self.aac(.6))).split()
        self.assertEqual(output[0], "decoded", output)
        length, rate, frames, peak, tail = int(output[1]), int(output[2]), int(output[3]), float(output[4]), float(output[5])
        # the container says three seconds, the stream stops at 1.7: silence after that
        self.assertAlmostEqual(length / rate, 3.0, delta=.1)
        self.assertAlmostEqual(frames / 44100, 3.0, delta=.1)
        self.assertGreater(peak, .05)
        self.assertEqual(tail, 0)

    def test_real_aac_file_cut_early_is_refused(self):
        self.assertEqual(self.probe("decode", str(self.aac(.3))).split(), ["not", "decoded"])

    def test_real_waveform_overview_and_detail_cache_roundtrip(self):
        path, _, _ = self.audio(".wav", "WAV")
        before = path.read_bytes()
        with patch.object(waveform, "cache_dir", return_value=self.root):
            overview = waveform.compute(str(path))
            detail = waveform.compute_hi(str(path))
            self.assertEqual(len(overview), waveform.COLS)
            self.assertEqual(detail.shape, (waveform.COLS, 3))
            for array in (np.asarray(overview), detail):
                self.assertTrue(np.isfinite(array).all())
                self.assertGreater(float(array.max()), 0)
                self.assertTrue(((array >= 0) & (array <= 1)).all())
            np.testing.assert_allclose(waveform.load(str(path)), overview, atol=1 / 255)
            np.testing.assert_allclose(waveform.load_hi(str(path)), detail, atol=1 / 255)
        self.assertEqual(path.read_bytes(), before)

    def test_waveform_decoder_exceptions_emit_failure_and_thread_finishes(self):
        for thread, load_name, compute_name in [
            (waveform._Thread(["broken"]), "load", "compute"),
            (waveform._HiThread("broken"), "load_hi", "compute_hi"),
        ]:
            results = []
            thread.ready.connect(lambda path, result, output=results: output.append((path, result)))
            with patch.object(waveform, load_name, return_value=None), \
                 patch.object(waveform, compute_name, side_effect=RuntimeError("decode failed")), \
                 self.assertLogs(waveform.log, level="WARNING"):
                thread.run()
            self.assertEqual(results, [("broken", None)])
            self.assertFalse(thread.isRunning())

    def test_beat_detection_decode_error_does_not_discard_other_results(self):
        result = beatdetect.BeatResult(120, 0, .9)
        with patch.object(analysis, "decode", side_effect=[(None, 0, 0), (np.ones(100), 4000, 400)]), \
             patch.object(beatdetect, "detect", return_value=result):
            results = beatdetect.analyze_many([(1, "broken"), (2, "good")])
        self.assertEqual(results, [(1, "The audio file cannot be decoded"), (2, result)])


    def test_beat_detection_cancel_after_decode_does_not_analyze(self):
        cancel = threading.Event()

        def decode(path):
            cancel.set()
            return np.ones(100), 4000, 400

        with patch.object(analysis, "decode", side_effect=decode), \
             patch.object(beatdetect, "detect") as detect, self.assertRaises(Cancelled):
            beatdetect.analyze("track", cancel=cancel)
        detect.assert_not_called()

    def probe(self, *args):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        env["PYTHONPATH"] = str(APP_ROOT)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("audio_process_probe.py")), *args],
            capture_output=True, text=True, timeout=25, env=env, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_real_native_crash_is_reported_and_child_reaped(self):
        self.assertIn("no active children", self.probe("crash"))

    def test_real_qt_multimedia_decoder_reaches_eof(self):
        for extension, format in [(".wav", "WAV"), (".mp3", "MP3")]:
            with self.subTest(format=format):
                path, _, _ = self.audio(extension, format)
                before = path.read_bytes()
                self.assertIn("released source", self.probe("qt", str(path)))
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
