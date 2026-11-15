# Benoit Saint-Moulin
# Mixxx export: a new standalone mixxxdb.sqlite, read back with the program's own reader

import os
import sqlite3
import threading

import pytest

from djapp import export
from djapp.export import manifest, mixxx
from djapp.export.manifest import Cancelled
from djapp.model import CUE, LOAD, LOOP, Cue, Node, Track
from djapp.sources import mixxx as reader


def audio(folder, name, size=64) -> str:
    p = os.path.join(str(folder), name)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


def track(folder, name, **fields) -> Track:
    t = Track(title=name, artist="Artist", path=audio(folder, name + ".mp3"))
    t.duration, t.bpm, t.samplerate = 200.5, 128.0, 44100
    for k, v in fields.items():
        setattr(t, k, v)
    return t


def pl(name, *tracks) -> Node:
    return Node("playlist", name, tracks=list(tracks))


def db(out) -> str:
    return os.path.join(str(out), "mixxxdb.sqlite")


def view(t: Track):
    return (t.title, t.artist, t.album, t.genre, t.comment, t.year, t.rating, t.key)


# ----------------------------------------------------------------
def test_round_trip_tracks_playlists_cues(tmp_path):
    a = track(
        tmp_path,
        "a",
        album="Alb",
        genre="House",
        comment="note",
        year=1999,
        rating=4,
        key=13,
        plays=3,
        bitrate=320,
        track_no=7,
    )
    a.cues = [
        Cue("drop", CUE, 1500.0, 0.0, 0),
        Cue("loop", LOOP, 4000.0, 2000.0, 1),
        Cue("start", LOAD, 250.0, 0.0, -1),
        Cue("memory", CUE, 900.0, 0.0, -1),
    ]
    b = track(tmp_path, "b")
    nodes = [
        Node("folder", "Sets", children=[pl("Warm", a, b), pl("Peak", b)]),
        pl("Top", b, a),
    ]
    out = tmp_path / "out"
    rep = mixxx.export(nodes, str(out))
    assert rep.path == db(out) and rep.tracks == 2
    got = reader.load(db(out))
    assert [n.name for n in got] == ["Sets / Warm", "Sets / Peak", "Top"]
    assert [[t.title for t in n.tracks] for n in got] == [
        ["a", "b"],
        ["b"],
        ["b", "a"],
    ]
    r = got[0].tracks[0]
    assert view(r) == view(a)
    assert r.path == a.path.replace("\\", "/") or os.path.samefile(r.path, a.path)
    assert r.bpm == 128.0 and r.duration == 200.5 and r.plays == 3 and r.bitrate == 320
    assert r.size == 64 and r.track_no == 7
    cues = {(c.hotcue, c.kind): c for c in r.cues}
    assert set(cues) == {(0, CUE), (1, LOOP), (-1, LOAD)}
    assert cues[(0, CUE)].name == "drop" and abs(cues[(0, CUE)].start - 1500) < 0.01
    assert abs(cues[(1, LOOP)].length - 2000) < 0.01
    assert abs(cues[(-1, LOAD)].start - 250) < 0.01
    # one row per file, shared by the playlists
    assert got[0].tracks[1] is not None
    with sqlite3.connect(db(out)) as con:
        assert con.execute("select count(*) from library").fetchone()[0] == 2


