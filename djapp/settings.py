# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : User settings stored as JSON in the data folder, with the data folder lookup, cache location and migration.

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from . import i18n
from .brand import APP_NAME, HOME_ENV, ORG_FOLDER, PORTABLE_FLAG, SETTINGS_FILE
from .i18n import tr

log = logging.getLogger(__name__)

LANGUAGE_CODES = tuple(i18n.LANGUAGES)

# every export format the program knows, exports() returns them
EXPORTS = (
    "Rekordbox XML",
    "CDJ/USB",
    "M3U",
    "Traktor NML",
    "VirtualDJ",
    "Mixxx",
    "iTunes XML",
    "CSV",
    "JSON",
    "PLS",
    "XSPF",
)

# the formats written from the documentation and never opened in the program they are meant for
BETA_EXPORTS = ("VirtualDJ", "Mixxx", "iTunes XML", "PLS", "XSPF")

def export_label(fmt: str) -> str:
    return f"{fmt} {tr('(beta)')}" if fmt in BETA_EXPORTS else fmt

# the name of a format typed in any case, with or without its beta label, "" when unknown
def export_name(text) -> str:
    if not isinstance(text, str):
        return ""
    s = text.strip().casefold()
    for fmt in EXPORTS:
        if s == fmt.casefold() or (fmt in BETA_EXPORTS and s.startswith(fmt.casefold() + " ")):
            return fmt
    return ""

def exports() -> tuple[str, ...]:
    return EXPORTS

DEFAULTS = {
    "source_path": "",
    "output_path": "",
    "music_root": "",
    "library_root": "",
    "library_geometry": "",
    "cache_backup_dir": "",  # folder of the last analysis backup
    "support_asked": False,  # the thank you after the first export has been shown
    "language": "",  # empty until the first start has asked
    "export_format": "Rekordbox XML",
    "copy_music": True,
    "verify_copy": True,
    # CDJ/USB: "keep" the playlists already on the key, or "replace" them all
    "key_mode": "keep",
    "key_ask": True,  # ask which one when the key already has a database
    "key_format": "Open Key",
    "anlz_processes": 0,  # 0 = auto, up to 8
    "player_settings": "",  # CDJ/USB: player switches chosen here, "11:1,23:0" (position:on), empty = leave them
    "auto_load": True,
    "confirm_exit": False,
    "waveform_color": "RGB",
    "crossfade": 3,
    "volume": 70,
    "log_level": "INFO",
}

def app_dir() -> Path:
    # frozen build: next to the exe, source run: the project folder
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

# --------------------------------------------------------
# a flag file next to the program: the portable zip carries it, the installer does not
def portable() -> bool:
    return (app_dir() / PORTABLE_FLAG).is_file()

# the folder of the program among the user's data, %APPDATA%\BSM\Boothbridge (~/.config/BSM/Boothbridge elsewhere);
# the variable BOOTHBRIDGE_HOME replaces it
def home_dir() -> Path:
    given = os.environ.get(HOME_ENV)
    if given:
        return Path(given)
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return Path(base) / ORG_FOLDER / APP_NAME

# the settings and the log: beside the program in a portable copy, else in the home folder
def data_dir() -> Path:
    d = app_dir() if portable() and not os.environ.get(HOME_ENV) else home_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("cannot create %s: %s", d, e)
    return d

# The analyses (beat grids, BPM, waveforms) are hours of work, not a preference: they can live in a data folder so that a format of the system disk does not take them.
# That folder is named by BSM_DATAS, else by the root of %APPDATA%\BSM\datas.json, else it is D:\Datas when it is there. Without any of them the cache is the
# cache folder of the home folder. Settings and log stay in the home folder. BOOTHBRIDGE_HOME (the tests) keeps everything in one folder.
class DatasUnavailable(OSError):
    pass

DEFAULT_DATAS = Path("D:/Datas")
DATAS_MARK = "datas.path"  # in the home folder: the default data folder was used once

def _first_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip().split("\n")[0].strip()
    except (OSError, ValueError):
        return ""

# the data folder to use, None when there is none and the home folder keeps the cache
# attention -> bug = D:/Datas was taken by default: no start at all on a PC without a D: disk, and a folder named "D:" made on macOS and Linux
def datas_root() -> Path | None:
    given = os.environ.get("BSM_DATAS", "").strip().strip('"')
    if not given:
        base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
        try:
            with open(Path(base) / ORG_FOLDER / "datas.json", encoding="utf-8-sig") as f:
                given = str(json.load(f).get("root") or "").strip()
        except (OSError, ValueError, AttributeError):
            given = ""
    if given:
        return Path(given)
    # used before: a disk that is gone is said, the library is not analysed again somewhere else
    used = _first_line(home_dir() / DATAS_MARK)
    if used:
        return Path(used)
    # not absolute outside Windows
    if DEFAULT_DATAS.is_absolute() and DEFAULT_DATAS.is_dir():
        return DEFAULT_DATAS
    return None

