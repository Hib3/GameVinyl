"""Regression tests for the multi-perspective review of Phase 2/3
(2026-11-08, wholesaler persona — deadlines & repress)."""
from datetime import timedelta

from conftest import advance
from grt import clock, deadlines_ops, repress


def _mk(db, **kw):
    vals = {"edition_id": "AR-1", "deadline_raw": "raw",
            "deadline_tz": "Asia/Tokyo",
            "deadline_confidence": "confirmed",
            "deadline_kind": "hard_cutoff"}
    vals.update(kw)
    return deadlines_ops.set_schedule(db, **vals)


def _jst(days=0, hours=0):
    return (clock.now(db=None) + timedelta(days=days, hours=hours)
            ).isoformat()


# W1: deadline_jst is CONVERTED via zoneinfo, not stored verbatim.
def test_deadline_conversion_from_foreign_tz(db):
    rid = deadlines_ops.set_schedule(
        db, edition_id="AR-1", deadline_raw="Dec 1, 2026 11:59 PM EST",
        deadline_tz="America/New_York",
        deadline_jst="2026-12-01T23:59:00",  # naive local wall time
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    row = db.execute("SELECT deadline_jst FROM release_schedule"
                     " WHERE id=?", (rid,)).fetchone()
    # EST (UTC-5) 23:59 -> JST next day 13:59.
    assert row[0].startswith("2026-12-02T13:59")


# W2: kind table — campaign_end / unknown never fire instant alerts;
# guarantee_cutoff never instant either, but appears in digest lines.
def test_deadline_kind_table(db):
    _mk(db, deadline_kind="campaign_end", deadline_jst=_jst(hours=10))
    _mk(db, deadline_kind="unknown", deadline_jst=_jst(hours=10))
    _mk(db, deadline_kind="guarantee_cutoff",
        deadline_jst=_jst(hours=10))
    assert deadlines_ops.staged_alerts(db) == 0
    lines = deadlines_ops.deadline_digest_lines(db)
    assert any("guarantee_cutoff" in x for x in lines)
    assert any("campaign_end" in x for x in lines)
    assert not any("unknown" in x for x in lines)
    summary = deadlines_ops.unconfirmed_summary(db)
    assert summary.get("kind_unknown") == 1


# W3: stages are 168/72/24; only the LATEST due stage fires.
def test_deadline_stages_latest_only(db):
    _mk(db, deadline_jst=_jst(hours=20))  # inside 24h stage only
    assert deadlines_ops.staged_alerts(db) == 1
    assert deadlines_ops.staged_alerts(db) == 0  # idempotent
    keys = [r[0] for r in db.execute(
        "SELECT idempotency_key FROM notification_outbox WHERE"
        " event_type='deadline_alert'").fetchall()]
    assert keys and keys[0].endswith("|24h")


def test_deadline_four_days_out_uses_digest_stage(db):
    _mk(db, deadline_jst=_jst(days=4))  # 7d stage is digest for hard
    assert deadlines_ops.staged_alerts(db) == 0
    assert deadlines_ops.deadline_digest_lines(db) != []


# W4: repress notification does not re-fire on later days.
def test_repress_no_daily_refire(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.commit()
    repress.register_signal(db, system="announcement",
                            edition_id="AR-1")
    repress.register_signal(db, system="variant_stock",
                            edition_id="AR-1")
    out = repress.evaluate(db, edition_id="AR-1", title="T")
    assert out["notified"] is True
    advance(3 * 24 * 3600)
    out = repress.evaluate(db, edition_id="AR-1", title="T")
    assert out["suspected"] is True and out["notified"] is False
    rows = db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='repress_suspected'").fetchone()[0]
    assert rows == 1


# W6: the 30-day window is a real 30 days (30d7h must NOT combine).
def test_repress_window_is_real_30_days(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.commit()
    t0 = clock.now(db)
    repress.register_signal(
        db, system="announcement", edition_id="AR-1",
        at=t0.isoformat())
    repress.register_signal(
        db, system="variant_stock", edition_id="AR-1",
        at=(t0 + timedelta(days=30, hours=7)).isoformat())
    advance(30 * 24 * 3600 + 7 * 3600)
    out = repress.evaluate(db, edition_id="AR-1", title="T")
    assert out["suspected"] is False


# W8: same system + same day dedupes for real.
def test_repress_same_day_dedupe(db):
    assert repress.register_signal(db, system="announcement",
                                   edition_id="AR-1") is True
    assert repress.register_signal(db, system="announcement",
                                   edition_id="AR-1") is False
    rows = db.execute("SELECT COUNT(*) FROM repress_signals"
                      ).fetchone()[0]
    assert rows == 1


# W7: arrival vocabulary + one recheck at +7d for not_found.
def test_arrival_check_recheck_cycle(db):
    rid = _mk(db, deadline_jst=_jst(hours=-2))
    out = deadlines_ops.arrival_check(db)
    assert out and out[0]["outcome"] == "not_found"
    assert deadlines_ops.arrival_check(db) == []  # no early recheck
    # The release surfaces three days later.
    db.execute(
        "INSERT INTO listings(edition_id, source, url, raw_title,"
        " first_seen) VALUES('AR-1', 'shop', 'https://x.jp/1', 'T',"
        " ?)", ((clock.now(db) + timedelta(days=3)).isoformat(),))
    db.commit()
    advance(7 * 24 * 3600)
    out = deadlines_ops.arrival_check(db)
    assert out and out[0]["outcome"] == "confirmed_arrived"
    assert out[0]["recheck"] is True
    assert deadlines_ops.arrival_check(db) == []  # only ONE recheck


def test_arrival_check_ignores_suspended_ebay_row(db):
    _mk(db, deadline_jst=_jst(hours=-2))
    db.execute(
        "INSERT INTO listings(edition_id, source, url, first_seen)"
        " VALUES('AR-1','eBay Browse used','https://ebay.example/item',?)",
        (_jst(),))
    db.commit()
    out = deadlines_ops.arrival_check(db)
    assert out and out[0]["outcome"] == "not_found"


def test_arrival_check_candidate_only_is_unknown(db):
    deadlines_ops.set_schedule(
        db, candidate_id=7, deadline_raw="raw",
        deadline_tz="Asia/Tokyo", deadline_jst=_jst(hours=-1),
        deadline_confidence="confirmed", deadline_kind="hard_cutoff")
    out = deadlines_ops.arrival_check(db)
    assert out and out[0]["outcome"] == "unknown"
