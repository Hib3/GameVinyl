"""QC Round 7 regression audit: delivery-file encodings and line endings.

Windows field history: an LF-only check.bat was unparseable/garbled by
cmd.exe. This test mechanically guarantees, for every shipped file:

* every ``*.bat`` is pure ASCII and uses CRLF for *every* line ending
  (no LF-only, no CR-only, file ends with CRLF);
* every Python / Markdown / TOML / JSON / SQL / template text file is
  valid UTF-8 without a BOM;
* requirements.txt is fully pinned (``==``) and pyproject.toml declares
  exactly the same pinned dependency set, so the two install routes
  (``pip install -r requirements.txt`` via setup.bat, and installing the
  project metadata) resolve to identical versions.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".sql", ".txt", ".template"}


def _files(suffixes):
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or SKIP_PARTS & set(p.parts):
            continue
        if p.suffix in suffixes or (suffixes == {".template"} and p.name.endswith(".template")):
            out.append(p)
    return out


def test_bat_files_exist():
    bats = _files({".bat"})
    assert bats, "no .bat files found - audit would be vacuous"
    names = {p.name for p in bats}
    assert {"setup.bat", "check.bat"} <= names


def test_bat_files_are_ascii_with_pure_crlf():
    for p in _files({".bat"}):
        data = p.read_bytes()
        assert data, f"{p.name}: empty"
        # Pure ASCII (cmd.exe code-page safety).
        non_ascii = [b for b in data if b > 127]
        assert not non_ascii, f"{p.name}: {len(non_ascii)} non-ASCII bytes"
        data.decode("ascii")
        # Every LF must be part of a CRLF; no stray CR either.
        assert data.count(b"\n") == data.count(b"\r\n"), (
            f"{p.name}: LF-only line endings present "
            f"(LF={data.count(bytes([10]))}, CRLF={data.count(bytes([13, 10]))})"
        )
        assert data.count(b"\r") == data.count(b"\r\n"), (
            f"{p.name}: CR-only line endings present"
        )
        assert data.endswith(b"\r\n"), f"{p.name}: does not end with CRLF"
        assert not data.startswith(b"\xef\xbb\xbf"), f"{p.name}: UTF-8 BOM"


def test_text_files_are_utf8_without_bom():
    files = _files(TEXT_SUFFIXES)
    files += [p for p in [ROOT / ".env.template"] if p.exists()]
    files += _files({".json"})
    assert files, "no text files found - audit would be vacuous"
    for p in files:
        data = p.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), f"{p}: UTF-8 BOM"
        data.decode("utf-8")  # raises UnicodeDecodeError on failure


def _requirements_pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, ver = line.partition("==")
        assert sep, f"requirements.txt line not pinned with ==: {line!r}"
        pins[name.lower()] = ver.strip()
    return pins


def test_requirements_fully_pinned():
    pins = _requirements_pins()
    assert len(pins) >= 5, f"suspiciously few requirements: {pins}"


def test_pyproject_dependencies_match_requirements():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = {}
    for name, ver in re.findall(r'"([A-Za-z0-9_.-]+)==([0-9][^"]*)"', text):
        declared[name.lower()] = ver
    assert declared == _requirements_pins(), (
        f"pyproject dependencies {declared} != requirements pins "
        f"{_requirements_pins()}"
    )
