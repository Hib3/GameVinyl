"""Evidence packs and strict ending judgement (agreement-06).

A listing moves to the ended side only after absence on TWO consecutive
successful fetches. Failed / BLOCKED / SUSPECT fetches never count as
absence. Wording is always "ended" — never "sold" vs "withdrawn".
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import clock, config


def normalized_text_hash(text: str) -> str:
    """Page-diff hash: whitespace-normalized body hash (own implementation,
    no hosted diff service)."""
    norm = " ".join((text or "").split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# W-check B3 (maniac A D-7): the failure-body policy above was
# never wired — every fetcher passed keep_body=False, and at the
# moment save_raw_evidence runs the fetch's outcome is not yet
# known (parsing happens after). Bodies are therefore held in a
# small bounded in-memory registry keyed by raw hash; when the
# run's outcome turns out to be a failure / SUSPECT, BaseFetcher
# calls commit_body_for() and the body lands on disk, with its
# path recorded on the fetch_runs row. Successful runs' bodies are
# simply dropped when the registry rolls over.
_PENDING_BODIES: dict[str, tuple[str, str]] = {}
_PENDING_LIMIT = 32
# W-check B5 (adversarial QA 中-2): the registry held up to 32
# bodies of UNBOUNDED size in the resident process's memory.
# Stored copies are capped at 1MB; the evidence HASH is always
# over the full body, so truncation never falsifies identity —
# only the retained copy is partial, like a log tail.
# W-check B5 R24 (adversarial QA 軽微-2): the cap was applied as
# a CHARACTER slice — a Japanese body could retain ~3MB and an
# astral-heavy one ~4MB per entry. The limit is in BYTES: the
# encoded form is sliced, and the partial trailing character a
# byte cut can create is dropped on decode.
_BODY_STORE_LIMIT = 1_000_000


def save_raw_evidence(source: str, url: str, body: str, keep_body: bool) -> dict:
    """Store the pre-normalization hash always; store the body itself only
    for failure / SUSPECT cases (retained 30 days by operations policy)."""
    _encoded = (body or "").encode("utf-8")
    h = hashlib.sha256(_encoded).hexdigest()
    stored = _encoded[:_BODY_STORE_LIMIT].decode(
        "utf-8", "ignore")
    path = None
    if keep_body:
        path = _write_body(source, h, stored)
    else:
        _PENDING_BODIES[h] = (source, stored)
        while len(_PENDING_BODIES) > _PENDING_LIMIT:
            _PENDING_BODIES.pop(next(iter(_PENDING_BODIES)))
    return {"raw_hash": h, "body_path": path}


def _write_body(source: str, raw_hash: str, body: str) -> str:
    d = config.data_dir() / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in source)[:40]
    p = d / f"{safe}_{raw_hash[:12]}.html"
    p.write_text(body or "", encoding="utf-8")
    return str(p)


def commit_body_for(raw_hash: str | None) -> str | None:
    """Persist a previously fetched body (failure evidence) and
    return its path, or None when the body is no longer held."""
    if not raw_hash:
        return None
    held = _PENDING_BODIES.pop(raw_hash, None)
    if held is None:
        return None
    source, body = held
    try:
        return _write_body(source, raw_hash, body)
    except Exception:
        return None


def build_evidence_pack(listing: dict, fetched_at: str | None = None) -> dict:
    return {
        "raw_title": listing.get("raw_title") or listing.get("title"),
        "description": listing.get("description"),
        "url": listing.get("url"),
        "source": listing.get("source"),
        "fetched_at": fetched_at,
        "raw_hash": listing.get("raw_hash"),
    }


def mark_seen(db, listing_id: int) -> None:
    db.execute(
        "UPDATE listings SET absent_streak=0, status='active',"
        " oos_declared=0, last_seen=?"
        " WHERE id=?", (clock.now_iso(db), listing_id))
    db.commit()


def mark_absent(db, listing_id: int, fetch_succeeded: bool) -> str | None:
    """Advance the absence streak after a successful fetch round in which
    the listing did not appear. Returns the new status, or None when the
    fetch did not succeed (absence not counted)."""
    if not fetch_succeeded:
        return None
    row = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    if row is None:
        return None
    # W-check B5 R24 (seller F-2): group judgement re-judged
    # members that were already 'ended' (the group roster is all
    # listings sharing the copy id, ended included) — each extra
    # round inflated absent_streak and recorded ANOTHER
    # listing_ended event for the same ending. An ended row is
    # terminal here: no re-transition, no duplicate audit event.
    if row["status"] == "ended":
        return "ended"
    streak = int(row["absent_streak"] or 0) + 1
    status = "ended" if streak >= 2 else "sold_candidate"
    db.execute("UPDATE listings SET absent_streak=?, status=? WHERE id=?",
               (streak, status, listing_id))
    if status == "ended":
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('listing_ended', ?, ?)",
            (json.dumps({"listing_id": listing_id,
                         "note": "終了（成約か取下げかは不明）"}),
             clock.now_iso(db)))
    db.commit()
    return status


def mark_copy_group_round(db, all_listing_ids: list[int],
                          present_listing_ids: set[int],
                          fetch_succeeded: bool) -> dict:
    """Physical-copy groups are judged as a group: absence counts only
    when every member listing is absent in the same successful round."""
    if not fetch_succeeded:
        return {}
    out = {}

    def _declared_oos(lid) -> bool:
        # W-check B5 (seller 中-1): a group member whose OWN
        # source declared it out of stock THIS cycle (structured
        # availability) was already moved to sold_candidate /
        # oos_declared=1 / streak+1 by the registration pass,
        # which runs before the absence round. The blanket
        # mark_seen below then reset it to active / oos_declared=0
        # / streak=0 in the same cycle — the declared OOS, and
        # the variant_stock signal armed on its return, were
        # silently erased. Ungrouped copies never hit this (their
        # round skips present listings); only the group path
        # overwrote. A declared-OOS member's state belongs to
        # the registration pass; presence here does not revive it.
        row = db.execute(
            "SELECT status, oos_declared FROM listings WHERE id=?",
            (lid,)).fetchone()
        return row is not None and row[0] == "sold_candidate" \
            and int(row[1] or 0) == 1

    if set(all_listing_ids) - set(present_listing_ids):
        if set(present_listing_ids) & set(all_listing_ids):
            for lid in present_listing_ids:
                if not _declared_oos(lid):
                    mark_seen(db, lid)
            return {lid: "partial" for lid in all_listing_ids}
    for lid in all_listing_ids:
        if lid in present_listing_ids:
            if _declared_oos(lid):
                out[lid] = "sold_candidate"
            else:
                mark_seen(db, lid)
                out[lid] = "active"
        else:
            out[lid] = mark_absent(db, lid, True)
    return out


def apply_absence_round(db, rounds: dict) -> dict:
    """Advance the absence lifecycle after one job cycle.

    ``rounds`` maps source name -> the set of listing URLs seen in
    that source's fully successful fetch round this cycle. Only
    sources present in the map are judged; a source that failed (or
    did not run) contributes no absence evidence — a failed fetch
    is not an empty shop (agreement-06).

    W-check B3 (seller D4): mark_absent / mark_copy_group_round
    existed since Phase 2 but had NO caller anywhere — a copy
    relisted at a new URL left its old listing 'active' forever;
    the ghost kept counting as a physical copy and its stale price
    kept serving as the bucket baseline, suppressing real drops.
    Bundled copies are judged as a group (mark_copy_group_round)
    and only when EVERY member's source was successfully observed
    this cycle; a partially observable group is left alone rather
    than judged on half the evidence.
    """
    if not rounds:
        return {"absent": 0, "ended": 0, "sold_candidate": 0}
    rows = db.execute(
        "SELECT id, source, url, physical_copy_id FROM listings"
        " WHERE COALESCE(source,'')!='eBay Browse used'"
        " AND status IN ('active', 'sold_candidate')").fetchall()
    # R14 (seller): membership is compared in CANONICAL form on
    # both sides. Rows registered before canonicalisation carry
    # tracking parameters in the stored URL; the rounds carry the
    # canonical form, so the legacy row looked absent while in
    # stock and was ended by the second round.
    from .normalize import canonical_url
    canon_rounds = {name: {canonical_url(u) for u in urls}
                    for name, urls in rounds.items()}
    present: dict[int, bool] = {}
    for r in rows:
        if r[1] in rounds:
            present[r[0]] = canonical_url(r[2] or "") in canon_rounds[r[1]]
    ended = sold = absent = 0

    def tally(status):
        nonlocal ended, sold
        if status == "ended":
            ended += 1
        elif status == "sold_candidate":
            sold += 1

    groups: dict[int, list] = {}
    singles: list = []
    for r in rows:
        if r[3]:
            # Groups are collected whole (R14): a group whose only
            # unobserved members belong to dead sources still gets
            # judged below; the per-group guard decides.
            groups.setdefault(r[3], []).append(r)
        elif r[0] in present:
            singles.append(r)
    # Sources that can never be observed again (row deleted or no
    # longer approved) must not freeze their copy groups forever
    # (R14, seller): the fully-observable guard used to require
    # EVERY member's source in `rounds`, so one permanently gone
    # shop kept its copies 'active' — and polluting price evidence —
    # indefinitely. A dead source's members count as absent in the
    # group judgement; a live-but-unobserved source still blocks.
    live_sources = {r[0] for r in db.execute(
        "SELECT name FROM sources WHERE status='approved'")}
    # W-check B5 (seller 中-2): 'approved' alone froze groups
    # forever — a member source that stays approved but has not
    # actually been fetched in over 30 days (shop gone quiet,
    # watch disabled) blocked the judgement indefinitely, so even
    # its OBSERVED members never degraded. Only a source with a
    # fetch run inside the freshness window counts as live for
    # the guard; a stale source's members are judged absent like
    # a dead source's.
    from datetime import timedelta as _td
    _fresh_since = clock.to_iso(clock.now(db) - _td(days=30))
    # W-check B5 R24 (seller F-1): freshness was measured by the
    # last ATTEMPT (started_at of any run) — a source that runs
    # daily but is permanently BLOCKED / failing kept the guard
    # 'fresh' forever and froze its whole copy group, the exact
    # shape B5 meant to unfreeze (a bot-walled shop is, for
    # observation purposes, a dead source). Freshness is the last
    # SUCCESSFUL observation: result='OK' runs only.
    fresh_sources = {r[0] for r in db.execute(
        "SELECT DISTINCT s.name FROM sources s"
        " JOIN fetch_runs f ON f.source_id=s.id"
        " WHERE f.started_at>=? AND f.result='OK'",
        (_fresh_since,))}
    for gid, members in groups.items():
        all_members = db.execute(
            "SELECT id, source FROM listings WHERE"
            " physical_copy_id=?"
            " AND COALESCE(source,'')!='eBay Browse used'",
            (gid,)).fetchall()
        if any(m[1] not in rounds and m[1] in live_sources
               and m[1] in fresh_sources
               for m in all_members):
            continue  # a live member source was not observed
        member_ids = [m[0] for m in all_members]
        present_ids = {m[0] for m in members if present.get(m[0])}
        _res = mark_copy_group_round(
            db, member_ids, present_ids, True)
        # W-check B5 R24 (seller, probe observation): a PARTIAL
        # round (some member present) judges nobody absent, but
        # the absent counter still counted the unjudged members —
        # a statistics-only inflation of the run summary.
        if not any(v == "partial" for v in _res.values()):
            absent += len(member_ids) - len(present_ids)
        for status in _res.values():
            tally(status)
    for r in singles:
        if present[r[0]]:
            continue  # re-registration already reset it
        absent += 1
        tally(mark_absent(db, r[0], True))
    return {"absent": absent, "ended": ended, "sold_candidate": sold}
