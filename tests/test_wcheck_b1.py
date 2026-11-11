"""W-check Block 1 regression tests (Windows install & boot).

Each test pins one defect found by the 7-persona W-check of
v2.0.3 and fixed for v2.0.4:

- .env saved as UTF-16 / Shift_JIS / with BOM (adversarial QA D-1/D-2)
- watchdog dying on an unparseable heartbeat (AI engineer #1)
- watchdog never retrying a spawn whose child died instantly (#2)
- storewatch aborting all sources on one bad source (#3)
- wait_start accepting an old version's START line (maniac A #4)
- task verification ignoring Settings: ExecutionTimeLimit PT72H,
  battery flags, disabled triggers (wholesaler #1, maniac B D-2)
- setup_tasks token check choking on a BOM (adversarial QA D-2)
- DST fold/gap deadlines silently 'confirmed' (wholesaler #3)
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ .env

def test_env_utf16_and_sjis_and_bom(tmp_path, monkeypatch):
    from grt import config
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GRT_DATA_DIR", str(data))
    token = "123456789:AAE_fake-token-shape-for-tests_0123456789"
    body = f"TELEGRAM_BOT_TOKEN={token}\nDISCOGS_TOKEN=discogs-token-0123456789\n"
    for encoding in ("utf-16", "utf-8-sig"):
        (data / ".env").write_bytes(body.encode(encoding))
        import os
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("DISCOGS_TOKEN", None)
        assert config.telegram_bot_token() == token
        assert config.discogs_token() == "discogs-token-0123456789"
    # Shift_JIS with a Japanese comment line must not raise either.
    sjis = ("# トークン設定\n" + body).encode("cp932")
    (data / ".env").write_bytes(sjis)
    import os
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    assert config.telegram_bot_token() == token


def test_env_bomless_utf16_and_directory(tmp_path, monkeypatch):
    # R4 residuals (adversarial QA): BOM-less UTF-16 saved by some
    # editors, and .env being a directory, must both degrade
    # honestly instead of raising or silently misreading.
    from grt import config
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GRT_DATA_DIR", str(data))
    token = "123456789:AAE_fake-token-shape-for-tests_0123456789"
    (data / ".env").write_bytes(
        f"TELEGRAM_BOT_TOKEN={token}\n".encode("utf-16-le"))
    import os
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    assert config.telegram_bot_token() == token
    (data / ".env").unlink()
    (data / ".env").mkdir()
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    assert config.telegram_bot_token() is None


def test_watchdog_recovery_with_garbage_anomaly_detail(
        tmp_path, monkeypatch):
    # R4 observation (AI engineer): a damaged watchdog_anomaly
    # detail_json must not crash the recovery branch every tick.
    import json as _json
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    from grt import clock, db as dbmod
    from grt import watchdog
    db = dbmod.connect()
    dbmod.init_schema(db)
    now = clock.now(db)
    fresh = clock.to_iso(now)
    _seed_hb(db, fresh)
    db.execute(
        "INSERT INTO system_events(kind, detail_json, created_at)"
        " VALUES('watchdog_anomaly', 'GARBAGE-NOT-JSON', ?)", (fresh,))
    db.commit()
    result = watchdog.check(db, restart=lambda: True)
    assert result["status"] == "recovered"
    _ = _json


def test_setup_tasks_tokens_ready_with_bom(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    token = "123456789:AAE_fake-token-shape-for-tests_0123456789"
    (data / ".env").write_bytes(
        f"TELEGRAM_BOT_TOKEN={token}\n".encode("utf-8-sig"))
    monkeypatch.setenv("GRT_DATA_DIR", str(data))
    st = _load_module("setup_tasks_b1", ROOT / "setup_tasks.py")
    assert st.tokens_ready() == 0


def test_setup_tasks_stamp_failure_aborts_start(tmp_path, monkeypatch):
    st = _load_module("setup_tasks_b1b", ROOT / "setup_tasks.py")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setattr(st, "START_REQUEST_FILE",
                        blocker / "logs" / "start_requested_at.txt")
    monkeypatch.setattr(st, "MODE_FILE", tmp_path / "mode.txt")
    assert st.start_now() == 1


# -------------------------------------------------------------- watchdog

def _seed_hb(db, alive_at: str):
    from grt import db as dbmod
    dbmod.init_schema(db)
    db.execute("INSERT OR REPLACE INTO heartbeat(id, alive_at, boot_at)"
               " VALUES(1, ?, ?)", (alive_at, alive_at))
    db.commit()


def test_watchdog_survives_garbage_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    from grt import db as dbmod
    from grt import watchdog
    db = dbmod.connect()
    _seed_hb(db, "not-a-date")
    result = watchdog.check(db, restart=lambda: True)
    assert result["status"] == "anomaly"
    assert result.get("alive_at_unparseable") is True
    assert result["restart_attempted"] is True


def test_watchdog_retries_after_retry_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    from grt import clock, db as dbmod
    from grt import watchdog
    db = dbmod.connect()
    _seed_hb(db, "2020-01-01T00:00:00+09:00")
    calls = {"n": 0}

    def spawn():
        calls["n"] += 1
        return True

    first = watchdog.check(db, restart=spawn)
    assert first["restart_attempted"] is True and calls["n"] == 1
    # Immediately again: still within the retry interval -> skipped.
    second = watchdog.check(db, restart=spawn)
    assert second.get("restart_skipped_duplicate") is True
    assert calls["n"] == 1
    # Age the recorded attempt beyond the retry interval: the child
    # evidently died instantly (heartbeat never moved), so the
    # watchdog must try again instead of giving up forever.
    db.execute(
        "UPDATE system_events SET created_at='2020-01-01T00:00:00+09:00'"
        " WHERE kind='watchdog_restart'")
    db.commit()
    third = watchdog.check(db, restart=spawn)
    assert third["restart_attempted"] is True and calls["n"] == 2
    _ = clock  # silence linter; clock used implicitly via check()


# ------------------------------------------------------------ storewatch

def test_storewatch_one_bad_source_does_not_starve_others(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    from grt import db as dbmod
    from grt import storewatch
    db = dbmod.connect()
    dbmod.init_schema(db)
    for name in ("BadShop", "GoodShop"):
        db.execute(
            "INSERT INTO sources(name, url, kind, status)"
            " VALUES(?, 'https://example.invalid/', 'STORE',"
            " 'approved')", (name,))
    db.commit()

    class _RespNoText:
        status_code = 200
        headers = {}

    class _Client:
        def get(self, url, **kw):
            if "BadShop" in url:
                return _RespNoText()
            class R:
                status_code = 200
                headers = {}
                text = "<html><body>shop page</body></html>"
            return R()

    # Make URLs distinguishable per source.
    rows = db.execute("SELECT id, name FROM sources").fetchall()
    for r in rows:
        db.execute("UPDATE sources SET url=? WHERE id=?",
                   (f"https://example.invalid/{r['name']}", r["id"]))
    db.commit()
    results = storewatch.run_all(db, client=_Client())
    by_name = {}
    src_names = {r["id"]: r["name"] for r in rows}
    for res in results:
        by_name[src_names[res.get("source_id")]] = res
    assert by_name["BadShop"]["ok"] is False
    assert by_name["GoodShop"]["ok"] is True
    events = db.execute(
        "SELECT COUNT(*) FROM system_events"
        " WHERE kind='storewatch_error'").fetchone()[0]
    assert events == 1


# ------------------------------------------------------------- wait_start

def test_wait_start_rejects_old_version_evidence(tmp_path, monkeypatch):
    ws = _load_module("wait_start_b1", ROOT / "wait_start.py")
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    monkeypatch.setattr(ws, "DATA_DIR", data)
    import time as _time
    (data / "logs" / "start_requested_at.txt").write_text(
        str(_time.time() - 3600), encoding="utf-8")
    (data / "logs" / "startup.log").write_text(
        "[2020-01-01T00:00:00+09:00] START version=0.0.0-old "
        "python=3.12 pid=1 db=x note=boot\n", encoding="utf-8")
    since = ws._since()
    assert ws._startup_log_evidence(since) is None
    # A line from THIS package's version, after the stamp, counts.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone(timedelta(hours=9)))
    (data / "logs" / "start_requested_at.txt").write_text(
        str(_time.time() - 10), encoding="utf-8")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    (data / "logs" / "startup.log").write_text(
        f"[{now.isoformat(timespec='seconds')}] START "
        f"version={version} python=3.12 pid=2 db=x note=boot\n",
        encoding="utf-8")
    assert ws._startup_log_evidence(ws._since()) is not None


# ------------------------------------------------------- task XML checks

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>{trig}</Enabled></LogonTrigger></Triggers>
  <Settings><Enabled>true</Enabled>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <DisallowStartIfOnBatteries>{disallow}</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>{stop}</StopIfGoingOnBatteries>
  </Settings>
  <Actions><Exec><Command>"C:\\x\\.venv\\Scripts\\python.exe"</Command>
  <Arguments>"C:\\x\\run.py"</Arguments></Exec></Actions>
</Task>"""