def test_schema_is_version_39_and_opens_cleanly(tmp_path):
    out = tmp_path / "out"
    mixxx.export([pl("p", track(tmp_path, "a"))], str(out))
    con = sqlite3.connect(db(out))
    try:
        settings = dict(con.execute("select name, value from settings"))
        assert settings["mixxx.schema.version"] == "39"
        assert settings["mixxx.schema.last_used_version"] == "39"
        assert settings["mixxx.schema.min_compatible_version"] == "3"
        assert con.execute("pragma integrity_check").fetchall() == [("ok",)]
        bad = [
            r
            for r in con.execute("pragma foreign_key_check")
            if not (r[0] == "library" and r[2] == "track_locations")
        ]
        assert bad == []
        names = {r[0] for r in con.execute("select name from sqlite_master")}
        for need in (
            "library",
            "track_locations",
            "Playlists",
            "PlaylistTracks",
            "crates",
            "crate_tracks",
            "cues",
            "settings",
            "idx_PlaylistTracks_track_id",
        ):
            assert need in names
        row = con.execute(
            "select fs_deleted, needs_verification, filename, directory, location "
            "from track_locations"
        ).fetchone()
        assert row[0] == 0 and row[1] == 0 and row[2] == "a.mp3"
        assert "\\" not in row[4] and row[4] == row[3] + "/a.mp3"
        lib = con.execute(
            "select mixxx_deleted, key, key_id, header_parsed, beats from library"
        ).fetchone()
        assert lib == (0, "", 0, 1, None)
    finally:
        con.close()


def test_key_numbering_and_text(tmp_path):
    ts = [track(tmp_path, f"k{i}", key=i) for i in (0, 11, 12, 23)]
    out = tmp_path / "out"
    mixxx.export([pl("p", *ts)], str(out))
    with sqlite3.connect(db(out)) as con:
        rows = con.execute("select key, key_id from library order by id").fetchall()
    assert rows == [("C", 1), ("B", 12), ("Cm", 13), ("Bm", 24)]


def test_a_second_export_into_the_same_folder_is_refused_and_changes_nothing(tmp_path):
    out = tmp_path / "out"
    mixxx.export([pl("p", track(tmp_path, "a"))], str(out))
    before = open(db(out), "rb").read()
    with pytest.raises(ValueError):
        mixxx.export([pl("q", track(tmp_path, "b"))], str(out))
    assert open(db(out), "rb").read() == before
    assert sorted(os.listdir(out)) == ["mixxxdb.sqlite"]


def test_an_existing_mixxx_database_is_never_touched(tmp_path):
    out = tmp_path / "profile"
    out.mkdir()
    real = out / "mixxxdb.sqlite"
    real.write_bytes(b"the library of a user")
    with pytest.raises(ValueError):
        mixxx.export([pl("p", track(tmp_path, "a"))], str(out))
    assert real.read_bytes() == b"the library of a user"
    assert os.listdir(out) == ["mixxxdb.sqlite"]


def test_same_named_playlists_do_not_collide(tmp_path):
    a, b = track(tmp_path, "a"), track(tmp_path, "b")
    nodes = [pl("Mix", a), pl("mix", b), Node("folder", "F", children=[pl("Mix", a)])]
    out = tmp_path / "out"
    mixxx.export(nodes, str(out))
    got = reader.load(db(out))
    assert len({n.name.casefold() for n in got}) == 3
    assert [n.tracks[0].title for n in got] == ["a", "b", "a"]


def test_empty_playlists_and_tracks_without_path_are_skipped(tmp_path):
    ghost = Track(title="ghost")
    nodes = [
        pl("none"),
        pl("only ghost", ghost),
        pl("real", ghost, track(tmp_path, "a")),
    ]
    out = tmp_path / "out"
    mixxx.export(nodes, str(out))
    got = reader.load(db(out))
    assert [n.name for n in got] == ["real"]
    assert [t.title for t in got[0].tracks] == ["a"]


def test_hostile_content_survives(tmp_path):
    nasty = "A<b>&\"' \x00\x07\ud800 \U0001f600 ; DROP TABLE library;--"
    t = track(
        tmp_path,
        "con",
        title=nasty,
        artist="x" * 5000,
        comment="c" * 70000,
        rating=99,
        year=-5,
        bpm=float("inf"),
        duration=float("nan"),
        key=999,
        size=-3,
    )
    t.cues = [
        Cue(nasty, CUE, float("nan"), 0.0, 0),
        Cue("far", CUE, 1e30, 0.0, 1),
        Cue("slot", CUE, 10.0, 0.0, 400),
        Cue("same", CUE, 20.0, 0.0, 2),
        Cue("twin", CUE, 30.0, 0.0, 2),
    ]
    twin = track(tmp_path, "con", title="same file again")
    twin.path = t.path
    nodes = [pl(nasty, t, twin, t), pl("NUL"), pl("CON", twin)]
    out = tmp_path / "out"
    mixxx.export(nodes, str(out))
    got = reader.load(db(out))
    r = got[0].tracks[0]
    assert "DROP TABLE" in r.title and "\x00" not in r.title
    assert len(r.artist) <= 255 and len(r.comment) <= 1000
    assert r.rating == 5 and r.bpm == 0 and r.key is None
    assert [c.hotcue for c in r.cues] == [2]
    assert [t.title for t in got[0].tracks] == [r.title, r.title, r.title]
    with sqlite3.connect(db(out)) as con:
        assert con.execute("select count(*) from library").fetchone()[0] == 1
        assert con.execute("pragma integrity_check").fetchall() == [("ok",)]


