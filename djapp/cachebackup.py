# Benoit Saint-Moulin
# Boothbridge : back up and restore the analysis cache (ANLZ files and waveforms)

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import VERSION
from .backup import MAX_RATIO
from .backup import verify as check_sidecar
from .export.cdj.usb import ANLZ_VERSION
from .export.files import Cancelled
from .export.manifest import write_atomic

MANIFEST = "cache_manifest.json"
FORMAT = "boothbridge-cache"

# What the exporter (usb.py, <key>.DAT and .EXT) and waveform.py (<key>.bin and .hi1000) write.
# Anything else in these folders is not ours and is neither saved nor deleted.
LAYOUT = {
    "anlz": re.compile(r"[0-9a-f]{40}\.(?:DAT|EXT)"),
    "wave": re.compile(r"[0-9a-f]{40}\.(?:bin|hi1000)"),
}

# A real cache file is a few MB at most, a 10 hour mix is about 100 MB in the zoomed waveform.
MAX_FILES = 200_000
MAX_FILE = 512 << 20
MAX_TOTAL = 64 << 30
MAX_DIRECTORY = 64 << 20
# small files compress very well (zero padding), only a big one with a huge ratio is a bomb
BIG = 1 << 20
SPARE = 256 << 20
CHUNK = 1 << 20

DAMAGED = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError)
UNREADABLE = DAMAGED + (OSError,)


# ----------------------------------------------------------------
# whether an error means the archive is damaged. zipfile seeks to the offset a header gives, a
# wrong one comes out as OSError EINVAL, any other OSError is the drive and is not ours to name
def is_damage(error: BaseException) -> bool:
    return not isinstance(error, OSError) or error.errno == errno.EINVAL


# ----------------------------------------------------------------
@dataclass
class Result:
    path: str = ""
    files: int = 0
    size: int = 0
    present: int = 0  # restore: already in the cache
    skipped: int = 0  # restore: not restored on purpose
    # backup: files that could not be read
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sha256: str = ""
    sidecar: bool = False


# ----------------------------------------------------------------
def shown(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "?", text)[:80]


# ----------------------------------------------------------------
def hooks(progress, cancel):
    last = [None]

    def report(pct: int, message: str):
        if progress and (pct, message) != last[0]:
            last[0] = (pct, message)
            progress(pct, message)

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    return report, check


