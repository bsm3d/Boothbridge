# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the detailed waveform, computed from the audio rather than upsampled from CDJ columns.

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import soundfile as sf
from djapp.analysis import Audio
from djapp.ui import waveform


# --------------------------------------------------------------
class DetailedWaveformTests(unittest.TestCase):
    def test_detail_is_computed_from_audio_not_upsampled_from_cdj_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "test.wav")
            rate = 48000
            samples = np.zeros(rate * 2, dtype=np.float32)
            samples[rate // 2:rate // 2 + 48] = 0.8
            sf.write(path, samples, rate)
            self.assertEqual(len(Audio(path).fine[0]), 300)
            self.assertEqual(len(Audio(path, detail=1000).fine[0]), 2000)
            with patch.object(waveform, "cache_dir", return_value=Path(tmp)):
                arr = waveform.compute_hi(path)
                self.assertEqual(arr.shape, (2000, 3))
                self.assertTrue(arr.any())
                self.assertTrue(waveform.hi_file(path).is_file())
                self.assertEqual(waveform.load_hi(path).shape, (2000, 3))

    def test_previous_low_resolution_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.wav"
            path.touch()
            with patch.object(waveform, "cache_dir", return_value=Path(tmp)):
                target = waveform.hi_file(str(path))
                target.with_suffix(".hi").write_bytes(bytes(1200 * 3))
                self.assertIsNone(waveform.load_hi(str(path)))
                self.assertEqual(target.suffix, ".hi1000")

    def test_prefetch_skips_a_file_that_went_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "present.wav"
            present.touch()
            loader = waveform.Loader()
            with patch.object(waveform, "cache_dir", return_value=Path(tmp)), \
                 patch.object(loader, "pump"):
                loader.prefetch([str(Path(tmp) / "gone.wav"), str(present)])
            self.assertEqual(loader.queue, [str(present)])


if __name__ == "__main__":
    unittest.main()
