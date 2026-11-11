"""W-check Block 5 (R21 findings -> R22/R23 fixes) regression tests.

The final cross-cutting round: TEXT-poisoned numeric columns,
human-review guard keying, queue starvation/exits, ghost listing
freshness, deadline cancellation and cross-channel propagation,
doujin seed identity, operator-key coverage, and transport-level
adversarial inputs (surrogates, unbounded bodies). Each test pins
one verified R21 defect.
"""
import json
from datetime import timedelta

import pytest

from grt import (bot as bot_mod, candidates, clock, deadlines_ops,
                 digest, evidence, matcher, outbox, pricedrop,
                 repress, scoring, seedops)
from grt.bot import Bot, _parse_candidate_id
from grt.fetchers.doujin_event import (DoujinEventFetcher,
                                       _parse_event_date,
                                       _window_state)
from grt.normalize import fingerprint_for
from tests.test_wcheck_b4_r19 import (_add_event, _bot, _candidate,
                                      _cb, _cmd, _edition,
                                      _queue_row)


# ---------------------------------------------------------------------------
# Numeric TEXT poison (AI engineer 中-1 / 中-2, 軽微-1 / 軽微-2)
# ---------------------------------------------------------------------------

def test_yen_value_tolerates_text_amounts():
    assert scoring.yen_value("zzz", None, "JPY") is None
    assert scoring.yen_value(None, "abc", "JPY") is None
    assert scoring.yen_value(3000, None, None) == 3000.0
    assert scoring.yen_value("3000", None, None) == 3000.0


class _Send:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text))


_ob_counter = [0]


def _outbox_row(db, *, state="pending", attempts=0, not_before=None,
                body="本文"):
    _ob_counter[0] += 1
    cur = db.execute(
        "INSERT INTO notification_outbox(event_type, body, state,"
        " attempts, not_before, idempotency_key)"
        " VALUES('test', ?, ?, ?, ?, ?)",
        (body, state, attempts, not_before,
         f"b5-{_ob_counter[0]}"))
    db.commit()
    return int(cur.lastrowid)


def test_recover_sending_survives_text_attempts(db):
    poison = _outbox_row(db, state="sending", attempts="abc")
    good = _outbox_row(db, state="sending", attempts=0)
    n = outbox.recover_sending(db, stale_minutes=None)
    assert n == 2  # the poison row no longer aborts the loop
    rows = db.execute(
        "SELECT id, state, attempts FROM notification_outbox"
        " ORDER BY id").fetchall()
    assert rows[0][1] == "pending" and rows[0][2] == 1
    assert rows[1][1] == "pending" and rows[1][2] == 1


def test_process_outbox_failure_survives_text_attempts(db):
    from conftest import set_clock_to
    set_clock_to(12)
    poison = _outbox_row(db, state="pending", attempts="abc")
    good = _outbox_row(db, state="pending", attempts=0)
    t = _Send(fail=True)
    stats = outbox.process_outbox(db, t, chat_id="555")
    row = db.execute(
        "SELECT state, attempts FROM notification_outbox WHERE id=?",
        (poison,)).fetchone()
    assert row[0] == "failed_retryable" and row[1] == 1
    row2 = db.execute(
        "SELECT state FROM notification_outbox WHERE id=?",
        (good,)).fetchone()
    assert row2[0] == "failed_retryable"  # pass was not aborted


def test_process_outbox_repairs_garbage_not_before(db):
    from conftest import set_clock_to
    set_clock_to(12)
    rid = _outbox_row(db, state="pending", not_before="garbage")
    t = _Send()
    outbox.process_outbox(db, t, chat_id="555")
    row = db.execute(
        "SELECT state FROM notification_outbox WHERE id=?",
        (rid,)).fetchone()
    assert row[0] == "sent"
    ev = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='outbox_time_repaired'").fetchone()[0]
    assert ev == 1


def test_price_command_survives_text_bucket_minimum(db, monkeypatch):
    _edition(db)
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, cond_rank) VALUES('AR-1',"
        " 'shopA', 'https://shop.example/i', NULL, 'JPY', 'abc',"
        " 'active', 3)")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/price AR-1") == "price"
    text = "\n".join(t for t, _m in sent)
    assert "円価格なし" in text


