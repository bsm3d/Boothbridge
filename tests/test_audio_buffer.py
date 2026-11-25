# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the audio buffer used for playback and metronome clicks against reference signals and sample positions.

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from djapp.model import Track
from djapp.ui.metronome import BUFFER_MS, Metronome, click_samples, pcm
from djapp.ui.player import Engine
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudio, QAudioFormat, QMediaPlayer
from PySide6.QtWidgets import QApplication


# ------------------------------------------------------------
def reference_click(track, start, frames, rate):
    positions = start + np.arange(frames, dtype=np.float64) * 1000 / rate
    indices = np.floor((positions - track.grid) / (60000 / track.bpm)).astype(np.int64)
    age = (positions - (track.grid + indices * 60000 / track.bpm)) / 1000
    downbeat = indices % 4 == 0
    envelope = np.maximum(0.0, 1 - age / .012) ** 2
    samples = (np.sin(2 * np.pi * np.where(downbeat, 1600., 1100.) * age)
               * envelope * np.where(downbeat, .28, .18))
    samples[(positions < track.grid) | (age >= .012)] = 0
    if track.duration > 0:
        samples[positions >= track.duration * 1000] = 0
    return samples.astype(np.float32)


# ------------------------------------------------------------
class AudioBufferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_click_output_matches_previous_algorithm_at_boundaries(self):
        for bpm in (60., 120., 137.25, 300.):
            track = Track(bpm=bpm, grid=13.25, duration=2.01)
            for rate in (8000, 44100, 48000, 96000):
                for start in (-20., 0., 13.25, 25.25, 2000., 10000.):
                    with self.subTest(bpm=bpm, rate=rate, start=start):
                        actual = click_samples(track, start, 4096, rate)
                        expected = reference_click(track, start, 4096, rate)
                        np.testing.assert_allclose(actual, expected, atol=2e-7)

    def test_silent_buffer_skips_sine_calculation(self):
        track = Track(bpm=120, grid=0, duration=10)
        with patch("djapp.ui.metronome.np.sin") as sine:
            samples = click_samples(track, 100, 1440, 48000)
        sine.assert_not_called()
        np.testing.assert_array_equal(samples, np.zeros(1440, dtype=np.float32))

    def test_pcm_formats_and_channels_preserve_exact_bytes(self):
        samples = np.array([-.28, 0., .18, .28], dtype=np.float32)
        for channels in (1, 2, 6):
            for kind in (QAudioFormat.Float, QAudioFormat.Int16, QAudioFormat.Int32, QAudioFormat.UInt8):
                with self.subTest(channels=channels, kind=kind):
                    fmt = QAudioFormat()
                    fmt.setSampleRate(48000)
                    fmt.setChannelCount(channels)
                    fmt.setSampleFormat(kind)
                    expanded = np.repeat(samples, channels)
                    if kind == QAudioFormat.Float:
                        expected = expanded.astype("<f4").tobytes()
                    elif kind == QAudioFormat.Int16:
                        expected = np.rint(expanded * 32767).astype("<i2").tobytes()
                    elif kind == QAudioFormat.Int32:
                        expected = np.rint(expanded.astype(np.float64) * 2147483647).astype("<i4").tobytes()
                    else:
                        expected = np.rint((expanded + 1) * 127.5).astype(np.uint8).tobytes()
                    self.assertEqual(pcm(samples, fmt), expected)

    def metronome(self):
        track = Track(bpm=120, grid=0, duration=10)
        engine = Mock(playing=True, path="track.wav", next_path="", timing_track=track)
        engine.a.position.return_value = 0
        metro = Metronome(engine)
        metro.enabled = True
        self.addCleanup(metro.close)
        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.Float)
        device = Mock()
        device.isNull.return_value = False
        device.preferredFormat.return_value = fmt
        sink = Mock()
        sink.error.return_value = QAudio.NoError
        sink.processedUSecs.return_value = 0
        sink.bytesFree.return_value = fmt.bytesForDuration(100000)
        sink.start.return_value.write.side_effect = lambda raw: len(raw)
        with patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
             patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.start_audio(track)
        return metro, sink, fmt

    def test_feed_limits_queue_to_30ms_and_skips_generation_when_full(self):
        metro, sink, fmt = self.metronome()
        with patch("djapp.ui.metronome.click_samples", wraps=click_samples) as generate:
            metro.feed()
            metro.feed()
        sink.setBufferSize.assert_called_once_with(fmt.bytesForDuration(BUFFER_MS * 1000))
        self.assertEqual(metro.written, 1440)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(len(sink.start.return_value.write.call_args.args[0]), 1440 * 8)
        sink.format.assert_not_called()

    def test_partial_and_zero_writes_do_not_advance_unwritten_frames(self):
        metro, sink, _ = self.metronome()
        output = sink.start.return_value
        output.write.side_effect = [0, 80, 11440]
        metro.feed()
        self.assertEqual(metro.written, 0)
        metro.feed()
        self.assertEqual(metro.written, 10)
        metro.feed()
        self.assertEqual(metro.written, 1440)
        metro.stop_audio()
        self.assertIsNone(metro.format)
        self.assertIsNone(metro.output)

    def test_replay_loaded_track_keeps_decoder_source(self):
        player, output = Mock(), Mock()
        player.source.return_value = QUrl.fromLocalFile("track.wav")
        player.mediaStatus.return_value = QMediaPlayer.LoadedMedia
        engine = SimpleNamespace(path="track.wav", next_path="", a=player,
                                 decks=[(player, output)], volume=.7, stop=Mock())
        track = Track(path="track.wav")
        with patch("djapp.ui.player.os.path.isfile", return_value=True):
            self.assertTrue(Engine.play(engine, "track.wav", track))
        engine.stop.assert_not_called()
        player.setSource.assert_not_called()
        player.stop.assert_called_once()
        player.setPosition.assert_called_once_with(0)
        player.play.assert_called_once()
        self.assertIs(engine.timing_track, track)

    def test_changed_invalid_or_fading_track_reloads_decoder(self):
        for next_path, status, source in [
            ("next.wav", QMediaPlayer.LoadedMedia, "track.wav"),
            ("", QMediaPlayer.InvalidMedia, "track.wav"),
            ("", QMediaPlayer.LoadedMedia, "other.wav"),
        ]:
            with self.subTest(next_path=next_path, status=status, source=source):
                player, output = Mock(), Mock()
                player.source.return_value = QUrl.fromLocalFile(source)
                player.mediaStatus.return_value = status
                engine = SimpleNamespace(path="track.wav", next_path=next_path, a=player,
                                         decks=[(player, output)], volume=.7, stop=Mock())
                with patch("djapp.ui.player.os.path.isfile", return_value=True):
                    self.assertTrue(Engine.play(engine, "track.wav"))
                engine.stop.assert_called_once()
                player.setSource.assert_called_once_with(QUrl.fromLocalFile("track.wav"))


if __name__ == "__main__":
    unittest.main()
