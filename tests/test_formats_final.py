# defects found by the last pass on the file formats, before a release build: odd values in a Mixxx library,
# a folder that links back to itself, a name that Windows cannot hold, half a surrogate pair in a project

import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from djapp import export, settings, sources
from djapp.export.fit import fit
from djapp.model import CUE, LOOP, Cue, Node, Track, playlists, unique_tracks
from djapp.sources import folder, index_folder, project, vdj
from djapp.sources.rbxml import url_to_path

MIXXX_SCHEMA = """
    CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT, filesize INTEGER, fs_deleted INTEGER);
    CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, album TEXT, year TEXT, genre TEXT,
        comment TEXT, composer TEXT, bpm REAL, duration REAL, rating INTEGER, timesplayed INTEGER,
        datetime_added TEXT, bitrate INTEGER, samplerate INTEGER, key_id INTEGER, tracknumber TEXT,
        location INTEGER, mixxx_deleted INTEGER);
    CREATE TABLE cues (track_id INTEGER, type INTEGER, position REAL, length REAL, hotcue INTEGER, label TEXT);
    CREATE TABLE Playlists (id INTEGER PRIMARY KEY, name TEXT, hidden INTEGER, position INTEGER);
    CREATE TABLE PlaylistTracks (playlist_id INTEGER, track_id INTEGER, position INTEGER);
    CREATE TABLE crates (id INTEGER PRIMARY KEY, name TEXT, show INTEGER);
    CREATE TABLE crate_tracks (crate_id INTEGER, track_id INTEGER);
"""


def cfg(**extra) -> dict:
    return {**settings.DEFAULTS, "copy_music": False, "verify_copy": False, **extra}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


# ------------------------------------------------
# SQLite keeps the type it is given, whatever the column says
class MixxxTypeTests(Case):
    def make(self, library: tuple, cue: tuple, playlist_name, crate_name) -> Path:
        path = self.root / "mixxxdb.sqlite"
        con = sqlite3.connect(path)
        con.executescript(MIXXX_SCHEMA)
        con.execute("INSERT INTO track_locations VALUES (1, 'C:/m/a.mp3', 100, 0)")
        con.execute(
            "INSERT INTO library VALUES (1, ?, ?, ?, '2020', ?, ?, ?, 120, 200, 3, 4, ?, 320, 44100, ?, '1', 1, 0)",
            library,
        )
        con.execute("INSERT INTO cues VALUES (1, 1, 44100, 0, ?, ?)", cue)
        con.execute("INSERT INTO Playlists VALUES (1, ?, 0, 0)", (playlist_name,))
        con.execute("INSERT INTO PlaylistTracks VALUES (1, 1, 0)")
        con.execute("INSERT INTO crates VALUES (1, ?, 1)", (crate_name,))
        con.execute("INSERT INTO crate_tracks VALUES (1, 1)")
        con.commit()
        con.close()
        return path

    def test_text_where_a_number_is_expected_is_not_a_type_error(self):
        path = self.make(
            ("a", "t", "al", "g", "c", "co", "2020-01-01", "abc"),
            ("abc", "label"),
            "p",
            "c",
        )
        nodes = sources.load(str(path))
        track = unique_tracks(nodes)[0]
        self.assertIsNone(track.key)
        self.assertEqual([(c.hotcue, c.name) for c in track.cues], [(-1, "label")])

    def test_a_blob_where_text_is_expected_is_empty_text(self):
        blob = b"\x00\xff\x10"
        path = self.make(
            (blob, blob, blob, blob, blob, blob, blob, blob), (blob, blob), blob, blob
        )
        nodes = sources.load(str(path))
        for track in unique_tracks(nodes):
            for name in (
                "title",
                "artist",
                "album",
                "genre",
                "comment",
                "composer",
                "added",
            ):
                self.assertEqual(getattr(track, name), "", name)
            self.assertIsInstance(track.cues[0].name, str)
        for node in playlists(nodes):
            self.assertIsInstance(node.name, str)

    def test_a_null_playlist_name_is_a_text(self):
        path = self.make(
            ("a", "t", "al", "g", "c", "co", "2020-01-01", 5), (3, "x"), None, None
        )
        nodes = sources.load(str(path))
        self.assertTrue(all(isinstance(n.name, str) for n in playlists(nodes)))

    def test_a_good_library_is_read_as_before(self):
        path = self.make(
            ("a", "t", "al", "g", "c", "co", "2020-01-01 10:00:00", 5),
            (3, "x"),
            "p",
            "c",
        )
        track = unique_tracks(sources.load(str(path)))[0]
        self.assertEqual(
            (track.title, track.artist, track.key, track.added),
            ("t", "a", 4, "2020-01-01"),
        )
        self.assertEqual([(c.hotcue, c.name) for c in track.cues], [(3, "x")])


