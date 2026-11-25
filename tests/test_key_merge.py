# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks merging playlists into a key that already holds some: old playlists stay and new ones join them.

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from djapp import export, settings
from djapp.export import manifest
from djapp.export.cdj import keymerge, reference, settingsread
from djapp.model import Cue, Node, Track, unique_tracks
from djapp.sources import key

from tests import child_env
from tests.test_key_support import (
    cue_view,
    export_to,
    make_track,
    names_of,
    normalised_pdb,
    pdb_bytes,
    random_library,
    tmp,
    tree_view,
)

REPO = Path(__file__).resolve().parent.parent


def tracks_in(nodes):
    return {t.path: t for t in unique_tracks(nodes)}


class MergeFunctionTests(unittest.TestCase):
    def pl(self, name, *paths):
        return Node("playlist", name, tracks=[Track(path=p) for p in paths])

    def test_same_name_without_case_replaces_in_place_and_the_rest_is_appended(self):
        old = [self.pl("Warm up", "a"), self.pl("Peak", "b"), self.pl("Late", "c")]
        new = [self.pl("new one", "d"), self.pl("PEAK", "e")]
        merged, replaced = keymerge.merge(new, old)
        self.assertEqual([n.name for n in merged], ["Warm up", "PEAK", "Late", "new one"])
        self.assertEqual(replaced, ["Peak"])
        self.assertEqual([t.path for t in merged[1].tracks], ["e"])
        self.assertEqual([t.path for t in merged[0].tracks], ["a"])

    def test_folders_are_merged_level_by_level_and_a_name_is_only_a_match_in_its_own_folder(self):
        old = [Node("folder", "Sets", children=[self.pl("A", "1"), Node("folder", "Deep", children=[self.pl("B", "2")])]),
               self.pl("A", "3")]
        new = [Node("folder", "SETS", children=[Node("folder", "deep", children=[self.pl("b", "4"), self.pl("C", "5")]),
                                                self.pl("D", "6")]),
               Node("folder", "Other", children=[self.pl("A", "7")])]
        merged, replaced = keymerge.merge(new, old)
        self.assertEqual(names_of(merged), ["Sets / A", "Sets / Deep / b", "Sets / Deep / C", "Sets / D", "A", "Other / A"])
        self.assertEqual(replaced, ["Sets / Deep / B"])
        self.assertEqual(merged[0].name, "Sets")  # the folder that was there keeps its name

    def test_a_folder_and_a_playlist_with_the_same_name_are_not_the_same_thing(self):
        old = [Node("folder", "X", children=[self.pl("in", "1")])]
        merged, replaced = keymerge.merge([self.pl("x", "2")], old)
        self.assertEqual((len(merged), replaced), (2, []))

    def test_a_track_is_one_object_and_the_new_one_wins(self):
        old_track = Track(path="K1", title="old")
        old = [Node("playlist", "Old", tracks=[old_track, Track(path="K2")])]
        fresh = Track(path="k1", title="new")
        new = [Node("playlist", "New", tracks=[fresh, fresh])]
        merged, _ = keymerge.merge(new, old)
        self.assertIs(merged[0].tracks[0], fresh)
        self.assertEqual(len(unique_tracks(merged)), 2)
        self.assertEqual([t.title for t in merged[0].tracks], ["new", ""])  # the playlist that stays
        self.assertEqual([t.title for t in merged[1].tracks], ["new", "new"])

    def test_ident_decides_what_the_same_track_is(self):
        a, b = Track(path="/src/one.mp3"), Track(path="/key/Contents/one.mp3")
        merged, _ = keymerge.merge([Node("playlist", "N", tracks=[a])], [Node("playlist", "O", tracks=[b])],
                                   lambda t: "same")
        self.assertIs(merged[1].tracks[0], a)
        self.assertIs(merged[0].tracks[0], a)

    def test_inputs_are_not_changed(self):
        old = [self.pl("A", "1")]
        new = [self.pl("a", "2"), self.pl("B", "3")]
        before = (tree_view_names(old), tree_view_names(new), [t for n in old for t in n.tracks])
        merged, _ = keymerge.merge(new, old)
        self.assertEqual((tree_view_names(old), tree_view_names(new), [t for n in old for t in n.tracks]), before)
        self.assertIsNot(merged[0], new[0])
        self.assertIsNot(merged[0], old[0])


