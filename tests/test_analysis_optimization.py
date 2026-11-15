import unittest
from unittest.mock import patch

import numpy as np
from djapp import analysis, beatdetect
from scipy.signal import sosfilt


# ----------------------------------------------------------------
def reference_refine(flux, period):
    def concentration(hist):
        total = float(hist.sum())
        if total <= 0:
            return 0.
        return float((hist + np.roll(hist, 1) + np.roll(hist, -1)).max()) / total

    candidates = period / (1 + np.linspace(-beatdetect.REFINE_SPAN, beatdetect.REFINE_SPAN, 121))
    scores = [concentration(beatdetect.fold(flux, p, beatdetect.PHASE_BINS))
              for p in candidates]
    best = candidates[int(np.argmax(scores))]
    spread = candidates[1] / candidates[0] - 1
    fine = best / (1 + np.linspace(-spread, spread, 41))
    scores = [concentration(beatdetect.fold(flux, p, beatdetect.PHASE_BINS)) for p in fine]
    index = int(np.argmax(scores))
    return float(fine[index]), scores[index]


# ----------------------------------------------------------------
def reference_bands(y, lo_sos, hi_sos, n):
    lo = sosfilt(lo_sos, y).astype("float32")
    hi = sosfilt(hi_sos, y).astype("float32")
    mi = y - lo - hi
    # column c is the samples from c * len / n to (c + 1) * len / n, written out the slow way
    fine = []
    for band in (lo, mi, hi):
        if len(band) < n:
            fine.append(np.zeros(n, dtype="float32"))
            continue
        fine.append(
            np.array(
                [np.abs(band[c * len(band) // n : (c + 1) * len(band) // n]).max() for c in range(n)],
                dtype="float32",
            )
        )
    top = max(float(b.max()) for b in fine) or 1.0
    return [np.clip(b / top, 0, 1) for b in fine]


# ----------------------------------------------------------------
class AnalysisOptimizationTests(unittest.TestCase):
    def test_refinement_matches_reference_exactly(self):
        rng = np.random.default_rng(42)
        for length in (500, 4000, 20000):
            flux = rng.random(length)
            before = flux.copy()
            for period in (40., 137.25, 200.0001):
                with self.subTest(length=length, period=period):
                    self.assertEqual(beatdetect.refine_period(flux, period),
                                     reference_refine(flux, period))
            np.testing.assert_array_equal(flux, before)

    def test_refinement_matches_reference_on_bin_edges_and_long_curves(self):
        rng = np.random.default_rng(7)
        # periods whose frames fall exactly on a bin edge, and a curve as long as a 34 minute track
        for length, periods in ((3000, (32., 48., 64., 96., 144.)), (700000, (61.5, 96.))):
            flux = rng.random(length)
            for period in periods:
                with self.subTest(length=length, period=period):
                    self.assertEqual(beatdetect.refine_period(flux, period),
                                     reference_refine(flux, period))

    def test_refinement_scan_keeps_its_prominence(self):
        flux = np.zeros(6000)
        flux[::50] = 1.0
        period, peak, prominence = beatdetect.refine_scan(flux, 50.5)
        self.assertAlmostEqual(period, 50.0, delta=0.05)
        self.assertGreater(peak, 0.9)
        self.assertGreater(prominence, 2.0)

    def test_refinement_of_an_empty_or_silent_curve_gives_no_peak(self):
        for flux in (np.zeros(0), np.ones(1), np.zeros(300)):
            with self.subTest(length=len(flux)):
                period, peak, prominence = beatdetect.refine_scan(flux, 50.)
                self.assertTrue(np.isfinite([period, peak, prominence]).all())
                if flux.sum() == 0:
                    self.assertEqual((peak, prominence), (0., 0.))

    def test_full_bpm_detection_preserves_tempo_phase_and_confidence(self):
        rate = 4000
        for bpm in (90., 120., 137.25, 175.):
            samples = np.zeros(rate * 20, dtype=np.float32)
            pulse = np.hanning(80).astype(np.float32)
            for start in np.arange(0, len(samples) - len(pulse), rate * 60 / bpm).astype(int):
                samples[start:start + len(pulse)] += pulse
            actual = beatdetect.detect(samples, rate)
            real_scan = beatdetect.refine_scan

            def reference_scan(flux, period):
                best, peak = reference_refine(flux, period)
                return best, peak, real_scan(flux, period)[2]

            with patch.object(beatdetect, "refine_scan", side_effect=reference_scan):
                expected = beatdetect.detect(samples, rate)
            self.assertEqual(actual, expected)
            self.assertAlmostEqual(actual.bpm, bpm, delta=.15)
            self.assertTrue(actual.reliable)

    def test_waveform_reduction_matches_reference_and_keeps_input(self):
        rng = np.random.default_rng(73)
        lo, hi = analysis.filters(11025)
        for samples in (np.zeros(8000, dtype=np.float32),
                        rng.standard_normal(8000).astype(np.float32)):
            before = samples.copy()
            for columns in (100, 400, 1200, 8001):
                with self.subTest(columns=columns):
                    actual = analysis.Audio.bands_py(samples, lo, hi, columns)
                    expected = reference_bands(samples, lo, hi, columns)
                    np.testing.assert_array_equal(actual, expected)
            np.testing.assert_array_equal(samples, before)

    def test_empty_peak_requests_do_not_divide_by_zero(self):
        self.assertEqual(analysis.peaks(np.ones(10), 0).size, 0)
        self.assertEqual(analysis.peaks(np.ones(10), -1).size, 0)
        np.testing.assert_array_equal(analysis.peaks(np.ones(2), 3), np.zeros(3))


if __name__ == "__main__":
    unittest.main()
