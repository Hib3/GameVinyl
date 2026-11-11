"""Phase 2 core acceptance tests (prompt v2 section 7, Phase 2).

#1 ingest conversion recorded + distinguishable; #2 fast-tier gate;
#3 auto-demotion declared; #4 REPRESS two-system rule; #5/#6
deadline TZ discipline + confirmed-only alerts; #7 parent-product
veto; #8 bundling rules; #12 bucket-safe price drops. (#9/#10/#11
live in variant_sweep/digest/bot tests below and in test_v20_bot.)
"""
from grt import (bundling, deadlines_ops, freqpolicy, matcher,
                 pricedrop, repress)


def _source(db, name="ShopA", **kw):
    vals = {"status": "approved", "has_ingest_list": "yes",
            "kind": "STORE", "url": "https://example.jp/list"}
    vals.update(kw)
    cur = db.execute(
        "INSERT INTO sources(name, status, has_ingest_list, kind, url)"
        " VALUES(?,?,?,?,?)",
        (name, vals["status"], vals["has_ingest_list"], vals["kind"],
         vals["url"]))
    db.commit()
    return int(cur.lastrowid)


# #1 ingest conversion
def test_ingest_conversion_recorded_and_distinguishable(db):
    sid = _source(db)
    out = freqpolicy.convert_to_ingest(db, sid)
    assert out["converted"] is True
    row = db.execute("SELECT fetch_mode FROM sources WHERE id=?",
                     (sid,)).fetchone()
    assert row[0] == "ingest"
    rule = db.execute(
        "SELECT fetch_mode FROM watch_rules WHERE source='ShopA'"
    ).fetchone()
    assert rule[0] == "ingest"
    sid2 = _source(db, "ShopB", status="candidate")
    assert freqpolicy.convert_to_ingest(db, sid2)["converted"] is False


# #2 fast-tier gate: all conditions + cap
def test_fast_tier_eligibility(db):
    sid = _source(db)
    ok, reasons = freqpolicy.fast_tier_eligible(db, sid)
    assert ok, reasons
    sid2 = _source(db, "ShopB", has_ingest_list="no")
    ok2, reasons2 = freqpolicy.fast_tier_eligible(db, sid2)
    assert not ok2 and "no ingest list" in reasons2
    db.execute(
        "INSERT INTO fetch_runs(source_id, result, started_at)"
        " VALUES(?, 'BLOCKED', datetime('now'))", (sid,))
    db.commit()
    ok3, _ = freqpolicy.fast_tier_eligible(db, sid)
    assert not ok3


# #3 two consecutive failures demote, declared as system_event
def test_auto_demotion_and_promotion(db):
    sid = _source(db)
    freqpolicy.apply_run_outcome(db, sid, ok=True)   # baseline
    freqpolicy.apply_run_outcome(db, sid, ok=False)
    st = freqpolicy.apply_run_outcome(db, sid, ok=False)
    assert st["event"] == "freq_demotion"
    assert st["tier_min"] > 360
    ev = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE kind='freq_demotion'"
    ).fetchone()[0]
    assert ev == 1


# #4 REPRESS: one system never fires; two systems within 30d fire
def test_repress_two_system_rule(db):
    # Review fix (wholesaler D5): the counted systems are the spec's
    # three (variant_stock / announcement / discogs_new_release).
    # A Discogs marketplace 0->1 ('discogs_stats') is NOT a repress
    # system and can never contribute to the threshold.
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.commit()
    assert repress.register_signal(db, system="discogs_stats",
                                   edition_id="AR-1")
    out = repress.evaluate(db, edition_id="AR-1", title="T")
    assert out["suspected"] is False
    repress.register_signal(db, system="announcement",
                            edition_id="AR-1")
    out = repress.evaluate(db, edition_id="AR-1", title="T")
    assert out["suspected"] is False  # only ONE counted system
    repress.register_signal(db, system="variant_stock",
                            edition_id="AR-1")
    out = repress.evaluate(db, edition_id="AR-1", title="T")
    assert out["suspected"] is True
    assert out["notified"] is True
    rows = db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='repress_suspected'").fetchone()[0]
    assert rows == 1
    notes = repress.digest_notes(db)
    assert notes == []  # two-system editions leave the digest notes


