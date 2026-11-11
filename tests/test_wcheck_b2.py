"""W-check Block 2 regression tests (resident operation / scheduler /
health layer), 2026-11-08.

Each test pins one defect found in the R6 seven-persona review and
fixed in the R8 pass, so the defect cannot silently return:

- outbox: retry schedule reaches the 12h step (maniac B D-1);
  in-process stale-'sending' recovery (C-B2-5/D-2); crash-loop
  attempt budget (D-5); atomic claim (C-B2-4/D-4); BaseException
  containment shape; send-time quiet re-judgement (C-B2-6/D-6);
  daytime quiet window math (D-7); enqueue savepoint isolation
  (D-8); dead-letter revival (C-B2-3/D-9(iii)); fallback key hash
  (D-10); URL key normalisation (D-11); priority order + batch
  limit (C-B2-9/D-13); chunking of over-limit bodies (C-B2-2);
  retry_after honoured (adversarial D-5).
- telegram_io / bot / poll: TelegramError detail parsing (D-5);
  fetch_updates shape validation (D-9); transient send failures
  un-record the update (D-3).
- main: scheduler misfire grace + listener + maintenance job
  registration (wholesaler D-2/D-11).
- discogs lane: BLOCKED aborts the run (maniac A D-1); partial
  success recorded honestly + ladder fed (D-2); empty stats payload
  rejected (D-9); appearance episodes re-notify (C-B2-1).
- storewatch: runs are ledgered, ladder fed, is_due real (D-4).
- health: zero-item anomaly for small sources (wholesaler D-5);
  DOWN + silence immediate alerts (D-6); gaps close (D-4b);
  blocked shelf enforced by BaseFetcher (maniac A D-8).
- freqpolicy: cadence column written (D-4a); consec banking capped
  (D-10).
- digest: corrupt found_at tolerated (C-B2-8); sweep horizon
  follows the configured quiet end (C-B2-7).
- deadlines: missed guarantee_cutoff declared (wholesaler D-3).
- matcher: match_review rows dedupe while unresolved (seller D-2).
- pricedrop: a re-drop after recovery notifies again (D-9(i)).
- maintenance: telemetry pruning + disk guard (seller D-1/D-5/D-6).
"""
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from conftest import advance, set_clock_to
from grt import bot as bot_mod
from grt import clock, digest, freqpolicy, health, maintenance, matcher
from grt import main as main_mod
from grt import outbox, pricedrop, storewatch
from grt import db as dbmod
from grt.config import set_setting
from grt.fetchers import discogs as discogs_mod
from grt.fetchers.base import BaseFetcher
from grt.fetchers.discogs import DiscogsFetcher
from grt.telegram_io import (HttpTransport, MockTransport, TelegramError,
                             fetch_updates)


def _chat(db):
    set_setting(db, "allowed_chat_id", "111")


def _source(db, name="src", kind="STORE", status="approved",
            url="https://example.invalid/shop"):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url)"
        " VALUES(?, ?, ?, ?)", (name, kind, status, url))
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# outbox state machine
# ---------------------------------------------------------------------------

def test_retry_schedule_uses_all_five_delays(db):
    _chat(db)
    outbox.enqueue(db, event_type="x", body="b", idempotency_key="k1",
                   quiet_exempt=True)
    t = MockTransport()
    t.fail_next(99)
    for i, mins in enumerate([1, 5, 30, 120, 720]):
        outbox.process_outbox(db, t)
        row = db.execute("SELECT attempts, state FROM notification_outbox"
                         " WHERE idempotency_key='k1'").fetchone()
        assert (row[0], row[1]) == (i + 1, "failed_retryable")
        advance(mins * 60)
    outbox.process_outbox(db, t)
    row = db.execute("SELECT attempts, state FROM notification_outbox"
                     " WHERE idempotency_key='k1'").fetchone()
    assert (row[0], row[1]) == (6, "dead_letter")


def test_dead_letter_revived_on_reenqueue(db):
    _chat(db)
    oid = outbox.enqueue(db, event_type="candidate_review", body="v1",
                         idempotency_key="candidate_review|42",
                         quiet_exempt=True)
    db.execute("UPDATE notification_outbox SET state='dead_letter',"
               " attempts=6 WHERE id=?", (oid,))
    db.commit()
    again = outbox.enqueue(db, event_type="candidate_review", body="v2",
                           idempotency_key="candidate_review|42",
                           quiet_exempt=True)
    assert again == oid  # same row, second life — not a silent None
    row = db.execute("SELECT state, attempts, body FROM notification_outbox"
                     " WHERE id=?", (oid,)).fetchone()
    assert (row[0], row[1], row[2]) == ("pending", 0, "v2")
    t = MockTransport()
    assert outbox.process_outbox(db, t)["sent"] == 1


def test_sent_key_stays_suppressed(db):
    _chat(db)
    oid = outbox.enqueue(db, event_type="x", body="b",
                         idempotency_key="k-sent", quiet_exempt=True)
    t = MockTransport()
    outbox.process_outbox(db, t)
    assert outbox.enqueue(db, event_type="x", body="b",
                          idempotency_key="k-sent",
                          quiet_exempt=True) is None


def test_enqueue_savepoint_isolates_caller_transaction(db):
    _chat(db)
    outbox.enqueue(db, event_type="x", body="b", idempotency_key="dup",
                   quiet_exempt=True)
    # caller begins its own unit of work
    db.execute("INSERT INTO settings(key, value) VALUES('caller', 'work')")
    # a duplicate enqueue must NOT roll the caller's row back (D-8a)
    assert outbox.enqueue(db, event_type="x", body="b",
                          idempotency_key="dup",
                          quiet_exempt=True) is None
    assert db.execute("SELECT COUNT(*) FROM settings WHERE key='caller'"
                      ).fetchone()[0] == 1
    # a successful enqueue must NOT commit the caller's work early (D-8b)
    assert outbox.enqueue(db, event_type="x", body="b2",
                          idempotency_key="fresh",
                          quiet_exempt=True) is not None
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM settings WHERE key='caller'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE idempotency_key='fresh'").fetchone()[0] == 0