def tree_view_names(nodes):
    return [(n.kind, n.name, [t.path for t in n.tracks], tree_view_names(n.children)) for n in nodes]


# --------------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.temp, self.root = tmp()
        self.addCleanup(self.temp.cleanup)
        self.music = self.root / "music"
        self.music.mkdir()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.out = self.root / "key"
        self.tracks = [make_track(self.music, f"m{i}", i, title=f"Title {i}", bpm=120 + i, grid=50.5 + i,
                                  cues=[Cue(name=f"cue {i}", start=400 + i, hotcue=i % 8)]) for i in range(6)]
        self.a = Node("playlist", "A", tracks=self.tracks[:3])
        self.b = Node("playlist", "B", tracks=self.tracks[2:5])

    def run_export(self, nodes, out=None, **extra):
        return export_to(nodes, out or self.out, self.cache, **extra)

    def snapshot(self, root=None):
        base = Path(root or self.out)
        return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(base.rglob("*")) if p.is_file()}

    def loaded(self, out=None):
        return key.read(str(out or self.out))


# --------------------------------------------------------------------------
class KeepTests(Base):
    def test_a_then_b_leaves_both_on_the_key_with_every_track_once(self):
        self.assertEqual(self.run_export([self.a], verify_copy=True).errors, [])
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (self.out / "Contents").rglob("*.flac")}
        rep = self.run_export([self.b], verify_copy=True)
        self.assertEqual(rep.errors, [])
        self.assertEqual((rep.kept, rep.replaced, rep.added), (1, [], 1))
        got = self.loaded()
        self.assertEqual(names_of(got.nodes), ["A", "B"])
        ours = tracks_in(got.nodes)
        self.assertEqual(len(ours), 5)
        self.assertEqual(rep.tracks, 5)
        # the audio of A is not copied again, nothing moved
        for p, stat in before.items():
            self.assertEqual((p.stat().st_mtime_ns, p.stat().st_size), stat)
        # A is as it was: metadata, cues and grid
        mine = [t for t in got.nodes[0].tracks]
        self.assertEqual([cue_view(c) for t in mine for c in t.cues],
                         [cue_view(c) for t in self.a.tracks for c in t.cues])
        self.assertEqual([round(t.bpm * 100) for t in mine], [round(t.bpm * 100) for t in self.a.tracks])
        self.assertTrue(all(t.grid is not None for t in mine))
        # the track both playlists share is one row of the database
        self.assertIs(got.nodes[0].tracks[2], got.nodes[1].tracks[0])
        summary = export.key_summary(str(self.out))
        self.assertEqual((summary.playlists, summary.tracks, summary.names), (2, 5, ["A", "B"]))

    def test_the_verification_after_a_merged_export_finds_nothing_wrong(self):
        self.run_export([self.a], verify_copy=True)
        self.run_export([self.b], verify_copy=True)
        chk = manifest.verify(str(self.out))
        self.assertTrue(chk.good, chk.summary())
        listed = json.loads((self.out / manifest.NAME).read_text(encoding="utf-8"))["files"]
        audio = [p for p in listed if p.endswith(".flac")]
        self.assertEqual(len(audio), 5)  # the audio of A is listed too
        self.assertEqual(len([p for p in listed if p.endswith("ANLZ0000.DAT")]), 5)
        self.assertFalse([p for p in listed if p.endswith(".bak")])
        for rel in ("PIONEER/rekordbox/export.pdb", "PIONEER/MYSETTING.DAT"):
            self.assertIn(rel, listed)

    def test_the_old_database_is_kept_as_a_backup_and_the_backup_is_replaced_next_time(self):
        self.run_export([self.a])
        first = pdb_bytes(self.out)
        bak = self.out / "PIONEER" / "rekordbox" / "export.pdb.bak"
        self.assertFalse(bak.exists())
        self.run_export([self.b])
        self.assertEqual(bak.read_bytes(), first)
        second = pdb_bytes(self.out)
        self.run_export([Node("playlist", "C", tracks=self.tracks[:1])])
        self.assertEqual(bak.read_bytes(), second)
        self.assertFalse(list(bak.parent.glob("*.tmp")) + list(bak.parent.glob("*.verified")))
        # the verification of the key does not see it
        self.assertTrue(manifest.verify(str(self.out)).good)

    def test_exporting_a_again_after_editing_it_replaces_it(self):
        self.run_export([self.a, self.b])
        edited = Node("playlist", "a", tracks=[self.tracks[0], self.tracks[5]])
        self.tracks[0].cues = [Cue(name="moved", start=1234, hotcue=4)]
        self.tracks[0].title = "Edited title"
        rep = self.run_export([edited], verify_copy=True)
        self.assertEqual(rep.errors, [])
        self.assertEqual((rep.kept, rep.replaced, rep.added), (1, ["A"], 0))
        got = self.loaded()
        self.assertEqual(names_of(got.nodes), ["a", "B"])
        self.assertEqual([t.title for t in got.nodes[0].tracks], ["Edited title", "Title 5"])
        self.assertEqual([(c.name, c.start, c.hotcue) for c in got.nodes[0].tracks[0].cues], [("moved", 1234.0, 4)])
        # the track B also lists is the same new one in both playlists, and nothing is doubled
        shared = [t for t in got.nodes[1].tracks if t.title == "Edited title"]
        self.assertEqual(shared, [])
        self.assertEqual(len(tracks_in(got.nodes)), 5)  # m1, m2 left A but B still has m2
        self.assertEqual(len(list((self.out / "Contents").rglob("*.flac"))), 6)

    def test_a_new_track_replaces_the_one_on_the_key_in_the_playlists_that_stay(self):
        self.run_export([self.a, self.b])
        self.tracks[2].title = "Changed in B only"
        self.tracks[2].comment = "new comment"
        self.run_export([Node("playlist", "C", tracks=[self.tracks[2]])])
        got = self.loaded()
        titles = {pl.name: [t.title for t in pl.tracks] for pl in got.nodes}
        self.assertEqual(titles["A"][2], "Changed in B only")
        self.assertEqual(titles["B"][0], "Changed in B only")
        self.assertEqual(titles["C"], ["Changed in B only"])
        self.assertEqual(len(tracks_in(got.nodes)), 5)

    def test_folders_keep_their_structure(self):
        tree = [Node("folder", "Club", children=[Node("folder", "Sets", children=[self.a]), self.b])]
        self.run_export(tree)
        more = [Node("folder", "club", children=[Node("folder", "SETS", children=[
                    Node("playlist", "C", tracks=self.tracks[4:])]), Node("playlist", "B", tracks=self.tracks[:1])]),
                Node("playlist", "Loose", tracks=self.tracks[5:])]
        rep = self.run_export(more)
        self.assertEqual(rep.replaced, ["Club / B"])
        got = self.loaded().nodes
        self.assertEqual([(n.kind, n.name) for n in got], [("folder", "Club"), ("playlist", "Loose")])
        self.assertEqual(names_of(got), ["Club / Sets / A", "Club / Sets / C", "Club / B", "Loose"])
        self.assertEqual([t.title for t in got[0].children[1].tracks], ["Title 0"])

    def test_the_default_is_keep_an_unknown_value_is_keep_and_no_value_is_replace(self):
        self.assertEqual(settings.DEFAULTS["key_mode"], "keep")
        self.assertTrue(keymerge.keeping(dict(settings.DEFAULTS)))
        self.assertTrue(keymerge.keeping({"key_mode": "whatever"}))
        self.assertFalse(keymerge.keeping({}))  # a hand made config is the old behaviour
        self.assertTrue(keymerge.keeping({"key_mode": "Keep"}))
        self.assertFalse(keymerge.keeping({"key_mode": "replace"}))
        self.assertFalse(keymerge.keeping({"key_mode": " Replace "}))

    def test_a_key_without_a_database_is_just_exported(self):
        (self.out / "Contents").mkdir(parents=True)
        rep = self.run_export([self.a])
        self.assertEqual((rep.errors, rep.kept, rep.added), ([], 0, 0))
        self.assertEqual(names_of(self.loaded().nodes), ["A"])
        self.assertFalse((self.out / "PIONEER" / "rekordbox" / "export.pdb.bak").exists())


