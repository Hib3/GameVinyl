"""Frequency policy (agreement-16) + ingest conversion (agreement-12).

One ladder for every source's fetch cadence, in minutes:
15 -> 60 -> 360 -> 1440. Movement is automatic and always declared:

- 2 consecutive failed runs  -> demote one tier (slower), recorded as
  a system_event and reported in the next health digest;
- 2 consecutive successful runs -> promote one tier, up to the
  source's CEILING (never faster than the ceiling).

Each source has a HOME tier (its starting cadence) derived from its
kind: ingest shops and doujin consignment start at 60, HTML store
watches and web-page sources at 360. The CEILING is 15 for sources
passing every fast-tier gate, 60 for any approved source, else the
home tier.

Review fix (2026-11-08, AI-engineer persona): the first version's
ladder was record-only — no job ever read tier_min, so cadences
never changed in practice — and promotion stopped at a hard-coded
home of 360 for every source, making the 15/60-minute tiers
unreachable. Now the scheduler jobs consult is_due() (last run +
tier) on a 15-minute tick, and promotion can climb to the ceiling.

The 15-minute tier is gated (Phase 2 acceptance #2): a source may
sit there only while ALL of these hold — status='approved',
has_ingest_list='yes', no BLOCKED/HTTP_ERROR run in the last 7 days,
health not SUSPECT — and at most MAX_FAST_TIER sources may hold it.

Ingest conversion (agreement-12): a shop whose listing pages can be
taken whole converts from per-query fetching to fetch_mode='ingest'
when it is approved and has an ingest list; the conversion is
recorded on sources + watch_rules so the two modes stay
distinguishable (Phase 2 acceptance #1).
"""
from __future__ import annotations

from datetime import timedelta

from . import clock

TIERS_MIN = [15, 60, 360, 1440]
MAX_FAST_TIER = 5
SOURCE_MIN_TIER = {
    "https://blackscreenrecords.com/collections/game-soundtracks.atom": 360,
    "https://mondoshop.com/collections/soundtracks.atom": 1440,
    "https://data-discs.com/collections/records.atom": 1440,
    "https://gamemusicrecords.com/?post_type=product&feed=rss2": 360,
}