def test_atomic_claim_single_delivery_under_concurrency(tmp_path):
    path = tmp_path / "claim.db"
    c1 = dbmod.connect(path)
    dbmod.init_schema(c1)
    _chat(c1)
    outbox.enqueue(c1, event_type="x", body="one-copy-only",
                   idempotency_key="kc", quiet_exempt=True)
    c1.close()

    class SlowTransport:
        def __init__(self):
            self.sent = []
            self.lock = threading.Lock()

        def send(self, chat_id, text, reply_markup=None):
            time.sleep(0.3)
            with self.lock:
                self.sent.append(text)
            return {"ok": True}

    t = SlowTransport()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        conn = dbmod.connect(path)
        try:
            barrier.wait(timeout=5)
            results.append(outbox.process_outbox(conn, t, chat_id="111"))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    assert t.sent == ["one-copy-only"]


def test_stale_sending_recovered_inside_live_process(db):
    _chat(db)
    oid = outbox.enqueue(db, event_type="x", body="b",
                         idempotency_key="k-stale", quiet_exempt=True)
    old = clock.to_iso(clock.now(db) - __import__("datetime").timedelta(
        minutes=outbox.STALE_SENDING_MINUTES + 1))
    db.execute("UPDATE notification_outbox SET state='sending',"
               " claimed_at=? WHERE id=?", (old, oid))
    db.commit()
    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["recovered"] == 1
    row = db.execute("SELECT state, attempts FROM notification_outbox"
                     " WHERE id=?", (oid,)).fetchone()
    assert (row[0], row[1]) == ("failed_retryable", 1)
    advance(61)
    assert outbox.process_outbox(db, t)["sent"] == 1


def test_recovery_consumes_attempts_and_dead_letters(db):
    oid = outbox.enqueue(db, event_type="x", body="b",
                         idempotency_key="k-loop", quiet_exempt=True)
    for _ in range(outbox.MAX_ATTEMPTS):
        db.execute("UPDATE notification_outbox SET state='sending'"
                   " WHERE id=?", (oid,))
        db.commit()
        main_mod.recover_orphaned_sending(db)
    row = db.execute("SELECT state, attempts FROM notification_outbox"
                     " WHERE id=?", (oid,)).fetchone()
    assert (row[0], row[1]) == ("dead_letter", outbox.MAX_ATTEMPTS)


def test_send_time_quiet_recheck(db):
    _chat(db)
    set_clock_to(21, 55)
    outbox.enqueue(db, event_type="x", body="evening",
                   idempotency_key="k-q1")  # not exempt; not yet quiet
    outbox.enqueue(db, event_type="x", body="floor", floor_applied=True,
                   idempotency_key="k-q2")
    advance(10 * 60)  # 22:05 — inside the default quiet window
    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["deferred_quiet"] == 1
    assert [text for _, text in t.sent] == ["floor"]
    row = db.execute("SELECT not_before FROM notification_outbox"
                     " WHERE idempotency_key='k-q1'").fetchone()
    assert "T06:30" in row[0]
    advance(8 * 3600 + 30 * 60)  # past 06:30 next day
    assert outbox.process_outbox(db, t)["sent"] == 1


def test_daytime_quiet_window_math(db):
    import json as _json
    set_setting(db, "policy_json",
                _json.dumps({"quiet": {"start": "13:00", "end": "14:00"}}))
    set_clock_to(13, 30)
    nb = outbox.quiet_not_before(db)
    assert (nb.hour, nb.minute) == (14, 0)
    assert nb.date() == clock.now(db).date()  # NOT tomorrow (D-7)


def test_overnight_quiet_window_math(db):
    set_clock_to(23, 0)
    nb = outbox.quiet_not_before(db)
    assert (nb.hour, nb.minute) == (6, 30)
    assert nb.date() > clock.now(db).date()
    set_clock_to(3, 0)
    nb = outbox.quiet_not_before(db)
    assert (nb.hour, nb.minute) == (6, 30)
    assert nb.date() == clock.now(db).date()


def test_long_body_is_chunked_not_dead_lettered(db):
    _chat(db)
    body = "\n".join(f"line {i:03d} " + "x" * 90 for i in range(60))
    assert len(body) > 4096
    outbox.enqueue(db, event_type="discovery_digest", body=body,
                   idempotency_key="k-big", quiet_exempt=True)
    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] == 1 and stats["dead_letter"] == 0
    texts = [text for _, text in t.sent]
    assert len(texts) >= 2
    assert all(len(text) <= 4096 for text in texts)
    assert "\n".join(texts) == body


def test_priority_order_and_batch_limit(db):
    _chat(db)
    for i in range(55):
        outbox.enqueue(db, event_type="x", body=f"bulk {i}", score=1,
                       idempotency_key=f"bulk-{i}", quiet_exempt=True)
    outbox.enqueue(db, event_type="deadline_alert", body="URGENT",
                   score=100, idempotency_key="urgent", quiet_exempt=True)
    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] == outbox.BATCH_LIMIT
    assert t.sent[0][1] == "URGENT"  # priority first, not 56th (D-13)
    assert outbox.process_outbox(db, t)["sent"] == 6  # the rest next pass


