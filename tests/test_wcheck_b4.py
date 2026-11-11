"""W-check Block 4 regression tests (R18): discovery / candidates /
deadlines / REPRESS.

Every test pins a defect the R16 seven-persona review reproduced:
queue kinds with no exit, the doujin lane's dead candidates,
deadline producer absence + postponement handling, REPRESS key
fragmentation and window/intake holes, scoring inputs that never
reached scoring, and per-item containment in the candidate lanes.
"""
import json
from datetime import timedelta

import pytest

from grt import (bot as bot_mod, bundling, candidates, clock,
                 deadlines_ops, digest, normalize, outbox, repress,
                 scoring, seedops)
from grt.bot import Bot
from grt.fetchers.blipblop import BlipBlopFetcher
from grt.fetchers.doujin_event import (DoujinEventFetcher,
                                       extract_event_items)
from grt.fetchers.webpages import FeedFetcher
from grt.normalize import fingerprint_for, page_identity_url


class _Transport:
    def __call__(self, *a, **kw):
        return {"ok": True}


_uid = [1000]


def _bot(db, monkeypatch):
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('allowed_chat_id', '555'),"
        " ('deadline_override_quiet_selected', '1')")
    db.commit()
    bot = Bot(db, _Transport())
    sent = []
    monkeypatch.setattr(
        bot, "_send",
        lambda chat, text, reply_markup=None:
        sent.append((text, reply_markup)))
    return bot, sent


def _cb(bot, data):
    _uid[0] += 1
    return bot.handle_update({
        "update_id": _uid[0],
        "callback_query": {"id": f"q{_uid[0]}",
                           "message": {"chat": {"id": 555}},
                           "data": data}})


def _cmd(bot, text):
    _uid[0] += 1
    return bot.handle_update({
        "update_id": _uid[0],
        "message": {"chat": {"id": 555}, "text": text}})


def _edition(db, eid="AR-1", title="テスト盤"):
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm)"
        " VALUES(?, ?, 'KICA-1001')", (eid, title))
    db.commit()
    return eid


def _candidate(db, title="候補タイトル", status="pending", fp=None,
               found_via="feed:test"):
    fp = fp or fingerprint_for(title, None)
    cur = db.execute(
        "INSERT INTO candidate_editions(title, raw_title, source_url,"
        " fingerprint, existence_confidence, price_confidence,"
        " found_at, first_seen, last_seen, found_via, status)"
        " VALUES(?, ?, 'https://shop.example/item', ?, '中', '低',"
        " '2026-11-08', '2026-11-08', '2026-11-08', ?, ?)",
        (title, title, fp, found_via, status))
    db.commit()
    return int(cur.lastrowid)


def _queue_row(db, kind, payload, priority=10, fingerprint=None):
    cur = db.execute(
        "INSERT INTO review_queue(kind, payload_json, fingerprint,"
        " priority, created_at) VALUES(?, ?, ?, ?, ?)",
        (kind, json.dumps(payload), fingerprint, priority,
         clock.now_iso(db)))
    db.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Doujin lane joins the review flow
# ---------------------------------------------------------------------------

def _add_event(db, name="テスト即売会", starts_on=None):
    cur = db.execute(
        "INSERT INTO event_catalog(name, kind, url, starts_on)"
        " VALUES(?, 'doujin', 'https://event.example/', ?)",
        (name, starts_on))
    db.commit()
    return int(cur.lastrowid)


def test_doujin_candidates_are_reviewable(db):
    ev = _add_event(db)
    f = DoujinEventFetcher(db, 1)
    reg = f.register_event_items(
        [{"title": "同人サントラ", "url": "https://c.example/w1",
          "circle": "波動", "raw_title": "同人サントラ"}], ev)
    assert reg["created"] == 1
    row = db.execute("SELECT * FROM candidate_editions").fetchone()
    assert row["status"] == "pending"
    assert row["found_via"] == "doujin_event:テスト即売会"
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='candidate_review'").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='candidate_review'").fetchone()[0] == 1


def test_doujin_event_date_becomes_a_deadline(db):
    ev = _add_event(db, starts_on="2027-02-07")
    f = DoujinEventFetcher(db, 1)
    f.register_event_items(
        [{"title": "同人サントラ", "url": "https://c.example/w1",
          "circle": "波動", "raw_title": "同人サントラ"}], ev)
    rows = db.execute("SELECT * FROM release_schedule").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["deadline_kind"] == "hard_cutoff"
    assert r["deadline_confidence"] == "confirmed"
    assert r["deadline_jst"].startswith("2027-02-07T00:00:00")
    assert r["candidate_id"] is not None


