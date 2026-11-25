# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the memory use of the NML and XML readers on large generated collections, measured with tracemalloc.

import os
import tempfile
import tracemalloc
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import defusedxml.ElementTree as ET
from djapp import sources
from djapp.model import Node
from djapp.sources import itunes, m3u, nml, rbxml, vdj

NML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'


def dump(nodes):
    return [
        (
            n.kind,
            n.name,
            n.uuid,
            n.query,
            [asdict(t) for t in n.tracks],
            dump(n.children),
        )
        for n in nodes
    ]


def nml_entry(i):
    return (
        f'<ENTRY TITLE="T{i}" ARTIST="A{i % 3}" AUDIO_ID="id{i}">'
        f'<LOCATION DIR="/:Music/:" FILE="t{i}.mp3" VOLUME="C:"/>'
        '<INFO BITRATE="320000" PLAYTIME="200" RANKING="102" COLOR="2"/>'
        f'<TEMPO BPM="{120 + i}"/><MUSICAL_KEY VALUE="{i % 24}"/>'
        f'<CUE_V2 NAME="AutoGrid" TYPE="4" START="25.5" LEN="0" HOTCUE="0"/>'
        f'<CUE_V2 NAME="c{i}" TYPE="0" START="{1000 + i}" LEN="0" HOTCUE="1"/>'
        f'<CUE_V2 NAME="l{i}" TYPE="5" START="2000" LEN="500" HOTCUE="2"/></ENTRY>'
    )


def nml_ref(i):
    return f'<ENTRY><PRIMARYKEY KEY="C:/:Music/:t{i}.mp3"/></ENTRY>'


def nml_text(tracks=12, playlists_first=False, extra=""):
    collection = (
        f'<COLLECTION ENTRIES="{tracks}">'
        + "".join(nml_entry(i) for i in range(tracks))
        + extra
        + "</COLLECTION>"
    )
    lists = (
        '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="4">'
        '<NODE TYPE="PLAYLIST" NAME="All"><PLAYLIST UUID="u1">'
        + "".join(nml_ref(i) for i in range(tracks))
        + nml_ref(999)
        + "</PLAYLIST></NODE>"
        '<NODE TYPE="FOLDER" NAME="Sets"><SUBNODES COUNT="2">'
        '<NODE TYPE="PLAYLIST" NAME="Odd"><PLAYLIST UUID="u2">'
        + "".join(nml_ref(i) for i in range(1, tracks, 2))
        + "</PLAYLIST></NODE>"
        '<NODE TYPE="PLAYLIST" NAME="Gone"><PLAYLIST UUID="u3">'
        + nml_ref(998)
        + "</PLAYLIST></NODE>"
        "</SUBNODES></NODE>"
        '<NODE TYPE="SMARTLIST" NAME="Fast"><SMARTLIST UUID="u4"><SEARCH_EXPRESSION QUERY="$BPM &gt; 125"/></SMARTLIST></NODE>'
        '<NODE TYPE="PLAYLIST" NAME="Empty"><PLAYLIST UUID="u5"/></NODE>'
        "</SUBNODES></NODE></PLAYLISTS>"
    )
    body = lists + collection if playlists_first else collection + lists
    return f'{NML_HEAD}<NML VERSION="19"><HEAD COMPANY="x"/>{body}</NML>'


def old_nodes(parent, by_key):
    out = []
    sub = parent.find("SUBNODES")
    for n in (sub if sub is not None else parent).findall("NODE"):
        kind, name = n.get("TYPE", ""), n.get("NAME", "")
        if kind == "FOLDER":
            kids = old_nodes(n, by_key)
            if kids:
                out.append(Node("folder", name, children=kids))
        elif kind == "PLAYLIST":
            pl = n.find("PLAYLIST")
            if pl is None:
                continue
            node = Node("playlist", name, uuid=pl.get("UUID", ""))
            for ent in pl.findall("ENTRY"):
                pk = ent.find("PRIMARYKEY")
                t = by_key.get(pk.get("KEY", "")) if pk is not None else None
                if t is not None:
                    node.tracks.append(t)
            if node.tracks:
                out.append(node)
        elif kind == "SMARTLIST":
            sl = n.find("SMARTLIST")
            if sl is None:
                continue
            q = sl.find("SEARCH_EXPRESSION")
            out.append(
                Node(
                    "smartlist",
                    name,
                    uuid=sl.get("UUID", ""),
                    query=q.get("QUERY", "") if q is not None else "",
                )
            )
    return out


