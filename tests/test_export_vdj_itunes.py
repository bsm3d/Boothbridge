# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks the VirtualDJ folder writer and the iTunes Library.xml writer by reading their output back.

# the VirtualDJ writer (one .vdjfolder per playlist in a tree) and the iTunes XML writer
# (one Library.xml), read back by the readers of the program

import importlib
import os
import plistlib
import threading
import xml.etree.ElementTree as ET

import pytest

from djapp import export
from djapp.export import itunes as itunes_writer
from djapp.export import vdj as vdj_writer
from djapp.export.manifest import Cancelled, Manifest
from djapp.model import Node, Track, playlists
from djapp.sources import itunes as itunes_reader
from djapp.sources import vdj as vdj_reader


# ----------------------------------------------------------
def audio(folder, name, body=b"audio"):
    path = folder / name
    path.write_bytes(body)
    return str(path)


def song(folder, name, **fields) -> Track:
    t = Track(title=name, artist="Artist", path=audio(folder, name + ".mp3", name.encode()))
    for k, v in fields.items():
        setattr(t, k, v)
    return t


def tree(folder):
    a, b, c = song(folder, "Alpha", duration=200.0), song(folder, "Beta"), song(folder, "Gamma")
    return [
        Node("folder", "Sets", children=[
            Node("playlist", "Warm up", tracks=[b, a]),
            Node("folder", "Deep", children=[Node("playlist", "Late", tracks=[c, a, b])]),
        ]),
        Node("playlist", "Top", tracks=[a, c]),
    ]


def files_of(out):
    return sorted(
        os.path.relpath(os.path.join(d, f), out).replace("\\", "/")
        for d, _, names in os.walk(out)
        for f in names
    )


def shape(nodes):
    return [
        (n.name, shape(n.children)) if n.is_folder else (n.name, [t.title for t in n.tracks])
        for n in nodes
    ]


FORMATS = {"vdj": "VirtualDJ", "itunes": "iTunes XML"}


def run(name, nodes, out, **kw):
    module, function = export.WRITERS[FORMATS[name]]
    writer = getattr(importlib.import_module(f"djapp.export.{module}"), function)
    return writer(nodes, str(out), **kw)


HOSTILE = [
    "a<b>&\"'c",
    "ctrl\x01\x02\x0b\x1fend",
    "lone\ud800half",
    "emoji \U0001f3b5 and 日本",
    "x" * 5000,
    "CON",
    "",
    "  spaced  ",
    "100% #1 + a=b?c",
]


# ***************************************
# ***************************************
def test_vdj_layout_and_round_trip(tmp_path):
    out = tmp_path / "out"
    nodes = tree(tmp_path)
    run("vdj", nodes, out)
    assert files_of(out) == [
        "Folders/Sets/Deep/Late.vdjfolder",
        "Folders/Sets/Warm up.vdjfolder",
        "Folders/Top.vdjfolder",
    ]
    root = ET.parse(out / "Folders" / "Sets" / "Warm up.vdjfolder").getroot()
    assert root.tag == "VirtualFolder"
    assert root.get("noDuplicates") == "no" and root.get("ordered") == "yes"
    first = root.findall("song")[0]
    assert os.path.isabs(first.get("path")) and first.get("title") == "Beta"
    assert [s.get("idx") for s in root.findall("song")] == ["0", "1"]
    assert root.findall("song")[1].get("songlength") == "200.0"
    assert int(first.get("size")) == len(b"Beta")

    back = vdj_reader.load(str(out / "database.xml"))
    assert len(back) == 1 and back[0].name == "Folders"
    got = shape(back[0].children)
    # the reader lists folders and playlists in name order, the tracks keep their order
    assert got == [
        ("Sets", [("Deep", [("Late", ["Gamma", "Alpha", "Beta"])]), ("Warm up", ["Beta", "Alpha"])]),
        ("Top", ["Alpha", "Gamma"]),
    ]
    paths = [os.path.normcase(t.path) for pl in playlists(back) for t in pl.tracks]
    wanted = [os.path.normcase(t.path) for pl in playlists(nodes) for t in pl.tracks]
    assert sorted(paths) == sorted(wanted)
    alpha = next(t for pl in playlists(back) for t in pl.tracks if t.title == "Alpha")
    assert alpha.artist == "Artist" and alpha.duration == pytest.approx(200.0)


def test_vdj_writes_no_database(tmp_path):
    out = tmp_path / "out"
    run("vdj", tree(tmp_path), out)
    assert not any(f.endswith("database.xml") for f in files_of(out))