def test_doujin_redistribution_at_another_event_is_recorded(db):
    ev1 = _add_event(db, "C100")
    ev2 = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    item = {"title": "再頒布サントラ", "url": "https://c.example/w1",
            "circle": "波動", "raw_title": "再頒布サントラ"}
    f.register_event_items([item], ev1)
    f.register_event_items([item], ev2)
    assert db.execute(
        "SELECT COUNT(*) FROM candidate_editions").fetchone()[0] == 1
    sights = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='doujin_event_sighting'").fetchone()[0]
    assert sights == 1


def test_extract_event_items_takes_all_works_in_a_container():
    html = """
    <ul><li>
      <a href="/circle/9">波動サークル</a>
      <a href="/work/1">新刊サントラ Vol.1</a>
      <a href="/work/2">既刊サントラ Vol.2</a>
      <a href="/work/3">既刊サントラ Vol.3</a>
    </li></ul>"""
    items = extract_event_items(html, "https://event.example/")
    titles = {i["title"] for i in items}
    assert titles == {"新刊サントラ Vol.1", "既刊サントラ Vol.2",
                      "既刊サントラ Vol.3"}
    assert all(i["circle"] == "波動サークル" for i in items)


# ---------------------------------------------------------------------------
# Seed promotion wired end to end
# ---------------------------------------------------------------------------

def test_seed_promotion_end_to_end(db, monkeypatch):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    assert seed_id is not None
    ev = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    f.register_event_items(
        [{"title": "深海サントラ", "url": "https://c.example/w9",
          "circle": "波動", "raw_title": "深海サントラ"}], ev)
    q = db.execute(
        "SELECT * FROM review_queue WHERE kind='seed_promotion'"
    ).fetchone()
    assert q is not None  # the harvest requested the promotion
    # A second sighting does not stack a duplicate request.
    f.register_event_items(
        [{"title": "深海サントラ", "url": "https://c.example/w9",
          "circle": "波動", "raw_title": "深海サントラ"}], ev)
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='seed_promotion'").fetchone()[0] == 1
    # The sighted seed leaves the retrospective dig-up list.
    assert seedops.retrospective_report(db) == []
    # Bot approval runs the promotion; the seed enters the normal
    # candidate review flow (status 'pending', NOT the old limbo
    # status 'candidate_review').
    bot, sent = _bot(db, monkeypatch)
    action = _cb(bot, f"qok:{q['id']}")
    assert action == "seed_promoted"
    row = db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (seed_id,)).fetchone()
    assert row[0] == "pending"
    rows = db.execute(
        "SELECT payload_json FROM review_queue WHERE"
        " kind='candidate_review' AND resolved_at IS NULL").fetchall()
    assert any(json.loads(r[0]).get("candidate_id") == seed_id
               for r in rows)


def test_seedops_tolerates_non_dict_hints(db):
    seed_id = seedops.import_seed(db, title="壊れたseed", circle="円")
    db.execute(
        "UPDATE candidate_editions SET variant_hints_json='[1,2]'"
        " WHERE id=?", (seed_id,))
    db.commit()
    # No exception; simply no match and an empty-hints report row.
    assert seedops.composite_key_matches(
        db.execute("SELECT * FROM candidate_editions WHERE id=?",
                   (seed_id,)).fetchone(),
        {"title": "壊れたseed", "circle": "円"}) is False
    assert seedops.retrospective_report(db)[0]["hints"] == {}


# ---------------------------------------------------------------------------
# Queue exits for every kind
# ---------------------------------------------------------------------------

def _match_review_setup(db):
    _edition(db)
    cur = db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, raw_title) VALUES('AR-1',"
        " 'shopA', 'https://shop.example/i1', 3000, 'JPY', 3000,"
        " 'active', 'テスト盤')")
    lid = int(cur.lastrowid)
    cur = db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id, score,"
        " existence_confidence, price_confidence) VALUES(?, 'AR-1',"
        " 80, '高', '中')", (lid,))
    dec_id = int(cur.lastrowid)
    db.commit()
    payload = {"listing": {"source": "shopA",
                           "url": "https://shop.example/i1",
                           "raw_title": "テスト盤"},
               "result": {"edition_id": "AR-1", "score": 80,
                          "decision_id": dec_id,
                          "existence_confidence": "高",
                          "price_confidence": "中",
                          "breakdown": {}, "variant_gate": False}}
    qid = _queue_row(db, "match_review", payload, priority=50,
                     fingerprint="https://shop.example/i1|AR-1")
    return lid, qid


