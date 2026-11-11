"""Regression tests for the multi-perspective review of Phase 2/3
(2026-11-08, AI-engineer persona — ladder wiring, repress producer
wiring, missed deadlines, per-event harvest declaration)."""
from datetime import timedelta

from grt import clock, deadlines_ops, digest, freqpolicy, health, repress
from grt.fetchers.blipblop import BlipBlopFetcher
from grt.fetchers.ingest import IngestFetcher


def _source(db, name="ShopA", **kw):
    vals = {"status": "approved", "has_ingest_list": "yes",
            "kind": "STORE", "url": "https://example.jp/list",
            "fetch_mode": "ingest"}
    vals.update(kw)
    cur = db.execute(
        "INSERT INTO sources(name, status, has_ingest_list, kind, url,"
        " fetch_mode) VALUES(?,?,?,?,?,?)",
        (name, vals["status"], vals["has_ingest_list"], vals["kind"],
         vals["url"], vals["fetch_mode"]))
    db.commit()
    return int(cur.lastrowid)


# E1a: promotion climbs to the ceiling — the 15-minute tier is
# reachable by an eligible ingest shop (was: capped at 360 forever).
def test_promotion_reaches_fast_tier(db):
    sid = _source(db)
    st = freqpolicy.apply_run_outcome(db, sid, ok=True)
    st = freqpolicy.apply_run_outcome(db, sid, ok=True)
    assert st["tier_min"] == 15
    assert st["event"] == "freq_promotion"


# E1b: an approved non-ingest source can reach 60 but not 15.
def test_promotion_ceiling_60_for_plain_source(db):
    sid = _source(db, "Plain", has_ingest_list="no",
                  fetch_mode="query")
    freqpolicy.apply_run_outcome(db, sid, ok=True)
    st = freqpolicy.apply_run_outcome(db, sid, ok=True)
    assert st["tier_min"] == 60
    freqpolicy.apply_run_outcome(db, sid, ok=True)
    st = freqpolicy.apply_run_outcome(db, sid, ok=True)
    assert st["tier_min"] == 60  # ceiling holds


# E1c: is_due follows the tier (the ladder now drives the jobs).
def test_is_due_uses_tier(db):
    sid = _source(db, "Plain", has_ingest_list="no",
                  fetch_mode="query")
    assert freqpolicy.is_due(db, sid) is True  # never ran
    now = clock.now_iso(db)
    db.execute(
        "INSERT INTO fetch_runs(source_id, result, started_at)"
        " VALUES(?, 'OK', ?)", (sid, now))
    db.commit()
    assert freqpolicy.is_due(db, sid) is False  # home 360 not elapsed
    old = (clock.now(db) - timedelta(hours=7)).isoformat()
    db.execute(
        "UPDATE fetch_runs SET started_at=? WHERE source_id=?",
        (old, sid))
    db.commit()
    assert freqpolicy.is_due(db, sid) is True


def test_official_rss_cadence_caps_polling_and_promotion(db):
    feeds = (
        ("https://blackscreenrecords.com/collections/game-soundtracks.atom", 360),
        ("https://mondoshop.com/collections/soundtracks.atom", 1440),
        ("https://data-discs.com/collections/records.atom", 1440),
        ("https://gamemusicrecords.com/?post_type=product&feed=rss2", 360),
    )
    for url, minimum in feeds:
        sid = _source(db, url, kind="RSS", url=url,
                      fetch_mode="ingest")
        assert freqpolicy.current_tier(db, sid) == minimum
        freqpolicy.apply_run_outcome(db, sid, True)
        state = freqpolicy.apply_run_outcome(db, sid, True)
        assert state["tier_min"] == minimum

        recent = (clock.now(db) - timedelta(minutes=minimum - 1)).isoformat()
        db.execute("INSERT INTO fetch_runs(source_id,result,started_at)"
                    " VALUES(?,'OK',?)", (sid, recent))
        db.commit()
        assert freqpolicy.is_due(db, sid) is False
        elapsed = (clock.now(db) - timedelta(minutes=minimum)).isoformat()
        db.execute("UPDATE fetch_runs SET started_at=? WHERE source_id=?",
                   (elapsed, sid))
        db.commit()
        assert freqpolicy.is_due(db, sid) is True


def test_official_rss_silence_threshold_uses_policy_minimum(db):
    sid = _source(
        db, "game-music-records-official", kind="RSS",
        url="https://gamemusicrecords.com/?post_type=product&feed=rss2",
        fetch_mode="ingest")
    db.execute("UPDATE sources SET expected_cadence_days=? WHERE id=?",
               (1 / 24, sid))
    last_success = (clock.now(db) - timedelta(hours=3)).isoformat()
    db.execute(
        "INSERT INTO source_health(source_id,state,consecutive_failures,"
        "last_success_at) VALUES(?,'HEALTHY',0,?)",
        (sid, last_success))
    db.commit()

    health.evaluate_time_based_health(db)

    assert db.execute(
        "SELECT COUNT(*) FROM observation_gaps WHERE scope=? AND gap_end IS NULL",
        (f"source:{sid}",)).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE event_type='silence_detected'"
        ).fetchone()[0] == 0
    clock.set_local_offset(10 * 3600)
    try:
        health.evaluate_time_based_health(db)
        assert db.execute(
            "SELECT COUNT(*) FROM observation_gaps WHERE scope=? AND gap_end IS NULL",
            (f"source:{sid}",)).fetchone()[0] == 1
    finally:
        clock.set_local_offset(0)


