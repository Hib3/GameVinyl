"""W-check Block 4 R19 follow-up regression tests.

The R19 seven-persona re-check found the R18 fixes correct on
their main paths but defective at their edges. Each test here
pins one of those edge defects: doujin postponements that never
reached the schedule, match_review approvals that the next
re-match silently undid, vague deadline re-registrations that
destroyed confirmed rows, REPRESS bridge key fragmentation,
host-as-operator over-counting, declared-vs-inferred stock
transitions, and poison rows in the queue consumers.
"""
import json

import pytest

from grt import (bot as bot_mod, candidates, clock, deadlines_ops,
                 digest, matcher, normalize, outbox, repress, seedops)
from grt.bot import Bot
from grt.fetchers.blipblop import BlipBlopFetcher
from grt.fetchers.doujin_event import (DoujinEventFetcher,
                                       extract_event_items)
from grt.fetchers.ingest import IngestFetcher
from grt.fetchers.webpages import FeedFetcher
from grt.normalize import fingerprint_for, page_identity_url


class _Transport:
    def __call__(self, *a, **kw):
        return {"ok": True}


_uid = [5000]


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


def _queue_row(db, kind, payload, priority=10, fingerprint=None):
    cur = db.execute(
        "INSERT INTO review_queue(kind, payload_json, fingerprint,"
        " priority, created_at) VALUES(?, ?, ?, ?, ?)",
        (kind, json.dumps(payload), fingerprint, priority,
         clock.now_iso(db)))
    db.commit()
    return int(cur.lastrowid)


def _add_event(db, name="テスト即売会", starts_on=None):
    cur = db.execute(
        "INSERT INTO event_catalog(name, kind, url, starts_on)"
        " VALUES(?, 'doujin', 'https://event.example/', ?)",
        (name, starts_on))
    db.commit()
    return int(cur.lastrowid)


def _candidate(db, title="候補タイトル", status="pending", fp=None,
               found_via="feed:test", url="https://shop.example/item"):
    fp = fp or fingerprint_for(title, None)
    cur = db.execute(
        "INSERT INTO candidate_editions(title, raw_title, source_url,"
        " fingerprint, existence_confidence, price_confidence,"
        " found_at, first_seen, last_seen, found_via, status)"
        " VALUES(?, ?, ?, ?, '中', '低', ?, ?, ?, ?, ?)",
        (title, title, url, fp, clock.now_iso(db), clock.now_iso(db),
         clock.now_iso(db), found_via, status))
    db.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Doujin: postponement, sightings, seed routing
# ---------------------------------------------------------------------------

def test_doujin_postponement_propagates_to_schedule(db):
    ev = _add_event(db, starts_on="2027-02-07")
    f = DoujinEventFetcher(db, 1)
    item = {"title": "同人サントラ", "url": "https://c.example/w1",
            "circle": "波動", "raw_title": "同人サントラ"}
    f.register_event_items([item], ev)
    # The event is postponed; the same item is re-harvested.
    db.execute("UPDATE event_catalog SET starts_on='2027-02-14'"
               " WHERE id=?", (ev,))
    db.commit()
    f.register_event_items([item], ev)
    live = db.execute(
        "SELECT * FROM release_schedule WHERE superseded_by IS NULL"
    ).fetchall()
    assert len(live) == 1
    assert live[0]["deadline_jst"].startswith("2027-02-14")
    old = db.execute(
        "SELECT COUNT(*) FROM release_schedule WHERE"
        " superseded_by IS NOT NULL").fetchone()[0]
    assert old == 1


def test_doujin_late_filled_start_date_creates_schedule(db):
    ev = _add_event(db, starts_on=None)
    f = DoujinEventFetcher(db, 1)
    item = {"title": "同人サントラ", "url": "https://c.example/w1",
            "circle": "波動", "raw_title": "同人サントラ"}
    f.register_event_items([item], ev)
    assert db.execute(
        "SELECT COUNT(*) FROM release_schedule").fetchone()[0] == 0
    db.execute("UPDATE event_catalog SET starts_on='2027-03-01'"
               " WHERE id=?", (ev,))
    db.commit()
    f.register_event_items([item], ev)
    rows = db.execute("SELECT * FROM release_schedule").fetchall()
    assert len(rows) == 1
    assert rows[0]["deadline_jst"].startswith("2027-03-01")