def test_match_review_approval_mints_price_evidence(db, monkeypatch):
    from grt.pricedrop import price_evidence_ok
    lid, qid = _match_review_setup(db)
    assert price_evidence_ok(db, lid, "AR-1") is False
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "match_review_approved"
    assert price_evidence_ok(db, lid, "AR-1") is True
    row = db.execute(
        "SELECT resolution FROM review_queue WHERE id=?",
        (qid,)).fetchone()
    assert row[0] == "approved"


def test_match_review_rejection_keeps_price_unusable(db, monkeypatch):
    from grt.pricedrop import price_evidence_ok
    lid, qid = _match_review_setup(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qno:{qid}") == "match_review_rejected"
    assert price_evidence_ok(db, lid, "AR-1") is False
    latest = db.execute(
        "SELECT price_confidence FROM match_decisions WHERE"
        " listing_id=? ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    assert latest[0] == "低"


def test_sweep_hit_approval_creates_candidate(db, monkeypatch):
    qid = _queue_row(db, "sweep_hit",
                     {"prefix": "KICA", "number": 1002,
                      "catalog": "KICA-1002"}, priority=40)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "sweep_hit_approved"
    cand = db.execute(
        "SELECT * FROM candidate_editions WHERE"
        " catalog_norm='KICA1002'").fetchone()
    assert cand is not None and cand["status"] == "pending"
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='candidate_review' AND resolved_at IS NULL"
        ).fetchone()[0] == 1


def _bundle_setup(db):
    _edition(db)
    ids = []
    for i, src in enumerate(("shopA", "shopB")):
        cur = db.execute(
            "INSERT INTO listings(edition_id, source, url, price,"
            " currency, total_jpy, cond_rank, status) VALUES('AR-1',"
            " ?, ?, 3000, 'JPY', 3000, NULL, 'active')",
            (src, f"https://{src}.example/i"))
        ids.append(int(cur.lastrowid))
    db.execute(
        "INSERT INTO seller_groups(seller_group, source, storefront)"
        " VALUES('g1', 'shopA', 's'), ('g1', 'shopB', 's')")
    db.commit()
    return ids


def test_copy_bundle_approval_executes_and_never_requeues(db,
                                                         monkeypatch):
    ids = _bundle_setup(db)
    res = bundling.propose_bundles(db)
    assert res["queued_for_review"] == 1
    q = db.execute(
        "SELECT * FROM review_queue WHERE kind='copy_bundle_review'"
        " AND resolved_at IS NULL").fetchone()
    assert q is not None
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{q['id']}") == "copy_bundle_approved"
    grouped = db.execute(
        "SELECT COUNT(*) FROM listings WHERE physical_copy_id"
        " IS NOT NULL").fetchone()[0]
    assert grouped == 2
    # Proposing again must not resurrect the resolved item.
    bundling.propose_bundles(db)
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='copy_bundle_review' AND resolved_at IS NULL"
        ).fetchone()[0] == 0


def test_dissolution_rows_are_notices_not_pending_reviews(db):
    ids = _bundle_setup(db)
    # Form a group, then drift its prices apart.
    gid_cur = db.execute(
        "INSERT INTO listing_copy_groups(edition_id, seller_group,"
        " first_seen) VALUES('AR-1', 'g1', '2026-11-08')")
    gid = int(gid_cur.lastrowid)
    for lid in ids:
        db.execute(
            "INSERT INTO listing_copy_members(group_id, listing_id)"
            " VALUES(?, ?)", (gid, lid))
        db.execute(
            "UPDATE listings SET physical_copy_id=? WHERE id=?",
            (gid, lid))
    db.execute("UPDATE listings SET price=9000, total_jpy=9000"
               " WHERE id=?", (ids[1],))
    db.commit()
    bundling.propose_bundles(db)
    row = db.execute(
        "SELECT resolved_at, resolution FROM review_queue WHERE"
        " kind='copy_bundle_review' AND"
        " resolution='dissolved_notice'").fetchone()
    assert row is not None
    assert row["resolved_at"] is not None
    # And it does not count as unresolved backlog.
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='copy_bundle_review' AND resolved_at IS NULL AND"
        " payload_json LIKE '%group_drift%'").fetchone()[0] == 0


