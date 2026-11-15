# regressions found by the review before the release build: a hostile cover that aborted a whole export, an
# export that waited for every queued analysis after a failed write, readers that raised the wrong error on
# a damaged iTunes or Engine file, and the help page whose markup was broken

import io
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from djapp import export
from djapp.export.cdj import usb
from djapp.model import Node
from djapp.sources import engine, itunes
from djapp.ui.dialogs import USAGE
from PIL import Image

from tests.test_engine import make_library
from tests.test_key_support import cfg, inline, make_track, tmp


# ------------------------------------------------
class CoverTests(unittest.TestCase):
    def test_a_cover_that_declares_an_absurd_size_is_skipped_not_fatal(self):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16)).save(buffer, "JPEG")
        data = bytearray(buffer.getvalue())
        at = data.index(b"\xff\xc0")
        data[at + 5 : at + 9] = struct.pack(">HH", 30000, 30000)
        self.assertIsNone(usb._decode_picture(bytes(data), "cover.mp3"))


# ------------------------------------------------
class FailedWriteTests(unittest.TestCase):
    def test_a_failed_write_does_not_wait_for_every_queued_analysis(self):
        temp, root = tmp()
        self.addCleanup(temp.cleanup)
        music, cache = root / "music", root / "cache"
        music.mkdir()
        cache.mkdir()
        tracks = [make_track(music, f"t{i}", i + 1) for i in range(8)]
        ran = []
        lock = threading.Lock()
        real = usb.anlz_job

        def slow(track, path):
            with lock:
                ran.append(path)
            time.sleep(0.3)
            return real(track, path)

        def full(*args, **kwargs):
            raise OSError(28, "No space left on device (injected)")

        with (
            inline(cache),
            patch.object(usb, "anlz_job", slow),
            patch.object(usb, "put_anlz", full),
            self.assertRaises(OSError),
        ):
            export.run(
                "CDJ/USB",
                [Node("playlist", "P", tracks=tracks)],
                str(root / "key"),
                cfg(anlz_processes=1),
            )
        self.assertLess(len(ran), 5)


# ------------------------------------------------
HEAD = '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Application Version</key><string>1</string>'
TRACK = (
    "<dict><key>1</key><dict><key>Track ID</key><integer>1</integer><key>Location</key>"
    "<string>file:///C:/a.mp3</string>{}</dict></dict>"
)


class ITunesHostileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(self, tracks="<dict/>", playlists="<array/>"):
        text = f"{HEAD}<key>Tracks</key>{tracks}<key>Playlists</key>{playlists}</dict></plist>"
        path = Path(self.temp.name) / "Library.xml"
        path.write_text(text, encoding="utf-8")
        return itunes.load(str(path))

    def test_an_integer_with_two_minus_signs_or_a_superscript_digit_is_zero(self):
        for text in ("--5", "\u00b2", "5-", "1_0_"):
            nodes = self.load(TRACK.format(f"<key>Year</key><integer>{text}</integer>"))
            self.assertIn(nodes[-1].tracks[0].year, (0, 10), text)

    def test_tracks_that_are_not_a_dictionary_make_a_refused_library(self):
        for value in ("<array/>", "<true/>", "<string>x</string>"):
            with self.assertRaises(ValueError):
                self.load(value)

    def test_playlists_and_items_of_the_wrong_kind_are_ignored(self):
        self.assertEqual(self.load(TRACK.format(""), "<true/>")[-1].name, "All tracks")
        entry = (
            "<array><dict><key>Playlist Persistent ID</key><string>A</string><key>Name</key><string>n</string>"
            "<key>Playlist Items</key>{}</dict></array>"
        )
        for items in ("<true/>", "<data>QQ==</data>", "<integer>3</integer>"):
            nodes = self.load(TRACK.format(""), entry.format(items))
            self.assertEqual([n.name for n in nodes], ["All tracks"])


# ------------------------------------------------
class EngineHostileTests(unittest.TestCase):
    def test_an_origin_track_id_that_is_not_a_whole_number_does_not_stop_the_read(self):
        for value in (float("inf"), float("nan"), "abc", b"\x00\x01"):
            with tempfile.TemporaryDirectory() as folder:
                db = make_library(Path(folder))
                con = sqlite3.connect(db)
                con.execute("UPDATE Track SET originTrackId = ? WHERE id = 1", (value,))
                con.commit()
                con.close()
                nodes = engine.load(str(db))
                self.assertTrue(any(n.name == "All tracks" for n in nodes), repr(value))


# ------------------------------------------------
class UsageMarkupTests(unittest.TestCase):
    def test_every_tag_of_the_help_is_closed_and_no_markdown_is_left(self):
        problems, stack = [], []

        class Check(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag not in ("br", "hr", "img"):
                    stack.append(tag)

            def handle_endtag(self, tag):
                if stack and stack[-1] == tag:
                    stack.pop()
                else:
                    problems.append((tag, self.getpos()))

        Check().feed(USAGE)
        self.assertEqual(problems, [])
        self.assertEqual(stack, [])
        self.assertNotIn("**", USAGE)

    def test_no_sentence_of_the_help_is_cut_between_a_list_and_a_paragraph(self):
        for start in (
            "</li>\n</ul>\n<p>name in",
            "</li>\n</ul>\n<p>unsupported",
            "</li>\n</ul>\n<p>writes FAT32",
            "</li>\n</ul>\n<p>time.",
            "</li>\n</ul>\n<p>default)",
            "</li>\n</ul>\n<p>as damaged",
            "</li>\n</ul>\n<p>not updated",
        ):
            self.assertNotIn(start, USAGE)


if __name__ == "__main__":
    unittest.main()
