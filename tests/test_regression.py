# real temporary audio -> tags -> sources -> project -> exports -> backup/restore

import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile
from djapp import (
    backup,
    beatdetect,
    export,
    settings,
    sources,
    tags,
    tagwrite,
)
from djapp.export import manifest
from djapp.export.cdj import usb
from djapp.model import LOOP, Cue, Node, playlists
from djapp.sources import project

from tests.test_beatdetect import RATE, click_track


# ----------------------------------------
class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        for index, bpm in enumerate((124, 128)):
            path = self.music / f"track{index}.wav"
            soundfile.write(path, click_track(bpm, 90, secs=20), RATE, subtype="PCM_16")
            tagwrite.write(str(path), {"title": f"Track {index}", "artist": "Regression",
                                      "album": "Synthetic", "bpm": str(bpm), "key": "8A"})
        self.nodes = sources.load(str(self.music))
        self.tracks = self.nodes[0].tracks
        for track in self.tracks:
            result = beatdetect.analyze(track.path)
            track.bpm, track.grid = result.bpm, result.first_beat_ms
            track.energy = 6.25
            track.cues = [Cue(start=500.125, hotcue=0, name="Start"),
                          Cue(kind=LOOP, start=2000.5, length=60000 / track.bpm * 4, hotcue=1)]
        self.nodes.append(Node("playlist", "Shared", tracks=[self.tracks[0]]))
        self.cfg = dict(settings.DEFAULTS, copy_music=True, verify_copy=True, anlz_processes=1)
        self.original = {track.path: hashlib.sha256(Path(track.path).read_bytes()).hexdigest()
                         for track in self.tracks}

    def assert_sources_unchanged(self):
        self.assertEqual(self.original, {
            path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in self.original})

    def assert_timing(self, actual, expected):
        self.assertEqual(actual.title, expected.title)
        self.assertEqual(actual.artist, expected.artist)
        self.assertAlmostEqual(actual.bpm, expected.bpm, delta=0.000001)
        self.assertAlmostEqual(actual.grid, expected.grid, delta=0.001)
        self.assertEqual(len(actual.cues), 2)
        for actual_cue, expected_cue in zip(actual.cues, expected.cues, strict=True):
            self.assertAlmostEqual(actual_cue.start, expected_cue.start, delta=0.001)
            self.assertAlmostEqual(actual_cue.length, expected_cue.length, delta=0.001)
            self.assertEqual(actual_cue.hotcue, expected_cue.hotcue)

    def test_tags_detection_project_and_portable_backup_restore(self):
        for index, expected_bpm in enumerate((124, 128)):
            track = self.tracks[index]
            self.assertAlmostEqual(track.bpm, expected_bpm, delta=0.1)
            self.assertAlmostEqual(track.grid, 90, delta=5)
            self.assertEqual(tags.basic(track.path)["title"], f"Track {index}")
            backup_path = Path(track.path + ".bb-tags.bak")
            self.assertTrue(backup_path.is_file())
            original_audio, rate = soundfile.read(backup_path)
            tagged_audio, tagged_rate = soundfile.read(track.path)
            np.testing.assert_array_equal(original_audio, tagged_audio)
            self.assertEqual(rate, tagged_rate)
        saved = self.root / "project.json"
        project.save(str(saved), self.nodes, self.cfg)
        nodes, cfg = project.read(str(saved))
        self.assertEqual(cfg["key_format"], self.cfg["key_format"])
        self.assertIs(nodes[0].tracks[0], nodes[1].tracks[0])
        for actual, expected in zip(nodes[0].tracks, self.tracks, strict=True):
            self.assert_timing(actual, expected)
        archive = self.root / "backup.zip"
        result = backup.create(str(archive), project.dump(nodes, cfg))
        self.assertEqual(result["audio"], 2)
        self.assertEqual(backup.verify(str(archive)), hashlib.sha256(archive.read_bytes()).hexdigest())
        restored_path = backup.restore(str(archive), str(self.root / "restored"))
        restored = project.load(restored_path)
        self.assertIs(restored[0].tracks[0], restored[1].tracks[0])
        self.assertEqual(restored[0].tracks[0].energy, 6.25)
        for actual, expected in zip(restored[0].tracks, self.tracks, strict=True):
            self.assert_timing(actual, expected)
            self.assertEqual(manifest.sha256_file(actual.path), self.original[expected.path])
        self.assert_sources_unchanged()

    def test_all_file_exports_roundtrip_audio_order_timing_and_checksums(self):
        for fmt in ("M3U", "Traktor NML", "Rekordbox XML"):
            with self.subTest(format=fmt):
                out = self.root / fmt.replace(" ", "_")
                report = export.run(fmt, self.nodes, str(out), self.cfg)
                self.assertEqual(report.errors, [])
                self.assertEqual(report.missing, [])
                self.assertTrue(Path(report.manifest).is_file())
                verified = manifest.verify(str(out))
                self.assertEqual(verified.damaged, [])
                self.assertEqual(verified.missing, [])
                self.assertFalse(verified.error)
                if fmt == "M3U":
                    files = sorted(out.rglob("*.m3u8"))
                    self.assertEqual(len(files), 2)
                    restored = [sources.load(str(path))[0] for path in files]
                else:
                    restored = list(playlists(sources.load(report.path)))
                main = next(node for node in restored if len(node.tracks) == 2)
                self.assertEqual([t.title for t in main.tracks], [t.title for t in self.tracks])
                for actual, expected in zip(main.tracks, self.tracks, strict=True):
                    self.assertEqual(manifest.sha256_file(actual.path), self.original[expected.path])
                    if fmt != "M3U":
                        self.assert_timing(actual, expected)
                self.assert_sources_unchanged()

    def test_timed_set_and_source_survive_portable_backup_with_independent_metadata(self):
        before = project.dump(self.nodes, self.cfg)
        selected = deepcopy(list(self.tracks))
        selected[0].energy = 8.5
        self.nodes.append(Node("playlist", "One hour set", tracks=selected))
        archive = self.root / "set-backup.zip"
        report = backup.create(str(archive), project.dump(self.nodes, self.cfg))
        self.assertEqual(report["audio"], 2)  # Independent metadata does not duplicate physical audio.
        self.assertEqual(backup.verify(str(archive)), hashlib.sha256(archive.read_bytes()).hexdigest())
        restored_path = backup.restore(str(archive), str(self.root / "set-restored"))
        restored = project.load(restored_path)
        self.assertEqual(len(restored), 3)
        self.assertEqual([track.title for track in restored[-1].tracks], [track.title for track in selected])
        self.assertEqual(restored[0].tracks[0].energy, 6.25)
        self.assertEqual(restored[-1].tracks[0].energy, 8.5)
        self.assertIsNot(restored[-1].tracks[0], restored[0].tracks[0])
        self.assertIsNot(restored[-1].tracks[0].cues[0], restored[0].tracks[0].cues[0])
        for actual, expected in zip(restored[-1].tracks, selected, strict=True):
            self.assert_timing(actual, expected)
            self.assertEqual(manifest.sha256_file(actual.path), self.original[expected.path])
        self.assertEqual(project.dump(self.nodes[:-1], self.cfg)["tracks"], before["tracks"])
        self.assert_sources_unchanged()

    def test_cdj_export_creates_real_database_analysis_and_detects_corruption(self):
        out = self.root / "CDJ"
        cache = self.root / "cache"
        cache.mkdir()
        with patch.object(usb, "cache_dir", return_value=cache):
            report = export.run("CDJ/USB", self.nodes, str(out), self.cfg)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.missing, [])
        self.assertEqual(report.tracks, 2)
        self.assertTrue((out / "PIONEER" / "rekordbox" / "export.pdb").stat().st_size > 0)
        analyses = list(out.rglob("ANLZ*.DAT"))
        self.assertEqual(len(analyses), 2)
        self.assertTrue(all(usb.whole(path.read_bytes()) for path in analyses))
        audio = sorted((out / "Contents").rglob("*.wav"))
        self.assertEqual(len(audio), 2)
        self.assertEqual({manifest.sha256_file(str(path)) for path in audio}, set(self.original.values()))
        verified = manifest.verify(str(out))
        self.assertFalse(verified.damaged or verified.missing or verified.error)
        analyses[0].write_bytes(b"corrupted analysis")
        self.assertEqual(len(manifest.verify(str(out)).damaged), 1)
        self.assert_sources_unchanged()


if __name__ == "__main__":
    unittest.main()
