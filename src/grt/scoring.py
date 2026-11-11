"""Discovery scoring and review priority (agreements 15 / 05).

The score method applies ONLY to discovery candidates and announcement
events — never mixed with listing events. The floor guarantee: any
candidate hitting a floor condition is immediate-band regardless of the
computed score.
"""
from __future__ import annotations

from .normalize import normalize_catalog, normalize_title
from .policy import get_policy

NONRETAIL = {
    "sample_mihon", "promo_not_for_sale", "bundle_bonus", "prize_campaign",
    "store_demo", "magazine_premium", "test_press", "doujin", "event_only",
    "unknown_nonretail",
}
RARE_FORMS = {"bundle_bonus", "prize_campaign", "promo_not_for_sale",
              "sample_mihon", "event_only"}


def _catalog_prefix(catalog_norm: str | None) -> str:
    if not catalog_norm:
        return ""
    pref = ""
    for ch in catalog_norm:
        if ch.isalpha():
            pref += ch
        else:
            break
    return pref


def floor_conditions_hit(db, cand: dict) -> list[str]:
    cand = _cand_safe(cand)
    pol = get_policy(db)["discovery"]
    allowed = set(pol.get("floor_conditions", []))
    hit: list[str] = []
    cat_norm = cand.get("catalog_norm") or \
        normalize_catalog(cand.get("catalog"))["norm"]
    prefix = _catalog_prefix(cat_norm)
    if "known_prefix" in allowed and prefix:
        row = db.execute(
            "SELECT 1 FROM priority_catalog_prefixes WHERE prefix=?",
            (prefix,)).fetchone()
        if row:
            hit.append("known_prefix")
    if "known_label" in allowed and cand.get("label"):
        row = db.execute(
            "SELECT 1 FROM catalog_prefix_dict WHERE label=?",
            (cand["label"],)).fetchone()
        if row:
            hit.append("known_label")
    if "nonretail_form" in allowed and cand.get("distribution_type") in NONRETAIL:
        hit.append("nonretail_form")
    if "stage" in allowed and cand.get("event_stage") in (
            "repress_announced", "preorder_open"):
        hit.append("stage")
    if "wantlist" in allowed:
        row = db.execute(
            "SELECT 1 FROM wantlist w JOIN editions e ON e.id=w.edition_id"
            " WHERE e.catalog_norm=? OR e.title=? LIMIT 1",
            (cat_norm, cand.get("title"))).fetchone()
        if row:
            hit.append("wantlist")
    return hit