def test_retry_after_overrides_schedule(db):
    _chat(db)
    outbox.enqueue(db, event_type="x", body="b", idempotency_key="k-ra",
                   quiet_exempt=True)

    class RateLimited:
        def send(self, chat_id, text, reply_markup=None):
            raise TelegramError("429", error_code=429, retry_after=3600)

    before = clock.now(db)
    outbox.process_outbox(db, RateLimited())
    row = db.execute("SELECT next_attempt_at FROM notification_outbox"
                     " WHERE idempotency_key='k-ra'").fetchone()
    delta = (clock.parse_iso(row[0]) - before).total_seconds()
    assert delta >= 3600  # the server said an hour; the schedule said 1m


def test_fallback_key_distinguishes_bodies(db):
    a = outbox.enqueue(db, event_type="note", body="first body")
    b = outbox.enqueue(db, event_type="note", body="second body")
    assert a is not None and b is not None and a != b


def test_listing_event_key_url_rules():
    k1 = outbox.listing_event_key("E", "s", "https://example.com/AB-123",
                                  "new_listing")
    k2 = outbox.listing_event_key("E", "s", "https://example.com/ab123",
                                  "new_listing")
    k3 = outbox.listing_event_key("E", "s", "HTTPS://EXAMPLE.COM/AB-123",
                                  "new_listing")
    assert k1 != k2  # path case/hyphens are significant (D-11)
    assert k1 == k3  # scheme/host are not


# ---------------------------------------------------------------------------
# telegram_io / bot transient contract
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._payload


def test_telegram_error_carries_retry_after(monkeypatch):
    payload = {"ok": False, "error_code": 429,
               "description": "Too Many Requests: retry after 3600",
               "parameters": {"retry_after": 3600}}
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _FakeResp(429, payload))
    with pytest.raises(TelegramError) as ei:
        HttpTransport(token="x").send("1", "hi")
    assert ei.value.retry_after == 3600
    assert ei.value.error_code == 429


def test_fetch_updates_validates_shape(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(
        200, {"ok": True, "result": {"not": "a list"}}))
    with pytest.raises(TelegramError):
        fetch_updates("tok")
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _FakeResp(200, json_error=True))
    with pytest.raises(TelegramError):
        fetch_updates("tok")


class _FlakyTransport:
    def __init__(self):
        self.sent = []
        self.fail = True

    def send(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise TelegramError("500 Internal", error_code=500)
        self.sent.append((str(chat_id), text))
        return {"ok": True}

    def answer_callback_query(self, cid, text=None):
        return {"ok": True}


def test_bot_transient_send_failure_unrecords_update(db):
    t = _FlakyTransport()
    b = bot_mod.Bot(db, t)
    upd = {"update_id": 777001,
           "message": {"chat": {"id": 111}, "text": "/start"}}
    assert b.handle_update(upd) == "transient_error"
    # un-recorded: the redelivery will not be dropped as a duplicate
    assert db.execute("SELECT COUNT(*) FROM telegram_updates"
                      " WHERE update_id=777001").fetchone()[0] == 0
    t.fail = False
    assert b.handle_update(upd) != "transient_error"
    assert db.execute("SELECT COUNT(*) FROM telegram_updates"
                      " WHERE update_id=777001").fetchone()[0] == 1
    # and a third delivery IS a duplicate
    assert b.handle_update(upd) == "duplicate_ignored"


# ---------------------------------------------------------------------------
# main: scheduler configuration
# ---------------------------------------------------------------------------

def test_scheduler_grace_listener_and_maintenance_job(tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRT_DB_PATH", str(tmp_path / "grt.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GRT_TEST_CLOCK_OFFSET_SECONDS", raising=False)
    recorded = {"jobs": {}, "listener": False}

    class Recorder:
        def __init__(self, *a, **kw):
            pass

        def add_job(self, func, trigger=None, **kw):
            recorded["jobs"][kw.get("id")] = kw

        def add_listener(self, fn, mask):
            recorded["listener"] = True

        def start(self):
            pass

    monkeypatch.setattr(main_mod, "BlockingScheduler", Recorder)
    main_mod.run()
    jobs = recorded["jobs"]
    # a slept-through 07:00 / 21:45 must not be dropped after 1s (D-2)
    assert jobs["digest"]["misfire_grace_time"] == \
        main_mod.GRACE_DAILY_CRON_S
    assert jobs["sweep"]["misfire_grace_time"] == \
        main_mod.GRACE_DAILY_CRON_S
    assert jobs["outbox"]["misfire_grace_time"] == \
        main_mod.GRACE_INTERVAL_S
    assert "maintenance" in jobs
    # R9 seller note: pin the maintenance slot itself, so a silent
    # schedule edit cannot slip past this test.
    assert jobs["maintenance"]["day_of_week"] == "sun"
    assert jobs["maintenance"]["hour"] == 3
    assert jobs["maintenance"]["minute"] == 30
    assert jobs["maintenance"]["misfire_grace_time"] == \
        main_mod.GRACE_WEEKLY_CRON_S
    assert recorded["listener"] is True


# ---------------------------------------------------------------------------
# Discogs lane
# ---------------------------------------------------------------------------

def _edition(db, eid, discogs_id, watch=2):
    db.execute(
        "INSERT INTO editions(id, title, catalog, discogs_id, watch_level)"
        " VALUES(?, '盤', 'CAT', ?, ?)", (eid, discogs_id, watch))
    db.commit()


def _discogs(db):
    sid = _source(db, "discogs", kind="db", url="")
    return DiscogsFetcher(db, sid, token="tok"), sid


class _StatsResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_discogs_blocked_aborts_run(db, monkeypatch):
    _edition(db, "AR-B1", 101)
    _edition(db, "AR-B2", 102)
    _edition(db, "AR-B3", 103)
    f, sid = _discogs(db)
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _StatsResp(403, {"error": "blocked"})

    monkeypatch.setattr(discogs_mod.httpx, "get", fake_get)
    res = f.run_watchlist()
    assert res["failures"] == 1 and res["snapshotted"] == 0
    assert len(calls) == 1  # did NOT hammer the remaining editions (D-1)
    row = db.execute("SELECT result FROM fetch_runs WHERE source_id=?",
                     (sid,)).fetchone()
    assert row[0] == "BLOCKED"


def test_discogs_partial_success_records_ok(db, monkeypatch):
    _edition(db, "AR-P1", 201)
    _edition(db, "AR-P2", 202)
    f, sid = _discogs(db)

    def fake_get(url, **kw):
        if url.endswith("/201"):
            return _StatsResp(200, {
                "num_for_sale": 2,
                "lowest_price": {"value": 5000, "currency": "JPY"}})
        return _StatsResp(200, ValueError("broken json"))

    monkeypatch.setattr(discogs_mod.httpx, "get", fake_get)
    res = f.run_watchlist()
    assert res["snapshotted"] == 1 and res["failures"] == 1
    row = db.execute("SELECT result, error_detail FROM fetch_runs"
                     " WHERE source_id=?", (sid,)).fetchone()
    # 1 of 2 snapshotted is a PARTIAL SUCCESS, not a total failure (D-2)
    assert row[0] == "OK" and "failures=1" in (row[1] or "")


def test_discogs_empty_stats_payload_rejected(db, monkeypatch):
    f, _sid = _discogs(db)
    monkeypatch.setattr(discogs_mod.httpx, "get",
                        lambda url, **kw: _StatsResp(200, {}))
    with pytest.raises(discogs_mod.FetchError) as ei:
        f.stats_for_release(999)
    assert ei.value.result == "PARSE_ERROR"  # no NULL 'success' (D-9)


def test_discogs_appearance_episodes_renotify(db, monkeypatch):
    _chat(db)
    _edition(db, "AR-E1", 301, watch=2)
    f, _sid = _discogs(db)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale) VALUES('AR-E1', 'discogs', ?, 0)",
        (clock.now_iso(db),))
    db.commit()
    monkeypatch.setattr(discogs_mod.httpx, "get", lambda url, **kw:
                        _StatsResp(200, {"num_for_sale": 1,
                                         "lowest_price": None}))
    f.snapshot_edition("AR-E1")
    keys = [r[0] for r in db.execute(
        "SELECT idempotency_key FROM notification_outbox").fetchall()]
    assert len(keys) == 1
    # sells out, then is re-listed: a NEW episode (C-B2-1)
    advance(3600)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale) VALUES('AR-E1', 'discogs', ?, 0)",
        (clock.now_iso(db),))
    db.commit()
    f.snapshot_edition("AR-E1")
    keys2 = [r[0] for r in db.execute(
        "SELECT idempotency_key FROM notification_outbox").fetchall()]
    assert len(keys2) == 2 and keys2[0] != keys2[1]