@pytest.mark.parametrize("limit,disallow,stop,trig,expect_problems", [
    ("PT72H", "true", "true", "true", 3),   # classic schtasks defaults
    ("PT0S", "false", "false", "true", 0),  # the XML/PS routes' settings
    ("PT0S", "false", "false", "false", 0),  # settings fine; trigger off
])
def test_parse_task_xml_settings(limit, disallow, stop, trig,
                                 expect_problems):
    st = _load_module("setup_tasks_b1c", ROOT / "setup_tasks.py")
    dg = _load_module("diagnose_b1", ROOT / "diagnose.py")
    xml = _TASK_XML.format(limit=limit, disallow=disallow, stop=stop,
                           trig=trig)
    info = st.parse_task_xml(xml)
    assert len(st.settings_problems(info)) == expect_problems
    # The diagnose copy must agree (the two implementations are
    # duplicates by design; this test is the anti-drift pin).
    dinfo = dg.parse_task_xml(xml)
    assert len(dg.task_settings_problems(dinfo)) == expect_problems
    assert dinfo["execution_time_limit"] == info["execution_time_limit"]
    if trig == "false":
        assert info["enabled"] is False
        assert dinfo["enabled"] is False


def test_task_xml_route1_carries_resident_settings():
    st = _load_module("setup_tasks_b1d", ROOT / "setup_tasks.py")
    xml = st.task_xml("GameRecordTool", "run.py")
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<DisallowStartIfOnBatteries>false" in xml
    info = st.parse_task_xml(xml)
    assert st.settings_problems(info) == []
    wd = st.task_xml("GameRecordToolWatchdog", "watchdog.py")
    assert "<Interval>PT15M</Interval>" in wd
    collector = st.parse_task_xml(st.task_xml("GameRecordToolCollector", "collector/server.py"))
    assert "LogonTrigger" in st.task_xml("GameRecordToolCollector", "collector/server.py")
    site_xml = st.task_xml("GameRecordToolSiteFreshness", "site/update_site.py")
    site = st.parse_task_xml(site_xml)
    assert site["trigger_intervals"] == ["PT1H"]
    start = re.search(r"<StartBoundary>([^<]+)</StartBoundary>", site_xml)[1]
    assert datetime.fromisoformat(start) >= datetime.now() + timedelta(minutes=55)
    for task in (info, st.parse_task_xml(wd), collector, site):
        assert Path(task["command"]).name == "pythonw.exe"
    assert Path(collector["arguments"].strip('"')).name == "server.py"
    assert Path(site["arguments"].strip('"')).name == "update_site.py"
    assert "pythonw.exe" in (ROOT / "register_tasks.ps1").read_text(encoding="utf-8")
    assert ".AddHours(1)" in (ROOT / "register_tasks.ps1").read_text(encoding="utf-8")


