"""Price-drop detection, bucket-safe (agreement-11, Phase 2 ops).

A drop is only ever judged INSIDE one bucket: same edition, same
variant, same condition rank (scoring.price_bucket_key — a listing
with cond_rank NULL has no bucket and can never drop-notify). The
qualifying rule itself is scoring.price_drop_qualifies_bucketed;
this module is its operational wrapper: it resolves the bucket's
previous best as the baseline, asks the rule, and enqueues at most
one notification per (bucket, price) pair (Phase 2 acceptance #12).
"""
from __future__ import annotations

from . import clock, outbox as outbox_mod
from .scoring import (price_bucket_key, price_drop_qualifies_bucketed,
                      yen_value)


def price_evidence_ok(db, listing_id, edition_id) -> bool:
    """9-C12 gate (W-check B3, AI-engineer 4): a listing's price may
    be USED as evidence — as a drop candidate or as a bucket
    baseline — only when the matcher's latest decision for that
    listing/edition pair carries price confidence '高'.

    The write-back comment in ingest.register_ingested always said
    price use "stays governed by price_confidence in
    match_decisions", but no consumer ever read it: an E1 judgement
    (existence 高, price 中 — variant not discriminated) wrote the
    edition_id back, and this module then compared that price
    against the bucket as if it were established evidence. That is
    exactly the variant price-mixing C-5.4 exists to prevent. A
    listing with NO decision row is not evidence either: every
    production listing is created through the matcher, so a missing
    decision means the price was never judged.
    """
    if listing_id is None or not edition_id:
        return False
    try:
        lid = int(listing_id)
    except (TypeError, ValueError):
        return False  # R14 (adversarial): a non-numeric id is no id
    row = db.execute(
        "SELECT price_confidence FROM match_decisions"
        " WHERE listing_id=? AND edition_id=? ORDER BY id DESC"
        " LIMIT 1", (lid, edition_id)).fetchone()
    return row is not None and row[0] == "高"


def _edition_title(db, edition_id) -> str:
    """Edition title for notification bodies (W-check B5,
    collector 中-3): a price-drop notice that names only the
    Edition ID forces a lookup before the user can judge it.
    Returns '' when the edition row is missing."""
    if not edition_id:
        return ""
    row = db.execute("SELECT title FROM editions WHERE id=?",
                     (edition_id,)).fetchone()
    if row is None or not row[0]:
        return ""
    from .normalize import one_line
    # W-check B5 R24 (collector 軽微-ア): the discovery digest
    # caps candidate titles at 120 chars; the titles B5 added to
    # notifications were uncapped, so one 5,000-char title (an
    # external feed value) bloated every alert body it touched.
    _t = one_line(row[0])
    return _t[:120] + "…" if len(_t) > 120 else _t


def _variant_ambiguous(db, edition_id, variant_id) -> bool:
    """True when a listing has no variant in a MULTI-variant edition
    (W-check B3, wholesaler 2): its bucket would silently merge the
    normal and limited variants' prices. Single-variant editions
    are unambiguous even when variant_id was not written back, and
    a known variant_id is always unambiguous."""
    if variant_id is not None:
        return False
    n = db.execute(
        "SELECT COUNT(*) FROM edition_variants WHERE edition_id=?",
        (edition_id,)).fetchone()[0]
    return n > 1


def _price_part(price) -> str:
    """Canonical price rendering for idempotency keys.

    R9 collector N-2: the key used to embed the price's Python repr,
    so int 8000 and float 8000.0 — the same price — produced
    different keys and a duplicate notification. All shipped
    producers feed DB REAL values (floats), so canonicalising via
    float() reproduces every existing key exactly while making
    int-typed inputs land on the same key.
    """
    return str(float(price))


def _episode_key(db, base: str) -> str:
    """Episode-aware idempotency key for a (bucket, price) pair.

    W-check B2 (maniac B D-9(i)): the key used to be exactly
    ``pricedrop|<bucket>|<price>`` forever, so a price that dropped
    to 8,000, recovered, and dropped to 8,000 again months later was
    suppressed as a 'duplicate' — the second episode (a fresh buying
    chance) was never notified. When the previous row for the pair
    has reached a terminal state (sent / dead_letter), a new episode
    suffix is minted; while a row is still in flight, the plain key
    is returned so the duplicate suppression still applies.
    """
    prefix = base + "|e"
    rows = db.execute(
        "SELECT id, state FROM notification_outbox"
        " WHERE idempotency_key=? OR substr(idempotency_key, 1, ?)=?"
        " ORDER BY id", (base, len(prefix), prefix)).fetchall()
    if not rows:
        return base
    if rows[-1][1] in ("pending", "failed_retryable", "sending"):
        return base
    return f"{base}|e{len(rows) + 1}"