def test_price_command_renders_text_num_for_sale_as_unknown(
        db, monkeypatch):
    _edition(db)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency) VALUES('AR-1',"
        " 'discogs', ?, 'many', 2500, 'JPY')",
        (clock.now_iso(db),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/price AR-1") == "price"
    text = "\n".join(t for t, _m in sent)
    assert "出品 不明件" in text


# ---------------------------------------------------------------------------
# match_review: reject settles poison, priority, per-pair guard
# (maniac B F-1 / F-2 / F-6a / F-6b)
# ---------------------------------------------------------------------------

def _poison_match_row(db):
    return _queue_row(db, "match_review", {
        "listing": {"source": "shopA",
                    "url": "https://shop.example/i1"},
        "result": {"edition_id": "AR-GHOST", "score": 80,
                   "decision_id": None}}, priority=50)


def test_match_review_reject_settles_unresolvable(db, monkeypatch):
    _edition(db)
    qid = _poison_match_row(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qno:{qid}") == "match_review_rejected"
    row = db.execute(
        "SELECT resolution FROM review_queue WHERE id=?",
        (qid,)).fetchone()
    assert row[0] == "rejected"


def test_match_review_approve_still_refuses_unresolvable(
        db, monkeypatch):
    _edition(db)
    qid = _poison_match_row(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "match_review_unresolvable"
    row = db.execute(
        "SELECT resolved_at FROM review_queue WHERE id=?",
        (qid,)).fetchone()
    assert row[0] is None


def _two_editions_one_listing(db):
    _edition(db, "AR-1", "テスト盤")
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm)"
        " VALUES('AR-2', 'テスト盤', 'KICA-1001')")
    cur = db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, raw_title) VALUES(NULL,"
        " 'shopA', 'https://shop.example/i1', 3000, 'JPY', 3000,"
        " 'active', 'テスト盤')")
    db.commit()
    return int(cur.lastrowid)


_LISTING = {"source": "shopA", "url": "https://shop.example/i1",
            "raw_title": "テスト盤", "title": "テスト盤",
            "catalog_raw": "KICA-1001", "price": 3000,
            "currency": "JPY"}


def _human_decision(db, lid, edition_id):
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id, score,"
        " existence_confidence, price_confidence, matcher_version)"
        " VALUES(?, ?, 90, '高', '高', 'human-review-1')",
        (lid, edition_id))
    db.commit()


def test_human_guard_does_not_freeze_cohabiting_edition(db):
    lid = _two_editions_one_listing(db)
    _human_decision(db, lid, "AR-1")
    matcher.match_listing(db, dict(_LISTING), listing_id=lid)
    # AR-2 was evaluated in the same run and must get its decision
    # (pre-fix: the listing-global guard returned early for all).
    n = db.execute(
        "SELECT COUNT(*) FROM match_decisions WHERE listing_id=?"
        " AND edition_id='AR-2'", (lid,)).fetchone()[0]
    assert n >= 1
    # AR-1's pair stays human-held: no new decision for it.
    n1 = db.execute(
        "SELECT COUNT(*) FROM match_decisions WHERE listing_id=?"
        " AND edition_id='AR-1'", (lid,)).fetchone()[0]
    assert n1 == 1


def test_human_guard_survives_interleaved_machine_decision(db):
    lid = _two_editions_one_listing(db)
    _human_decision(db, lid, "AR-1")
    # An intervening run evaluates only AR-2; its machine decision
    # becomes the listing's latest overall.
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id, score,"
        " existence_confidence, price_confidence, matcher_version)"
        " VALUES(?, 'AR-2', 70, '高', '中', 'matcher-x')", (lid,))
    db.commit()
    matcher.match_listing(db, dict(_LISTING), listing_id=lid)
    # The human pair must NOT be re-decided by the machine.
    rows = db.execute(
        "SELECT matcher_version FROM match_decisions WHERE"
        " listing_id=? AND edition_id='AR-1' ORDER BY id DESC",
        (lid,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == "human-review-1"
    assert pricedrop.price_evidence_ok(db, lid, "AR-1") is True


def test_match_review_rows_enter_at_priority_45(db):
    _edition(db, "AR-1", "テスト盤")
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm)"
        " VALUES('AR-2', 'テスト盤', 'KICA-1001')")
    db.commit()
    matcher.match_listing(db, dict(_LISTING))
    rows = db.execute(
        "SELECT priority FROM review_queue WHERE"
        " kind='match_review' AND resolved_at IS NULL").fetchall()
    assert rows and all(r[0] == 45 for r in rows)