def files(cache) -> list[tuple[str, str, int]]:
    found = []
    for sub, rule in LAYOUT.items():
        try:
            with os.scandir(Path(cache) / sub) as entries:
                for e in entries:
                    try:
                        if rule.fullmatch(e.name) and e.is_file(follow_symlinks=False):
                            found.append(
                                (
                                    f"{sub}/{e.name}",
                                    e.path,
                                    e.stat(follow_symlinks=False).st_size,
                                )
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort()
    return found


# ----------------------------------------------------------------
def stats(cache) -> tuple[int, int]:
    found = files(cache)
    return len(found), sum(size for _name, _path, size in found)


# --------------------------------------------------
# A file that cannot be deleted is listed, the others still go
def clear(cache, progress=None, cancel=None) -> Result:
    report, check = hooks(progress, cancel)
    found = files(cache)
    res = Result()
    for i, (name, path, _size) in enumerate(found, 1):
        check()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            res.failed.append(f"{name}: {e.strerror or e}")
        else:
            res.files += 1
        report(i * 100 // len(found), "Clearing the cache")
    return res


def digest_of(path, check) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(4 << 20):
            check()
            h.update(block)
    return h.hexdigest()


# ----------------------------------------------------------------
# zip the cache files to dest, with a manifest and a dest.sha256 file next to it.
# A file that cannot be opened is left out and listed in Result.failed, the rest is saved.
# A read error in the middle of a file stops the whole backup. Nothing is left behind then
def create(cache, dest, progress=None, cancel=None) -> Result:
    report, check = hooks(progress, cancel)
    target = Path(dest).absolute()
    check()
    found = files(cache)
    if not found:
        raise ValueError("The analysis cache is empty, there is nothing to back up")
    total = sum(size for _name, _path, size in found) or 1
    fd, name = tempfile.mkstemp(prefix=".bb-cache-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        failed = []
        saved = done = 0
        with zipfile.ZipFile(
            temp, "w", zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for arc, path, _size in found:
                check()
                try:
                    info = zipfile.ZipInfo.from_file(path, arc, strict_timestamps=False)
                    src = open(path, "rb")  # closed by the with below  # noqa: SIM115
                except OSError as e:
                    failed.append(f"{arc}: {e.strerror or e}")
                    continue
                info.compress_type = zipfile.ZIP_DEFLATED
                with src, archive.open(info, "w") as out:
                    while block := src.read(CHUNK):
                        check()
                        out.write(block)
                        done += len(block)
                        report(done * 90 // total, "Saving the analyses")
                saved += 1
            if not saved:
                raise OSError("No cache file could be read:\n" + "\n".join(failed[:5]))
            note = {
                "format": FORMAT,
                "format_version": 1,
                "program": "Boothbridge",
                "program_version": VERSION,
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "anlz_version": ANLZ_VERSION,
                "files": saved,
            }
            archive.writestr(
                MANIFEST, json.dumps(note, indent=1), compress_type=zipfile.ZIP_DEFLATED
            )
        try:
            read_all(
                temp,
                check,
                lambda pct: report(90 + pct * 7 // 100, "Checking the archive"),
            )
        except UNREADABLE as e:
            if not is_damage(e):
                raise
            raise OSError(f"The archive failed its own check: {e}") from e
        check()
        with temp.open("r+b") as f:
            os.fsync(f.fileno())
        report(97, "Computing the checksum")
        checksum = digest_of(temp, check)
        check()
        side = str(target) + ".sha256"
        # an old checksum must not stay next to a new archive
        Path(side).unlink(missing_ok=True)
        os.replace(temp, target)
        try:
            write_atomic(side, f"{checksum} *{target.name}\n".encode())
        except OSError as e:
            raise OSError(
                f"The ZIP was saved as {target}, but its .sha256 file could not be written: {e}"
            ) from e
        report(100, "Done")
        return Result(
            path=str(target),
            files=saved,
            size=target.stat().st_size,
            failed=failed,
            sha256=checksum,
            sidecar=True,
        )
    finally:
        temp.unlink(missing_ok=True)


# entry count and directory size sit at the end of the file, read them before
# ZipFile builds one object per entry, an archive can claim millions
def declared(path):
    with open(path, "rb") as f:
        end = f.seek(0, os.SEEK_END)
        f.seek(max(0, end - 65557))
        tail = f.read()
        i = tail.rfind(b"PK\x05\x06")
        if i < 0 or len(tail) - i < 22:
            raise ValueError("This file is not a ZIP archive")
        count, size = struct.unpack("<HL", tail[i + 10 : i + 16])
        if count == 0xFFFF or size == 0xFFFFFFFF:
            at = end - len(tail) + i - 20
            if at < 0:
                raise ValueError("Invalid ZIP64 directory")
            f.seek(at)
            locator = f.read(20)
            if len(locator) != 20 or locator[:4] != b"PK\x06\x07":
                raise ValueError("Invalid ZIP64 directory")
            f.seek(struct.unpack("<Q", locator[8:16])[0])
            record = f.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise ValueError("Invalid ZIP64 directory")
            count, size = struct.unpack("<QQ", record[32:48])
    if count > MAX_FILES + 1 or size > MAX_DIRECTORY:
        raise ValueError("The ZIP holds too many entries to be a cache backup")


# --------------------------------------------------------------
def manifest(archive) -> dict:
    try:
        info = archive.getinfo(MANIFEST)
    except KeyError:
        raise ValueError(
            f"This ZIP is not a cache backup: {MANIFEST} is missing"
        ) from None
    if info.flag_bits & 1:
        raise ValueError(f"This ZIP is not a cache backup: {MANIFEST} is encrypted")
    with archive.open(info) as f:
        raw = f.read((64 << 10) + 1)
    if len(raw) > 64 << 10:
        raise ValueError(f"This ZIP is not a cache backup: {MANIFEST} is too large")
    try:
        note = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError(
            f"This ZIP is not a cache backup: {MANIFEST} cannot be read"
        ) from None
    if not isinstance(note, dict) or note.get("format") != FORMAT:
        raise ValueError("This ZIP is not a cache backup of Boothbridge")
    if note.get("format_version") != 1:
        raise ValueError("This backup was made in a format this version does not know")
    for key in ("anlz_version", "files"):
        if type(note.get(key)) is not int or note[key] < 0:
            raise ValueError(
                f"This ZIP is not a cache backup: {key} is missing from {MANIFEST}"
            )
    return note


# the manifest and every cache entry, after checking all of them. Nothing is written here
def plan(archive) -> tuple[dict, list[tuple[zipfile.ZipInfo, str]]]:
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1:
        raise ValueError("The ZIP holds too many entries to be a cache backup")
    note = manifest(archive)
    seen, entries, total = set(), [], 0
    for info in infos:
        name = info.filename
        if name in seen:
            raise ValueError(f"The entry {shown(name)} appears twice in the ZIP")
        seen.add(name)
        if name == MANIFEST:
            continue
        sub, _, leaf = name.partition("/")
        rule = LAYOUT.get(sub)
        mode = info.external_attr >> 16
        # attention -> orig_filename keeps a backslash that filename turns into a slash on Windows
        if (
            info.orig_filename != name
            or rule is None
            or not rule.fullmatch(leaf)
            or info.is_dir()
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
        ):
            raise ValueError(f"The entry {shown(name)} is not a cache file")
        if info.flag_bits & 1 or info.compress_type not in (
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
        ):
            raise ValueError(
                f"The entry {shown(name)} is encrypted or uses an unknown compression"
            )
        if info.file_size > MAX_FILE:
            raise ValueError(f"The entry {shown(name)} claims to be too large")
        if info.file_size > BIG and info.file_size > MAX_RATIO * max(
            1, info.compress_size
        ):
            raise ValueError(
                f"The entry {shown(name)} is compressed beyond {MAX_RATIO} to 1"
            )
        total += info.file_size
        if total > MAX_TOTAL:
            raise ValueError("The files in this ZIP would take too much room")
        entries.append((info, name))
    if note["files"] != len(entries):
        raise ValueError(
            f"The manifest announces {note['files']} files, the ZIP holds {len(entries)}"
        )
    return note, entries


# ------------------------------------------------------
# read every entry to its end, so zipfile checks the CRC, and compare the manifest with the entries
def read_all(path, check, step=None) -> dict:
    with zipfile.ZipFile(path) as archive:
        note, entries = plan(archive)
        total = sum(info.file_size for info, _name in entries) or 1
        done = 0
        for info, _name in entries:
            check()
            with archive.open(info) as f:
                while block := f.read(CHUNK):
                    check()
                    done += len(block)
                    if step:
                        step(done * 100 // total)
    return note


# --------------------------------------------------------------
# Raises ValueError when something is wrong
def verify(path, progress=None, cancel=None) -> Result:
    report, check = hooks(progress, cancel)
    path = str(path)
    side = os.path.exists(path + ".sha256")
    if side:
        check_sidecar(path, lambda pct, message: report(pct // 2, message), cancel)
    declared(path)
    try:
        note = read_all(
            path, check, lambda pct: report(50 + pct // 2, "Checking the archive")
        )
    except UNREADABLE as e:
        if not is_damage(e):
            raise
        raise ValueError(f"The ZIP is damaged: {e}") from e
    report(100, "Done")
    return Result(
        path=path, files=note["files"], size=os.path.getsize(path), sidecar=side
    )


# false when dst exists. rename refuses to replace a file on Windows, link does it elsewhere
def move_in(src, dst) -> bool:
    try:
        if os.name == "nt":
            os.rename(src, dst)
        else:
            os.link(src, dst)
            os.unlink(src)
    except FileExistsError:
        return False
    return True


# put the files of a backup back in the cache. A file already there is kept, its name holds
# what it was made from, so it is as good as the saved one. Every entry is checked before
# anything is written, the files are unpacked aside and moved in at the end
def restore(path, cache, progress=None, cancel=None) -> Result:
    report, check = hooks(progress, cancel)
    path, cache = str(path), Path(cache)
    side = os.path.exists(path + ".sha256")
    if side:
        check_sidecar(
            path, lambda pct, message: report(pct * 20 // 100, message), cancel
        )
    declared(path)
    res = Result(path=path, sidecar=side)
    try:
        with zipfile.ZipFile(path) as archive:
            note, entries = plan(archive)
            old = note["anlz_version"] != ANLZ_VERSION
            todo = []
            for info, name in entries:
                if old and name.startswith("anlz/"):
                    res.skipped += 1
                elif os.path.lexists(cache / name):
                    res.present += 1
                else:
                    todo.append((info, name))
            if res.skipped:
                res.notes.append(
                    f"{res.skipped} analysis files come from analysis version {note['anlz_version']}, "
                    f"this program uses {ANLZ_VERSION}. They would be calculated again, "
                    "so they were not restored."
                )
            if not todo:
                report(100, "Done")
                return res
            need = sum(info.file_size for info, _name in todo)
            cache.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(cache).free < need + SPARE:
                raise OSError("There is not enough free space to restore the analyses")
            stage = Path(tempfile.mkdtemp(prefix=".bb-restore-", dir=cache))
            try:
                done = 0
                for info, name in todo:
                    check()
                    out = stage / name
                    out.parent.mkdir(exist_ok=True)
                    written = 0
                    with archive.open(info) as src, open(out, "xb") as dst:
                        while block := src.read(CHUNK):
                            check()
                            written += len(block)
                            if written > info.file_size:
                                raise ValueError(
                                    f"The entry {shown(name)} is larger than the ZIP says"
                                )
                            dst.write(block)
                            done += len(block)
                            report(
                                20 + done * 75 // max(1, need), "Restoring the analyses"
                            )
                    if written != info.file_size:
                        raise ValueError(
                            f"The entry {shown(name)} is smaller than the ZIP says"
                        )
                check()
                for sub in {name.split("/")[0] for _info, name in todo}:
                    (cache / sub).mkdir(exist_ok=True)
                for _info, name in todo:
                    if move_in(stage / name, cache / name):
                        res.files += 1
                    else:
                        res.present += 1
            finally:
                shutil.rmtree(stage, ignore_errors=True)
    except UNREADABLE as e:
        if not is_damage(e):
            raise
        raise ValueError(f"The ZIP is damaged: {e}") from e
    report(100, "Done")
    return res