def _count_or(value, default: int) -> int:
    """independent_sources as an int, or the default (R14,
    adversarial: 'many'/None from an external payload used to raise
    out of scoring entirely)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_CAND_STR_KEYS = ("catalog", "catalog_norm", "label", "title",
                  "distribution_type", "event_stage", "country",
                  "source_country", "existence_confidence")


def _cand_safe(cand: dict) -> dict:
    """Coerce a candidate dict's string fields to strings (W-check
    B4, wholesaler: B3 hardened independent_sources only — a list
    distribution_type still raised TypeError on the set-membership
    test, an int catalog_norm raised inside the prefix split, and
    a list label raised sqlite3.ProgrammingError at bind time).
    Containers are never valid values for these fields and become
    None; other scalars stringify. Returns a copy."""
    out = dict(cand)
    for key in _CAND_STR_KEYS:
        v = out.get(key)
        if v is None or isinstance(v, str):
            continue
        if isinstance(v, (list, dict, set, tuple)):
            out[key] = None
        else:
            out[key] = str(v)
    return out


def discovery_score(db, cand: dict) -> dict:
    """cand keys: country/source_country, independent_sources (int),
    catalog/catalog_norm, event_stage, distribution_type, label,
    existence_confidence, repost_only (bool)."""
    cand = _cand_safe(cand)
    w = get_policy(db)["discovery"]
    score = 0
    parts: dict[str, int] = {}
    if cand.get("source_country") == "JP" or cand.get("country") == "JP":
        parts["jp_source"] = w["jp_source"]
    if _count_or(cand.get("independent_sources", 1), 1) >= 2:
        parts["multi_source"] = w["multi_source"]
    cat_norm = cand.get("catalog_norm") or \
        normalize_catalog(cand.get("catalog"))["norm"]
    prefix = _catalog_prefix(cat_norm)
    if prefix and db.execute(
            "SELECT 1 FROM priority_catalog_prefixes WHERE prefix=?",
            (prefix,)).fetchone():
        parts["noroute_band"] = w["noroute_band"]
    if cand.get("event_stage") in ("repress_announced", "preorder_open"):
        parts["stage_boost"] = w["stage_boost"]
    if cand.get("distribution_type") in RARE_FORMS:
        parts["rare_form"] = w["rare_form"]
    if "noroute_band" in parts or (cand.get("label") and db.execute(
            "SELECT 1 FROM catalog_prefix_dict WHERE label=?",
            (cand["label"],)).fetchone()):
        parts["known_band_or_label"] = w["known_band_or_label"]
    if cand.get("existence_confidence") == "中":
        parts["mid_confidence"] = w["mid_confidence"]
    if cand.get("repost_only"):
        parts["repost_only"] = w["repost_only"]
    score = sum(parts.values())
    floors = floor_conditions_hit(db, cand)
    floor_applied = bool(floors)
    if floor_applied or score >= w["immediate_threshold"]:
        band = "immediate"
    elif score >= w["digest_top_threshold"]:
        band = "digest_top"
    else:
        band = "digest"
    return {"score": score, "parts": parts, "floor_applied": floor_applied,
            "floor_conditions": floors, "band": band}


# ---------------------------------------------------------------------------
# Price-drop rule with bucket separation (agreement-11, round-9 item 9-A11)
#
# Agreement-11: price comparisons happen ONLY inside the same
# Edition x variant x condition bucket, where the bucket is the identical
# cond_rank value. A listing whose cond_rank is NULL forms no bucket and
# is never a bucket-comparison target. The legacy edition-only rule still
# lives in outbox.price_drop_qualifies (outside this fix's file scope and
# left untouched); the function below is the bucket-correct rule, kept
# unwired in Phase 1 -- price-drop *operation* is Phase 2 -- and pinned
# by tests as a rule function.

def price_bucket_key(edition_id: str, variant_id, cond_rank):
    """Bucket identity, or None when no bucket exists (cond_rank NULL)."""
    if cond_rank is None:
        return None
    return (edition_id, variant_id, cond_rank)


def yen_value(total_jpy, price, currency) -> float | None:
    """The yen-comparable value of a listing row, or None.

    W-check B3 (currency cluster: collector D-2/D-5, wholesaler 3,
    maniac A D-1, adversarial 1): consumers used to fall back from
    total_jpy to the raw ``price`` unconditionally, so a foreign
    amount (USD 30 with total_jpy NULL) was compared and displayed
    as if it were 30 yen. The ONLY yen values a row carries are its
    converted total_jpy, or — when the row's own currency is JPY —
    its price. A foreign or unknown-currency row with no conversion
    has NO yen value and must never enter a yen comparison, a yen
    minimum, or a yen notification.
    """
    # R14 hardening (maniac A currency normalisation; adversarial
    # 中-2): the currency comparison goes through normalize_currency
    # so stored 'jpy'/'JPY ' rows still count as yen, and a
    # non-finite value (an inf that slipped in through a producer)
    # is not a yen value at all — it poisoned bucket minima.
    import math

    from .normalize import normalize_currency
    # W-check B5 (AI engineer): float() was unguarded — a legacy
    # TEXT value in a numeric column ('zzz' in total_jpy) raised
    # ValueError out of every caller: pricedrop died per bucket
    # (silently suppressing drop detection for the whole bucket,
    # every cycle) and /price returned no reply at all. A
    # non-numeric amount is simply not a yen value.
    def _as_float(v):
        # R24 (AI engineer 軽微-C): float() of a huge Python int
        # (a JSON number literal parsed by a fetcher) raises
        # OverflowError, which escaped the guard.
        try:
            return float(v)
        except (TypeError, ValueError, OverflowError):
            return None
    if total_jpy is not None:
        v = _as_float(total_jpy)
        return v if v is not None and math.isfinite(v) else None
    if normalize_currency(currency) == "JPY" and price is not None:
        v = _as_float(price)
        return v if v is not None and math.isfinite(v) else None
    return None


def price_drop_qualifies_bucketed(db, edition_id: str, new_price: float, *,
                                  variant_id=None, cond_rank=None,
                                  baseline_price: float | None = None) -> bool:
    """True when a price drop is established *inside one bucket*.

    Baseline resolution: an explicit ``baseline_price`` wins; otherwise
    the baseline is the bucket's previous BEST (minimum) yen value
    over live rows — the same quantity the operational wrapper
    (pricedrop.bucket_best) resolves, so the rule function and the
    operation can never disagree about what "the bucket's price"
    means. (W-check B3, collector S-3 / wholesaler 8: the fallback
    used to take the LATEST row by id — any status, possibly the
    candidate itself, possibly an ended row's stale price — while
    the wrapper took the minimum of live rows; the same drop could
    qualify under one reading and not the other.) Only yen values
    count (see :func:`yen_value`): a foreign amount is never a yen
    baseline. With no in-bucket baseline no drop can be established,
    so this returns False -- deliberately unlike the legacy
    edition-only rule, whose no-baseline True would let a price from
    another bucket pose as the comparison partner. The drop itself
    follows the same deterministic rule as the legacy one: price
    actually changed AND at least ``price_drop_rate`` (default 10%)
    below the baseline. The operational wrapper additionally applies
    the 9-C12 price-confidence gate; this function is the pure
    price rule.
    """
    if price_bucket_key(edition_id, variant_id, cond_rank) is None:
        return False
    if baseline_price is None:
        rows = db.execute(
            "SELECT total_jpy, price, currency FROM listings"
            " WHERE edition_id=?"
            " AND COALESCE(source,'')!='eBay Browse used'"
            " AND variant_id IS ? AND cond_rank=?"
            " AND status NOT IN ('ended', 'sold_candidate')",
            (edition_id, variant_id, cond_rank)).fetchall()
        vals = [yen_value(r[0], r[1], r[2]) for r in rows]
        vals = [v for v in vals if v is not None and v > 0]
        if not vals:
            return False
        baseline_price = min(vals)
    last = float(baseline_price)
    rate = float(get_policy(db).get("price_drop_rate", 0.10))
    return new_price != last and new_price <= last * (1 - rate)


def review_priority(db, cand: dict) -> int:
    cand = _cand_safe(cand)
    w = get_policy(db)["review_priority"]
    prio = 0
    if cand.get("source_country") == "JP" or cand.get("country") == "JP":
        prio += w["jp_source"]
    if _count_or(cand.get("independent_sources", 1), 1) >= 2:
        prio += w["multi_source"]
    cat_norm = cand.get("catalog_norm") or \
        normalize_catalog(cand.get("catalog"))["norm"]
    prefix = _catalog_prefix(cat_norm)
    if prefix and db.execute(
            "SELECT 1 FROM priority_catalog_prefixes WHERE prefix=?",
            (prefix,)).fetchone():
        prio += w["noroute_prefix"]
    if cand.get("existence_confidence") == "中":
        prio += w["mid_confidence"]
    if cand.get("repost_only"):
        prio += w["repost_only"]
    return prio
