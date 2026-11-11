"""QC round 5: fault injection + notification-path spec corrections.

Faults are injected ONLY through stubs/mocks (monkeypatched httpx,
failing transports, a second SQLite connection holding a write lock).
No real tokens, no real network.

Covered round-9 items: 9-A2(1)/(2), 9-A3 retry/dead-letter reporting,
9-B4, 9-B13, 9-C4, 9-C8, 9-C12, 9-C22, 9-A11 (deadline-sweep kinds +
quiet-override wiring).
"""
import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import advance, set_clock_to
from grt import bot as bot_mod
from grt import clock, digest, health, outbox
from grt import db as dbmod
from grt.main import _redact_poll_error
from grt.config import set_setting
from grt.fetchers.blipblop import BlipBlopFetcher
from grt.fetchers.discogs import DiscogsFetcher
from grt.telegram_io import MockTransport, TelegramError, fetch_updates


class RecordingTransport:
    """Transport double that records reply_markup as well as text."""

    def __init__(self):
        self.sent = []          # (chat_id, text)
        self.markups = []       # reply_markup per send (or None)
        self.answered = []
        self._failures_left = 0

    def fail_next(self, n):
        self._failures_left = n

    def send(self, chat_id, text, reply_markup=None):
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TelegramError("mock delivery failure")
        self.sent.append((str(chat_id), text))
        self.markups.append(reply_markup)
        return {"ok": True}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append(str(callback_query_id))
        return {"ok": True}


def _source(db, name="src1", kind="shop"):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status) VALUES(?, ?, 'approved')",
        (name, kind))
    db.commit()
    return cur.lastrowid


def _last_fetch_run(db):
    return db.execute(
        "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()


# --------------------------------------------------------------------------
# 1a. Send failures: retry schedule, dead_letter, reporting
# --------------------------------------------------------------------------

def test_retry_intervals_and_dead_letter_reporting(db):
    assert outbox.RETRY_DELAYS_MIN == [1, 5, 30, 120, 720]
    # W-check B2 correction (maniac B D-1): MAX_ATTEMPTS was 5, which
    # made the documented 12h step unreachable (the 5th failure
    # dead-lettered instead of scheduling it). It is 6 now: five
    # scheduled retries, dead_letter on the 6th failure.
    assert outbox.MAX_ATTEMPTS == 6
    set_setting(db, "allowed_chat_id", "111")
    outbox.enqueue(db, event_type="new_listing", body="always fails",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.com/r5", quiet_exempt=True)
    t = MockTransport()
    t.fail_next(99)
    expected_next_min = [1, 5, 30, 120, 720]
    for i, mins in enumerate(expected_next_min):
        before = clock.now(db)
        outbox.process_outbox(db, t)
        row = db.execute("SELECT * FROM notification_outbox").fetchone()
        assert row["state"] == "failed_retryable"
        assert row["attempts"] == i + 1
        assert row["last_error"]  # failure is recorded on the row
        nxt = clock.parse_iso(row["next_attempt_at"])
        delta = (nxt - before).total_seconds()
        assert mins * 60 - 2 <= delta <= mins * 60 + 2
        advance(mins * 60)
    outbox.process_outbox(db, t)  # 6th failure -> dead_letter
    row = db.execute("SELECT * FROM notification_outbox").fetchone()
    assert (row["attempts"], row["state"]) == (6, "dead_letter")
    assert outbox.dead_letter_count(db) == 1
    # dead_letter is reported in the daily digest ...
    assert "dead_letter 1件" in digest.build_health_digest(db)
    # ... and in the /status-equivalent aggregation (bot _cmd_status).
    b = bot_mod.Bot(db, RecordingTransport())
    b._cmd_status(111)
    assert any("dead_letter: 1件" in text for _, text in b.transport.sent)


def test_send_failure_recovers_when_transport_heals(db):
    set_setting(db, "allowed_chat_id", "111")
    outbox.enqueue(db, event_type="new_listing", body="heals",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.com/heal", quiet_exempt=True)
    t = MockTransport()
    t.fail_next(2)
    outbox.process_outbox(db, t)
    advance(60)
    outbox.process_outbox(db, t)
    advance(300)
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] == 1
    row = db.execute("SELECT state, attempts FROM notification_outbox").fetchone()
    assert (row[0], row[1]) == ("sent", 2)


# --------------------------------------------------------------------------
# 1b. Fetcher faults: Discogs API
# --------------------------------------------------------------------------

def _discogs_db(db):
    sid = _source(db, name="discogs", kind="db")
    db.execute(
        "INSERT INTO editions(id, title, catalog, discogs_id, watch_level)"
        " VALUES('AR-000001', 'Game OST', 'ABC-123', 555, 2)")
    db.commit()
    return sid


class _Resp:
    def __init__(self, status=200, payload=None, json_error=None, text=""):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error
        self.text = text
        # W-check B5 fixture support: http_get now streams
        # (25MB body cap), so the fake also answers httpx.stream.
        self.headers = {}

    def iter_bytes(self):
        yield (self.text or "").encode("utf-8")

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _StreamOf:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


def _patch_http(monkeypatch, resp):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: resp)
    monkeypatch.setattr(httpx, "stream",
                        lambda *a, **k: _StreamOf(resp))


def test_discogs_stats_uses_release_stats_endpoint(monkeypatch):
    from grt.fetchers.discogs import API
    seen = {}
    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp(payload={"num_for_sale": 0})
    monkeypatch.setattr(httpx, "get", fake_get)
    f = DiscogsFetcher(None, 1, token="dummy")
    assert f.stats_for_release(144593)["num_for_sale"] == 0
    assert seen["url"] == API + "/marketplace/stats/144593"

@pytest.mark.parametrize("status,expected", [
    (500, "HTTP_ERROR"), (404, "HTTP_ERROR"),
    (403, "BLOCKED"), (429, "BLOCKED"), (401, "BLOCKED"),
])
def test_discogs_http_errors_recorded_not_success(db, monkeypatch, status, expected):
    sid = _discogs_db(db)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=status))
    f = DiscogsFetcher(db, sid, token="dummy")
    res = f.run_watchlist()  # must not raise
    assert res["failures"] == 1 and res["snapshotted"] == 0
    run = _last_fetch_run(db)
    assert run["result"] == expected and run["result"] != "OK"
    assert run["fetcher_kind"] == "API"
    assert db.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 0


