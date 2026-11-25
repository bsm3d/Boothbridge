# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that timing exports carry the right positions, with empty audio and mocked analysis.

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from djapp import native
from djapp.analysis import Bands
from djapp.export import nml, rbxml
from djapp.export.cdj import anlz, usb
from djapp.model import LOOP, Cue, Node, Track
from djapp.sources import nml as nml_reader
from djapp.sources import project
from djapp.sources import rbxml as rbxml_reader


# ----------------------------------------------------------
class EmptyAudio:
    ok = False

    def at(self, n):
        z = np.zeros(n, dtype=np.float32)
        return Bands(0, z, z, z)


# ----------------------------------------------------------
class TimingExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.track = Track(path=str(Path(self.tmp.name) / "track.wav"), title="Timing",
                           duration=600.25, bpm=123.456789, grid=1820.125)
        Path(self.track.path).touch()
        self.track.cues = [Cue(start=2500.125, hotcue=0),
                           Cue(kind=LOOP, start=4000.5, length=2000.25, hotcue=1)]
        self.nodes = [Node("playlist", "First", tracks=[self.track]),
                      Node("playlist", "Shared", tracks=[self.track])]

    def assert_timing(self, track):
        self.assertAlmostEqual(track.bpm, self.track.bpm, places=6)
        self.assertAlmostEqual(track.grid, self.track.grid, places=3)
        self.assertEqual(len(track.cues), len(self.track.cues))
        for actual, expected in zip(track.cues, self.track.cues, strict=True):
            self.assertAlmostEqual(actual.start, expected.start, places=3)
            self.assertAlmostEqual(actual.length, expected.length, places=3)
            self.assertEqual(actual.hotcue, expected.hotcue)

    def test_project_roundtrip_preserves_precision_and_shared_track(self):
        path = str(Path(self.tmp.name) / "work.json")
        project.save(path, self.nodes, {})
        nodes = project.load(path)
        self.assertIs(nodes[0].tracks[0], nodes[1].tracks[0])
        self.assert_timing(nodes[0].tracks[0])

    def test_nml_export_roundtrip_preserves_edited_timing(self):
        report = nml.export(self.nodes, self.tmp.name)
        self.assert_timing(nml_reader.load(report.path)[0].tracks[0])

    def test_rekordbox_export_roundtrip_preserves_edited_timing(self):
        report = rbxml.export(self.nodes, self.tmp.name)
        self.assert_timing(rbxml_reader.load(report.path)[0].tracks[0])

    def test_cleared_grid_stays_absent_in_projects_and_xml_exports(self):
        self.track.grid = None
        path = str(Path(self.tmp.name) / "work.json")
        project.save(path, self.nodes, {})
        tracks = [project.load(path)[0].tracks[0],
                  nml_reader.load(nml.export(self.nodes, self.tmp.name).path)[0].tracks[0],
                  rbxml_reader.load(rbxml.export(self.nodes, self.tmp.name).path)[0].tracks[0]]
        for track in tracks:
            self.assertIsNone(track.grid)
            self.assertAlmostEqual(track.bpm, self.track.bpm, places=6)

    def test_zero_bpm_with_manual_marker_roundtrips_in_xml(self):
        self.track.bpm = 0
        self.assert_timing(nml_reader.load(nml.export(self.nodes, self.tmp.name).path)[0].tracks[0])
        self.assert_timing(rbxml_reader.load(rbxml.export(self.nodes, self.tmp.name).path)[0].tracks[0])

    def test_cdj_grid_phase_and_fractional_tempo_do_not_accumulate_rounding(self):
        data = anlz.pqtz(self.track)
        count = struct.unpack_from(">I", data, 20)[0]
        beats = [struct.unpack_from(">HHI", data, 24 + i * 8) for i in range(count)]
        step = 60000 / self.track.bpm
        back = int(self.track.grid // step)
        for i, (bar, tempo, ms) in enumerate(beats):
            self.assertEqual(tempo, round(self.track.bpm * 100))
            self.assertEqual(bar, (i - back) % 4 + 1)
            self.assertLessEqual(abs(ms - (self.track.grid + (i - back) * step)), 0.5)

    def test_cleared_grid_is_not_recreated_by_python_or_native_cdj_writer(self):
        self.track.grid = None
        self.assertEqual(anlz.beats(self.track), [])
        self.assertEqual(struct.unpack_from(">I", anlz.pqtz(self.track), 20)[0], 0)
        with patch.object(native, "available", return_value=True), \
                patch.object(native, "anlz_build", return_value=(b"dat", b"ext")) as build:
            self.assertEqual(anlz.build(self.track, "/Contents/track.wav", EmptyAudio()), (b"dat", b"ext"))
            self.assertEqual(build.call_args.args[1], 0)

    def test_cdj_export_cache_invalidates_when_timing_changes(self):
        first = usb.cache_key(self.track, "/Contents/track.wav")
        self.track.bpm += 0.001
        second = usb.cache_key(self.track, "/Contents/track.wav")
        self.track.grid += 1
        third = usb.cache_key(self.track, "/Contents/track.wav")
        self.assertEqual(len({first, second, third}), 3)

    def test_cdj_reports_unrepresentable_tempo_before_native_call(self):
        self.track.bpm = 1000
        with self.assertRaisesRegex(ValueError, "655.35"):
            anlz.pqtz(self.track)
        with patch.object(native, "anlz_build") as build:
            with self.assertRaisesRegex(ValueError, "655.35"):
                anlz.build(self.track, "/Contents/track.wav", EmptyAudio())
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
