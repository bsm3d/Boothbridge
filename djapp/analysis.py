# Benoit Saint-Moulin
# Boothbridge : audio decoding and waveform bands

# Traktor already did the musical analysis (bpm, grid, key, cues), only the waveforms need the audio.
# Three bands per column: low < 200 Hz < mid < 2500 Hz < high, peaks normalised on the whole track.
# The fine resolution (150 columns per second, what the players scroll) is computed once,
# the overviews (1200, 400, 100) are taken from it

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from . import native

log = logging.getLogger(__name__)

try:
    import numpy as np
    import soundfile as sf
    from scipy.signal import butter, sosfilt

    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False

DETAIL = 150  # columns per second


# ------------------------------------------------------------
@dataclass
class Bands:
    frames: int  # sample count at the file rate, PVBR wants it
    low: np.ndarray
    mid: np.ndarray
    high: np.ndarray

    def __len__(self):
        return len(self.low)


# --------------------------------------------------------------
@lru_cache(maxsize=8)
def filters(rate: int):
    # a file at 11 kHz is 2.7 kHz once folded, the cut-offs must stay under Nyquist
    top = rate * 0.45
    return (
        butter(2, min(200, top), "lowpass", fs=rate, output="sos"),
        butter(2, min(2500, top), "highpass", fs=rate, output="sos"),
    )


# quarter rate (11 kHz for a CD rip) is plenty for a picture of the sound
FOLD = 4