def test_doujin_sightings_keyed_by_event_id_and_deduped(db):
    ev1 = _add_event(db, "C100")
    ev2 = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    item = {"title": "再頒布盤", "url": "https://c.example/w1",
            "circle": "波動", "raw_title": "再頒布盤"}
    f.register_event_items([item], ev1)
    for _ in range(3):
        f.register_event_items([item], ev2)

    def sightings():
        return db.execute(
            "SELECT COUNT(*) FROM system_events WHERE"
            " kind='doujin_event_sighting'").fetchone()[0]
    assert sightings() == 1
    # Renaming event 2 (same id) is not a new sighting.
    db.execute("UPDATE event_catalog SET name='C101改' WHERE id=?",
               (ev2,))
    db.commit()
    f.register_event_items([item], ev2)
    assert sightings() == 1
    # A DIFFERENT event id that merely shares event 1's name IS a
    # different event: the sighting fires.
    ev3 = _add_event(db, "C100")
    f.register_event_items([item], ev3)
    assert sightings() == 2
    detail = json.loads(db.execute(
        "SELECT detail_json FROM system_events WHERE"
        " kind='doujin_event_sighting' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0])
    assert detail["event_id"] == ev3
    assert detail["candidate_id"] is not None


def test_doujin_item_matching_a_seed_creates_no_split_candidate(db):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    ev = _add_event(db, "C101")
    f = DoujinEventFetcher(db, 1)
    reg = f.register_event_items(
        [{"title": "深海サントラ", "url": "https://c.example/w9",
          "circle": "波動", "raw_title": "深海サントラ"}], ev)
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='seed_promotion'").fetchone()[0] == 1
    others = db.execute(
        "SELECT COUNT(*) FROM candidate_editions WHERE id!=? AND"
        " status='pending'", (seed_id,)).fetchone()[0]
    assert others == 0


def test_promote_seed_folds_the_harvest_candidate(db, monkeypatch):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    harvest = _candidate(
        db, title="深海サントラ", found_via="doujin_event:C101",
        fp=fingerprint_for("深海サントラ", None, "波動"))
    deadlines_ops.set_schedule(
        db, candidate_id=harvest, channel="doujin_event",
        deadline_raw="C101 開催 2027-02-07",
        deadline_tz="Asia/Tokyo", deadline_jst="2027-02-07T00:00:00",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    repress.register_signal(
        db, candidate_id=harvest, system="announcement",
        at="2026-11-01T10:00:00+09:00")
    seedops.request_seed_promotion(
        db, seed_id, found={"title": "深海サントラ", "circle": "波動"})
    q = db.execute(
        "SELECT * FROM review_queue WHERE kind='seed_promotion'"
    ).fetchone()
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{q['id']}") == "seed_promoted"
    row = db.execute(
        "SELECT status, reject_reason FROM candidate_editions"
        " WHERE id=?", (harvest,)).fetchone()
    assert row[0] == "rejected"
    assert row[1] == f"merged_into_seed:{seed_id}"
    sched = db.execute(
        "SELECT candidate_id FROM release_schedule WHERE"
        " superseded_by IS NULL").fetchall()
    assert [r[0] for r in sched] == [seed_id]
    sig = db.execute(
        "SELECT candidate_id FROM repress_signals").fetchall()
    assert [r[0] for r in sig] == [seed_id]


def test_promote_seed_binds_the_approved_found(db):
    seed_id = seedops.import_seed(
        db, title="深海サントラ", circle="波動", event_name="C100")
    seedops.request_seed_promotion(
        db, seed_id, found={"title": "深海サントラ", "circle": "波動"})
    q = db.execute(
        "SELECT * FROM review_queue WHERE kind='seed_promotion'"
    ).fetchone()
    db.execute(
        "UPDATE review_queue SET resolution='approved',"
        " resolved_at=? WHERE id=?",
        (clock.now_iso(db), q["id"]))
    db.commit()
    # A different 'found' than the approved one must not promote.
    out = seedops.promote_seed(
        db, seed_id, found={"title": "全然別の盤", "circle": "他人"},
        review_queue_id=q["id"])
    assert out["promoted"] is False
    out = seedops.promote_seed(
        db, seed_id, found={"title": "深海サントラ", "circle": "波動"},
        review_queue_id=q["id"])
    assert out["promoted"] is True
    # The approval is single-use.
    db.execute(
        "UPDATE candidate_editions SET status='seed' WHERE id=?",
        (seed_id,))
    db.commit()
    out = seedops.promote_seed(
        db, seed_id, found={"title": "深海サントラ", "circle": "波動"},
        review_queue_id=q["id"])
    assert out["promoted"] is False


def test_extract_event_items_skips_navigation_links(db):
    html = """
    <ul><li>
      <a href="/item/list">商品一覧</a>
      <a href="/product/guide">購入ガイド</a>
      <a href="/detail/help">ヘルプ詳細</a>
      <a href="/work/55">本物の新刊</a>
      <a href="/search?q=/work/99">検索</a>
    </li></ul>"""
    items = extract_event_items(html, "https://event.example/")
    titles = {i["title"] for i in items}
    assert titles == {"本物の新刊"}


def test_extract_event_items_reads_image_alt(db):
    html = """
    <ul><li>
      <a href="/work/7"><img alt="画像だけの新刊" src="x.png"></a>
    </li></ul>"""
    items = extract_event_items(html, "https://event.example/")
    assert {i["title"] for i in items} == {"画像だけの新刊"}


# ---------------------------------------------------------------------------
# match_review: verified consumption + approval stickiness
# ---------------------------------------------------------------------------

def _match_setup(db, *, result_extra=None, decision_edition="AR-1"):
    _edition(db)
    cur = db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, raw_title) VALUES('AR-1',"
        " 'shopA', 'https://shop.example/i1', 3000, 'JPY', 3000,"
        " 'active', 'テスト盤')")
    lid = int(cur.lastrowid)
    cur = db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id, score,"
        " existence_confidence, price_confidence) VALUES(?, ?,"
        " 80, '高', '中')", (lid, decision_edition))
    dec_id = int(cur.lastrowid)
    db.commit()
    result = {"edition_id": "AR-1", "score": 80,
              "decision_id": dec_id, "existence_confidence": "高",
              "price_confidence": "中", "breakdown": {},
              "variant_gate": False}
    if result_extra:
        result.update(result_extra)
    payload = {"listing": {"source": "shopA",
                           "url": "https://shop.example/i1",
                           "raw_title": "テスト盤"},
               "result": result}
    qid = _queue_row(db, "match_review", payload, priority=50,
                     fingerprint="https://shop.example/i1|AR-1")
    return lid, qid


def test_match_review_unresolvable_payload_is_not_consumed(
        db, monkeypatch):
    qid = _queue_row(db, "match_review",
                     {"listing": {}, "result": {}}, priority=50)
    bot, sent = _bot(db, monkeypatch)
    action = _cb(bot, f"qok:{qid}")
    assert action == "match_review_unresolvable"
    row = db.execute(
        "SELECT resolved_at FROM review_queue WHERE id=?",
        (qid,)).fetchone()
    assert row[0] is None  # NOT settled: no false success
    assert db.execute(
        "SELECT COUNT(*) FROM match_decisions").fetchone()[0] == 0
    assert any("未処理のまま" in t for t, _ in sent)


def test_match_review_decision_mismatch_is_refused(db, monkeypatch):
    # The payload claims AR-1, but the decision it cites judged a
    # different edition: approving must not mint a 高 decision.
    lid, qid = _match_setup(db, decision_edition="AR-999")
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "match_review_unresolvable"
    from grt.pricedrop import price_evidence_ok
    assert price_evidence_ok(db, lid, "AR-1") is False


def test_match_review_approval_survives_the_next_rematch(
        db, monkeypatch):
    from grt.pricedrop import price_evidence_ok
    lid, qid = _match_setup(db)
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"qok:{qid}") == "match_review_approved"
    assert price_evidence_ok(db, lid, "AR-1") is True
    n_decisions = db.execute(
        "SELECT COUNT(*) FROM match_decisions").fetchone()[0]
    # The next ingest cycle re-matches the same sighting.
    listing = {"source": "shopA", "url": "https://shop.example/i1",
               "raw_title": "テスト盤", "title": "テスト盤",
               "catalog_raw": "KICA-1001", "price": 3000,
               "currency": "JPY"}
    matcher.match_listing(db, listing, listing_id=lid)
    assert price_evidence_ok(db, lid, "AR-1") is True
    assert db.execute(
        "SELECT COUNT(*) FROM match_decisions").fetchone()[0] \
        == n_decisions
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='match_review' AND resolved_at IS NULL"
    ).fetchone()[0] == 0