def test_candidate_approval_catalogues_the_discovery(db, monkeypatch):
    cid = _candidate(db)
    repress.register_signal(db, system="announcement",
                            candidate_id=cid, detail="x")
    deadlines_ops.set_schedule(
        db, candidate_id=cid, channel="doujin_event",
        deadline_raw="C101 2027-02-07", deadline_tz="Asia/Tokyo",
        deadline_jst="2027-02-07T00:00:00",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    _queue_row(db, "candidate_review", {"candidate_id": cid})
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"approve:{cid}") == "candidate_approved"
    cand = db.execute(
        "SELECT * FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()
    assert cand["promoted_edition_id"] == f"AR-C{cid:05d}"
    ed = db.execute(
        "SELECT * FROM editions WHERE id=?",
        (cand["promoted_edition_id"],)).fetchone()
    assert ed is not None and ed["watch_level"] == 0
    # Signals migrated into the edition key space.
    assert "announcement" in repress.systems_in_window(
        db, edition_id=cand["promoted_edition_id"])
    # Schedules gained the edition id.
    sched = db.execute(
        "SELECT edition_id FROM release_schedule WHERE"
        " candidate_id=?", (cid,)).fetchone()
    assert sched[0] == cand["promoted_edition_id"]
    # The reply names the new edition id.
    assert any(f"AR-C{cid:05d}" in t for t, _ in sent)


def test_candidate_resolution_is_kind_filtered(db, monkeypatch):
    # A seed_promotion row whose payload ALSO carries a candidate_id
    # must not be resolved by an unrelated candidate approval
    # (promote_seed's Gate 1 trusts resolution='approved').
    cid = _candidate(db)
    _queue_row(db, "candidate_review", {"candidate_id": cid})
    trap = _queue_row(db, "seed_promotion",
                      {"seed_id": 999, "candidate_id": cid})
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"approve:{cid}") == "candidate_approved"
    row = db.execute(
        "SELECT resolved_at FROM review_queue WHERE id=?",
        (trap,)).fetchone()
    assert row[0] is None


def test_queue_survives_poison_and_stale_head_rows(db, monkeypatch):
    cid = _candidate(db)
    _queue_row(db, "candidate_review", {"candidate_id": cid},
               priority=10)
    poison = _queue_row(db, "candidate_review",
                        {"candidate_id": 10 ** 30}, priority=99)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    # The poison row was settled stale and the real candidate was
    # presented with its buttons.
    assert db.execute(
        "SELECT resolution FROM review_queue WHERE id=?",
        (poison,)).fetchone()[0] == "stale"
    presented = [m for _t, m in sent if m]
    assert any(b["callback_data"] == f"approve:{cid}"
               for m in presented
               for row_ in m["inline_keyboard"] for b in row_)


def test_queue_presents_match_review_with_buttons(db, monkeypatch):
    lid, qid = _match_review_setup(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/queue") == "queue"
    markups = [m for _t, m in sent if m]
    datas = [b["callback_data"] for m in markups
             for row_ in m["inline_keyboard"] for b in row_]
    assert f"qok:{qid}" in datas and f"qno:{qid}" in datas
    assert any("出品レビュー" in t for t, _m in sent)




# ---------------------------------------------------------------------------
# Deadlines: producer semantics, confirmation guards, consumers
# ---------------------------------------------------------------------------

def test_set_schedule_never_stores_confirmed_without_instant(db):
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="shop", deadline_raw="近日中",
        deadline_tz="Asia/Tokyo", deadline_jst="近日中",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    row = db.execute(
        "SELECT deadline_jst, deadline_confidence FROM"
        " release_schedule WHERE id=?", (sid,)).fetchone()
    assert row["deadline_jst"] is None
    assert row["deadline_confidence"] == "tz_unknown"
    # And it is VISIBLE in the unconfirmed summary (was invisible).
    assert deadlines_ops.unconfirmed_summary(db).get("tz_unknown") == 1


def test_set_schedule_validates_kind_vocabulary(db):
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="shop", deadline_raw="x",
        deadline_kind="nonsense_kind")
    row = db.execute(
        "SELECT deadline_kind FROM release_schedule WHERE id=?",
        (sid,)).fetchone()
    assert row[0] == "unknown"


def test_set_schedule_postponement_supersedes(db):
    past = clock.now(db) + timedelta(hours=20)
    sid1 = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label", deadline_raw="初回",
        deadline_tz="Asia/Tokyo", deadline_jst=past.isoformat(),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    later = clock.now(db) + timedelta(days=5)
    sid2 = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label", deadline_raw="延期後",
        deadline_tz="Asia/Tokyo", deadline_jst=later.isoformat(),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    assert sid2 != sid1
    old = db.execute(
        "SELECT superseded_by FROM release_schedule WHERE id=?",
        (sid1,)).fetchone()
    assert old[0] == sid2
    assert db.execute(
        "SELECT COUNT(*) FROM deadline_history WHERE schedule_id=?",
        (sid1,)).fetchone()[0] == 1
    # The stale date must not alert; the new one is not due yet.
    assert deadlines_ops.staged_alerts(db) == 0
    # Re-setting the same instant is a no-op (same slot).
    assert deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label", deadline_raw="延期後",
        deadline_tz="Asia/Tokyo", deadline_jst=later.isoformat(),
        deadline_confidence="confirmed",
        deadline_kind="hard_cutoff") == sid2