# --------------------------------------------------------
# average FOLD frames across channels into reduced-rate mono, slice accumulation is cheaper than a short-axis mean in workers
def fold(y, ch: int):
    w = FOLD * ch
    done = native.fold(y, w)
    if done is not None:
        return done
    a = y[: len(y) // w * w].reshape(-1, w)
    out = a[:, 0].copy()
    for i in range(1, w):
        out += a[:, i]
    out *= 1.0 / w
    return out


BLOCK = 1 << 16  # frames, a multiple of FOLD
QT_STALL_MS = 20000  # a decoder that gives nothing for this long is given up


# block by block into one reused buffer: a whole track at once means ~150 MB of
# fresh pages per file, and Windows serialises those page faults across processes
def read_sf(path: str):
    with sf.SoundFile(path) as f:
        ch, sr, frames = f.channels, f.samplerate, f.frames
        buf = np.empty((BLOCK, ch), dtype="float32")
        out = np.empty(frames // FOLD + 1, dtype="float32")
        pos = 0
        while True:
            try:
                n = f.read(BLOCK, dtype="float32", always_2d=True, out=buf).shape[0]
            except (OSError, RuntimeError) as e:
                # a damaged or cut file (mp3 mostly) that played for most of its length is
                # kept, the rest stays silent so the time scale holds. Less than half: refused
                if pos < max(1, len(out) // 2):
                    raise
                log.warning(
                    "%s is damaged after %d of %d frames: %s",
                    path,
                    pos * FOLD,
                    frames,
                    e,
                )
                last = max(pos, frames // FOLD)
                out[pos:last] = 0
                return out[:last], 1, frames, sr
            if n == 0:
                break
            m = fold(buf[:n].reshape(-1), ch)
            out[pos : pos + len(m)] = m
            pos += len(m)
            if n < BLOCK:
                break
    return out[:pos], 1, frames, sr


# a process with no Qt application (a pool worker) gets a core one the first time it needs the decoder
qt_core = None


# the decoder Qt ships for the player, for what libsndfile cannot read: AAC (m4a, aac), ALAC. It runs as
# fast as it can on the event loop of the calling thread, so any worker thread or process may use it
def read_qt(path: str):
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

    global qt_core
    if QCoreApplication.instance() is None:
        qt_core = QCoreApplication([])
    formats = QAudioFormat.SampleFormat
    kinds = {
        formats.Float: ("float32", 1.0, 0.0),
        formats.Int16: ("int16", 1 / 32768, 0.0),
        formats.Int32: ("int32", 1 / 2147483648, 0.0),
        formats.UInt8: ("uint8", 1 / 128, -1.0),
    }
    decoder = QAudioDecoder()
    loop = QEventLoop()
    watch = QTimer()
    parts: list = []
    pending: list = []
    state = {
        "rate": 0,
        "channels": 0,
        "frames": 0,
        "waiting": 0,
        "seen": -1,
        "length": 0,
        "error": "",
    }

    def flush():
        data = np.concatenate(pending)
        pending.clear()
        w = FOLD * state["channels"]
        usable = len(data) // w * w
        if usable:
            parts.append(fold(data[:usable], state["channels"]))
        if usable < len(data):
            pending.append(data[usable:])
        state["waiting"] = sum(len(p) for p in pending) // state["channels"]

    def fail(text):
        state["error"] = state["error"] or text
        loop.quit()

    def take():
        # the length the container declares, in ms, is only known while the decoder runs
        state["length"] = max(state["length"], decoder.duration())
        while decoder.bufferAvailable():
            block = decoder.read()
            fmt = block.format()
            kind = kinds.get(fmt.sampleFormat())
            rate, channels = fmt.sampleRate(), fmt.channelCount()
            if kind is None or rate <= 0 or channels < 1:
                return fail("The decoder gave a sample format that is not supported")
            if state["rate"] and (rate, channels) != (state["rate"], state["channels"]):
                return fail(
                    "The sample rate or the channel count changes inside the file"
                )
            state["rate"], state["channels"] = rate, channels
            data = np.frombuffer(bytes(block.constData()), dtype=kind[0])
            if kind[0] != "float32":
                data = data.astype("float32") * kind[1] + kind[2]
            pending.append(data)
            state["frames"] += len(data) // channels
            state["waiting"] += len(data) // channels
            if state["waiting"] >= BLOCK:
                flush()

    def stalled():
        if state["frames"] == state["seen"]:
            fail("The decoder stopped giving samples")
        state["seen"] = state["frames"]

    decoder.bufferReady.connect(take)
    decoder.finished.connect(loop.quit)
    decoder.error.connect(
        lambda code: fail(decoder.errorString() or "The decoder failed")
    )
    watch.timeout.connect(stalled)
    decoder.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
    watch.start(QT_STALL_MS)
    decoder.start()
    loop.exec()
    watch.stop()
    take()
    decoder.stop()
    if state["error"] and not state["frames"]:
        raise RuntimeError(state["error"])
    if pending:
        flush()
    frames, sr = state["frames"], state["rate"]
    if not parts or not frames:
        raise RuntimeError(state["error"] or "The decoder gave no audio")
    out = np.concatenate(parts)
    # cut short, by an error or by a damaged stream: the same rule as a damaged mp3. At least half of
    # the length the container declares is kept and the rest is silence, less is refused
    declared = state["length"] * sr // 1000
    if state["error"] or declared - frames > sr // 4:
        if declared and frames < declared // 2:
            raise RuntimeError(
                state["error"] or "The audio stops after less than half of its length"
            )
        log.warning(
            "%s is damaged after %d of %d frames: %s",
            path,
            frames,
            declared,
            state["error"] or "it ends early",
        )
        last = max(len(out), declared // FOLD)
        out = np.concatenate((out, np.zeros(last - len(out), dtype="float32")))
        frames = max(frames, declared)
    return out, 1, frames, sr


def read_librosa(path: str):
    # mp3 / m4a when libsndfile can't, much slower
    import librosa

    y, sr = librosa.load(path, sr=None, mono=True)
    return fold(y, 1), 1, len(y), sr


# ------------------------------------------------------------
# mono float32 samples at 1/FOLD of the file rate, that rate, and the frame count
def decode(path: str):
    if not HAVE_AUDIO:
        return None, 0, 0
    for reader in (read_sf, read_qt, read_librosa):
        try:
            y, _, frames, sr = reader(path)
            break
        except (OSError, RuntimeError, ValueError, ImportError) as e:
            failure = e
    else:
        log.warning("cannot decode %s: %s", path, failure)
        return None, 0, 0
    return y, sr // FOLD, frames


# --------------------------------------------------------
# where each of n columns starts in a signal of `length` samples: column c is the samples
# from c * length / n to (c + 1) * length / n, so the columns cover the whole signal at an even
# pace
# attention -> bug = every column took length // n samples and the tail was dropped: at
# 44.1 kHz that is 73 samples for 73.5, a waveform 0.68 percent too slow. A sound at 5:50 was
# drawn 2.4 s late, on the players and in the window
def column_starts(length: int, n: int):
    return (np.arange(n, dtype=np.int64) * length) // n


def peaks(sig, n: int):
    if n <= 0:
        return np.zeros(max(n, 0), dtype="float32")
    if len(sig) < n:
        return np.zeros(n, dtype="float32")
    return np.maximum.reduceat(np.abs(sig), column_starts(len(sig), n))


# peak of a over n columns, a being a finer column array
def shrink(a, n: int):
    idx = (np.arange(n + 1) * len(a)) // n
    idx[-1] = len(a)
    return (
        np.maximum.reduceat(a, np.minimum(idx[:-1], len(a) - 1))
        if len(a)
        else np.zeros(n)
    )


# ------------------------------------------------------------
class Audio:
    def __init__(self, path: str, detail: int = DETAIL):
        y, self.rate, self.frames = decode(path)
        self.ok = y is not None and len(y) > self.rate // detail
        if not self.ok:
            self.seconds = 0.0
            return
        self.seconds = len(y) / self.rate
        lo_sos, hi_sos = filters(self.rate)
        n = max(1, round(self.seconds * detail))
        self.fine = native.bands(y, lo_sos, hi_sos, n) or self.bands_py(
            y, lo_sos, hi_sos, n
        )

    # ------------------------------------------------------------
    # the bands of one decoded track
    @staticmethod
    def bands_py(y, lo_sos, hi_sos, n: int):
        lo = sosfilt(lo_sos, y).astype("float32")
        hi = sosfilt(hi_sos, y).astype("float32")
        mi = y - lo - hi
        fine = []
        starts = column_starts(len(y), n) if len(y) >= n > 0 else None
        for band in (lo, mi, hi):
            # Filtered bands are local scratch buffers; reduce them without an abs copy.
            np.abs(band, out=band)
            fine.append(
                np.maximum.reduceat(band, starts)
                if starts is not None
                else np.zeros(n, dtype="float32")
            )
        top = max(float(b.max()) for b in fine) or 1.0
        for band in fine:
            band /= top
            np.clip(band, 0, 1, out=band)
        return fine

    def at(self, n: int) -> Bands:
        if not self.ok:
            z = np.zeros(n, dtype="float32")
            return Bands(self.frames, z, z, z)
        if n == len(self.fine[0]):
            return Bands(self.frames, *self.fine)
        return Bands(self.frames, *(shrink(b, n) for b in self.fine))
