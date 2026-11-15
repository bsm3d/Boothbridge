import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.sources import project
from djapp.ui.main_window import playlist_duration
from djapp.ui.zoomwave import clock

BIG = "1" + "0" * 400


# ------------------------------------------------------------
class HostileProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hostile.tbproj"

    def read(self, track: str, settings: str = "{}", cue: str = "{}"):
        # written by hand, so that 1e999 stays a literal in the file
        text = ('{"format": "FORMAT", "version": 1, "settings": SETTINGS, "tracks": [TRACK], '
                '"tree": [{"kind": "playlist", "name": "p", "tracks": [0]}]}')
        for key, value in (("FORMAT", project.FORMAT), ("SETTINGS", settings), ("TRACK", track.replace("CUE", cue))):
            text = text.replace(key, value)
        self.path.write_text(text, encoding="utf-8")
        nodes, cfg = project.read(str(self.path))
        return nodes[0].tracks[0], nodes[0], cfg

    def test_overflowing_key_and_non_finite_numbers_load_as_unset(self):
        for text in ("1e999", "-1e999", "NaN", "Infinity"):
            with self.subTest(value=text):
                track, node, _cfg = self.read('{"title": "x", "key": %s, "grid": %s, "duration": %s, '
                                              '"bpm": %s, "gain": %s, "energy": %s}' % ((text,) * 6))
                self.assertIsNone(track.key)
                self.assertIsNone(track.grid)
                self.assertIsNone(track.energy)
                self.assertEqual((track.duration, track.bpm, track.gain), (0.0, 0.0, 0.0))
                self.assertEqual(clock(track.duration * 1000), "00:00.0")
                self.assertEqual(playlist_duration(node), "0:00:00")
                self.assertEqual(track.title, "x")

    def test_integers_too_large_for_a_float_load_as_unset(self):
        track, node, _cfg = self.read('{"grid": %s, "duration": %s, "bpm": %s, "energy": %s}' % ((BIG,) * 4))
        self.assertIsNone(track.grid)
        self.assertIsNone(track.energy)
        self.assertEqual((track.duration, track.bpm), (0.0, 0.0))
        self.assertEqual(playlist_duration(node), "0:00:00")

    def test_non_finite_cue_numbers_keep_their_defaults(self):
        track, _node, _cfg = self.read('{"cues": [CUE]}', cue='{"start": 1e999, "length": NaN, "hotcue": 1e999}')
        cue = track.cues[0]
        self.assertEqual((cue.start, cue.length, cue.hotcue), (0.0, 0.0, -1))

    def test_a_key_outside_the_wheel_is_no_key(self):
        for text in ("24", "-1", "99"):
            with self.subTest(key=text):
                self.assertIsNone(self.read('{"key": %s}' % text)[0].key)
        self.assertEqual(self.read('{"key": 23}')[0].key, 23)

    def test_finite_values_still_load(self):
        track, _node, _cfg = self.read('{"key": 5, "grid": 12.5, "duration": 200.25, "bpm": 120, "energy": 7.5}')
        self.assertEqual((track.key, track.grid, track.duration, track.bpm, track.energy),
                         (5, 12.5, 200.25, 120.0, 7.5))

    def test_non_finite_project_settings_are_dropped(self):
        _track, _node, cfg = self.read("{}", settings='{"crossfade": 1e999, "key_format": "Camelot", '
                                                       '"volume": 1}')
        self.assertNotIn("crossfade", cfg)
        self.assertEqual(cfg["key_format"], "Camelot")

