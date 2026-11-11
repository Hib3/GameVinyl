"""Phase 1 acceptance criteria (implementation prompt section 7).

Criteria 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13 here; criterion 2 in
test_golden.py; criterion 3 in test_watchdog.py. Anything that would need
a real Telegram send or a real Discogs call is exercised through
MockTransport / injected callables and is reported as a mock substitution
in the build report — nothing here touches the network.
"""
import json

import pytest

from conftest import advance, set_clock_to
from grt import bot as bot_mod
from grt import digest, evidence, health, migrate, outbox
from grt.config import set_setting
from grt.telegram_io import MockTransport


def _source(db, name="src1"):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status) VALUES(?, 'shop', 'approved')",
        (name,))
    db.commit()
    return cur.lastrowid


# --- Criterion 1: idempotent migration ------------------------------------

def test_criterion1_migration_idempotent(db):
    r1 = migrate.run_migration(db)
    r2 = migrate.run_migration(db)
    assert r1["after"] == r2["after"] == {
        "editions": 3777, "variants": 0, "price_links": 0}
    assert r2["inserted_this_run"] == 0


# --- Criterion 4: source health state machine ------------------------------

def test_criterion4_degraded_down_recovery(db):
    sid = _source(db)
    for _ in range(3):
        health.record_fetch_run(db, sid, result="HTTP_ERROR")
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "DEGRADED"
    ev = db.execute("SELECT COUNT(*) FROM system_events"
                    " WHERE kind='source_degraded'").fetchone()[0]
    assert ev >= 1
    advance(25 * 3600)
    health.evaluate_time_based_health(db)
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "DOWN"
    health.record_fetch_run(db, sid, result="OK", items_extracted=3)
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "DOWN"  # one success is not enough
    health.record_fetch_run(db, sid, result="OK", items_extracted=3)
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "HEALTHY"


# --- Criterion 5: zero-item anomaly -----------------------------------------

def test_criterion5_zero_item_suspect(db):
    sid = _source(db)
    for n in (12, 9, 11):
        health.record_fetch_run(db, sid, result="OK", items_extracted=n)
    health.record_fetch_run(db, sid, result="OK", items_extracted=0)
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "HEALTHY"  # first zero is not yet an anomaly
    health.record_fetch_run(db, sid, result="OK", items_extracted=0)
    state = db.execute("SELECT state FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()[0]
    assert state == "SUSPECT"
    last = db.execute("SELECT result FROM fetch_runs ORDER BY id DESC"
                      " LIMIT 1").fetchone()[0]
    assert last == "PARSE_EMPTY"


# --- Criterion 6: outbox idempotency ----------------------------------------

def test_criterion6_duplicate_event_single_send(db):
    from conftest import set_clock_to
    set_clock_to(12)
    set_setting(db, "allowed_chat_id", "111")
    kw = dict(event_type="new_listing", body="出品通知",
              edition_id="AR-000001", source="discogs",
              listing_url="https://www.discogs.com/sell/release/1")
    assert outbox.enqueue(db, **kw) is not None
    assert outbox.enqueue(db, **kw) is None  # duplicate suppressed
    rows = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
    assert rows == 1
    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] == 1
    assert len(t.sent) == 1


# --- Criterion 7: quiet hours hold -------------------------------------------

def test_criterion7_quiet_hold(db):
    set_setting(db, "allowed_chat_id", "111")
    set_clock_to(23, 0)
    outbox.enqueue(db, event_type="new_listing", body="夜間イベント",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.com/1")
    row = db.execute("SELECT not_before FROM notification_outbox").fetchone()
    t = MockTransport()
    outbox.process_outbox(db, t)
    assert t.sent == []  # held during quiet hours
    assert row[0] >= "06:30" or "T06:" in row[0]
    set_clock_to(7, 0, add_days=1)
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] == 1


# --- Criterion 8: retry schedule -> dead_letter ------------------------------