def test_queue_survives_nested_poison_payloads(db, monkeypatch):
    _edition(db)
    _queue_row(db, "match_review",
               {"listing": "壊", "result": 42}, priority=90)
    good = _candidate(db, title="正常な候補")
    _queue_row(db, "candidate_review", {"candidate_id": good},
               priority=10)
    bot, sent = _bot(db, monkeypatch)
    # /queue must present the good item, not wedge on the poison.
    assert _cmd(bot, "/queue") in ("queue", "queue_empty")
    assert any("正常な候補" in t for t, _ in sent)
    poison = db.execute(
        "SELECT resolution FROM review_queue WHERE"
        " kind='match_review'").fetchone()
    assert poison[0] == "stale"


# ---------------------------------------------------------------------------
# Deadlines: slot integrity, canonical unconfirmed counts, sweep
# ---------------------------------------------------------------------------

def _sched(db, **kw):
    base = dict(channel="web", deadline_raw="締切",
                deadline_confidence="confirmed",
                deadline_kind="hard_cutoff")
    base.update(kw)
    return deadlines_ops.set_schedule(db, **base)


def test_instantless_reregistration_keeps_the_confirmed_row(db):
    _edition(db)
    first = _sched(db, edition_id="AR-1", deadline_tz="Asia/Tokyo",
                   deadline_jst="2027-02-07T23:59:00")
    _sched(db, edition_id="AR-1", deadline_raw="近日中",
           deadline_confidence="tz_unknown", deadline_jst=None)
    live = db.execute(
        "SELECT id FROM release_schedule WHERE"
        " superseded_by IS NULL AND deadline_jst IS NOT NULL"
    ).fetchall()
    assert [r[0] for r in live] == [first]


