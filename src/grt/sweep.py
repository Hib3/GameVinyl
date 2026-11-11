"""Catalog-prefix sweep, steady-state operation (agreement-05).

Weekly batch that walks catalog number ranges looking for editions
nobody documented. Hard rules (Phase 3 acceptance #2/#3):

- at most WEEKLY_QUERY_BUDGET (500) lookups per weekly run;
- a prefix band expands from ±5 to ±20 ONLY after its ±5 range is
  fully resolved (every number has a ledger state);
- every probed number lands in catalog_prefix_ledger with one of
  searched_none / unsearched / blocked / hit — a BLOCKED result is
  recorded as 'blocked' and NEVER collapses into searched_none;
- sweep hits go to the review queue with a priority, never to the
  instant-notification path;
- the catalog_suffix_rules version used is stamped into each
  ledger row (suffix_rule_version), and unapproved dictionary rows
  are never applied (acceptance #4).
"""
from __future__ import annotations

import json

import httpx

from . import clock, config
from .fetchers.base import classify_http

WEEKLY_QUERY_BUDGET = 500
NARROW = 5
WIDE = 20


def _applicable_suffix_rows(db):
    """Dictionary rows the application gate admits (C-12.2: an
    observed suffix only, with a basis edition and an evidence URL).

    W-check B3 unified the gate between the version counter and the
    applier; R14 (maniac B S-2) closes the empty-string hole — the
    gate tested IS NOT NULL, so evidence_url='' / base_edition_id=''
    passed although an empty string is no evidence at all."""
    return db.execute(
        "SELECT suffix, policy_version FROM catalog_suffix_rules"
        " WHERE approved=1 AND base_edition_id IS NOT NULL"
        " AND TRIM(base_edition_id) != ''"
        " AND evidence_url IS NOT NULL"
        " AND TRIM(evidence_url) != ''").fetchall()


def suffix_rule_version(db) -> int:
    rows = _applicable_suffix_rows(db)
    return max((int(v or 0) for _, v in rows), default=0)


def approved_suffixes(db) -> list[str]:
    # W-check B3 (maniac B D-11): an empty suffix row duplicated
    # every base query (and doubled the sweep_hit rows). Blank
    # suffixes are not suffixes; duplicates collapse.
    out: list[str] = []
    for s, _v in _applicable_suffix_rows(db):
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def approved_suffix_versions(db) -> list[tuple[str, int]]:
    """Applicable (suffix, dictionary version) pairs — the unit the
    ledger's per-suffix probe map is keyed by (R14, maniac B S-3):
    the dictionary version is per-suffix, so adding suffix B does
    not make suffix A 'changed' on every resolved number."""
    best: dict[str, int] = {}
    for s, v in _applicable_suffix_rows(db):
        s = (s or "").strip()
        if s:
            best[s] = max(best.get(s, 0), int(v or 0))
    return sorted(best.items())


def discogs_catalog_query(catalog: str) -> str:
    """Production querier: 'hit' / 'none' / 'blocked' for a catno."""
    token = config.discogs_token()
    if not token:
        return "blocked"
    try:
        resp = httpx.get(
            "https://api.discogs.com/database/search",
            params={"catno": catalog, "type": "release"},
            headers={"Authorization": f"Discogs token={token}",
                     "User-Agent": "GameRecordTool/2.0 sweep"},
            timeout=30)
    except httpx.HTTPError:
        return "blocked"
    if classify_http(resp.status_code) != "OK":
        return "blocked"
    try:
        data = resp.json()
        return "hit" if (data.get("results") or []) else "none"
    except Exception:
        return "blocked"


def _band_complete(db, prefix: str, centre: int, half: int) -> bool:
    """Complete = every valid number resolved as hit/searched_none.

    Review fix (Maniac A, D2): BLOCKED never counts as complete —
    blocked means "existence unknown", so a fully-blocked band must
    NOT widen to ±20 while its centre is still unresolved. Negative
    numbers are not valid catalog numbers and are excluded from the
    requirement (review D1: they previously made completion
    unreachable)."""
    lo = max(0, centre - half)
    rows = {r[0]: r[1] for r in db.execute(
        "SELECT number, state FROM catalog_prefix_ledger WHERE"
        " prefix=? AND number BETWEEN ? AND ?",
        (prefix, lo, centre + half)).fetchall()}
    return all(rows.get(n) in ("hit", "searched_none")
               for n in range(lo, centre + half + 1))


def _band_has_hit(db, prefix: str, centre: int, half: int) -> bool:
    row = db.execute(
        "SELECT 1 FROM catalog_prefix_ledger WHERE prefix=? AND"
        " number BETWEEN ? AND ? AND state='hit' LIMIT 1",
        (prefix, max(0, centre - half), centre + half)).fetchone()
    return row is not None


