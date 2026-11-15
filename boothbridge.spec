# Benoit Saint-Moulin
# Boothbridge : PyInstaller spec, portable onedir build (run through build.py)

import os

ROOT = os.path.abspath(SPECPATH)
# build.py compiles the core into build/stage first, the spec then reads the staged copy
BASE = os.environ.get("BB_STAGE", ROOT)
PKG = os.path.join(BASE, "djapp")
ICON = os.path.join(PKG, "ui", "res", "icon.ico")

datas = [
    (os.path.join(PKG, "ui", "res"), os.path.join("djapp", "ui", "res")),
    (os.path.join(PKG, "ui", "icons"), os.path.join("djapp", "ui", "icons")),
]

# librosa (and numba / llvmlite behind it) is only the last fallback, soundfile reads most files and the
# decoder of Qt reads m4a / aac. Out of the portable build, it weighs more than the app.
excludes = [
    "librosa", "numba", "llvmlite", "audioread", "resampy", "sklearn", "joblib",
    "matplotlib", "pandas", "IPython", "tkinter", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
]

# PyInstaller cannot read imports inside a compiled .pyd: list the package modules and
# the libraries the core uses by hand
core_imports = [
    "numpy", "scipy.signal", "scipy.ndimage", "soundfile", "PIL.Image", "sqlite3", "xml.etree.ElementTree",
    "defusedxml.ElementTree",
    "hashlib", "struct", "base64", "concurrent.futures", "dataclasses", "datetime",
    "plistlib", "ctypes", "ctypes.wintypes", "msvcrt", "binascii", "html", "json", "logging", "multiprocessing",
    "queue", "random", "subprocess", "tempfile", "threading", "urllib.parse",
]

# every standard library module the sources import, read from the original sources, so
# that a new import in a compiled module cannot be forgotten in the list above
import ast
import sys

for dirpath, _, names in os.walk(os.path.join(ROOT, "djapp")):
    for n in names:
        if not n.endswith(".py"):
            continue
        with open(os.path.join(dirpath, n), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            found = []
            if isinstance(node, ast.Import):
                found = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found = [node.module]
            for name in found:
                if name.split(".")[0] in sys.stdlib_module_names:
                    core_imports.append(name)
core_imports = sorted(set(core_imports))

modules = []
for dirpath, _, names in os.walk(PKG):
    rel = os.path.relpath(dirpath, BASE).replace(os.sep, ".")
    for n in names:
        stem = n.split(".")[0]
        if n.endswith((".py", ".pyd")) and stem != "__init__":
            modules.append(f"{rel}.{stem}")

# the PyArmor runtime that compile_core.py puts inside the package (the sealed native modules
# include it), the protected __init__ files import it
if os.path.isdir(os.path.join(PKG, "pyarmor_runtime_000000")):
    core_imports.append("djapp.pyarmor_runtime_000000")

# the C++ core, built next to native.py by compile_core.py
native_libs = [(os.path.join(dirpath, n), os.path.relpath(dirpath, BASE))
               for dirpath, _, names in os.walk(PKG) for n in names if n.startswith(("fastcore.", "libfastcore."))]

a = Analysis(
    [os.path.join(BASE, "main.py")],
    pathex=[BASE],
    datas=datas,
    binaries=native_libs,
    hiddenimports=["PySide6.QtMultimedia", *core_imports, *modules],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Boothbridge",
    console=False,
    icon=[ICON],
    upx=False,
    contents_directory="runtime",
)

coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Boothbridge")