def test_idless_schedules_never_supersede_each_other(db):
    a = _sched(db, deadline_tz="Asia/Tokyo",
               deadline_jst="2027-02-07T23:59:00")
    b = _sched(db, deadline_tz="Asia/Tokyo",
               deadline_jst="2027-03-07T23:59:00")
    live = db.execute(
        "SELECT COUNT(*) FROM release_schedule WHERE"
        " superseded_by IS NULL").fetchone()[0]
    assert live == 2 and a != b


def test_same_instant_two_channels_alert_once(db):
    _edition(db)
    _sched(db, edition_id="AR-1", channel="doujin_event",
           deadline_tz="Asia/Tokyo",
           deadline_jst="2026-11-09T12:00:00")
    _sched(db, edition_id="AR-1", channel="manual",
           deadline_tz="Asia/Tokyo",
           deadline_jst="2026-11-09T12:00:00")
    live = db.execute(
        "SELECT COUNT(*) FROM release_schedule WHERE"
        " superseded_by IS NULL").fetchone()[0]
    assert live == 2  # distinct registrations stay
    # Freeze 'now' inside the 72h stage for both rows.
    import grt.clock as _clock
    real_now = _clock.now
    from datetime import datetime, timedelta
    fixed = _clock.parse_iso("2026-11-09T12:00:00+09:00") \
        - timedelta(hours=30)
    _clock.now = lambda db_: fixed
    try:
        created = deadlines_ops.staged_alerts(db)
    finally:
        _clock.now = real_now
    assert created == 1