def test_discogs_broken_json_recorded_as_parse_error(db, monkeypatch):
    sid = _discogs_db(db)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _Resp(status=200, json_error=ValueError("no json")))
    res = DiscogsFetcher(db, sid, token="dummy").run_watchlist()
    assert res["failures"] == 1
    assert _last_fetch_run(db)["result"] == "PARSE_ERROR"


def test_discogs_empty_and_nondict_payloads_not_success(db, monkeypatch):
    sid = _discogs_db(db)
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: _Resp(status=200,
                              json_error=ValueError("empty body")))
    assert DiscogsFetcher(db, sid, token="dummy").run_watchlist()["failures"] == 1
    assert _last_fetch_run(db)["result"] == "PARSE_ERROR"
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _Resp(status=200, payload=[]))
    assert DiscogsFetcher(db, sid, token="dummy").run_watchlist()["failures"] == 1
    assert _last_fetch_run(db)["result"] == "PARSE_ERROR"


def test_discogs_timeout_recorded(db, monkeypatch):
    sid = _discogs_db(db)

    def boom(*a, **k):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(httpx, "get", boom)
    res = DiscogsFetcher(db, sid, token="dummy").run_watchlist()
    assert res["failures"] == 1
    assert _last_fetch_run(db)["result"] == "TIMEOUT"


def test_discogs_unexpected_item_error_contained_and_logged(db, monkeypatch):
    sid = _discogs_db(db)
    # An unusable payload shape -> the snapshot INSERT raises a
    # non-FetchError; the run must survive and record the failure.
    # (Updated in W-check B2 R9: a non-dict lowest_price is rejected
    # upstream in stats_for_release as a classified PARSE_ERROR per
    # maniac A observation 1. Updated again in W-check B3: the VALUE
    # is now validated there too (adversarial 2 / maniac A D-5), so
    # the unexpected-error trigger moved to the currency's shape,
    # which stats does not validate and sqlite cannot bind.
    # Updated again in R14: the currency IS sanitised now (a dict
    # currency means "currency unknown", the observation is kept),
    # so the trigger moved to a num_for_sale too large for a
    # SQLite INTEGER — stats accepts any non-negative int, and the
    # INSERT raises OverflowError, a genuine non-FetchError.)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=200, payload={
        "num_for_sale": 10 ** 30,
        "lowest_price": {"value": 100, "currency": "JPY"}}))
    res = DiscogsFetcher(db, sid, token="dummy").run_watchlist()
    assert res["failures"] == 1
    assert _last_fetch_run(db)["result"] != "OK"
    ev = db.execute("SELECT COUNT(*) FROM system_events"
                    " WHERE kind='fetcher_item_error'").fetchone()[0]
    assert ev == 1