# ---------------------------------------------------------------------------
# storewatch ledger
# ---------------------------------------------------------------------------

class _HttpClient:
    def __init__(self, status, text=""):
        self.status = status
        self.text = text
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return SimpleNamespace(status_code=self.status, text=self.text)


def test_storewatch_records_runs_and_ladder(db):
    sid = _source(db, "shop1")
    blocked = _HttpClient(403)
    res = storewatch.run_all(db, client=blocked)
    assert res and res[0]["ok"] is False
    rows = db.execute("SELECT result FROM fetch_runs WHERE source_id=?",
                      (sid,)).fetchall()
    assert [r[0] for r in rows] == ["BLOCKED"]  # ledgered (D-4)
    assert db.execute("SELECT COUNT(*) FROM blocked_sources"
                      ).fetchone()[0] == 1  # shelved visibly
    assert db.execute("SELECT COUNT(*) FROM freq_state WHERE source_id=?",
                      (sid,)).fetchone()[0] == 1  # ladder fed
    # and is_due is now REAL for this lane: an immediate second run
    # fetches nothing (previously: every 15-minute tick, forever)
    ok_client = _HttpClient(200, "<html>shop</html>")
    assert storewatch.run_all(db, client=ok_client) == []
    assert ok_client.calls == 0


# ---------------------------------------------------------------------------
# health layer
# ---------------------------------------------------------------------------

def test_zero_item_anomaly_for_small_source(db):
    sid = _source(db, "tiny")
    health.record_fetch_run(db, sid, result="OK", items_extracted=3)
    health.record_fetch_run(db, sid, result="OK", items_extracted=0)
    health.record_fetch_run(db, sid, result="OK", items_extracted=0)
    row = db.execute("SELECT result FROM fetch_runs WHERE source_id=?"
                     " ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    assert row[0] == "PARSE_EMPTY"  # median was 1.5 < 5: old rule blind
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "SUSPECT"


def test_down_transition_alerts_immediately(db):
    _chat(db)
    sid = _source(db, "downy")
    health.record_fetch_run(db, sid, result="OK", items_extracted=1)
    old = clock.to_iso(clock.now(db) - __import__("datetime").timedelta(
        hours=25))
    db.execute("UPDATE source_health SET last_success_at=? WHERE source_id=?",
               (old, sid))
    db.commit()
    health.evaluate_time_based_health(db)
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "DOWN"
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE event_type='source_down'").fetchone()[0] == 1


def test_silence_alert_gap_close_and_second_episode(db):
    _chat(db)
    sid = _source(db, "silent")
    db.execute("UPDATE sources SET expected_cadence_days=1.0 WHERE id=?",
               (sid,))
    db.commit()
    health.record_fetch_run(db, sid, result="OK", items_extracted=1)
    old = clock.to_iso(clock.now(db) - __import__("datetime").timedelta(
        days=3))
    db.execute("UPDATE source_health SET last_success_at=? WHERE source_id=?",
               (old, sid))
    db.commit()
    health.evaluate_time_based_health(db)
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE event_type='silence_detected'"
                      ).fetchone()[0] == 1
    gap = db.execute("SELECT gap_end FROM observation_gaps"
                     " WHERE scope=?", (f"source:{sid}",)).fetchone()
    assert gap is not None and gap[0] is None
    # recovery closes the gap (D-4b) ...
    health.record_fetch_run(db, sid, result="OK", items_extracted=1)
    health.evaluate_time_based_health(db)
    gap = db.execute("SELECT gap_end FROM observation_gaps"
                     " WHERE scope=?", (f"source:{sid}",)).fetchone()
    assert gap[0] is not None
    # ... and a SECOND silence is a new episode, not suppressed
    # forever: advance the test clock 3 days past the recovery.
    advance(3 * 86400)
    health.evaluate_time_based_health(db)
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE event_type='silence_detected'"
                      ).fetchone()[0] == 2


