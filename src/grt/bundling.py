"""Same-physical-copy bundling (agreement-27).

Marketplaces relist the SAME physical copy across shops/agents of
one seller group. Price statistics must count such a group as ONE
copy, and grouping must be conservative.

Review fixes (2026-11-08, used-seller persona — the first version
grouped by the raw source string, merged distinct simultaneous
copies at one shop, could never add a later relisting to an
existing group, and re-queued the same NULL-cond set every day):

- the grouping key uses the CONFIRMED seller group from the
  seller_groups table (falling back to the source name itself);
- two listings from the SAME source are never bundled: a shop does
  not list one copy twice at once — simultaneous same-source
  listings are distinct copies by construction. A bundle forms
  only across DIFFERENT sources of the same seller group;
- a later relisting from a source not yet represented in an
  existing group JOINS that group when cond and spread allow;
- a NULL-cond member is never auto-grouped (acceptance #8) and
  its review-queue entry is deduplicated on the exact listing-id
  set — but (W-check B3) it no longer freezes the known-cond
  subsets of the same set;
- prices must sit within 10% of the group minimum — at formation
  AND on every later scan: a group whose members drift apart in
  price or cond is dissolved back to individual copies and queued
  for review (W-check B3);
- a group is formed under ONE cond_rank, recorded on the group
  row; join checks run against the group's actual members
  (W-check B3).
"""
from __future__ import annotations

import json

from . import clock

MAX_PRICE_SPREAD = 0.10


def _spread_ok(prices: list[float]) -> bool:
    vals = [p for p in prices if p is not None]
    if len(vals) < 2:
        return True
    lo = min(vals)
    if lo <= 0:
        return False
    return (max(vals) - lo) / lo <= MAX_PRICE_SPREAD


def _seller_group(db, source: str) -> str:
    row = db.execute(
        "SELECT seller_group FROM seller_groups WHERE source=?",
        (source,)).fetchone()
    return row[0] if row and row[0] else source


def _price(row) -> float | None:
    """The yen-comparable price (W-check B3): a foreign amount is
    not a yen price and never enters a spread computation."""
    from .scoring import yen_value
    return yen_value(row["total_jpy"], row["price"], row["currency"])


def _find_group(db, edition_id, variant_id, group, cond):
    """The existing copy group for one (edition, variant, seller
    group, COND) set.

    W-check B3 (seller D1 / adversarial 5): the lookup used to
    ignore cond_rank entirely (the groups table had no cond
    column), and join checks were computed from whichever cond
    bucket happened to be processed — never from the group's
    actual members. Groups are now formed under one cond_rank,
    recorded on the group row. A legacy group with no recorded
    cond is adopted only when every member unanimously carries
    this bucket's cond.
    """
    row = db.execute(
        "SELECT id FROM listing_copy_groups WHERE edition_id=? AND"
        " variant_id IS ? AND seller_group=? AND cond_rank IS ?"
        " LIMIT 1", (edition_id, variant_id, group, cond)).fetchone()
    if row:
        return row[0]
    legacy = db.execute(
        "SELECT id FROM listing_copy_groups WHERE edition_id=? AND"
        " variant_id IS ? AND seller_group=? AND cond_rank IS NULL"
        " LIMIT 1", (edition_id, variant_id, group)).fetchone()
    if legacy:
        conds = {r[0] for r in db.execute(
            "SELECT DISTINCT cond_rank FROM listings"
            " WHERE physical_copy_id=?"
            " AND COALESCE(source,'')!='eBay Browse used'",
            (legacy[0],)).fetchall()}
        if conds == {cond}:
            db.execute(
                "UPDATE listing_copy_groups SET cond_rank=?"
                " WHERE id=?", (cond, legacy[0]))
            db.commit()
            return legacy[0]
    return None