# ------------------------------------------------------------------------
class ReplaceTests(Base):
    def test_replace_is_what_the_export_always_did(self):
        self.run_export([self.a])
        rep = self.run_export([self.b], key_mode="replace", verify_copy=True)
        self.assertEqual((rep.errors, rep.kept, rep.replaced, rep.added), ([], 0, [], 0))
        self.assertEqual(names_of(self.loaded().nodes), ["B"])
        # the database is the one a fresh key gets from B alone
        self.run_export([self.b], out=self.root / "fresh", key_mode="replace")
        self.assertEqual(normalised_pdb(pdb_bytes(self.out)), normalised_pdb(pdb_bytes(self.root / "fresh")))
        # the audio A left on the key is still there, not in the database
        self.assertEqual(len(list((self.out / "Contents").rglob("*.flac"))), 5)
        self.assertEqual(manifest.verify(str(self.out)).good, True)


    def test_replace_does_not_read_the_old_database_but_saves_it(self):
        self.run_export([self.a])
        db = self.out / "PIONEER" / "rekordbox" / "export.pdb"
        db.write_bytes(b"this is not a database")
        rep = self.run_export([self.b], key_mode="replace")
        self.assertEqual(rep.errors, [])
        self.assertEqual(names_of(self.loaded().nodes), ["B"])
        self.assertEqual((db.parent / "export.pdb.bak").read_bytes(), b"this is not a database")