def test_unconfirmed_partition_covers_null_axes(db):
    _edition(db)
    _sched(db, edition_id="AR-1", deadline_confidence="tz_unknown",
           deadline_jst=None)
    _sched(db, edition_id="AR-1", channel="c2",
           deadline_confidence="confirmed", deadline_kind="unknown",
           deadline_tz="Asia/Tokyo",
           deadline_jst="2027-02-07T00:00:00")
    db.execute(
        "INSERT INTO release_schedule(edition_id, channel,"
        " deadline_raw, deadline_confidence, deadline_kind,"
        " deadline_jst) VALUES('AR-1', 'c3', 'x', 'confirmed',"
        " 'hard_cutoff', NULL)")
    db.execute(
        "INSERT INTO release_schedule(edition_id, channel,"
        " deadline_raw, deadline_confidence, deadline_kind,"
        " deadline_jst) VALUES('AR-1', 'c4', 'x', NULL, NULL, NULL)")
    db.commit()
    summary = deadlines_ops.unconfirmed_summary(db)
    assert deadlines_ops.unconfirmed_count(db) == 4
    assert sum(summary.values()) == 4


def test_deadline_sweep_survives_a_null_title(db):
    from conftest import set_clock_to
    set_clock_to(21, 45)
    db.execute(
        "INSERT INTO editions(id, title) VALUES('AR-NULL', NULL)")
    _edition(db, "AR-OK", "正常タイトル盤")
    now = clock.now(db)
    soon = now.replace(hour=23, minute=0, second=0, microsecond=0)
    if soon <= now:
        from datetime import timedelta as _td
        soon = soon + _td(days=1)
    iso = clock.to_iso(soon)
    for eid in ("AR-NULL", "AR-OK"):
        deadlines_ops.set_schedule(
            db, edition_id=eid, channel="web", deadline_raw="締切",
            deadline_tz="Asia/Tokyo", deadline_jst=iso,
            deadline_confidence="confirmed",
            deadline_kind="hard_cutoff")
    sent = digest.deadline_sweep(db)
    assert sent == 2
    bodies = [r[0] for r in db.execute(
        "SELECT body FROM notification_outbox WHERE"
        " event_type='deadline_sweep'").fetchall()]
    assert any("正常タイトル盤" in b for b in bodies)


def test_rejected_candidate_deadline_does_not_alert(db):
    cid = _candidate(db, title="却下済み候補", status="rejected")
    deadlines_ops.set_schedule(
        db, candidate_id=cid, channel="doujin_event",
        deadline_raw="開催 2026-11-09", deadline_tz="Asia/Tokyo",
        deadline_jst="2026-11-09T00:00:00",
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    import grt.clock as _clock
    from datetime import timedelta
    real_now = _clock.now
    fixed = _clock.parse_iso("2026-11-09T00:00:00+09:00") \
        - timedelta(hours=30)
    _clock.now = lambda db_: fixed
    try:
        created = deadlines_ops.staged_alerts(db)
        lines = deadlines_ops.deadline_digest_lines(db)
    finally:
        _clock.now = real_now
    assert created == 0
    assert lines == []


# ---------------------------------------------------------------------------
# REPRESS: canonical instants, grouping, bridge, follow-ups
# ---------------------------------------------------------------------------

def test_register_signal_canonicalises_offsets(db):
    _edition(db)
    ok = repress.register_signal(
        db, edition_id="AR-1", system="announcement",
        at="2026-11-08T03:00:00+00:00")  # = 12:00 JST
    assert ok is True
    again = repress.register_signal(
        db, edition_id="AR-1", system="announcement",
        at="2026-11-08T12:00:00+09:00")
    assert again is False  # the same instant, deduped
    future = repress.register_signal(
        db, edition_id="AR-1", system="variant_stock",
        at="2027-06-01T00:00:00+09:00")
    assert future is True
    row = db.execute(
        "SELECT at FROM repress_signals WHERE"
        " system='variant_stock'").fetchone()
    assert row[0] <= clock.now_iso(db)  # clamped to now


def test_register_signal_respects_the_callers_transaction(db):
    _edition(db)
    db.execute("BEGIN")
    repress.register_signal(
        db, edition_id="AR-1", system="announcement",
        at="2026-11-01T10:00:00+09:00")
    db.rollback()  # the caller's rollback must take the signal too
    assert db.execute(
        "SELECT COUNT(*) FROM repress_signals").fetchone()[0] == 0


def test_candidate_rejection_withdraws_its_bridged_signal(
        db, monkeypatch):
    _edition(db, "AR-1", "既知の名盤")
    cid = _candidate(db, title="既知の名盤")
    repress.register_signal(
        db, edition_id="AR-1", candidate_id=cid,
        system="announcement", at="2026-11-05T10:00:00+09:00")
    repress.register_signal(
        db, edition_id="AR-1", system="variant_stock",
        at="2026-11-05T11:00:00+09:00")
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"rej:{cid}:OTHER") == "candidate_rejected"
    # The shared row is gone: it can no longer complete the pair.
    assert db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='announcement'").fetchone()[0] == 0
    notes = repress.digest_notes(db)
    # The lone variant_stock signal may still appear as a watch
    # note — what must NOT happen is the pair completing into a
    # REPRESS疑い for a candidate the human rejected.
    assert all("REPRESS疑い" not in n for n in notes)