# ---------------------------------------------------------------------------
# Queue presentation exits (maniac B F-3 / F-4 / F-5, collector 軽微-4)
# ---------------------------------------------------------------------------

def test_unknown_kind_rows_settle_in_queue_scan(db, monkeypatch):
    good = _candidate(db, title="正常な候補")
    _queue_row(db, "mystery_kind", {"x": 1}, priority=99)
    _queue_row(db, "candidate_review", {"candidate_id": good},
               priority=10)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    assert any("正常な候補" in t for t, _ in sent)
    row = db.execute(
        "SELECT resolution FROM review_queue WHERE"
        " kind='mystery_kind'").fetchone()
    assert row[0] == "stale"


def test_promoted_seed_promotion_row_settles_stale(db, monkeypatch):
    cid = _candidate(db, title="昇格済みseed", status="pending")
    _queue_row(db, "seed_promotion", {"seed_id": cid,
                                      "found": {}}, priority=90)
    good = _candidate(db, title="正常な候補")
    _queue_row(db, "candidate_review", {"candidate_id": good},
               priority=10)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    assert any("正常な候補" in t for t, _ in sent)
    row = db.execute(
        "SELECT resolution FROM review_queue WHERE"
        " kind='seed_promotion'").fetchone()
    assert row[0] == "stale"


def test_sweep_hit_without_catalog_settles_stale(db, monkeypatch):
    _queue_row(db, "sweep_hit", {"prefix": "KICA"}, priority=90)
    good = _candidate(db, title="正常な候補")
    _queue_row(db, "candidate_review", {"candidate_id": good},
               priority=10)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    assert any("正常な候補" in t for t, _ in sent)
    row = db.execute(
        "SELECT resolution FROM review_queue WHERE"
        " kind='sweep_hit'").fetchone()
    assert row[0] == "stale"


def test_candidate_presentation_shows_bridge_edition(db, monkeypatch):
    _edition(db, "AR-9", "既存盤")
    cid = _candidate(db, title="橋渡し候補")
    db.execute(
        "INSERT INTO repress_signals(edition_id, candidate_id,"
        " system, at) VALUES('AR-9', ?, 'announcement', ?)",
        (cid, clock.now_iso(db)))
    db.commit()
    _queue_row(db, "candidate_review", {"candidate_id": cid},
               priority=10)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    text = "\n".join(t for t, _ in sent)
    assert "既存目録との関連: AR-9" in text


# ---------------------------------------------------------------------------
# Ghost freshness: bucket baseline, /coverage, copy groups
# (seller 中-1 / 中-2 / 軽微-3)
# ---------------------------------------------------------------------------

def _listing_with_decision(db, *, last_seen, total=4000):
    _edition(db)
    cur = db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, cond_rank, last_seen)"
        " VALUES('AR-1', 'shopA', 'https://shop.example/i', ?,"
        " 'JPY', ?, 'active', 3, ?)", (total, total, last_seen))
    lid = int(cur.lastrowid)
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id, score,"
        " existence_confidence, price_confidence) VALUES(?, 'AR-1',"
        " 90, '高', '高')", (lid,))
    db.commit()
    return lid


def test_bucket_best_ignores_stale_ghost_listing(db):
    lid = _listing_with_decision(
        db, last_seen="2025-01-01T00:00:00+00:00")
    assert pricedrop.bucket_best(db, "AR-1", None, 3) is None


def test_bucket_best_uses_fresh_listing(db):
    lid = _listing_with_decision(db, last_seen=clock.now_iso(db))
    assert pricedrop.bucket_best(db, "AR-1", None, 3) == 4000.0


def test_coverage_excludes_stale_listing(db, monkeypatch):
    _listing_with_decision(db, last_seen="2025-01-01T00:00:00+00:00")
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/coverage") == "coverage"
    text = "\n".join(t for t, _m in sent)
    assert "価格証拠あり: 0件" in text


