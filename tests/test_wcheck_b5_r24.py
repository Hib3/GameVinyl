"""W-check Block 5 R24 follow-up regression tests.

The R24 seven-persona re-check found the R23 fixes correct on
their main paths but defective at their own margins — the same
lesson as R19, one level down. Each test pins one R24 finding:
non-finite/negative numeric value classes escaping the B5
defensive helpers, a freshness guard that counted failed
attempts, a cancelled deadline resurrected by the next harvest,
a bridge hint that promised what approval would not do, and
their smaller siblings.
"""
import json
from datetime import timedelta

import pytest

from grt import (candidates, clock, deadlines_ops, digest,
                 evidence, outbox, scoring, seedops)
from grt.bot import Bot
from grt.db import init_schema
from grt.fetchers.doujin_event import DoujinEventFetcher
from grt.normalize import fingerprint_for
from tests.test_wcheck_b4_r19 import (_add_event, _bot, _candidate,
                                      _cb, _cmd, _edition,
                                      _queue_row)
from tests.test_wcheck_b5 import _Send, _outbox_row, _sched


# ---------------------------------------------------------------------------
# Numeric margins: Inf / negative / huge (AI engineer 中-A / 中-B / 軽微-C)
# ---------------------------------------------------------------------------

def test_attempts_of_is_total():
    assert outbox._attempts_of(float("inf")) == 0
    assert outbox._attempts_of(float("nan")) == 0
    assert outbox._attempts_of(-200) == 0
    assert outbox._attempts_of("abc") == 0
    assert outbox._attempts_of(3) == 3
    assert outbox._attempts_of(None) == 0


def test_process_outbox_survives_inf_attempts(db):
    from conftest import set_clock_to
    set_clock_to(12)
    poison = _outbox_row(db, state="pending",
                         attempts=float("inf"))
    good = _outbox_row(db, state="pending", attempts=0)
    stats = outbox.process_outbox(db, _Send(fail=True),
                                  chat_id="555")
    for rid in (poison, good):
        row = db.execute(
            "SELECT state FROM notification_outbox WHERE id=?",
            (rid,)).fetchone()
        assert row[0] == "failed_retryable"


def test_recover_sending_survives_negative_attempts(db):
    old = "2020-01-01T00:00:00+00:00"
    for attempts in (-200, 0):
        db.execute(
            "INSERT INTO notification_outbox(event_type, body,"
            " state, attempts, claimed_at, idempotency_key)"
            " VALUES('t', 'b', 'sending', ?, ?, ?)",
            (attempts, old, f"neg-{attempts}"))
    db.commit()
    n = outbox.recover_sending(db, stale_minutes=10)
    assert n == 2  # the IndexError used to roll back BOTH


def test_price_command_survives_inf_snapshot(db, monkeypatch):
    _edition(db)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency) VALUES('AR-1',"
        " 'discogs', ?, ?, ?, 'JPY')",
        (clock.now_iso(db), float("inf"), float("inf")))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/price AR-1") == "price"
    assert sent