def test_confirm_with_tz_refuses_to_move_a_confirmed_deadline(db):
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="manual",
        deadline_raw="2027-03-01 10:00", deadline_tz="Asia/Tokyo",
        deadline_jst="2027-03-01T10:00:00",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    before = db.execute(
        "SELECT deadline_jst FROM release_schedule WHERE id=?",
        (sid,)).fetchone()[0]
    assert deadlines_ops.confirm_with_tz(db, sid, "UTC") is False
    after = db.execute(
        "SELECT deadline_jst FROM release_schedule WHERE id=?",
        (sid,)).fetchone()[0]
    assert after == before


def test_confirm_with_tz_records_offset_for_offset_raw(db):
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="manual",
        deadline_raw="2027-03-01T10:00:00+09:00",
        deadline_confidence="tz_unknown", deadline_kind="hard_cutoff")
    assert deadlines_ops.confirm_with_tz(db, sid, "UTC") is True
    row = db.execute(
        "SELECT deadline_tz, deadline_jst, deadline_confidence"
        " FROM release_schedule WHERE id=?", (sid,)).fetchone()
    # The instant came from the text's own offset; the recorded tz
    # is that offset, not the button's zone.
    assert row["deadline_tz"] == "+09:00"
    assert row["deadline_confidence"] == "confirmed"
    assert db.execute(
        "SELECT COUNT(*) FROM deadline_history WHERE schedule_id=?",
        (sid,)).fetchone()[0] == 1


def test_missed_declaration_respects_a_72h_alert(db):
    past = clock.now(db) - timedelta(hours=2)
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label", deadline_raw="締切",
        deadline_tz="Asia/Tokyo", deadline_jst=past.isoformat(),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    # The 72h alert DID go out (only the 24h stage was slept through).
    outbox.enqueue(db, event_type="deadline_alert", body="x",
                   idempotency_key=f"deadline_alert|{sid}|72h")
    db.commit()
    deadlines_ops.staged_alerts(db)
    missed = db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " idempotency_key=?", (f"deadline_missed|{sid}",)).fetchone()[0]
    assert missed == 0