# ------------------------------------------------
# a Windows junction is not a symlink for Python: os.walk and DirEntry.is_symlink go into it, and two of them
# pointing back up make a tree with more paths than a run can ever list
class JunctionTests(Case):
    def link(self, where: Path, target: Path):
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(where), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if made.returncode:
            self.skipTest("no junction here: " + made.stdout + made.stderr)
        self.addCleanup(
            lambda: subprocess.run(
                ["cmd", "/c", "rmdir", str(where)], capture_output=True, check=False
            )
        )

    def within(self, seconds: float, work):
        out = {}

        def run():
            out["value"] = work()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), "still walking after the time allowed")
        return out["value"]

    def library(self) -> Path:
        music = self.root / "music"
        music.mkdir()
        (music / "a.mp3").write_bytes(b"\xff\xfb" * 100)
        self.link(music / "j1", music)
        self.link(music / "j2", music)
        return music

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows thing")
    def test_a_music_folder_with_junctions_back_up_is_read_once(self):
        music = self.library()
        nodes = self.within(30, lambda: folder.load(str(music)))
        self.assertEqual([t.path for t in unique_tracks(nodes)], [str(music / "a.mp3")])

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows thing")
    def test_the_music_index_and_the_count_do_not_enter_junctions(self):
        music = self.library()
        found = self.within(30, lambda: index_folder(str(music)))
        self.assertEqual(found, {"a.mp3": [str(music / "a.mp3")]})
        self.assertEqual(self.within(30, lambda: folder.count_audio(str(music))), 1)
        self.assertEqual(
            len(self.within(30, lambda: folder.tracks_from([str(music)]))), 1
        )

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows thing")
    def test_a_virtualdj_folder_with_junctions_is_read(self):
        home = self.root / "vdj"
        (home / "Folders" / "sub").mkdir(parents=True)
        (home / "database.xml").write_text(
            '<?xml version="1.0"?><VirtualDJ_Database Version="8"/>', encoding="utf-8"
        )
        (home / "Folders" / "sub" / "p.vdjfolder").write_text(
            '<?xml version="1.0"?><VirtualDJ_VFolder><song path="C:/a.mp3"/></VirtualDJ_VFolder>',
            encoding="utf-8",
        )
        self.link(home / "Folders" / "sub" / "back", home / "Folders")
        nodes = self.within(30, lambda: sources.load(str(home / "database.xml")))
        self.assertEqual([n.name for n in playlists(nodes)], ["p"])

    def test_a_folder_that_cannot_be_listed_is_nothing(self):
        self.assertEqual(vdj.read_dir(str(self.root / "missing"), {}, lambda p: p), [])


