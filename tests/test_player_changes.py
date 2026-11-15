# A player rewrites the analysis files of the tracks it plays. An export that keeps those files must not
# call that damage, and a playlist none of whose files can be found is not written empty on the key

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp import settings
from djapp.export import manifest
from djapp.export.files import Report
from djapp.model import Node, Track, playlists
from djapp.sources import key
from djapp.ui.dialogs import LogWindow
from djapp.ui.main_window import MainWindow
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.test_key_support import export_to, make_track, tmp


# ------------------------------------------------------------
# what a CDJ did to the files of a key: the same size, the same time stamp, other bytes. Here the
# header field that rekordbox and this program write as 1 becomes 0
def as_a_player_does(path: Path):
    st = path.stat()
    raw = bytearray(path.read_bytes())
    assert raw[15] == 1
    raw[15] = 0
    path.write_bytes(bytes(raw))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


# ------------------------------------------------------------
class KeptFilesTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.root = tmp()
        self.addCleanup(self.dir.cleanup)
        self.music, self.cache, self.out = self.root / "music", self.root / "cache", self.root / "key"
        self.music.mkdir()
        self.cache.mkdir()
        self.old = [make_track(self.music, f"old{i}", i + 1, artist=f"Artist {i}") for i in range(3)]
        self.new = make_track(self.music, "fresh", 9, artist="Fresh")
        export_to([Node("playlist", "Old", tracks=self.old)], self.out, self.cache, verify_copy=True)
        self.dat = sorted((self.out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.DAT"))[1]
        self.rel = self.dat.relative_to(self.out).as_posix()

    def merge(self):
        return export_to([Node("playlist", "New", tracks=[self.new])], self.out, self.cache, verify_copy=True)

    def test_a_kept_file_changed_by_a_player_is_not_an_error(self):
        as_a_player_does(self.dat)
        rep = self.merge()
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.player_changed, [self.rel])
        self.assertEqual(rep.reused, 3)
        self.assertEqual(self.dat.read_bytes()[15], 0)  # left as the player made it

    def test_nothing_changed_nothing_said(self):
        rep = self.merge()
        self.assertEqual((rep.errors, rep.player_changed), ([], []))

    def test_the_later_check_names_it_as_modified_and_a_strict_one_as_damaged(self):
        as_a_player_does(self.dat)
        self.merge()
        lenient = manifest.verify(str(self.out), allow_player_changes=True)
        self.assertEqual((lenient.modified, lenient.damaged), ([self.rel], []))
        strict = manifest.verify(str(self.out))
        self.assertEqual((strict.modified, strict.damaged), ([], [self.rel]))

    def test_the_file_the_export_writes_itself_is_still_checked_strictly(self):
        rep = self.merge()
        fresh = sorted((self.out / "PIONEER" / "USBANLZ").rglob("ANLZ0000.DAT"))
        written = next(p for p in fresh if p != self.dat and p.relative_to(self.out).as_posix() not in rep.player_changed)
        as_a_player_does(written)
        chk = manifest.verify(str(self.out), lenient=frozenset({self.rel}))
        self.assertEqual(chk.damaged, [written.relative_to(self.out).as_posix()])


# ------------------------------------------------------------
class LenientTests(unittest.TestCase):
    def test_only_an_analysis_file_listed_as_kept_is_forgiven(self):
        dir_, root = tmp()
        self.addCleanup(dir_.cleanup)
        dat = root / "PIONEER" / "USBANLZ" / "P001" / "00000001" / "ANLZ0000.DAT"
        audio = root / "Contents" / "a.wav"
        for p in (dat, audio):
            p.parent.mkdir(parents=True)
            p.write_bytes(b"first")
        man = manifest.Manifest(str(root))
        man.add(str(dat))
        man.add(str(audio))
        man.save()
        for p in (dat, audio):
            p.write_bytes(b"other")  # same size
        both = frozenset({"PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT", "Contents/a.wav"})
        chk = manifest.verify(str(root), lenient=both)
        self.assertEqual(chk.modified, ["PIONEER/USBANLZ/P001/00000001/ANLZ0000.DAT"])
        self.assertEqual(chk.damaged, ["Contents/a.wav"])  # audio is never forgiven
        self.assertEqual(manifest.verify(str(root)).modified, [])

    def test_keep_carries_the_old_hash_and_notes_the_file(self):
        dir_, root = tmp()
        self.addCleanup(dir_.cleanup)
        f = root / "ANLZ0000.DAT"
        f.write_bytes(b"data")
        first = manifest.Manifest(str(root))
        first.add(str(f))
        first.save()
        again = manifest.Manifest(str(root))
        again.keep(str(f))
        self.assertEqual(again.carried, {"ANLZ0000.DAT"})
        g = root / "ANLZ0000.EXT"
        g.write_bytes(b"new file")
        again.keep(str(g))  # not in the old list: read, and not noted
        self.assertEqual(again.carried, {"ANLZ0000.DAT"})
        self.assertEqual(again.files["ANLZ0000.EXT"]["sha256"], manifest.sha256_file(str(g)))


# ------------------------------------------------------------
class LeftOutTests(unittest.TestCase):
    def setUp(self):
        self.dir, self.root = tmp()
        self.addCleanup(self.dir.cleanup)
        self.music, self.cache, self.out = self.root / "music", self.root / "cache", self.root / "key"
        self.music.mkdir()
        self.cache.mkdir()
        self.here = make_track(self.music, "here", 1, artist="Here")
        self.gone = [Track(title=f"gone{i}", artist="Gone", path=str(self.root / "nowhere" / f"gone{i}.mp3")) for i in range(2)]

    def names(self):
        return {p.name: [t.title for t in p.tracks] for p in playlists(key.load(str(self.out)))}

    def test_a_playlist_without_any_file_is_not_written(self):
        rep = export_to([Node("playlist", "Gone", tracks=self.gone), Node("playlist", "Here", tracks=[self.here])],
                        self.out, self.cache)
        self.assertEqual(rep.left_out, ["Gone"])
        self.assertEqual(self.names(), {"Here": ["here"]})
        self.assertEqual(sorted(rep.missing), sorted(t.path for t in self.gone))

    def test_a_playlist_with_one_file_left_is_written_with_it(self):
        rep = export_to([Node("playlist", "Mixed", tracks=[self.gone[0], self.here])], self.out, self.cache)
        self.assertEqual((rep.left_out, self.names()), ([], {"Mixed": ["here"]}))
        self.assertEqual(rep.missing, [self.gone[0].path])

    def test_the_playlist_already_on_the_key_stays_when_its_new_version_has_no_file(self):
        export_to([Node("playlist", "Set", tracks=[self.here])], self.out, self.cache)
        rep = export_to([Node("playlist", "Set", tracks=self.gone)], self.out, self.cache)
        self.assertEqual((rep.left_out, rep.replaced), (["Set"], []))
        self.assertEqual(self.names(), {"Set": ["here"]})

    def test_a_file_in_a_playlist_left_out_and_in_another_is_counted_once(self):
        mixed = Node("playlist", "Mixed", tracks=[self.gone[0], self.here])
        rep = export_to([Node("playlist", "Gone", tracks=self.gone), mixed], self.out, self.cache)
        self.assertEqual(sorted(rep.missing), sorted(t.path for t in self.gone))

    def test_the_count_of_tracks_exported_does_not_take_the_missing_off_twice(self):
        rep = export_to([Node("playlist", "Gone", tracks=self.gone), Node("playlist", "Here", tracks=[self.here])],
                        self.out, self.cache)
        self.assertEqual(rep.tracks - len(rep.missing), 1)

    def test_an_empty_playlist_is_not_a_playlist_without_files(self):
        rep = export_to([Node("playlist", "Empty", tracks=[]), Node("playlist", "Here", tracks=[self.here])],
                        self.out, self.cache)
        self.assertEqual(rep.left_out, [])

    def test_every_playlist_without_files_writes_nothing(self):
        rep = export_to([Node("playlist", "Gone", tracks=self.gone)], self.out, self.cache)
        self.assertEqual(rep.left_out, ["Gone"])
        self.assertFalse((self.out / "PIONEER" / "rekordbox" / "export.pdb").exists())

    def test_a_folder_that_only_held_such_a_playlist_goes_too(self):
        tree = [Node("folder", "Crate", children=[Node("playlist", "Gone", tracks=self.gone)]),
                Node("playlist", "Here", tracks=[self.here])]
        rep = export_to(tree, self.out, self.cache)
        self.assertEqual((rep.left_out, self.names()), (["Gone"], {"Here": ["here"]}))


# ------------------------------------------------------------
class EndMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.log = LogWindow()
        self.main = MainWindow(dict(settings.DEFAULTS, auto_load=False, confirm_exit=False), self.log)

    def tearDown(self):
        self.main.set_dirty(False)
        with patch.object(settings, "save"):
            self.main.close()
        self.main.engine.deleteLater()
        self.main.deleteLater()
        self.log.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def finish(self, rep):
        with patch.object(self.main, "probe_key"), patch.object(self.main, "offer_eject"), \
                patch.object(QMessageBox, "warning") as warning, patch.object(QMessageBox, "information") as info:
            self.main.exported(rep)
        return warning, info

    def test_a_player_change_is_told_as_information_not_as_a_warning(self):
        rep = Report(tracks=5, path="I:\\", manifest="m", player_changed=["a", "b"])
        warning, info = self.finish(rep)
        warning.assert_not_called()
        text = info.call_args.args[2]
        self.assertIn("2 analysis files kept from the earlier export were changed since", text)
        self.assertNotIn("Export again", text)

    def test_a_playlist_left_out_is_named_in_a_warning(self):
        rep = Report(tracks=5, path="I:\\", manifest="m", left_out=["Eric", "Other"], missing=["x.mp3"])
        warning, _info = self.finish(rep)
        self.assertIn("Not written, none of their files was found: Eric, Other", warning.call_args.args[2])

    def test_a_clean_export_says_neither(self):
        _warning, info = self.finish(Report(tracks=5, path="I:\\", manifest="m"))
        text = info.call_args.args[2]
        self.assertNotIn("Not written", text)
        self.assertNotIn("changed since", text)


if __name__ == "__main__":
    unittest.main()
