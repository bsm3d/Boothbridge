# explicit metadata writes: keep unrelated tags and audio, back up, then replace atomically

from __future__ import annotations

import math
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import keys
from .export.manifest import Cancelled
from .tagfile import TagError, Tagged, open_tagged

TEXT_FIELDS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "genre",
    "label",
    "remixer",
    "composer",
    "comment",
)
NUMBER_FIELDS = ("year", "track_no", "disc_no")
FILE_FIELDS = (*TEXT_FIELDS, *NUMBER_FIELDS, "bpm", "key")
EXTENSIONS = (".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".alac", ".ogg")


@dataclass(frozen=True)
class WriteResult:
    path: str
    backup: str
    size: int


# ------------------------------------------------------------------------
def validate(values: dict[str, str]):
    for field, value in values.items():
        if field not in FILE_FIELDS:
            raise ValueError(f"Unsupported audio tag: {field}")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"Invalid text in {field}")
        try:
            value.encode("utf-8")
        # half a surrogate pair from a damaged file name
        except UnicodeEncodeError as e:
            raise ValueError(f"Invalid text in {field}") from e
        if (
            field in NUMBER_FIELDS
            and value
            and (not value.isdecimal() or not 0 < int(value) <= 65535)
        ):
            raise ValueError(
                f"{field} must be a whole number from 1 to 65535, or empty"
            )
        if field == "bpm" and value:
            try:
                bpm = float(value)
            except ValueError as e:
                raise ValueError("BPM must be a positive number, or empty") from e
            if not math.isfinite(bpm) or not 0 < bpm <= 1000:
                raise ValueError("BPM must be from 0 to 1000, or empty")
        if field == "key" and value and keys.parse(value) is None:
            raise ValueError("Invalid musical key")


# ------------------------------------------------------------
def open_audio(path: str) -> Tagged:
    return open_tagged(path)


def set_tags(audio: Tagged, values: dict[str, str]):
    for field, value in values.items():
        audio.set(field, value)


# whether the file already says what the editor is about to write. The editor sends every field
# each time, and rewriting the ones that did not change would cut a date "2024-05-17" down to its
# year, drop the "/12" of a track number or reformat a key
def same(field: str, old: str, new: str) -> bool:
    old, new = str(old).strip(), str(new).strip()
    if field == "year":
        return old[:4] == new or old == new
    if field in ("track_no", "disc_no"):
        return old.partition("/")[0].strip() == new
    if field == "bpm" and old and new:
        try:
            return abs(float(old) - float(new)) < 0.0005
        except ValueError:
            return False
    if field == "key" and old and new:
        a, b = keys.parse(old), keys.parse(new)
        return a is not None and a == b
    return old == new


def get_tags(audio: Tagged, fields) -> dict[str, str]:
    return {field: audio.get(field) for field in fields}


def fingerprint(path: Path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino


# ------------------------------------------------------------
# validate on a copy, retain the first .bb-tags.bak, never mutate the source in place
def write(path: str, values: dict[str, str], progress=None, cancel=None) -> WriteResult:
    validate(values)
    source = Path(path)
    if source.suffix.lower() not in EXTENSIONS:
        raise ValueError("Writing tags is not supported for this audio format")
    if source.is_symlink() or not source.is_file():
        raise ValueError("Choose an existing regular audio file, not a symbolic link")
    source = source.absolute()
    if not source.stat().st_mode & stat.S_IWRITE:
        raise PermissionError("The source audio is read-only; tags were not written")
    initial = fingerprint(source)
    backup = source.with_name(source.name + ".bb-tags.bak")
    cb = progress or (lambda _pct, _msg: None)

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if fingerprint(source) != initial:
            raise OSError(
                "The source audio changed during tag writing; reload it and try again"
            )

    fd, name = tempfile.mkstemp(
        prefix=".bb-tags-", suffix=source.suffix, dir=source.parent
    )
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            copied = 0
            while block := src.read(4 << 20):
                check()
                dst.write(block)
                copied += len(block)
                cb(min(60, copied * 60 // max(1, initial[0])), "Preparing audio copy")
            dst.flush()
            os.fsync(dst.fileno())
        check()
        audio = open_audio(str(temp))
        current = get_tags(audio, values)
        values = {f: v for f, v in values.items() if not same(f, current.get(f, ""), v)}
        if not values:
            cb(100, "The file already holds these tags")
            return WriteResult(
                str(source),
                str(backup) if backup.is_file() else "",
                source.stat().st_size,
            )
        set_tags(audio, values)
        audio.save()
        readback = get_tags(open_audio(str(temp)), values)
        if any(not same(f, readback.get(f, ""), v) for f, v in values.items()):
            raise ValueError(
                "Audio tags could not be verified; the original file was left unchanged"
            )
        cb(75, "Tags verified; creating backup")
        check()
        if backup.exists() or backup.is_symlink():
            if backup.is_symlink() or not backup.is_file():
                raise OSError("The backup path is not a regular file")
        else:
            # whole or not there: the copy is made beside it and takes its name when it is complete
            partial = backup.with_name(backup.name + ".part")
            try:
                with partial.open("wb") as dst, source.open("rb") as src:
                    while block := src.read(4 << 20):
                        check()
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(partial, backup)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        check()
        with temp.open("r+b") as stream:
            os.fsync(stream.fileno())
        shutil.copymode(source, temp)
        check()
        os.replace(temp, source)
        cb(100, "Audio tags written")
        return WriteResult(str(source), str(backup), source.stat().st_size)
    except TagError as e:
        raise ValueError(f"Cannot write tags to this audio file: {e}") from e
    finally:
        if temp.exists():
            temp.chmod(stat.S_IREAD | stat.S_IWRITE)
            temp.unlink()