def test_blocked_shelf_stops_fetcher(db):
    sid = _source(db, "shelved")
    for _ in range(3):
        health.record_fetch_run(db, sid, result="BLOCKED")
    assert health.is_shelved(db, sid) is True

    class CountingFetcher(BaseFetcher):
        kind = "HTML"
        calls = 0

        def fetch_items(self, url):
            CountingFetcher.calls += 1
            return ([{"title": "x"}], {})

    f = CountingFetcher(db, sid)
    res = f.run("https://example.invalid/")
    assert res["result"] == "BLOCKED_SHELF"
    assert CountingFetcher.calls == 0  # no network while shelved (D-8)
    advance(25 * 3600)  # shelf window passes: one probe is allowed
    res = f.run("https://example.invalid/")
    assert res["ok"] is True and CountingFetcher.calls == 1


# ---------------------------------------------------------------------------
# freqpolicy
# ---------------------------------------------------------------------------

def test_freqpolicy_writes_expected_cadence(db):
    sid = _source(db, "cad")
    freqpolicy.apply_run_outcome(db, sid, True)
    row = db.execute("SELECT expected_cadence_days FROM sources WHERE id=?",
                     (sid,)).fetchone()
    assert row[0] == pytest.approx(360 / 1440.0)


def test_freqpolicy_consec_counters_do_not_bank(db):
    sid = _source(db, "bank")
    freqpolicy.apply_run_outcome(db, sid, True)
    freqpolicy.apply_run_outcome(db, sid, True)  # promote 360 -> 60
    st = db.execute("SELECT tier_min FROM freq_state WHERE source_id=?",
                    (sid,)).fetchone()
    assert st[0] == 60  # ceiling for an approved non-ingest source
    for _ in range(3):
        freqpolicy.apply_run_outcome(db, sid, True)
    row = db.execute("SELECT tier_min, consec_ok FROM freq_state"
                     " WHERE source_id=?", (sid,)).fetchone()
    assert row[0] == 60 and row[1] <= 1  # no banked promotion credit


# ---------------------------------------------------------------------------
# digest / deadlines
# ---------------------------------------------------------------------------

def test_carry_over_count_tolerates_corrupt_found_at(db):
    db.execute(
        "INSERT INTO candidate_editions(title, raw_title, source_url,"
        " fingerprint, status, found_at) VALUES('x', 'x', 'u', 'fp-bad',"
        " 'pending', 'not-a-date')")
    db.commit()
    assert digest._carry_over_count(db) == 0  # was: ValueError, daily


def _deadline_row(db, when_iso, kind="hard_cutoff"):
    db.execute(
        "INSERT INTO release_schedule(edition_id, channel, deadline_raw,"
        " deadline_jst, deadline_confidence, deadline_kind)"
        " VALUES('AR-DL', 'web', 'raw', ?, 'confirmed', ?)",
        (when_iso, kind))
    db.commit()
    return db.execute("SELECT MAX(id) FROM release_schedule").fetchone()[0]


def test_deadline_sweep_uses_configured_quiet_end(db):
    set_clock_to(21, 45)
    tomorrow = clock.now(db) + __import__("datetime").timedelta(days=1)
    dl = tomorrow.replace(hour=7, minute=30, second=0, microsecond=0)
    _deadline_row(db, clock.to_iso(dl))
    # default quiet end 06:30: a 07:30 deadline is outside the sweep
    assert digest.deadline_sweep(db) == 0
    # user quiet end 08:00: the same deadline IS inside tonight's window
    set_setting(db, "policy_json", json.dumps(
        {"quiet": {"start": "22:00", "end": "08:00"}}))
    assert digest.deadline_sweep(db) == 1


def test_missed_guarantee_cutoff_is_declared(db):
    _chat(db)
    set_clock_to(7, 5)
    past = clock.now(db) - __import__("datetime").timedelta(hours=2)
    _deadline_row(db, clock.to_iso(past), kind="guarantee_cutoff")
    from grt import deadlines_ops
    created = deadlines_ops.staged_alerts(db)
    assert created == 1  # was 0: swept-through guarantee vanished (D-3)
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE idempotency_key LIKE 'deadline_missed|%'"
                      ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# matcher review dedupe / pricedrop episode / maintenance
# ---------------------------------------------------------------------------

