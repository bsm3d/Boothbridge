# Benoit Saint-Moulin
# Boothbridge : ANLZ files against the ones rekordbox wrote for the same tracks

"""The comparisons with a real export need the Test_B export of rekordbox 6 with its audio, which
stays out of the repo. Point BB_ORACLE to its root, they are skipped when it is missing. Other exports
(the Golden Valide key, for one) differ in tempo rounding and mp3 padding, and fail them. The cue tests
at the end need no export."""

import os
import struct
from pathlib import Path

import pytest

from djapp.export.cdj import anlz
from djapp.analysis import Audio
from djapp.model import Cue, LOOP, Track

ROOT = Path(os.environ.get("BB_ORACLE", "no-reference-export"))



def sections(data: bytes) -> list[tuple[bytes, bytes]]:
    out, o = [], struct.unpack_from(">I", data, 4)[0]
    assert struct.unpack_from(">I", data, 8)[0] == len(data)
    while o < len(data):
        lh, lt = struct.unpack_from(">II", data, o + 4)
        out.append((data[o:o + 4], data[o:o + lt]))
        o += lt
    assert o == len(data)
    return out


def refs():
    for dat in sorted((ROOT / "PIONEER" / "USBANLZ").glob("*/*/ANLZ0000.DAT")):
        secs = dict(sections(dat.read_bytes()))
        path = secs[b"PPTH"][16:-2].decode("utf-16-be")
        yield dat, path


@pytest.fixture(scope="module", params=list(refs()) if (ROOT / "PIONEER" / "USBANLZ").is_dir() else [None], ids=lambda p: Path(p[1]).name if p else "none")
def pair(request):
    if request.param is None:
        pytest.skip("no reference export, point BB_ORACLE to one")
    dat, path = request.param
    ref_dat = dict(sections(dat.read_bytes()))
    ref_ext = sections(dat.with_suffix(".EXT").read_bytes())
    q = ref_dat[b"PQTZ"]
    first_n, tempo, first_ms = struct.unpack_from(">HHI", q, 24)
    t = Track(bpm=tempo / 100, grid=float(first_ms))
    audio = Audio(str(ROOT / path.lstrip("/")))
    t.duration = audio.seconds
    ours_dat, ours_ext = anlz.build(t, path, audio)
    return path, dat, ref_dat, ref_ext, dict(sections(ours_dat)), sections(ours_ext)


def test_folder(pair):
    path, dat = pair[0], pair[1]
    assert anlz.anlz_dir(path) == "/" + dat.parent.relative_to(ROOT).as_posix()


def test_dat_layout(pair):
    _, _, ref, _, ours, _ = pair
    assert list(ours) == list(ref)
    for name in (b"PPTH", b"PWAV", b"PWV2"):
        assert ours[name][:24] == ref[name][:24] or name != b"PPTH"
        assert len(ours[name]) == len(ref[name])
    assert ours[b"PPTH"] == ref[b"PPTH"]
    assert ours[b"PCOB"][:24] == ref[b"PCOB"][:24]


def test_ext_layout(pair):
    ref = [n for n, _ in pair[3] if n not in (b"PQT2", b"PSSI")]
    assert [n for n, _ in pair[5]] == ref


def test_frames_and_detail(pair):
    _, _, ref, ref_ext, ours, ours_ext = pair
    rf = struct.unpack_from(">I", ref[b"PVBR"], len(ref[b"PVBR"]) - 4)[0]
    of = struct.unpack_from(">I", ours[b"PVBR"], len(ours[b"PVBR"]) - 4)[0]
    # decoders disagree on the mp3 padding by a frame or two
    assert abs(rf - of) < 44100 * 0.1
    r5 = dict(ref_ext)[b"PWV5"]
    o5 = dict(ours_ext)[b"PWV5"]
    assert o5[12:16] == r5[12:16]
    assert abs(struct.unpack_from(">I", o5, 16)[0] - struct.unpack_from(">I", r5, 16)[0]) <= 15