# E3: a deadline missed during downtime is declared, once.
def test_missed_deadline_declared_once(db):
    # W-check B5 fixture update: the row is now registered with a
    # FUTURE deadline and the clock advances past it — the actual
    # downtime scenario. Registering an already-past deadline no
    # longer declares a miss (maniac A 中-4: the declaration
    # asserts the process was down; a row created after its own
    # deadline was never missable). That case is pinned in
    # test_wcheck_b5.py instead.
    future = (clock.now(db) + timedelta(hours=1)).isoformat()
    deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="label",
        deadline_raw="締切", deadline_tz="Asia/Tokyo",
        deadline_jst=future, deadline_confidence="confirmed",
        deadline_kind="hard_cutoff")
    clock.set_local_offset(2 * 3600)
    try:
        created = deadlines_ops.staged_alerts(db)
        assert created == 1
        rows = db.execute(
            "SELECT body FROM notification_outbox WHERE"
            " idempotency_key LIKE 'deadline_missed|%'").fetchall()
        assert len(rows) == 1 and "締切経過（未通知）" in rows[0][0]
        assert deadlines_ops.staged_alerts(db) == 0  # never twice
    finally:
        clock.set_local_offset(0)


# E4: the harvest declaration is per event over its whole window.
def test_harvest_declaration_per_event(db):
    today = clock.now(db).date().isoformat()
    week_ago = (clock.now(db) - timedelta(days=7)).date().isoformat()
    for name in ("EventA", "EventB"):
        db.execute(
            "INSERT INTO event_catalog(name, url, harvest_from,"
            " harvest_to) VALUES(?, 'https://e.jp', ?, ?)",
            (name, week_ago, today))
        cur = db.execute(
            "INSERT INTO sources(name, url, kind, status) VALUES(?,"
            " 'https://e.jp', 'EVENT', 'approved')",
            ("doujin_event:" + name,))
        db.commit()
        if name == "EventA":
            three_days_ago = (
                clock.now(db) - timedelta(days=3)).isoformat()
            db.execute(
                "INSERT INTO fetch_runs(source_id, result,"
                " fetcher_kind, started_at) VALUES(?, 'OK',"
                " 'DOUJIN_EVENT', ?)",
                (int(cur.lastrowid), three_days_ago))
            db.commit()
    text = digest.build_health_digest(db)
    assert "EventB" in text and "harvest異常" in text
    assert "EventA の収穫期間中" not in text


# E2a: a repress announcement from Blip Blop registers a signal.
def test_blipblop_repress_announcement_wired(db):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES"
        " ('blipblop', 'community', 'approved', 'https://blipblop.net/feed/')")
    db.commit()
    f = BlipBlopFetcher(db, int(cur.lastrowid))
    out = f.register_candidates([{
        "title": "幻の名盤 再プレス決定", "url": "https://bb.jp/1",
        "summary": "長らく廃盤だった盤がリイシュー", "published": ""}])
    assert out["created"] == 1
    stage = db.execute(
        "SELECT event_stage FROM announcements").fetchone()[0]
    assert stage == "repress_announced"
    sig = db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='announcement'").fetchone()[0]
    assert sig == 1


# E2b: a revived listing feeds the variant_stock system.
def test_ingest_revival_feeds_variant_stock(db):
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
            "route": "retail", "description": ""}
    f.register_ingested("Shop", [item])
    db.execute("UPDATE listings SET status='ended'")
    db.commit()
    f.register_ingested("Shop", [item])
    sig = db.execute(
        "SELECT COUNT(*) FROM repress_signals WHERE"
        " system='variant_stock' AND edition_id='AR-1'"
    ).fetchone()[0]
    assert sig == 1


# E7 (already fixed by the wholesaler rewrite; pinned here): the
# arrival check matches schedule ids exactly — 1 is not 12.
def test_arrival_check_exact_schedule_ids(db):
    past = (clock.now(db) - timedelta(days=1)).isoformat()
    ids = []
    for i in range(12):
        # W-check B4: set_schedule supersedes a live row for the
        # same (target, channel, kind) — 12 registrations on ONE
        # channel are now one slot (re-registering the same instant
        # returns the existing id). Distinct channels keep the 12
        # distinct rows this id-exactness test exists to pin.
        ids.append(deadlines_ops.set_schedule(
            db, edition_id="AR-1", channel=f"label{i}", deadline_raw="x",
            deadline_tz="Asia/Tokyo", deadline_jst=past,
            deadline_confidence="confirmed",
            deadline_kind="hard_cutoff"))
    assert ids[0] == 1 and ids[-1] == 12
    results = deadlines_ops.arrival_check(db)
    assert {r["schedule_id"] for r in results} == set(ids)