def test_price_command_survives_inf_bucket(db, monkeypatch):
    _edition(db)
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, cond_rank) VALUES('AR-1',"
        " 'shopA', 'https://shop.example/i', NULL, 'JPY', ?,"
        " 'active', 3)", (float("inf"),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/price AR-1") == "price"
    text = "\n".join(t for t, _m in sent)
    assert "円価格なし" in text


def test_yen_value_survives_huge_ints():
    assert scoring.yen_value(10 ** 400, None, None) is None
    assert scoring.yen_value(None, 10 ** 400, "JPY") is None


# ---------------------------------------------------------------------------
# Freshness / absence margins (seller F-1 / F-2 / F-3 / F-4)
# ---------------------------------------------------------------------------

def _group_with_member_runs(db, runs):
    db.execute(
        "INSERT INTO sources(id, name, kind, status) VALUES(1,"
        " 'ShopA', 'STORE', 'approved'), (2, 'ShopB', 'STORE',"
        " 'approved')")
    db.execute(
        "INSERT INTO listings(id, source, url, status,"
        " physical_copy_id) VALUES(1, 'ShopA', 'https://a/1',"
        " 'active', 7), (2, 'ShopB', 'https://b/1', 'active', 7)")
    for result in runs:
        db.execute(
            "INSERT INTO fetch_runs(source_id, started_at, result)"
            " VALUES(2, ?, ?)", (clock.now_iso(db), result))
    db.commit()


def test_group_blocked_only_by_successful_member_source(db):
    _group_with_member_runs(db, ["BLOCKED", "FAILED"])
    evidence.apply_absence_round(db, {"ShopA": set()})
    evidence.apply_absence_round(db, {"ShopA": set()})
    st = {r[0]: r[1] for r in db.execute(
        "SELECT id, status FROM listings")}
    # A source that runs but never succeeds does NOT freeze the
    # group (pre-fix: any recent run counted as 'fresh').
    assert st[1] != "active"


def test_group_still_blocked_by_ok_member_source(db):
    _group_with_member_runs(db, ["OK"])
    evidence.apply_absence_round(db, {"ShopA": set()})
    evidence.apply_absence_round(db, {"ShopA": set()})
    st = {r[0]: r[1] for r in db.execute(
        "SELECT id, status FROM listings")}
    assert st[1] == "active"


def test_mark_absent_does_not_re_end_an_ended_listing(db):
    db.execute(
        "INSERT INTO listings(id, source, url, status,"
        " absent_streak) VALUES(1, 'Shop', 'https://a/1',"
        " 'ended', 2)")
    db.commit()
    assert evidence.mark_absent(db, 1, True) == "ended"
    row = db.execute(
        "SELECT absent_streak FROM listings WHERE id=1").fetchone()
    assert row[0] == 2  # not inflated
    n = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='listing_ended'").fetchone()[0]
    assert n == 0  # no duplicate audit event


def test_partial_group_round_counts_nobody_absent(db):
    db.execute(
        "INSERT INTO listings(id, source, url, status,"
        " physical_copy_id) VALUES(1, 'Shop', 'https://a/1',"
        " 'active', 7), (2, 'Shop', 'https://a/2', 'active', 7)")
    db.commit()
    out = evidence.apply_absence_round(
        db, {"Shop": {"https://a/1"}})
    assert out["absent"] == 0


def test_bucket_best_rejects_unreadable_last_seen(db):
    from tests.test_wcheck_b5 import _listing_with_decision
    from grt import pricedrop
    _listing_with_decision(db, last_seen=None)
    assert pricedrop.bucket_best(db, "AR-1", None, 3) is None


def test_coverage_rejects_text_amount_and_garbage_seen(
        db, monkeypatch):
    _edition(db)
    now = clock.now_iso(db)
    # AR-1 pins seller F-4 exactly: the ONLY amount is a TEXT
    # total_jpy (price is NULL, so the legitimate JPY-price
    # branch cannot rescue it) — pre-fix it counted via SQLite
    # type ordering (TEXT > INTEGER).
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, last_seen) VALUES('AR-1',"
        " 's', 'https://s.example/t', NULL, 'JPY', 'abc',"
        " 'active', ?)", (now,))
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm)"
        " VALUES('AR-2', '盤2', 'KICA-1002')")
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, last_seen) VALUES('AR-2',"
        " 's', 'https://s.example/g', 3000, 'JPY', 3000,"
        " 'active', 'garbage')")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/coverage") == "coverage"
    text = "\n".join(t for t, _m in sent)
    assert "価格証拠あり: 0件" in text


# ---------------------------------------------------------------------------
# Deadline tombstone + conflict (wholesaler 中-A / 中-B / 軽微-C)
# ---------------------------------------------------------------------------

def test_cancelled_deadline_is_not_resurrected_by_harvest(db):
    _edition(db)
    sid = _sched(db, channel="doujin_event", jst="2027-02-07")
    assert deadlines_ops.cancel_schedule(db, sid) is True
    again = _sched(db, channel="doujin_event", jst="2027-02-07")
    assert again == sid  # suppressed onto the tombstone
    live = db.execute(
        "SELECT COUNT(*) FROM release_schedule WHERE"
        " superseded_by IS NULL").fetchone()[0]
    assert live == 0
    _sched(db, channel="doujin_event", jst="2027-02-07")
    n = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='deadline_reregister_suppressed'").fetchone()[0]
    assert n == 1  # recorded once per tombstone


def test_manual_reregistration_revives_a_cancelled_deadline(db):
    _edition(db)
    sid = _sched(db, channel="doujin_event", jst="2027-02-07")
    assert deadlines_ops.cancel_schedule(db, sid) is True
    new = _sched(db, channel="manual", jst="2027-02-07")
    assert new != sid
    live = db.execute(
        "SELECT COUNT(*) FROM release_schedule WHERE"
        " superseded_by IS NULL").fetchone()[0]
    assert live == 1


