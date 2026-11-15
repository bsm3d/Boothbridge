# values in collection files that are not what they should be: numbers that are not numbers ("inf",
# "nan", "1e999") and an encoding label that is not an encoding.
#
# Each case is the smallest file that failed in a seeded run of the Test center (the seed is in the
# test name), frozen here so that it does not depend on the generator

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from djapp import sources
from djapp.model import CUE, LOOP, unique_tracks

NML = """<?xml version="1.0" encoding="UTF-8"?>
<NML VERSION="19"><COLLECTION ENTRIES="1">
<ENTRY TITLE="t" ARTIST="a">
<LOCATION DIR="/:m/:" FILE="a.mp3" VOLUME="C:"/>
{body}
</ENTRY></COLLECTION>
<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">
<NODE TYPE="PLAYLIST" NAME="p"><PLAYLIST ENTRIES="1" TYPE="LIST" UUID="u">
<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="C:/:m/:a.mp3"/></ENTRY></PLAYLIST></NODE>
</SUBNODES></NODE></PLAYLISTS></NML>
"""

RBXML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="1">
<TRACK TrackID="1" Name="t" Location="file://localhost/C:/m/a.mp3" {attrs}>
{body}
</TRACK></COLLECTION>
<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">
<NODE Name="p" Type="1" KeyType="0" Entries="1"><TRACK Key="1"/></NODE>
</NODE></PLAYLISTS></DJ_PLAYLISTS>
"""

VDJ = """<?xml version="1.0" encoding="UTF-8"?>
<VirtualDJ_Database Version="8"><Song FilePath="C:\\m\\a.mp3" {attrs}>
{body}
</Song></VirtualDJ_Database>
"""


# ------------------------------------------------
# the first number of the model that is not finite, empty when there is none
def nonfinite(nodes) -> str:
    for t in unique_tracks(nodes):
        for name in ("duration", "bpm", "gain", "grid", "size", "bitrate", "samplerate", "rating", "plays",
                     "year", "track_no", "disc_no", "color"):
            v = getattr(t, name)
            if isinstance(v, float) and not math.isfinite(v):
                return f"track.{name} is {v}"
        for c in t.cues:
            if not (math.isfinite(c.start) and math.isfinite(c.length)):
                return f"cue start {c.start}, length {c.length}"
    return ""


# ------------------------------------------------
class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def load(self, name: str, text: str):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        nodes = sources.load(str(path))
        self.assertEqual(nonfinite(nodes), "")
        return unique_tracks(nodes)[0]

# ------------------------------------------------
class NumberTests(unittest.TestCase):
    def test_a_number_from_a_file_is_finite_and_sane_or_the_default(self):
        for text in ("inf", "-inf", "nan", "1e999", "-1e999", "1e16", "abc", "", None):
            with self.subTest(text=text):
                self.assertEqual(sources.num(text), 0.0)
                self.assertEqual(sources.num(text, -1), -1)
        self.assertEqual(sources.num("12.5"), 12.5)
        self.assertEqual(sources.num(" -3 "), -3.0)
        self.assertEqual(sources.num(10 ** 400, 7), 7)

    def test_a_position_is_zero_when_missing_and_none_when_unusable(self):
        self.assertEqual(sources.position(None), 0.0)
        self.assertEqual(sources.position("4.5"), 4.5)
        for text in ("inf", "-inf", "nan", "1e999", "abc", ""):
            with self.subTest(text=text):
                self.assertIsNone(sources.position(text))


# ------------------------------------------------
class TraktorTests(Case):
    def read(self, body: str):
        return self.load("collection.nml", NML.format(body=body))

    def test_seed_680_a_hot_cue_number_of_1e999_is_no_hot_cue(self):
        t = self.read('<CUE_V2 NAME="x" TYPE="0" START="1000" LEN="0" HOTCUE="1e999"/>')
        self.assertEqual([(c.start, c.hotcue) for c in t.cues], [(1000.0, -1)])

    def test_seed_1394_a_playing_time_of_1e999_is_no_duration(self):
        self.assertEqual(self.read('<INFO PLAYTIME_FLOAT="1e999"/>').duration, 0.0)
        self.assertEqual(self.read('<INFO PLAYTIME_FLOAT="1e999" PLAYTIME="200"/>').duration, 200.0)

    def test_other_numbers_in_the_entry_are_dropped(self):
        t = self.read('<ALBUM TRACK="inf"/><INFO BITRATE="-inf" FILESIZE="nan" RANKING="1e999" PLAYCOUNT="inf" '
                      'COLOR="nan" RELEASE_DATE="1e999/1/1"/><TEMPO BPM="inf"/><LOUDNESS ANALYZED_DB="-inf"/>')
        self.assertEqual((t.track_no, t.bitrate, t.size, t.rating, t.plays, t.color, t.year, t.bpm, t.gain),
                         (0,) * 7 + (0.0, 0.0))

    def test_a_cue_without_a_usable_start_is_skipped(self):
        t = self.read('<CUE_V2 NAME="a" TYPE="0" START="inf" LEN="0" HOTCUE="0"/>'
                      '<CUE_V2 NAME="b" TYPE="0" START="nan" LEN="0" HOTCUE="1"/>'
                      '<CUE_V2 NAME="c" TYPE="0" START="2500" LEN="0" HOTCUE="2"/>')
        self.assertEqual([(c.name, c.start) for c in t.cues], [("c", 2500.0)])

    def test_a_bad_cue_length_is_no_length_and_a_loop_without_length_is_a_cue(self):
        t = self.read('<CUE_V2 NAME="a" TYPE="5" START="1000" LEN="inf" HOTCUE="0"/>'
                      '<CUE_V2 NAME="b" TYPE="5" START="2000" LEN="500" HOTCUE="1"/>')
        self.assertEqual([(c.kind, c.length) for c in t.cues], [(CUE, 0.0), (LOOP, 500.0)])


    def test_a_grid_marker_without_a_usable_start_is_not_a_grid(self):
        t = self.read('<CUE_V2 NAME="AutoGrid" TYPE="4" START="inf" LEN="0" HOTCUE="-1"/>'
                      '<CUE_V2 NAME="AutoGrid" TYPE="4" START="125.5" LEN="0" HOTCUE="-1"/>')
        self.assertEqual((t.grid, t.cues), (125.5, []))


# ------------------------------------------------------------------------
class RekordboxTests(Case):
    def read(self, attrs: str = "", body: str = ""):
        return self.load("rekordbox.xml", RBXML.format(attrs=attrs, body=body))

    def test_seed_1297_a_hot_cue_number_of_1e999_and_a_colour_of_minus_1e999(self):
        t = self.read(body='<POSITION_MARK Name="m" Type="3" Start="171.75" Num="1e999" '
                           'Red="255" Green="-1e999" Blue="0"/>')
        self.assertEqual([(c.start, c.hotcue, c.color) for c in t.cues], [(171750.0, -1, "#FF0000")])

    def test_seed_786_a_total_time_of_1e999_is_no_duration(self):
        self.assertEqual(self.read('TotalTime="1e999"').duration, 0.0)

    def test_seed_93_an_end_of_nan_is_no_length_and_a_colour_of_nan_is_zero(self):
        t = self.read(body='<POSITION_MARK Name="m" Type="4" Start="411.28" End="nan" Num="-1" '
                           'Red="1" Green="2" Blue="nan"/>')
        cue = t.cues[0]
        self.assertEqual((cue.start, cue.length, cue.color), (411280.0, 0.0, "#010200"))

    def test_seed_863_a_start_of_inf_drops_the_cue(self):
        t = self.read(body='<POSITION_MARK Name="a" Type="0" Start="inf" Num="-1"/>'
                           '<POSITION_MARK Name="b" Type="0" Start="2" Num="-1"/>')
        self.assertEqual([(c.name, c.start) for c in t.cues], [("b", 2000.0)])

    def test_a_usable_end_still_gives_a_length(self):
        t = self.read(body='<POSITION_MARK Name="l" Type="4" Start="4" End="6.5" Num="-1"/>')
        self.assertEqual((t.cues[0].kind, t.cues[0].length), (LOOP, 2500.0))

    def test_other_numbers_are_dropped_and_the_sample_rate_keeps_its_default(self):
        t = self.read('Size="inf" BitRate="nan" SampleRate="1e999" AverageBpm="inf" Rating="-inf" PlayCount="1e999" '
                      'Year="nan" TrackNumber="inf" DiscNumber="1e999"')
        self.assertEqual((t.size, t.bitrate, t.samplerate, t.bpm, t.rating, t.plays, t.year, t.track_no, t.disc_no),
                         (0, 0, 44100, 0.0, 0, 0, 0, 0, 0))

    def test_a_grid_that_is_not_a_number_is_no_grid(self):
        self.assertIsNone(self.read(body='<TEMPO Inizio="inf" Bpm="120" Metro="4/4" Battito="1"/>').grid)
        self.assertEqual(self.read(body='<TEMPO Inizio="0.25" Bpm="120" Metro="4/4" Battito="1"/>').grid, 250.0)


# ------------------------------------------------
class VirtualDjTests(Case):
    def read(self, attrs: str = "", body: str = ""):
        return self.load("database.xml", VDJ.format(attrs=attrs, body=body))

    def test_seed_689_a_file_size_of_inf_is_no_size(self):
        self.assertEqual(self.read('FileSize="inf"').size, 0)

    def test_other_numbers_are_dropped(self):
        t = self.read(body='<Tags TrackNumber="inf" Year="nan" Stars="1e999"/>'
                           '<Infos SongLength="1e999" Bitrate="-inf" PlayCount="nan"/>')
        self.assertEqual((t.track_no, t.year, t.rating, t.duration, t.bitrate, t.plays), (0, 0, 0, 0.0, 0, 0))

    def test_a_point_without_a_usable_position_is_skipped(self):
        t = self.read(body='<Poi Pos="inf" Name="a" Num="1" Type="cue"/><Poi Pos="nan" Name="b" Num="2" Type="beatgrid"/>'
                           '<Poi Pos="1.5" Name="c" Num="3" Type="cue"/>')
        self.assertEqual(([(c.name, c.start) for c in t.cues], t.grid), ([("c", 1500.0)], None))

    def test_a_phase_that_is_not_a_number_is_no_grid(self):
        self.assertIsNone(self.read(body='<Scan Bpm="0.5" Phase="inf"/>').grid)
        self.assertEqual(self.read(body='<Scan Bpm="0.5" Phase="0.25"/>').grid, 250.0)

    def test_a_seconds_per_beat_that_overflows_is_no_tempo(self):
        t = self.read(body='<Scan Bpm="5e-324"/><Poi Pos="1" Name="l" Num="1" Type="loop" Size="4"/>')
        self.assertEqual(t.bpm, 0.0)
        self.assertEqual([(c.kind, c.length) for c in t.cues], [(CUE, 0.0)])


    def test_a_loop_longer_than_anything_sane_is_a_plain_cue(self):
        t = self.read(body='<Scan Bpm="100000"/><Poi Pos="1" Name="l" Num="1" Type="loop" Size="1e15"/>')
        self.assertEqual([(c.kind, c.length) for c in t.cues], [(CUE, 0.0)])


# ------------------------------------------------
class OtherReaderTests(Case):
    def test_mixxx_real_columns_holding_infinity(self):
        path = self.root / "mixxxdb.sqlite"
        con = sqlite3.connect(path)
        con.executescript("""
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
            INSERT INTO track_locations VALUES (1, 'C:/m/a.mp3', 9e999, 0);
            INSERT INTO library VALUES (1, 'a', 't', '', '2020', '', '', '', 9e999, -9e999, 0, 0, '', 320, 44100,
                0, '1', 1, 0);
            INSERT INTO cues VALUES (1, 1, 9e999, 0, 0, 'c');
            INSERT INTO cues VALUES (1, 4, 1000, 9e999, 1, 'loop');
            INSERT INTO Playlists VALUES (1, 'p', 0, 0);
            INSERT INTO PlaylistTracks VALUES (1, 1, 0);
        """)
        con.commit()
        con.close()
        nodes = sources.load(str(path))
        self.assertEqual(nonfinite(nodes), "")
        t = unique_tracks(nodes)[0]
        self.assertEqual((t.size, t.bpm, t.duration), (0, 0.0, 0.0))
        self.assertEqual([(c.start, c.length) for c in t.cues], [(1000 / 88.2, 0.0)])

    def test_m3u_extinf_with_infinite_durations(self):
        path = self.root / "list.m3u8"
        path.write_text("#EXTM3U\n#EXTINF:inf,A - B\na.mp3\n#EXTINF:-inf,C - D\nb.mp3\n#EXTINF:nan,E - F\nc.mp3\n"
                        "#EXTINF:120,G - H\nd.mp3\n", encoding="utf-8")
        tracks = sources.load(str(path))[0].tracks
        self.assertEqual([t.duration for t in tracks], [0.0, 0.0, 0.0, 120.0])


class EncodingTests(Case):
    def test_seed_21226_an_unknown_encoding_in_a_rekordbox_file_is_a_value_error(self):
        text = RBXML.format(attrs="", body="").replace('encoding="UTF-8"', 'encoding="TTF-8"')
        with self.assertRaisesRegex(ValueError, "rekordbox.xml declares an encoding that is not supported"):
            self.load("rekordbox.xml", text)

    def test_seed_21612_an_unknown_encoding_in_the_virtualdj_database_is_a_value_error(self):
        text = VDJ.format(attrs="", body="").replace('encoding="UTF-8"', 'encoding="UTFm8"')
        with self.assertRaisesRegex(ValueError, "database.xml declares an encoding that is not supported"):
            self.load("database.xml", text)

    def test_seed_21612_a_vdjfolder_with_an_unknown_encoding_is_skipped(self):
        path = self.root / "mix.vdjfolder"
        path.write_text('<?xml version="1.0" encoding="UTFm8"?><VirtualDJ_VFolder><song path="a.mp3"/>'
                        "</VirtualDJ_VFolder>", encoding="utf-8")
        self.assertEqual(sources.load(str(path)), [])

    def test_an_unknown_encoding_in_a_traktor_file_is_read_as_utf8(self):
        text = NML.format(body="").replace('encoding="UTF-8"', 'encoding="TTF-8"')
        self.assertEqual(self.load("collection.nml", text).title, "t")

    def test_a_damaged_vdjfolder_is_skipped_and_a_relative_song_is_beside_its_list(self):
        # one list nested too deep made the whole folder fail, and a relative song path stayed relative to
        # the working directory
        home = self.root / "VirtualDJ"
        (home / "Folders").mkdir(parents=True)
        (home / "database.xml").write_text(VDJ.format(attrs="", body=""), encoding="utf-8")
        (home / "Folders" / "b.vdjfolder").write_text("<VirtualDJ_VFolder>" + "<x>" * 700 + "</x>" * 700
                                                     + "</VirtualDJ_VFolder>", encoding="utf-8")
        (home / "Folders" / "c.vdjfolder").write_text('<VirtualDJ_VFolder><song path="rel.mp3"/><song path="C:\\m\\a.mp3"/>'
                                                     "</VirtualDJ_VFolder>", encoding="utf-8")
        nodes = sources.load(str(home / "database.xml"))
        folder = nodes[0]
        self.assertEqual([n.name for n in folder.children], ["c"])
        paths = [t.path for t in folder.children[0].tracks]
        self.assertEqual(paths, [str(home / "Folders" / "rel.mp3"), "C:\\m\\a.mp3"])
        self.assertIs(folder.children[0].tracks[1], nodes[-1].tracks[0])

    def test_a_utf16_m3u_is_read_as_utf16(self):
        # read as cp1252, every line was a track named with NULs
        path = self.root / "list.m3u"
        path.write_bytes("#EXTM3U\r\n#EXTINF:120,Art - Tit\r\nC:\\m\\a.mp3\r\n".encode("utf-16"))
        tracks = sources.load(str(path))[0].tracks
        self.assertEqual([(t.path, t.artist, t.title, t.duration) for t in tracks], [("C:\\m\\a.mp3", "Art", "Tit", 120.0)])


if __name__ == "__main__":
    unittest.main()