def test_deadlines_command_hides_past_and_superseded(db, monkeypatch):
    past_today = clock.now(db) - timedelta(hours=1)
    deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label", deadline_raw="本日終了",
        deadline_tz="Asia/Tokyo", deadline_jst=past_today.isoformat(),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    _edition(db, "AR-2", "未来盤")
    future = clock.now(db) + timedelta(days=2)
    deadlines_ops.set_schedule(
        db, edition_id="AR-2", channel="label", deadline_raw="明後日",
        deadline_tz="Asia/Tokyo", deadline_jst=future.isoformat(),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/deadlines")
    text = "\n".join(t for t, _m in sent)
    assert "明後日" in text
    assert "本日終了" not in text


def test_deadline_command_registers_manually(db, monkeypatch):
    _edition(db)
    bot, sent = _bot(db, monkeypatch)
    action = _cmd(bot, "/deadline AR-1 2027-02-07 23:59 Asia/Tokyo hard")
    assert action == "deadline_registered"
    row = db.execute("SELECT * FROM release_schedule").fetchone()
    assert row["deadline_confidence"] == "confirmed"
    assert row["deadline_kind"] == "hard_cutoff"
    assert row["deadline_jst"].startswith("2027-02-07T23:59")
    assert row["channel"] == "manual"


def test_deadline_command_without_tz_stays_unconfirmed(db, monkeypatch):
    _edition(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cmd(bot, "/deadline AR-1 2027-02-07 23:59") \
        == "deadline_registered"
    row = db.execute("SELECT * FROM release_schedule").fetchone()
    assert row["deadline_jst"] is None
    assert row["deadline_confidence"] == "tz_unknown"


def test_coverage_counts_store_listing_evidence(db, monkeypatch):
    _edition(db)
    # W-check B5 fixture update: /coverage now requires the
    # listing to be FRESH (seen within 30 days — seller 中-2);
    # this fixture models a live listing, so it carries a
    # current last_seen.
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, status, last_seen) VALUES('AR-1', 'shopA',"
        " 'https://shop.example/i', 3000, 'JPY', 'active', ?)",
        (clock.now_iso(db),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/coverage")
    text = "\n".join(t for t, _m in sent)
    assert "価格証拠あり: 1件" in text


def test_coverage_ignores_suspended_ebay_listing(db, monkeypatch):
    _edition(db, "AR-1", "通常盤")
    _edition(db, "AR-2", "eBayのみの盤")
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price, total_jpy,"
        " currency, status, last_seen) VALUES('AR-2', 'eBay Browse used',"
        " 'https://ebay.example/item', 1000, 1000, 'JPY', 'active', ?)",
        (clock.now_iso(db),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/coverage")
    text = "\n".join(t for t, _m in sent)
    assert "価格証拠あり: 0件" in text


def test_health_digest_unconfirmed_headline_is_distinct_rows(db):
    deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="a", deadline_raw="x",
        deadline_kind="hard_cutoff")  # tz_unknown axis
    deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="b",
        deadline_raw="2027-01-01", deadline_tz="Asia/Tokyo",
        deadline_jst="2027-01-01T00:00:00",
        deadline_confidence="confirmed",
        deadline_kind="unknown")  # kind axis
    text = digest.build_health_digest(db)
    assert "締切未確定（要確認）: 2件" in text


def test_deadline_sweep_body_names_the_target(db):
    _edition(db, "AR-1", "スイープ対象盤")
    from conftest import set_clock_to
    set_clock_to(21, 45)
    soon = clock.now(db) + timedelta(hours=2)
    deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label", deadline_raw="今夜",
        deadline_tz="Asia/Tokyo", deadline_jst=soon.isoformat(),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    assert digest.deadline_sweep(db) == 1
    body = db.execute(
        "SELECT body FROM notification_outbox WHERE"
        " event_type='deadline_sweep'").fetchone()[0]
    assert "スイープ対象盤" in body


# ---------------------------------------------------------------------------
# REPRESS intake / window hardening + bridge
# ---------------------------------------------------------------------------

def test_register_signal_rejects_garbage_and_idless(db):
    assert repress.register_signal(
        db, system="announcement", candidate_id=1, at="zzzz") is False
    assert repress.register_signal(
        db, system="announcement", at="2026-01-01T00:00:00") is False
    assert repress.register_signal(
        db, system="announcement", candidate_id=1, at=12345) is False
    assert db.execute(
        "SELECT COUNT(*) FROM repress_signals").fetchone()[0] == 0


def test_register_signal_future_at_is_clamped_not_counted_early(db):
    future = (clock.now(db) + timedelta(days=365)).isoformat()
    assert repress.register_signal(
        db, system="announcement", candidate_id=7, at=future) is True
    row = db.execute("SELECT at FROM repress_signals").fetchone()
    assert row[0] <= clock.now_iso(db)
    # Even a second counted system with a garbage-future date can
    # no longer fire from outside the window.
    assert repress.register_signal(
        db, system="variant_stock", candidate_id=7,
        at="2999-01-01T00:00:00") is True
    # Both were clamped to now -> in window -> this DOES fire, but
    # for real (present) signals, not future ones.
    assert set(repress.systems_in_window(db, candidate_id=7)) == \
        {"announcement", "variant_stock"}


def test_systems_in_window_has_an_upper_bound(db):
    # A legacy row written directly with a future timestamp (the
    # pre-B4 intake allowed it) must not count.
    future = (clock.now(db) + timedelta(days=10)).isoformat()
    db.execute(
        "INSERT INTO repress_signals(candidate_id, system, detail,"
        " at) VALUES(9, 'announcement', '', ?)", (future,))
    db.commit()
    assert repress.systems_in_window(db, candidate_id=9) == []


def test_digest_notes_skip_ghosts_rejected_and_carry_dates(db):
    db.execute(
        "INSERT INTO repress_signals(system, detail, at)"
        " VALUES('announcement', '', ?)", (clock.now_iso(db),))
    cid = _candidate(db, title="却下済み候補", status="rejected")
    repress.register_signal(db, system="announcement", candidate_id=cid)
    cid2 = _candidate(db, title="様子見候補")
    repress.register_signal(db, system="discogs_stats",
                            candidate_id=cid2)
    db.commit()
    notes = digest_notes_text(db)
    assert all("cand:None" not in n for n in notes)
    assert all("却下済み候補" not in n and f"cand:{cid}" not in n
               for n in notes)
    mine = [n for n in notes if f"cand:{cid2}" in n]
    assert len(mine) == 1 and "最終" in mine[0]


def digest_notes_text(db):
    return repress.digest_notes(db)


def test_announcement_for_known_edition_keys_to_the_edition(db):
    _edition(db, "AR-1", "既存盤タイトル")
    f = BlipBlopFetcher(db, 1)
    f.register_candidates([{
        "title": "既存盤タイトル",
        "url": "https://blipblop.example/post1",
        "summary": "再プレスが決定しました", "published": ""}])
    sig = db.execute(
        "SELECT edition_id, candidate_id FROM repress_signals WHERE"
        " system='announcement'").fetchone()
    assert sig is not None and sig["edition_id"] == "AR-1"


# ---------------------------------------------------------------------------
# Blip Blop stage classification + scoring wiring + containment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("summary,expected", [
    ("再プレスが決定しました", "repress_announced"),
    ("再発予定はありません", None),
    ("完売していたLP、再発売が決定", None),
    ("予約受付開始のお知らせ", "preorder_open"),
    ("通常の新譜情報", None),
])
def test_event_stage_classification(db, summary, expected):
    stage = BlipBlopFetcher._event_stage(
        {"title": "テスト盤", "summary": summary})
    assert stage == expected