def test_cross_channel_conflict_is_surfaced(db):
    _edition(db, "AR-1", "競合タイトル")
    in3 = (clock.now(db) + timedelta(days=3)).date().isoformat()
    in4 = (clock.now(db) + timedelta(days=4)).date().isoformat()
    _sched(db, channel="manual", jst=in3, kind="guarantee_cutoff")
    # The doujin lane's FIRST observation is already the new
    # date — no moved_from, so B5 propagation cannot fire.
    _sched(db, channel="doujin_event", jst=in4,
           kind="guarantee_cutoff")
    n = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='deadline_channel_conflict'").fetchone()[0]
    assert n == 1
    lines = deadlines_ops.deadline_digest_lines(db)
    assert any("締切の競合" in ln and "競合タイトル" in ln
               for ln in lines)


def test_offset_carrying_value_converts_without_tz(db):
    _edition(db)
    rid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", deadline_raw="x",
        deadline_tz=None,
        deadline_jst="2027-02-07T00:00:00+09:00",
        deadline_confidence="confirmed",
        deadline_kind="hard_cutoff")
    row = db.execute(
        "SELECT deadline_jst FROM release_schedule WHERE id=?",
        (rid,)).fetchone()
    assert row[0] == "2027-02-07T00:00:00+09:00"


def test_new_decoration_words_fold():
    assert fingerprint_for("【早期予約】星空サントラ", None) \
        == fingerprint_for("星空サントラ", None)
    assert fingerprint_for("【先行予約】星空サントラ", None) \
        == fingerprint_for("星空サントラ", None)


# ---------------------------------------------------------------------------
# Presentation honesty (collector 中-A / 軽微-エ, maniac B N-5)
# ---------------------------------------------------------------------------

def _bridge_candidate(db, monkeypatch, signals):
    _edition(db, "AR-A", "盤A")
    _edition(db, "AR-B", "盤B")
    cid = _candidate(db, title="橋渡し候補")
    for eid in signals:
        db.execute(
            "INSERT INTO repress_signals(edition_id, candidate_id,"
            " system, at) VALUES(?, ?, 'announcement', ?)",
            (eid, cid, clock.now_iso(db)))
    db.commit()
    _queue_row(db, "candidate_review", {"candidate_id": cid},
               priority=10)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    return "\n".join(t for t, _ in sent)


def test_bridge_hint_multiple_signals(db, monkeypatch):
    text = _bridge_candidate(db, monkeypatch, ["AR-A", "AR-B"])
    assert "複数の盤へのシグナル" in text
    assert "承認するとこの盤に紐付きます" not in text


def test_bridge_hint_ghost_signal(db, monkeypatch):
    text = _bridge_candidate(db, monkeypatch, ["AR-GHOST"])
    assert "承認するとこの盤に紐付きます" not in text
    assert "新規" in text


def test_bridge_hint_single_existing_signal(db, monkeypatch):
    text = _bridge_candidate(db, monkeypatch, ["AR-A"])
    assert "既存目録との関連: AR-A" in text
    assert "承認するとこの盤に紐付きます" in text


def test_deadlines_confirmed_list_shows_titles(db, monkeypatch):
    _edition(db, "AR-1", "確定タイトル盤")
    _sched(db, channel="manual", jst="2027-02-07")
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/deadlines") == "deadlines"
    text = "\n".join(t for t, _m in sent)
    assert "確定タイトル盤" in text


# ---------------------------------------------------------------------------
# Queue margins (maniac B N-1 / N-3 / N-4, adversarial 軽微-1)
# ---------------------------------------------------------------------------

def _nonscalar_match_row(db):
    _edition(db)
    return _queue_row(db, "match_review", {
        "listing": {"source": "shopA", "url": ["not-a-string"]},
        "result": {"edition_id": "AR-1", "score": 80,
                   "decision_id": None}}, priority=45)