# --------------------------------------------------------------------------
# 1c. Fetcher faults: Blip Blop RSS
# --------------------------------------------------------------------------

def _blip(db):
    return _source(db, name="blipblop", kind="community")


_RSS_OK = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
           '<item><title>Item One</title><link>https://example.com/1</link>'
           '<description>d</description></item></channel></rss>')


@pytest.mark.parametrize("status,text,expected", [
    (500, _RSS_OK, "HTTP_ERROR"),
    (404, _RSS_OK, "HTTP_ERROR"),
    (403, _RSS_OK, "BLOCKED"),
    (200, "<<< not rss at all", "PARSE_ERROR"),
    (200, "", "PARSE_ERROR"),
    (200, "   \n  ", "PARSE_ERROR"),
])
def test_blipblop_faults_recorded_not_success(db, monkeypatch, status, text, expected):
    sid = _blip(db)
    # B5 fixture update: BlipBlop goes through http_get, which
    # streams since the body-cap fix — patch both entry points.
    _patch_http(monkeypatch, _Resp(status=status, text=text))
    f = BlipBlopFetcher(db, sid)
    res = f.run("https://blipblop.net/feed/")  # must not raise
    assert res["ok"] is False and res["result"] == expected
    run = _last_fetch_run(db)
    assert run["result"] == expected and run["result"] != "OK"
    assert run["fetcher_kind"] == "RSS"
    if expected == "BLOCKED":
        assert db.execute(
            "SELECT COUNT(*) FROM blocked_sources").fetchone()[0] == 1


def test_blipblop_timeout_recorded(db, monkeypatch):
    sid = _blip(db)

    def boom(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(httpx, "get", boom)
    # B5: http_get streams — the timeout must fire there too.
    monkeypatch.setattr(httpx, "stream", boom)
    res = BlipBlopFetcher(db, sid).run("https://blipblop.net/feed/")
    assert res["ok"] is False and res["result"] == "TIMEOUT"
    assert _last_fetch_run(db)["result"] == "TIMEOUT"


def test_blipblop_valid_feed_still_succeeds(db, monkeypatch):
    sid = _blip(db)
    # B5: BlipBlop goes through http_get (streaming since the
    # body-cap fix) — patch both entry points.
    _patch_http(monkeypatch, _Resp(status=200, text=_RSS_OK))
    res = BlipBlopFetcher(db, sid).run("https://blipblop.net/feed/")
    assert res["ok"] is True and len(res["items"]) == 1
    assert _last_fetch_run(db)["result"] == "OK"


# --------------------------------------------------------------------------
# 1d. Telegram token expiry (401)
# --------------------------------------------------------------------------

def test_fetch_updates_401_raises_telegram_error_repeatedly(db, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=401))
    for _ in range(3):  # the poll-equivalent caller can keep looping
        with pytest.raises(TelegramError) as ei:
            fetch_updates("dummy-token", offset=1, timeout=1)
        assert "401" in str(ei.value)


def test_poll_error_recording_path_exists_in_main():
    # main.py is owned by another worker; its poll-error logger is a
    # closure inside run(), so it cannot be imported in isolation. This
    # source-level guard pins the existing recording path: fetch_updates
    # failures are caught, appended to poll_errors.log, and the loop
    # continues after a sleep instead of dying.
    src = (Path(__file__).resolve().parents[1] / "src" / "grt"
           / "main.py").read_text(encoding="utf-8")
    assert "poll_errors.log" in src
    assert "_log_poll_error(\"fetch_updates\", exc)" in src
    assert "continue" in src


def test_poll_error_redacts_telegram_token_from_message_and_traceback():
    fake = "123456789:AA" + "x" * 32
    cleaned = _redact_poll_error(
        f"request failed https://api.telegram.org/bot{fake}/getUpdates\n"
        f"TelegramError: {fake}")
    assert fake not in cleaned
    assert cleaned.count("[REDACTED_TELEGRAM_TOKEN]") == 2


def test_poll_loop_equivalent_survives_401_and_logs(db, monkeypatch, tmp_path):
    # Poll-loop equivalent of main.poll_loop's error branch, driven with
    # the real fetch_updates against a stubbed 401 endpoint: three
    # consecutive 401s are each recorded and the loop keeps going.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=401))
    log = tmp_path / "poll_errors.log"
    handled = 0
    for _ in range(3):
        try:
            fetch_updates("dummy-token", offset=None, timeout=1)
        except Exception as exc:  # same containment as main.poll_loop
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"fetch_updates: {exc!r}\n")
            handled += 1
            continue
    assert handled == 3
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3 and all("401" in ln for ln in lines)