def test_repress_announcement_reaches_scoring_floor(db):
    f = BlipBlopFetcher(db, 1)
    res = f.register_candidates([{
        "title": "再プレス盤タイトル",
        "url": "https://blipblop.example/p1",
        "summary": "再プレス決定", "published": ""}])
    assert res["created"] == 1
    q = db.execute(
        "SELECT priority FROM review_queue WHERE"
        " kind='candidate_review'").fetchone()
    cand = {"title": "再プレス盤タイトル", "country": None,
            "source_country": None, "existence_confidence": "中",
            "event_stage": "repress_announced"}
    assert q[0] == scoring.review_priority(db, cand)
    sc = scoring.discovery_score(db, cand)
    assert sc["floor_applied"] or sc["score"] >= 60


def test_blipblop_poison_item_does_not_stop_the_lane(db):
    f = BlipBlopFetcher(db, 1)
    good1 = {"title": "良品その1", "url": "https://b.example/1",
             "summary": "", "published": ""}
    poison = {"title": "毒\ud800タイトル", "url": "https://b.example/2",
              "summary": "", "published": ""}
    good2 = {"title": "良品その2", "url": "https://b.example/3",
             "summary": "", "published": ""}
    res = f.register_candidates([good1, poison, good2])
    assert res["created"] == 2 and res["failed"] == 1
    titles = {r[0] for r in db.execute(
        "SELECT title FROM candidate_editions").fetchall()}
    assert titles == {"良品その1", "良品その2"}
    # Next cycle: the poison still fails alone; the goods still flow.
    res2 = f.register_candidates([poison])
    assert res2["failed"] == 1


def test_feed_poison_item_does_not_stop_the_lane(db):
    f = FeedFetcher(db, 1)
    items = [{"title": "フィード良品", "url": "https://f.example/1",
              "summary": "", "published": ""},
             {"title": "毒\ud800", "url": "https://f.example/2",
              "summary": "", "published": ""},
             {"title": "フィード良品2", "url": "https://f.example/3",
              "summary": "", "published": ""}]
    res = f.register_candidates(items, found_via="feed:test")
    assert res["created"] == 2 and res["failed"] == 1


# ---------------------------------------------------------------------------
# Corroboration is used: priority refresh + rejected reopening
# ---------------------------------------------------------------------------