# ------------------------------------------------
# a name of a folder, a playlist, an artist or an album is not a limit of the file system
class LongNameTests(Case):
    def library(self, name: str) -> list[Node]:
        audio = self.root / "a.mp3"
        audio.write_bytes(b"ID3" + bytes(300))
        track = Track(
            title="T", artist=name, album=name, path=str(audio), duration=60.0
        )
        return [Node("folder", name, children=[Node("playlist", name, tracks=[track])])]

    def test_m3u_with_names_of_300_characters(self):
        nodes = self.library("L" * 300)
        out = self.root / "m3u"
        report = export.run("M3U", nodes, str(out), cfg(copy_music=True))
        self.assertEqual(report.errors, [])
        lists = list((out / "Playlists").rglob("*.m3u8"))
        self.assertEqual(len(lists), 1)
        track = sources.load(str(lists[0]))[0].tracks[0]
        self.assertTrue(os.path.isfile(track.path), track.path)

    def test_the_copy_of_a_track_with_an_artist_of_300_characters(self):
        nodes = self.library("A" * 300)
        for fmt, name in (
            ("Rekordbox XML", "rekordbox.xml"),
            ("Traktor NML", "collection.nml"),
        ):
            with self.subTest(fmt=fmt):
                out = self.root / name
                report = export.run(fmt, nodes, str(out), cfg(copy_music=True))
                self.assertEqual(report.errors, [])
                track = unique_tracks(sources.load(str(out / name)))[0]
                self.assertTrue(os.path.isfile(track.path), track.path)

    def test_short_names_are_not_cut(self):
        nodes = self.library("Artist")
        out = self.root / "m3u"
        export.run("M3U", nodes, str(out), cfg(copy_music=True))
        self.assertTrue((out / "Playlists" / "Artist" / "Artist.m3u8").is_file())
        self.assertTrue((out / "Contents" / "Artist" / "Artist" / "a.mp3").is_file())