def test_send_401_goes_to_retry_not_crash(db, monkeypatch):
    set_setting(db, "allowed_chat_id", "111")
    outbox.enqueue(db, event_type="new_listing", body="tok",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.com/401", quiet_exempt=True)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(status=401))
    from grt.telegram_io import HttpTransport
    stats = outbox.process_outbox(db, HttpTransport(token="dummy"))
    assert stats["sent"] == 0 and stats["failed"] == 1
    row = db.execute("SELECT state, attempts, last_error"
                     " FROM notification_outbox").fetchone()
    assert row[0] == "failed_retryable" and row[1] == 1
    assert "401" in row[2]


# --------------------------------------------------------------------------
# 1e. DB write-lock contention
# --------------------------------------------------------------------------

def _db_path(db):
    return db.execute("PRAGMA database_list").fetchone()[2]


class _Locker:
    """Holds BEGIN IMMEDIATE on a second connection until released."""

    def __init__(self, path):
        self.path = path
        self.held = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('lock_probe', '1')"
            " ON CONFLICT(key) DO UPDATE SET value='1'")
        self.held.set()
        self.release.wait(timeout=15)
        conn.commit()
        conn.close()

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(timeout=10)
        return self

    def __exit__(self, *a):
        self.release.set()
        self.thread.join(timeout=15)