def source_min_tier(db, source_id: int) -> int:
    row = db.execute("SELECT url FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    return SOURCE_MIN_TIER.get(row[0], 15) if row else 15


def home_tier(db, source_id: int) -> int:
    """Starting cadence from the source's kind (spec C-4.4)."""
    src = db.execute("SELECT kind, fetch_mode FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    if src is None:
        return 360
    if src["fetch_mode"] == "ingest" or src["kind"] == "EVENT":
        return max(60, source_min_tier(db, source_id))
    return max(360, source_min_tier(db, source_id))


def ceiling_tier(db, source_id: int) -> int:
    """Fastest tier this source may be promoted to."""
    eligible, _ = fast_tier_eligible(db, source_id)
    if eligible:
        return max(15, source_min_tier(db, source_id))
    src = db.execute("SELECT status FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    if src is not None and src["status"] == "approved":
        return max(60, source_min_tier(db, source_id))
    return home_tier(db, source_id)


def _state(db, source_id: int) -> dict:
    row = db.execute("SELECT * FROM freq_state WHERE source_id=?",
                     (source_id,)).fetchone()
    if row is None:
        home = home_tier(db, source_id)
        db.execute(
            "INSERT INTO freq_state(source_id, tier_min, consec_fail,"
            " consec_ok, demoted, updated_at) VALUES(?,?,?,?,0,?)",
            (source_id, home, 0, 0, clock.now_iso(db)))
        db.commit()
        return {"source_id": source_id, "tier_min": home,
                "consec_fail": 0, "consec_ok": 0, "demoted": 0}
    return dict(row)


def current_tier(db, source_id: int) -> int:
    return _state(db, source_id)["tier_min"]


def is_due(db, source_id: int) -> bool:
    """True when the source's tier interval has elapsed since its
    last recorded run (or it has never run). This is what makes the
    ladder real: the scheduler jobs consult it on every tick."""
    from datetime import datetime
    row = db.execute(
        "SELECT MAX(started_at) FROM fetch_runs WHERE source_id=?",
        (source_id,)).fetchone()
    last = row[0] if row else None
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=clock.JST)
    tier = max(current_tier(db, source_id),
               source_min_tier(db, source_id))
    return (clock.now(db) - last_dt).total_seconds() >= tier * 60


def _tier_index(tier_min: int) -> int:
    for i, t in enumerate(TIERS_MIN):
        if tier_min <= t:
            return i
    return len(TIERS_MIN) - 1


def fast_tier_eligible(db, source_id: int) -> tuple[bool, list[str]]:
    """All agreement-16 conditions for the 15-minute tier."""
    src = db.execute("SELECT * FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    if src is None:
        return False, ["no such source"]
    reasons: list[str] = []
    if src["status"] != "approved":
        reasons.append("not approved")
    if (src["has_ingest_list"] or "") != "yes":
        reasons.append("no ingest list")
    since = (clock.now(db) - timedelta(days=7)).isoformat()
    bad = db.execute(
        "SELECT COUNT(*) FROM fetch_runs WHERE source_id=? AND"
        " started_at>=? AND result IN ('BLOCKED','HTTP_ERROR')",
        (source_id, since)).fetchone()[0]
    if bad:
        reasons.append(f"{bad} blocked/error runs in 7d")
    health = db.execute(
        "SELECT state FROM source_health WHERE source_id=?",
        (source_id,)).fetchone()
    if health is not None and health[0] == "SUSPECT":
        reasons.append("health SUSPECT")
    holders = db.execute(
        "SELECT COUNT(*) FROM freq_state WHERE tier_min<=15 AND demoted=0"
        " AND source_id!=?", (source_id,)).fetchone()[0]
    if holders >= MAX_FAST_TIER:
        reasons.append("fast tier full (5)")
    return (not reasons), reasons


def apply_run_outcome(db, source_id: int, ok: bool) -> dict:
    """Fold one fetch outcome into the ladder. Returns the new state.

    Demotion/promotion events are written to system_events with kind
    'freq_demotion' / 'freq_promotion' so the health digest can
    declare them (agreement-16; Phase 2 acceptance #3).

    W-check B2 (wholesaler D-7): the 15-minute tier cap is a hard
    spec limit, but the eligibility count and the tier write were a
    non-atomic read-modify-write — two sources promoting in parallel
    could both pass the count and land 6 holders in a 5-slot tier.
    When this connection is not already inside a transaction, the
    whole update runs under BEGIN IMMEDIATE so the count and the
    write are one atomic unit.
    """
    if not db.in_transaction:
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                return _apply_run_outcome_inner(db, source_id, ok)
            except Exception:
                db.rollback()
                raise
        except Exception:
            # If the explicit transaction cannot be taken (a caller
            # managing its own), fall back to the plain path rather
            # than losing the outcome entirely.
            if db.in_transaction:
                return _apply_run_outcome_inner(db, source_id, ok)
            raise
    return _apply_run_outcome_inner(db, source_id, ok)


def _apply_run_outcome_inner(db, source_id: int, ok: bool) -> dict:
    import json
    st = _state(db, source_id)
    tier = st["tier_min"]
    cf = st["consec_fail"] + 1 if not ok else 0
    co = st["consec_ok"] + 1 if ok else 0
    demoted = st["demoted"]
    event = None
    idx = _tier_index(tier)
    if not ok and cf >= 2 and idx < len(TIERS_MIN) - 1:
        tier = TIERS_MIN[idx + 1]
        cf = 0
        event = "freq_demotion"
    elif ok and co >= 2 and idx > 0:
        # Promotion climbs toward the source's CEILING, not just
        # back to home: an eligible ingest shop can reach 15, an
        # approved source 60 (review fix — the old rule required
        # the demoted flag and capped recovery at 360, so the fast
        # tiers were unreachable by construction).
        ceiling = ceiling_tier(db, source_id)
        if tier > ceiling:
            tier = TIERS_MIN[idx - 1]
            co = 0
            event = "freq_promotion"
    # The fast tier is re-validated on every outcome: a source that
    # stops qualifying is moved to 60 immediately.
    if tier <= 15:
        eligible, _reasons = fast_tier_eligible(db, source_id)
        if not eligible:
            tier = 60
            event = event or "freq_demotion"
    if tier < source_min_tier(db, source_id):
        tier = source_min_tier(db, source_id)
        event = event or "freq_demotion"
    # W-check B2 (wholesaler D-10): consecutive counters used to BANK
    # while a move was impossible — a source pinned at its ceiling
    # accumulated consec_ok indefinitely, so once it later gained a
    # faster ceiling, ONE success promoted it ('2 consecutive' was
    # spent from the bank). When no move happened, cap the counter
    # at 1: the next move always needs its own fresh pair.
    if event is None:
        if ok:
            co = min(co, 1)
        else:
            cf = min(cf, 1)
    demoted = 1 if tier > home_tier(db, source_id) else 0
    db.execute(
        "UPDATE freq_state SET tier_min=?, consec_fail=?, consec_ok=?,"
        " demoted=?, updated_at=? WHERE source_id=?",
        (tier, cf, co, demoted, clock.now_iso(db), source_id))
    # Silence detection (health.evaluate_time_based_health) reads
    # sources.expected_cadence_days — a column NOTHING used to write,
    # so the whole silence layer was dormant unless a human hand-set
    # values (wholesaler D-4a). The ladder tier IS the source's
    # expected cadence; keep the column in step with it.
    db.execute(
        "UPDATE sources SET expected_cadence_days=? WHERE id=?",
        (tier / 1440.0, source_id))
    if event:
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES(?, ?, ?)",
            (event, json.dumps({"source_id": source_id,
                                "tier_min": tier}), clock.now_iso(db)))
    db.commit()
    return {"source_id": source_id, "tier_min": tier,
            "consec_fail": cf, "consec_ok": co, "demoted": demoted,
            "event": event}


def convert_to_ingest(db, source_id: int) -> dict:
    """Agreement-12 conversion for one shop (Phase 2 acceptance #1)."""
    src = db.execute("SELECT * FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    if src is None:
        return {"converted": False, "reason": "no such source"}
    if src["status"] != "approved":
        return {"converted": False, "reason": "not approved"}
    if (src["has_ingest_list"] or "") != "yes":
        return {"converted": False, "reason": "no ingest list"}
    db.execute("UPDATE sources SET fetch_mode='ingest' WHERE id=?",
               (source_id,))
    rule = db.execute(
        "SELECT id FROM watch_rules WHERE source=? AND fetch_mode='ingest'",
        (src["name"],)).fetchone()
    if rule is None:
        st = _state(db, source_id)
        db.execute(
            "INSERT INTO watch_rules(source, query, fetch_mode,"
            " interval_min, enabled) VALUES(?, NULL, 'ingest', ?, 1)",
            (src["name"], st["tier_min"]))
    db.commit()
    return {"converted": True, "source": src["name"], "mode": "ingest"}


def pending_declarations(db, since_iso: str) -> list[dict]:
    """Demotion/promotion events since a point, for the digest."""
    import json
    rows = db.execute(
        "SELECT detail_json, created_at FROM system_events WHERE"
        " kind IN ('freq_demotion','freq_promotion') AND created_at>=?"
        " ORDER BY created_at", (since_iso,)).fetchall()
    out = []
    for detail, at in rows:
        try:
            d = json.loads(detail or "{}")
        except Exception:
            d = {}
        d["at"] = at
        out.append(d)
    return out