def known_centres(db, prefix: str) -> list[int]:
    """Band centres derived from REAL catalog numbers (review D1).

    The first implementation defaulted every prefix's centre to 1
    when the ledger had no hits, so the sweep only ever probed
    numbers 0-6 of each band and never reached the ranges where the
    NO_ROUTE editions actually live. Centres now come from the
    editions table (the numeric tail of catalog_norm for this
    prefix) plus ledger hits."""
    import re
    centres: set[int] = set()
    for (cat,) in db.execute(
            "SELECT catalog_norm FROM editions WHERE catalog_norm"
            " LIKE ?", (prefix + "%",)).fetchall():
        m = re.search(r"(\d+)$", cat or "")
        if m:
            try:
                centres.add(int(m.group(1)))
            except ValueError:
                pass
    for (num,) in db.execute(
            "SELECT number FROM catalog_prefix_ledger WHERE"
            " prefix=? AND state='hit'", (prefix,)).fetchall():
        centres.add(int(num))
    return sorted(centres)


def _record(db, prefix: str, number: int, state: str,
            rule_version: int) -> None:
    db.execute(
        "INSERT INTO catalog_prefix_ledger(prefix, number, state,"
        " checked_at, suffix_rule_version) VALUES(?,?,?,?,?)"
        " ON CONFLICT(prefix, number) DO UPDATE SET state=excluded.state,"
        " checked_at=excluded.checked_at,"
        " suffix_rule_version=excluded.suffix_rule_version",
        (prefix, number, state, clock.now_iso(db), rule_version))