def test_match_review_rows_dedupe_while_unresolved(db):
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm, label,"
        " year, format, country, vgmdb_id, distribution_type,"
        " variant_completeness, watch_level)"
        " VALUES('AR-M1', 'テスト盤', NULL, NULL, NULL, NULL, 'LP', 'JP',"
        " 777, 'retail', 'unknown', 0)")
    db.commit()
    listing = {"raw_title": "全然違うタイトル", "catalog_raw": None,
               "vgmdb_id": 777, "url": "https://example.invalid/i/1",
               "route": "retail", "distribution_type": "retail",
               "description": ""}
    matcher.match_listing(db, listing, listing_id=1)
    matcher.match_listing(db, listing, listing_id=2)
    rows = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE kind='match_review'"
        " AND resolved_at IS NULL").fetchone()[0]
    assert rows == 1  # re-matching does not stack copies (seller D-2)


def test_pricedrop_second_episode_notifies(db):
    _chat(db)
    # W-check B3 fixture update: the same-copy path now enforces the
    # 9-C12 evidence gate, so the listing needs a real row and a
    # price-confidence-高 decision (production listings always have
    # both; the bare dict predates the gate).
    db.execute("INSERT INTO editions(id, title) VALUES('AR-PD', '盤')")
    db.execute(
        "INSERT INTO listings(id, source, url, edition_id, cond_rank,"
        " price, currency, status) VALUES(1, 'shop',"
        " 'https://example.invalid/pd', 'AR-PD', 5, 10000, 'JPY',"
        " 'active')")
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id,"
        " existence_confidence, price_confidence)"
        " VALUES(1, 'AR-PD', '高', '高')")
    db.commit()
    # R14 fixture update: same yen-value contract as
    # test_pricedrop_key_price_type_canonical.
    listing = {"id": 1, "edition_id": "AR-PD", "variant_id": None,
               "cond_rank": 5, "currency": "JPY", "price": 8000,
               "total_jpy": 8000,
               "url": "https://example.invalid/pd"}
    r1 = pricedrop.on_listing_price_update(db, listing, 10000, 8000)
    assert r1["notified"] is True
    db.execute("UPDATE notification_outbox SET state='sent'")
    db.commit()
    # price recovered and dropped to the same 8,000 again: new episode
    r2 = pricedrop.on_listing_price_update(db, listing, 10000, 8000)
    assert r2["notified"] is True
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE event_type='price_drop'").fetchone()[0] == 2
    # while episode 2 is still in flight, a third call IS a duplicate
    r3 = pricedrop.on_listing_price_update(db, listing, 10000, 8000)
    assert r3["notified"] is False