def test_coverage_excludes_zero_stock_snapshot(db, monkeypatch):
    _edition(db)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency) VALUES('AR-1',"
        " 'discogs', ?, 0, 2500, 'JPY')", (clock.now_iso(db),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/coverage") == "coverage"
    text = "\n".join(t for t, _m in sent)
    assert "価格証拠あり: 0件" in text


def test_coverage_excludes_expired_discogs_snapshot(db, monkeypatch):
    _edition(db)
    _edition(db, "AR-2")
    expired = clock.to_iso(clock.now(db) - timedelta(hours=6, seconds=1))
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency) VALUES('AR-1',"
        " 'discogs', ?, 1, 2500, 'JPY')", (expired,))
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency) VALUES('AR-2',"
        " 'discogs', ?, 1, 2500, 'JPY')", (clock.now_iso(db),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/coverage") == "coverage"
    text = "\n".join(t for t, _m in sent)
    assert "価格証拠あり: 1件" in text


def test_declared_oos_member_not_revived_by_group_round(db):
    db.execute(
        "INSERT INTO listings(id, source, url, status,"
        " oos_declared, physical_copy_id) VALUES(1, 'Shop',"
        " 'https://a/1', 'sold_candidate', 1, 7), (2, 'Shop',"
        " 'https://a/2', 'active', 0, 7)")
    db.commit()
    evidence.mark_copy_group_round(db, [1, 2], {1, 2}, True)
    row = db.execute(
        "SELECT status, oos_declared FROM listings WHERE id=1"
    ).fetchone()
    assert row[0] == "sold_candidate" and row[1] == 1


def _group_fixture(db, *, member_b_fresh):
    db.execute(
        "INSERT INTO sources(id, name, kind, status) VALUES(1,"
        " 'ShopA', 'STORE', 'approved'), (2, 'ShopB', 'STORE',"
        " 'approved')")
    db.execute(
        "INSERT INTO listings(id, source, url, status,"
        " physical_copy_id) VALUES(1, 'ShopA', 'https://a/1',"
        " 'active', 7), (2, 'ShopB', 'https://b/1', 'active', 7)")
    if member_b_fresh:
        db.execute(
            "INSERT INTO fetch_runs(source_id, started_at, result)"
            " VALUES(2, ?, 'OK')", (clock.now_iso(db),))
    db.commit()


def test_group_judged_when_member_source_never_ran(db):
    _group_fixture(db, member_b_fresh=False)
    evidence.apply_absence_round(db, {"ShopA": set()})
    evidence.apply_absence_round(db, {"ShopA": set()})
    st = {r[0]: r[1] for r in db.execute(
        "SELECT id, status FROM listings")}
    assert st[1] != "active" and st[2] != "active"


def test_group_still_blocked_by_fresh_unobserved_source(db):
    _group_fixture(db, member_b_fresh=True)
    evidence.apply_absence_round(db, {"ShopA": set()})
    evidence.apply_absence_round(db, {"ShopA": set()})
    st = {r[0]: r[1] for r in db.execute(
        "SELECT id, status FROM listings")}
    assert st[1] == "active" and st[2] == "active"


# ---------------------------------------------------------------------------
# Deadlines: cross-channel propagation, cancellation, digest shape
# (wholesaler 中-3 / 中-4 / 軽微-4, collector 中-3 / 軽微-1)
# ---------------------------------------------------------------------------

def _sched(db, *, channel, jst, kind="hard_cutoff",
           edition_id="AR-1"):
    return deadlines_ops.set_schedule(
        db, edition_id=edition_id, channel=channel,
        deadline_raw=jst, deadline_tz="Asia/Tokyo",
        deadline_jst=f"{jst}T00:00:00+09:00",
        deadline_confidence="confirmed", deadline_kind=kind)


def test_cross_channel_postponement_supersedes_same_instant(db):
    _edition(db)
    _sched(db, channel="manual", jst="2027-02-07")
    _sched(db, channel="manual", jst="2027-02-07",
           kind="guarantee_cutoff")
    _sched(db, channel="doujin_event", jst="2027-02-07")
    _sched(db, channel="doujin_event", jst="2027-02-14")
    live = db.execute(
        "SELECT channel, deadline_kind FROM release_schedule"
        " WHERE superseded_by IS NULL ORDER BY channel,"
        " deadline_kind").fetchall()
    pairs = {(r[0], r[1]) for r in live}
    # The manual hard row at the OLD instant followed the move;
    # the guarantee-kind row (different kind) is untouched, and
    # the doujin row itself moved to the new instant.
    assert pairs == {("manual", "guarantee_cutoff"),
                     ("doujin_event", "hard_cutoff")}


def test_cancel_schedule_stops_the_alert(db):
    _edition(db)
    in3 = (clock.now(db) + timedelta(days=3)).date().isoformat()
    sid = _sched(db, channel="manual", jst=in3)
    assert deadlines_ops.cancel_schedule(db, sid) is True
    assert deadlines_ops.cancel_schedule(db, sid) is False
    assert deadlines_ops.staged_alerts(db) == 0
    row = db.execute(
        "SELECT superseded_by FROM release_schedule WHERE id=?",
        (sid,)).fetchone()
    assert row[0] == sid  # self-superseded


def test_deadline_cancel_command(db, monkeypatch):
    _edition(db)
    sid = _sched(db, channel="manual", jst="2027-02-07")
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, f"/deadline_cancel #{sid}") \
        == "deadline_cancelled"
    assert _cmd(bot, "/deadline_cancel #99999") \
        == "deadline_cancel_missing"


def test_deadline_digest_dedupes_sibling_channels(db):
    _edition(db, "AR-1", "兄弟盤タイトル")
    in3 = (clock.now(db) + timedelta(days=3)).date().isoformat()
    _sched(db, channel="manual", jst=in3, kind="guarantee_cutoff")
    _sched(db, channel="doujin_event", jst=in3,
           kind="guarantee_cutoff")
    lines = deadlines_ops.deadline_digest_lines(db)
    hits = [ln for ln in lines if "兄弟盤タイトル" in ln]
    assert len(hits) == 1


def test_staged_alert_body_names_the_work(db):
    _edition(db, "AR-1", "段階通知タイトル")
    in2 = (clock.now(db) + timedelta(days=2)).date().isoformat()
    _sched(db, channel="manual", jst=in2)
    created = deadlines_ops.staged_alerts(db)
    assert created
    body = db.execute(
        "SELECT body FROM notification_outbox ORDER BY id DESC"
        " LIMIT 1").fetchone()[0]
    assert "段階通知タイトル" in body


def test_deadlines_command_notes_hidden_unconfirmed(db, monkeypatch):
    _edition(db)
    for ch in ("a", "b", "c", "d", "e"):
        deadlines_ops.set_schedule(
            db, edition_id="AR-1", channel=ch, deadline_raw="x",
            deadline_kind="hard_cutoff")
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/deadlines") == "deadlines"
    text = "\n".join(t for t, _m in sent)
    assert "ほか 2 件" in text


def test_repress_digest_note_names_the_work(db):
    _edition(db, "AR-1", "リプレス盤タイトル")
    db.execute(
        "INSERT INTO repress_signals(edition_id, system, at,"
        " detail) VALUES('AR-1', 'announcement', ?, 'x')",
        (clock.now_iso(db),))
    db.commit()
    notes = repress.digest_notes(db)
    assert any("リプレス盤タイトル" in n for n in notes)


# ---------------------------------------------------------------------------
# Doujin: dates, seed identity, promotion edges (maniac A)
# ---------------------------------------------------------------------------

def test_parse_event_date_variants():
    assert _parse_event_date("2027-02-07").isoformat() == "2027-02-07"
    assert _parse_event_date("2027/02/07").isoformat() == "2027-02-07"
    assert _parse_event_date("2027年2月7日").isoformat() == "2027-02-07"
    assert _parse_event_date("2027-2-7").isoformat() == "2027-02-07"
    assert _parse_event_date("未定") is None


def test_window_state_accepts_slash_dates():
    row = {"harvest_from": "2026/10/01", "harvest_to": "2026/12/31"}
    assert _window_state(row, "2026-11-08") == "in"
    row2 = {"harvest_from": "2027/01/01", "harvest_to": "2027/02/01"}
    assert _window_state(row2, "2026-11-08") == "before"


def test_doujin_slash_start_date_produces_deadline(db):
    ev = _add_event(db, starts_on="2027/02/07")
    f = DoujinEventFetcher(db, 1)
    f.register_event_items(
        [{"title": "同人サントラ", "url": "https://c.example/w1",
          "circle": "波動", "raw_title": "同人サントラ"}], ev)
    rows = db.execute("SELECT deadline_jst FROM release_schedule"
                      ).fetchall()
    assert len(rows) == 1
    assert rows[0][0].startswith("2027-02-07")


def test_doujin_unparseable_start_records_event(db):
    ev = _add_event(db, starts_on="未定")
    f = DoujinEventFetcher(db, 1)
    item = {"title": "同人サントラ", "url": "https://c.example/w1",
            "circle": "波動", "raw_title": "同人サントラ"}
    f.register_event_items([item], ev)
    assert db.execute(
        "SELECT COUNT(*) FROM release_schedule").fetchone()[0] == 0
    n = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='event_start_unparseable'").fetchone()[0]
    assert n == 1


def test_seed_sighting_also_registers_deadline(db):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    ev = _add_event(db, "C101", starts_on="2027-05-01")
    f = DoujinEventFetcher(db, 1)
    f.register_event_items(
        [{"title": "深海サントラ", "url": "https://c.example/w9",
          "circle": "波動", "raw_title": "深海サントラ"}], ev)
    rows = db.execute(
        "SELECT deadline_jst FROM release_schedule WHERE"
        " candidate_id=?", (seed_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0].startswith("2027-05-01")


def test_duplicate_seeds_one_sighting_one_request(db):
    seedops.import_seed(db, title="深海サントラ", circle="波動",
                        event_name="C100")
    seedops.import_seed(db, title="深海サントラ", circle="波動",
                        event_name="C99")
    ev = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    f.register_event_items(
        [{"title": "深海サントラ", "url": "https://c.example/w9",
          "circle": "波動", "raw_title": "深海サントラ"}], ev)
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='seed_promotion'").fetchone()[0] == 1


def test_reharvest_after_promotion_creates_no_second_candidate(
        db, monkeypatch):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    ev = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    item = {"title": "深海サントラ", "url": "https://c.example/w9",
            "circle": "波動", "raw_title": "深海サントラ"}
    f.register_event_items([item], ev)
    q = db.execute(
        "SELECT id FROM review_queue WHERE kind='seed_promotion'"
    ).fetchone()
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{q[0]}") == "seed_promoted"
    f.register_event_items([item], ev)  # sighted again post-promotion
    n = db.execute(
        "SELECT COUNT(*) FROM candidate_editions WHERE"
        " title='深海サントラ'").fetchone()[0]
    assert n == 1


def test_rejected_promotion_returns_to_retrospective(db, monkeypatch):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    qid = seedops.request_seed_promotion(
        db, seed_id, {"title": "深海サントラ", "circle": "波動"})
    bot, sent = _bot(db, monkeypatch)
    _cb(bot, f"qno:{qid}")
    ids = [r["id"] for r in seedops.retrospective_report(db)]
    assert seed_id in ids


def test_promotion_folds_all_matching_candidates(db, monkeypatch):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    # candidate_editions.fingerprint is UNIQUE, so the two
    # harvests of the same work carry distinct catalog segments;
    # the fold key is title+circle (segments 0 and 2).
    _candidate(db, title="深海サントラ",
               fp=fingerprint_for("深海サントラ", "AAA-1", "波動"),
               found_via="doujin_event:1")
    _candidate(db, title="深海サントラ",
               fp=fingerprint_for("深海サントラ", "BBB-2", "波動"),
               found_via="doujin_event:2")
    ev = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    f.register_event_items(
        [{"title": "深海サントラ", "url": "https://c.example/w9",
          "circle": "波動", "raw_title": "深海サントラ"}], ev)
    q = db.execute(
        "SELECT id FROM review_queue WHERE kind='seed_promotion'"
    ).fetchone()
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{q[0]}") == "seed_promoted"
    rows = db.execute(
        "SELECT status FROM candidate_editions WHERE id != ?"
        " ORDER BY id", (seed_id,)).fetchall()
    assert all(r[0] == "rejected" for r in rows)


def test_import_seed_zero_padded_catalog_is_duplicate(db):
    a = seedops.import_seed(db, title="波サントラ", circle="波動",
                            event_name="C100", catalog_norm="WAVE-001")
    b = seedops.import_seed(db, title="波サントラ", circle="波動",
                            event_name="C100", catalog_norm="WAVE-1")
    assert a is not None and b is None


# ---------------------------------------------------------------------------
# Operator keys, fingerprints, transport adversaries
# (wholesaler 中-1 / 中-5 / 軽微-1 / 軽微-2, adversarial 中-1 / 中-2 /
#  軽微-1 / 軽微-2 / 軽微-3)
# ---------------------------------------------------------------------------

def test_host_of_tenant_and_idn():
    assert candidates._host_of("https://circle-a.booth.pm/i") \
        == "circle-a.booth.pm"
    assert candidates._host_of("https://circle-a.booth.pm/i") \
        != candidates._host_of("https://circle-b.booth.pm/i")
    assert candidates._host_of("https://a.com.au/x") == "a.com.au"
    assert candidates._host_of("https://a.com.au/x") \
        != candidates._host_of("https://b.com.au/x")
    assert candidates._host_of("https://例え.jp/x") \
        == candidates._host_of("https://xn--r8jz45g.jp/x")


def test_fingerprint_ignores_head_decorations():
    assert fingerprint_for("【予約】星空サントラ", None) \
        == fingerprint_for("星空サントラ", None)
    assert fingerprint_for("【限定盤】星空サントラ", None) \
        != fingerprint_for("星空サントラ", None)


class _SurrogateIntolerantTransport:
    """Faithful stand-in for httpx: lone surrogates cannot be
    JSON-encoded and raise UnicodeEncodeError."""

    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, reply_markup=None):
        text.encode("utf-8")  # raises on lone surrogates
        self.sent.append(text)


def test_bot_send_sanitizes_lone_surrogates(db):
    t = _SurrogateIntolerantTransport()
    bot = Bot(db, t)
    bot._send(555, "毒\ud800タイトル")
    assert t.sent and "\ud800" not in t.sent[0]


def test_callback_id_grammar_is_ascii_only():
    assert _parse_candidate_id("12") == 12
    assert _parse_candidate_id("１２") is None
    assert _parse_candidate_id("-3") == -3


def test_deadline_year_0001_gets_explicit_reply(db, monkeypatch):
    _edition(db)
    bot, sent = _bot(db, monkeypatch)
    action = _cmd(bot, "/deadline AR-1 0001-01-01 00:00"
                       " Asia/Tokyo hard")
    # Pre-fix this returned 'invalid_update' with NO reply at all;
    # the explicit out-of-range outcome is the fix.
    assert action == "deadline_out_of_range"
    assert sent  # a reply was actually delivered


def test_http_get_body_cap(db, monkeypatch):
    from grt.fetchers import base as fetch_base

    class _Resp:
        status_code = 200
        headers = {}

        def iter_bytes(self):
            yield b"x" * 64
            yield b"y" * 64

    class _Ctx:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fetch_base, "MAX_BODY_BYTES", 100)
    monkeypatch.setattr(fetch_base.httpx, "stream",
                        lambda *a, **kw: _Ctx())
    with pytest.raises(fetch_base.FetchError) as ei:
        fetch_base.http_get("https://shop.example/big")
    assert ei.value.result == "RESPONSE_TOO_LARGE"