def _short_conn(path):
    conn = sqlite3.connect(path, timeout=1, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def test_lock_brief_then_writes_succeed(db):
    path = _db_path(db)
    sid = _source(db)
    locker = _Locker(path)
    locker.thread.start()
    assert locker.held.wait(timeout=10)

    def release_after(sec):
        import time as _t
        _t.sleep(sec)
        locker.release.set()
    threading.Thread(target=release_after, args=(0.6,), daemon=True).start()
    # Both calls block briefly on the lock, then succeed (no loss).
    rid = outbox.enqueue(db, event_type="new_listing", body="locked-brief",
                         edition_id="AR-000001", source="discogs",
                         listing_url="https://example.com/lock",
                         quiet_exempt=True)
    assert rid is not None
    run_id = health.record_fetch_run(db, sid, result="OK", items_extracted=1)
    assert run_id is not None
    locker.thread.join(timeout=15)
    assert db.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 1


def test_lock_long_outbox_process_contained_and_recovers(db, tmp_path):
    path = _db_path(db)
    set_setting(db, "allowed_chat_id", "111")
    short = _short_conn(path)
    try:
        assert outbox.enqueue(
            short, event_type="new_listing", body="locked-long",
            edition_id="AR-000001", source="discogs",
            listing_url="https://example.com/lock2",
            quiet_exempt=True) is not None
        with _Locker(path):
            t = RecordingTransport()
            stats = outbox.process_outbox(short, t)  # must not raise
            assert stats["sent"] == 0 and stats["db_errors"] >= 1
            assert t.sent == []
        # After the lock is gone the same row is still pending and the
        # next pass delivers it — nothing was lost or marked sent.
        row = short.execute(
            "SELECT state FROM notification_outbox").fetchone()
        assert row[0] == "pending"
        t = RecordingTransport()
        stats = outbox.process_outbox(short, t)
        assert stats["sent"] == 1 and len(t.sent) == 1
    finally:
        short.close()


def test_lock_long_health_and_enqueue_fail_visibly_then_recover(db):
    path = _db_path(db)
    sid = _source(db)
    short = _short_conn(path)
    try:
        with _Locker(path):
            # health: the failure raises (visible, not a fake success)
            # and records nothing.
            with pytest.raises(sqlite3.OperationalError):
                health.record_fetch_run(short, sid, result="OK",
                                        items_extracted=1)
            short.rollback()
            # outbox enqueue: returns None and writes no phantom row.
            assert outbox.enqueue(
                short, event_type="x", body="y",
                idempotency_key="lock-k", quiet_exempt=True) is None
            short.rollback()
        assert db.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
        # Recovery: the same operations succeed once the lock clears.
        assert health.record_fetch_run(short, sid, result="OK",
                                       items_extracted=1) is not None
        assert outbox.enqueue(short, event_type="x", body="y",
                              idempotency_key="lock-k",
                              quiet_exempt=True) is not None
    finally:
        short.close()


# --------------------------------------------------------------------------
# 2a. 9-C22/C4: candidate-review buttons are actually sent via the outbox
# --------------------------------------------------------------------------

def _candidate(db, title="Review Me", fingerprint="fp-r5", **kw):
    cur = db.execute(
        "INSERT INTO candidate_editions(title, raw_title, source_url,"
        " fingerprint, existence_confidence, price_confidence, found_at,"
        " status) VALUES(?, ?, 'https://example.com/cand', ?, '中', '低',"
        " ?, 'pending')",
        (title, title, fingerprint, clock.now_iso(db)))
    db.commit()
    return cur.lastrowid


def test_blipblop_registration_queues_one_button_message(db):
    from conftest import set_clock_to
    set_clock_to(12)
    set_setting(db, "allowed_chat_id", "111")
    sid = _blip(db)
    f = BlipBlopFetcher(db, sid)
    items = [{"title": "New Game OST Vinyl", "url": "https://example.com/n1",
              "summary": "s", "published": ""}]
    assert f.register_candidates(items)["created"] == 1
    # Re-registering the same feed item creates nothing new.
    assert f.register_candidates(items)["created"] == 0
    rows = db.execute(
        "SELECT * FROM notification_outbox WHERE event_type='candidate_review'"
    ).fetchall()
    assert len(rows) == 1  # exactly one review notification per candidate
    cid = db.execute("SELECT id FROM candidate_editions").fetchone()[0]
    assert rows[0]["idempotency_key"] == f"candidate_review|{cid}"
    # Direct sweep over the queue must not duplicate it either.
    assert outbox.enqueue_pending_candidate_reviews(db) == 0
    t = RecordingTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] >= 1
    review_sends = [(txt, mk) for (chat, txt), mk in zip(t.sent, t.markups)
                    if "候補レビュー" in txt]
    assert len(review_sends) == 1
    txt, markup = review_sends[0]
    assert "New Game OST Vinyl" in txt
    buttons = markup["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["承認", "却下"]
    assert [b["callback_data"] for b in buttons] == [
        f"approve:{cid}", f"reject:{cid}"]


def test_pending_review_sweep_for_manually_queued_rows(db):
    from conftest import set_clock_to
    set_clock_to(12)
    set_setting(db, "allowed_chat_id", "111")
    cid = _candidate(db)
    db.execute(
        "INSERT INTO review_queue(kind, payload_json, fingerprint,"
        " priority, created_at) VALUES('candidate_review', ?, 'fp-r5', 0, ?)",
        (json.dumps({"candidate_id": cid, "source": "manual"}),
         clock.now_iso(db)))
    db.commit()
    assert outbox.enqueue_pending_candidate_reviews(db) == 1
    assert outbox.enqueue_pending_candidate_reviews(db) == 0  # dedup
    t = RecordingTransport()
    outbox.process_outbox(db, t)
    markups = [mk for mk in t.markups if mk]
    assert len(markups) == 1
    assert markups[0]["inline_keyboard"][0][0]["callback_data"] == \
        f"approve:{cid}"


def test_pending_review_sweep_skips_malformed_payloads(db):
    """Regression (debugger-16): a review_queue row whose payload has a
    non-numeric / missing candidate_id (or is not a JSON object at all)
    used to raise ValueError/AttributeError out of
    enqueue_pending_candidate_reviews, aborting the sweep. Malformed
    rows are skipped; valid rows in the same sweep still queue."""
    cid = _candidate(db)
    bad_payloads = [
        json.dumps({"candidate_id": "abc"}),
        json.dumps({"candidate_id": None}),
        json.dumps({"other": 1}),
        json.dumps([1, 2, 3]),
        "not-json",
    ]
    for i, payload in enumerate(bad_payloads):
        db.execute(
            "INSERT INTO review_queue(kind, payload_json, fingerprint,"
            " priority, created_at) VALUES('candidate_review', ?, ?, 0, ?)",
            (payload, f"fp-bad-{i}", clock.now_iso(db)))
    db.execute(
        "INSERT INTO review_queue(kind, payload_json, fingerprint,"
        " priority, created_at) VALUES('candidate_review', ?, 'fp-good',"
        " 0, ?)",
        (json.dumps({"candidate_id": cid}), clock.now_iso(db)))
    db.commit()
    assert outbox.enqueue_pending_candidate_reviews(db) == 1


def test_review_message_queued_while_gate_closed_not_sent_directly(db):
    # Gate is closed (quiet exception never selected): the review is
    # queued in the outbox only. Nothing is sent by enqueue itself, and
    # delivery remains subject to the resident process's gate rule
    # (main.outbox_job runs process_outbox only when the gate is open).
    assert not bot_mod.gate_open(db)
    cid = _candidate(db)
    set_setting(db, "allowed_chat_id", "111")
    oid = outbox.enqueue_candidate_review(db, cid)
    assert oid is not None
    row = db.execute("SELECT state FROM notification_outbox WHERE id=?",
                     (oid,)).fetchone()
    assert row[0] == "pending"


def test_review_button_round_trip_through_bot(db):
    from conftest import set_clock_to
    set_clock_to(12)
    # The callback_data delivered on the button is accepted by the
    # already-implemented bot receiver and resolves the candidate once.
    set_setting(db, "allowed_chat_id", "111")
    cid = _candidate(db)
    outbox.enqueue_candidate_review(db, cid)
    t = RecordingTransport()
    outbox.process_outbox(db, t)
    markup = [mk for mk in t.markups if mk][0]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    bot = bot_mod.Bot(db, MockTransport())
    bot.handle_update({"update_id": 1, "message": {
        "chat": {"id": 111}, "text": "/start"}})
    bot.handle_update({"update_id": 2, "callback_query": {
        "id": "cb1", "message": {"chat": {"id": 111}}, "data": "quiet_off"}})
    action = bot.handle_update({"update_id": 3, "callback_query": {
        "id": "cb2", "message": {"chat": {"id": 111}}, "data": data}})
    assert action == "candidate_approved"
    assert db.execute("SELECT status FROM candidate_editions WHERE id=?",
                      (cid,)).fetchone()[0] == "approved"


def test_outbox_markup_survives_retry(db):
    from conftest import set_clock_to
    set_clock_to(12)
    set_setting(db, "allowed_chat_id", "111")
    cid = _candidate(db)
    outbox.enqueue_candidate_review(db, cid)
    t = RecordingTransport()
    t.fail_next(1)
    outbox.process_outbox(db, t)
    assert t.sent == []
    advance(60)
    outbox.process_outbox(db, t)
    assert len(t.sent) == 1 and t.markups[0] is not None


# --------------------------------------------------------------------------
# 2b. 9-C12: Discogs appearance notification carries no price
# --------------------------------------------------------------------------

def test_discogs_appearance_notification_has_no_price(db, monkeypatch):
    sid = _discogs_db(db)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency)"
        " VALUES('AR-000001', 'discogs', ?, 0, NULL, NULL)",
        (clock.to_iso(clock.now(db) - timedelta(days=1)),))
    db.commit()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=200, payload={
        "num_for_sale": 1,
        "lowest_price": {"value": 9999, "currency": "JPY"}}))
    snap = DiscogsFetcher(db, sid, token="dummy").snapshot_edition("AR-000001")
    assert snap["lowest_price"] == 9999  # history snapshot keeps the price
    row = db.execute(
        "SELECT * FROM notification_outbox WHERE event_type='new_listing'"
    ).fetchone()
    assert row is not None
    assert "9999" not in row["body"]
    assert "価格（出品価格）" not in row["body"]
    assert "確信度: 存在・高 / 価格・-" in row["body"]
    assert row["last_notified_price"] is None
    # ... while price_snapshots (history only, never notified) has it.
    hist = db.execute(
        "SELECT lowest_price FROM price_snapshots ORDER BY taken_at DESC"
        " LIMIT 1").fetchone()
    assert hist[0] == 9999


