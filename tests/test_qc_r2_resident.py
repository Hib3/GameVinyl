"""QC Round 2 - resident process, actually running.

Drives the REAL grt.main.run() (BlockingScheduler + poll thread) in a
worker thread with only the network edges stubbed:
- grt.main.fetch_updates -> in-process stub returning [] (no network)
- grt.main.HttpTransport -> MockTransport instance (no network)
- DiscogsFetcher / BlipBlopFetcher -> fakes that record fetch_runs via
  the real health.record_fetch_run and never touch the network

The scheduler is shortened by swapping BlockingScheduler for a
subclass that rewrites every job (interval AND cron) to a 1-second
interval on a single-thread executor, so minutes/hours/daily jobs all
fire repeatedly within a few real seconds — the "minutes-equivalent"
run. All job bodies, the gate checks, outbox, digest and health code
under test are the production ones.

No real tokens: a dummy token string is placed in the environment.
"""
import importlib.util
import json
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from grt import clock
from grt import config as config_mod
from grt import db as dbmod
from grt import health
from grt import main as main_mod
from grt import outbox, watchdog as watchdog_mod
from grt.config import get_setting, set_setting
from grt.telegram_io import MockTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DUMMY_TOKEN = "123456789:AA_QC-R2-DUMMY-TOKEN-0000000000000"


# ---------------------------------------------------------------------------
# resident harness

class _Resident:
    def __init__(self, tmp_path, monkeypatch, preseed=None):
        self.db_file = tmp_path / "grt.db"
        self.transport = MockTransport()
        self.calls = {"discogs": 0, "blip_run": 0, "blip_register": 0}
        self.captured = {}
        self.thread = None
        monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("GRT_DB_PATH", str(self.db_file))
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
        monkeypatch.setenv("DISCOGS_TOKEN", "QC-R2-DUMMY-DISCOGS")
        monkeypatch.delenv("GRT_TEST_CLOCK_OFFSET_SECONDS", raising=False)

        if preseed is not None:
            db = dbmod.connect(self.db_file)
            dbmod.init_schema(db)
            preseed(db)
            db.close()

        calls = self.calls

        class FakeDiscogs:
            def __init__(self, db, source_id):
                self.db, self.source_id = db, source_id

            def run_watchlist(self):
                calls["discogs"] += 1
                health.record_fetch_run(
                    self.db, self.source_id, result="OK", items_extracted=2)
                return {"enabled": True, "snapshotted": 2}

        class FakeBlipBlop:
            feed_url = "https://blipblop.net/feed/"

            def __init__(self, db, source_id):
                self.db, self.source_id = db, source_id

            def run(self, url):
                calls["blip_run"] += 1
                health.record_fetch_run(
                    self.db, self.source_id, result="OK", items_extracted=1)
                return {"ok": True, "items": [{
                    "title": "QC R2 Candidate",
                    "url": "https://example.invalid/qc-r2",
                    "summary": "stub feed item"}]}

            def register_candidates(self, items):
                calls["blip_register"] += 1
                for item in items:
                    self.db.execute(
                        "INSERT OR IGNORE INTO candidate_editions("
                        " title, raw_title, source_url, fingerprint, status,"
                        " found_at) VALUES(?, ?, ?, 'qc-r2-fp', 'pending', ?)",
                        (item["title"], item["title"], item["url"],
                         clock.now_iso(self.db)))
                self.db.commit()
                return {"created": 1, "notified_immediate": 0}

        def fake_fetch_updates(token, offset=None, timeout=25):
            assert token == DUMMY_TOKEN
            time.sleep(0.05)  # long-poll stand-in: block briefly, no updates
            return []

        from apscheduler.executors.pool import ThreadPoolExecutor

        captured = self.captured
        real_scheduler_cls = main_mod.BlockingScheduler

        class FastScheduler(real_scheduler_cls):
            def __init__(self, *a, **kw):
                kw["executors"] = {"default": ThreadPoolExecutor(1)}
                kw["job_defaults"] = {"coalesce": True, "max_instances": 1,
                                      "misfire_grace_time": 5}
                super().__init__(*a, **kw)
                captured["scheduler"] = self

            def add_job(self, func, trigger=None, **kw):
                job_id = kw.pop("id", None)
                captured.setdefault("job_specs", {})[job_id] = dict(kw)
                kw.clear()  # drop interval/cron trigger arguments
                return super().add_job(func, "interval", seconds=1,
                                       id=job_id)

        monkeypatch.setattr(main_mod, "DiscogsFetcher", FakeDiscogs)
        monkeypatch.setattr(main_mod, "BlipBlopFetcher", FakeBlipBlop)
        monkeypatch.setattr(main_mod, "HttpTransport",
                            lambda *a, **k: self.transport)
        monkeypatch.setattr(main_mod, "fetch_updates", fake_fetch_updates)
        monkeypatch.setattr(main_mod, "BlockingScheduler", FastScheduler)

    def start(self):
        self.thread = threading.Thread(target=main_mod.run, daemon=True,
                                       name="qc-r2-resident")
        self.thread.start()
        for _ in range(200):
            sched = self.captured.get("scheduler")
            if sched is not None and sched.running:
                return sched
            time.sleep(0.05)
        raise AssertionError("resident scheduler did not start")

    def stop(self):
        sched = self.captured.get("scheduler")
        if sched is not None and sched.running:
            sched.shutdown(wait=False)
        if self.thread is not None:
            self.thread.join(timeout=15)
            assert not self.thread.is_alive(), "run() did not return"

    def snapshot(self):
        db = dbmod.connect(self.db_file)
        try:
            return db
        except Exception:
            raise

    def query(self, sql, args=()):
        db = dbmod.connect(self.db_file)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()


