# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Tempo and first-beat detection from the onset curve, for tracks that have no beat grid.

#
# Autocorrelation finds a period in the onset curve; nearby candidates refine the
# tempo and phase. Drifting live recordings still need a manual grid

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .export.files import Cancelled

log = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # numpy is a requirement  # pragma: no cover
    np = None

HOP = 32  # onset curve step, in samples of the decoded signal
SMOOTH_MS = 6.0  # amplitude smoothing, wider than a click and narrower than a hi-hat
MIN_BPM, MAX_BPM = 60.0, 200.0
OCTAVE_LIMIT = 185.0  # a faster result is the off-beat subdivision of the real tempo
# above, weak odd pulses mean half the tempo
ALTERNATION_BPM, ALTERNATION_RATIO = 140.0, 0.6
# a tempo is preferred near 120, not at half or double
PRIOR_BPM = 120.0
# log compression of the amplitude, lower keeps loud kicks above quiet hats
COMPRESSION = 8.0
MIN_SECONDS = 8.0
PHASE_BINS = 96
REFINE_SPAN = 0.03  # candidate tempos around the autocorrelation peak, relative
# BPM: an integer tempo wins when it explains the onsets almost as well
INTEGER_TOLERANCE = 0.05

# The fold concentration alone never passed 0.3 on real music, so a correct tempo was refused.
# A real grid is a pulse that stands out from the neighbouring tempos (prominence) and gives the same tempo in every part of the track (live bands drift 1 to 5 %).
# Thresholds from 132 tracks with a known tempo, 7 live recordings and 5 long pieces with none: tempo spread between parts, full score up to OK, none from END.
STEADY_OK, STEADY_END = 0.004, 0.012
RELIABLE_AT = 0.3
# from here a fast result can also be read at half the tempo, and the user may prefer it
HALF_OFFER_BPM = 135.0
# seconds: cut in 3 parts, in 2, or measured whole
LONG_TRACK, MEDIUM_TRACK = 45.0, 20.0

@dataclass(frozen=True)
class BeatResult:
    bpm: float
    first_beat_ms: float
    # 0..1, a pulse that stands out from its neighbours and holds on the whole track
    confidence: float

    @property
    def reliable(self) -> bool:
        return self.confidence >= RELIABLE_AT

# --------------------------------------------------------
# onset strength per HOP samples and the time offset of its first value, in seconds
def onset_curve(y, rate: int):
    win = max(3, round(rate * SMOOTH_MS / 1000))
    a = np.abs(np.asarray(y, dtype="float64"))
    csum = np.concatenate(([0.0], np.cumsum(a)))
    smooth = (csum[win:] - csum[:-win]) / win
    reference = float(np.percentile(smooth, 99)) if len(smooth) else 0.0
    if reference <= 1e-9:
        raise ValueError("The track is silent")
    level = np.log1p(COMPRESSION * smooth / reference)[::HOP]
    flux = np.maximum(0.0, np.diff(level))
    # keeps the peaks, drops the swell
    flux -= np.convolve(flux, np.ones(41) / 41, mode="same") * 0.5
    flux = np.maximum(flux, 0.0)
    origin = (0.5 * HOP + win / 2) / rate
    return flux, origin

def half_tempo(bpm: float) -> float | None:
    return round(bpm / 2, 4) if bpm >= HALF_OFFER_BPM else None

# Score of each candidate tempo: a weighted sum of its onset fold, distance from 120 BPM, pulse regularity, support from other candidates, autocorrelation.
# Weights fitted on 3102 tagged tracks. Held out by source: 94 % right, against 92 % for the old prior.
# The gain is on played music, where the autocorrelation peak of the beat is often not the strongest.
RANK_LAGS = (0.5, 2 / 3, 0.75, 1.5, 4 / 3, 2.0, 3.0, 4.0)
RANK_WEIGHTS = {
    "octaves": -0.13228,
    "octaves2": -5.00273,
    "log_peak": 0.91557,
    "log_prominence": 0.15238,
    "alternation": 1.84708,
    "peak_share": 5.82778,
    "prominence_share": 0.63527,
    "fast": -0.42535,
    "very_fast": -0.11857,
    "double_support": 1.82110,
    "three_two_support": 0.17725,
    "four_three_support": -2.94396,
    "first": 1.80441,
    "second": 1.53981,
    "third": 0.24743,
    "lag": 1.46981,
    "lag_share": -0.09956,
    "lag_x0.5": 0.94772,
    "lag_x0.667": -1.88265,
    "lag_x0.75": 1.30249,
    "lag_x1.5": 0.17458,
    "lag_x1.333": -1.12034,
    "lag_x2": 0.45420,
    "lag_x3": 1.88204,
    "lag_x4": 2.06118,
}