# --------------------------------------------------------------------------
# 2c. 9-A2(1)/9-B4: DEGRADED transition sends exactly one Telegram warning
# --------------------------------------------------------------------------

def test_degraded_transition_warns_once_per_episode(db):
    from conftest import set_clock_to
    set_clock_to(12)
    set_setting(db, "allowed_chat_id", "111")
    sid = _source(db, name="flaky-shop")
    for _ in range(3):
        health.record_fetch_run(db, sid, result="HTTP_ERROR")
    rows = db.execute(
        "SELECT * FROM notification_outbox"
        " WHERE event_type='source_degraded'").fetchall()
    assert len(rows) == 1
    assert "DEGRADED" in rows[0]["body"] and "flaky-shop" in rows[0]["body"]
    # More failures in the same episode: no duplicate warning.
    health.record_fetch_run(db, sid, result="HTTP_ERROR")
    health.record_fetch_run(db, sid, result="HTTP_ERROR")
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox"
        " WHERE event_type='source_degraded'").fetchone()[0] == 1
    # The warning is actually delivered through the outbox.
    t = RecordingTransport()
    outbox.process_outbox(db, t)
    assert any("ソース劣化警告" in txt for _, txt in t.sent)
    # Recovery, then a NEW degradation episode warns again (key is
    # per-episode, not per-source forever).
    health.record_fetch_run(db, sid, result="OK", items_extracted=2)
    health.record_fetch_run(db, sid, result="OK", items_extracted=2)
    assert db.execute("SELECT state FROM source_health WHERE source_id=?",
                      (sid,)).fetchone()[0] == "HEALTHY"
    for _ in range(3):
        health.record_fetch_run(db, sid, result="HTTP_ERROR")
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox"
        " WHERE event_type='source_degraded'").fetchone()[0] == 2