def _bundle_bucket(db, edition_id, variant_id, group, members):
    """Group/join for one (edition, variant, group, cond) set.

    Returns (groups_created, listings_joined). All members share
    one cond_rank by construction (the caller splits by cond)."""
    cond = members[0]["cond_rank"] if members else None
    gid = _find_group(db, edition_id, variant_id, group, cond)
    ungrouped = [m for m in members if not m["physical_copy_id"]]
    if gid is not None:
        # Join checks run against the group's ACTUAL members —
        # every source already represented, every price already in
        # the group — not just this bucket's slice of them.
        actual = db.execute(
            "SELECT * FROM listings WHERE physical_copy_id=?"
            " AND COALESCE(source,'')!='eBay Browse used'",
            (gid,)).fetchall()
        represented = {m["source"] for m in actual}
        group_prices = [_price(m) for m in actual]
        joined = 0
        for m in ungrouped:
            if m["source"] in represented:
                continue  # same source = a different copy
            if _price(m) is None and any(
                    p is not None for p in group_prices):
                continue  # spread cannot be verified for this copy
            if not _spread_ok(group_prices + [_price(m)]):
                continue
            db.execute(
                "INSERT OR IGNORE INTO listing_copy_members"
                "(group_id, listing_id) VALUES(?,?)", (gid, m["id"]))
            db.execute(
                "UPDATE listings SET physical_copy_id=? WHERE id=?",
                (gid, m["id"]))
            represented.add(m["source"])
            group_prices.append(_price(m))
            joined += 1
        db.commit()
        return 0, joined
    by_source: dict[str, list] = {}
    for m in ungrouped:
        by_source.setdefault(m["source"], []).append(m)
    if len(by_source) < 2:
        return 0, 0  # one source only: distinct copies, no bundle
    chosen = [v[0] for v in by_source.values()]
    if not _spread_ok([_price(m) for m in chosen]):
        return 0, 0
    cur = db.execute(
        "INSERT INTO listing_copy_groups(edition_id, variant_id,"
        " seller_group, first_seen, cond_rank) VALUES(?,?,?,?,?)",
        (edition_id, variant_id, group, clock.now_iso(db), cond))
    gid = int(cur.lastrowid)
    for m in chosen:
        db.execute(
            "INSERT OR IGNORE INTO listing_copy_members(group_id,"
            " listing_id) VALUES(?,?)", (gid, m["id"]))
        db.execute(
            "UPDATE listings SET physical_copy_id=? WHERE id=?",
            (gid, m["id"]))
    db.commit()
    return 1, 0


def _revalidate_groups(db) -> int:
    """Dissolve groups whose premises no longer hold (W-check B3,
    seller D8): membership was verified once at formation, but a
    seller re-pricing a copy (or a condition re-grade) silently
    voided the 10% rule and the single-cond rule while statistics
    kept counting the group as one copy. A group whose live
    members now span two cond_ranks, or whose yen prices spread
    beyond 10%, is dissolved back to individual copies and queued
    for human review; the dissolution is recorded as an event.
    Returns the number dissolved."""
    dissolved = 0
    groups = db.execute(
        "SELECT id, cond_rank FROM listing_copy_groups").fetchall()
    for gid, gcond in groups:
        members = db.execute(
            "SELECT * FROM listings WHERE physical_copy_id=?"
            " AND COALESCE(source,'')!='eBay Browse used'"
            " AND status NOT IN ('ended')", (gid,)).fetchall()
        if len(members) < 2:
            continue
        conds = {m["cond_rank"] for m in members
                 if m["cond_rank"] is not None}
        drift = len(conds) > 1 or (
            gcond is not None and conds and conds != {gcond})
        if not drift:
            drift = not _spread_ok([_price(m) for m in members])
        if not drift:
            continue
        member_ids = [m["id"] for m in members]
        db.execute(
            "UPDATE listings SET physical_copy_id=NULL"
            " WHERE physical_copy_id=?", (gid,))
        db.execute(
            "DELETE FROM listing_copy_members WHERE group_id=?",
            (gid,))
        db.execute(
            "DELETE FROM listing_copy_groups WHERE id=?", (gid,))
        payload = json.dumps(
            {"group_id": gid, "listing_ids": member_ids,
             "reason": "group_drift"}, sort_keys=True)
        dup = db.execute(
            "SELECT 1 FROM review_queue WHERE"
            " kind='copy_bundle_review' AND payload_json=?"
            " AND resolved_at IS NULL", (payload,)).fetchone()
        if not dup:
            # W-check B4 (maniac B): the dissolution has ALREADY
            # happened when this row is written — calling it a
            # pending 'review' item was false (nothing the user
            # could approve or reject would undo it) and it piled
            # into the unresolvable backlog forever. The row is a
            # NOTICE: inserted already-resolved, so it stays
            # auditable without masquerading as a decision.
            db.execute(
                "INSERT INTO review_queue(kind, payload_json,"
                " created_at, resolved_at, resolution)"
                " VALUES('copy_bundle_review', ?, ?, ?,"
                " 'dissolved_notice')",
                (payload, clock.now_iso(db), clock.now_iso(db)))
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('bundle_dissolved', ?, ?)",
            (json.dumps({"group_id": gid,
                         "listing_ids": member_ids}),
             clock.now_iso(db)))
        db.commit()
        dissolved += 1
    return dissolved