def test_systems_in_window_counts_a_shared_row_once(db):
    _edition(db)
    cid = _candidate(db)
    repress.register_signal(
        db, edition_id="AR-1", candidate_id=cid,
        system="announcement", at="2026-11-05T10:00:00+09:00")
    systems = repress.systems_in_window(db, candidate_id=cid)
    assert "announcement" not in systems  # edition-keyed, not the
    # candidate's own system


def test_blipblop_followup_article_on_known_candidate_signals(db):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url)"
        " VALUES('Blip', 'rss', 'approved', 'https://b.example/')")
    db.commit()
    f = BlipBlopFetcher(db, int(cur.lastrowid))
    first = {"title": "無名の発掘盤 アナログ化", "url": "https://b.example/1",
             "summary": "初出記事"}
    assert f.register_candidates([dict(first)])["created"] == 1
    # A follow-up article about the SAME work (same headline,
    # new URL, repress stage in the summary) hits the duplicate
    # path — before R19 it was dropped entirely: no announcement
    # row, no signal, although repress news normally arrives as
    # exactly this kind of follow-up.
    follow = {"title": "無名の発掘盤 アナログ化",
              "url": "https://b.example/2",
              "summary": "続報：再プレス決定"}
    assert f.register_candidates([follow])["created"] == 0  # dup
    ann = db.execute(
        "SELECT COUNT(*) FROM announcements").fetchone()[0]
    assert ann == 2  # the follow-up was RECORDED, not dropped
    sigs = db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='announcement'").fetchone()[0]
    assert sigs == 1


def test_blipblop_clause_negation_does_not_announce(db):
    stage = BlipBlopFetcher._event_stage(
        {"title": "お知らせ",
         "summary": "再プレスの予定はございません。再発も行いません"})
    assert stage != "repress_announced"
    stage = BlipBlopFetcher._event_stage(
        {"title": "名盤が再プレス決定", "summary": "予約受付中です"})
    assert stage == "repress_announced"


def test_blipblop_generic_title_never_bridges(db):
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm)"
        " VALUES('AR-G', 'オリジナルサウンドトラック', 'OST-1')")
    db.commit()
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url)"
        " VALUES('Blip', 'rss', 'approved', 'https://b.example/')")
    db.commit()
    f = BlipBlopFetcher(db, int(cur.lastrowid))
    assert f._announcement_edition_id(
        "オリジナルサウンドトラック") is None
    assert f._announcement_edition_id("既知の名盤") is None


def test_candidate_approval_links_the_bridged_edition(
        db, monkeypatch):
    _edition(db, "AR-1", "既知の名盤")
    cid = _candidate(db, title="既知の名盤")
    repress.register_signal(
        db, edition_id="AR-1", candidate_id=cid,
        system="announcement", at="2026-11-05T10:00:00+09:00")
    bot, sent = _bot(db, monkeypatch)
    assert _cb(bot, f"approve:{cid}") == "candidate_approved"
    row = db.execute(
        "SELECT promoted_edition_id FROM candidate_editions"
        " WHERE id=?", (cid,)).fetchone()
    assert row[0] == "AR-1"  # linked, NOT a duplicate AR-C mint
    assert db.execute(
        "SELECT COUNT(*) FROM editions WHERE id LIKE 'AR-C%'"
    ).fetchone()[0] == 0
    assert any("AR-1" in t for t, _ in sent)


# ---------------------------------------------------------------------------
# Corroboration: operator-level hosts, repeated reopenings
# ---------------------------------------------------------------------------

