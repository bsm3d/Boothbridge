# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Compares Boothbridge's export.pdb with one rekordbox wrote for the same three tracks, layout and live rows.

"""The fixture is the export.pdb rekordbox 6 wrote for three tracks (no audio in the
repo). Its content is read back, fed to our writer, and both files must agree on
layout and on every live row, except what rekordbox computes on its own side."""

import struct
from pathlib import Path

import pytest

from djapp import keys
from djapp.export.cdj import pdbwrite as P
from djapp.export.cdj.reference import PAGES
from djapp.export.cdj.devicesql import HEAP, dstr, index_size
from djapp.export.cdj.pdbread import dstr as rstr, read, rows, track_strings
from djapp.model import Node, Track

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "test_b_export.pdb"

# the file is written by rekordbox, so it is not in the repository
pytestmark = pytest.mark.skipif(not FIX.is_file(), reason="no rekordbox export.pdb fixture")

def names(pdb, kind, name_at):
    return {struct.unpack_from("<I", r, id_at)[0]: rstr(r, off(r))
            for r in rows(pdb, kind) for id_at, off in [name_at]}

@pytest.fixture(scope="module")
def ref():
    return read(FIX.read_bytes())

@pytest.fixture(scope="module")
def ours(ref, tmp_path_factory):
    artists = names(ref, P.ARTISTS, (4, lambda r: r[9]))
    albums = {struct.unpack_from("<I", r, 12)[0]: (rstr(r, r[21]), struct.unpack_from("<I", r, 8)[0])
              for r in rows(ref, P.ALBUMS)}
    genres = names(ref, P.GENRES, (0, lambda r: 4))
    keyn = names(ref, P.KEYS, (0, lambda r: 8))

    items = []
    for r in rows(ref, P.TRACKS):
        s = track_strings(r)
        f = struct.unpack_from
        alb = albums.get(f("<I", r, 0x40)[0], ("", 0))
        t = Track(title=s[17], comment=s[16], artist=artists.get(f("<I", r, 0x44)[0], ""),
                  album=alb[0], album_artist=artists.get(alb[1], ""),
                  genre=genres.get(f("<I", r, 0x3c)[0], ""), added=s[10],
                  samplerate=f("<I", r, 0x08)[0], bitrate=f("<I", r, 0x30)[0],
                  track_no=f("<I", r, 0x34)[0], bpm=f("<I", r, 0x38)[0] / 100,
                  disc_no=f("<H", r, 0x4c)[0], plays=f("<H", r, 0x4e)[0], year=f("<H", r, 0x50)[0],
                  duration=f("<H", r, 0x54)[0], color=r[0x58], rating=r[0x59],
                  key=keys.parse(keyn.get(f("<I", r, 0x20)[0], "")), path=s[20])
        items.append(P.Item(t, f("<I", r, 0x48)[0], s[20], s[14], f("<I", r, 0x10)[0],
                            art=f("<I", r, 0x1c)[0]))
    items.sort(key=lambda it: it.id)

    ids = {it.id: it.track for it in items}
    ents = sorted(rows(ref, P.PL_ENTRIES), key=lambda r: r[:4])
    pl = Node("playlist", rstr(rows(ref, P.PL_TREE)[0], 20),
              tracks=[ids[struct.unpack_from("<I", e, 4)[0]] for e in ents])

    out = tmp_path_factory.mktemp("pdb") / "export.pdb"
    # analyze date = day of the export
    raw = FIX.read_bytes()
    static = {k: raw[ref.pages[k][0].idx * 4096:(ref.pages[k][0].idx + 1) * 4096] for k in PAGES}
    P.write(items, [pl], out, today=track_strings(rows(ref, P.TRACKS)[0])[15], static=static)
    return read(out.read_bytes())

# ============================================================
# Layout
# ============================================================

def test_header(ref, ours):
    assert ours.next_unused == ref.next_unused
    assert ours.seq > max(pg.seq for pl in ours.pages.values() for pg in pl)
    for k, (ec, first, last) in ref.tables.items():
        assert ours.tables[k][1:] == (first, last), k

def test_page_headers(ref, ours):
    for k, pages in ref.pages.items():
        assert len(ours.pages[k]) == len(pages), k
        for a, b in zip(pages, ours.pages[k]):
            assert (b.idx, b.kind, b.flags) == (a.idx, a.kind, a.flags), k
            # history is copied as is, with the deleted rows rekordbox leaves behind
            if k != P.HISTORY:
                assert b.free + b.used + index_size(len(b.rows)) == HEAP

# ============================================================
# Rows
# ============================================================

def content(r: bytes) -> bytes:
    return r.rstrip(b"\x00")

@pytest.mark.parametrize("kind", [P.GENRES, P.ARTISTS, P.ALBUMS, P.KEYS, P.COLORS,
                                  P.PL_TREE, P.PL_ENTRIES, P.ARTWORK, P.COLUMNS, P.T17, P.T18])
def test_rows(ref, ours, kind):
    a = sorted(content(r) for r in rows(ref, kind))
    b = sorted(content(r) for r in rows(ours, kind))
    assert a == b

def test_tracks(ref, ours):
    a = {struct.unpack_from("<I", r, 0x48)[0]: bytearray(r) for r in rows(ref, P.TRACKS)}
    b = {struct.unpack_from("<I", r, 0x48)[0]: bytearray(r) for r in rows(ours, P.TRACKS)}
    assert a.keys() == b.keys()
    for tid in a:
        ra, rb = a[tid], b[tid]
        # index_shift follows the slot, rekordbox keeps stale copies so slots differ
        # 0x14 is a rekordbox internal checksum, the players ignore it
        for r in (ra, rb):
            r[2:4] = b"\0\0"
            r[0x14:0x18] = bytes(4)
        assert track_strings(ra) == track_strings(rb)
        assert content(ra) == content(rb), tid

def test_long_strings_aligned(ours):
    for r in rows(ours, P.TRACKS):
        for o in struct.unpack_from("<21H", r, 0x5e):
            if r[o] in (0x40, 0x90):
                assert o % 4 == 0

def test_dstr():
    assert dstr("") == b"\x03"
    assert dstr("ON") == b"\x07ON"
    assert dstr("Röyksopp")[:4] == b"\x90\x14\x00\x00"
    assert dstr("x" * 127)[:4] == b"\x40\x83\x00\x00"