# only the default needs a mark, the variable and the pointer file say it again at each start
def _remember(root: Path) -> None:
    if root != DEFAULT_DATAS or os.environ.get("BSM_DATAS", "").strip():
        return
    mark = home_dir() / DATAS_MARK
    if mark.is_file():
        return
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(f"{root}\n", encoding="utf-8")
    except OSError as e:
        log.warning("cannot write %s: %s", mark, e)

def _tree(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p.stat().st_size
    return out

# the one-time copy of the cache from the home folder to D:\Datas: a copy, never a move (the old files stay until the user removes them), never over an entry already
# there, each entry checked file by file before it takes its name. Returns the entries copied
def adopt_cache(src: Path, dst: Path) -> list:
    done = []
    if not src.is_dir() or os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return done
    for a in sorted(src.iterdir()):
        b = dst / a.name
        if b.exists():
            continue
        part = dst / (a.name + ".part")
        try:
            if part.is_dir():
                shutil.rmtree(part, ignore_errors=True)
            elif part.exists():
                part.unlink()
            if a.is_dir():
                shutil.copytree(a, part)
                if _tree(a) != _tree(part):
                    raise DatasUnavailable(f"the copy of {a} is incomplete, nothing was kept")
            else:
                shutil.copy2(a, part)
                if a.stat().st_size != part.stat().st_size:
                    raise DatasUnavailable(f"the copy of {a} is incomplete, nothing was kept")
            os.replace(part, b)
        except DatasUnavailable:
            shutil.rmtree(part, ignore_errors=True) if part.is_dir() else part.unlink(missing_ok=True)
            raise
        except OSError as e:
            shutil.rmtree(part, ignore_errors=True) if part.is_dir() else part.unlink(missing_ok=True)
            raise DatasUnavailable(f"{a} could not be copied to {b}: {e}") from e
        done.append(a.name)
    return done

_ADOPTED: list = []

def cache_dir() -> Path:
    root = None if os.environ.get(HOME_ENV) else datas_root()
    if root is None:
        d = home_dir() / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    if root.anchor and not Path(root.anchor).exists():
        raise DatasUnavailable(
            f"The data folder {root} is on a disk that is not there ({root.anchor}). "
            f"{APP_NAME} does not analyse your library again somewhere else. Connect the disk, or set BSM_DATAS to another folder."
        )
    d = root / APP_NAME / "cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DatasUnavailable(f"The data folder {d} cannot be made: {e}") from e
    _remember(root)
    if not _ADOPTED:
        adopt_cache(home_dir() / "cache", d)
        _ADOPTED.append(True)
    return d

CONFIG = data_dir() / SETTINGS_FILE

# what older versions kept elsewhere: the settings beside the program, the caches in %LOCALAPPDATA%\Boothbridge.
# Copied or moved once, never over what is already there, and never when the data folder is replaced by hand
def migrate() -> None:
    if os.environ.get(HOME_ENV):
        return
    try:
        old = app_dir() / SETTINGS_FILE
        if old != CONFIG and old.is_file() and not CONFIG.exists():
            shutil.copy2(old, CONFIG)
            log.info("settings copied from %s", old)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for name in ("anlz", "wave"):
                src, dst = Path(local) / APP_NAME / name, cache_dir() / name
                if src.is_dir() and not dst.exists():
                    shutil.move(str(src), str(dst))
                    log.info("%s moved to %s", src, dst)
    except OSError as e:
        log.warning("the data of an older version could not be moved: %s", e)

def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        # utf-8-sig: Notepad can save the file with a BOM
        with open(CONFIG, encoding="utf-8-sig") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update(saved)
        else:
            log.warning("settings are not a JSON object, defaults used")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, RecursionError) as e:
        log.warning("settings unreadable, defaults used: %s", e)
    if cfg["language"] not in LANGUAGE_CODES:
        cfg["language"] = ""
    # a hand edited file: every value keeps the type of its default, else the default
    for key, default in DEFAULTS.items():
        value = cfg[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, str)
        if not ok:
            cfg[key] = default
    if cfg["log_level"].upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        cfg["log_level"] = DEFAULTS["log_level"]
    cfg["log_level"] = cfg["log_level"].upper()
    name = export_name(cfg["export_format"])
    cfg["export_format"] = name if name in exports() else "Rekordbox XML"
    return cfg

# --------------------------------------------------------
def save(cfg: dict):
    tmp = CONFIG.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG)
    except (OSError, UnicodeError) as e:
        log.error("cannot save settings: %s", e)

# --------------------------------------------------------
def default_collection() -> str:
    ni = Path.home() / "Documents" / "Native Instruments"
    found = sorted(
        ni.glob("Traktor*/collection.nml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(found[0]) if found else ""
