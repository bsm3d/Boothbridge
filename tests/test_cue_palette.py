# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Checks that hot cue colours match the RGB palette rekordbox writes for a CDJ key.

import unittest

from djapp.export.cdj import anlz, anlzread
from djapp.model import Cue, Track


# --------------------------------------------------------
class DevicePaletteTests(unittest.TestCase):
    def test_the_palette_is_the_one_rekordbox_writes(self):
        # rbxport docs/reference/usb-export-db.md, Cue record tables
        self.assertEqual(len(anlz.DEVICE_PALETTE), 65)
        doc = {0: 0x000000, 1: 0x0000FF, 9: 0x00E0FF, 21: 0x00FF00, 22: 0x1AFF00, 31: 0xFFFF00, 42: 0xFF0000,
               46: 0xFF005C, 53: 0xFF00FF, 62: 0x1A00FF, 63: 0x000000, 64: 0xFFFFFF}
        for code, rgb in doc.items():
            self.assertEqual(anlz.DEVICE_PALETTE[code], rgb, code)

    def test_a_shown_colour_gets_its_code_and_the_written_rgb(self):
        for code, shown in enumerate(anlz.HOT_PALETTE, 1):
            with self.subTest(code=code):
                cue = Cue("A", start=83032, hotcue=0, color=f"#{shown:06X}")
                self.assertEqual(anlz.hot_color(cue), (code, anlz.DEVICE_PALETTE[code]))
        # the 88 byte example of the document: code 46 then FF005C
        entry = anlz._extended_cue_entry(Cue("", start=83032, hotcue=0, color="#F51E8C"))
        self.assertEqual(entry[-4:], bytes.fromhex("2EFF005C"))

    def test_a_colour_read_back_from_a_key_finds_its_code_again(self):
        # one tie: E600FF is the shown colour of 53 and the written colour of 54, the shown one wins
        self.assertEqual(anlz.HOT_PALETTE[52], anlz.DEVICE_PALETTE[54])
        for code in range(1, 63):
            if code == 54:
                continue
            with self.subTest(code=code):
                cue = Cue("A", start=1000, hotcue=3, color=f"#{anlz.DEVICE_PALETTE[code]:06X}")
                self.assertEqual(anlz.hot_color(cue)[0], code)

    def test_the_colour_id_before_the_name_is_as_before(self):
        # 0x1c is chosen from the shown colour, as it was before
        color_id, word = anlz.cue_color(Cue("A", start=1000, hotcue=0))
        self.assertEqual((color_id, word >> 24, word & 0xFFFFFF), (5, 22, 0x1AFF00))

    def test_a_key_written_then_read_keeps_the_code(self):
        cue = Cue("A", start=1000, hotcue=2, color="#E62828")
        ext = anlz.pmai(anlz.cue_lists(Track(bpm=120, duration=60, cues=[cue]), True))
        read = anlzread.parse(ext).cues[0]
        self.assertEqual(read.color, "#FF0000")
        again = Cue("A", start=1000, hotcue=2, color=read.color)
        self.assertEqual(anlz.hot_color(again), (42, 0xFF0000))


if __name__ == "__main__":
    unittest.main()