def bucket_best(db, edition_id: str, variant_id, cond_rank,
                *, exclude_listing_id: int = 0) -> float | None:
    """The bucket's previous best YEN price, or None.

    W-check B3: rows contribute only their yen value (a foreign
    amount is never a yen baseline — collector D-5 / maniac A D-1),
    only positive values count (a negative or zero row used to
    poison the minimum and suppress every real drop forever —
    wholesaler 4/9, adversarial 1), and only listings whose price
    the matcher actually established (9-C12, price_evidence_ok)
    may serve as the comparison baseline.
    """
    rows = db.execute(
        "SELECT id, total_jpy, price, currency, last_seen"
        " FROM listings"
        " WHERE edition_id=? AND variant_id IS ? AND cond_rank=?"
        " AND id!=? AND status NOT IN ('ended', 'sold_candidate')",
        (edition_id, variant_id, cond_rank, exclude_listing_id)
    ).fetchall()
    # W-check B5 (seller): a listing whose source stopped being
    # observed (shop closed, watch disabled — the sources row
    # stays 'approved') never passes through an absence round, so
    # its price served as the bucket baseline FOREVER, capping
    # every real drop judgement. A baseline must be FRESH: rows
    # last seen over 30 days ago do not count (live sources
    # re-observe their listings every cycle, and the absence
    # lifecycle ends genuinely-gone listings within two rounds,
    # so 30 days only ever excludes the unobserved).
    from datetime import timedelta as _td
    fresh_cutoff = clock.now(db) - _td(days=30)
    vals = []
    for r in rows:
        try:
            seen = clock.parse_iso(r[4]) if r[4] else None
        except Exception:
            seen = None
        # W-check B5 R24 (seller F-3): a NULL or unparseable
        # last_seen fell through as FRESH — a baseline that can
        # never die. Unreadable freshness is not freshness (the
        # /coverage side already required a parseable value);
        # every current writer stamps clock.now_iso, so this
        # only ever excludes legacy/corrupt rows.
        if seen is None or seen < fresh_cutoff:
            continue
        v = yen_value(r[1], r[2], r[3])
        if v is None or v <= 0:
            continue
        if not price_evidence_ok(db, r[0], edition_id):
            continue
        vals.append(v)
    return min(vals) if vals else None


def on_listing_price(db, listing: dict) -> dict:
    """Evaluate one listing's current price against its bucket."""
    # W-check B3: the candidate price itself must be a YEN value
    # (a foreign amount labelled 円 fabricated drops like
    # "8,000円 → 30円" for a $30 listing) and established price
    # evidence (9-C12); a variant-less listing in a multi-variant
    # edition has no bucket, exactly like a cond-less one.
    price = yen_value(listing.get("total_jpy"), listing.get("price"),
                      listing.get("currency"))
    if not price or price <= 0 or not listing.get("edition_id"):
        return {"notified": False, "reason": "no price/edition"}
    if not price_evidence_ok(db, listing.get("id"),
                             listing["edition_id"]):
        return {"notified": False,
                "reason": "price confidence not high (9-C12)"}
    if _variant_ambiguous(db, listing["edition_id"],
                          listing.get("variant_id")):
        return {"notified": False,
                "reason": "no bucket (variant unknown)"}
    bucket = price_bucket_key(listing["edition_id"],
                              listing.get("variant_id"),
                              listing.get("cond_rank"))
    if bucket is None:
        return {"notified": False, "reason": "no bucket (cond unknown)"}
    prev = bucket_best(db, listing["edition_id"],
                       listing.get("variant_id"),
                       listing.get("cond_rank"),
                       exclude_listing_id=int(listing.get("id") or 0))
    if prev is None:
        return {"notified": False, "reason": "first in bucket",
                "bucket": bucket}
    qualifies = price_drop_qualifies_bucketed(
        db, listing["edition_id"], float(price),
        variant_id=listing.get("variant_id"),
        cond_rank=listing.get("cond_rank"), baseline_price=float(prev))
    if not qualifies:
        return {"notified": False, "reason": "no qualifying drop",
                "bucket": bucket, "prev_best": prev}
    _title = _edition_title(db, listing["edition_id"])
    body = (f"値下げ検知: {listing['edition_id']}"
            + (f" {_title}" if _title else "") + "\n"
            f"同条件バケット内の最安 {prev:,.0f}円 → {price:,.0f}円\n"
            f"{listing.get('url') or ''}")
    row_id = outbox_mod.enqueue(
        db, event_type="price_drop", body=body,
        edition_id=listing["edition_id"], price=float(price),
        idempotency_key=_episode_key(
            db, f"pricedrop|{bucket}|{_price_part(price)}"))
    return {"notified": row_id is not None, "bucket": bucket,
            "prev_best": prev, "price": price}


