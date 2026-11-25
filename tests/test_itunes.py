# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the iTunes library reader on synthetic XML, including playlists, folders and rejected unsafe input.

import tempfile
import unittest
from pathlib import Path

from defusedxml import DefusedXmlException
from djapp import sources
from djapp.model import playlists, unique_tracks
from djapp.sources import itunes, rbxml

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Major Version</key><integer>1</integer><key>Application Version</key><string>12.13</string>
<key>Music Folder</key><string>file://localhost/C:/Music/</string>
"""


def track(i, name, **extra):
    fields = {"Track ID": f"<integer>{i}</integer>", "Name": f"<string>{name}</string>",
              "Artist": "<string>Artist</string>", "Kind": "<string>MPEG audio file</string>",
              "Total Time": "<integer>180000</integer>", "BPM": "<integer>124</integer>",
              "Rating": "<integer>80</integer>", "Play Count": "<integer>3</integer>",
              "Location": f"<string>file://localhost/C:/Music/Some%20Folder/{name}.mp3</string>"}
    fields.update(extra)
    return f"<key>{i}</key><dict>" + "".join(f"<key>{k}</key>{v}" for k, v in fields.items()) + "</dict>"


def playlist(pid, name, ids, parent="", folder=False, **extra):
    body = f"<key>Name</key><string>{name}</string><key>Playlist Persistent ID</key><string>{pid}</string>"
    if parent:
        body += f"<key>Parent Persistent ID</key><string>{parent}</string>"
    if folder:
        body += "<key>Folder</key><true/>"
    body += "".join(f"<key>{k}</key>{v}" for k, v in extra.items())
    if ids:
        body += "<key>Playlist Items</key><array>" + "".join(f"<dict><key>Track ID</key><integer>{i}</integer></dict>" for i in ids) + "</array>"
    return f"<dict>{body}</dict>"


class ITunesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def library(self, tracks, playlists, name="Library.xml"):
        text = (HEAD + "<key>Tracks</key><dict>" + "".join(tracks) + "</dict><key>Playlists</key><array>"
                + "".join(playlists) + "</array></dict></plist>")
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_the_library_is_recognised_and_its_tracks_and_playlists_are_read(self):
        path = self.library(
            [track(1, "One", Year="<integer>2020</integer>", **{"Date Added": "<date>2021-03-04T10:00:00Z</date>"}),
             track(2, "Two", Genre="<string>House</string>")],
            [playlist("MASTER", "Library", [1, 2], Master="<true/>"),
             playlist("F1", "Sets", [], folder=True),
             playlist("P1", "Warm up", [2, 1], parent="F1")])
        self.assertEqual(sources.detect(path), "itunes")
        nodes = sources.load(path)
        names = [(n.kind, n.name) for n in nodes]
        self.assertEqual(names, [("folder", "Sets"), ("playlist", "All tracks")])
        warm = nodes[0].children[0]
        self.assertEqual([t.title for t in warm.tracks], ["Two", "One"])
        one = warm.tracks[1]
        self.assertTrue(one.path.replace("\\", "/").endswith("C:/Music/Some Folder/One.mp3"))
        self.assertEqual((one.rating, one.plays, one.bpm, one.duration, one.year, one.added), (4, 3, 124.0, 180.0, 2020, "2021-03-04"))
        self.assertIs(nodes[1].tracks[0], warm.tracks[1])  # a shared track is one object

    def test_files_that_cannot_be_played_are_left_out(self):
        path = self.library(
            [track(1, "Keep"), track(2, "Drm", Protected="<true/>"), track(3, "Cast", Kind="<string>Podcast</string>"),
             track(4, "Film", **{"Has Video": "<true/>"}),
             ("<key>5</key><dict><key>Track ID</key><integer>5</integer>"
              "<key>Name</key><string>Cloud</string></dict>")],
            [playlist("P1", "All", [1, 2, 3, 4, 5])])
        nodes = sources.load(path)
        self.assertEqual([t.title for t in nodes[0].tracks], ["Keep"])

    def test_empty_playlists_and_empty_folders_are_not_listed(self):
        path = self.library([track(1, "One")], [playlist("F1", "Empty folder", [], folder=True), playlist("P1", "Empty", [])])
        self.assertEqual([n.name for n in sources.load(path)], ["All tracks"])

    def test_a_computed_album_rating_is_not_a_rating_and_junk_numbers_are_harmless(self):
        path = self.library([track(1, "One", **{"Rating Computed": "<true/>", "Year": "<integer>99999999999999999999999</integer>",
                                               "BPM": "<real>nan</real>"})], [])
        t = sources.load(path)[0].tracks[0]
        self.assertEqual((t.rating, t.year, t.bpm), (0, 0, 0.0))

    def test_an_xml_with_entities_or_without_tracks_is_refused(self):
        bad = self.root / "Library.xml"
        bad.write_text('<?xml version="1.0"?><!DOCTYPE plist [<!ENTITY x "y">]><plist><dict><key>Tracks</key><dict/>'
                       '<key>Name</key><string>&x;</string></dict></plist>', encoding="utf-8")
        with self.assertRaises(DefusedXmlException):
            itunes.load(str(bad))
        empty = self.root / "Other.xml"
        empty.write_text(HEAD + "</dict></plist>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not an iTunes library"):
            itunes.load(str(empty))

    def test_a_deep_nesting_is_a_bad_file(self):
        deep = "<array>" * 40 + "</array>" * 40
        path = self.root / "Library.xml"
        path.write_text(HEAD + "<key>Tracks</key>" + deep + "</dict></plist>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "too deeply"):
            itunes.load(str(path))

    def test_location_paths(self):
        self.assertEqual(itunes.location_path("file://localhost/Users/me/A%20B.mp3").replace("\\", "/"), "/Users/me/A B.mp3")
        self.assertEqual(itunes.location_path("file:///C:/Music/a.mp3").replace("\\", "/"), "C:/Music/a.mp3")
        self.assertEqual(itunes.location_path("https://example.com/a.mp3"), "")
        self.assertEqual(itunes.location_path(""), "")

    def test_a_share_keeps_its_host(self):
        # file://nas/... used to lose the host and point at a folder of the local disk
        self.assertEqual(itunes.location_path("file://nas/Music/a%20b.mp3").replace("\\", "/"), "//nas/Music/a b.mp3")
        self.assertEqual(itunes.location_path("file://localhost//nas/Music/a.mp3").replace("\\", "/"), "//nas/Music/a.mp3")
        self.assertEqual(itunes.location_path("file://C:/Music/a.mp3").replace("\\", "/"), "C:/Music/a.mp3")
        self.assertEqual(rbxml.url_to_path("file://nas/Music/a%20b.mp3"), "//nas/Music/a b.mp3")
        self.assertEqual(rbxml.url_to_path("file://LOCALHOST/C:/Music/a.mp3").replace("\\", "/"), "C:/Music/a.mp3")

    def test_folders_in_a_circle_and_a_chain_too_deep_keep_their_playlists(self):
        # Q sat in two folders that are each other's parent: no root reached it and it was dropped. A chain of
        # 1500 folders loaded, then every walk of the tree hit the recursion limit
        chain = [playlist(f"C{i}", f"C{i}", [], parent=f"C{i - 1}" if i else "", folder=True) for i in range(1500)]
        path = self.library([track(1, "One"), track(2, "Two")],
                            [playlist("Q", "Q", [1], parent="A"), playlist("A", "A", [], parent="B", folder=True),
                             playlist("B", "B", [], parent="A", folder=True), *chain,
                             playlist("D", "Deep", [2], parent="C1499")])
        nodes = sources.load(path)
        lists = {n.name: n for n in playlists(nodes)}
        self.assertEqual([t.title for t in lists["Q"].tracks], ["One"])
        self.assertEqual([t.title for t in lists["Deep"].tracks], ["Two"])
        self.assertEqual(len(unique_tracks(nodes)), 2)

        def depth(ns):
            return 1 + max((depth(n.children) for n in ns), default=0) if ns else 0

        self.assertLessEqual(depth(nodes), itunes.TREE_DEPTH + 1)


if __name__ == "__main__":
    unittest.main()