def test_beats(pair):
    _, _, ref, _, ours, _ = pair
    n_ref = struct.unpack_from(">I", ref[b"PQTZ"], 20)[0]
    n_ours = struct.unpack_from(">I", ours[b"PQTZ"], 20)[0]
    assert abs(n_ref - n_ours) <= 1
    for i in range(min(n_ref, n_ours)):
        a = struct.unpack_from(">HHI", ref[b"PQTZ"], 24 + 8 * i)
        b = struct.unpack_from(">HHI", ours[b"PQTZ"], 24 + 8 * i)
        assert a[:2] == b[:2]
        # the stored tempo is rounded to 0.01 bpm, rekordbox places beats with the exact one
        assert abs(a[2] - b[2]) <= 5, i


def test_cues_roundtrip():
    t = Track(bpm=120, duration=60, cues=[Cue("A", start=1000, hotcue=0),
                                          Cue("drop", LOOP, 5000, 2000, hotcue=3),
                                          Cue("", start=250)])
    lists = [s for n, s in sections(anlz.pmai(anlz.cue_lists(t, False)))]
    hot, mem = lists
    assert struct.unpack_from(">IHH", hot, 12) == (1, 0, 2)
    assert struct.unpack_from(">I", hot, 24 + 12)[0] == 1          # slot A
    assert struct.unpack_from(">I", hot, 24 + 56 + 12)[0] == 4     # slot D
    assert hot[24 + 56 + 0x1c] == 2
    assert struct.unpack_from(">II", hot, 24 + 56 + 0x20) == (5000, 7000)
    assert struct.unpack_from(">I", mem, 24 + 12)[0] == 0


def test_hot_cue_colours():
    t = Track(bpm=120, duration=60, cues=[Cue("A", start=1000, hotcue=0),
                                          Cue("loop", LOOP, 5000, 2000, hotcue=1),
                                          Cue("sg", start=2000, hotcue=2, color="#FE0101"),
                                          Cue("", start=250)])
    hot, mem = [s for n, s in sections(anlz.pmai(anlz.cue_lists(t, True)))]
    rows = []
    off = 20
    for _ in range(3):
        size = struct.unpack_from(">I", hot, off + 8)[0]
        rows.append((hot[off + 28], hot[off + size - 3:off + size]))
        off += size
    # after the code rekordbox writes the RGB of its device palette, not the colour it shows for
    # the code (rbxport docs, Cue record tables): green 28E214 is code 22, written 1AFF00
    assert rows[0] == (5, bytes.fromhex("1AFF00"))      # slot A: its green, code 22
    assert rows[1] == (7, bytes.fromhex("0000FF"))      # slot B: its blue, code 1
    # source colour FE0101: nearest is code 42, the red rekordbox writes FF0000 (shown E62828)
    assert anlz.hot_color(t.cues[2]) == (42, 0xFF0000)
    assert rows[2] == (2, bytes.fromhex("FF0000"))
    assert mem[20 + 28] == 0                          # memory cue: none


# a source (NML, Mixxx) can hand over a slot past H: the player has eight pads and the key
# reader drops anything above slot 8, so the writer leaves those cues out of the hot list
def test_hot_cue_slots_past_h_are_not_written():
    t = Track(bpm=120, duration=60, cues=[Cue("I", start=500, hotcue=8), Cue("far", start=600, hotcue=2**32),
                                          Cue("A", start=1000, hotcue=0), Cue("", start=250)])
    for ext in (False, True):
        hot, mem = [s for n, s in sections(anlz.pmai(anlz.cue_lists(t, ext)))]
        count_at = 16 if ext else 18
        assert struct.unpack_from(">H", hot, count_at)[0] == 1
        assert struct.unpack_from(">I", hot, (20 if ext else 24) + 12)[0] == 1   # slot A only
        assert struct.unpack_from(">H", mem, count_at)[0] == 1
    assert [c.hotcue for c in anlz.hot_cues(t)] == [0]
