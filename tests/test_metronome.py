import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from djapp.model import Track
from djapp.ui.metronome import Metronome, click_samples, pcm, valid_grid
from PySide6.QtMultimedia import QAudioFormat, QAudioSink
from PySide6.QtWidgets import QApplication


# ------------------------------------------------------------
def audio_format(kind=QAudioFormat.Int16, channels=1):
    fmt = QAudioFormat()
    fmt.setSampleRate(48000)
    fmt.setChannelCount(channels)
    fmt.setSampleFormat(kind)
    return fmt


# ------------------------------------------------------------
class MetronomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.audio_error = type(QAudioSink(audio_format()).error())

    def setUp(self):
        self.track = Track(bpm=123.456, grid=500, duration=600)

    def test_no_click_before_first_beat_and_bar_start_is_accented(self):
        rate = 48000
        self.assertFalse(click_samples(self.track, 0, rate // 2, rate).any())
        first = click_samples(self.track, 500, 576, rate)
        second = click_samples(self.track, 500 + 60000 / self.track.bpm, 576, rate)
        self.assertGreater(np.max(np.abs(first)), np.max(np.abs(second)))
        self.assertLessEqual(np.max(np.abs(first)), 0.28)

    def test_chunks_match_one_continuous_render(self):
        rate = 48000
        first = click_samples(self.track, 495, 400, rate)
        second = click_samples(self.track, 495 + 400 * 1000 / rate, 560, rate)
        whole = click_samples(self.track, 495, 960, rate)
        np.testing.assert_allclose(np.concatenate([first, second]), whole, atol=1e-7)

    def test_fractional_bpm_stays_aligned_after_ten_minutes(self):
        self.track.duration = 1000
        # Beat 1200 is a bar start, with the same waveform as the first beat.
        late = self.track.grid + 1200 * 60000 / self.track.bpm
        np.testing.assert_allclose(click_samples(self.track, late, 576, 48000),
                                   click_samples(self.track, self.track.grid, 576, 48000), atol=1e-7)

    def test_seek_recomputes_phase_and_end_of_track_is_silent(self):
        ms = self.track.grid + 300 * 60000 / self.track.bpm + 100
        self.assertFalse(click_samples(self.track, ms, 480, 48000).any())
        self.assertFalse(click_samples(self.track, self.track.duration * 1000, 480, 48000).any())

    def test_invalid_grid_is_not_a_zero_based_fallback(self):
        for track in (Track(bpm=0, grid=0), Track(bpm=120), Track(bpm=float("nan"), grid=0)):
            self.assertFalse(valid_grid(track))
            with self.assertRaises(ValueError):
                click_samples(track, 0, 480, 48000)

    def test_pcm_matches_all_supported_device_formats(self):
        samples = np.array([0, 0.18, -0.18], dtype=np.float32)
        for kind in (QAudioFormat.Int16, QAudioFormat.Int32, QAudioFormat.Float, QAudioFormat.UInt8):
            fmt = audio_format(kind, channels=2)
            self.assertEqual(len(pcm(samples, fmt)), fmt.bytesForFrames(3))
        self.assertEqual(pcm(np.zeros(3), audio_format(QAudioFormat.UInt8)), bytes([128] * 3))

    def make_stream(self):
        engine = SimpleNamespace(playing=True, path="track.wav", next_path="",
                                 timing_track=self.track, a=MagicMock())
        engine.a.position.return_value = 500
        metro = Metronome(engine)
        self.addCleanup(metro.close)
        sink = MagicMock()
        sink.format.return_value = audio_format()
        sink.error.return_value = self.audio_error.NoError
        sink.processedUSecs.return_value = 0
        sink.bytesFree.return_value = 960
        output = sink.start.return_value
        output.write.side_effect = len
        device = MagicMock()
        device.isNull.return_value = False
        device.preferredFormat.return_value = audio_format()
        return metro, engine, sink, device, output

    def test_stream_flushes_on_pause_seek_and_grid_edit(self):
        metro, engine, sink, device, output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100), \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            self.assertEqual(metro.written, 480)
            self.assertEqual(output.write.call_count, 1)
            engine.playing = False
            metro.feed()
            self.assertIsNone(metro.sink)
            engine.playing = True
            engine.a.position.return_value = 1000
            metro.transport()
            metro.feed()
            self.assertEqual(metro.origin_ms, 1000)
            self.track.grid += 1
            metro.feed()
            self.assertEqual(metro.signature[2], 501)

    def test_missing_audio_device_and_sink_errors_are_reported(self):
        metro, _engine, sink, device, _output = self.make_stream()
        errors = []
        metro.failed.connect(errors.append)
        device.isNull.return_value = True
        with patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                self.assertLogs("djapp.ui.metronome", level="ERROR"):
            metro.set_enabled(True)
            metro.feed()
        self.assertFalse(metro.enabled)
        self.assertIn("No audio output", errors[0])
        device.isNull.return_value = False
        sink.error.return_value = self.audio_error.OpenError
        with patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink), \
                self.assertLogs("djapp.ui.metronome", level="ERROR"):
            metro.set_enabled(True)
            metro.feed()
        self.assertFalse(metro.enabled)
        self.assertEqual(len(errors), 2)

    def test_crossfade_and_missing_grid_mute_without_stale_clicks(self):
        metro, engine, sink, device, output = self.make_stream()
        with patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.set_enabled(True)
            metro.feed()
            written = output.write.call_count
            engine.next_path = "next.wav"
            metro.feed()
            self.assertIsNone(metro.sink)
            self.assertEqual(output.write.call_count, written)
            engine.next_path = ""
            self.track.grid = None
            metro.feed()
            self.assertIsNone(metro.sink)
            self.assertEqual(output.write.call_count, written)

    def test_device_priming_does_not_trigger_a_restart_loop(self):
        metro, _engine, sink, device, _output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100) as clock, \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink) as create:
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            clock.return_value = 100.1
            metro.feed()
            self.assertEqual(create.call_count, 1)
            self.assertGreater(metro.written, 0)

    def test_audio_write_failure_disables_and_reports_the_metronome(self):
        metro, _engine, sink, device, output = self.make_stream()
        output.write.side_effect = None
        output.write.return_value = -1
        failures = []
        metro.failed.connect(failures.append)
        with patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink), \
                self.assertLogs("djapp.ui.metronome", level="ERROR"):
            metro.set_enabled(True)
            metro.feed()
        self.assertFalse(metro.enabled)
        self.assertIsNone(metro.sink)
        self.assertEqual(len(failures), 1)

    def test_latency_offset_changes_click_phase_and_discards_old_buffer(self):
        metro, _engine, sink, device, output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100), \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.sync(500)
            metro.set_enabled(True)
            metro.set_offset(5)
            metro.feed()
            expected = pcm(click_samples(self.track, 495, 480, 48000), audio_format())
            self.assertEqual(output.write.call_args.args[0], expected)
            metro.set_offset(-35)
            self.assertIsNone(metro.sink)
            metro.feed()
            expected = pcm(click_samples(self.track, 535, 480, 48000), audio_format())
            self.assertEqual(output.write.call_args.args[0], expected)

    def test_backend_cannot_queue_more_than_thirty_ms_before_consuming(self):
        metro, _engine, sink, device, output = self.make_stream()
        sink.bytesFree.return_value = 192000
        with patch("djapp.ui.metronome.time.monotonic", return_value=100), \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.sync(500)
            metro.set_enabled(True)
            for _ in range(20):
                metro.feed()
            self.assertEqual(metro.written, 1440)
            self.assertEqual(output.write.call_count, 1)
            sink.processedUSecs.return_value = 10000
            metro.feed()
            self.assertEqual(metro.written, 1920)
            self.assertEqual(len(output.write.call_args.args[0]), 960)

    def test_position_jump_flushes_pcm_even_without_explicit_seek(self):
        metro, _engine, sink, device, _output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100), \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            metro.sync(1500)
            self.assertIsNone(metro.sink)
            metro.feed()
            self.assertEqual(metro.origin_ms, 1500)
            metro.sync(500)
            self.assertIsNone(metro.sink)

    def test_duplicate_position_notifications_do_not_restart_output(self):
        metro, _engine, sink, device, _output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100) as clock, \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink) as create:
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            clock.return_value = 100.02
            metro.sync(510)
            metro.feed()
            self.assertEqual(create.call_count, 1)

    def test_phase_correction_only_runs_when_audio_clock_advances(self):
        metro, _engine, sink, device, _output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100) as clock, \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            clock.return_value = 100.02
            sink.processedUSecs.return_value = 10000
            metro.feed()
            origin = metro.origin_ms
            self.assertGreater(origin, 500)
            clock.return_value = 100.05
            metro.feed()
            self.assertEqual(metro.origin_ms, origin)

    def test_stream_tracks_ten_minutes_of_audio_clock_drift(self):
        metro, _engine, sink, device, output = self.make_stream()
        sink.bytesFree.return_value = 192000
        with patch("djapp.ui.metronome.time.monotonic", return_value=100) as clock, \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink) as create:
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            for step in range(1, 60001):
                elapsed = step * 0.01
                clock.return_value = 100 + elapsed
                sink.processedUSecs.return_value = round(elapsed * 0.9999 * 1e6)
                metro.feed()
            processed = sink.processedUSecs.return_value / 1000
            self.assertLess(abs(metro.position() - metro.origin_ms - processed), 1)
            self.assertLessEqual(metro.written * 1000 / 48000 - processed, 30.1)
            self.assertEqual(create.call_count, 1)
            self.assertGreater(output.write.call_count, 59000)

    def test_underrun_after_long_stall_discards_stale_clicks(self):
        metro, _engine, sink, device, _output = self.make_stream()
        with patch("djapp.ui.metronome.time.monotonic", return_value=100) as clock, \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink) as create:
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            clock.return_value = 100.02
            sink.processedUSecs.return_value = 10000
            metro.feed()
            clock.return_value = 100.5
            sink.processedUSecs.return_value = 20000
            sink.error.return_value = self.audio_error.UnderrunError
            metro.feed()
            self.assertEqual(create.call_count, 2)
            self.assertEqual(metro.origin_ms, 1000)
            self.assertEqual(metro.written, 0)

    def test_repeated_loop_seeks_recompute_pcm_from_grid_anchor(self):
        metro, engine, sink, device, output = self.make_stream()
        self.track.grid = 517.125
        loop_start = 517.125 + 4 * 60000 / self.track.bpm
        with patch("djapp.ui.metronome.time.monotonic", return_value=100), \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.set_enabled(True)
            for _ in range(20):
                engine.a.position.return_value = int(loop_start)
                metro.transport()
                metro.feed()
                expected = pcm(click_samples(self.track, int(loop_start), 480, 48000), audio_format())
                self.assertEqual(output.write.call_args.args[0], expected)
                self.assertEqual(metro.written, 480)

    def test_partial_frame_aligned_writes_do_not_skip_samples(self):
        metro, _engine, sink, device, output = self.make_stream()
        output.write.side_effect = None
        output.write.return_value = 240
        with patch("djapp.ui.metronome.time.monotonic", return_value=100), \
                patch("djapp.ui.metronome.QMediaDevices.defaultAudioOutput", return_value=device), \
                patch("djapp.ui.metronome.QAudioSink", return_value=sink):
            metro.sync(500)
            metro.set_enabled(True)
            metro.feed()
            self.assertEqual(metro.written, 120)
            metro.feed()
            expected = pcm(click_samples(self.track, 502.5, 480, 48000), audio_format())
            self.assertEqual(output.write.call_args.args[0], expected)


if __name__ == "__main__":
    unittest.main()