# #5/#6 deadline discipline
def test_deadline_tz_discipline_and_confirmed_only(db):
    # W-check B5 R24 fixture update (wholesaler 軽微-C): the
    # original first case used an OFFSET-CARRYING value and
    # asserted it was discarded without a tz argument. That was
    # the trap R24 fixed — an offset-carrying value determines
    # its own instant. The discipline that remains (and is what
    # this test exists to pin) is the NAIVE case: a wall time
    # with no zone is not an instant. Both shapes are pinned now.
    rid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", deadline_raw="12/31 23:59",
        deadline_tz=None, deadline_jst="2026-12-31T23:59:00",
        deadline_confidence="tz_unknown")
    row = db.execute("SELECT deadline_jst FROM release_schedule"
                     " WHERE id=?", (rid,)).fetchone()
    assert row[0] is None  # naive + no TZ -> no JST, ever
    summary = deadlines_ops.unconfirmed_summary(db)
    assert summary.get("tz_unknown") == 1
    created = deadlines_ops.staged_alerts(db)
    assert created == 0  # unconfirmed rows never alert
    rid2 = deadlines_ops.set_schedule(
        db, edition_id="AR-1", channel="other",
        deadline_raw="12/31 23:59",
        deadline_tz=None, deadline_jst="2026-12-31T23:59:00+09:00",
        deadline_confidence="tz_unknown")
    row2 = db.execute("SELECT deadline_jst FROM release_schedule"
                      " WHERE id=?", (rid2,)).fetchone()
    assert row2[0] == "2026-12-31T23:59:00+09:00"  # offset converts


# #7 parent product veto
def test_parent_barcode_veto(db):
    db.execute(
        "INSERT INTO parent_products(parent_title, parent_barcode,"
        " edition_id) VALUES('Game Box', '4900000000001', 'AR-1')")
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.commit()
    results = matcher.match_listing(
        db, {"raw_title": "Game Box", "barcode": "4900000000001",
             "url": "https://example.jp/1"})
    assert results and results[0]["vetoes"] == ["parent_product"]
    prices = db.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE edition_id='AR-1'"
    ).fetchone()[0]
    assert prices == 0


# #8 bundling rules
def _listing(db, url, price, cond, source="shop"):
    cur = db.execute(
        "INSERT INTO listings(edition_id, variant_id, source, url,"
        " price, total_jpy, cond_rank, status, first_seen, last_seen)"
        " VALUES('AR-1', NULL, ?, ?, ?, ?, ?, 'active', '2026-11-08',"
        " '2026-11-08')", (source, url, price, price, cond))
    db.commit()
    return int(cur.lastrowid)


def test_bundling_groups_and_counts_once(db):
    # Review fix (seller D3): a bundle forms across DIFFERENT
    # sources of one confirmed seller group — never within a single
    # source, whose simultaneous listings are distinct copies.
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.execute(
        "INSERT INTO seller_groups(seller_group, source) VALUES"
        " ('G1', 'shopA'), ('G1', 'shopB')")
    db.commit()
    _listing(db, "https://x/1", 10000, 3, source="shopA")
    _listing(db, "https://x/2", 10500, 3, source="shopB")  # within 10%
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 1
    assert bundling.count_physical_copies(db, "AR-1") == 1


def test_bundling_null_cond_goes_to_review(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    _listing(db, "https://x/1", 10000, 3)
    _listing(db, "https://x/2", 10000, None)
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 0 and out["queued_for_review"] == 1


def test_bundling_wide_spread_not_grouped(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.execute(
        "INSERT INTO seller_groups(seller_group, source) VALUES"
        " ('G1', 'shopA'), ('G1', 'shopB')")
    db.commit()
    _listing(db, "https://x/1", 10000, 3, source="shopA")
    _listing(db, "https://x/2", 12000, 3, source="shopB")  # 20% spread
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 0


# #12 price drop only inside a bucket
def test_price_drop_bucket_safety(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    base_id = _listing(db, "https://x/1", 10000, 3)
    # W-check B3 fixture update: the baseline listing and both probe
    # listings carry price-高 decisions so the 9-C12 evidence gate
    # passes for all of them and the ONLY thing that can differ is
    # the bucket logic under test.
    for probe_id in (999, 998, base_id):
        db.execute(
            "INSERT INTO match_decisions(listing_id, edition_id,"
            " existence_confidence, price_confidence)"
            " VALUES(?, 'AR-1', '高', '高')", (probe_id,))
    db.commit()
    cheap_other_bucket = {"id": 999, "edition_id": "AR-1",
                          "variant_id": None, "cond_rank": 1,
                          "price": 5000.0, "total_jpy": 5000.0,
                          "currency": "JPY",
                          "url": "https://x/9"}
    out = pricedrop.on_listing_price(db, cheap_other_bucket)
    assert out["notified"] is False  # different bucket: no comparison
    same_bucket = {"id": 998, "edition_id": "AR-1", "variant_id": None,
                   "cond_rank": 3, "price": 8000.0, "total_jpy": 8000.0,
                   "currency": "JPY",
                   "url": "https://x/8"}
    out2 = pricedrop.on_listing_price(db, same_bucket)
    assert out2["notified"] is True
