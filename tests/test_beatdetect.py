# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks tempo detection on synthetic click tracks at known BPM, with noise, hi-hats and seeded randomness.

import os
import tempfile
import threading
import unittest

import numpy as np
from djapp import beatdetect
from djapp.export.files import Cancelled

RATE = 11025


# ------------------------------------------------------------
def click_track(bpm, first_ms, secs=40, hats=True, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    n = int(secs * RATE)
    y = np.zeros(n)
    beat = 60 / bpm
    k = 0
    while True:
        t = first_ms / 1000 + k * beat
        if t >= secs - 0.2:
            break
        i = round(t * RATE)
        m = int(0.08 * RATE)
        tt = np.arange(m) / RATE
        y[i:i + m] += (np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 40))[:n - i] * (1.0 if k % 4 == 0 else 0.8)
        if hats:
            j = round((t + beat / 2) * RATE)
            mm = int(0.02 * RATE)
            if j + mm < n:
                y[j:j + mm] += 0.2 * rng.standard_normal(mm) * np.exp(-np.arange(mm) / RATE * 150)
        k += 1
    return y + noise * rng.standard_normal(n)


# ----------------------------------------------------------
class BeatDetectTests(unittest.TestCase):
    def test_tempos_and_first_beat(self):
        for bpm, first in [(120, 0), (128, 123.4), (174, 57), (90, 310), (98.7, 41), (125.5, 80)]:
            for hats in (True, False):
                with self.subTest(bpm=bpm, hats=hats):
                    r = beatdetect.detect(click_track(bpm, first, hats=hats), RATE)
                    self.assertAlmostEqual(r.bpm, bpm, delta=0.05)
                    self.assertAlmostEqual(r.first_beat_ms, first, delta=5)
                    self.assertTrue(r.reliable)

    def test_a_pulse_over_the_octave_limit_is_read_at_half_tempo_on_the_exact_value(self):
        # equal pulses faster than OCTAVE_LIMIT: every other one is the off-beat, and the halved
        # tempo is the exact half of the measured one, not a new estimate on the sparser lattice
        for bpm in (186, 190, 195):
            with self.subTest(bpm=bpm):
                y = np.zeros(40 * RATE)
                tt = np.arange(int(0.08 * RATE)) / RATE
                click = np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 40)
                k = 0
                while 0.3 + k * 60 / bpm < 39.5:
                    i = round((0.3 + k * 60 / bpm) * RATE)
                    y[i:i + len(click)] += click
                    k += 1
                r = beatdetect.detect(y, RATE)
                self.assertAlmostEqual(r.bpm, bpm / 2, delta=0.06)

    def test_noisy_track_is_still_reliable(self):
        r = beatdetect.detect(click_track(128, 123.4, noise=0.15), RATE)
        self.assertAlmostEqual(r.bpm, 128, delta=0.1)
        self.assertTrue(r.reliable)

    def test_noise_is_not_reliable(self):
        y = np.random.default_rng(3).standard_normal(20 * RATE)
        try:
            r = beatdetect.detect(y, RATE)
        except ValueError:
            return
        self.assertFalse(r.reliable)

    def test_a_steady_long_track_is_reliable_and_uses_three_parts(self):
        y = click_track(126, 40, secs=60)
        self.assertGreaterEqual(len(y) / RATE, beatdetect.LONG_TRACK)
        r = beatdetect.detect(y, RATE)
        self.assertAlmostEqual(r.bpm, 126, delta=0.1)
        self.assertTrue(r.reliable)
        self.assertGreater(r.confidence, 0.8)

    def test_a_drifting_tempo_is_not_reliable(self):
        # a live band that speeds up by 6 %: no constant grid can hold on it
        rng = np.random.default_rng(5)
        secs, y, t = 60, np.zeros(60 * RATE), 0.2
        while t < secs - 0.3:
            bpm = 118 + 7 * t / secs
            i, m = round(t * RATE), int(0.08 * RATE)
            tt = np.arange(m) / RATE
            y[i:i + m] += np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 40)
            t += 60 / bpm
        y += 0.02 * rng.standard_normal(len(y))
        try:
            r = beatdetect.detect(y, RATE)
        except ValueError:
            return
        self.assertFalse(r.reliable)

    def test_confidence_rewards_a_clear_and_steady_pulse_only(self):
        self.assertEqual(beatdetect.confidence_of(0.0, 3.0), 1.0)
        self.assertEqual(beatdetect.confidence_of(0.0, 1.0), 0.0)
        self.assertEqual(beatdetect.confidence_of(beatdetect.STEADY_END, 3.0), 0.0)
        self.assertEqual(beatdetect.confidence_of(beatdetect.STEADY_OK, 2.0), 1.0)
        middle = (beatdetect.STEADY_OK + beatdetect.STEADY_END) / 2
        self.assertAlmostEqual(beatdetect.confidence_of(middle, 2.0), 0.5)
        self.assertTrue(beatdetect.BeatResult(120, 0, beatdetect.RELIABLE_AT).reliable)
        self.assertFalse(beatdetect.BeatResult(120, 0, beatdetect.RELIABLE_AT - 0.01).reliable)

    def test_silence_and_short_tracks_are_rejected(self):
        with self.assertRaises(ValueError):
            beatdetect.detect(np.zeros(20 * RATE), RATE)
        with self.assertRaises(ValueError):
            beatdetect.detect(click_track(120, 0, secs=3), RATE)

    def test_analyze_reads_a_file_and_reports_progress(self):
        import soundfile
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "click.wav")
            soundfile.write(path, click_track(124, 90, secs=30), RATE)
            r = beatdetect.analyze(path, progress=lambda *a: seen.append(a))
        self.assertAlmostEqual(r.bpm, 124, delta=0.1)
        self.assertEqual(seen[-1][0], 100)

    def test_analyze_honours_cancel_and_bad_files(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            beatdetect.analyze("missing.wav", cancel=cancel)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bad.wav")
            with open(path, "wb") as f:
                f.write(b"not audio")
            with self.assertRaises(ValueError):
                beatdetect.analyze(path)


class CandidateRankingTests(unittest.TestCase):
    def test_support_counts_the_other_candidates_at_a_ratio_only(self):
        peaks, bpms = [0.5, 0.25, 0.125, 0.0625], [90.0, 180.0, 60.0, 121.0]
        self.assertEqual(beatdetect.support(peaks, bpms, 0, 2.0, 0.5), 0.25)
        self.assertEqual(beatdetect.support(peaks, bpms, 0, 1.5, 1 / 1.5), 0.125)
        self.assertEqual(beatdetect.support(peaks, bpms, 0, 4 / 3), 0.0625)
        self.assertEqual(beatdetect.support(peaks, bpms, 1, 2.0, 0.5), 0.5)
        self.assertEqual(beatdetect.support(peaks, bpms, 0, 3.0), 0.0)

    def test_the_weights_name_every_lag_the_score_reads(self):
        for ratio in beatdetect.RANK_LAGS:
            self.assertIn(beatdetect.lag_key(ratio), beatdetect.RANK_WEIGHTS)
        self.assertEqual(beatdetect.lag_key(2 / 3), "lag_x0.667")
        self.assertEqual(beatdetect.lag_key(2.0), "lag_x2")

    def test_the_beat_of_a_click_track_scores_above_its_other_candidates(self):
        y = click_track(105, 30, secs=40)
        pad = round(RATE * 0.1)
        flux, _ = beatdetect.onset_curve(np.concatenate((np.zeros(pad), y)), RATE)
        step = beatdetect.HOP / RATE
        ac = beatdetect.autocorrelation(flux)
        lags = beatdetect.candidate_periods(flux, step, ac=ac)
        periods, scores = beatdetect.rank_candidates(flux, step, ac, lags)
        self.assertEqual(len(periods), len(lags))
        self.assertEqual(len(scores), len(lags))
        self.assertAlmostEqual(60 / (periods[int(np.argmax(scores))] * step), 105, delta=0.5)


class HalfTempoTests(unittest.TestCase):
    def test_a_fast_result_can_be_read_at_half_the_tempo_and_a_slow_one_cannot(self):
        self.assertEqual(beatdetect.half_tempo(159.0), 79.5)
        self.assertEqual(beatdetect.half_tempo(beatdetect.HALF_OFFER_BPM), beatdetect.HALF_OFFER_BPM / 2)
        self.assertIsNone(beatdetect.half_tempo(beatdetect.HALF_OFFER_BPM - 0.01))
        self.assertIsNone(beatdetect.half_tempo(124.0))


if __name__ == "__main__":
    unittest.main()
