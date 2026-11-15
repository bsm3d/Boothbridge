# Benoit Saint-Moulin
# Boothbridge : portable build (PyInstaller onedir) and its zip

# python build.py [--no-zip] [--installer]   the installer needs Inno Setup (BB_ISCC, or the usual folders)
# dist/Boothbridge/ is copied as is (USB key, another PC, no admin rights). Settings and log land next to Boothbridge.exe.
# No .py file ships: the code lives compiled in the PYZ, the core (tags, sources, export/cdj) is native (Cython, then MinGW gcc),
# the __init__ files that carry logic go through PyArmor.

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "Boothbridge"
SUFFIX = ""

sys.path.insert(0, str(ROOT))
from djapp import VERSION

# Qt pieces the app never touches, the hooks drag them in anyway
PRUNE = [
    "qml",
    "Qt6WebEngine*",
    "Qt6Quick*",
    "Qt6Qml*",
    "Qt6Pdf*",
    "Qt6Designer*",
    "opengl32sw.dll",
    "Qt6VirtualKeyboard*",
    "Qt6Charts*",
    "Qt63D*",
]
# the buttons of the standard Qt dialogs (qt_translator in djapp/app.py): only these four files of Qt's translations stay
KEEP_QT_TRANSLATIONS = {
    "qtbase_fr.qm",
    "qtbase_de.qm",
    "qtbase_es.qm",
    "qtbase_zh_CN.qm",
}
SOURCE_SUFFIXES = {".py", ".pyw", ".pyi", ".pyx", ".pxd", ".c", ".cpp", ".h", ".hpp"}


def clear(folder: Path):
    # rename first: Windows refuses while a Boothbridge.exe from there is running, a plain rmtree would delete half the folder under the running app
    if not folder.exists():
        return
    old = folder.with_name(folder.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    try:
        folder.rename(old)
    except OSError:
        sys.exit(f"{folder} is in use, close Boothbridge first (nothing was deleted)")
    shutil.rmtree(old, ignore_errors=True)


def run_pyinstaller():
    clear(DIST)
    clear(ROOT / "build")
    # the core (tags, sources, export/cdj) is compiled to native modules first
    subprocess.run(
        [sys.executable, str(ROOT / "compile_core.py")], cwd=ROOT, check=True
    )
    env = {**os.environ, "BB_STAGE": str(ROOT / "build" / "stage")}
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            str(ROOT / "boothbridge.spec"),
        ],
        cwd=ROOT,
        check=True,
        env=env,
    )


def prune():
    rt = DIST / "runtime"
    gone = 0
    for pat in PRUNE:
        for p in rt.rglob(pat):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            gone += 1
    for folder in rt.rglob("translations"):
        if folder.is_dir():
            for p in folder.rglob("*"):
                if p.is_file() and p.name not in KEEP_QT_TRANSLATIONS:
                    p.unlink()
                    gone += 1
    # sources shipped as data by some hooks, the compiled modules are in the PYZ already
    for p in rt.rglob("*"):
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES:
            p.unlink()
            gone += 1
    return gone


def check_seal():
    # what PyInstaller copied is what the seal says, byte for byte
    from djapp import integrity

    seal = ast.parse(
        (ROOT / "build" / "stage" / "djapp" / "_seal.py").read_text(encoding="utf-8")
    )
    hashes = ast.literal_eval(seal.body[0].value)
    found = integrity.problems(DIST / "runtime" / "djapp", hashes)
    if found:
        sys.exit("the built folder does not match its seal:\n  " + "\n  ".join(found))
    print(f"seal checked: {len(hashes)} native modules match")


# Inno Setup: the path in BB_ISCC, the PATH, then the places the installer of Inno Setup uses
def find_iscc() -> str | None:
    found = os.environ.get("BB_ISCC") or shutil.which("iscc")
    if found:
        return found
    for base in (
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
    ):
        for name in ("Inno Setup 6", "Inno Setup 5"):
            for sub in ("Programs", ""):
                path = Path(base) / sub / name / "ISCC.exe"
                if path.is_file():
                    return str(path)
    return None


# the setup of the folder that was just built, named like the zip so the two are never mixed up
def make_installer() -> Path:
    iscc = find_iscc()
    if not iscc:
        sys.exit("Inno Setup was not found: install it or set BB_ISCC to ISCC.exe")
    command = [iscc, f"/DAppVersion={VERSION}"]
    if SUFFIX:
        command.append(f"/DEdition={SUFFIX}")
    command.append(str(ROOT / "installer" / "Boothbridge.iss"))
    subprocess.run(command, cwd=ROOT, check=True)
    return ROOT / "dist" / f"Boothbridge-{VERSION}-setup{SUFFIX}.exe"


def size(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def make_zip() -> Path:
    z = ROOT / "dist" / f"Portable_Boothbridge-{VERSION}-win64{SUFFIX}.zip"
    z.unlink(missing_ok=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in sorted(DIST.rglob("*")):
            if f.is_file():
                zf.write(f, Path("Boothbridge") / f.relative_to(DIST))
        # the settings and the log of the portable copy stay beside it (the installed program keeps them in %APPDATA%\BSM)
        zf.writestr("Boothbridge/portable.flag", "Settings and log stay in this folder. Delete this file to use %APPDATA%\\BSM\\Boothbridge instead.\n")
    return z




def main() -> int:
    run_pyinstaller()
    n = prune()
    check_seal()
    # the license travels with every copy, PolyForm asks for it, and so do the licences of the libraries
    shutil.copyfile(ROOT / "LICENSE", DIST / "LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD_PARTY.md", DIST / "THIRD_PARTY.md")
    left = [
        p
        for p in DIST.rglob("*")
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
    ]
    if left:
        sys.exit(
            "Source files remain in the release:\n  " + "\n  ".join(map(str, left))
        )
    print(f"\npruned {n} files, no source files left, {size(DIST):.0f} MB in {DIST}")
    if "--no-zip" not in sys.argv:
        z = make_zip()
        print(f"zip: {z} ({z.stat().st_size / 1e6:.0f} MB)")
    if "--installer" in sys.argv:
        s = make_installer()
        print(f"setup: {s} ({s.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