def _rejected_with_source(db, url):
    cid = _candidate(db, title="復活テスト盤", status="rejected",
                     url=url)
    _queue_row(db, "candidate_review", {"candidate_id": cid})
    db.execute(
        "UPDATE review_queue SET resolved_at=?, resolution='rejected'"
        " WHERE kind='candidate_review'", (clock.now_iso(db),))
    db.commit()
    return cid


def test_subdomains_of_one_operator_do_not_reopen(db):
    cid = _rejected_with_source(db, "https://shop.example.com/i1")
    fp = db.execute(
        "SELECT fingerprint FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0]
    candidates.note_candidate_source(
        db, fp, "https://blog.example.com/news")
    db.commit()
    assert db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0] == "rejected"


def test_trailing_dot_host_is_the_same_operator(db):
    cid = _rejected_with_source(db, "https://example.com/i1")
    fp = db.execute(
        "SELECT fingerprint FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0]
    candidates.note_candidate_source(
        db, fp, "https://example.com./i2")
    db.commit()
    assert candidates.independent_source_count(db, cid) == 1
    assert db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0] == "rejected"


def test_second_reopening_also_notifies(db):
    cid = _rejected_with_source(db, "https://one.example/i1")
    fp = db.execute(
        "SELECT fingerprint FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0]
    candidates.note_candidate_source(db, fp, "https://two.example/i")
    db.commit()
    assert db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0] == "pending"
    # Reject again; a THIRD operator corroborates.
    db.execute(
        "UPDATE candidate_editions SET status='rejected' WHERE id=?",
        (cid,))
    db.commit()
    candidates.note_candidate_source(
        db, fp, "https://three.example/i")
    db.commit()
    keys = [r[0] for r in db.execute(
        "SELECT idempotency_key FROM notification_outbox WHERE"
        " idempotency_key LIKE 'candidate_review|" + str(cid)
        + "|reopen%'").fetchall()]
    assert len(keys) == 2  # both reopenings queued a message


def test_tenant_subdomains_are_distinct_operators(db):
    assert candidates._host_of("https://a.myshopify.com/x") \
        == "a.myshopify.com"
    assert candidates._host_of("https://b.myshopify.com/x") \
        == "b.myshopify.com"


# ---------------------------------------------------------------------------
# Ingest stock lifecycle: declared vs inferred
# ---------------------------------------------------------------------------

def _ingest_setup(db):
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm,"
        " label, year, format, country, distribution_type,"
        " watch_level) VALUES('AR-1', 'テスト盤', 'KICA-1001',"
        " 'KICA1001', 'King Records', 1995, 'LP', 'JP', 'retail',"
        " 0)")
    db.commit()
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES('Shop',"
        " 'STORE', 'approved', 'https://shop.jp/l')")
    db.commit()
    return IngestFetcher(db, int(cur.lastrowid))


def _item(**kw):
    base = {"title": "テスト盤", "raw_title": "テスト盤",
            "url": "https://shop.jp/p/1", "price": 10000.0,
            "currency": "JPY", "catalog_raw": "KICA-1001",
            "label": "King Records", "year": 1995, "format": "LP",
            "country": "JP", "distribution_type": "retail",
            "route": "retail", "description": "",
            "availability": "InStock"}
    base.update(kw)
    return base


def _variant_stock_signals(db):
    return db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='variant_stock'").fetchone()[0]


def test_declared_out_of_stock_return_arms_variant_stock(db):
    f = _ingest_setup(db)
    f.register_ingested("Shop", [_item()])
    f.register_ingested("Shop", [_item(availability="OutOfStock")])
    row = db.execute(
        "SELECT status, oos_declared FROM listings").fetchone()
    assert row[0] == "sold_candidate" and row[1] == 1
    f.register_ingested("Shop", [_item()])
    assert _variant_stock_signals(db) == 1


def test_alternating_declared_and_absent_reaches_ended(db):
    from grt import evidence
    f = _ingest_setup(db)
    f.register_ingested("Shop", [_item()])
    f.register_ingested("Shop", [_item(availability="OutOfStock")])
    lid = db.execute("SELECT id FROM listings").fetchone()[0]
    evidence.mark_absent(db, lid, True)  # page-absent round
    assert db.execute(
        "SELECT status FROM listings").fetchone()[0] == "ended"


