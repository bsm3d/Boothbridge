# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the timeline cue pad: cue editing and playback positions, run offscreen.

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.model import CUE, LOOP, Cue, Track
from djapp.ui import theme
from djapp.ui.player import Engine, Player
from djapp.ui.timeline import CueEdit, Timeline, parse_clock
from djapp.ui.waveform import Loader
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


# --------------------------------------------------
class TimelineCuePadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt" and not QFontDatabase.families():
            for name in ("segoeui.ttf", "seguisym.ttf"):
                font = Path(os.environ["WINDIR"]) / "Fonts" / name
                if QFontDatabase.addApplicationFont(str(font)) < 0:
                    raise RuntimeError(f"Cannot load the Windows UI font for offscreen layout checks: {font}")
            cls.app.setFont(QFont("Segoe UI", 9))

    def setUp(self):
        self.track = Track(title="Test track", artist="Test artist", bpm=120)
        self.loader = Loader()
        self.timeline = Timeline(self.track, "", self.loader, {})

    def tearDown(self):
        self.timeline.close()
        self.app.processEvents()

    def test_eight_letter_pads_set_and_jump_to_hot_cues(self):
        self.assertEqual(len(self.timeline.cue_pads), 8)
        self.assertEqual([button.text() for button in self.timeline.cue_pads], list("ABCDEFGH"))

        self.timeline.canvas.set_pos(1250)
        self.timeline.cue_pads[2].click()
        cue = next(cue for cue in self.track.cues if cue.hotcue == 2)
        self.assertEqual((cue.kind, cue.start), (CUE, 1250))
        self.assertIn(theme.CUE_COLORS[2], self.timeline.cue_pads[2].styleSheet())

        self.timeline.canvas.set_pos(3000)
        self.timeline.cue_pads[2].click()
        self.assertEqual(self.timeline.position(), 1250)

    def test_snap_and_metronome_follow_hot_cue_pads_on_one_row(self):
        engine = Engine()
        timeline = Timeline(self.track, "", self.loader, {}, engine)
        try:
            layout = timeline.layout()
            rows = [layout.itemAt(i).layout() for i in range(layout.count()) if layout.itemAt(i).layout()]
            pads = next(r for r in rows
                        if any(r.itemAt(i).widget() is timeline.cue_pads[-1] for i in range(r.count())))
            widgets = [pads.itemAt(i).widget() for i in range(pads.count())]
            snap = widgets.index(timeline.snap_btn)
            self.assertGreater(snap, widgets.index(timeline.cue_pads[-1]))
            self.assertIs(widgets[snap + 1], timeline.metronome_controls)
        finally:
            timeline.close()
            engine.close()
            engine.deleteLater()

    def test_toolbar_actions_and_footer_follow_requested_order(self):
        layout = self.timeline.layout()
        toolbar = layout.itemAt(2).layout()
        widgets = [toolbar.itemAt(i).widget() for i in range(toolbar.count())]
        self.assertIs(widgets[0], self.timeline.play_button)
        self.assertEqual(widgets[1].text(), "Hot cues")
        self.assertEqual(widgets[2:10], self.timeline.cue_pads)
        self.assertIs(widgets[10], self.timeline.snap_btn)
        self.assertEqual([w.text() for w in widgets[-4::2]], ["Tempo", "First beat"])
        self.assertIs(widgets[-3], self.timeline.bpm_box)
        self.assertIs(widgets[-1], self.timeline.grid_box)
        actions = layout.itemAt(3).layout()
        buttons = [actions.itemAt(i).widget() for i in range(actions.count())]
        self.assertEqual(
            buttons[:4], [self.timeline.set_grid_button, self.timeline.clear_grid_button,
                          self.timeline.add_button, self.timeline.add_loop_button],
        )
        self.assertEqual(buttons[4].text(), "Nudge")
        self.assertEqual(buttons[5:9], self.timeline.nudge_buttons)
        self.assertEqual(buttons[9:11], [self.timeline.detect_button, self.timeline.apply_grid_button])
        self.assertIsNone(buttons[11])
        self.assertEqual(buttons[12:], [self.timeline.show_hot, self.timeline.show_mem,
                                       self.timeline.show_loop])
        footer = layout.itemAt(5).layout()
        widgets = [footer.itemAt(i).widget() for i in range(footer.count())]
        self.assertIs(widgets[0], self.timeline.stats)
        self.assertIs(widgets[2], self.timeline.shortcut_hint)
        self.assertEqual(widgets[3:6], [self.timeline.edit_button, self.timeline.delete_button,
                                       self.timeline.copy_button])
        self.assertEqual(widgets[-1].text(), "Close")

    def test_compact_toolbar_fits_and_zoom_spans_waveform_height(self):
        engine = Engine()
        timeline = Timeline(self.track, "", self.loader, {}, engine)
        try:
            timeline.setStyleSheet(theme.STYLE)
            timeline.show()
            for width in (1280, 1600):
                timeline.resize(width, 540)
                self.app.processEvents()
                self.assertEqual(timeline.width(), width)
                controls = timeline.zoom_controls
                self.assertEqual(controls.width(), 36)
                self.assertEqual(controls.y(), timeline.canvas.y())
                self.assertEqual(controls.height(), timeline.canvas.height())
                self.assertEqual(controls.in_button.y(), 0)
                self.assertEqual(controls.out_button.geometry().bottom(), controls.height() - 1)
                self.assertLessEqual(
                    abs(controls.reset_button.geometry().center().y() - controls.rect().center().y()), 1,
                )
                self.assertLessEqual(timeline.grid_box.geometry().right(), width)
                self.assertLessEqual(timeline.show_loop.geometry().right(), width)
                self.assertGreater(timeline.bpm_box.x(), timeline.metronome_controls.geometry().right())
                self.assertGreater(timeline.show_hot.x(), timeline.apply_grid_button.geometry().right())
                for button in (controls.in_button, controls.reset_button, controls.out_button):
                    self.assertEqual((button.width(), button.height()), (36, 30))
                    self.assertEqual(button.geometry().center().x(), controls.rect().center().x())
                self.assertEqual(timeline.metronome_controls.button.text(), "")
                self.assertFalse(timeline.metronome_controls.button.icon().isNull())
                self.assertFalse(timeline.metronome_controls.status.isVisible())
                self.assertGreaterEqual(
                    timeline.shortcut_hint.width(),
                    timeline.shortcut_hint.fontMetrics().horizontalAdvance(timeline.shortcut_hint.text()),
                )
        finally:
            timeline.close()
            engine.close()
            engine.deleteLater()

    def test_cue_list_names_the_colour_with_a_swatch_and_keeps_the_text_colour(self):
        self.track.cues.append(Cue(start=1250, hotcue=2))
        self.timeline.fill()
        item = self.timeline.table.topLevelItem(0)
        self.assertFalse(item.icon(1).isNull())
        swatch = item.icon(1).pixmap(14, 14).toImage().pixelColor(7, 7)
        self.assertEqual(swatch.name(), theme.CUE_COLORS[2].lower())
        self.assertEqual(item.foreground(1).style(), Qt.NoBrush)

    def test_edit_delete_enablement_tracks_selection_and_filters(self):
        self.track.cues.append(Cue(start=1250, hotcue=0))
        self.timeline.fill()
        self.assertFalse(self.timeline.edit_button.isEnabled())
        self.assertFalse(self.timeline.delete_button.isEnabled())
        self.timeline.table.topLevelItem(0).setSelected(True)
        self.assertTrue(self.timeline.edit_button.isEnabled())
        self.assertTrue(self.timeline.delete_button.isEnabled())
        self.timeline.show_hot.setChecked(False)
        self.assertFalse(self.timeline.edit_button.isEnabled())
        self.assertFalse(self.timeline.delete_button.isEnabled())

    def test_nudge_labels_fit_padding_and_grow_with_font_size(self):
        self.timeline.setStyleSheet(theme.STYLE)
        self.timeline.show()
        initial_widths = None
        for size in (10, 18):
            for button in self.timeline.nudge_buttons:
                button.setStyleSheet(
                    f"QPushButton {{ padding: 5px 6px; font-size: {size}pt; }}"
                )
            self.app.processEvents()
            widths = []
            for button in self.timeline.nudge_buttons:
                self.assertGreaterEqual(button.minimumWidth(), 46)
                self.assertGreater(button.maximumWidth(), button.minimumWidth())
                self.assertIn("padding: 5px 6px", button.styleSheet())
                text_width = button.fontMetrics().horizontalAdvance(button.text())
                self.assertGreaterEqual(button.width(), text_width + 12)
                self.assertGreaterEqual(button.height(), button.fontMetrics().height() + 10)
                widths.append(button.width())
            if initial_widths is None:
                initial_widths = widths
            else:
                self.assertGreater(widths[0], initial_widths[0])
                self.assertGreater(widths[-1], initial_widths[-1])

    def test_compact_fields_retain_original_timing_precision(self):
        self.track.duration = 7200
        self.timeline.refresh_beatgrid()
        self.timeline.bpm_box.setValue(123.456789)
        self.timeline.grid_box.setValue(7123456.789)
        self.timeline.apply_beatgrid()
        self.assertEqual(self.track.bpm, 123.456789)
        self.assertEqual(self.track.grid, 7123456.789)
        self.assertEqual(self.timeline.bpm_box.decimals(), 6)
        self.assertEqual(self.timeline.grid_box.decimals(), 3)

    def test_pad_menu_has_same_loop_options_and_removal_as_playlist(self):
        self.track.cues.append(Cue(kind=CUE, start=200, hotcue=5))
        self.timeline.fill()
        self.timeline.canvas.set_pos(4000)

        menu = self.timeline.pad_menu(5)
        self.assertEqual(
            [action.text() for action in menu.actions()],
            ["Set 1-beat loop", "Set 2-beat loop", "Set 4-beat loop", "Set 8-beat loop",
             "", "Remove cue F"],
        )
        menu.actions()[3].trigger()

        slot = [cue for cue in self.track.cues if cue.hotcue == 5]
        self.assertEqual(len(slot), 1)
        self.assertEqual((slot[0].kind, slot[0].start, slot[0].length), (LOOP, 4000, 4000))
        self.assertNotIn("border: 2px solid white", self.timeline.cue_pads[5].styleSheet())

        self.timeline.cue_pads[5].click()
        self.assertIs(self.timeline.loop, slot[0])
        self.assertIn("border: 2px solid white", self.timeline.cue_pads[5].styleSheet())

        self.timeline.cue_pads[5].click()
        self.assertIsNone(self.timeline.loop)
        self.assertNotIn("border: 2px solid white", self.timeline.cue_pads[5].styleSheet())

        self.timeline.pad_menu(5).actions()[-1].trigger()
        self.assertFalse(self.track.cues)

    def test_loop_pad_menu_options_match_playlist(self):
        self.assertEqual(
            [action.text() for action in self.timeline.pad_menu(0).actions()],
            ["Set 1-beat loop", "Set 2-beat loop", "Set 4-beat loop", "Set 8-beat loop"],
        )

    def test_active_loop_state_is_shared_with_playlist_player(self):
        engine = Engine()
        self.addCleanup(engine.close)
        player = Player(engine, self.loader, {})
        self.addCleanup(player.close)
        timeline = Timeline(self.track, "", self.loader, {}, engine)
        self.addCleanup(timeline.close)
        player.track = self.track
        player.path = timeline.path
        loop = Cue(kind=LOOP, start=1000, length=2000, hotcue=1)
        self.track.cues.append(loop)
        timeline.fill()

        timeline.cue_pads[1].click()
        self.assertEqual(engine.loop, (self.track, loop))
        player.tick()
        self.assertIn("border: 2px solid white", player.pads[1].styleSheet())

        player.pads[1].click()
        self.assertIsNone(engine.loop)
        timeline.refresh_cue_pads()
        self.assertNotIn("border: 2px solid white", timeline.cue_pads[1].styleSheet())

    def test_snap_toggle_quantizes_cues_and_loops_and_is_shared(self):
        engine = Engine()
        self.addCleanup(engine.close)
        player = Player(engine, self.loader, {})
        self.addCleanup(player.close)
        timeline = Timeline(self.track, "", self.loader, {}, engine)
        self.addCleanup(timeline.close)
        self.track.grid = 500
        timeline.canvas.set_pos(1820)

        timeline.snap_btn.click()
        self.assertTrue(engine.snap_to_beat)
        self.assertEqual(timeline.snap_btn.text(), "S")
        player.tick()
        self.assertTrue(player.snap_btn.isChecked())
        self.assertEqual(player.snap_btn.text(), "S")
        timeline.pad(0)
        self.assertEqual(self.track.cues[0].start, 2000)
        timeline.set_loop(1, 2)
        self.assertEqual(self.track.cues[1].start, 2000)
        self.assertEqual(self.track.cues[1].length, 1000)

        player.snap_btn.click()
        self.assertFalse(engine.snap_to_beat)
        timeline.tick()
        self.assertFalse(timeline.snap_btn.isChecked())
        self.assertEqual(timeline.snap_btn.text(), "S")
        self.assertFalse(timeline.canvas.snap_to_beat)

    def test_pads_refresh_after_undo(self):
        self.timeline.canvas.set_pos(500)
        self.timeline.pad(0)
        self.assertIn(theme.CUE_COLORS[0], self.timeline.cue_pads[0].styleSheet())

        self.timeline.undo()

        self.assertFalse(self.track.cues)
        self.assertIn(theme.BG_MED, self.timeline.cue_pads[0].styleSheet())

    def test_cue_edit_refuses_infinite_time_and_loop_length(self):
        for text in ("inf", "-inf", "1e999", "nan", "1:inf", "1:nan"):
            self.assertIsNone(parse_clock(text), text)
        cue = Cue(start=1000)
        self.track.cues.append(cue)
        edit = CueEdit(cue, self.timeline)
        edit.time.setText("1e999")
        edit.length.setText("inf")
        edit.apply(cue, 0)
        edit.deleteLater()
        self.assertEqual((cue.start, cue.length, cue.kind), (1000, 0.0, CUE))
        self.timeline.fill()
        self.assertEqual(self.timeline.table.topLevelItem(0).text(2), "")

    def test_beatgrid_controls_apply_and_share_undo_redo(self):
        self.track.duration = 10
        self.track.grid = 500
        self.timeline.refresh_beatgrid()
        self.timeline.bpm_box.setValue(121.5)
        self.timeline.grid_box.setValue(525.5)

        self.timeline.apply_beatgrid()

        self.assertEqual((self.track.bpm, self.track.grid), (121.5, 525.5))
        self.timeline.undo()
        self.assertEqual((self.track.bpm, self.track.grid), (120, 500))
        self.timeline.undo(redo=True)
        self.assertEqual((self.track.bpm, self.track.grid), (121.5, 525.5))

    def test_beatgrid_set_cursor_nudge_clear_and_undo(self):
        self.track.duration = 10
        self.timeline.refresh_beatgrid()
        self.timeline.canvas.set_pos(1820)

        self.timeline.set_grid_at_cursor()
        self.assertEqual(self.track.grid, 1820)
        self.timeline.nudge_grid(1)
        self.assertEqual(self.track.grid, 1821)
        self.timeline.clear_grid()
        self.assertIsNone(self.track.grid)

        self.timeline.undo()
        self.assertEqual(self.track.grid, 1821)

    def test_beatgrid_undo_does_not_replace_cues_or_deactivate_active_loop(self):
        loop = Cue(kind=LOOP, start=1000, length=2000, hotcue=0)
        self.track.cues.append(loop)
        self.timeline.loop = loop
        self.timeline.bpm_box.setValue(125)
        self.timeline.apply_beatgrid()
        self.timeline.undo()
        self.assertIs(self.track.cues[0], loop)
        self.assertIs(self.timeline.loop, loop)
        self.assertEqual((loop.start, loop.length), (1000, 2000))

    def test_metronome_controls_are_shared_and_volume_is_independent(self):
        engine = Engine()
        self.addCleanup(engine.close)
        self.track.grid = 500
        player = Player(engine, self.loader, {})
        self.addCleanup(player.close)
        player.track = self.track
        player.metronome_controls.refresh()
        timeline = Timeline(self.track, "", self.loader, {}, engine)
        self.addCleanup(timeline.close)
        timeline.metronome_controls.button.click()
        self.assertTrue(engine.metronome.enabled)
        self.assertTrue(player.metronome_controls.button.isChecked())
        player.metronome_controls.volume.setValue(25)
        self.assertEqual(engine.metronome.volume, 0.25)
        self.assertEqual(engine.volume, 0.7)
        self.assertEqual(timeline.metronome_controls.volume.value(), 25)
        timeline.metronome_controls.offset.setValue(-35)
        self.assertEqual(engine.metronome.offset_ms, -35)
        self.assertEqual(player.metronome_controls.offset.value(), -35)
        player.metronome_controls.button.click()
        self.assertFalse(engine.metronome.enabled)
        self.assertFalse(timeline.metronome_controls.button.isChecked())


if __name__ == "__main__":
    unittest.main()
