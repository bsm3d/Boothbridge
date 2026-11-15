import hashlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "Boothbridge"
if not (ROOT / "tools").is_dir():
    ROOT = Path(__file__).resolve().parents[1]


def load_checksums():
    spec = importlib.util.spec_from_file_location("checksums", ROOT / "tools" / "checksums.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checksums_file_matches_hashlib(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"boothbridge" * 100000)
    out = tmp_path / "SHA256SUMS.txt"
    assert load_checksums().main([str(a), "--out", str(out)]) == 0
    assert out.read_text(encoding="ascii") == f"{hashlib.sha256(a.read_bytes()).hexdigest()} *a.bin\n"


def test_checksums_missing_file_fails(tmp_path):
    assert load_checksums().main([str(tmp_path / "nope")]) == 1


def test_installer_script_names_the_edition():
    iss = (ROOT / "installer" / "Boothbridge.iss").read_text(encoding="utf-8")
    assert "OutputBaseFilename=Boothbridge-{#AppVersion}-setup{#Edition}" in iss
    assert "{6ACF9846-E0D6-415D-9B8B-C845B4BBEDAE}" in iss
    assert "PrivilegesRequired=admin" in iss
    assert "DefaultDirName={autopf}\\{#AppName}" in iss

