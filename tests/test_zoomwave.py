# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the zoomed beat grid waveform: beat and bar marks drawn on the track, run offscreen.

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.model import Cue, Track
from djapp.ui import cuehistory
from djapp.ui.zoomwave import (
    GRID_HEAD,
    ZoomControls,
    ZoomWave,
    beat_grid_marks,
    quantize_to_beat,
)
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget


class BeatGridWaveformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_marks_beats_and_distinguishes_bar_starts(self):
        track = Track(bpm=120, grid=500)

        marks = beat_grid_marks(track, 0, 5000, 1000)

        self.assertEqual(len(marks), 10)
        self.assertEqual(marks[0], (500, True))
        self.assertEqual(marks[4], (2500, True))
        self.assertTrue(all(downbeat for _, downbeat in marks[::4]))
        self.assertFalse(any(downbeat for _, downbeat in marks[1:4]))

    def test_dense_zoom_shows_downbeats_without_overcrowding(self):
        track = Track(bpm=120, grid=0)

        marks = beat_grid_marks(track, 0, 60000, 600)

        self.assertEqual([time for time, _ in marks], list(range(0, 60001, 2000)))
        self.assertTrue(all(downbeat for _, downbeat in marks))

    def test_no_marks_without_valid_grid(self):
        self.assertEqual(beat_grid_marks(Track(bpm=120), 0, 10000, 1000), [])
        self.assertEqual(beat_grid_marks(Track(bpm=0, grid=0), 0, 10000, 1000), [])
        self.assertEqual(beat_grid_marks(Track(bpm=120, grid=0), 0, 0, 1000), [])

    def test_partial_view_starts_at_next_grid_beat(self):
        track = Track(bpm=120, grid=500)

        marks = beat_grid_marks(track, 2200, 1800, 1000)

        self.assertEqual(marks, [(2500, True), (3000, False), (3500, False), (4000, False)])

    def test_quantize_uses_nearest_beat_relative_to_grid(self):
        track = Track(bpm=120, grid=500)

        self.assertEqual(quantize_to_beat(track, 1820), 2000)
        self.assertEqual(quantize_to_beat(track, 1180), 1000)
        self.assertEqual(quantize_to_beat(track, -100), 0)

    def test_quantize_leaves_position_unchanged_without_valid_grid(self):
        self.assertEqual(quantize_to_beat(Track(bpm=0, grid=0), 1234), 1234)
        self.assertEqual(quantize_to_beat(Track(bpm=120), 1234), 1234)
        self.assertEqual(quantize_to_beat(None, 1234), 1234)

    def test_waveform_drag_uses_snap_toggle(self):
        track = Track(bpm=120, grid=500, duration=10)
        wave = ZoomWave()
        wave.resize(1002, 120)
        wave.show_track(track, None)
        wave.snap_to_beat = True
        cue = Cue(start=1000)
        wave.grab = cue
        wave.hold = 0
        main = wave.areas()[0]
        x = main.left() + main.width() * 1820 / 10000

        wave.drag_cue(x, snap=False)

        self.assertEqual(cue.start, 2000)
        wave.close()

    def test_grid_marker_drag_cancel_and_undo(self):
        track = Track(bpm=120, grid=500, duration=10)
        wave = ZoomWave()
        wave.resize(1002, 120)
        wave.show_track(track, None)
        main = wave.areas()[0]

        wave.drag = "grid"
        wave.grid_origin = track.grid
        wave.before = cuehistory.snap(track)
        wave.drag_grid(wave.x_of(2000, main))
        wave.end_grid_drag(cancel=True)
        self.assertEqual(track.grid, 500)

        moved = []
        wave.grid_moved.connect(moved.append)
        wave.drag = "grid"
        wave.grid_origin = track.grid
        wave.before = cuehistory.snap(track)
        wave.drag_grid(wave.x_of(2000, main))
        wave.end_grid_drag(cancel=False)

        self.assertAlmostEqual(track.grid, 2000)
        self.assertEqual(moved, [track])
        self.assertTrue(cuehistory.undo(track))
        self.assertEqual(track.grid, 500)
        wave.close()


    def make_wave(self):
        track = Track(bpm=120, grid=500, duration=10)
        wave = ZoomWave()
        self.addCleanup(wave.close)
        wave.resize(1002, 120)
        wave.show_track(track, None)
        return wave, track

    def press_grid(self, wave):
        main = wave.areas()[0]
        point = QPointF(wave.x_of(wave.track.grid, main), main.top() + 3)
        wave.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, point, point,
                                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

    def test_zoom_reaches_half_second_without_losing_cursor_anchor(self):
        wave, _track = self.make_wave()
        wave.set_pos(5500)
        controls = ZoomControls(wave)
        self.addCleanup(controls.close)
        requests = []
        wave.need_hi.connect(lambda: requests.append(True))
        for _ in range(8):
            controls.in_button.click()
        self.assertEqual(wave.span, 500)
        self.assertTrue(wave.start <= wave.needle <= wave.start + wave.span)
        self.assertEqual(requests, [True])
        self.assertFalse(controls.in_button.isEnabled())
        self.assertEqual(controls.range_label.text(), "0.500 s visible")
        controls.reset_button.click()
        self.assertEqual((wave.start, wave.span), (0, 10000))
        self.assertTrue(wave.following)

    def test_short_track_and_zoomed_time_mapping_stay_in_bounds(self):
        wave, track = self.make_wave()
        track.duration = 0.2
        wave.show_track(track, None)
        wave.zoom(100, wave.areas()[0].center().x())
        self.assertEqual((wave.start, wave.span), (0, 200))
        wave, _track = self.make_wave()
        wave.span, wave.start = 500, 1000
        rect = wave.areas()[0]
        self.assertAlmostEqual(wave.ms_of(wave.x_of(1234.125, rect), rect), 1234.125)

    def test_white_needle_is_drawn_only_in_main_waveform(self):
        wave, _track = self.make_wave()
        wave.show()
        for start, span in ((0, 10000), (2000, 4000)):
            wave.start, wave.span = start, span
            wave.set_pos(4000)
            self.app.processEvents()
            image = QWidget.grab(wave).toImage()
            main, strip = wave.areas()
            scale = image.devicePixelRatio()
            x = int(wave.x_of(wave.needle, main))
            self.assertEqual(
                image.pixelColor(round(x * scale), round(main.center().y() * scale)),
                QColor(Qt.white),
            )
            strip_x = int(strip.left() + strip.width() * wave.needle / wave.dur)
            for y in range(int(strip.top()), int(strip.bottom())):
                self.assertNotEqual(
                    image.pixelColor(round(strip_x * scale), round(y * scale)), QColor(Qt.white),
                )

    def test_overview_scrubbing_still_seeks_and_centres_zoom_window(self):
        wave, _track = self.make_wave()
        positions = []
        wave.seek.connect(positions.append)
        wave.span = 2000
        strip = wave.areas()[1]
        wave.to_strip(strip.left() + strip.width() * 0.7, strip)
        self.assertEqual(positions, [7000])
        self.assertEqual(wave.needle, 7000)
        self.assertEqual(wave.start, 6000)
        self.assertTrue(wave.following)
        wave.to_strip(strip.right() + 100, strip)
        self.assertEqual(wave.needle, 10000)
        self.assertEqual(wave.start, 8000)
        self.assertEqual(positions[-1], 10000)

    def test_grid_handle_does_not_steal_hot_cue_at_same_position(self):
        wave, track = self.make_wave()
        cue = Cue(start=500, hotcue=0)
        track.cues.append(cue)
        main = wave.areas()[0]
        x = wave.x_of(500, main)
        self.assertTrue(wave.grid_at(x, main.top() + 3))
        self.assertIsNone(wave.cue_at(x, main.top() + 3))
        self.assertIs(wave.cue_at(x, main.top() + GRID_HEAD + 3), cue)
        self.assertFalse(wave.grid_at(x, main.top() + GRID_HEAD + 3))

    def test_grid_drag_is_unsnapped_clamped_and_escape_preserves_redo(self):
        wave, track = self.make_wave()
        before = cuehistory.snap(track)
        track.grid = 501
        cuehistory.record(track, before)
        cuehistory.undo(track)
        self.press_grid(wave)
        wave.snap_to_beat = True
        main = wave.areas()[0]
        wave.drag_grid(wave.x_of(1821.125, main))
        self.assertAlmostEqual(track.grid, 1821.125)
        wave.drag_grid(wave.x_of(-100, main))
        self.assertEqual(track.grid, 0)
        wave.drag_grid(wave.x_of(12000, main))
        self.assertEqual(track.grid, 10000)
        wave.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertEqual(track.grid, 500)
        self.assertTrue(cuehistory.redo(track))
        self.assertEqual(track.grid, 501)

    def test_grid_release_outside_without_move_event_cancels(self):
        wave, track = self.make_wave()
        self.press_grid(wave)
        wave.drag_grid(wave.x_of(2000, wave.areas()[0]))
        point = QPointF(wave.width() + 100, wave.height() + 100)
        wave.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, point, point,
                                          Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
        self.assertEqual(track.grid, 500)
        self.assertFalse(cuehistory.undo(track))


if __name__ == "__main__":
    unittest.main()
