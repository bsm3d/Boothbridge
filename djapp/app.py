# Benoit Saint-Moulin
# Boothbridge : application start

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
from pathlib import Path

from . import APP_NAME, i18n, settings
from .brand import LOG_FILE

USAGE = (
    f"{APP_NAME} --export COLLECTION OUTPUT [--format FORMAT] [--music FOLDER]"
    " [--keep | --replace]"
    f"\n{APP_NAME} --verify OUTPUT\n"
    f"{APP_NAME} --help\n\n"
    "FORMAT: Rekordbox XML (default), "
    "CDJ/USB, "
    "M3U, Traktor NML. Everything in the\n"
    f"collection is exported, the report goes to {LOG_FILE} in the data folder of the program.\n"
    "CDJ/USB keeps the playlists already on the key and adds the new ones (--keep, the default).\n"
    "A new playlist with the same name, in the same folder, takes the place of the old one.\n"
    "--replace writes a database with the new playlists only. Either way the database that was on\n"
    "the key is saved first as PIONEER/rekordbox/export.pdb.bak.\n"
    "--verify reads an export back and compares it to the checksums written with it."
)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------
def headless(argv: list[str]) -> int:
    from . import export, sources

    def opt(name, default=""):
        return (
            argv[argv.index(name) + 1]
            if name in argv and argv.index(name) + 1 < len(argv)
            else default
        )

    i = argv.index("--export")
    if len(argv) < i + 3:
        log_line(USAGE)
        return 2
    src, out = argv[i + 1], argv[i + 2]
    if src.startswith("--") or out.startswith("--"):
        log_line(USAGE)
        return 2
    for name in ("--format", "--music"):
        if name in argv and argv.index(name) + 1 >= len(argv):
            log_line(f"{name} needs a value")
            return 2
    if "--keep" in argv and "--replace" in argv:
        log_line("choose --keep or --replace, not both")
        return 2
    cfg = settings.load()
    fmt = opt("--format", "Rekordbox XML")
    fmt = settings.export_name(fmt) or fmt
    if "--replace" in argv:
        cfg["key_mode"] = "replace"
    elif "--keep" in argv:
        cfg["key_mode"] = "keep"
    log = logging.getLogger("headless")
    if fmt not in settings.EXPORTS:
        log_line(
            f"unknown format {fmt}, the formats are: {', '.join(settings.exports())}"
        )
        return 2
    log.info("export %s -> %s (%s)", src, out, fmt)
    try:
        nodes = sources.load(src, opt("--music", cfg["music_root"]))
        rep = export.run(fmt, nodes, out, cfg)
    except (ValueError, OSError) as e:
        # the windowed exe would show a crash box instead of the reason
        log_line(f"export failed: {e}")
        return 1
    except Exception as e:
        log.exception("export failed")
        log_line(f"export failed: {type(e).__name__}: {e}")
        return 1
    if fmt == "CDJ/USB":
        if rep.kept or rep.replaced:
            log_line(
                f"kept {plural(rep.kept, 'playlist')} already on the key, replaced {len(rep.replaced)}"
                + (f" ({', '.join(rep.replaced)})" if rep.replaced else "")
                + f", added {rep.added}"
            )
        elif cfg["key_mode"] == "replace" and rep.backup:
            log_line(
                "replaced the database of the key, the old one is saved as export.pdb.bak"
            )
    for m in rep.missing:
        log.warning("missing: %s", m)
    if rep.errors:
        log_line(f"{plural(len(rep.errors), 'error')}, the first: {rep.errors[0]}")
    if rep.left_out:
        log_line(
            f"not written, none of their files was found: {', '.join(rep.left_out)}"
        )
    for p in rep.player_changed:
        log.warning("kept as it is, changed since the last export: %s", p)
    for e in rep.errors:
        log.error(e)
    log.info(
        "%d tracks, %d missing, %d errors, %d cached",
        rep.tracks,
        len(rep.missing),
        len(rep.errors),
        rep.cached,
    )
    return 1 if rep.errors else 0