# ----------------------------------------------------------------------
class RefusalTests(Base):
    def test_a_database_that_cannot_be_read_stops_keep_and_touches_nothing(self):
        self.run_export([self.a])
        db = self.out / "PIONEER" / "rekordbox" / "export.pdb"
        good = db.read_bytes()
        for damage in (good[:5000], b"\0" * len(good), good[:4096 * 3] + b"\xff" * 4096 + good[4096 * 4:]):
            db.write_bytes(damage)
            before = self.snapshot()
            with self.assertRaises(ValueError) as cm:
                self.run_export([self.b])
            self.assertIn("Nothing was written", str(cm.exception))
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(list(self.out.rglob("*.tmp")) + list(self.out.rglob("*.part")))
        self.assertTrue((self.out / manifest.NAME).exists())

    def test_the_headless_command_reports_it_and_replace_gets_out_of_it(self):
        from djapp import app, sources
        self.run_export([self.a])
        db = self.out / "PIONEER" / "rekordbox" / "export.pdb"
        db.write_bytes(db.read_bytes()[:5000])
        project = self.root / "b.m3u8"
        project.write_text("\n".join(t.path for t in self.b.tracks), encoding="utf-8")
        self.assertEqual(sources.detect(str(project)), "m3u")
        lines = []
        from unittest.mock import patch

        from tests.test_key_support import inline
        with inline(self.cache), patch.object(app, "log_line", lines.append), \
                patch.object(settings, "load", return_value=dict(settings.DEFAULTS, anlz_processes=1, verify_copy=False)):
            self.assertEqual(app.headless(["--export", str(project), str(self.out), "--format", "CDJ/USB"]), 1)
            self.assertIn("export failed", lines[-1])
            self.assertIn("replace", lines[-1])
            self.assertEqual(app.headless(["--export", str(project), str(self.out), "--format", "CDJ/USB", "--replace"]), 0)
            self.assertEqual(names_of(self.loaded().nodes), ["b"])
            self.assertEqual(app.headless(["--export", str(project), str(self.out), "--format", "CDJ/USB", "--keep", "--replace"]), 2)