# --------------------------------------------------------
def autocorrelation(flux):
    n = len(flux)
    # wider peaks: an integer lag must not miss a beat period that falls between two frames
    kernel = np.hanning(9)
    wide = np.convolve(flux, kernel / kernel.sum(), mode="same")
    centred = wide - wide.mean()
    size = 1 << (2 * n - 1).bit_length()
    spectrum = np.fft.rfft(centred, size)
    ac = np.fft.irfft(spectrum * np.conj(spectrum), size)[:n]
    ac /= np.maximum(1, n - np.arange(n))
    return ac

def candidate_periods(flux, step: float, count: int = 5, ac=None) -> list[float]:
    n = len(flux)
    if ac is None:
        ac = autocorrelation(flux)
    lo, hi = int(60 / MAX_BPM / step), int(60 / MIN_BPM / step) + 1
    if hi * 4 >= n:
        raise ValueError("The track is too short to measure a tempo")
    lags = np.arange(lo, hi)
    score = ac[lags].copy()
    for multiple, weight in ((2, 0.5), (4, 0.25)):
        score += weight * ac[np.minimum(lags * multiple, n - 1)]
    peaks = [
        i
        for i in range(1, len(lags) - 1)
        if score[i] >= score[i - 1] and score[i] > score[i + 1]
    ]
    peaks.sort(key=lambda i: -score[i])
    chosen: list[float] = []
    for i in peaks:
        lag = int(lags[i])
        if any(abs(lag - other) < 0.04 * other for other in chosen):
            continue
        # parabolic interpolation of the plain autocorrelation around the peak
        window = ac[lag - 1 : lag + 2]
        denominator = window[0] - 2 * window[1] + window[2]
        shift = 0.5 * (window[0] - window[2]) / denominator if denominator < 0 else 0.0
        chosen.append(lag + max(-1.0, min(1.0, float(shift))))
        if len(chosen) == count:
            break
    if not chosen:
        raise ValueError("No regular beat found")
    return chosen

# the peak strength of the other candidates that sit at one of the ratios of candidate i
def support(peaks, bpms, i: int, *ratios: float) -> float:
    return sum(
        peaks[j]
        for j in range(len(bpms))
        if j != i and any(abs(bpms[j] / (bpms[i] * r) - 1) <= 0.03 for r in ratios)
    )

# the score of each candidate (lags from candidate_periods), in the same order. Each one is measured
# again around its lag, then compared with the others
def rank_candidates(flux, step: float, ac, lags: list[float]):
    scan = [refine_scan(flux, lag) for lag in lags]
    periods = [s[0] for s in scan]
    bpms = [60 / (p * step) for p in periods]
    peaks = np.array([s[1] for s in scan])
    prominences = np.array([s[2] for s in scan])
    base = max(float(ac[0]), 1e-12)

    # the autocorrelation at the lag of each candidate and at its multiples
    def at_lag(lag):
        k = round(lag)
        return float(ac[k]) / base if 1 <= k < len(ac) else 0.0

    at = [{r: at_lag(lag * r) for r in (1.0, *RANK_LAGS)} for lag in lags]
    best_lag = max(max(a[1.0] for a in at), 1e-12)
    top_peak, top_prominence = max(peaks.max(), 1e-12), max(prominences.max(), 1e-12)
    scores = []
    for i, bpm in enumerate(bpms):
        octaves = math.log2(bpm / PRIOR_BPM)
        w = RANK_WEIGHTS
        score = (
            w["octaves"] * octaves
            + w["octaves2"] * octaves**2
            + w["log_peak"] * math.log(peaks[i] + 1e-6)
            + w["log_prominence"] * math.log(prominences[i] + 1e-6)
            + w["alternation"] * alternation(flux, periods[i])
            + w["peak_share"] * peaks[i] / top_peak
            + w["prominence_share"] * prominences[i] / top_prominence
            + w["fast"] * (bpm > 140.0)
            + w["very_fast"] * (bpm > OCTAVE_LIMIT)
            + w["double_support"] * support(peaks, bpms, i, 2.0, 0.5)
            + w["three_two_support"] * support(peaks, bpms, i, 1.5, 1 / 1.5)
            + w["four_three_support"] * support(peaks, bpms, i, 4 / 3, 0.75)
            + w["first"] * (i == 0)
            + w["second"] * (i == 1)
            + w["third"] * (i == 2)
            + w["lag"] * at[i][1.0]
            + w["lag_share"] * at[i][1.0] / best_lag
        )
        for r in RANK_LAGS:
            score += w[lag_key(r)] * at[i][r]
        scores.append(score)
    return periods, scores