def test_relisting_under_a_new_url_signals(db):
    f = _ingest_setup(db)
    f.register_ingested("Shop", [_item()])
    db.execute("UPDATE listings SET status='ended'")
    db.commit()
    f.register_ingested("Shop", [_item(url="https://shop.jp/p/2")])
    assert _variant_stock_signals(db) == 1


# ---------------------------------------------------------------------------
# Boundaries: outbox typing, feed containment, identity, bot surfaces
# ---------------------------------------------------------------------------

def test_enqueue_candidate_review_rejects_non_int_ids(db):
    assert outbox.enqueue_candidate_review(db, True) is None
    assert outbox.enqueue_candidate_review(db, 1.9) is None
    assert outbox.enqueue_candidate_review(db, 10 ** 30) is None
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox"
    ).fetchone()[0] == 0


def test_feed_lane_contains_a_non_dict_item(db):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url)"
        " VALUES('Feed', 'rss', 'approved', 'https://f.example/')")
    db.commit()
    f = FeedFetcher(db, int(cur.lastrowid))
    out = f.register_candidates(
        ["毒アイテム",
         {"title": "正常な新譜", "url": "https://f.example/1"}],
        found_via="feed:test")
    assert out["failed"] == 1 and out["created"] == 1


def test_page_identity_url_drops_default_ports():
    assert page_identity_url("https://example.com:443/a/") \
        == "https://example.com/a"
    assert page_identity_url("http://example.com:80/a") \
        == "https://example.com/a"
    assert page_identity_url("https://example.com:8443/a") \
        == "https://example.com:8443/a"


def test_deadline_command_accepts_bot_notation_and_bad_tz(
        db, monkeypatch):
    cid = _candidate(db)
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, f"/deadline 候補#{cid} 2027-02-07 23:59"
              " Asia/Tokyo campaign")
    rows = db.execute(
        "SELECT candidate_id, deadline_jst FROM release_schedule"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == cid
    assert rows[0][1].startswith("2027-02-07")
    _cmd(bot, "/deadline AR-1 2027-02-07 23:59 Foo/Bar campaign")
    assert any("タイムゾーン" in t and "Foo/Bar" in t
               for t, _ in sent)
    assert db.execute(
        "SELECT COUNT(*) FROM release_schedule").fetchone()[0] == 1


def test_coverage_ignores_zero_and_negative_prices(db, monkeypatch):
    _edition(db, "AR-Z", "ゼロ円盤")
    _edition(db, "AR-P", "正価盤")
    # W-check B5 fixture update: /coverage requires listings to
    # be FRESH (last_seen within 30 days); both rows here model
    # live listings, so both carry a current last_seen — the
    # zero/negative exclusion being tested is unaffected.
    _now = clock.now_iso(db)
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, last_seen) VALUES('AR-Z',"
        " 's', 'https://s.example/z', 0, 'JPY', 0, 'active', ?)",
        (_now,))
    db.execute(
        "INSERT INTO listings(edition_id, source, url, price,"
        " currency, total_jpy, status, last_seen) VALUES('AR-P',"
        " 's', 'https://s.example/p', 3000, 'JPY', 3000, 'active',"
        " ?)", (_now,))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/coverage")
    text = sent[-1][0]
    assert "価格証拠あり: 1件" in text  # only the real price


def test_sweep_hit_approval_reopens_a_rejected_candidate(
        db, monkeypatch):
    from grt.normalize import normalize_catalog
    sweep_fp = fingerprint_for(
        "KICA-9999", normalize_catalog("KICA-9999")["norm"])
    cid = _candidate(db, title="KICA-9999", status="rejected",
                     fp=sweep_fp)
    db.execute(
        "UPDATE candidate_editions SET catalog_norm='KICA9999'"
        " WHERE id=?", (cid,))
    db.commit()
    qid = _queue_row(db, "sweep_hit",
                     {"prefix": "KICA", "number": 9999,
                      "catalog": "KICA-9999"}, priority=40)
    bot, sent = _bot(db, monkeypatch)
    action = _cb(bot, f"qok:{qid}")
    assert action == "sweep_hit_reopened"
    assert db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()[0] == "pending"