def test_discovery_digest_caps_candidate_titles(db):
    _candidate(db, title="あ" * 5000)
    body = digest.build_discovery_digest(db)
    assert len(body) < 2000


def test_health_digest_surfaces_duplicate_suspects(db):
    db.execute(
        "INSERT INTO duplicate_suspects(kind, key, payload_json,"
        " created_at) VALUES('title', 'k', '{}', ?)",
        (clock.now_iso(db),))
    db.commit()
    text = digest.build_health_digest(db)
    assert "目録の衝突疑い（未確認）: 1件" in text


def test_evidence_body_store_is_capped(db):
    import hashlib
    body = "x" * (2 * 1024 * 1024)
    rec = evidence.save_raw_evidence(
        "test", "https://shop.example/big", body, keep_body=False)
    # The hash still covers the FULL body...
    assert rec["raw_hash"] == hashlib.sha256(
        body.encode()).hexdigest()
    # ...but the retained copy is capped (pre-fix: full 2MB held).
    _src, stored = evidence._PENDING_BODIES[rec["raw_hash"]]
    assert len(stored) <= evidence._BODY_STORE_LIMIT

def test_http_get_does_not_decode_transport_gzip_twice(monkeypatch):
    import gzip
    import httpx
    from grt.fetchers import base as fetch_base

    body = b"<rss>sample</rss>"
    compressed = gzip.compress(body)

    def response(_request):
        return httpx.Response(200, headers={
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
            "Content-Type": "application/rss+xml",
        }, stream=httpx.ByteStream(compressed))

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(fetch_base.httpx, "stream", client.stream)
        result = fetch_base.http_get("https://feed.example/rss")

    assert result.content == body
    assert "content-encoding" not in result.headers
    assert result.headers["content-length"] == str(len(body))
    assert result.headers["content-type"] == "application/rss+xml"
