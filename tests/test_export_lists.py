# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks CSV, JSON, PLS and XSPF exports: they parse back, hostile text survives and copied audio matches.

# CSV, JSON, PLS and XSPF exports: every file parses back, hostile text survives, a
# spreadsheet cannot be made to run a formula, copied audio is what the paths point to

import csv
import io
import json
import threading
import xml.etree.ElementTree as ET
from configparser import RawConfigParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from djapp import export, settings
from djapp.export import lists, manifest
from djapp.export.manifest import Cancelled
from djapp.model import CUE, FADE_IN, GRID, LOOP, Cue, Node, Track

NS = "{http://xspf.org/ns/0/}"


# ------------------------------------------------------------
def audio(folder: Path, name: str, **fields) -> Track:
    p = folder / name
    p.write_bytes(name.encode() * 50)
    t = Track(title=Path(name).stem, artist="Artist", path=str(p), duration=200.4)
    for k, v in fields.items():
        setattr(t, k, v)
    return t


def cfg(**extra) -> dict:
    return {**settings.DEFAULTS, "copy_music": False, "verify_copy": False, **extra}


@pytest.fixture
def tree(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    a = audio(music, "a.mp3", album="Alb", genre="House", year=2020, bpm=124.5, key=12)
    a.rating, a.energy = 4, 7.0
    b = audio(music, "b.flac", artist="")
    c = audio(music, "c.wav", comment="note")
    nodes = [
        Node("folder", "Sets", children=[Node("playlist", "Warm", tracks=[a, b])]),
        Node("playlist", "Peak", tracks=[c, a]),
        Node("playlist", "Empty"),
        Node("folder", "Nothing", children=[Node("playlist", "Void")]),
    ]
    return nodes, tmp_path / "out", (a, b, c)


def read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\r\n")
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def read_pls(path: Path) -> RawConfigParser:
    p = RawConfigParser(interpolation=None, delimiters=("=",))
    p.optionxform = str
    p.read_string(path.read_text("utf-8"))
    return p


def url_path(url: str) -> str:
    u = urlparse(url)
    assert u.scheme == "file"
    s = unquote(u.path)
    if u.netloc:
        return "//" + u.netloc + s
    # file:///C:/x on Windows
    return s[1:] if len(s) > 2 and s[2] == ":" else s


# ------------------------------------------------------------
class TestCsv:
    def test_rows_and_header(self, tree):
        nodes, out, (a, b, c) = tree
        lists.export_csv(nodes, str(out))
        rows = read_csv(out / "playlists.csv")
        assert rows[0] == list(lists.COLUMNS)
        assert len(rows) == 1 + 2 + 2
        first = dict(zip(rows[0], rows[1]))
        assert first["playlist"] == "Sets / Warm"
        assert first["position"] == "1"
        assert first["title"] == "a"
        assert first["year"] == "2020"
        assert first["bpm"] == "124.5"
        assert first["key"] == "Cm"
        assert first["rating"] == "4"
        assert first["energy"] == "7"
        assert first["duration"] == "200.4"
        assert first["path"] == a.path
        second = dict(zip(rows[0], rows[2]))
        assert second["position"] == "2" and second["artist"] == ""
        assert second["energy"] == "" and second["key"] == ""
        assert {r[0] for r in rows[1:]} == {"Sets / Warm", "Peak"}

    def test_quoting_and_accents(self, tmp_path):
        t = Track(title='He said "hi", ok\nnext', artist="Béatrice, é", path="x.mp3")
        lists.export_csv([Node("playlist", 'Été, "live"', tracks=[t])], str(tmp_path))
        raw = (tmp_path / "playlists.csv").read_bytes()
        assert b'"He said ""hi"", ok\nnext"' in raw
        rows = read_csv(tmp_path / "playlists.csv")
        assert rows[1][0] == 'Été, "live"'
        assert rows[1][3] == 'He said "hi", ok\nnext'
        assert rows[1][2] == "Béatrice, é"

    @pytest.mark.parametrize("bad", ["=1+1", "+1", "-2", "@SUM(A1)", "\t=x", "\r=x"])
    def test_formula_is_neutralised(self, tmp_path, bad):
        t = Track(title=bad, artist=bad, album=bad, genre=bad, path="x.mp3")
        lists.export_csv([Node("playlist", bad + "p", tracks=[t])], str(tmp_path))
        rows = read_csv(tmp_path / "playlists.csv")
        for cell in rows[1][2:6]:
            assert cell == "'" + bad
        assert not any(c.startswith(lists.FORMULA) for c in rows[1])

    def test_folder_name_formula(self, tmp_path):
        t = Track(title="t", path="x.mp3")
        pl = Node("playlist", "p", tracks=[t])
        lists.export_csv([Node("folder", "=evil", children=[pl])], str(tmp_path))
        # the cell starts with the folder, so it is the whole cell that is protected
        assert read_csv(tmp_path / "playlists.csv")[1][0] == "'=evil / p"

    def test_pathless_and_hostile_tracks_are_kept(self, tmp_path):
        bad = Track(title="a\ud800b\x00c", artist="\U0001f3b5", bpm=float("nan"))
        bad.duration, bad.year, bad.key = 1e12, -5, 99
        lists.export_csv(
            [Node("playlist", "P" * 5000, tracks=[bad, bad])], str(tmp_path)
        )
        rows = read_csv(tmp_path / "playlists.csv")
        assert len(rows) == 3
        assert rows[1][3] == "a\ufffdbc" and rows[1][2] == "\U0001f3b5"
        assert rows[1][12] == "" and rows[1][8] == ""


# ------------------------------------------------------------
class TestJson:
    def load(self, out: Path) -> dict:
        raw = (out / "playlists.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        return json.loads(raw.decode("utf-8"))

    def test_tree(self, tree):
        nodes, out, (a, b, c) = tree
        lists.export_json(nodes, str(out))
        doc = self.load(out)
        assert doc["format"] == "boothbridge-playlists" and doc["version"] == 1
        folder, peak = doc["tree"]
        assert folder["kind"] == "folder" and folder["name"] == "Sets"
        warm = folder["children"][0]
        assert warm["kind"] == "playlist" and warm["name"] == "Warm"
        assert [t["title"] for t in warm["tracks"]] == ["a", "b"]
        assert [t["title"] for t in peak["tracks"]] == ["c", "a"]
        t = warm["tracks"][0]
        assert t["key"] == "Cm" and t["energy"] == 7 and t["bpm"] == 124.5
        assert t["path"] == a.path and t["cues"] == [] and "grid" not in t
        assert warm["tracks"][1]["energy"] is None
        assert len(doc["tree"]) == 2

    def test_cues_and_grid(self, tmp_path):
        t = Track(title="g", path="x.mp3", bpm=128.0, grid=73.25)
        t.cues = [
            Cue("Drop", CUE, 1000.0, hotcue=2, color="#FF0000"),
            Cue("Mem", CUE, 5000.5),
            Cue("Loop", LOOP, 8000.0, 2000.0, hotcue=-1),
            Cue("", FADE_IN, 1.0),
            Cue("", GRID, 73.25),
            Cue("far", CUE, float("inf")),
        ]
        lists.export_json([Node("playlist", "p", tracks=[t])], str(tmp_path))
        got = self.load(tmp_path)["tree"][0]["tracks"][0]
        assert got["grid"] == {"anchor": 73.25, "bpm": 128}
        cues = got["cues"]
        assert [c["type"] for c in cues] == ["cue", "cue", "loop", "fade_in"]
        assert cues[0] == {
            "type": "cue",
            "slot": 2,
            "name": "Drop",
            "start": 1000,
            "color": "#FF0000",
        }
        assert cues[1]["slot"] is None
        assert cues[2]["start"] == 8000 and cues[2]["end"] == 10000

    def test_lone_surrogates_and_accents(self, tmp_path):
        t = Track(title="a\ud800b é", artist="\udc00", path="x\udfff.mp3")
        t.cues = [Cue("c\ud800", CUE, 1.0, color="\ud800")]
        lists.export_json([Node("playlist", "n\ud83d", tracks=[t])], str(tmp_path))
        raw = (tmp_path / "playlists.json").read_bytes()
        assert "é".encode() in raw
        doc = json.loads(raw)
        assert doc["tree"][0]["tracks"][0]["title"] == "a\ufffdb é"

    def test_escaped_fallback_still_parses(self, tmp_path, monkeypatch):
        # a writer that let a surrogate through falls back to \u escapes
        doc = {"x": "a\ud800"}
        monkeypatch.setattr(lists, "tree", lambda nodes, moved: [doc])
        data = lists.json_bytes([], {})
        assert json.loads(data)["tree"][0]["x"] == "a\ud800"

    def test_absurd_numbers(self, tmp_path):
        t = Track(title="n", path="p.mp3", bpm=1e300, duration=float("nan"))
        t.rating, t.energy, t.grid = 99, float("inf"), float("nan")
        lists.export_json([Node("playlist", "p", tracks=[t])], str(tmp_path))
        got = self.load(tmp_path)["tree"][0]["tracks"][0]
        assert got["rating"] == 5 and got["energy"] is None and "grid" not in got
        assert got["bpm"] == 655.35 and got["duration"] is None


# ------------------------------------------------------------
class TestPls:
    def test_layout(self, tree):
        nodes, out, (a, b, c) = tree
        lists.export_pls(nodes, str(out))
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.pls"))
        assert files == ["Playlists/Peak.pls", "Playlists/Sets/Warm.pls"]
        raw = (out / "Playlists" / "Sets" / "Warm.pls").read_bytes()
        assert raw.startswith(b"[playlist]\n") and not raw.startswith(b"\xef")
        p = read_pls(out / "Playlists" / "Sets" / "Warm.pls")["playlist"]
        assert p["NumberOfEntries"] == "2" and p["Version"] == "2"
        assert p["File1"] == a.path and p["Title1"] == "Artist - a"
        assert p["Length1"] == "200"
        # no artist: the title alone
        assert p["File2"] == b.path and p["Title2"] == "b"

    def test_unknown_length_and_one_line_titles(self, tmp_path):
        t = Track(title="two\nlines\r\nhere", artist="x", path=str(tmp_path / "q.mp3"))
        pl = Node("playlist", "p", tracks=[t])
        lists.export_pls([pl], str(tmp_path / "out"))
        got = read_pls(tmp_path / "out" / "Playlists" / "p.pls")["playlist"]
        assert got["Length1"] == "-1"
        assert got["Title1"] == "x - two lines  here"

    def test_pathless_track_is_left_out(self, tmp_path):
        t = Track(title="no file")
        u = Track(title="ok", path="z.mp3")
        lists.export_pls(
            [Node("playlist", "p", tracks=[t, u]), Node("playlist", "q", tracks=[t])],
            str(tmp_path),
        )
        assert [p.name for p in tmp_path.rglob("*.pls")] == ["p.pls"]
        assert (
            read_pls(tmp_path / "Playlists" / "p.pls")["playlist"]["NumberOfEntries"]
            == "1"
        )

    def test_names(self, tmp_path):
        t = Track(title="t", path="z.mp3")
        nodes = [
            Node("playlist", "CON", tracks=[t]),
            Node("playlist", "same", tracks=[t]),
            Node("playlist", "SAME", tracks=[t]),
            Node("playlist", 'a/b:c*"d"', tracks=[t]),
            Node("playlist", "x" * 600, tracks=[t]),
        ]
        lists.export_pls(nodes, str(tmp_path))
        names = sorted(p.name for p in (tmp_path / "Playlists").iterdir())
        assert len(names) == 5 and "CON.pls" not in names
        assert "same.pls" in names and "SAME (2).pls" in names
        assert all(len(n) <= 205 for n in names)

    def test_copy_gives_relative_paths_to_the_copies(self, tree):
        nodes, out, (a, b, c) = tree
        lists.export_pls(nodes, str(out), copy=True)
        where = out / "Playlists" / "Sets"
        p = read_pls(where / "Warm.pls")["playlist"]
        target = (where / p["File1"]).resolve()
        assert "\\" not in p["File1"] and target.is_file()
        assert out.resolve() in target.parents
        assert target.read_bytes() == Path(a.path).read_bytes()


# ------------------------------------------------------------
class TestXspf:
    def load(self, path: Path) -> ET.Element:
        root = ET.parse(path).getroot()
        assert root.tag == NS + "playlist" and root.get("version") == "1"
        return root

    def test_layout(self, tree):
        nodes, out, (a, b, c) = tree
        lists.export_xspf(nodes, str(out))
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.xspf"))
        assert files == ["Playlists/Peak.xspf", "Playlists/Sets/Warm.xspf"]
        root = self.load(out / "Playlists" / "Sets" / "Warm.xspf")
        assert root.find(NS + "title").text == "Warm"
        tracks = root.find(NS + "trackList").findall(NS + "track")
        assert len(tracks) == 2
        t = tracks[0]
        assert url_path(t.find(NS + "location").text) == Path(a.path).as_posix()
        assert t.find(NS + "title").text == "a"
        assert t.find(NS + "creator").text == "Artist"
        assert t.find(NS + "album").text == "Alb"
        assert t.find(NS + "duration").text == "200400"
        assert tracks[1].find(NS + "creator") is None
        peak = self.load(out / "Playlists" / "Peak.xspf")
        note = peak.find(NS + "trackList")[0].find(NS + "annotation")
        assert note.text == "note"
        raw = (out / "Playlists" / "Peak.xspf").read_text("utf-8")
        assert raw.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert 'xmlns="http://xspf.org/ns/0/"' in raw

    def test_location_is_a_percent_encoded_url(self):
        assert lists.file_url("C:\\Music\\a b&c#é.mp3") == (
            "file:///C:/Music/a%20b%26c%23%C3%A9.mp3"
        )
        assert lists.file_url("\\\\srv\\share\\a b.mp3") == "file://srv/share/a%20b.mp3"
        assert lists.file_url("/home/me/a b.mp3") == "file:///home/me/a%20b.mp3"
        rel = lists.file_url("rel/a.mp3")
        assert rel.startswith("file://") and rel.endswith("/rel/a.mp3")

    def test_hostile_text_stays_well_formed(self, tmp_path):
        bad = "<&>\"' \x00\x01\x1f\ud800\ufffe é \U0001f3b5\ncut"
        t = Track(title=bad, artist=bad, album=bad, comment=bad, path=bad + ".mp3")
        t.duration = 1e12
        nodes = [Node("playlist", bad, tracks=[t, t])]
        lists.export_xspf(nodes, str(tmp_path))
        (f,) = (tmp_path / "Playlists").iterdir()
        root = self.load(f)
        tracks = root.find(NS + "trackList").findall(NS + "track")
        assert len(tracks) == 2
        title = tracks[0].find(NS + "title").text
        assert title.startswith("<&>\"' ") and "\n" not in title and "é" in title
        assert "\x00" not in title and "\U0001f3b5" in title
        assert tracks[0].find(NS + "location").text.startswith("file://")

    def test_copy_points_to_the_copies(self, tree):
        nodes, out, (a, b, c) = tree
        lists.export_xspf(nodes, str(out), copy=True)
        root = self.load(out / "Playlists" / "Sets" / "Warm.xspf")
        loc = root.find(NS + "trackList")[0].find(NS + "location").text
        target = Path(url_path(loc)).resolve()
        assert out.resolve() in target.parents and target.is_file()
        assert target.read_bytes() == Path(a.path).read_bytes()

    def test_same_names_do_not_collide(self, tmp_path):
        t = Track(title="t", path="z.mp3")
        nodes = [
            Node("playlist", "dup", tracks=[t]),
            Node("playlist", "dup", tracks=[t]),
        ]
        lists.export_xspf(nodes, str(tmp_path))
        assert sorted(p.name for p in (tmp_path / "Playlists").iterdir()) == [
            "dup (2).xspf",
            "dup.xspf",
        ]


# ------------------------------------------------------------
WRITERS = {
    "CSV": ("playlists.csv", lists.export_csv),
    "JSON": ("playlists.json", lists.export_json),
    "PLS": ("Playlists/Peak.pls", lists.export_pls),
    "XSPF": ("Playlists/Peak.xspf", lists.export_xspf),
}


@pytest.mark.parametrize("fmt", WRITERS)
class TestEveryFormat:
    def test_cancel_stops_cleanly(self, tree, fmt):
        nodes, out, _ = tree
        ev = threading.Event()
        ev.set()
        with pytest.raises(Cancelled):
            WRITERS[fmt][1](nodes, str(out), cancel=ev)
        assert not any(p.suffix == ".tmp" for p in out.rglob("*"))
        assert not (out / WRITERS[fmt][0]).exists()

    def test_cancel_during_the_copy(self, tree, fmt):
        nodes, out, _ = tree
        ev = threading.Event()
        ev.set()
        with pytest.raises(Cancelled):
            WRITERS[fmt][1](nodes, str(out), copy=True, cancel=ev)
        assert not any(p.suffix == ".tmp" for p in out.rglob("*"))

    def test_second_export_replaces_the_first(self, tree, fmt):
        nodes, out, _ = tree
        name, fn = WRITERS[fmt]
        fn(nodes, str(out))
        first = sorted(p.relative_to(out).as_posix() for p in out.rglob("*"))
        before = (out / name).read_bytes()
        fn(nodes, str(out))
        assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == first
        assert (out / name).read_bytes() == before

    def test_manifest_lists_every_file_and_verifies(self, tree, fmt):
        nodes, out, _ = tree
        rep = export.run(fmt, nodes, str(out), cfg(verify_copy=True))
        assert rep.errors == [] and rep.verified > 0
        written = {
            p.relative_to(out).as_posix()
            for p in out.rglob("*")
            if p.is_file() and p.name != manifest.NAME
        }
        assert written and written == set(manifest.read(str(out))["files"])

    def test_progress_ends_at_100(self, tree, fmt):
        nodes, out, _ = tree
        seen = []
        WRITERS[fmt][1](
            nodes, str(out), copy=True, progress=lambda p, m: seen.append(p)
        )
        assert seen[-1] == 100 and seen == sorted(seen)

    def test_no_playlist_no_crash(self, tmp_path, fmt):
        out = tmp_path / "o"
        rep = WRITERS[fmt][1]([], str(out))
        assert rep.tracks == 0 and out.is_dir()

    def test_report_counts_missing_audio(self, tmp_path, fmt):
        t = Track(title="gone", path=str(tmp_path / "gone.mp3"))
        rep = WRITERS[fmt][1]([Node("playlist", "p", tracks=[t])], str(tmp_path / "o"))
        assert rep.tracks == 1 and rep.missing == [t.path]


# a folder named like a playlist file wanted the path of that file, and the second of the two
# to be written failed on it, in every export that writes one file per playlist
@pytest.mark.parametrize("fmt,ext", [("PLS", ".pls"), ("XSPF", ".xspf"), ("M3U", ".m3u8"), ("VirtualDJ", ".vdjfolder")])
@pytest.mark.parametrize("folder_first", [True, False])
def test_a_folder_named_like_a_playlist_file_does_not_take_its_path(tmp_path, fmt, ext, folder_first):
    t = Track(title="t", path=str(tmp_path / "z.mp3"))
    nodes = [
        Node("folder", "A" + ext.upper(), children=[Node("playlist", "inner", tracks=[t])]),
        Node("playlist", "A", tracks=[t]),
    ]
    if not folder_first:
        nodes.reverse()
    out = tmp_path / "o"
    rep = export.run(fmt, nodes, str(out), cfg())
    assert rep.errors == []
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*" + ext) if p.is_file())
    assert len(files) == 2 and any("/inner" + ext in f for f in files) and any(f.endswith("/A" + ext) for f in files)


# a checksum list whose mtime was not a number passed the reading, then the next export that
# reused a copy fell over comparing it with the file's time
def test_a_manifest_with_a_time_that_is_not_a_number_is_refused_and_the_next_export_runs(tree):
    nodes, out, _ = tree
    export.run("PLS", nodes, str(out), cfg(copy_music=True))
    p = out / manifest.NAME
    data = json.loads(p.read_text("utf-8"))
    for e in data["files"].values():
        e["mtime"] = "soon"
    data["seal"] = manifest.seal(data["files"])
    p.write_text(json.dumps(data), "utf-8")
    with pytest.raises(ValueError):
        manifest.read(str(out))
    rep = export.run("PLS", nodes, str(out), cfg(copy_music=True, verify_copy=True))
    assert rep.errors == [] and rep.verified > 0


def test_registry_points_to_the_four_writers():
    for fmt in WRITERS:
        assert export.WRITERS[fmt] == ("lists", f"export_{fmt.lower()}")
        assert callable(getattr(lists, f"export_{fmt.lower()}"))