def test_maintenance_prunes_telemetry_only(db, tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    old = clock.to_iso(clock.now(db) - __import__("datetime").timedelta(
        days=200))
    new = clock.now_iso(db)
    sid = _source(db, "maint")
    db.execute("INSERT INTO fetch_runs(source_id, started_at, result)"
               " VALUES(?, ?, 'OK')", (sid, old))
    db.execute("INSERT INTO fetch_runs(source_id, started_at, result)"
               " VALUES(?, ?, 'OK')", (sid, new))
    db.execute("INSERT INTO system_events(kind, created_at)"
               " VALUES('old_event', ?)", (old,))
    db.execute("INSERT INTO telegram_updates(update_id, received_at)"
               " VALUES(1, ?)", (old,))
    db.execute("INSERT INTO telegram_updates(update_id, received_at)"
               " VALUES(2, ?)", (new,))
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at)"
        " VALUES('AR-K', 'discogs', ?)", (old,))
    db.commit()
    res = maintenance.run_maintenance(db)
    assert res["fetch_runs"] == 1 and res["telegram_updates"] == 1
    assert db.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM telegram_updates"
                      ).fetchone()[0] == 1
    # archive data is never pruned
    assert db.execute("SELECT COUNT(*) FROM price_snapshots"
                      ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# AI-engineer late report (#3 poll-thread death invisible, #7
# last_job_at pollution, #4 offset ordering covered above): the poll
# thread is supervised by the heartbeat job and stamps poll_alive_at;
# a non-dict update cannot kill the loop; last_job_at advances only
# when a job actually ran (gate open).

class _Boom(BaseException):
    """A BaseException the poll loop's `except Exception` cannot catch."""


def _wait_for(predicate, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_poll_thread_supervised_restart_and_alive_stamp(tmp_path,
                                                        monkeypatch):
    from grt.config import get_setting
    from test_qc_r2_resident import _Resident, _preseed_open_gate
    res = _Resident(tmp_path, monkeypatch, preseed=_preseed_open_gate)
    state = {"calls": 0}

    def flaky_fetch(token, offset=None, timeout=25):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _Boom("simulated fatal poll failure")
        time.sleep(0.02)
        return []

    monkeypatch.setattr(main_mod, "fetch_updates", flaky_fetch)
    res.start()
    try:
        def restarted():
            if state["calls"] < 2:
                return False
            db = dbmod.connect(res.db_file)
            try:
                ev = db.execute(
                    "SELECT COUNT(*) FROM system_events"
                    " WHERE kind='poll_thread_dead'").fetchone()[0]
                alive = get_setting(db, "poll_alive_at")
            finally:
                db.close()
            return ev >= 1 and alive is not None

        assert _wait_for(restarted), (
            "dead poll thread was not restarted / never stamped alive")
    finally:
        res.stop()


def test_poll_loop_survives_non_dict_update(tmp_path, monkeypatch):
    from grt.config import get_setting
    from test_qc_r2_resident import _Resident, _preseed_open_gate
    res = _Resident(tmp_path, monkeypatch, preseed=_preseed_open_gate)
    state = {"calls": 0}
    batch = ["garbage-not-a-dict",
             {"update_id": 5,
              "message": {"chat": {"id": 999}, "text": "hello"}}]

    def fetch_with_batch(token, offset=None, timeout=25):
        state["calls"] += 1
        if state["calls"] == 1:
            return list(batch)
        time.sleep(0.02)
        return []

    monkeypatch.setattr(main_mod, "fetch_updates", fetch_with_batch)
    res.start()
    try:
        def settled():
            db = dbmod.connect(res.db_file)
            try:
                off = get_setting(db, "telegram_update_offset")
            finally:
                db.close()
            return off == "6" and state["calls"] >= 3

        assert _wait_for(settled), (
            "offset did not advance past the batch, or the poll loop "
            "died on the non-dict element")
    finally:
        res.stop()


def test_last_job_at_only_advances_when_gate_open(tmp_path, monkeypatch):
    from test_qc_r2_resident import _Resident

    def preseed_closed(db):
        set_setting(db, "allowed_chat_id", "111")
        db.execute(
            "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
            " VALUES(1, '2020-01-01T00:00:00+09:00',"
            " '2020-01-01T00:00:00+09:00', '2020-01-01T00:00:00+09:00')")
        db.commit()

    res = _Resident(tmp_path, monkeypatch, preseed=preseed_closed)
    res.start()
    try:
        def outbox_job_ran():
            db = dbmod.connect(res.db_file)
            try:
                alive = db.execute(
                    "SELECT alive_at FROM heartbeat WHERE id=1"
                ).fetchone()[0]
            finally:
                db.close()
            return alive != "2020-01-01T00:00:00+09:00"

        assert _wait_for(outbox_job_ran, timeout=15)
        db = dbmod.connect(res.db_file)
        try:
            last_job = db.execute(
                "SELECT last_job_at FROM heartbeat WHERE id=1"
            ).fetchone()[0]
        finally:
            db.close()
        assert last_job == "2020-01-01T00:00:00+09:00", (
            "last_job_at advanced although the gate was closed and "
            "no job ran (AI-engineer #7)")
    finally:
        res.stop()


def test_maintenance_step_isolation(db, tmp_path, monkeypatch):
    # R9 seller N-1: one broken prune step must not stop the later
    # steps (above all the disk guard) or the summary event.
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    old = clock.to_iso(clock.now(db) - __import__("datetime").timedelta(
        days=200))
    db.execute("INSERT INTO system_events(kind, created_at)"
               " VALUES('old_event', ?)", (old,))
    db.execute("INSERT INTO telegram_updates(update_id, received_at)"
               " VALUES(1, ?)", (old,))
    db.execute("ALTER TABLE fetch_runs RENAME TO fetch_runs_gone")
    db.commit()
    res = maintenance.run_maintenance(db)
    assert "fetch_runs" in res.get("errors", {}), res
    # later steps still ran
    assert res["system_events"] == 1 and res["telegram_updates"] == 1
    assert "disk" in res and res["disk"].get("ok") is True
    # the summary event was recorded despite the failure
    ev = db.execute("SELECT detail_json FROM system_events"
                    " WHERE kind='maintenance_run'").fetchone()
    assert ev is not None and "fetch_runs" in ev[0]


def test_revival_does_not_commit_caller_transaction(db):
    # R9 collector N-1: _revive_dead_letter used to db.commit(),
    # flushing the caller's unrelated uncommitted work early — the
    # D-8 contract broken for the revival path only.
    _chat(db)
    oid = outbox.enqueue(db, event_type="candidate_review", body="v1",
                         idempotency_key="candidate_review|77",
                         quiet_exempt=True)
    db.execute("UPDATE notification_outbox SET state='dead_letter',"
               " attempts=6 WHERE id=?", (oid,))
    db.commit()
    # caller starts its own unit of work, then revives via enqueue
    db.execute("INSERT INTO sources(name, kind, status)"
               " VALUES('caller-work', 'STORE', 'approved')")
    again = outbox.enqueue(db, event_type="candidate_review", body="v2",
                           idempotency_key="candidate_review|77",
                           quiet_exempt=True)
    assert again == oid
    db.rollback()  # the caller abandons its unit of work...
    # ...so BOTH its own row and the revival must be gone with it
    assert db.execute("SELECT COUNT(*) FROM sources"
                      " WHERE name='caller-work'").fetchone()[0] == 0
    row = db.execute("SELECT state, body FROM notification_outbox"
                     " WHERE id=?", (oid,)).fetchone()
    assert (row[0], row[1]) == ("dead_letter", "v1")


def test_pricedrop_key_price_type_canonical(db):
    # R9 collector N-2: int 8000 and float 8000.0 are the same price
    # and must produce the same idempotency key.
    _chat(db)
    # W-check B3 fixture update: same 9-C12 evidence-gate setup as
    # test_pricedrop_second_episode_notifies.
    db.execute("INSERT INTO editions(id, title) VALUES('AR-PD2', '盤')")
    db.execute(
        "INSERT INTO listings(id, source, url, edition_id, cond_rank,"
        " price, currency, status) VALUES(1, 'shop',"
        " 'https://example.invalid/pd2', 'AR-PD2', 5, 10000, 'JPY',"
        " 'active')")
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id,"
        " existence_confidence, price_confidence)"
        " VALUES(1, 'AR-PD2', '高', '高')")
    db.commit()
    # R14 fixture update: the same-copy path now requires the
    # listing to carry a yen value (production rows always do —
    # the row is re-read after the price UPDATE; the bare dict
    # omitted price/total_jpy entirely).
    listing = {"id": 1, "edition_id": "AR-PD2", "variant_id": None,
               "cond_rank": 5, "currency": "JPY", "price": 8000,
               "total_jpy": 8000,
               "url": "https://example.invalid/pd2"}
    r1 = pricedrop.on_listing_price_update(db, listing, 10000, 8000)
    assert r1["notified"] is True
    r2 = pricedrop.on_listing_price_update(db, listing, 10000, 8000.0)
    assert r2["notified"] is False  # in-flight duplicate, not a 2nd row
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      " WHERE event_type='price_drop'").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# R9 maniac A follow-ups: D-8 shelf scope (Discogs / storewatch lanes
# now consult the shelf themselves), lowest_price shape validation,
# and BaseFetcher.run returning the effective (rewritten) result.