# --------------------------------------------------------------------------
# 2d. 9-B13 + 9-A2(2): digest fetcher-kind aggregation, DOWN pinned on top
# --------------------------------------------------------------------------

def test_health_digest_uses_fetcher_kind_not_source_kind(db):
    sid_api = _source(db, name="some-shop", kind="shop")
    sid_rss = _source(db, name="some-community", kind="community")
    health.record_fetch_run(db, sid_api, result="OK", items_extracted=3,
                            fetcher_kind="API")
    health.record_fetch_run(db, sid_api, result="HTTP_ERROR",
                            fetcher_kind="API")
    health.record_fetch_run(db, sid_rss, result="BLOCKED", http_status=403,
                            fetcher_kind="RSS")
    text = digest.build_health_digest(db)
    assert "API: 1/1/0" in text
    assert "RSS: 0/0/1" in text
    assert "shop:" not in text and "community:" not in text


def test_health_digest_down_pinned_at_top(db):
    sid = _source(db, name="down-shop")
    health.record_fetch_run(db, sid, result="OK", items_extracted=3,
                            fetcher_kind="API")
    db.execute(
        "UPDATE source_health SET state='DOWN', consecutive_failures=9"
        " WHERE source_id=?", (sid,))
    db.commit()
    text = digest.build_health_digest(db)
    lines = text.splitlines()
    assert lines[1].startswith("■ DOWN中のソース")
    assert text.index("DOWN中のソース") < text.index("取得結果")


# --------------------------------------------------------------------------
# 2e. 9-A11: deadline sweep kinds + quiet-override wiring
# --------------------------------------------------------------------------

def _schedule(db, *, kind, confidence="confirmed", edition_id=None,
              candidate_id=None, hours_ahead=5):
    deadline = clock.now(db) + timedelta(hours=hours_ahead)
    cur = db.execute(
        "INSERT INTO release_schedule(edition_id, candidate_id, channel,"
        " deadline_raw, deadline_tz, deadline_jst, deadline_confidence,"
        " deadline_kind) VALUES(?, ?, 'shop', 'raw', 'JST', ?, ?, ?)",
        (edition_id, candidate_id, clock.to_iso(deadline), confidence, kind))
    db.commit()
    return cur.lastrowid


def test_deadline_sweep_excludes_campaign_end_and_unknown(db):
    set_clock_to(21, 45)
    _schedule(db, kind="hard_cutoff")
    _schedule(db, kind="guarantee_cutoff")
    _schedule(db, kind="campaign_end")
    _schedule(db, kind="unknown")
    _schedule(db, kind=None)
    _schedule(db, kind="hard_cutoff", confidence="tz_unknown")
    sent = digest.deadline_sweep(db)
    assert sent == 2
    bodies = [r[0] for r in db.execute(
        "SELECT body FROM notification_outbox"
        " WHERE event_type='deadline_sweep'").fetchall()]
    assert any("hard_cutoff" in b for b in bodies)
    assert any("guarantee_cutoff" in b for b in bodies)
    assert not any("campaign_end" in b for b in bodies)


def test_deadline_sweep_quiet_override_on_wires_flag_and_reason(db):
    set_clock_to(23, 0)  # inside quiet hours 22:00-06:30
    set_setting(db, "allowed_chat_id", "111")
    set_setting(db, "deadline_override_quiet", "1")
    db.execute(
        "INSERT INTO editions(id, title, distribution_channel)"
        " VALUES('AR-000777', 'Made Order OST', 'made_to_order')")
    db.commit()
    _schedule(db, kind="hard_cutoff", edition_id="AR-000777", hours_ahead=6)
    assert digest.deadline_sweep(db) == 1
    row = db.execute(
        "SELECT * FROM notification_outbox"
        " WHERE event_type='deadline_sweep'").fetchone()
    assert row["quiet_override"] == 1
    assert "hard_cutoff" in (row["quiet_override_reason"] or "")
    # Not held by quiet: deliverable right now, and the health digest
    # reports the override with its reason.
    t = RecordingTransport()
    assert outbox.process_outbox(db, t)["sent"] == 1
    health_text = digest.build_health_digest(db)
    assert "静穏例外の発動: 1件" in health_text