def test_vdj_hostile_names_survive(tmp_path):
    out = tmp_path / "out"
    tracks = [
        Track(title=h, artist=h, path=audio(tmp_path, f"h{i}.mp3"), duration=float("nan"), size=-5)
        for i, h in enumerate(HOSTILE)
    ]
    nodes = [Node("folder", h, children=[Node("playlist", h, tracks=tracks)]) for h in HOSTILE]
    run("vdj", nodes, out)
    for here, _, names in os.walk(out):
        for f in names:
            assert not f.endswith(".tmp")
            ET.parse(os.path.join(here, f))
    back = vdj_reader.load(str(out / "database.xml"))
    lists = list(playlists(back))
    assert lists and all(len(pl.tracks) == len(HOSTILE) for pl in lists)
    assert any(t.title == "emoji \U0001f3b5 and 日本" for t in lists[0].tracks)
    assert all(len(t.title) <= 255 for t in lists[0].tracks)
    assert not any(name.upper().split(".")[0] == "CON" for _, _, names in os.walk(out) for name in names)


def test_vdj_skips_what_has_nothing_to_list(tmp_path):
    out = tmp_path / "out"
    nodes = [
        Node("playlist", "Empty"),
        Node("playlist", "No file", tracks=[Track(title="x")]),
        Node("folder", "Hollow", children=[Node("playlist", "Empty too")]),
        Node("playlist", "Mixed", tracks=[Track(title="x"), song(tmp_path, "Real")]),
    ]
    run("vdj", nodes, out)
    assert files_of(out) == ["Folders/Mixed.vdjfolder"]
    songs = ET.parse(out / "Folders" / "Mixed.vdjfolder").getroot().findall("song")
    assert len(songs) == 1 and songs[0].get("idx") == "0"


def test_vdj_duplicate_track_in_a_playlist_is_kept(tmp_path):
    t = song(tmp_path, "Twice")
    run("vdj", [Node("playlist", "Dup", tracks=[t, t])], tmp_path / "out")
    root = ET.parse(tmp_path / "out" / "Folders" / "Dup.vdjfolder").getroot()
    assert len(root.findall("song")) == 2


def test_vdj_same_name_playlists_do_not_collide(tmp_path):
    out = tmp_path / "out"
    a, b = song(tmp_path, "A"), song(tmp_path, "B")
    nodes = [Node("playlist", "Mix", tracks=[a]), Node("playlist", "mix", tracks=[b])]
    run("vdj", nodes, out)
    assert files_of(out) == ["Folders/Mix.vdjfolder", "Folders/mix (2).vdjfolder"]


def test_vdj_second_export_replaces_in_place(tmp_path):
    out = tmp_path / "out"
    nodes = tree(tmp_path)
    run("vdj", nodes, out)
    before = files_of(out)
    run("vdj", nodes, out)
    assert files_of(out) == before


def test_vdj_cancel_stops_cleanly(tmp_path):
    out = tmp_path / "out"
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        run("vdj", tree(tmp_path), out, cancel=stop)
    assert not [f for f in files_of(out) if f.endswith((".tmp", ".part"))]


def test_vdj_copy_moves_audio_and_points_at_the_copies(tmp_path):
    out = tmp_path / "out"
    src = tmp_path / "src"
    src.mkdir()
    nodes = tree(src)
    run("vdj", nodes, out, copy=True)
    copies = [f for f in files_of(out) if f.startswith("Contents/")]
    assert len(copies) == 3
    root = ET.parse(out / "Folders" / "Top.vdjfolder").getroot()
    for s in root.findall("song"):
        p = s.get("path")
        assert os.path.isfile(p) and os.path.normcase(p).startswith(os.path.normcase(str(out)))


def test_vdj_manifest_registers_every_file(tmp_path):
    out = tmp_path / "out"
    src = tmp_path / "src"
    src.mkdir()
    out.mkdir()
    man = Manifest(str(out))
    run("vdj", tree(src), out, copy=True, man=man)
    assert sorted(man.files) == files_of(out)