def test_criterion8_retry_then_dead_letter(db):
    set_setting(db, "allowed_chat_id", "111")
    outbox.enqueue(db, event_type="new_listing", body="失敗し続ける通知",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.com/2", quiet_exempt=True)
    t = MockTransport()
    t.fail_next(99)
    outbox.process_outbox(db, t)                     # attempt 1 -> +1min
    row = db.execute("SELECT attempts, state FROM notification_outbox").fetchone()
    assert (row[0], row[1]) == (1, "failed_retryable")
    # W-check B2 correction (maniac B D-1): the documented schedule
    # is 1m -> 5m -> 30m -> 2h -> 12h, but the old MAX_ATTEMPTS=5
    # dead-lettered on the 5th failure, so the 12h step was
    # unreachable and this test pinned the defect. The schedule now
    # runs all five delays; the 6th failure dead-letters.
    for step in (60, 300, 1800, 7200, 43200):        # 1m, 5m, 30m, 2h, 12h
        advance(step)
        outbox.process_outbox(db, t)
    row = db.execute("SELECT attempts, state FROM notification_outbox").fetchone()
    assert (row[0], row[1]) == (6, "dead_letter")
    assert outbox.dead_letter_count(db) == 1
    assert "dead_letter 1件" in digest.build_health_digest(db)


# --- Criterion 9: strict ending judgement -------------------------------------

def test_criterion9_ending_judgement(db):
    cur = db.execute(
        "INSERT INTO listings(edition_id, source, url, status, absent_streak)"
        " VALUES('AR-000001', 'discogs', 'https://x', 'active', 0)")
    lid = cur.lastrowid
    db.commit()
    assert evidence.mark_absent(db, lid, fetch_succeeded=False) is None
    st = db.execute("SELECT status FROM listings WHERE id=?", (lid,)).fetchone()[0]
    assert st == "active"  # failed fetches never count as absence
    assert evidence.mark_absent(db, lid, fetch_succeeded=True) == "sold_candidate"
    # a BLOCKED round in between does not reset or advance the streak
    assert evidence.mark_absent(db, lid, fetch_succeeded=False) is None
    assert evidence.mark_absent(db, lid, fetch_succeeded=True) == "ended"


# --- Criteria 10-12: bot gate, allowlist, update idempotency -------------------

def _bot(db):
    return bot_mod.Bot(db, MockTransport())


def _msg(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def _cb(uid, chat, data):
    return {"update_id": uid,
            "callback_query": {"message": {"chat": {"id": chat}}, "data": data}}


def test_criterion10_setup_gate(db):
    b = _bot(db)
    assert not bot_mod.gate_open(db)  # unselected state is detectable
    b.handle_update(_msg(1, 111, "/start"))
    action = b.handle_update(_msg(2, 111, "/watch AR-000001"))
    assert action == "gate_closed"
    sent_texts = [t for _, t in b.transport.sent]
    assert any("初期設定が未完了" in t for t in sent_texts)
    b.handle_update(_cb(3, 111, "quiet_off"))
    assert bot_mod.gate_open(db)


def test_criterion11_allowlist(db):
    b = _bot(db)
    b.handle_update(_msg(1, 111, "/start"))
    b.handle_update(_cb(2, 111, "quiet_off"))
    action = b.handle_update(_msg(3, 999, "/status"))
    assert action == "ignored_foreign"
    assert all(chat == "111" for chat, _ in b.transport.sent)
    action = b.handle_update(_msg(4, 111, "/status"))
    assert action == "status"


def test_criterion12_update_idempotency_and_double_approve(db):
    b = _bot(db)
    b.handle_update(_msg(1, 111, "/start"))
    b.handle_update(_cb(2, 111, "quiet_off"))
    upd = _msg(3, 111, "/watch AR-000001")
    db.execute("INSERT OR IGNORE INTO editions(id, title) VALUES('AR-000001', 'x')")
    db.commit()
    assert b.handle_update(upd) == "watch_on"
    assert b.handle_update(dict(upd)) == "duplicate_ignored"
    level = db.execute("SELECT watch_level FROM editions WHERE id='AR-000001'"
                       ).fetchone()[0]
    assert level == 2
    cur = db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status)"
        " VALUES('c', 'fp1', 'pending')")
    cid = cur.lastrowid
    db.commit()
    assert b.handle_update(_cb(4, 111, f"approve:{cid}")) == "candidate_approved"
    assert b.handle_update(_cb(5, 111, f"approve:{cid}")) == "already_resolved"
    st = db.execute("SELECT status FROM candidate_editions WHERE id=?",
                    (cid,)).fetchone()[0]
    assert st == "approved"


# --- Criterion 13: health digest contents --------------------------------------

def test_criterion13_health_digest(db):
    set_setting(db, "allowed_chat_id", "111")
    sid = _source(db)
    # 9-B13 fix: the digest aggregates by FETCHER kind (API/RSS/...),
    # not by sources.kind. _source registers sources.kind='shop', which
    # must therefore NOT appear as the aggregation key; these runs were
    # fetched via the API fetcher, so the correct line is "API: 1/0/1".
    # (The previous expectation "shop: 1/0/1" pinned the wrong taxonomy
    # in the test itself and has been corrected with the code.)
    health.record_fetch_run(db, sid, result="OK", items_extracted=4,
                            fetcher_kind="API")
    health.record_fetch_run(db, sid, result="BLOCKED", http_status=403,
                            fetcher_kind="API")
    outbox.enqueue(db, event_type="x", body="y", quiet_exempt=True,
                   idempotency_key="k1")
    db.execute("UPDATE notification_outbox SET state='dead_letter'")
    db.commit()
    text = digest.build_health_digest(db)
    assert "成功/失敗/BLOCKED" in text
    assert "API: 1/0/1" in text
    assert "shop:" not in text
    assert "dead_letter 1件" in text
    assert "公式の保存検索アラート依存" in text  # flea-market standing note
    assert "締切未確定" in text
    # Digest is delivered even on a zero-notification day.
    ids = digest.send_daily_digests(db)
    assert ids["health"] is not None
    t = MockTransport()
    outbox.process_outbox(db, t)
    assert any("日次ヘルスレポート" in body for _, body in t.sent)
