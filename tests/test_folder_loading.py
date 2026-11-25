# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks folder loading: enumeration of the files and sub folders when a folder is opened.

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from djapp import sources
from djapp.export.files import Cancelled
from djapp.model import Track
from djapp.sources import folder, m3u, vdj


# ------------------------------------------------------------
class FolderLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def touch(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_folder_enumerated_once_with_natural_order_and_hierarchy(self):
        for name in ("track10.MP3", "track2.mp3", "notes.txt", "set10/song.mp3", "set2/song.mp3"):
            self.touch(name)
        (self.root / "empty").mkdir()
        scanned, tags = [], []
        original = os.scandir

        def scan(path):
            scanned.append(os.fspath(path))
            return original(path)

        def track(path):
            tags.append(path)
            return Track(path=path)

        progress = []
        with patch.object(folder.os, "scandir", side_effect=scan), \
             patch.object(folder.os, "walk", side_effect=AssertionError("Second traversal")), \
             patch.object(folder, "make_track", side_effect=track):
            nodes = folder.load(str(self.root), progress=lambda pct, text: progress.append((pct, text)))
        self.assertEqual(len(scanned), 4)
        self.assertEqual(len(set(scanned)), 4)
        self.assertEqual(len(tags), 4)
        children = nodes[0].children
        self.assertEqual([node.name for node in children], [self.root.name, "set2", "set10"])
        self.assertEqual([Path(track.path).name for track in children[0].tracks],
                         ["track2.mp3", "track10.MP3"])
        self.assertEqual(progress[-1], (100, "4 tracks"))


    def test_scan_phase_can_cancel_before_tags_are_read(self):
        self.touch("track.mp3")

        def cancel(pct, text):
            raise Cancelled()

        with patch.object(folder, "make_track") as track, self.assertRaises(Cancelled):
            folder.load(str(self.root), progress=cancel)
        track.assert_not_called()

    def test_progress_is_monotonic_and_counts_exact_audio_total(self):
        for i in range(27):
            self.touch(f"track{i}.mp3")
        progress = []
        with patch.object(folder, "make_track", side_effect=lambda path: Track(path=path)):
            folder.load(str(self.root), progress=lambda pct, text: progress.append((pct, text)))
        self.assertEqual([pct for pct, _ in progress], [0, 92, 100])
        self.assertIn("25/27", progress[1][1])

    def test_dropped_folder_filters_before_sort_and_preserves_duplicates(self):
        track = self.touch("song2.mp3")
        self.touch("song10.mp3")
        self.touch("ignore.txt")
        with patch.object(folder, "make_track", side_effect=lambda path: Track(path=path)):
            tracks = folder.tracks_from([str(self.root), str(track)])
        self.assertEqual([Path(track.path).name for track in tracks],
                         ["song2.mp3", "song10.mp3", "song2.mp3"])
        self.assertIsNot(tracks[0], tracks[2])

    def test_relocator_builds_one_index_and_caches_repeated_paths(self):
        existing = self.touch("existing.mp3")
        alternative = self.touch("music/missing.mp3")
        missing = str(self.root / "gone" / "missing.mp3")
        resolve = sources.relocator(str(self.root / "music"))
        with patch.object(sources, "index_folder", wraps=sources.index_folder) as index, \
             patch.object(sources.os.path, "exists", wraps=os.path.exists) as exists:
            for _ in range(4):
                self.assertEqual(resolve(str(existing)), str(existing))
                index.assert_not_called()
            for _ in range(4):
                self.assertEqual(resolve(missing), str(alternative))
            index.assert_called_once()
            self.assertEqual(exists.call_count, 2)

    def test_m3u_existing_paths_skip_root_scan_keep_order_and_extinf(self):
        audio = self.touch("song.mp3")
        playlist = self.root / "set.m3u8"
        playlist.write_text("#EXTM3U\n#EXTINF:12,Artist - First\nsong.mp3\n"
                            "#EXTINF:23,Second\nsong.mp3\nhttps://example.invalid/stream\n")
        with patch.object(sources, "index_folder") as index:
            nodes = m3u.load(str(playlist), str(self.root))
        index.assert_not_called()
        tracks = nodes[0].tracks
        self.assertEqual([(t.title, t.duration, t.path) for t in tracks],
                         [("First", 12, str(audio)), ("Second", 23, str(audio))])
        self.assertIsNot(tracks[0], tracks[1])

    def test_m3u_repeated_missing_paths_only_scan_root_once(self):
        audio = self.touch("music/song.mp3")
        playlist = self.root / "set.m3u"
        playlist.write_text("gone/song.mp3\ngone/song.mp3\n")
        with patch.object(sources, "index_folder", wraps=sources.index_folder) as index:
            tracks = m3u.load(str(playlist), str(self.root / "music"))[0].tracks
        index.assert_called_once()
        self.assertEqual([track.path for track in tracks], [str(audio), str(audio)])

    def test_vdj_directory_uses_scandir_and_shared_tracks(self):
        self.touch("song.mp3")
        self.touch("Playlists/z.m3u").write_text("../song.mp3\n../song.mp3\n")
        self.touch("Playlists/a.m3u").write_text("../song.mp3\n")
        (self.root / "database.xml").write_text("<VirtualDJ_Database/>")
        with patch.object(vdj.os, "listdir", side_effect=AssertionError("Use directory entries")), \
             patch.object(sources, "index_folder") as index:
            nodes = vdj.load(str(self.root / "database.xml"), str(self.root))
        index.assert_not_called()
        playlists = nodes[0].children
        self.assertEqual([node.name for node in playlists], ["a", "z"])
        self.assertIs(playlists[0].tracks[0], playlists[1].tracks[0])
        self.assertIs(playlists[1].tracks[0], playlists[1].tracks[1])

    def test_legacy_vdj_helpers_still_accept_prebuilt_index(self):
        audio = self.touch("music/song.mp3")
        playlist = self.touch("list.m3u")
        playlist.write_text("gone/song.mp3\n")
        tracks = vdj.read_m3u(str(playlist), {}, {"song.mp3": [str(audio)]})
        self.assertEqual(tracks[0].path, str(audio))


if __name__ == "__main__":
    unittest.main()