def lag_key(ratio: float) -> str:
    return "lag_x" + f"{ratio:.3f}".rstrip("0").rstrip(".")

def fold(flux, period: float, bins: int, offset: int = 0):
    phase = ((np.arange(len(flux)) + offset) % period) / period
    return np.bincount((phase * bins).astype(int) % bins, weights=flux, minlength=bins)

def concentration(hist) -> float:
    total = float(hist.sum())
    if total <= 0:
        return 0.0
    smoothed = hist.copy()
    smoothed[1:] += hist[:-1]
    smoothed[0] += hist[-1]
    smoothed[:-1] += hist[1:]
    smoothed[-1] += hist[0]
    return float(smoothed.max()) / total

# --------------------------------------------------------
# the tempo, around period, that folds the onsets the most. Returns (period, concentration)
def refine_period(flux, period: float):
    best, peak, _ = refine_scan(flux, period)
    return best, peak

# same search, plus how far the best tempo stands out: its fold concentration over the median
# of the neighbouring tempos. Near 1 when no tempo is better than another
def refine_scan(flux, period: float):
    positions = np.arange(len(flux), dtype=np.int64)
    indices = np.empty(len(flux), dtype=np.int64)
    # the phase of each frame in whole bins, by integer multiply and shift: much faster than a float
    # remainder. The shift is kept low enough for the product to stay inside 63 bits. The step is
    # rounded up so a frame that sits exactly on a bin edge stays in the upper bin, as in floats
    bits = min(36, 62 - (len(flux) * PHASE_BINS).bit_length())

    def score(candidate):
        step = math.ceil((1 << bits) * PHASE_BINS / candidate)
        np.multiply(positions, step, out=indices)
        np.right_shift(indices, bits, out=indices)
        np.remainder(indices, PHASE_BINS, out=indices)
        return concentration(np.bincount(indices, weights=flux, minlength=PHASE_BINS))

    candidates = period / (1 + np.linspace(-REFINE_SPAN, REFINE_SPAN, 121))
    scores = [score(p) for p in candidates]
    best = candidates[int(np.argmax(scores))]
    spread = candidates[1] / candidates[0] - 1
    fine = best / (1 + np.linspace(-spread, spread, 41))
    fine_scores = [score(p) for p in fine]
    index = int(np.argmax(fine_scores))
    median = float(np.median(scores))
    return (
        float(fine[index]),
        fine_scores[index],
        (max(scores) / median if median > 0 else 0.0),
    )

# --------------------------------------------------------
# how well the lattice holds on the whole track: the tempo is measured again in each part, and the
# result is how far those tempos are apart (relative) and how clear the weakest part is
def consistency(flux, period: float, parts: int):
    if parts <= 1:
        _, _, prominence = refine_scan(flux, period)
        return 0.0, prominence
    size = len(flux) // parts
    periods, clarity = [], []
    for k in range(parts):
        p, _, prominence = refine_scan(flux[k * size : (k + 1) * size], period)
        periods.append(p)
        clarity.append(prominence)
    return (max(periods) - min(periods)) / (sum(periods) / parts), min(clarity)

# --------------------------------------------------------
def confidence_of(spread: float, prominence: float) -> float:
    strength = max(0.0, min(1.0, prominence - 1.0))
    steadiness = (
        1.0
        if spread <= STEADY_OK
        else max(0.0, 1.0 - (spread - STEADY_OK) / (STEADY_END - STEADY_OK))
    )
    return strength * steadiness

