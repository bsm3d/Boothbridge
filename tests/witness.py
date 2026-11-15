# The independent witness of the tag tests: a tag library that the program neither uses nor ships.
# It only reads back what the program's own tag code wrote. Without it the tests that need it are skipped
# (pip install -r tests/requirements.txt).
NAME = "mutagen"

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, GEOB, ID3, TALB, TBPM, TDRC, TIT2, TKEY, TPE1, TPOS, TRCK, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

__all__ = [
    "APIC", "COMM", "FLAC", "GEOB", "ID3", "MP3", "MP4", "MP4Cover", "MP4FreeForm", "NAME", "OggVorbis", "Picture",
    "TALB", "TBPM", "TDRC", "TIT2", "TKEY", "TPE1", "TPOS", "TRCK", "TXXX", "WAVE",
]