# ***************************************
# ***************************************
def test_itunes_plist_layout(tmp_path):
    out = tmp_path / "out"
    t = song(
        tmp_path, "Some Track", album="Al", genre="House", year=2020, bpm=127.6,
        rating=3, duration=215.5, size=1234, plays=4, added="2024-05-06", track_no=2,
    )
    run("itunes", [Node("playlist", "P", tracks=[t])], out)
    lib = plistlib.loads((out / "Library.xml").read_bytes())
    assert (lib["Major Version"], lib["Minor Version"]) == (1, 1)
    assert lib["Application Version"] and lib["Music Folder"].startswith("file://localhost/")
    assert len(lib["Tracks"]) == 1
    (key, d), = lib["Tracks"].items()
    assert key == str(d["Track ID"])
    assert d["Name"] == "Some Track" and d["Artist"] == "Artist" and d["Album"] == "Al"
    assert d["Genre"] == "House" and d["Year"] == 2020 and d["BPM"] == 128
    assert d["Rating"] == 60 and d["Total Time"] == 215500 and d["Size"] == 1234
    assert d["Kind"] == "MPEG audio file" and d["Play Count"] == 4
    assert len(d["Persistent ID"]) == 16 and int(d["Persistent ID"], 16) >= 0
    assert d["Location"].startswith("file://localhost/") and " " not in d["Location"]
    assert d["Date Added"].year == 2024
    pls = lib["Playlists"]
    master, mine = pls[0], pls[1]
    assert master["Master"] is True and master["Playlist Items"] == [{"Track ID": d["Track ID"]}]
    assert mine["Name"] == "P" and mine["Playlist Items"] == [{"Track ID": d["Track ID"]}]
    assert len(mine["Playlist Persistent ID"]) == 16 and "Parent Persistent ID" not in mine


def test_itunes_folders_use_parent_persistent_id(tmp_path):
    out = tmp_path / "out"
    run("itunes", tree(tmp_path), out)
    pls = plistlib.loads((out / "Library.xml").read_bytes())["Playlists"][1:]
    by = {p["Name"]: p for p in pls}
    assert by["Sets"]["Folder"] is True and "Playlist Items" not in by["Sets"]
    assert by["Warm up"]["Parent Persistent ID"] == by["Sets"]["Playlist Persistent ID"]
    assert by["Late"]["Parent Persistent ID"] == by["Deep"]["Playlist Persistent ID"]
    assert by["Deep"]["Parent Persistent ID"] == by["Sets"]["Playlist Persistent ID"]
    assert "Parent Persistent ID" not in by["Top"]
    ids = [p["Playlist ID"] for p in pls]
    pers = [p["Playlist Persistent ID"] for p in pls]
    assert len(set(ids)) == len(ids) and len(set(pers)) == len(pers)


def test_itunes_round_trip(tmp_path):
    out = tmp_path / "out"
    nodes = tree(tmp_path)
    nodes[1].tracks[0].rating = 4
    nodes[1].tracks[0].bpm = 126.0
    run("itunes", nodes, out)
    back = itunes_reader.load(str(out / "Library.xml"))
    assert back[-1].name == "All tracks" and len(back[-1].tracks) == 3
    assert shape(back[:-1]) == shape(nodes)
    paths = [os.path.normcase(os.path.normpath(t.path)) for pl in playlists(back[:-1]) for t in pl.tracks]
    wanted = [os.path.normcase(os.path.normpath(t.path)) for pl in playlists(nodes) for t in pl.tracks]
    assert paths == wanted
    top = back[1].tracks[0]
    assert (top.rating, top.bpm, top.artist) == (4, 126.0, "Artist")
    assert top.duration == pytest.approx(200.0)


def test_itunes_location_is_a_percent_encoded_file_url(tmp_path):
    out = tmp_path / "out"
    name = "100% #1 café +&.mp3"
    t = Track(title="t", path=audio(tmp_path, name))
    run("itunes", [Node("playlist", "P", tracks=[t])], out)
    lib = plistlib.loads((out / "Library.xml").read_bytes())
    url = next(iter(lib["Tracks"].values()))["Location"]
    assert "%25%20%231%20caf%C3%A9%20%2B%26.mp3" in url
    back = itunes_reader.load(str(out / "Library.xml"))
    assert os.path.normcase(back[0].tracks[0].path) == os.path.normcase(os.path.normpath(t.path))


def test_itunes_hostile_content_survives(tmp_path):
    out = tmp_path / "out"
    tracks = [
        Track(
            title=h, artist=h, album=h, genre=h, comment=h,
            path=audio(tmp_path, f"h{i}.mp3"), rating=99, bpm=float("nan"),
            duration=float("inf"), year=10**12, size=-1, added="not a date",
        )
        for i, h in enumerate(HOSTILE)
    ]
    nodes = [Node("folder", h, children=[Node("playlist", h, tracks=tracks)]) for h in HOSTILE]
    run("itunes", nodes, out)
    lib = plistlib.loads((out / "Library.xml").read_bytes())
    assert len(lib["Tracks"]) == len(HOSTILE)
    for d in lib["Tracks"].values():
        assert len(d["Name"]) <= 255 and d["Rating"] == 100
    names = [d["Name"] for d in lib["Tracks"].values()]
    assert "emoji \U0001f3b5 and 日本" in names and "ctrlend" in names
    assert "ctrl\x01" not in "".join(names)
    back = itunes_reader.load(str(out / "Library.xml"))
    assert len(list(playlists(back))) == len(HOSTILE) + 1