# where the beats fall in the onset curve, in frames, within [0, period). offset is the
# index of flux[0] in the whole curve, so that parts of a track share one lattice
def beat_phase(flux, period: float, offset: int = 0) -> float:
    bins = 512
    hist = fold(flux, period, bins, offset)
    kernel = max(3, bins // 48) | 1
    smooth = np.convolve(np.tile(hist, 3), np.ones(kernel), mode="same")[
        bins : 2 * bins
    ]
    peak = int(np.argmax(smooth))
    half = bins // 32
    offsets = np.arange(-half, half + 1)
    weights = hist[(peak + offsets) % bins]
    if weights.sum() <= 0:
        return peak / bins * period
    centre = peak + float((offsets * weights).sum() / weights.sum())
    return (centre + 0.5) / bins * period % period

# --------------------------------------------------------
# sharpens the tempo: a period that is slightly off moves the beats of the second half of
# the track against those of the first half, and that shift tells how much to correct
def correct_drift(flux, period: float) -> float:
    half = len(flux) // 2
    for _ in range(3):
        shift = beat_phase(flux[half:], period, half) - beat_phase(
            flux[:half], period, 0
        )
        shift = (shift + period / 2) % period - period / 2
        period *= 1 + shift / half
    return period

# ----------------------------------------------------------
# first beat of the lattice with a real onset under it, in frames
def first_audible_beat(flux, period: float, phase: float) -> float:
    count = int((len(flux) - phase) // period) + 1
    centres = phase + period * np.arange(count)
    radius = max(1, int(period * 0.08))
    index = np.clip(
        centres.astype(int)[:, None] + np.arange(-radius, radius + 1), 0, len(flux) - 1
    )
    strength = flux[index].max(axis=1)
    reference = float(np.percentile(strength, 75))
    audible = np.nonzero(strength >= 0.3 * reference)[0] if reference > 0 else []
    return float(centres[audible[0]]) if len(audible) else phase

# --------------------------------------------------------
# weaker over stronger mean onset of the odd and even pulses of the lattice: close to 0 when
# every other pulse is an off-beat, close to 1 when all pulses weigh the same
def alternation(flux, period: float) -> float:
    phase = beat_phase(flux, period)
    count = int((len(flux) - phase) // period) + 1
    centres = phase + period * np.arange(count)
    radius = max(1, int(period * 0.08))
    index = np.clip(
        centres.astype(int)[:, None] + np.arange(-radius, radius + 1), 0, len(flux) - 1
    )
    strength = flux[index].max(axis=1)
    even, odd = float(strength[0::2].mean()), float(strength[1::2].mean())
    return min(even, odd) / max(even, odd, 1e-12)

def snap_tempo(period: float, step: float) -> float:
    bpm = 60 / (period * step)
    nearest = round(bpm)
    return 60 / (nearest * step) if abs(bpm - nearest) <= INTEGER_TOLERANCE else period

# --------------------------------------------------------
def detect(y, rate: int) -> BeatResult:
    if np is None:
        raise RuntimeError("Beat detection needs numpy")
    if len(y) < MIN_SECONDS * rate:
        raise ValueError(f"The track is shorter than {MIN_SECONDS:g} seconds")
    pad = round(rate * 0.1)  # a beat at the very start still has a rising edge
    flux, origin = onset_curve(np.concatenate((np.zeros(pad), y)), rate)
    origin -= pad / rate
    step = HOP / rate
    if float(flux.max()) <= 0:
        raise ValueError("No rhythmic content found")
    ac = autocorrelation(flux)
    periods, scores = rank_candidates(
        flux, step, ac, candidate_periods(flux, step, ac=ac)
    )
    period = periods[int(np.argmax(scores))]
    period = snap_tempo(correct_drift(flux, period), step)
    bpm = 60 / (period * step)
    if bpm > OCTAVE_LIMIT or (
        bpm > ALTERNATION_BPM and alternation(flux, period) < ALTERNATION_RATIO
    ):
        period *= 2  # every other pulse is the weak off-beat of the real tempo
        period = snap_tempo(period, step)
    phase = beat_phase(flux, period)
    first = first_audible_beat(flux, period, phase)
    seconds = len(y) / rate
    parts = 3 if seconds >= LONG_TRACK else 2 if seconds >= MEDIUM_TRACK else 1
    spread, prominence = consistency(flux, period, parts)
    return BeatResult(
        bpm=round(60 / (period * step), 4),
        first_beat_ms=max(0.0, round((first * step + origin) * 1000, 3)),
        confidence=round(confidence_of(spread, prominence), 3),
    )

# job entry point for a batch of (key, path). Returns (key, BeatResult or error text) for each
def analyze_many(
    items: list[tuple[object, str]], progress=lambda *_: None, cancel=None
) -> list[tuple]:
    done = []
    for n, (key, path) in enumerate(items):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        progress(
            int(100 * n / max(1, len(items))), f"Detecting {n + 1} of {len(items)}..."
        )
        try:
            done.append((key, analyze(path, cancel=cancel)))
        except Cancelled:
            raise
        # one odd file must not end the batch
        except Exception as e:  # noqa: BLE001
            done.append((key, str(e) or type(e).__name__))
    progress(100, "Done")
    return done

# --------------------------------------------------------
def analyze(path: str, progress=lambda *_: None, cancel=None) -> BeatResult:
    from .analysis import HAVE_AUDIO, decode

    if not HAVE_AUDIO:
        raise RuntimeError("Beat detection needs numpy, scipy and soundfile")

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    check()
    progress(5, "Decoding the audio...")
    y, rate, _frames = decode(path)
    if y is None or rate <= 0:
        raise ValueError("The audio file cannot be decoded")
    check()
    progress(60, "Detecting the tempo...")
    result = detect(y, rate)
    check()
    progress(100, "Done")
    return result
