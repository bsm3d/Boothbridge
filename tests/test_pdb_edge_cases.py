# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks export.pdb writing and reading on edge cases, with consistency checks between tracks, nodes and cues.

import unittest

from djapp.export.cdj import anlz, pdbread, pdbwrite, reference, validate
from djapp.export.fit import fit
from djapp.model import Node, Track

P = pdbwrite
EMOJI = "Track \U0001F3A7 \U0001F525 中文 العربية café é"


def make(i, **kw):
    t = Track(title=f"Title {i}", artist=f"Artist {i % 50}", album=f"Album {i % 20}", genre=f"Genre {i % 7}",
              label=f"Label {i % 5}", path=f"/music/{i}.mp3", size=1000 + i, bitrate=320, duration=200.0 + i % 90,
              bpm=120.0 + i % 60, key=i % 24 if i % 5 else None, year=2000 + i % 25, rating=i % 6, audio_id=f"id{i}")
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def build(tracks, nodes, longest_path=""):
    items = []
    for i, t in enumerate(tracks, 1):
        item = P.Item(track=fit(t), id=i, path=longest_path or f"/Contents/a/{i}.mp3",
                      anlz=f"/PIONEER/USBANLZ/P000/{i:08X}/ANLZ0000.DAT", size=t.size, art=0)
        items.append(item)
    return P.build_py_file(items, nodes, "2026-10-16", reference.pages())


# an independent read of the whole database: what a player does when it browses
def inconsistencies(data: bytes, tracks: list[Track]) -> list[str]:
    problems = []
    validate.pdb(data)
    pdb = pdbread.read(data)
    rows = [pdbread.track_row(r) for r in pdbread.rows(pdb, P.TRACKS)]
    if [r.id for r in rows] != list(range(1, len(tracks) + 1)):
        problems.append("track ids are not 1..n in order")
    artists, albums = pdbread.artists(pdb), pdbread.albums(pdb)
    genres, labels, keys = (pdbread.names(pdb, k) for k in (P.GENRES, P.LABELS, P.KEYS))
    for kind, table in ((P.ARTISTS, artists), (P.ALBUMS, albums), (P.GENRES, genres), (P.LABELS, labels), (P.KEYS, keys)):
        if len(pdbread.rows(pdb, kind)) != len(table):
            problems.append(f"table {kind}: row ids are not unique")
    for want, row in zip((fit(t) for t in tracks), rows):
        for field, table in (("artist", artists), ("album", albums), ("genre", genres), ("label", labels), ("key", keys)):
            ref = getattr(row, field)
            if ref and ref not in table:
                problems.append(f"track {row.id}: {field} {ref} does not exist")
        if row.strings[17] != want.title:
            problems.append(f"track {row.id}: the title does not read back")
        if any(0xD800 <= ord(c) <= 0xDFFF or c == "�" for c in "".join(row.strings)):
            problems.append(f"track {row.id}: a string holds a lone surrogate")
    tree = pdbread.tree(pdb)
    ids = {n.id for n in tree}
    folders = {n.id for n in tree if n.folder}
    if len(ids) != len(tree):
        problems.append("playlist ids are not unique")
    siblings = {}
    for n in tree:
        siblings.setdefault(n.parent, []).append(n.sort)
        if n.parent and n.parent not in folders:
            problems.append(f"playlist {n.id}: its parent is not a folder")
    for parent, order in siblings.items():
        if sorted(order) != list(range(len(order))):
            problems.append(f"children of {parent}: sort is not 0..n-1")
    position = {}
    track_ids = {r.id for r in rows}
    for index, track, playlist in pdbread.entries(pdb):
        if track not in track_ids:
            problems.append("an entry points at a missing track")
            break
        if playlist not in ids or playlist in folders:
            problems.append("an entry sits in a missing playlist or in a folder")
            break
        position.setdefault(playlist, []).append(index)
    for playlist, idx in position.items():
        if sorted(idx) != list(range(1, len(idx) + 1)):
            problems.append(f"entries of playlist {playlist}: positions are not 1..n")
            break
    return problems


# ------------------------------------------------------------
class EdgeCaseTests(unittest.TestCase):
    def test_nested_folders_empty_playlists_and_a_smartlist(self):
        tracks = [make(1), make(2)]
        nodes = [
            Node("folder", "Top", children=[
                Node("folder", "Mid", children=[Node("folder", "Low", children=[Node("playlist", "Deep", tracks=tracks)])]),
                Node("playlist", "Empty inside"),
            ]),
            Node("smartlist", "Smart", query="BPM > 120"),
            Node("folder", "Empty folder"),
            Node("playlist", "Nothing"),
        ]
        data = build(tracks, nodes)
        self.assertEqual(inconsistencies(data, tracks), [])
        names = [n.name for n in pdbread.tree(pdbread.read(data))]
        self.assertEqual(names, ["Top", "Mid", "Low", "Deep", "Empty inside", "Smart", "Empty folder", "Nothing"])
        # a smartlist has no tracks: it is a playlist without entries, never a folder
        smart = next(n for n in pdbread.tree(pdbread.read(data)) if n.name == "Smart")
        self.assertFalse(smart.folder)

    def test_emoji_cjk_arabic_combining_and_very_long_text_read_back(self):
        tracks = [
            make(1, title=EMOJI, artist=EMOJI, album=EMOJI, genre=EMOJI, label=EMOJI, comment=EMOJI),
            make(2, title="x" * 255, artist="y" * 256, album="z" * 300),
            make(3, title="L" * 5000, artist="A" * 20000, comment="C" * 100000),
            make(4, title="", artist="", album="", genre="", label=""),
            make(5, title="\U0001F3A7" * 200, artist="\U0001F525" * 300),
            make(6, title="a\x00b\x01c\u2028d", artist="tab\tnew\nline"),
        ]
        data = build(tracks, [Node("playlist", "P \U0001F3A7" + "n" * 400, tracks=tracks)])
        self.assertEqual(inconsistencies(data, tracks), [])
        rows = [pdbread.track_row(r) for r in pdbread.rows(pdbread.read(data), P.TRACKS)]
        self.assertEqual(rows[0].strings[17], EMOJI)
        self.assertEqual(rows[4].strings[17], "\U0001F3A7" * 200)

    def test_a_very_long_path_reads_back(self):
        path = "/Contents/" + "d" * 250 + "/" + "f" * 250 + ".mp3"
        data = build([make(1)], [Node("playlist", "p", tracks=[make(1)])], longest_path=path)
        validate.pdb(data)
        row = pdbread.track_row(pdbread.rows(pdbread.read(data), P.TRACKS)[0])
        self.assertEqual(row.strings[20], path)

    def test_more_than_65535_tracks_stay_consistent(self):
        for n in (65535, 65536, 70000):
            with self.subTest(tracks=n):
                tracks = [make(i) for i in range(1, n + 1)]
                nodes = [Node("playlist", "All", tracks=tracks), Node("playlist", "Half", tracks=tracks[::2])]
                self.assertEqual(inconsistencies(build(tracks, nodes), tracks), [])

    def test_the_analysis_folders_stay_unique_and_small_enough_for_fat32(self):
        used = set()
        paths = [anlz.anlz_dir(f"/Contents/Artist {i % 900}/Album/{i}.mp3", used) for i in range(70000)]
        self.assertEqual(len(set(paths)), 70000)
        per_parent = {}
        for p in paths:
            per_parent[p.split("/")[3]] = per_parent.get(p.split("/")[3], 0) + 1
        self.assertLess(max(per_parent.values()), 65534)


if __name__ == "__main__":
    unittest.main()
