"""Regression tests: record_startup_gap must never kill startup.

Found by debugger 2/20 (2026-11-08) during the v1.5 field-failure
investigation. record_startup_gap (added in QC round 2) runs in run()'s
prologue, unguarded, BEFORE the poll thread starts. It assumed the
heartbeat row is always perfectly shaped. Every writer shipped
(v1.0-v1.5 touch_heartbeat) writes ISO timestamps and the schema is
identical across versions, so the malformed states below are not
reachable through the app's own writes — but startup bookkeeping must
not be a single point of failure for the whole process:

- alive_at not parseable as ISO -> ValueError propagated out of
  clock.parse_iso (its None-return only covers empty input).
- heartbeat row without last_job_at -> IndexError when the detail
  dict was built.
"""
import sqlite3
from datetime import timedelta

from grt import clock, db as dbmod
from grt import main as main_mod


def _db_with_heartbeat(tmp_path, alive_at):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    db.execute(
        "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
        " VALUES(1, ?, ?, ?)", (alive_at, alive_at, alive_at))
    db.commit()
    return db


def test_garbage_alive_at_returns_none(tmp_path):
    db = _db_with_heartbeat(tmp_path, "not-a-date")
    assert main_mod.record_startup_gap(db) is None


def test_slash_format_alive_at_returns_none(tmp_path):
    db = _db_with_heartbeat(tmp_path, "2026/11/08 07:00:00")
    assert main_mod.record_startup_gap(db) is None


def test_heartbeat_without_last_job_at_column(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE heartbeat(id INTEGER PRIMARY KEY, alive_at TEXT)")
    raw.execute(
        "CREATE TABLE observation_gaps(id INTEGER PRIMARY KEY,"
        " scope TEXT, gap_start TEXT, gap_end TEXT, declared_at TEXT)")
    raw.execute(
        "CREATE TABLE system_events(id INTEGER PRIMARY KEY,"
        " kind TEXT, detail_json TEXT, created_at TEXT)")
    stale = clock.to_iso(clock.now() - timedelta(hours=3))
    raw.execute("INSERT INTO heartbeat VALUES(1, ?)", (stale,))
    raw.commit()
    raw.close()
    db = dbmod.connect(path)
    detail = main_mod.record_startup_gap(db)
    assert detail is not None
    assert detail["last_job_at"] is None
    assert detail["previous_alive_at"] == stale


def test_normal_stale_heartbeat_still_recorded(tmp_path):
    stale = clock.to_iso(clock.now() - timedelta(hours=3))
    db = _db_with_heartbeat(tmp_path, stale)
    detail = main_mod.record_startup_gap(db)
    assert detail is not None
    assert detail["previous_alive_at"] == stale
    # and the fresh/missing cases still quietly do nothing
    assert main_mod.record_startup_gap(db) is None