def run_weekly_sweep(db, *, querier=None, budget: int = WEEKLY_QUERY_BUDGET,
                     centres: list[tuple[str, int]] | None = None) -> dict:
    """One weekly batch. `centres` = [(prefix, centre_number), ...].

    Defaults to the priority prefixes' known edition numbers as
    centres. Returns a summary; never raises on a single failure.

    R14 restructure (wholesaler 1/2, maniac B S-1/S-3): suffix
    probing is tracked PER NUMBER AND PER SUFFIX in the ledger's
    suffix_probe_json map ({suffix: dictionary version probed}).
    A number is re-probed only for suffixes whose own dictionary
    version moved since that number last probed them — adding one
    suffix no longer re-probes every suffix on every resolved
    number, and a budget cut mid-pass persists exactly the suffixes
    that completed, so an unprobed suffix is never stamped as
    probed. Fresh (unresolved) numbers across ALL centres are
    probed in pass 1; resolved numbers' stale suffixes are deferred
    to pass 2 and consume only the budget pass 1 leaves over, so a
    dictionary update cannot starve new discovery for a week.
    """
    querier = querier or discogs_catalog_query
    rule_version = suffix_rule_version(db)
    suffix_versions = approved_suffix_versions(db)
    priority = [r[0] for r in db.execute(
        "SELECT prefix FROM priority_catalog_prefixes").fetchall()]
    if centres is None:
        centres = []
        for prefix in priority:
            centres.extend((prefix, c) for c in known_centres(db, prefix))
    used = 0
    hits = 0
    deferred: list[tuple[str, int]] = []

    def queue_hit(payload: str) -> None:
        # A sweep_hit is queued at most once EVER for a payload
        # (R14, maniac B S-3): a hit the user already resolved —
        # above all a REJECTED one — must not resurrect on the next
        # dictionary version bump.
        dup = db.execute(
            "SELECT 1 FROM review_queue WHERE kind='sweep_hit'"
            " AND payload_json=?", (payload,)).fetchone()
        if not dup:
            db.execute(
                "INSERT INTO review_queue(kind, payload_json,"
                " priority, created_at) VALUES('sweep_hit', ?, 40,"
                " ?)", (payload, clock.now_iso(db)))

    def probe_suffixes(prefix: str, number: int, probe_map: dict) -> None:
        """Probe this number's stale suffixes, persisting the map
        for exactly the suffixes that completed."""
        nonlocal used, hits
        catalog = f"{prefix}{number}"
        needed = [(s, v) for s, v in suffix_versions
                  if probe_map.get(s) != v]
        done = False
        for suffix, version in needed:
            if used >= budget:
                break
            try:
                sres = querier(catalog + suffix)
            except Exception:
                sres = "blocked"
            used += 1
            done = True
            probe_map[suffix] = version
            if sres == "hit":
                hits += 1
                queue_hit(json.dumps(
                    {"prefix": prefix, "number": number,
                     "catalog": catalog + suffix, "suffix": suffix}))
        if done:
            db.execute(
                "UPDATE catalog_prefix_ledger SET suffix_probe_json=?"
                " WHERE prefix=? AND number=?",
                (json.dumps(probe_map), prefix, number))
            db.commit()

    for prefix, centre in centres:
        # Widening gate (review D2): ±20 only when ±5 is fully
        # resolved AND the band produced a hit or the prefix is a
        # NO_ROUTE-concentrated priority prefix (agreement-05).
        widen = _band_complete(db, prefix, centre, NARROW) and (
            _band_has_hit(db, prefix, centre, NARROW)
            or prefix in priority)
        half = WIDE if widen else NARROW
        lo = max(0, centre - half)
        # Probe order (review D7): never-probed numbers first;
        # blocked re-probes come after, so a permanently blocked
        # band cannot starve fresh exploration of the budget.
        numbers = list(range(lo, centre + half + 1))
        states = {r[0]: r[1] for r in db.execute(
            "SELECT number, state FROM catalog_prefix_ledger WHERE"
            " prefix=? AND number BETWEEN ? AND ?",
            (prefix, lo, centre + half)).fetchall()}
        probe_maps: dict[int, dict] = {}
        for n, raw in db.execute(
                "SELECT number, suffix_probe_json FROM"
                " catalog_prefix_ledger WHERE prefix=? AND number"
                " BETWEEN ? AND ?", (prefix, lo, centre + half)
        ).fetchall():
            try:
                parsed = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                parsed = {}
            probe_maps[n] = parsed if isinstance(parsed, dict) else {}
        numbers.sort(key=lambda n: 0 if n not in states else 1)
        for number in numbers:
            if used >= budget:
                db.commit()
                return {"queries": used, "hits": hits,
                        "budget_exhausted": True}
            catalog = f"{prefix}{number}"
            if states.get(number) in ("searched_none", "hit"):
                # Resolved number: only its stale suffixes can be
                # news. Deferred to pass 2 (see docstring).
                probe_map = probe_maps.get(number, {})
                if any(probe_map.get(s) != v
                       for s, v in suffix_versions):
                    deferred.append((prefix, number))
                continue
            try:
                result = querier(catalog)
            except Exception:
                # Review D6: one failing lookup must not abort the
                # whole weekly run; it is recorded as blocked.
                result = "blocked"
            used += 1
            state = {"hit": "hit", "none": "searched_none",
                     "blocked": "blocked"}.get(result, "blocked")
            _record(db, prefix, number, state, rule_version)
            if state == "hit":
                hits += 1
                queue_hit(json.dumps(
                    {"prefix": prefix, "number": number,
                     "catalog": catalog}))
            db.commit()
            # Suffix variants (review D4), inside the same budget.
            # When the BASE lookup is blocked the API is already
            # refusing — suffix variants of a blocked base only burn
            # the weekly budget against a wall (maniac B D-10).
            if state != "blocked":
                probe_suffixes(prefix, number,
                               probe_maps.get(number, {}))
    # Pass 2: stale suffixes on resolved numbers, leftover budget.
    for prefix, number in deferred:
        if used >= budget:
            break
        row = db.execute(
            "SELECT suffix_probe_json FROM catalog_prefix_ledger"
            " WHERE prefix=? AND number=?", (prefix, number)
        ).fetchone()
        try:
            probe_map = json.loads(row[0]) if row and row[0] else {}
        except (ValueError, TypeError):
            probe_map = {}
        if not isinstance(probe_map, dict):
            probe_map = {}
        probe_suffixes(prefix, number, probe_map)
    db.commit()
    return {"queries": used, "hits": hits, "budget_exhausted": False}


def unsearched_band_count(db) -> int:
    """Numbers in known bands with no ledger row yet (review D3).

    The first implementation counted ledger rows with
    state='unsearched' — a state nothing ever writes — so /coverage
    reported "未探査 0件" even when nothing had been probed. This
    counts, for every priority prefix's real centres, the ±5 numbers
    that have no ledger row at all."""
    total = 0
    for (prefix,) in db.execute(
            "SELECT prefix FROM priority_catalog_prefixes").fetchall():
        for centre in known_centres(db, prefix):
            lo = max(0, centre - NARROW)
            have = {r[0] for r in db.execute(
                "SELECT number FROM catalog_prefix_ledger WHERE"
                " prefix=? AND number BETWEEN ? AND ?",
                (prefix, lo, centre + NARROW)).fetchall()}
            total += sum(1 for n in range(lo, centre + NARROW + 1)
                         if n not in have)
    return total