# --------------------------------------------------------------------------
class CommandLineTests(Base):
    def test_keep_is_the_default_and_the_log_line_says_what_happened(self):
        from unittest.mock import patch

        from djapp import app

        from tests.test_key_support import inline
        for name, tracks in (("one", self.tracks[:2]), ("two", self.tracks[2:4])):
            (self.root / f"{name}.m3u8").write_text("\n".join(t.path for t in tracks), encoding="utf-8")
        lines = []
        base = dict(settings.DEFAULTS, anlz_processes=1, verify_copy=False)
        with inline(self.cache), patch.object(app, "log_line", lines.append), patch.object(settings, "load", return_value=base):
            self.assertEqual(app.headless(["--export", str(self.root / "one.m3u8"), str(self.out), "--format", "CDJ/USB"]), 0)
            self.assertEqual(app.headless(["--export", str(self.root / "two.m3u8"), str(self.out), "--format", "CDJ/USB", "--keep"]), 0)
            self.assertIn("kept 1 playlist already on the key, replaced 0, added 1", lines[-1])
            self.assertEqual(app.headless(["--export", str(self.root / "one.m3u8"), str(self.out), "--format", "CDJ/USB"]), 0)
            self.assertIn("kept 1 playlist already on the key, replaced 1 (one), added 0", lines[-1])
            self.assertEqual(app.headless(["--export", str(self.root / "one.m3u8"), str(self.out), "--format", "CDJ/USB", "--replace"]), 0)
            self.assertIn("replaced the database", lines[-1])
        self.assertEqual(names_of(self.loaded().nodes), ["one"])
        self.assertIn("--replace", app.USAGE)
        self.assertIn("--keep", app.USAGE)