def test_match_review_nonscalar_url_reject_settles(
        db, monkeypatch):
    qid = _nonscalar_match_row(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qno:{qid}") == "match_review_rejected"
    row = db.execute(
        "SELECT resolution FROM review_queue WHERE id=?",
        (qid,)).fetchone()
    assert row[0] == "rejected"


def test_match_review_nonscalar_url_approve_unresolvable(
        db, monkeypatch):
    qid = _nonscalar_match_row(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "match_review_unresolvable"
    assert sent  # an honest reply, not silence


def test_match_review_nan_score_is_unresolvable(db, monkeypatch):
    _edition(db)
    cur = db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, raw_title) VALUES('AR-1',"
        " 'shopA', 'https://shop.example/i1', 3000, 'JPY', 3000,"
        " 'active', 'テスト盤')")
    lid = int(cur.lastrowid)
    cur = db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id,"
        " score, existence_confidence, price_confidence)"
        " VALUES(?, 'AR-1', 80, '高', '中')", (lid,))
    dec_id = int(cur.lastrowid)
    db.commit()
    qid = _queue_row(db, "match_review", {
        "listing": {"source": "shopA",
                    "url": "https://shop.example/i1"},
        "result": {"edition_id": "AR-1", "score": float("nan"),
                   "decision_id": dec_id}}, priority=45)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "match_review_unresolvable"


def test_priority_backfill_on_init(db):
    _queue_row(db, "match_review", {"listing": {}, "result": {}},
               priority=0)
    init_schema(db)
    row = db.execute(
        "SELECT priority FROM review_queue WHERE"
        " kind='match_review'").fetchone()
    assert row[0] == 45


def test_health_digest_counts_only_unresolved_suspects(db):
    now = clock.now_iso(db)
    db.execute(
        "INSERT INTO duplicate_suspects(kind, key, payload_json,"
        " created_at) VALUES('title', 'a', '{}', ?)", (now,))
    db.execute(
        "INSERT INTO duplicate_suspects(kind, key, payload_json,"
        " created_at, resolved_at) VALUES('title', 'b', '{}',"
        " ?, ?)", (now, now))
    db.commit()
    text = digest.build_health_digest(db)
    assert "目録の衝突疑い（未確認）: 1件" in text


def test_surrogate_command_argument_is_sanitised(db, monkeypatch):
    _edition(db)
    bot, sent = _bot(db, monkeypatch)
    action = _cmd(bot, "/watch \ud800abc")
    assert action != "invalid_update"
    assert sent  # a reply was delivered, not silence


# ---------------------------------------------------------------------------
# Doujin / seed margins (maniac A 中 fold, 軽微 dedupe)
# ---------------------------------------------------------------------------

def test_circleless_seed_does_not_fold_known_circle_candidate(
        db, monkeypatch):
    # A circle-less seed promotes through the CATALOG gate
    # (the title+circle gate requires both circles by design).
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="", event_name="C100",
        catalog_norm="ABC-001")
    victim = _candidate(
        db, title="深海サントラ",
        fp=fingerprint_for("深海サントラ", "", "波動"),
        found_via="doujin_event:1")
    qid = seedops.request_seed_promotion(
        db, seed_id, {"title": "深海サントラ", "circle": "",
                      "catalog_norm": "ABC-001"})
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "seed_promoted"
    row = db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (victim,)).fetchone()
    assert row[0] == "pending"  # NOT silently merged away


def test_circleless_seed_still_folds_circleless_candidate(
        db, monkeypatch):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="", event_name="C100",
        catalog_norm="ABC-001")
    victim = _candidate(
        db, title="深海サントラ",
        fp=fingerprint_for("深海サントラ", "", ""),
        found_via="doujin_event:1")
    qid = seedops.request_seed_promotion(
        db, seed_id, {"title": "深海サントラ", "circle": "",
                      "catalog_norm": "ABC-001"})
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "seed_promoted"
    row = db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (victim,)).fetchone()
    assert row[0] == "rejected"  # folded, as before


def test_event_start_unparseable_recorded_once(db):
    ev = _add_event(db, starts_on="未定")
    f = DoujinEventFetcher(db, 1)
    item = {"title": "同人サントラ", "url": "https://c.example/w1",
            "circle": "波動", "raw_title": "同人サントラ"}
    f.register_event_items([item], ev)
    f.register_event_items([item], ev)
    f.register_event_items([item], ev)
    n = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='event_start_unparseable'").fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# Evidence byte cap (adversarial 軽微-2)
# ---------------------------------------------------------------------------

def test_evidence_cap_is_in_bytes(db):
    import hashlib
    body = "あ" * 600_000  # 1.8MB as UTF-8
    rec = evidence.save_raw_evidence(
        "test", "https://shop.example/big", body, keep_body=False)
    assert rec["raw_hash"] == hashlib.sha256(
        body.encode()).hexdigest()
    _src, stored = evidence._PENDING_BODIES[rec["raw_hash"]]
    assert len(stored.encode("utf-8")) <= 1_000_000