# ------------------------------------------------------------
# containers of the wrong type in a project file are a load error, never a TypeError
class ProjectShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "shape.tbproj"

    def read(self, tracks: str = "[]", tree: str = "[]", settings: str = "{}"):
        self.path.write_text(f'{{"format": "{project.FORMAT}", "version": 1, "settings": {settings}, '
                             f'"tracks": {tracks}, "tree": {tree}}}', encoding="utf-8")
        return project.read(str(self.path))

    def test_seed_115_the_cues_of_a_track_are_a_number(self):
        with self.assertRaisesRegex(ValueError, "shape.tbproj is not a valid project: the cues of a track must be a list"):
            self.read(tracks='[{"title": "a"}, {"title": "b", "cues": 2147483648}]')

    def test_seed_219_the_tracks_of_a_playlist_are_a_float_inside_nested_folders(self):
        tree = ('[{"kind": "folder", "name": "a", "children": [{"kind": "folder", "name": "b", "children": '
                '[{"kind": "playlist", "name": "c", "tracks": -1e+308}]}]}]')
        with self.assertRaisesRegex(ValueError, "the tracks of a playlist must be a list"):
            self.read(tracks='[{"title": "a"}]', tree=tree)

    def test_seed_265_the_cues_of_a_track_are_true(self):
        with self.assertRaisesRegex(ValueError, "the cues of a track must be a list"):
            self.read(tracks='[{"title": "a"}, {"title": "b", "cues": true}]')

    def test_every_container_refuses_every_wrong_type_with_a_load_error(self):
        wrong = ("5", "1.5", "true", "false", "0", '""', '"abc"', "{}", '{"a": 1}')
        places = (
            ("tracks", lambda v: {"tracks": v}),
            ("tree", lambda v: {"tree": v}),
            ("the cues of a track", lambda v: {"tracks": '[{"cues": VALUE}]'.replace("VALUE", v)}),
            ("the children of a folder", lambda v: {"tree": '[{"kind": "folder", "children": VALUE}]'.replace("VALUE", v)}),
            ("the tracks of a playlist", lambda v: {"tree": '[{"kind": "playlist", "tracks": VALUE}]'.replace("VALUE", v)}),
        )
        for message, args in places:
            for value in wrong:
                with self.subTest(container=message, value=value),                         self.assertRaisesRegex(ValueError, f"is not a valid project: {message} must be a list"):
                    self.read(**args(value))

    def test_missing_null_and_empty_containers_still_load(self):
        for tracks, tree in (("[]", "[]"), ("null", "null")):
            with self.subTest(tracks=tracks, tree=tree):
                self.assertEqual(self.read(tracks, tree)[0], [])
        nodes, _cfg = self.read('[{"cues": null}, {"cues": []}, {"cues": [5, {"start": 100}]}]',
                                '[{"kind": "folder", "children": null}, {"kind": "playlist", "tracks": null}]')
        self.assertEqual([(n.kind, n.children, n.tracks) for n in nodes], [("folder", [], []), ("playlist", [], [])])
        self.path.write_text(f'{{"format": "{project.FORMAT}", "version": 1}}', encoding="utf-8")
        self.assertEqual(project.read(str(self.path)), ([], {}))

    def test_a_path_setting_that_is_not_text_is_dropped(self):
        for value in ("5", "1.5", "true"):
            with self.subTest(value=value):
                settings = ('{"music_root": VALUE, "source_path": VALUE, "output_path": VALUE, '
                            '"key_format": "Camelot"}').replace("VALUE", value)
                _nodes, cfg = self.read(settings=settings)
                self.assertEqual(cfg, {"key_format": "Camelot"})

    def test_seed_737_durations_that_add_up_to_infinity_are_not_durations(self):
        big = "1.7976931348623157e+308"
        nodes, _cfg = self.read(tracks='[{"duration": %s, "bpm": %s, "gain": %s, "grid": %s, "cues": [{"start": %s}]}, '
                                       '{"duration": %s}]' % ((big,) * 6),
                                tree='[{"kind": "playlist", "name": "p", "tracks": [0, 1]}]')
        first = nodes[0].tracks[0]
        self.assertEqual((first.duration, first.bpm, first.gain, first.grid, first.cues[0].start), (0.0, 0.0, 0.0, None, 0.0))
        self.assertEqual(playlist_duration(nodes[0]), "0:00:00")
