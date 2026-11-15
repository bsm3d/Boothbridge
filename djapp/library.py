# read-only music-library scans and exact-file duplicate detection

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field

from .export.files import Cancelled
from .model import Track
from .sources import is_audio
from .sources.folder import make_track, natural


@dataclass
class LibraryScan:
    tracks: list[Track] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DuplicateScan:
    groups: list[list[str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# symlinks and Windows junctions. islink() is False for a junction and os.walk enters it
def is_link(path):
    if os.path.islink(path):
        return True
    junction = getattr(os.path, "isjunction", None)
    if junction is not None:
        return junction(path)
    try:
        # Python before 3.12 has no isjunction; a junction is a mount-point reparse point
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (AttributeError, OSError):
        return False


# --------------------------------------------------------------
# recursively read tags off the GUI thread; never follow symlinks or junctions
def scan(root: str, progress=None, cancel=None) -> LibraryScan:
    check_cancel(cancel)
    if not root or not os.path.isdir(root):
        raise FileNotFoundError(
            f"Music library folder is unavailable: {root or '(not configured)'}"
        )
    result = LibraryScan()

    def failed(error):
        result.errors.append(str(error))

    for folder, dirs, files in os.walk(
        os.path.abspath(root), onerror=failed, followlinks=False
    ):
        check_cancel(cancel)
        dirs[:] = sorted(
            (d for d in dirs if not is_link(os.path.join(folder, d))), key=natural
        )
        for name in sorted(files, key=natural):
            check_cancel(cancel)
            path = os.path.join(folder, name)
            if not is_audio(name) or is_link(path):
                continue
            try:
                with open(path, "rb"):
                    pass
                result.tracks.append(make_track(path))
            except OSError as error:
                result.errors.append(f"{path}: {error}")
            if progress and len(result.tracks) % 25 == 0:
                progress(0, f"Reading music library: {len(result.tracks)} tracks...")
    check_cancel(cancel)
    if progress:
        progress(100, f"{len(result.tracks)} tracks")
    return result


# ----------------------------------------------------------
# equal size and SHA-256 identifies exact file copies, not different encodings.
#
# Empty files, hardlinks and aliases of one file are not copies of each other
def duplicates(paths: list[str], progress=None, cancel=None) -> DuplicateScan:
    result = DuplicateScan()
    sizes: dict[int, list[str]] = {}
    seen = set()
    files = set()
    for path in paths:
        check_cancel(cancel)
        identity = os.path.normcase(os.path.abspath(path))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            info = os.stat(path)
            if info.st_size == 0:
                continue
            # Same device and inode is one physical file; some filesystems report 0 for both
            ident = (info.st_dev, info.st_ino)
            if all(ident):
                if ident in files:
                    continue
                files.add(ident)
            sizes.setdefault(info.st_size, []).append(path)
        except OSError as error:
            result.errors.append(f"{path}: {error}")
    candidates = [path for group in sizes.values() if len(group) > 1 for path in group]
    hashes: dict[tuple[int, str], list[str]] = {}
    for index, path in enumerate(candidates):
        check_cancel(cancel)
        if progress:
            progress(
                100 * index // max(1, len(candidates)),
                f"Checking duplicates: {index + 1}/{len(candidates)}",
            )
        try:
            before = os.stat(path)
            digest = hashlib.sha256()
            with open(path, "rb") as audio:
                while chunk := audio.read(1 << 20):
                    check_cancel(cancel)
                    digest.update(chunk)
            after = os.stat(path)
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError(
                    "File changed while checking duplicates; refresh and scan again"
                )
            hashes.setdefault((before.st_size, digest.hexdigest()), []).append(path)
        except OSError as error:
            result.errors.append(f"{path}: {error}")
    check_cancel(cancel)
    result.groups = [group for group in hashes.values() if len(group) > 1]
    if progress:
        progress(100, f"{len(result.groups)} exact duplicate groups")
    return result