# ------------------------------------------------
# a file name can hold half a surrogate pair on Windows, and the project is the one file that has to say so
class ProjectSurrogateTests(Case):
    def test_a_title_and_a_path_with_half_a_pair_are_saved_and_read_back(self):
        track = Track(title="lone\ud800sur", path="C:/m/lone\ud800sur.mp3")
        nodes = [Node("playlist", "p\udfff", tracks=[track])]
        path = self.root / "p.json"
        project.save(str(path), nodes, {})
        back = project.load(str(path))
        self.assertEqual(back[0].name, "p\udfff")
        self.assertEqual(
            (back[0].tracks[0].title, back[0].tracks[0].path), (track.title, track.path)
        )

    def test_a_project_without_one_keeps_its_letters_readable(self):
        path = self.root / "p.json"
        project.save(
            str(path),
            [Node("playlist", "Caf\u00e9", tracks=[Track(title="\u65e5\u672c")])],
            {},
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("Caf\u00e9", text)
        self.assertIn("\u65e5\u672c", text)


# ------------------------------------------------
# one track with an address that is not one does not make the whole collection a bad file
class LocationTests(Case):
    def test_an_address_that_does_not_parse_is_taken_as_it_is(self):
        self.assertEqual(url_to_path("file://[::1/x"), "file://[::1/x")

    def test_the_other_tracks_of_the_collection_are_read(self):
        text = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">
<TRACK TrackID="1" Name="bad" Location="file://[::1/x"/>
<TRACK TrackID="2" Name="good" Location="file://localhost/C:/m/a.mp3"/>
</COLLECTION><PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">
<NODE Name="p" Type="1" KeyType="0" Entries="2"><TRACK Key="1"/><TRACK Key="2"/></NODE>
</NODE></PLAYLISTS></DJ_PLAYLISTS>"""
        path = self.root / "rekordbox.xml"
        path.write_text(text, encoding="utf-8")
        tracks = sources.load(str(path))[0].tracks
        self.assertEqual([t.title for t in tracks], ["bad", "good"])
        self.assertEqual(tracks[1].path, os.path.normpath("C:/m/a.mp3"))


# ------------------------------------------------
# the analysis files count milliseconds in 32 bits: one cue beyond them lost the analysis of the whole track
class CueRangeTests(Case):
    def test_fit_drops_what_the_analysis_cannot_hold(self):
        far = [
            Cue("a", CUE, 4.3e9, 0.0, 0),
            Cue("b", CUE, 1e15, 0.0, -1),
            Cue("c", LOOP, 10.0, 1e12, 1),
            Cue("d", LOOP, 4e9, 4e9, 2),
            Cue("e", CUE, -1e12, 0.0, 3),
        ]
        near = [
            Cue("ok", CUE, 1500.0, 0.0, 0),
            Cue("past the end", CUE, 9e6, 0.0, -1),
            Cue("loop", LOOP, 1000.0, 2000.0, 1),
            Cue("before", CUE, -50.0, 0.0, 2),
        ]
        self.assertEqual(
            [c.name for c in fit(Track(cues=far + near)).cues], [c.name for c in near]
        )

    def test_a_track_in_range_comes_back_as_it_is(self):
        track = Track(title="t", cues=[Cue("ok", CUE, 1500.0, 0.0, 0)])
        self.assertIs(fit(track), track)

    def test_a_cue_far_past_the_end_does_not_cost_the_track_its_analysis(self):
        from tests.test_key_support import export_to, make_track

        music, cache = self.root / "music", self.root / "cache"
        music.mkdir()
        cache.mkdir()
        track = make_track(music, "t", 1, bpm=120.0, grid=0.0)
        track.cues = [Cue("far", CUE, 1e12, 0.0, 0), Cue("near", CUE, 1000.0, 0.0, 1)]
        report = export_to(
            [Node("playlist", "p", tracks=[track])], self.root / "key", cache
        )
        self.assertEqual(report.errors, [])
        got = sources.load(str(self.root / "key"))
        self.assertEqual(
            [(c.hotcue, c.name) for c in unique_tracks(got)[0].cues], [(1, "near")]
        )
        self.assertEqual(unique_tracks(got)[0].bpm, 120.0)


# ------------------------------------------------
# U+FFFE and U+FFFF are not characters of XML: a tag read in the wrong byte order can hold them, and the
# collection written from it could not be read back by this program, or by the one it was written for
class NoncharacterTests(Case):
    def test_a_title_with_noncharacters_is_written_and_read_back_without_them(self):
        bad = "a" + chr(0xFFFE) + chr(0xFFFF) + "b"
        track = Track(
            title=bad,
            artist=bad,
            album=bad,
            comment=bad,
            path="C:/m/a.mp3",
            duration=10.0,
        )
        track.cues = [Cue(bad, CUE, 100.0, 0.0, 0)]
        nodes = [Node("playlist", bad, tracks=[track])]
        for fmt, name in (
            ("Rekordbox XML", "rekordbox.xml"),
            ("Traktor NML", "collection.nml"),
        ):
            with self.subTest(fmt=fmt):
                out = self.root / name
                export.run(fmt, nodes, str(out), cfg())
                back = sources.load(str(out / name))
                got = unique_tracks(back)[0]
                self.assertEqual(
                    (got.title, got.artist, got.comment, got.cues[0].name), ("ab",) * 4
                )
                self.assertEqual(back[0].name, "ab")


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------
class AcceptedFormatsTests(unittest.TestCase):
    ALLOWED = (".mp3", ".aiff", ".aif", ".flac", ".ogg", ".aac", ".wav")

    def test_only_the_six_formats_are_audio_files(self):
        from djapp import sources
        self.assertEqual(sources.AUDIO_EXT, set(self.ALLOWED))
        for ext in self.ALLOWED:
            self.assertTrue(sources.is_audio("song" + ext.upper()))
        for name in ("clip.mp4", "film.mov", "film.mkv", "film.avi", "clip.webm", "clip.m4v", "song.m4a", "song.alac", "a.txt"):
            with self.subTest(name=name):
                self.assertFalse(sources.is_audio(name))

    def test_a_dropped_file_of_another_format_is_not_added(self):
        from djapp.sources.folder import tracks_from
        with tempfile.TemporaryDirectory() as tmp:
            names = ("a.mp4", "b.mp3", "c.m4a", "d.flac", "e.mov")
            for n in names:
                (Path(tmp) / n).write_bytes(bytes(64))
            files = [str(Path(tmp) / n) for n in names]
            self.assertEqual([Path(t.path).name for t in tracks_from(files)], ["b.mp3", "d.flac"])
            self.assertEqual([Path(t.path).name for t in tracks_from([tmp])], ["b.mp3", "d.flac"])

    def test_an_export_still_counts_a_collection_m4a_as_audio(self):
        from djapp.export.manifest import file_category
        self.assertEqual(file_category("Contents/Artist/Album/song.m4a"), "Audio")
        self.assertEqual(file_category("Contents/Artist/Album/song.flac"), "Audio")