# the reader as it was before the streaming one: the whole tree in memory
def old_nml(path):
    root = nml.read_tree(path)
    by_key = {}
    col = root.find("COLLECTION")
    for e in col.findall("ENTRY") if col is not None else []:
        loc = e.find("LOCATION")
        if loc is not None:
            by_key[nml.location_key(loc)] = nml.read_entry(e)
    top = root.find("PLAYLISTS")
    rootnode = top.find("NODE")
    return old_nodes(rootnode if rootnode is not None else top, by_key)


def rb_text(tracks=12, playlists_first=False):
    items = "".join(
        f'<TRACK TrackID="{i}" Name="T{i}" Artist="A{i % 3}" TotalTime="200" AverageBpm="{120 + i}" Tonality="Am" '
        f'Location="file://localhost/C:/Music/t{i}.mp3"><TEMPO Inizio="0.025" Bpm="128"/>'
        f'<POSITION_MARK Name="c" Type="0" Start="1.5" Num="0"/><POSITION_MARK Name="l" Type="4" Start="2" End="3" Num="1"/></TRACK>'
        for i in range(tracks)
    )
    collection = f'<COLLECTION Entries="{tracks}">{items}</COLLECTION>'
    by_id = (
        "".join(f'<TRACK Key="{i}"/>' for i in range(tracks)) + '<TRACK Key="nope"/>'
    )
    by_loc = "".join(
        f'<TRACK Key="file://localhost/C:/Music/t{i}.mp3"/>'
        for i in range(1, tracks, 2)
    )
    lists = (
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="3">'
        f'<NODE Name="ById" Type="1" KeyType="0">{by_id}</NODE>'
        '<NODE Type="0" Name="Sets" Count="1">'
        f'<NODE Name="ByLoc" Type="1" KeyType="1">{by_loc}</NODE></NODE>'
        '<NODE Name="Empty" Type="1" KeyType="0"/>'
        "</NODE></PLAYLISTS>"
    )
    body = lists + collection if playlists_first else collection + lists
    return f'<?xml version="1.0" encoding="UTF-8"?><DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox"/>{body}</DJ_PLAYLISTS>'


def old_rb(path):
    root = ET.parse(path).getroot()
    by_id, by_loc = {}, {}
    for el in root.find("COLLECTION").findall("TRACK"):
        t = rbxml.read_track(el)
        by_id[el.get("TrackID", "")] = t
        by_loc[el.get("Location", "")] = t

    def nodes(parent):
        out = []
        for n in parent.findall("NODE"):
            if n.get("Type") == "0":
                kids = nodes(n)
                if kids:
                    out.append(Node("folder", n.get("Name", ""), children=kids))
                continue
            ref = by_loc if n.get("KeyType") == "1" else by_id
            node = Node("playlist", n.get("Name", ""))
            node.tracks = [
                ref[tr.get("Key", "")]
                for tr in n.findall("TRACK")
                if tr.get("Key", "") in ref
            ]
            if node.tracks:
                out.append(node)
        return out

    return nodes(root.find("PLAYLISTS/NODE"))