def test_itunes_skips_tracks_without_a_file_and_empty_lists(tmp_path):
    out = tmp_path / "out"
    nodes = [
        Node("playlist", "Empty"),
        Node("playlist", "No file", tracks=[Track(title="x")]),
        Node("folder", "Hollow", children=[Node("playlist", "Empty too")]),
        Node("playlist", "Mixed", tracks=[Track(title="x"), song(tmp_path, "Real")]),
    ]
    run("itunes", nodes, out)
    lib = plistlib.loads((out / "Library.xml").read_bytes())
    assert len(lib["Tracks"]) == 1
    assert [p["Name"] for p in lib["Playlists"][1:]] == ["Mixed"]
    assert len(lib["Playlists"][1]["Playlist Items"]) == 1


def test_itunes_same_named_playlists_stay_two(tmp_path):
    out = tmp_path / "out"
    a, b = song(tmp_path, "A"), song(tmp_path, "B")
    nodes = [Node("playlist", "Mix", tracks=[a]), Node("playlist", "Mix", tracks=[b])]
    run("itunes", nodes, out)
    pls = plistlib.loads((out / "Library.xml").read_bytes())["Playlists"][1:]
    assert len(pls) == 2 and pls[0]["Playlist Persistent ID"] != pls[1]["Playlist Persistent ID"]
    assert len(itunes_reader.load(str(out / "Library.xml"))) == 3


def test_itunes_same_track_twice_in_a_playlist_and_shared_between_lists(tmp_path):
    out = tmp_path / "out"
    t = song(tmp_path, "Twice")
    nodes = [Node("playlist", "One", tracks=[t, t]), Node("playlist", "Two", tracks=[t])]
    run("itunes", nodes, out)
    lib = plistlib.loads((out / "Library.xml").read_bytes())
    assert len(lib["Tracks"]) == 1
    assert len(lib["Playlists"][1]["Playlist Items"]) == 2


def test_itunes_second_export_overwrites_the_file(tmp_path):
    out = tmp_path / "out"
    nodes = tree(tmp_path)
    run("itunes", nodes, out)
    first = plistlib.loads((out / "Library.xml").read_bytes())
    run("itunes", nodes, out)
    second = plistlib.loads((out / "Library.xml").read_bytes())
    assert files_of(out) == ["Library.xml"]
    # the identifiers stay the same so that a program matching them sees the same things
    assert [p["Playlist Persistent ID"] for p in first["Playlists"]] == [
        p["Playlist Persistent ID"] for p in second["Playlists"]
    ]
    assert {d["Persistent ID"] for d in first["Tracks"].values()} == {
        d["Persistent ID"] for d in second["Tracks"].values()
    }


def test_itunes_cancel_stops_cleanly(tmp_path):
    out = tmp_path / "out"
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        run("itunes", tree(tmp_path), out, cancel=stop)
    assert not files_of(out)


def test_itunes_copy_moves_audio_and_points_at_the_copies(tmp_path):
    out = tmp_path / "out"
    src = tmp_path / "src"
    src.mkdir()
    run("itunes", tree(src), out, copy=True)
    assert len([f for f in files_of(out) if f.startswith("Contents/")]) == 3
    lib = plistlib.loads((out / "Library.xml").read_bytes())
    assert lib["Music Folder"].endswith("/Contents/")
    back = itunes_reader.load(str(out / "Library.xml"))
    for t in back[-1].tracks:
        assert os.path.isfile(t.path)
        assert os.path.normcase(t.path).startswith(os.path.normcase(str(out)))


def test_itunes_manifest_registers_every_file(tmp_path):
    out = tmp_path / "out"
    src = tmp_path / "src"
    src.mkdir()
    out.mkdir()
    man = Manifest(str(out))
    run("itunes", tree(src), out, copy=True, man=man)
    assert sorted(man.files) == files_of(out)


def test_both_writers_are_registered():
    assert export.WRITERS["VirtualDJ"] == ("vdj", "export")
    assert export.WRITERS["iTunes XML"] == ("itunes", "export")
    assert vdj_writer.export and itunes_writer.export
