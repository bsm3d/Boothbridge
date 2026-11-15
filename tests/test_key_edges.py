# two odd keys the reader meets: a PCO2 section with no cue next to a PCOB that has some, and a
# track id that appears twice in the database

import unittest
from unittest.mock import patch

from djapp.export.cdj import anlzread, pdbread
from djapp.model import Cue, Node, playlists
from djapp.sources import key

from tests.test_key_support import export_to, make_track, tmp


def cue(start, slot=1):
    return anlzread.AnlzCue(slot, False, start)

# ----------------------------------------------------------------
class CombineTests(unittest.TestCase):
    def test_an_empty_pco2_does_not_hide_the_cues_of_the_dat(self):
        dat = anlzread.Anlz(path="/Contents/a.flac", cues=[cue(1000)])
        ext = anlzread.Anlz(path="/Contents/a.flac", cues=[], detailed=True)
        self.assertEqual([c.start for c in anlzread.combine(dat, ext).cues], [1000])

    def test_a_pco2_with_cues_wins_as_before(self):
        dat = anlzread.Anlz(cues=[cue(1000)])
        ext = anlzread.Anlz(cues=[cue(2000)], detailed=True)
        both = anlzread.combine(dat, ext)
        self.assertEqual([c.start for c in both.cues], [2000])
        self.assertTrue(both.detailed)

    def test_no_cue_anywhere_is_no_cue(self):
        out = anlzread.combine(anlzread.Anlz(), anlzread.Anlz(detailed=True))
        self.assertEqual(out.cues, [])

# ----------------------------------------------------------------
class DuplicateIdTests(unittest.TestCase):
    def test_a_track_id_that_appears_twice_is_said_and_the_first_row_is_used(self):
        t, root = tmp()
        self.addCleanup(t.cleanup)
        music, cache, out = root / "music", root / "cache", root / "key"
        music.mkdir()
        cache.mkdir()
        tracks = [make_track(music, f"t{i}", i + 1, cues=[Cue(name="A", start=500.0, hotcue=0)]) for i in range(3)]
        export_to([Node("playlist", "Set", tracks=tracks)], out, cache)
        clean = key.read(str(out))
        self.assertEqual(clean.warnings, [])
        real = pdbread.rows

        def doubled(pdb, kind):
            rows = real(pdb, kind)
            return rows + rows if kind == pdbread.TRACKS else rows

        with patch.object(pdbread, "rows", doubled):
            data = key.read(str(out))
        self.assertEqual(len(data.warnings), 1)
        self.assertIn("appear twice", data.warnings[0])
        self.assertIn(": 3", data.warnings[0])
        self.assertEqual([t.title for pl in playlists(data.nodes) for t in pl.tracks],
                         [t.title for pl in playlists(clean.nodes) for t in pl.tracks])


if __name__ == "__main__":
    unittest.main()
