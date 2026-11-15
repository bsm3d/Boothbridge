import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.model import CUE, LOOP, Cue, Track
from djapp.ui.player import Engine, Player
from djapp.ui.waveform import Loader
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


class PlaylistPlayerCueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.track = Track(title="Test track", bpm=120)
        self.engine = Engine()
        self.player = Player(self.engine, Loader(), {})
        self.player.track = self.track
        self.player.path = "test-track.mp3"
        self.player.mark = 1250

    def tearDown(self):
        self.player.close()
        self.player.loader.stop()
        self.engine.close()
        self.player.deleteLater()
        self.engine.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_playlist_waveform_is_taller(self):
        self.assertEqual(self.player.view.minimumHeight(), 86)

    def test_snap_and_metronome_follow_cue_pads_on_single_toolbar(self):
        bar = self.player.transport_controls.layout()
        widgets = [bar.itemAt(i).widget() for i in range(bar.count())]
        self.assertEqual(widgets[:2], [self.player.btn, self.player.time])
        self.assertEqual(widgets[2].text(), "Hot cues")
        self.assertEqual(widgets[3:11], self.player.pads)
        for button in self.player.pads:
            self.assertEqual((button.width(), button.height()), (26, 26))
        self.assertEqual((self.player.snap_btn.width(), self.player.snap_btn.height()), (30, 26))
        pad = widgets.index(self.player.pads[-1])
        snap = widgets.index(self.player.snap_btn)
        self.assertGreater(snap, pad)
        self.assertIs(widgets[snap + 1], self.player.metronome_controls)
        playback = self.player.toolbar.itemAtPosition(0, 1).layout()
        self.assertIsNotNone(playback.itemAt(0).spacerItem())
        self.assertIs(playback.itemAt(1).widget(), self.player.mode)
        self.assertEqual(playback.itemAt(2).widget().text(), "Fade")
        self.assertIs(playback.itemAt(3).widget(), self.player.fade_box)
        self.assertEqual(self.player.metronome_controls.button.text(), "")
        self.assertFalse(self.player.metronome_controls.button.icon().isNull())
        self.assertTrue(self.player.metronome_controls.status.isHidden())
        self.assertIs(self.player.metronome_controls.layout().itemAt(0).widget(),
                      self.player.metronome_controls.button)

    def test_pad_menu_creates_selected_loop_and_removes_it(self):
        menu = self.player.pad_menu(3)
        self.assertEqual(
            [action.text() for action in menu.actions()],
            ["Set 1-beat loop", "Set 2-beat loop", "Set 4-beat loop", "Set 8-beat loop"],
        )
        menu.actions()[3].trigger()

        loop = self.track.cues[0]
        self.assertEqual((loop.kind, loop.hotcue, loop.start, loop.length), (LOOP, 3, 1250, 4000))
        self.assertIsNone(self.player.loop)

        remove_menu = self.player.pad_menu(3)
        remove_menu.actions()[-1].trigger()
        self.assertEqual(self.track.cues, [])

    def test_loop_pad_toggles_active_state_visibly(self):
        loop = Cue(kind=LOOP, start=1000, length=2000, hotcue=1)
        self.track.cues.append(loop)
        self.player.refresh_pads()

        self.player.pads[1].click()
        self.assertIs(self.player.loop, loop)
        self.assertIn("border: 2px solid white", self.player.pads[1].styleSheet())

        self.player.pads[1].click()
        self.assertIsNone(self.player.loop)
        self.assertNotIn("border: 2px solid white", self.player.pads[1].styleSheet())

    def test_loop_menu_replaces_cue_in_its_slot(self):
        self.track.cues.append(Cue(kind=CUE, start=300, hotcue=2))

        self.player.pad_menu(2).actions()[1].trigger()

        self.assertEqual(len(self.track.cues), 1)
        self.assertEqual((self.track.cues[0].kind, self.track.cues[0].length), (LOOP, 1000))

    def test_snap_toggle_quantizes_new_cues_and_loops_to_grid(self):
        self.track.grid = 500
        self.player.mark = 1820

        self.player.snap_btn.click()
        self.assertEqual(self.player.snap_btn.text(), "S")
        self.player.pad(0)

        self.assertEqual(self.track.cues[0].start, 2000)
        self.player.set_loop(1, 2)
        self.assertEqual(self.track.cues[1].start, 2000)
        self.assertEqual(self.track.cues[1].length, 1000)

    def test_snap_can_be_disabled_for_precise_unsnapped_positions(self):
        self.track.grid = 500
        self.player.mark = 1820
        self.player.snap_btn.setChecked(True)
        self.player.snap_btn.setChecked(False)
        self.assertEqual(self.player.snap_btn.text(), "S")

        self.player.pad(0)

        self.assertEqual(self.track.cues[0].start, 1820)

    def test_playing_another_track_during_a_crossfade_drops_the_stale_fade(self):
        tracks = [Track(title=f"t{i}", path=f"t{i}.wav", duration=2.0) for i in range(3)]
        self.player.tracks = tracks
        with patch.object(os.path, "isfile", return_value=True):
            self.player.load(tracks[0], tracks[0].path)
            self.engine.play(tracks[0].path, tracks[0])
            self.player.start_next(1000)
            self.assertEqual(self.engine.next_path, tracks[1].path)
            # the user plays t2 from the list while the crossfade to t1 runs
            self.player.load(tracks[2], tracks[2].path)
            self.player.play()
            for _ in range(30):
                self.player.tick()
        self.assertEqual((self.engine.path, self.player.track, self.player.fading),
                         (tracks[2].path, tracks[2], 0.0))