def test_tracks_without_cues_or_a_grid_and_empty_fields(tmp_path):
    t = Track(title="", path=audio(tmp_path, "z.flac"))
    out = tmp_path / "out"
    mixxx.export([pl("p", t)], str(out))
    r = reader.load(db(out))[0].tracks[0]
    assert r.title == "" and r.cues == [] and r.key is None
    with sqlite3.connect(db(out)) as con:
        assert con.execute("select filetype, beats from library").fetchone() == (
            "flac",
            None,
        )


def test_cue_positions_follow_the_sample_rate(tmp_path):
    t = track(tmp_path, "a", samplerate=48000)
    t.cues = [Cue("c", CUE, 1000.0, 0.0, 0), Cue("l", LOOP, 2000.0, 500.0, -1)]
    out = tmp_path / "out"
    mixxx.export([pl("p", t)], str(out))
    with sqlite3.connect(db(out)) as con:
        rows = con.execute(
            "select type, position, length, hotcue, color from cues order by type"
        ).fetchall()
    # interleaved stereo samples: 1 s is 96000
    assert rows[0][:4] == (1, 96000.0, 0.0, 0)
    assert rows[1][:4] == (4, 192000.0, 48000.0, -1)
    assert all(r[4] != 0 for r in rows)


def test_cancel_stops_cleanly_and_leaves_nothing(tmp_path):
    ev = threading.Event()
    ev.set()
    out = tmp_path / "out"
    with pytest.raises(Cancelled):
        mixxx.export([pl("p", track(tmp_path, "a"))], str(out), cancel=ev)
    assert os.listdir(out) == []


def test_a_failure_leaves_no_temporary_file(tmp_path, monkeypatch):
    def boom(con):
        raise ValueError("boom")

    monkeypatch.setattr(mixxx, "check", boom)
    out = tmp_path / "out"
    with pytest.raises(ValueError):
        mixxx.export([pl("p", track(tmp_path, "a"))], str(out))
    assert os.listdir(out) == []


def test_copy_puts_the_audio_in_the_export_and_paths_point_to_it(tmp_path):
    t = track(tmp_path, "a")
    out = tmp_path / "out"
    mixxx.export([pl("p", t)], str(out), copy=True)
    r = reader.load(db(out))[0].tracks[0]
    assert os.path.isfile(r.path)
    assert os.path.normcase(os.path.abspath(r.path)).startswith(
        os.path.normcase(os.path.abspath(str(out)))
    )
    assert os.path.getsize(r.path) == 64 and r.size == 64


def test_manifest_registers_the_database_and_the_copies(tmp_path):
    t = track(tmp_path, "a")
    out = tmp_path / "out"
    man = manifest.Manifest(str(out))
    mixxx.export([pl("p", t)], str(out), copy=True, man=man)
    man.save()
    assert "mixxxdb.sqlite" in man.files
    assert any(k.startswith("Contents/") for k in man.files)
    assert manifest.verify(str(out)).good


def test_registered_through_run(tmp_path):
    out = tmp_path / "out"
    rep = export.run(
        "Mixxx",
        [pl("p", track(tmp_path, "a"))],
        str(out),
        {"copy_music": False, "verify_copy": True},
    )
    assert rep.tracks == 1
    assert manifest.verify(str(out)).good