def test_deadline_sweep_quiet_override_off_holds_in_quiet(db):
    set_clock_to(23, 0)
    set_setting(db, "allowed_chat_id", "111")
    set_setting(db, "deadline_override_quiet", "0")
    db.execute(
        "INSERT INTO editions(id, title, distribution_channel)"
        " VALUES('AR-000777', 'Made Order OST', 'made_to_order')")
    db.commit()
    _schedule(db, kind="hard_cutoff", edition_id="AR-000777", hours_ahead=6)
    assert digest.deadline_sweep(db) == 1
    row = db.execute(
        "SELECT * FROM notification_outbox"
        " WHERE event_type='deadline_sweep'").fetchone()
    assert row["quiet_override"] == 0
    t = RecordingTransport()
    assert outbox.process_outbox(db, t)["sent"] == 0  # held to quiet end
    assert clock.parse_iso(row["not_before"]).hour == 6


def test_deadline_sweep_override_via_priority1_want_and_kind_guard(db):
    set_clock_to(23, 0)
    set_setting(db, "deadline_override_quiet", "1")
    db.execute("INSERT INTO editions(id, title) VALUES('AR-000888', 'W')")
    db.execute(
        "INSERT INTO wantlist(edition_id, priority) VALUES('AR-000888', 1)")
    db.commit()
    _schedule(db, kind="hard_cutoff", edition_id="AR-000888", hours_ahead=6)
    # guarantee_cutoff never gets the quiet exception (agreement-28).
    _schedule(db, kind="guarantee_cutoff", edition_id="AR-000888",
              hours_ahead=5)
    assert digest.deadline_sweep(db) == 2
    rows = db.execute(
        "SELECT quiet_override FROM notification_outbox"
        " WHERE event_type='deadline_sweep' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1, 0]


# --------------------------------------------------------------------------
# 2f. 9-C8: discovery digest band sections + carry-over reporting
# --------------------------------------------------------------------------

def _digest_candidate(db, title, fingerprint, *, found_via=None,
                      existence="中", distribution_type=None, days_ago=0):
    found = clock.now(db) - timedelta(days=days_ago)
    cur = db.execute(
        "INSERT INTO candidate_editions(title, raw_title, source_url,"
        " fingerprint, existence_confidence, price_confidence, found_at,"
        " status, found_via, distribution_type)"
        " VALUES(?, ?, 'https://example.com/x', ?, ?, '低', ?, 'pending',"
        " ?, ?)",
        (title, title, fingerprint, existence, clock.to_iso(found),
         found_via, distribution_type))
    db.commit()
    return cur.lastrowid


def test_discovery_digest_bands_and_carry_over(db):
    cid = _digest_candidate(db, "Top Candidate", "fp-top",
                            found_via="blipblop_rss", days_ago=1)
    db.execute("UPDATE candidate_editions SET source_urls_json=? WHERE id=?",
               (json.dumps(["https://example.com/x", "https://other.example/y"]), cid))
    db.commit()
    _digest_candidate(db, "Floor Candidate", "fp-floor",
                      distribution_type="event_only", days_ago=1)  # floor
    _digest_candidate(db, "Plain Candidate", "fp-plain",
                      existence="低", days_ago=0)              # score 0
    text = digest.build_discovery_digest(db)
    i_imm = text.index("即時帯")
    i_top = text.index("ダイジェスト先頭帯")
    i_plain = text.index("通常ダイジェスト帯")
    assert i_imm < i_top < i_plain
    assert "Floor Candidate" in text[i_imm:i_top]
    assert "Top Candidate" in text[i_top:i_plain]
    assert "Plain Candidate" in text[i_plain:]
    # Two candidates were found yesterday and are still pending: they
    # are carried over, and the count is reported (not silently dropped).
    assert "前日からの繰越: 2件" in text
    assert "繰越申告" in text and "2件" in text


def test_send_daily_digests_discovery_body_has_bands_and_carry(db):
    set_setting(db, "allowed_chat_id", "111")
    _digest_candidate(db, "Top Candidate", "fp-top2",
                      found_via="blipblop_rss", days_ago=2)
    ids = digest.send_daily_digests(db)
    assert ids["discovery"] is not None
    t = RecordingTransport()
    outbox.process_outbox(db, t)
    bodies = [txt for _, txt in t.sent if "発見ダイジェスト" in txt]
    assert len(bodies) == 1
    assert "ダイジェスト先頭帯" in bodies[0]
    assert "前日からの繰越: 1件" in bodies[0]