# ------------------------------------------------------------------- DST

def test_dst_fold_deadline_becomes_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    from grt import db as dbmod
    from grt import deadlines_ops
    db = dbmod.connect()
    dbmod.init_schema(db)
    # 2026-11-01 01:30 in New York happens twice (EDT then EST).
    sid = deadlines_ops.set_schedule(
        db, channel="test", deadline_raw="2026-11-01 01:30",
        deadline_tz="America/New_York",
        deadline_jst="2026-11-01T01:30:00",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    row = db.execute(
        "SELECT deadline_confidence, deadline_jst FROM release_schedule"
        " WHERE id=?", (sid,)).fetchone()
    assert row["deadline_confidence"] == "ambiguous"
    # An ordinary wall time stays exactly as the caller filed it.
    sid2 = deadlines_ops.set_schedule(
        db, channel="test", deadline_raw="2026-12-01 23:59",
        deadline_tz="America/New_York",
        deadline_jst="2026-12-01T23:59:00",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    row2 = db.execute(
        "SELECT deadline_confidence, deadline_jst FROM release_schedule"
        " WHERE id=?", (sid2,)).fetchone()
    assert row2["deadline_confidence"] == "confirmed"
    assert row2["deadline_jst"] == "2026-12-02T13:59:00+09:00"