def write_wav(path: str, seconds: float):
    import wave

    import numpy as np

    rate = 44100
    n = int(rate * seconds)
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(n) / rate) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.repeat(tone, 2).tobytes())


# a real file through the Qt backend, which drops a position set while the media loads
class PlayerRealFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "tone.wav")
        write_wav(cls.path, 4.0)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.engine = Engine()
        self.engine.set_volume(0.0)
        self.player = Player(self.engine, Loader(), {})

    def tearDown(self):
        self.player.stop()
        self.player.close()
        self.player.loader.stop()
        self.engine.close()
        self.player.deleteLater()
        self.engine.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def wait(self, done, seconds=4.0):
        from time import monotonic

        end = monotonic() + seconds
        while monotonic() < end and not done():
            self.app.processEvents()
        return done()

    def test_a_needle_set_before_play_is_where_play_starts(self):
        t = Track(title="tone", path=self.path, duration=4.0)
        self.player.load(t, self.path)
        self.player.seek(1500)
        self.player.play()
        self.assertTrue(self.wait(lambda: self.engine.a.position() >= 1400),
                        f"played from {self.engine.a.position()} ms")
        self.assertIsNone(self.engine.pending_seek)

    def test_stop_forgets_a_position_asked_while_loading(self):
        t = Track(title="tone", path=self.path, duration=4.0)
        self.player.load(t, self.path)
        self.player.play()
        self.engine.seek(1500)
        self.engine.stop()
        self.assertIsNone(self.engine.pending_seek)

    def test_the_length_the_player_reads_wins_over_the_tags(self):
        t = Track(title="tone", path=self.path, duration=6.0)  # a wrong tag
        self.player.load(t, self.path)
        self.assertAlmostEqual(self.player.view.dur, 6000, delta=1)
        self.player.play()
        self.assertTrue(self.wait(lambda: self.engine.a.duration() > 0))
        self.player.tick()
        self.assertAlmostEqual(self.player.view.dur, 4000, delta=50)
        self.assertAlmostEqual(self.player.view.span, 4000, delta=50)
        self.assertTrue(self.player.time.text().endswith("/ 00:04.0"),self.player.time.text())


if __name__ == "__main__":
    unittest.main()