def _wait_for(predicate, timeout=10.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _sweep_deadline(db):
    """A confirmed deadline inside the next sweep horizon."""
    now = clock.now(db)
    horizon = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if now.hour >= 7:
        horizon += timedelta(days=1)
    if horizon - now < timedelta(minutes=25):
        deadline = horizon - timedelta(minutes=5)
    else:
        deadline = now + timedelta(minutes=20)
    return clock.to_iso(deadline)


def _preseed_open_gate(db):
    set_setting(db, "allowed_chat_id", "111")
    set_setting(db, "deadline_override_quiet", "0")
    set_setting(db, "deadline_override_quiet_selected", "1")
    # heartbeat 3 minutes old: alive-ish, below the startup-gap threshold
    recent = clock.to_iso(clock.now(db) - timedelta(minutes=3))
    db.execute(
        "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
        " VALUES(1, ?, ?, ?)", (recent, recent, recent))
    # a stale source the fetchers never touch: health job must DOWN it
    cur = db.execute(
        "INSERT INTO sources(name, kind, status) VALUES('stale-src', 'db',"
        " 'approved')")
    sid = cur.lastrowid
    old = clock.to_iso(clock.now(db) - timedelta(hours=25))
    db.execute(
        "INSERT INTO fetch_runs(source_id, started_at, finished_at, result,"
        " items_extracted) VALUES(?, ?, ?, 'OK', 4)", (sid, old, old))
    db.execute(
        "INSERT INTO source_health(source_id, state, consecutive_failures,"
        " last_success_at) VALUES(?, 'HEALTHY', 0, ?)", (sid, old))
    # a confirmed deadline for the 21:45 sweep
    db.execute(
        "INSERT INTO release_schedule(edition_id, channel, deadline_raw,"
        " deadline_jst, deadline_confidence, deadline_kind)"
        " VALUES('AR-000001', 'qc', 'raw', ?, 'confirmed', 'order_close')",
        (_sweep_deadline(db),))
    # one due outbox row for the per-minute outbox job
    outbox.enqueue(db, event_type="new_listing", body="QC R2 即時通知",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.invalid/listing/1",
                   quiet_exempt=True)
    db.commit()


# ---------------------------------------------------------------------------
# 1. All scheduler jobs actually fire, with DB evidence

def test_resident_all_jobs_fire_with_db_evidence(tmp_path, monkeypatch):
    from conftest import set_clock_to
    set_clock_to(12)
    res = _Resident(tmp_path, monkeypatch, preseed=_preseed_open_gate)
    sched = res.start()
    try:
        # every production job is registered under its production id
        job_ids = {j.id for j in sched.get_jobs()}
        # v2.0: Phase 2/3 jobs join the Phase 1 set (storewatch,
        # ingest, deadline_alerts, arrival_check, bundling,
        # variant_sweep, prefix_sweep, doujin_harvest).
        # v2.0.2: + webpages (no-API feed/sitemap/structured job).
        # v2.0.3 (review fix): + goods — the GOODS fetcher had no
        # production job at all until the adversarial-QA round.
        # W-check B2: + maintenance — the weekly retention/disk-guard
        # job (seller D-1/D-5/D-6: nothing was ever pruned and no one
        # watched the disk).
        # W-check B2 R9: + daily_catchup — the hourly digest/sweep
        # catch-up (adversarial QA D-4 residual: a live process that
        # slept past a daily slot's grace only recorded the miss).
        assert job_ids == {"heartbeat", "outbox", "health", "discogs",
                           "blipblop", "digest", "sweep",
                           "storewatch", "ingest", "deadline_alerts",
                           "arrival_check", "bundling", "variant_sweep",
                           "prefix_sweep", "doujin_harvest", "webpages",
                           "goods", "maintenance", "daily_catchup",
                           "yahoo_used"}

        ok = _wait_for(lambda: (
            res.calls["discogs"] >= 1 and res.calls["blip_run"] >= 1
            and res.calls["blip_register"] >= 1
            and res.query(
                "SELECT COUNT(*) FROM system_events"
                " WHERE kind='source_down'")[0][0] >= 1
            and res.query(
                "SELECT COUNT(*) FROM notification_outbox"
                " WHERE event_type='deadline_sweep'"
                " AND state='sent'")[0][0] >= 1
            and res.query(
                "SELECT COUNT(*) FROM notification_outbox"
                " WHERE event_type='health_digest'"
                " AND state='sent'")[0][0] >= 1), timeout=15)
        assert ok, (
            f"jobs did not all fire: calls={res.calls}, "
            f"events={res.query('SELECT kind, COUNT(*) FROM system_events GROUP BY kind')}, "
            f"outbox={res.query('SELECT event_type, state, COUNT(*) FROM notification_outbox GROUP BY 1,2')}")

        # heartbeat job: alive_at advanced past the preseeded value and
        # the outbox job stamped last_job_at
        hb = res.query("SELECT alive_at, last_job_at, boot_at FROM heartbeat"
                       " WHERE id=1")[0]
        assert hb[0] is not None and hb[1] is not None and hb[2] is not None
        preseed_recent = clock.now() - timedelta(minutes=3)
        assert clock.parse_iso(hb[0]) > preseed_recent

        # fetch ledger: both stub fetchers recorded real fetch_runs rows
        kinds = {r[0] for r in res.query(
            "SELECT s.name FROM fetch_runs f JOIN sources s ON s.id=f.source_id"
            " WHERE f.result='OK'")}
        assert {"discogs", "blipblop"} <= kinds

        # digest job: both digests enqueued once per day (idempotent) and
        # actually delivered through the stub transport
        bodies = [t for _, t in res.transport.sent]
        assert any("日次ヘルスレポート" in b for b in bodies)
        assert any("発見ダイジェスト" in b for b in bodies)
        assert any("QC R2 即時通知" in b for b in bodies)  # outbox job
        assert any("締切間近" in b for b in bodies)        # sweep job
        digests = res.query(
            "SELECT event_type, COUNT(*) FROM notification_outbox"
            " WHERE event_type IN ('health_digest','discovery_digest')"
            " GROUP BY 1")
        assert {r[0]: r[1] for r in digests} == {
            "health_digest": 1, "discovery_digest": 1}
    finally:
        res.stop()


# ---------------------------------------------------------------------------
# 2. Setup gate: fetch jobs stay off while closed, start when opened

def _preseed_closed_gate(db):
    set_setting(db, "allowed_chat_id", "111")
    # gate deliberately NOT selected
    outbox.enqueue(db, event_type="new_listing", body="ゲート中の通知",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.invalid/listing/gate",
                   quiet_exempt=True)
    db.commit()


def test_gate_closed_blocks_fetch_jobs_then_open_starts_them(
        tmp_path, monkeypatch):
    res = _Resident(tmp_path, monkeypatch, preseed=_preseed_closed_gate)
    res.start()
    try:
        time.sleep(2.5)  # several 1-second job rounds with the gate closed
        assert res.calls == {"discogs": 0, "blip_run": 0, "blip_register": 0}
        assert res.query(
            "SELECT COUNT(*) FROM fetch_runs")[0][0] == 0
        assert res.query(
            "SELECT COUNT(*) FROM notification_outbox"
            " WHERE event_type IN ('health_digest','discovery_digest')"
            )[0][0] == 0
        row = res.query("SELECT state FROM notification_outbox")[0]
        assert row[0] == "pending", "outbox must not send while gate closed"
        assert res.transport.sent == []
        # ... while the ungated heartbeat still proves the process lives
        assert res.query("SELECT COUNT(*) FROM heartbeat WHERE id=1"
                         )[0][0] == 1

        # open the gate the way the bot's quiet choice would (settings)
        db = dbmod.connect(res.db_file)
        set_setting(db, "deadline_override_quiet", "0")
        set_setting(db, "deadline_override_quiet_selected", "1")
        db.close()

        ok = _wait_for(lambda: (
            res.calls["discogs"] >= 1 and res.calls["blip_run"] >= 1
            and res.query(
                "SELECT state FROM notification_outbox"
                " WHERE body='ゲート中の通知'")[0][0] == "sent"), timeout=15)
        assert ok, f"jobs did not start after gate opened: {res.calls}"
        assert any("ゲート中の通知" in t for _, t in res.transport.sent)
    finally:
        res.stop()


# ---------------------------------------------------------------------------
# 3. 9-A2(3): startup gap detection

def test_record_startup_gap_unit(db):
    old = clock.to_iso(clock.now(db) - timedelta(hours=2))
    health.touch_heartbeat(db)
    db.execute("UPDATE heartbeat SET alive_at=?, boot_at=? WHERE id=1",
               (old, old))
    db.commit()
    detail = main_mod.record_startup_gap(db)
    assert detail is not None
    assert detail["previous_alive_at"] == old
    assert detail["gap_seconds"] >= 2 * 3600 - 5
    assert detail["missed_heartbeat_intervals"] >= 20
    gaps = db.execute("SELECT scope, gap_start, gap_end, declared_at"
                      " FROM observation_gaps").fetchall()
    assert len(gaps) == 1
    assert gaps[0][0] == "main_process" and gaps[0][1] == old
    assert gaps[0][2] is not None and gaps[0][3] is not None
    ev = db.execute("SELECT detail_json FROM system_events"
                    " WHERE kind='startup_observation_gap'").fetchall()
    assert len(ev) == 1 and json.loads(ev[0][0])["gap_minutes"] >= 119
    # idempotent: recording the same gap again changes nothing
    assert main_mod.record_startup_gap(db) is None
    assert db.execute("SELECT COUNT(*) FROM observation_gaps"
                      ).fetchone()[0] == 1


def test_record_startup_gap_fresh_or_missing_heartbeat_is_silent(db):
    assert main_mod.record_startup_gap(db) is None  # no heartbeat row yet
    health.touch_heartbeat(db)  # fresh heartbeat (just now)
    assert main_mod.record_startup_gap(db) is None
    recent = clock.to_iso(clock.now(db) - timedelta(minutes=9))
    db.execute("UPDATE heartbeat SET alive_at=? WHERE id=1", (recent,))
    db.commit()
    assert main_mod.record_startup_gap(db) is None  # <= 2x interval: no gap
    assert db.execute("SELECT COUNT(*) FROM observation_gaps"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM system_events"
                      " WHERE kind='startup_observation_gap'"
                      ).fetchone()[0] == 0


def _preseed_stale_heartbeat(db):
    set_setting(db, "allowed_chat_id", "111")
    stale = clock.to_iso(clock.now(db) - timedelta(hours=3))
    db.execute(
        "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
        " VALUES(1, ?, ?, ?)", (stale, stale, stale))
    db.commit()


def test_resident_startup_records_gap_then_restart_is_clean(
        tmp_path, monkeypatch):
    res = _Resident(tmp_path, monkeypatch, preseed=_preseed_stale_heartbeat)
    res.start()
    try:
        ok = _wait_for(lambda: res.query(
            "SELECT COUNT(*) FROM observation_gaps"
            " WHERE scope='main_process'")[0][0] == 1, timeout=10)
        assert ok, "startup did not record the observation gap"
        gap = res.query("SELECT gap_start, gap_end FROM observation_gaps")[0]
        assert gap[1] > gap[0]
        ev = res.query("SELECT detail_json FROM system_events"
                       " WHERE kind='startup_observation_gap'")
        assert len(ev) == 1
        assert json.loads(ev[0][0])["gap_minutes"] >= 170
        # heartbeat now belongs to the new boot
        hb = res.query("SELECT alive_at, boot_at FROM heartbeat")[0]
        assert hb[0] == hb[1] or hb[0] >= hb[1]
    finally:
        res.stop()
    # Immediate second boot: heartbeat is fresh, so NO second gap.
    res2 = _Resident(tmp_path, monkeypatch)
    # reuse the same DB file the first resident wrote
    res2.db_file = res.db_file
    res2.start()
    try:
        time.sleep(2.0)
        assert res2.query(
            "SELECT COUNT(*) FROM observation_gaps"
            " WHERE scope='main_process'")[0][0] == 1
    finally:
        res2.stop()


# ---------------------------------------------------------------------------
# 4. Watchdog (independent process side), actually run

def test_watchdog_stale_detection_restart_and_recovery(db):
    set_setting(db, "allowed_chat_id", "111")
    health.touch_heartbeat(db)
    stale = clock.to_iso(clock.now(db) - timedelta(minutes=30))
    db.execute("UPDATE heartbeat SET alive_at=? WHERE id=1", (stale,))
    db.commit()
    restarts = []
    result = watchdog_mod.check(db, restart=lambda: restarts.append(1) or True)
    assert result["status"] == "anomaly"
    assert result["restart_attempted"] is True and result["restart_ok"] is True
    assert restarts == [1]
    kinds = [r[0] for r in db.execute(
        "SELECT kind FROM system_events ORDER BY id").fetchall()]
    assert "watchdog_anomaly" in kinds and "watchdog_restart" in kinds

    # second stale tick: no duplicate anomaly for the same alive_at
    watchdog_mod.check(db, restart=lambda: True)
    assert db.execute("SELECT COUNT(*) FROM system_events"
                      " WHERE kind='watchdog_anomaly'").fetchone()[0] == 1

    # main process returns: fresh heartbeat -> recovery notice queued
    health.touch_heartbeat(db)
    result = watchdog_mod.check(db, restart=lambda: True)
    assert result["status"] == "recovered"
    row = db.execute("SELECT body, state FROM notification_outbox"
                     " WHERE event_type='recovery_notice'").fetchone()
    assert row is not None and "停止期間" in row[0] and stale in row[0]
    t = MockTransport()
    outbox.process_outbox(db, t)
    assert any("復旧通知" in b for _, b in t.sent)


def test_watchdog_module_main_entrypoint(tmp_path, monkeypatch, capsys):
    db_file = tmp_path / "grt.db"
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRT_DB_PATH", str(db_file))
    db = dbmod.connect(db_file)
    dbmod.init_schema(db)
    health.touch_heartbeat(db)
    stale = clock.to_iso(clock.now(db) - timedelta(minutes=45))
    db.execute("UPDATE heartbeat SET alive_at=? WHERE id=1", (stale,))
    db.commit()
    db.close()
    restarts = []
    monkeypatch.setattr(watchdog_mod, "_default_restart",
                        lambda: restarts.append(1) or True)
    watchdog_mod.main()  # the function the root watchdog.py delegates to
    printed = capsys.readouterr().out
    assert "anomaly" in printed
    assert restarts == [1]
    db = dbmod.connect(db_file)
    try:
        assert db.execute("SELECT COUNT(*) FROM system_events"
                          " WHERE kind='watchdog_anomaly'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM system_events"
                          " WHERE kind='watchdog_restart'").fetchone()[0] == 1
    finally:
        db.close()
    # root entry script is a thin delegate to that same main()
    root_src = (PROJECT_ROOT / "watchdog.py").read_text(encoding="utf-8")
    assert "from grt.watchdog import main" in root_src


# ---------------------------------------------------------------------------
# R6-D7: .env allowlist (resident startup loads .env via config)

def test_dotenv_allowlist_imports_only_production_keys(
        tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=" + DUMMY_TOKEN + "\n"
        "DISCOGS_TOKEN=QC-DUMMY-DISCOGS\n"
        "GRT_TEST_CLOCK_OFFSET_SECONDS=999999\n"
        "EVIL_UNKNOWN_KEY=pwned\n"
        "ALLOWED_CHAT_ID=424242\n", encoding="utf-8")
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    for key in ("TELEGRAM_BOT_TOKEN", "DISCOGS_TOKEN",
                "GRT_TEST_CLOCK_OFFSET_SECONDS", "EVIL_UNKNOWN_KEY",
                "ALLOWED_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    try:
        config_mod.load_dotenv()
        assert config_mod.os.environ["TELEGRAM_BOT_TOKEN"] == DUMMY_TOKEN
        assert config_mod.os.environ["DISCOGS_TOKEN"] == "QC-DUMMY-DISCOGS"
        # the test-clock backdoor and unknown keys must NOT be imported
        assert "GRT_TEST_CLOCK_OFFSET_SECONDS" not in config_mod.os.environ
        assert "EVIL_UNKNOWN_KEY" not in config_mod.os.environ
        # spec section 11: the allowlist lives in DB settings, not env
        assert "ALLOWED_CHAT_ID" not in config_mod.os.environ
        assert clock._offset_seconds() == 0
    finally:
        for key in ("TELEGRAM_BOT_TOKEN", "DISCOGS_TOKEN"):
            config_mod.os.environ.pop(key, None)


def test_clock_offset_legitimate_routes_still_work(db, monkeypatch):
    # (a) settings-table route (the spec's test_clock_offset_seconds)
    set_setting(db, "test_clock_offset_seconds", "3600")
    assert clock._offset_seconds(db) == 3600
    # (b) real environment variable set by the test runner itself
    monkeypatch.setenv("GRT_TEST_CLOCK_OFFSET_SECONDS", "120")
    assert clock._offset_seconds(db) == 120
    monkeypatch.delenv("GRT_TEST_CLOCK_OFFSET_SECONDS")
    # (c) process-local fallback used by tests/conftest.py
    clock.set_local_offset(7)
    try:
        db.execute("DELETE FROM settings"
                   " WHERE key='test_clock_offset_seconds'")
        db.commit()
        assert clock._offset_seconds(db) == 7
    finally:
        clock.set_local_offset(0)


def test_source_jobs_check_due_immediately_and_every_15_minutes(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from conftest import set_clock_to
    set_clock_to(12)
    before = datetime.now(clock.JST)
    resident = _Resident(tmp_path, monkeypatch, preseed=_preseed_open_gate)
    try:
        resident.start()
        after = datetime.now(clock.JST)
        for job in ("discogs", "blipblop", "doujin_harvest"):
            spec = resident.captured["job_specs"][job]
            assert spec["minutes"] == 15
            assert before - timedelta(seconds=1) <= spec["next_run_time"] <= after
    finally:
        resident.stop()


def test_source_jobs_skip_recent_then_run_when_due(tmp_path, monkeypatch):
    from conftest import set_clock_to
    set_clock_to(12)

    def preseed(db):
        _preseed_open_gate(db)
        for name, kind in (("discogs", "db"), ("blipblop", "community")):
            sid = db.execute(
                "INSERT INTO sources(name,kind,status) VALUES(?,?,'approved')",
                (name, kind)).lastrowid
            now = clock.now_iso(db)
            db.execute(
                "INSERT INTO fetch_runs(source_id,started_at,finished_at,result,items_extracted) "
                "VALUES(?,?,?,'OK',1)", (sid, now, now))
        db.commit()

    resident = _Resident(tmp_path, monkeypatch, preseed=preseed)
    try:
        resident.start()
        time.sleep(2.5)
        assert resident.calls["discogs"] == 0
        assert resident.calls["blip_run"] == 0
        with resident.snapshot() as db:
            old = clock.to_iso(clock.now(db) - timedelta(hours=7))
            db.execute("UPDATE fetch_runs SET started_at=? WHERE source_id IN "
                       "(SELECT id FROM sources WHERE name IN ('discogs','blipblop'))",
                       (old,))
            db.commit()
        assert _wait_for(lambda: resident.calls["discogs"] == 1 and
                         resident.calls["blip_run"] == 1, timeout=8)
        time.sleep(1.5)
        assert resident.calls["discogs"] == 1
        assert resident.calls["blip_run"] == 1
    finally:
        resident.stop()