# ----------------------------------------------------------------------
class SameKeyTests(Base):
    def test_a_key_loaded_edited_and_exported_back_keeps_its_audio(self):
        self.run_export([self.a, self.b])
        audio = {p: p.read_bytes() for p in (self.out / "Contents").rglob("*.flac")}
        nodes = key.load(str(self.out))
        nodes[0].tracks[0].cues = [Cue(name="edited", start=777, hotcue=7)]
        nodes[0].name = "A2"
        for verify in (False, True):
            rep = self.run_export(nodes, verify_copy=verify)
            self.assertEqual(rep.errors, [])
            self.assertEqual({p: p.read_bytes() for p in (self.out / "Contents").rglob("*.flac")}, audio)
        got = self.loaded().nodes
        self.assertEqual(names_of(got), ["A", "B", "A2"])
        self.assertEqual([c.name for c in got[2].tracks[0].cues], ["edited"])
        self.assertEqual(len(list((self.out / "Contents").rglob("*.flac"))), 5)
        self.assertEqual(len(list((self.out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.DAT"))), 5)

    def test_replace_to_the_same_key_too(self):
        self.run_export([self.a])
        audio = {p: p.read_bytes() for p in (self.out / "Contents").rglob("*.flac")}
        nodes = key.load(str(self.out))
        rep = self.run_export(nodes, key_mode="replace", verify_copy=True)
        self.assertEqual(rep.errors, [])
        self.assertEqual({p: p.read_bytes() for p in (self.out / "Contents").rglob("*.flac")}, audio)

    def test_a_key_copy_is_exported_to_another_key_whole(self):
        self.run_export([self.a, self.b])
        other = self.root / "other"
        rep = self.run_export(key.load(str(self.out)), out=other, verify_copy=True)
        self.assertEqual(rep.errors, [])
        self.assertEqual(tree_view(key.load(str(other))), tree_view(key.load(str(self.out))))


# ------------------------------------------------------
class IdempotenceTests(Base):
    def anlz_files(self, root):
        base = Path(root) / "PIONEER" / "USBANLZ"
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*.*"))}

    def test_loading_a_key_and_exporting_it_again_gives_the_same_files(self):
        for seed in (1, 2, 3, 4, 5, 6):
            with self.subTest(seed=seed):
                one, two = self.root / f"one{seed}", self.root / f"two{seed}"
                self.run_export(random_library(seed, self.music, 8), out=one, key_mode="replace")
                self.run_export(key.load(str(one)), out=two, key_mode="replace")
                self.assertEqual(normalised_pdb(pdb_bytes(one)), normalised_pdb(pdb_bytes(two)), f"seed {seed}")
                self.assertEqual(self.anlz_files(one), self.anlz_files(two), f"seed {seed}")

    def test_exporting_the_same_playlists_again_in_keep_mode_changes_nothing(self):
        nodes = random_library(9, self.music, 8)
        self.run_export(nodes)
        db, files = pdb_bytes(self.out), self.anlz_files(self.out)
        rep = self.run_export(nodes)
        self.assertEqual(rep.added, 0)
        self.assertEqual(normalised_pdb(pdb_bytes(self.out)), normalised_pdb(db))
        self.assertEqual(self.anlz_files(self.out), files)

    def test_a_track_keeps_its_analysis_folder_when_the_order_changes(self):
        # every path starts its search at the same folder, so the folder a track gets depends
        # on the order the tracks are placed in
        from unittest.mock import patch

        from djapp.export.cdj import anlz

        real = anlz.anlz_dir
        with patch.object(anlz, "anlz_dir", lambda path, used=None: real("/same", used)):
            self.run_export([self.a])
            first = dict(self.loaded().anlz)
            self.assertEqual(len(set(first.values())), 3)
            self.run_export([Node("playlist", "A", tracks=self.a.tracks[::-1]), self.b])
        second = self.loaded().anlz
        for p, folder in first.items():
            self.assertEqual(second[p], folder)
        self.assertEqual(len(set(second.values())), 5)


# the C++ core and the Python reference write the same merged key. Without fastcore both
# runs are Python, which only shows that two runs agree
class NativeTests(Base):
    SCRIPT = r"""
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests.test_key_support import export_to
from djapp import native
from djapp.model import Node, Cue, Track
from tests.test_key_support import random_library
music, out, cache = (Path(p) for p in sys.argv[2:5])
nodes = random_library(21, music, 7)
export_to(nodes[:1], out, cache)
export_to(nodes[1:] or nodes, out, cache)
files = {}
for p in sorted(out.rglob("*")):
    if p.is_file() and "Contents" not in p.parts and p.name != "boothbridge_checksums.json":
        data = p.read_bytes()
        if p.name == "export.pdb":
            data = data[:0x14] + bytes(4) + data[0x18:]
        files[str(p.relative_to(out))] = hashlib.sha256(data).hexdigest()
print(json.dumps({"native": native.available(), "files": files}))
"""

    def run_script(self, name, pure):
        env = child_env(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        env.pop("BB_PURE_PYTHON", None)
        if pure:
            env["BB_PURE_PYTHON"] = "1"
        out = subprocess.run([sys.executable, "-c", self.SCRIPT, str(REPO), str(self.music), str(self.root / name),
                              str(self.root / f"cache_{name}")], capture_output=True, text=True, env=env,
                             cwd=str(REPO), timeout=300, check=False)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_native_and_python_agree_on_a_merged_key(self):
        for name in ("native", "pure"):
            (self.root / f"cache_{name}").mkdir()
        fast, pure = self.run_script("native", False), self.run_script("pure", True)
        self.assertFalse(pure["native"])
        self.assertEqual(fast["files"], pure["files"])
        self.assertGreater(len(pure["files"]), 10)


# --------------------------------------------------------------------
class PlayerSettingsTests(Base):
    def player_file(self):
        return self.out / "PIONEER" / "MYSETTING.DAT"

    def read_back(self, name="MYSETTING.DAT"):
        return settingsread.read(name, (self.out / "PIONEER" / name).read_bytes())

    def test_the_four_files_written_pass_every_check(self):
        built = reference.files()
        for name in reference.SETTINGS:
            self.assertEqual(settingsread.read(name, built[name]).problems, [], name)
        self.assertEqual(settingsread.read("MYSETTING.DAT", built["MYSETTING.DAT"]).value(11), reference.ON)

    def test_each_check_names_what_is_wrong(self):
        good = reference.files()["MYSETTING.DAT"]
        flipped = bytearray(good)
        flipped[settingsread.HEAD.size + 10] ^= 1
        self.assertEqual(settingsread.read("MYSETTING.DAT", bytes(flipped)).problems, ["checksum"])
        self.assertEqual(settingsread.read("MYSETTING.DAT", good[:-3]).problems, ["size"])
        self.assertEqual(settingsread.read("MYSETTING.DAT", b"short").problems, ["short"])
        self.assertEqual(settingsread.read("OTHER.DAT", good).problems, ["header"])
        # a file with a good checksum that is another one of the four
        self.assertIn("magic", settingsread.read("DJMMYSETTING.DAT", good).problems)
        # the mixer checksum covers the header: a changed version breaks it
        mixer = bytearray(reference.files()["DJMMYSETTING.DAT"])
        mixer[70] ^= 1
        self.assertEqual(settingsread.read("DJMMYSETTING.DAT", bytes(mixer)).problems, ["checksum"])

    def test_the_choices_keep_known_switches_and_drop_the_rest(self):
        self.assertEqual(reference.parse_choices("11:0, 23:1"), {11: reference.OFF, 23: reference.ON})
        for bad in ("", "x", "11:2", "12:1", "11", "11:1:1", None, 5, "-11:1", "11:１", "²:1"):
            self.assertEqual(reference.parse_choices(bad), {}, repr(bad))
        text = reference.format_choices({36: reference.ON, 11: reference.OFF, 12: reference.ON})
        self.assertEqual(text, "11:0,36:1")
        self.assertEqual(reference.parse_choices(text), {11: reference.OFF, 36: reference.ON})

    def test_a_new_key_gets_the_chosen_values_and_the_defaults_elsewhere(self):
        rep = self.run_export([self.a], player_settings="11:0,23:1")
        self.assertEqual((rep.errors, rep.settings_problems, rep.settings_set), ([], [], 2))
        got = self.read_back()
        self.assertTrue(got.ok)
        self.assertEqual((got.value(11), got.value(23)), (reference.OFF, reference.ON))
        default = settingsread.read("MYSETTING.DAT", reference.files()["MYSETTING.DAT"])
        others = [p for p in range(1, 41) if p not in (11, 23)]
        self.assertEqual([got.value(p) for p in others], [default.value(p) for p in others])

    def test_no_choice_leaves_the_file_exactly_as_it_was(self):
        self.run_export([self.a])
        before = self.player_file().read_bytes()
        self.assertEqual(before, reference.files()["MYSETTING.DAT"])
        self.player_file().write_bytes(settingsread.patched(settingsread.read("MYSETTING.DAT", before), before, {10: 0x85}))
        owner = self.player_file().read_bytes()
        rep = self.run_export([self.b])
        self.assertEqual((rep.settings_problems, rep.settings_set), ([], 0))
        self.assertEqual(self.player_file().read_bytes(), owner)

    def test_chosen_settings_change_only_their_bytes_in_a_file_already_on_the_key(self):
        self.run_export([self.a])
        before = self.player_file().read_bytes()
        self.player_file().write_bytes(settingsread.patched(settingsread.read("MYSETTING.DAT", before), before, {10: 0x85}))
        rep = self.run_export([self.b], player_settings="35:0")
        self.assertEqual(rep.settings_problems, [])
        got = self.read_back()
        self.assertTrue(got.ok, got.problems)
        self.assertEqual((got.value(35), got.value(10)), (reference.OFF, 0x85))
        default = reference.files()["MYSETTING.DAT"]
        changed = [i for i in range(len(got.body)) if got.body[i] != settingsread.read("MYSETTING.DAT", default).body[i]]
        self.assertEqual([i + 1 for i in changed], [10, 35])

    def test_a_damaged_file_is_reported_and_left_alone(self):
        self.run_export([self.a])
        data = bytearray(self.player_file().read_bytes())
        data[settingsread.HEAD.size + 10] ^= 1
        self.player_file().write_bytes(bytes(data))
        rep = self.run_export([self.b], player_settings="35:0")
        self.assertEqual(rep.settings_problems, [("MYSETTING.DAT", "checksum")])
        self.assertEqual(self.player_file().read_bytes(), bytes(data))

    def test_a_file_of_another_size_is_not_changed(self):
        self.run_export([self.a])
        body = bytes(60)
        raw = self.player_file().read_bytes()
        rebuilt = reference._settings_file("PIONEER", "0.001", reference.PLAYER_BODY_MAGIC + body)
        self.player_file().write_bytes(rebuilt)
        rep = self.run_export([self.b], player_settings="35:0")
        self.assertEqual(rep.settings_problems, [("MYSETTING.DAT", "layout")])
        self.assertEqual(self.player_file().read_bytes(), rebuilt)
        self.assertNotEqual(raw, rebuilt)

    def test_the_verification_still_passes_after_the_choices_are_written(self):
        self.run_export([self.a], verify_copy=True)
        self.run_export([self.b], verify_copy=True, player_settings="36:1")
        chk = manifest.verify(str(self.out))
        self.assertTrue(chk.good, chk.summary())
        self.assertEqual(self.read_back().value(36), reference.ON)


if __name__ == "__main__":
    unittest.main()