def on_listing_price_update(db, listing: dict, old_price: float,
                            new_price: float) -> dict:
    """Same-copy price change (review fix, seller D8).

    Most used records are single copies: there IS no second listing
    in the bucket to compare against, so on_listing_price's
    bucket-best baseline structurally misses the commonest drop —
    a shop cutting the price of its own one copy. When a known
    listing's own price falls, the baseline is its previous price
    (same copy, same bucket by construction). The qualification
    rule and the idempotency key space are shared with
    on_listing_price, so the two paths can never double-notify.

    Contract (R14): old_price / new_price are YEN values (the caller
    converts with scoring.yen_value; rows without a yen value are
    refused below before any comparison happens)."""
    # R14 (adversarial 中-1): the amounts must be positive and
    # finite — this path used to pass a negative new price straight
    # into a "-500円" notification; the bucket path's price<=0 guard
    # had no counterpart here.
    import math
    if not old_price or not new_price or new_price >= old_price \
            or old_price <= 0 or new_price <= 0 \
            or not (math.isfinite(old_price)
                    and math.isfinite(new_price)):
        return {"notified": False, "reason": "not a drop"}
    if not listing.get("edition_id"):
        return {"notified": False, "reason": "no price/edition"}
    # R14 (AI-engineer F1 / collector B / wholesaler 4): the amounts
    # this function compares and prints ARE yen (the caller passes
    # yen values — ingest converts via yen_value). The old gate let
    # currency=None (unknown) rows through and printed their raw
    # amounts as yen, contradicting the bucket path, which refuses
    # the same row. The listing must HAVE a yen value at all: a
    # foreign or unknown-currency row without a converted total has
    # no yen price history to compare.
    if yen_value(listing.get("total_jpy"), listing.get("price"),
                 listing.get("currency")) is None:
        return {"notified": False, "reason": "no yen value"}
    if not price_evidence_ok(db, listing.get("id"),
                             listing["edition_id"]):
        return {"notified": False,
                "reason": "price confidence not high (9-C12)"}
    if _variant_ambiguous(db, listing["edition_id"],
                          listing.get("variant_id")):
        return {"notified": False,
                "reason": "no bucket (variant unknown)"}
    bucket = price_bucket_key(listing["edition_id"],
                              listing.get("variant_id"),
                              listing.get("cond_rank"))
    if bucket is None:
        return {"notified": False, "reason": "no bucket (cond unknown)"}
    qualifies = price_drop_qualifies_bucketed(
        db, listing["edition_id"], float(new_price),
        variant_id=listing.get("variant_id"),
        cond_rank=listing.get("cond_rank"),
        baseline_price=float(old_price))
    if not qualifies:
        return {"notified": False, "reason": "no qualifying drop",
                "bucket": bucket, "prev_best": old_price}
    _title = _edition_title(db, listing["edition_id"])
    body = (f"値下げ検知: {listing['edition_id']}"
            + (f" {_title}" if _title else "") + "\n"
            f"同一出品の価格 {old_price:,.0f}円 → {new_price:,.0f}円\n"
            f"{listing.get('url') or ''}")
    row_id = outbox_mod.enqueue(
        db, event_type="price_drop", body=body,
        edition_id=listing["edition_id"], price=float(new_price),
        idempotency_key=_episode_key(
            db, f"pricedrop|{bucket}|{_price_part(new_price)}"))
    return {"notified": row_id is not None, "bucket": bucket,
            "prev_best": old_price, "price": new_price}
