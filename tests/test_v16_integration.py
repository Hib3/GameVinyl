"""v1.6 integration hardening tests.

Covers the three fixes added at integration after the 20-debugger
round: (1) init_schema converges upgraded DBs by ensuring the two
late-added columns eagerly, (2) a failing startup bookkeeping step
can no longer take the boot down with it, (3) diagnose's manifest
check detects missing / altered / leftover code files (mixed
versions left behind by overwrite-copy updates).
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from grt import db as dbmod  # noqa: E402
from grt import main as main_mod  # noqa: E402


# ---------------------------------------------------------------- (1)
def _old_shape_db(path: Path) -> sqlite3.Connection:
    """A DB whose two tables predate the late-added columns."""
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE fetch_runs(id INTEGER PRIMARY KEY, source_id INT)")
    db.execute(
        "CREATE TABLE notification_outbox(id INTEGER PRIMARY KEY,"
        " body TEXT)")
    db.commit()
    return db


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def test_init_schema_adds_late_columns_to_old_db(tmp_path):
    db = _old_shape_db(tmp_path / "old.db")
    assert "fetcher_kind" not in _columns(db, "fetch_runs")
    assert "reply_markup_json" not in _columns(db, "notification_outbox")
    dbmod.init_schema(db)
    assert "fetcher_kind" in _columns(db, "fetch_runs")
    assert "reply_markup_json" in _columns(db, "notification_outbox")
    # Idempotent: a second run must not raise or duplicate.
    dbmod.init_schema(db)
    assert "fetcher_kind" in _columns(db, "fetch_runs")


# ---------------------------------------------------------------- (2)
class _Sentinel(Exception):
    pass


class _FakeScheduler:
    def __init__(self, *a, **kw):
        pass

    def add_job(self, *a, **kw):
        pass

    def start(self):
        raise _Sentinel("reached scheduler")


def test_startup_step_failure_is_contained(tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path / "data"))
    from grt import config
    monkeypatch.setattr(config, "_dotenv_loaded", True, raising=False)

    def boom(db):
        raise RuntimeError("gap boom")

    monkeypatch.setattr(main_mod, "record_startup_gap", boom)
    monkeypatch.setattr(main_mod, "BlockingScheduler", _FakeScheduler)
    with pytest.raises(_Sentinel):
        main_mod.run()
    log = (tmp_path / "data" / "logs" / "startup.log").read_text(
        encoding="utf-8")
    assert "record_startup_gap failed" in log
    assert "gap boom" in log


def test_log_startup_step_failure_writes_log(tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path / "data"))
    main_mod._log_startup_step_failure(
        "recover_orphaned_sending", ValueError("bad row"))
    log = (tmp_path / "data" / "logs" / "startup.log").read_text(
        encoding="utf-8")
    assert "recover_orphaned_sending failed" in log
    assert "bad row" in log


# ---------------------------------------------------------------- (3)
def _write_manifest(root: Path, files: dict[str, bytes]) -> None:
    lines = []
    for rel, data in sorted(files.items()):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
    (root / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _run_manifest_check(tmp_path, monkeypatch) -> list[str]:
    import diagnose

    monkeypatch.setattr(diagnose, "__file__", str(tmp_path / "diagnose.py"))
    diagnose.LINES.clear()
    diagnose.check_manifest()
    return list(diagnose.LINES)


def test_manifest_check_ok(tmp_path, monkeypatch):
    _write_manifest(tmp_path, {
        "run.py": b"print('x')\n",
        "src/grt/main.py": b"# main\n",
    })
    lines = _run_manifest_check(tmp_path, monkeypatch)
    assert any(ln.startswith("[OK]") for ln in lines)
    assert not any(ln.startswith("[NG]") for ln in lines)


def test_manifest_check_detects_tamper_missing_and_leftover(
        tmp_path, monkeypatch):
    _write_manifest(tmp_path, {
        "run.py": b"original\n",
        "src/grt/main.py": b"# main\n",
        "src/grt/gone.py": b"# removed in new version\n",
    })
    # Tamper one file, delete another, leave an old-version extra.
    (tmp_path / "run.py").write_bytes(b"tampered\n")
    (tmp_path / "src/grt/gone.py").unlink()
    (tmp_path / "src/grt/old_leftover.py").write_bytes(b"# v1.0 remnant\n")
    (tmp_path / "collector/old_contract.json").parent.mkdir(parents=True)
    (tmp_path / "collector/old_contract.json").write_text("{}")
    lines = _run_manifest_check(tmp_path, monkeypatch)
    ng = [ln for ln in lines if ln.startswith("[NG]")]
    assert any("run.py" in ln for ln in ng)
    assert any("gone.py" in ln for ln in ng)
    assert any("old_leftover.py" in ln for ln in ng)
    assert any("collector/old_contract.json" in ln for ln in ng)


def test_manifest_check_absent_manifest_is_advisory(tmp_path, monkeypatch):
    lines = _run_manifest_check(tmp_path, monkeypatch)
    assert any("MANIFEST.sha256 がありません" in ln for ln in lines)
    assert not any(ln.startswith("[NG]") for ln in lines)


def test_manifest_check_ignores_git_metadata(tmp_path, monkeypatch):
    _write_manifest(tmp_path, {"run.py": b"print('x')\n"})
    git_meta = tmp_path / ".git" / "config"
    git_meta.parent.mkdir()
    git_meta.write_text("[core]\n", encoding="utf-8")
    lines = _run_manifest_check(tmp_path, monkeypatch)
    assert any(ln.startswith("[OK]") for ln in lines)
    assert not any(".git/" in ln for ln in lines)