def vdj_text(tracks=12):
    songs = "".join(
        f'<Song FilePath="C:\\Music\\t{i}.mp3" FileSize="900"><Tags Author="A{i % 3}" Title="T{i}" Year="2020" Stars="3"/>'
        f'<Infos SongLength="200" FirstSeen="1700000000" Bitrate="320"/><Comment> hi </Comment>'
        f'<Scan Bpm="0.5" Key="Am" Phase="0.02"/><Poi Pos="1.5" Type="cue" Num="1" Name="c"/>'
        f'<Poi Pos="3" Type="loop" Num="2" Size="4"/><Poi Pos="0.5" Type="beatgrid"/></Song>'
        for i in range(tracks)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><VirtualDJ_Database Version="2026"><FavoriteFolder Path="x"/>{songs}</VirtualDJ_Database>'


def old_vdj(path):
    return [vdj.read_song(el) for el in ET.parse(path).getroot().iter("Song")]


def tune(i, name):
    return (
        f"<key>{i}</key><dict><key>Track ID</key><integer>{i}</integer><key>Name</key><string>{name}</string>"
        "<key>Kind</key><string>MPEG audio file</string><key>Total Time</key><integer>180000</integer>"
        f"<key>Location</key><string>file://localhost/C:/Music/{name}.mp3</string></dict>"
    )


def plist(i, name, ids, parent="", folder=False):
    body = f"<key>Name</key><string>{name}</string><key>Playlist Persistent ID</key><string>p{i}</string>"
    if parent:
        body += f"<key>Parent Persistent ID</key><string>{parent}</string>"
    if folder:
        body += "<key>Folder</key><true/>"
    else:
        body += (
            "<key>Playlist Items</key><array>"
            + "".join(
                f"<dict><key>Track ID</key><integer>{n}</integer></dict>" for n in ids
            )
            + "</array>"
        )
    return f"<dict>{body}</dict>"


def itunes_text(tracks=10, playlists_first=False):
    library = (
        "<key>Tracks</key><dict>"
        + "".join(tune(i, f"s{i}") for i in range(tracks))
        + "</dict>"
    )
    lists = (
        "<key>Playlists</key><array>"
        + plist(1, "Set", [3, 1, 2, 77], folder=True)
        + plist(2, "Inside", [4, 5, 5], parent="p1")
        + plist(3, "Top", range(tracks))
        + plist(4, "Gone", [900])
        + "</array>"
    )
    body = lists + library if playlists_first else library + lists
    return (
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Major Version</key><integer>1</integer>'
        "<key>Application Version</key><string>12.13</string>"
        + body
        + "</dict></plist>"
    )


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return str(path)


# --------------------------------------------------------------
class SameModelTests(Files):
    def test_nml_streaming_gives_what_the_whole_tree_gave(self):
        path = self.write("collection.nml", nml_text())
        nodes = nml.load(path)
        self.assertEqual(dump(nodes), dump(old_nml(path)))
        self.assertEqual([n.name for n in nodes], ["All", "Sets", "Fast"])
        self.assertEqual(len(nodes[0].tracks), 12)
        self.assertEqual(nodes[0].tracks[3].title, "T3")
        self.assertEqual(len(nodes[0].tracks[3].cues), 2)

    def test_nml_playlists_before_the_collection_give_the_same_model(self):
        first = self.write("first.nml", nml_text(playlists_first=True))
        last = self.write("last.nml", nml_text())
        self.assertEqual(dump(nml.load(first)), dump(nml.load(last)))
        self.assertEqual(dump(nml.load(first)), dump(old_nml(first)))

    def test_nml_with_a_music_folder_relocates_like_before(self):
        music = self.root / "Music"
        music.mkdir()
        (music / "t3.mp3").write_bytes(b"x")
        path = self.write("collection.nml", nml_text())
        tracks = {t.title: t for n in nml.load(path, str(music)) for t in n.tracks}
        self.assertEqual(os.path.basename(tracks["T3"].path), "t3.mp3")
        self.assertEqual(
            os.path.dirname(tracks["T3"].path), os.path.normpath(str(music))
        )
        self.assertNotEqual(
            os.path.dirname(tracks["T4"].path), os.path.normpath(str(music))
        )

    def test_rekordbox_streaming_gives_what_the_whole_tree_gave(self):
        path = self.write("rekordbox.xml", rb_text())
        nodes = rbxml.load(path)
        self.assertEqual(dump(nodes), dump(old_rb(path)))
        self.assertEqual([n.name for n in nodes], ["ById", "Sets"])
        self.assertEqual(len(nodes[0].tracks), 12)
        self.assertEqual(len(nodes[1].children[0].tracks), 6)

    def test_rekordbox_playlists_before_the_collection_give_the_same_model(self):
        first = self.write("first.xml", rb_text(playlists_first=True))
        last = self.write("last.xml", rb_text())
        self.assertEqual(dump(rbxml.load(first)), dump(rbxml.load(last)))

    def test_virtualdj_streaming_gives_what_the_whole_tree_gave(self):
        path = self.write("database.xml", vdj_text())
        nodes = vdj.load(path)
        self.assertEqual([n.name for n in nodes], ["All tracks"])
        reference = old_vdj(path)
        self.assertEqual(len(reference), 12)
        for got, want in zip(nodes[0].tracks, reference, strict=True):
            self.assertEqual(got.title, want.title)
            self.assertEqual(got.bpm, want.bpm)
            self.assertEqual(got.grid, want.grid)
            self.assertEqual(
                [asdict(c) for c in got.cues], [asdict(c) for c in want.cues]
            )

    def test_virtualdj_playlist_file_streaming(self):
        folder = self.root / "Folders"
        folder.mkdir()
        (folder / "mix.vdjfolder").write_text(
            '<VirtualFolder><song path="C:\\a.mp3" title="A" artist="X" songlength="10" bpm="0.5"/>'
            '<song path="C:\\b.mp3" title="B"/><song path="C:\\a.mp3" title="again"/></VirtualFolder>'
        )
        (folder / "broken.vdjfolder").write_text(
            '<VirtualFolder><song path="C:\\a.mp3"/>'
        )
        self.write("database.xml", vdj_text(2))
        nodes = vdj.load(str(self.root / "database.xml"))
        folders = next(n for n in nodes if n.name == "Folders")
        names = [n.name for n in folders.children]
        self.assertEqual(names, ["mix"])
        self.assertEqual([t.title for t in folders.children[0].tracks], ["A", "B", "A"])

    def test_itunes_streaming_gives_the_expected_model(self):
        path = self.write("Library.xml", itunes_text())
        nodes = itunes.load(path)
        self.assertEqual([n.name for n in nodes], ["Set", "Top", "All tracks"])
        self.assertEqual([n.name for n in nodes[0].children], ["Inside"])
        self.assertEqual(
            [t.title for t in nodes[0].children[0].tracks], ["s4", "s5", "s5"]
        )
        self.assertEqual(len(nodes[1].tracks), 10)
        self.assertEqual(len(nodes[2].tracks), 10)
        self.assertEqual(nodes[1].tracks[2].duration, 180.0)

    def test_itunes_playlists_before_the_tracks_give_the_same_model(self):
        first = self.write("first.xml", itunes_text(playlists_first=True))
        last = self.write("last.xml", itunes_text())
        self.assertEqual(dump(itunes.load(first)), dump(itunes.load(last)))

    def test_itunes_tracks_twice_is_a_bad_file_once_playlists_used_the_first(self):
        text = itunes_text().replace(
            "</dict></plist>", "<key>Tracks</key><dict/></dict></plist>"
        )
        with self.assertRaisesRegex(ValueError, "not an iTunes library"):
            itunes.load(self.write("twice.xml", text))


# --------------------------------------------------------------
class CeilingTests(Files):
    def test_the_file_ceiling_is_one_a_real_library_fits_in_and_a_hostile_one_does_not_outgrow(
        self,
    ):
        self.assertLessEqual(sources.MAX_FILE, 256 << 20)
        self.assertGreaterEqual(sources.MAX_FILE, 100 << 20)

    def test_a_file_over_the_ceiling_is_refused_before_it_is_read(self):
        paths = {
            nml.load: self.write("collection.nml", nml_text()),
            rbxml.load: self.write("rekordbox.xml", rb_text()),
            itunes.load: self.write("Library.xml", itunes_text()),
        }
        vdjdir = self.root / "vdj"
        vdjdir.mkdir()
        paths[vdj.load] = str(vdjdir / "database.xml")
        Path(paths[vdj.load]).write_text(vdj_text())
        with patch.object(sources, "MAX_FILE", 1000):
            for load, path in paths.items():
                with (
                    self.subTest(load=load.__module__),
                    self.assertRaisesRegex(ValueError, "too large.*limit"),
                ):
                    load(path)

    def test_too_many_tracks_is_refused_with_a_reason(self):
        cases = [
            (nml, "MAX_TRACKS", nml.load, self.write("collection.nml", nml_text(10))),
            (rbxml, "MAX_TRACKS", rbxml.load, self.write("rekordbox.xml", rb_text(10))),
            (
                itunes,
                "MAX_TRACKS",
                itunes.load,
                self.write("Library.xml", itunes_text(10)),
            ),
            (vdj, "MAX_TRACKS", vdj.load, self.write("database.xml", vdj_text(10))),
        ]
        for module, name, load, path in cases:
            with (
                self.subTest(reader=module.__name__),
                patch.object(module, name, 5),
                self.assertRaisesRegex(ValueError, "too many tracks"),
            ):
                load(path)

    def test_an_m3u_with_too_many_tracks_is_refused(self):
        # 64 MB of one letter lines was 30 million tracks and some 15 GB
        path = self.write("list.m3u", "a\n" * 10)
        with (
            patch.object(m3u, "MAX_TRACKS", 5),
            self.assertRaisesRegex(ValueError, "too many tracks"),
        ):
            m3u.load(path)
        self.assertEqual(len(m3u.load(path)[0].tracks), 10)

    def test_too_many_cue_points_playlists_and_entries_are_refused(self):
        path = self.write("collection.nml", nml_text(10))
        with (
            patch.object(nml, "MAX_CUES", 5),
            self.assertRaisesRegex(ValueError, "too many cue points"),
        ):
            nml.load(path)
        with (
            patch.object(nml, "MAX_NODES", 2),
            self.assertRaisesRegex(ValueError, "too many playlists"),
        ):
            nml.load(path)
        with (
            patch.object(nml, "MAX_REFS", 5),
            self.assertRaisesRegex(ValueError, "too many playlist entries"),
        ):
            nml.load(path)
        lib = self.write("Library.xml", itunes_text(10))
        with (
            patch.object(itunes, "MAX_REFS", 5),
            self.assertRaisesRegex(ValueError, "too many playlist entries"),
        ):
            itunes.load(lib)

    def test_too_many_elements_held_at_once_is_refused(self):
        wide = nml_text(2, extra="<ENTRY>" + "<X/>" * 300 + "</ENTRY>")
        path = self.write("collection.nml", wide)
        with (
            patch.object(sources, "MAX_LIVE", 100),
            self.assertRaisesRegex(ValueError, "too many elements"),
        ):
            nml.load(path)
        self.assertTrue(nml.load(path))

    def test_elements_given_back_do_not_count_against_the_ceiling(self):
        path = self.write("collection.nml", nml_text(200))
        with patch.object(sources, "MAX_LIVE", 400):
            self.assertEqual(len(nml.load(path)[0].tracks), 200)

    def test_nesting_beyond_reason_is_refused_in_every_format(self):
        deep = "<NODE>" * 700 + "</NODE>" * 700
        with self.assertRaisesRegex(ValueError, "too deeply"):
            nml.load(self.write("a.nml", f"<NML><PLAYLISTS>{deep}</PLAYLISTS></NML>"))
        with self.assertRaisesRegex(ValueError, "too deeply"):
            rbxml.load(
                self.write(
                    "a.xml",
                    f"<DJ_PLAYLISTS><PLAYLISTS>{deep}</PLAYLISTS></DJ_PLAYLISTS>",
                )
            )
        with self.assertRaisesRegex(ValueError, "too deeply"):
            sources.load(
                self.write(
                    "b.xml", f"<DJ_PLAYLISTS>{'<a>' * 700}{'</a>' * 700}</DJ_PLAYLISTS>"
                )
            )


# --------------------------------------------------------------
class StreamingTests(Files):
    def test_a_long_playlist_does_not_make_memory_grow_with_the_file(self):
        refs = "".join(nml_ref(0) for _ in range(60_000))
        text = (
            f'{NML_HEAD}<NML VERSION="19"><COLLECTION ENTRIES="1">{nml_entry(0)}</COLLECTION><PLAYLISTS>'
            f'<NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1"><NODE TYPE="PLAYLIST" NAME="Long"><PLAYLIST UUID="u">{refs}'
            "</PLAYLIST></NODE></SUBNODES></NODE></PLAYLISTS></NML>"
        )
        path = self.write("collection.nml", text)
        size = os.path.getsize(path)
        tracemalloc.start()
        try:
            nodes = nml.load(path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(len(nodes[0].tracks), 60_000)
        self.assertLess(peak, size / 2)

    def test_the_progress_callback_still_cancels_a_load(self):
        class Stop(Exception):
            pass

        def progress(pct, msg):
            if pct > 10:
                raise Stop

        for load, path in (
            (nml.load, self.write("collection.nml", nml_text(8000))),
            (rbxml.load, self.write("rekordbox.xml", rb_text(8000))),
            (itunes.load, self.write("Library.xml", itunes_text(8000))),
        ):
            with self.subTest(load=load.__module__), self.assertRaises(Stop):
                load(path, "", progress)

    def test_progress_goes_up_and_ends_at_one_hundred(self):
        seen = []
        nml.load(
            self.write("collection.nml", nml_text(8000)),
            "",
            lambda pct, msg: seen.append(pct),
        )
        self.assertEqual(seen[-1], 100)
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(len(seen), 2)

    def test_entities_and_external_data_are_still_refused_while_streaming(self):
        from defusedxml import DefusedXmlException

        bomb = '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "x">]><NML><COLLECTION><ENTRY TITLE="&a;"/></COLLECTION></NML>'
        with self.assertRaises(DefusedXmlException):
            nml.load(self.write("a.nml", bomb))
        with self.assertRaisesRegex(ValueError, "entities"):
            sources.load(self.write("b.nml", bomb))

    def test_a_character_the_parser_refuses_is_dropped_wherever_it_falls_in_the_file(
        self,
    ):
        base = nml_text(1)
        start = base.index('TITLE="T0"') + len('TITLE="')
        for pad in range((1 << 16) - start - 45, (1 << 16) - start + 5):
            title = "a" * pad
            text = base.replace('TITLE="T0"', f'TITLE="{title}&#0;b&#x41;&#1;"', 1)
            nodes = nml.load(self.write("collection.nml", text))
            self.assertEqual(nodes[0].tracks[0].title, title + "bA", pad)

    def test_the_cleaned_text_matches_the_whole_file_cleaned(self):
        raw = (nml_text(30) + "&#0;&#xD800;&#65;\x01\x02&#12").encode("utf-8")
        path = Path(self.write("raw.nml", raw))

        class Small:
            def __init__(self, f):
                self.f = f

            def read(self, n):
                return self.f.read(37)

        with open(path, "rb") as f:
            cleaned = nml.Cleaned(Small(f))
            parts = []
            while chunk := cleaned.read():
                parts.append(chunk)
        self.assertEqual("".join(parts), nml.clean_text(raw.decode("utf-8")))

    def test_an_unknown_encoding_is_read_as_utf8_like_before(self):
        text = nml_text(2).replace('encoding="UTF-8"', 'encoding="no-such-encoding"')
        nodes = nml.load(self.write("collection.nml", text))
        self.assertEqual(len(nodes[0].tracks), 2)
        with self.assertRaisesRegex(ValueError, "not supported"):
            rbxml.load(
                self.write(
                    "rekordbox.xml",
                    rb_text(2).replace(
                        'encoding="UTF-8"', 'encoding="no-such-encoding"'
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