def propose_bundles(db) -> dict:
    """Scan listings and apply the bundling rules. Returns a summary."""
    dissolved = _revalidate_groups(db)
    rows = db.execute(
        "SELECT * FROM listings"
        " WHERE COALESCE(source,'')!='eBay Browse used'"
        " AND status NOT IN ('ended')"
        " AND edition_id IS NOT NULL ORDER BY edition_id, variant_id,"
        " source").fetchall()
    buckets: dict[tuple, list] = {}
    for r in rows:
        key = (r["edition_id"], r["variant_id"],
               _seller_group(db, r["source"]))
        buckets.setdefault(key, []).append(r)
    grouped = queued = joined = 0
    for (edition_id, variant_id, group), members in buckets.items():
        # W-check B3 (seller D7): the NULL-cond review decision used
        # to be taken over the WHOLE (edition, variant, group) set
        # before the cond split, so one cond-less listing froze the
        # bundling of every clean known-cond pair in the same set
        # forever. The cond split comes first now: known-cond
        # subsets bundle normally; only the NULL-cond members
        # themselves are queued (acceptance #8 protects THEM from
        # automatic grouping, not their neighbours).
        by_cond: dict[object, list] = {}
        for m in members:
            by_cond.setdefault(m["cond_rank"], []).append(m)
        null_members = by_cond.pop(None, [])
        if null_members and len(members) >= 2:
            payload = json.dumps(
                {"edition_id": edition_id, "variant_id": variant_id,
                 "seller_group": group,
                 "listing_ids": [m["id"] for m in null_members],
                 "reason": "cond_rank_unknown"}, sort_keys=True)
            dup = db.execute(
                "SELECT 1 FROM review_queue WHERE"
                " kind='copy_bundle_review' AND payload_json=?",
                (payload,)).fetchone()
            # W-check B4 (maniac B): the dedupe used to consider
            # only UNRESOLVED rows, so an approved (or rejected)
            # item was re-queued as a fresh pending row on the next
            # propose run — an endless resolve→requeue loop. Any
            # prior row for the identical payload settles it: an
            # approval is consumed by the bot at resolve time (the
            # bundling itself happens there), a rejection stands.
            if not dup:
                db.execute(
                    "INSERT INTO review_queue(kind, payload_json,"
                    " created_at) VALUES('copy_bundle_review', ?, ?)",
                    (payload, clock.now_iso(db)))
                db.commit()
                queued += 1
        for cond_members in by_cond.values():
            g, j = _bundle_bucket(db, edition_id, variant_id, group,
                                  cond_members)
            grouped += g
            joined += j
    return {"grouped": grouped, "queued_for_review": queued,
            "joined": joined, "dissolved": dissolved}
def apply_approved_bundle(db, payload: dict) -> tuple[int, int]:
    """Consume a human approval of a cond_rank_unknown proposal
    (W-check B4): bundle the payload's listings as one bucket.
    The spread/source rules inside _bundle_bucket still apply —
    approval removes the cond-unknown bar, not the other guards.
    Returns (groups_created, listings_joined)."""
    ids = payload.get("listing_ids") or []
    members = []
    for lid in ids:
        row = db.execute(
            "SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        if row is not None and row["source"] != "eBay Browse used" \
                and row["status"] != "ended":
            members.append(row)
    if len(members) < 2:
        return 0, 0
    return _bundle_bucket(db, payload.get("edition_id"),
                          payload.get("variant_id"),
                          payload.get("seller_group"), members)


def count_physical_copies(db, edition_id: str) -> int:
    """Copies for statistics: a bundle counts once (acceptance #8)."""
    rows = db.execute(
        "SELECT id, physical_copy_id FROM listings WHERE edition_id=?"
        " AND COALESCE(source,'')!='eBay Browse used'"
        " AND status NOT IN ('ended')", (edition_id,)).fetchall()
    seen_groups = set()
    count = 0
    for r in rows:
        gid = r["physical_copy_id"]
        if gid:
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
        count += 1
    return count