def verify(argv: list[str]) -> int:
    from .export import manifest

    i = argv.index("--verify")
    if len(argv) < i + 2:
        log_line(USAGE)
        return 2
    chk = manifest.verify(argv[i + 1], allow_player_changes=True)
    for p in chk.damaged:
        log_line(f"damaged: {p}")
    for p in chk.missing:
        log_line(f"missing: {p}")
    for p in chk.modified:
        log_line(
            f"modified since export, integrity unconfirmed (possible player update): {p}"
        )
    log_line(chk.summary())
    return 0 if chk.good else 1


MODES = (
    "--export",
    "--verify",
)
OPTIONS = (
    "--format",
    "--music",
    "--keep",
    "--replace",
)
INTERNAL = (
    "--multiprocessing-fork",
    "--format-disk",
    "--size",
    "--serial",
    "--label",
    "--progress",
    "--owner",
)


# ----------------------------------------------------------
# exit code when the command line asks for help or is wrong, None to go on. Without it
# an unknown option (or --help) would open the window
def check_options(argv: list[str]) -> int | None:
    args = argv[1:]
    if any(a in ("--help", "-h", "/?") for a in args):
        log_line(USAGE)
        return 0
    unknown = [
        a for a in args if a.startswith("--") and a not in (*MODES, *OPTIONS, *INTERNAL)
    ]
    if unknown:
        log_line(f"unknown option: {unknown[0]}\n\n{USAGE}")
        return 2
    stray = [a for a in args if a in OPTIONS]
    if stray and "--export" not in args:
        log_line(f"{stray[0]} goes with --export\n\n{USAGE}")
        return 2
    return None


def log_line(msg: str):
    logging.getLogger("headless").info(msg)
    if sys.stdout is not None:
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode("ascii"))


# ----------------------------------------------------------
# the buttons of the standard Qt dialogs (Yes, No, Cancel...) come with Qt in each language
def qt_translator(app, code: str):
    import PySide6
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    if code == i18n.SOURCE:
        return
    name = {"zh": "zh_CN"}.get(code, code)
    # the folder Qt reports, then the one next to the PySide6 package (a frozen build keeps them there)
    folders = [
        QLibraryInfo.path(QLibraryInfo.TranslationsPath),
        str(Path(PySide6.__file__).parent / "translations"),
    ]
    translator = QTranslator(app)
    for folder in folders:
        if translator.load(QLocale(name), "qtbase", "_", folder):
            app.installTranslator(translator)
            logging.getLogger(__name__).info("Qt translations from %s", folder)
            return
    logging.getLogger(__name__).info("no Qt translation found for %s", name)


def main() -> int:
    # the analysis workers are processes, a frozen build re-enters here without this
    multiprocessing.freeze_support()

    # the Qt player's ffmpeg backend prints every probed file on the console
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg*=false")
    os.environ.setdefault("QT_FFMPEG_DEBUG", "0")

    # the compiled core is compared with its seal before anything of it is imported
    from . import integrity

    found = integrity.check()
    if found:
        logging.basicConfig(
            filename=str(settings.data_dir() / LOG_FILE), level=logging.ERROR
        )
        return integrity.stop(found, quiet=any(a in sys.argv for a in MODES))

    # the privileged half of formatting a drive: no window, no settings, no log file of its own
    if "--format-disk" in sys.argv:
        from . import drives

        return drives.helper_main(sys.argv)

    code = check_options(sys.argv)
    if code is not None:
        return code

    settings.migrate()
    cfg = settings.load()
    root = logging.getLogger()
    root.setLevel(cfg["log_level"])
    fh = logging.FileHandler(settings.data_dir() / LOG_FILE, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(fh)

    if "--export" in sys.argv:
        return headless(sys.argv)
    if "--verify" in sys.argv:
        return verify(sys.argv)

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .ui import language, theme
    from .ui.dialogs import LogBridge, LogWindow
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "ui" / "res" / "icon.png")))
    app.setStyleSheet(theme.STYLE)

    # the language is asked at the first start, then it is a setting of the Preferences
    if cfg["language"] not in i18n.LANGUAGES:
        cfg["language"] = language.choose()
        settings.save(cfg)
    i18n.set_language(cfg["language"])
    qt_translator(app, cfg["language"])

    logwin = LogWindow()
    bridge = LogBridge()
    bridge.line.connect(logwin.add)
    root.addHandler(bridge)

    win = MainWindow(cfg, logwin)
    win.show()
    rc = app.exec()
    root.removeHandler(bridge)
    return rc