def test_discogs_lane_respects_blocked_shelf(db, monkeypatch):
    sid = _source(db, "discogs", kind="db", url="")
    for _ in range(3):
        health.record_fetch_run(db, sid, result="BLOCKED")
    assert health.is_shelved(db, sid) is True
    _edition(db, "AR-SH", 777, watch=2)
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return _StatsResp(200, {"num_for_sale": 1, "lowest_price":
                                {"value": 100, "currency": "JPY"}})

    monkeypatch.setattr(discogs_mod.httpx, "get", fake_get)
    f = DiscogsFetcher(db, sid, token="<redacted>")
    res = f.run_watchlist()
    assert res.get("shelved") is True and calls["n"] == 0
    # no ledger row for the skipped run (only the 3 seed BLOCKED rows)
    assert db.execute("SELECT COUNT(*) FROM fetch_runs WHERE source_id=?",
                      (sid,)).fetchone()[0] == 3


def test_storewatch_respects_blocked_shelf(db):
    sid = _source(db, "shelvedshop")
    for _ in range(3):
        health.record_fetch_run(db, sid, result="BLOCKED")
    assert health.is_shelved(db, sid) is True
    client = _HttpClient(200, "<html>shop</html>")
    assert storewatch.run_all(db, client=client) == []
    assert client.calls == 0  # not probed while shelved
    advance(25 * 3600)  # shelf window passes: probing resumes
    res = storewatch.run_all(db, client=client)
    assert client.calls == 1 and len(res) == 1


def test_discogs_lowest_price_shape_validated(db, monkeypatch):
    f, _sid = _discogs(db)
    monkeypatch.setattr(
        discogs_mod.httpx, "get",
        lambda url, **kw: _StatsResp(
            200, {"num_for_sale": 1, "lowest_price": [1, 2]}))
    with pytest.raises(discogs_mod.FetchError) as ei:
        f.stats_for_release(999)
    assert ei.value.result == "PARSE_ERROR"  # not a raw AttributeError


def test_fetcher_run_returns_effective_result(db):
    sid = _source(db, "effres")
    health.record_fetch_run(db, sid, result="OK", items_extracted=3)

    class EmptyFetcher(BaseFetcher):
        kind = "HTML"

        def fetch_items(self, url):
            return ([], {})

    f = EmptyFetcher(db, sid)
    f.run("https://example.invalid/")
    res = f.run("https://example.invalid/")
    # the second consecutive zero is rewritten PARSE_EMPTY in the
    # ledger, and the RETURN value now says so too (was: "OK")
    assert res["ok"] is True and res["result"] == "PARSE_EMPTY"


def test_startup_catchup_recovers_partial_digest(db):
    # R9 wholesaler N-1: only the health digest made it into the
    # outbox before a crash — the catch-up must restore the missing
    # discovery digest instead of judging by the health key alone.
    _chat(db)
    set_setting(db, "deadline_override_quiet", "0")
    set_setting(db, "deadline_override_quiet_selected", "1")
    set_clock_to(8, 30)
    today = clock.now(db).strftime("%Y-%m-%d")
    outbox.enqueue(db, event_type="health_digest", body="h",
                   idempotency_key="health_digest|" + today,
                   quiet_exempt=True)
    main_mod._startup_catchup(db)
    keys = {r[0] for r in db.execute(
        "SELECT idempotency_key FROM notification_outbox")}
    assert "discovery_digest|" + today in keys
    # and a second catch-up adds nothing new
    n = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
    main_mod._startup_catchup(db)
    assert db.execute("SELECT COUNT(*) FROM notification_outbox"
                      ).fetchone()[0] == n


def test_ok_response_without_result_is_telegram_error(monkeypatch):
    # R9 adversarial QA N-1: {"ok": true} with no "result" key used
    # to raise a raw KeyError; on the send path that escaped the
    # transient wrapping and permanently lost the user's command.
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _FakeResp(200, {"ok": True}))
    with pytest.raises(TelegramError):
        HttpTransport(token="<redacted>").send("1", "hi")
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _FakeResp(200, {"ok": True}))
    with pytest.raises(TelegramError):
        fetch_updates("tok")


def test_daily_catchup_covers_yesterdays_sweep_slot(db):
    # R9 adversarial QA D-4 residual: a live process that slept past
    # the 21:45 sweep's grace gets the sweep re-executed by the
    # (hourly) daily catch-up — keyed to yesterday's slot at 01:00.
    from grt.config import get_setting
    _chat(db)
    set_setting(db, "deadline_override_quiet", "0")
    set_setting(db, "deadline_override_quiet_selected", "1")
    set_clock_to(1, 0, add_days=1)  # tomorrow, 01:00
    yesterday = digest.sweep_slot_date(clock.now(db))
    assert get_setting(db, "last_deadline_sweep_slot") != yesterday
    main_mod._daily_catchup(db)
    assert get_setting(db, "last_deadline_sweep_slot") == yesterday
    n = db.execute("SELECT COUNT(*) FROM system_events"
                   " WHERE kind='deadline_sweep_run'").fetchone()[0]
    assert n == 1
    main_mod._daily_catchup(db)  # slot already covered: no re-run
    assert db.execute("SELECT COUNT(*) FROM system_events"
                      " WHERE kind='deadline_sweep_run'"
                      ).fetchone()[0] == 1