def test_note_candidate_source_canonicalises_urls(db):
    cid = _candidate(db)
    row = db.execute(
        "SELECT fingerprint FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()
    candidates.note_candidate_source(
        db, row[0], "https://shop.example/item?utm_source=x")
    candidates.note_candidate_source(
        db, row[0], "https://shop.example/item")
    stored = json.loads(db.execute(
        "SELECT source_urls_json FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0])
    hosts = {normalize.canonical_url(u) for u in stored}
    assert len(hosts) == 1  # primary + utm twin collapse to one
    assert candidates.independent_source_count(db, cid) == 1


def test_independent_corroboration_refreshes_queue_priority(db):
    cid = _candidate(db)
    qid = _queue_row(db, "candidate_review", {"candidate_id": cid},
                     priority=30)
    fp = db.execute(
        "SELECT fingerprint FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0]
    candidates.note_candidate_source(
        db, fp, "https://label.example/news/1")
    db.commit()
    prio = db.execute(
        "SELECT priority FROM review_queue WHERE id=?",
        (qid,)).fetchone()[0]
    assert prio > 30  # multi_source component now has a producer


def test_rejected_candidate_reopens_on_independent_corroboration(db):
    cid = _candidate(db)
    db.execute(
        "UPDATE candidate_editions SET status='rejected' WHERE id=?",
        (cid,))
    db.commit()
    fp = db.execute(
        "SELECT fingerprint FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0]
    # Same-host re-sighting: no reopening.
    candidates.note_candidate_source(
        db, fp, "https://shop.example/item2")
    db.commit()
    assert db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0] == "rejected"
    # A NEW independent host: reopened with a fresh review row and
    # a fresh button message.
    candidates.note_candidate_source(
        db, fp, "https://label.example/news/1")
    db.commit()
    assert db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0] == "pending"
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='candidate_review' AND resolved_at IS NULL"
        ).fetchone()[0] == 1
    # R19: the reopen suffix now names the corroborating host
    # (a fixed '|reopen' suffix silently swallowed the SECOND
    # reopening's notification) — match the prefix.
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " idempotency_key LIKE ?",
        (f"candidate_review|{cid}|reopen%",)).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Misc B4 surfaces
# ---------------------------------------------------------------------------

def test_fingerprint_folds_middle_dot_variants():
    a = fingerprint_for("オリジナル・サウンドトラック", None)
    b = fingerprint_for("オリジナルサウンドトラック", None)
    assert a == b


def test_page_identity_url_unifies_variants():
    a = page_identity_url("http://www.shop.example/item/1/")
    b = page_identity_url("https://shop.example/item/1")
    assert a == b


def test_enqueue_pending_reviews_survives_poison_rows(db):
    cid = _candidate(db)
    _queue_row(db, "candidate_review", {"candidate_id": True})
    _queue_row(db, "candidate_review", {"candidate_id": 1.9})
    _queue_row(db, "candidate_review", {"candidate_id": 10 ** 30})
    _queue_row(db, "candidate_review", {"candidate_id": cid})
    assert outbox.enqueue_pending_candidate_reviews(db) == 1


def test_scoring_tolerates_mistyped_candidate_fields(db):
    sc = scoring.discovery_score(db, {
        "distribution_type": ["goods"], "catalog_norm": 12345,
        "label": ["x"], "title": ["y"], "country": "JP"})
    assert isinstance(sc["score"], int)
    assert isinstance(scoring.review_priority(db, {
        "distribution_type": {"x": 1}, "catalog_norm": 999}), int)


def test_tzfix_callback_grammar_is_strict(db, monkeypatch):
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="manual",
        deadline_raw="2027-03-01 10:00",
        deadline_confidence="tz_unknown", deadline_kind="hard_cutoff")
    bot, sent = _bot(db, monkeypatch)
    # The wrapped-index bug: -1 must NOT confirm as Europe/London —
    # the callback is rejected as malformed instead.
    assert _cb(bot, f"tzfix:{sid}:-1") == "unknown_callback"
    assert _cb(bot, f"tzfix:{sid}_0:0") == "unknown_callback"
    assert _cb(bot, "tzfix:99999999999999999999999:0") \
        == "unknown_callback"
    row = db.execute(
        "SELECT deadline_confidence FROM release_schedule WHERE"
        " id=?", (sid,)).fetchone()
    assert row[0] == "tz_unknown"


def test_variant_stock_flap_does_not_arm_repress(db):
    # A sold_candidate (one INFERRED absence round) returning is a
    # flap, not a confirmed-gone copy returning: no variant_stock
    # signal. An 'ended' copy returning DOES arm it (control case).
    # R19 fixture note: the flap here is absence-derived. A shop-
    # DECLARED OutOfStock -> InStock transition is a different
    # fact (the shop itself declared both states) and DOES arm
    # since R19 — see test_wcheck_b4_r19.
    from grt.fetchers.ingest import IngestFetcher
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm, label,"
        " year, format, country, distribution_type, watch_level)"
        " VALUES('AR-1', 'Game OST LP', 'KICA-1001', 'KICA1001',"
        " 'King Records', 1995, 'LP', 'JP', 'retail', 0)")
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES('Shop',"
        " 'STORE', 'approved', 'https://shop.jp/l')")
    db.commit()
    f = IngestFetcher(db, int(cur.lastrowid))
    item = {"title": "Game OST LP", "raw_title": "Game OST LP",
            "url": "https://shop.jp/p/1", "price": 10000.0,
            "currency": "JPY", "catalog_raw": "KICA-1001",
            "label": "King Records", "year": 1995, "format": "LP",
            "country": "JP", "distribution_type": "retail",
            "route": "retail", "description": "",
            "availability": "InStock"}
    f.register_ingested("Shop", [dict(item)])
    # One inferred absence round -> sold_candidate (the flap).
    from grt import evidence
    lid = db.execute("SELECT id FROM listings").fetchone()[0]
    evidence.mark_absent(db, lid, True)
    st = db.execute("SELECT status FROM listings").fetchone()
    assert st[0] == "sold_candidate"
    f.register_ingested("Shop", [dict(item)])
    assert db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='variant_stock'").fetchone()[0] == 0
    # Control: confirmed-gone ('ended') returning arms the signal.
    db.execute("UPDATE listings SET status='ended'")
    db.commit()
    f.register_ingested("Shop", [dict(item)])
    assert db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='variant_stock'").fetchone()[0] == 1
