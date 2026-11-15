# the waveform keeps the pace of the audio: a sound late in a track is drawn where it plays.
# Every column took len // n samples and the tail was dropped: 73 samples for 73.5 at 44.1 kHz,
# so a sound at 5:50 was drawn 2.4 s late on the players and in the window

import os
import tempfile
import unittest
import wave
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import analysis, native
from djapp.export.cdj import usb
from djapp.ui import waveform


def clicks(rate: int, seconds: float, marks) -> np.ndarray:
    rng = np.random.default_rng(7)
    y = (rng.standard_normal(int(rate * seconds)) * 0.01).astype("float32")
    for t in marks:
        y[int(t * rate) : int(t * rate) + rate // 50] = 0.9
    return y


def found(bands, per_second: float) -> list[float]:
    top = np.max(np.stack(bands), axis=0)
    cols = np.flatnonzero(top > 0.5)
    first = [cols[0]] + [c for before, c in pairwise(cols) if c - before > 50]
    return [float(c) / per_second for c in first]


class WavePaceTests(unittest.TestCase):
    MARKS = (10.0, 180.0, 350.0)

    def check(self, bands_of):
        # 11025 is a 44.1 kHz file once folded: 73.5 samples for a column
        for rate in (11025, 12000, 8000 // 4):
            with self.subTest(rate=rate):
                y = clicks(rate, 360.0, self.MARKS)
                lo, hi = analysis.filters(rate)
                n = round(360.0 * analysis.DETAIL)
                bands = bands_of(y, lo, hi, n)
                self.assertEqual(len(bands[0]), n)
                for got, want in zip(found(bands, analysis.DETAIL), self.MARKS, strict=True):
                    self.assertAlmostEqual(got, want, delta=1.5 / analysis.DETAIL)

    def test_a_late_sound_is_drawn_where_it_plays(self):
        self.check(analysis.Audio.bands_py)

    @unittest.skipUnless(native.available(), "No built fastcore DLL in this source checkout")
    def test_the_native_core_keeps_the_same_pace(self):
        self.check(native.bands)

    @unittest.skipUnless(native.available(), "No built fastcore DLL in this source checkout")
    def test_the_native_core_and_the_reference_agree_to_the_bit(self):
        for rate, seconds in ((11025, 61.3), (12000, 30.0), (5512, 45.7)):
            y = clicks(rate, seconds, (1.0, seconds / 2, seconds - 1.0))
            lo, hi = analysis.filters(rate)
            for n in (100, 1200, round(seconds * analysis.DETAIL), len(y)):
                with self.subTest(rate=rate, n=n):
                    a = native.bands(y, lo, hi, n)
                    b = analysis.Audio.bands_py(y.copy(), lo, hi, n)
                    for x, z in zip(a, b, strict=True):
                        np.testing.assert_array_equal(x, z)

    def test_the_last_samples_are_in_the_last_column(self):
        rate = 11025
        y = np.zeros(rate * 10, dtype="float32")
        y[-20:] = 0.8  # in the tail that used to be dropped
        lo, hi = analysis.filters(rate)
        n = 10 * analysis.DETAIL
        bands = analysis.Audio.bands_py(y, lo, hi, n)
        self.assertGreater(float(np.max(np.stack(bands), axis=0)[-1]), 0.5)

    def test_the_columns_start_evenly_and_cover_everything(self):
        starts = analysis.column_starts(1000, 7)
        self.assertEqual(starts.tolist(), [0, 142, 285, 428, 571, 714, 857])
        self.assertEqual(analysis.column_starts(10, 10).tolist(), list(range(10)))
        # a long track at a high detail does not overflow
        long = analysis.column_starts(11025 * 3600 * 6, 1000 * 3600 * 6)
        self.assertEqual(int(long[-1]), (1000 * 3600 * 6 - 1) * (11025 * 3600 * 6) // (1000 * 3600 * 6))

    def test_peaks_keep_the_pace_too(self):
        sig = np.zeros(735, dtype="float32")
        sig[730] = -1.0
        got = analysis.peaks(sig, 10)
        self.assertEqual(got.tolist(), [0.0] * 9 + [1.0])
        self.assertEqual(analysis.peaks(np.ones(2), 3).tolist(), [0.0, 0.0, 0.0])

    def test_a_whole_file_through_the_decoder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "late.wav"
            rate = 44100
            y = clicks(rate, 200.0, (5.0, 195.0))
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes((y * 32767).astype("<i2").tobytes())
            a = analysis.Audio(str(path))
            self.assertTrue(a.ok)
            got = found(a.fine, analysis.DETAIL)
            self.assertAlmostEqual(got[0], 5.0, delta=0.02)
            self.assertAlmostEqual(got[1], 195.0, delta=0.02)
            # the overview too: 1200 columns for 200 s
            over = a.at(waveform.COLS)
            top = np.max(np.stack([over.low, over.mid, over.high]), axis=0)
            late = np.flatnonzero(top > 0.5)[-1]
            self.assertAlmostEqual(late / waveform.COLS * 200.0, 195.0, delta=200.0 / waveform.COLS)

    # the caches made at the old pace are not read again
    def test_the_caches_are_made_again(self):
        self.assertEqual(waveform.WAVE_VERSION, 2)
        self.assertEqual(usb.ANLZ_VERSION, 9)
        self.assertEqual(native.VERSION, 3)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.wav"
            path.write_bytes(b"x")
            now = waveform.key(str(path))
            with patch.object(waveform, "WAVE_VERSION", 1):
                self.assertNotEqual(waveform.key(str(path)), now)


if __name__ == "__main__":
    unittest.main()
