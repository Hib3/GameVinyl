"""Variant-completeness inventory sweep (agreement-23).

The completeness value domain is the spec's (C-6.3):
{unknown, single_confirmed, multiple}.

Review fix (2026-11-08, collector persona): the first version
wrote 'complete' / 'incomplete' — values OUTSIDE that domain — and
in doing so overwrote legitimately granted single_confirmed /
multiple values, permanently breaking the matcher's P2 price gate
(which keys on 'single_confirmed'). Rules now:

- 'multiple': two or more variant rows with status='confirmed';
- 'single_confirmed': exactly ONE variant row, status='confirmed',
  and the single-variant fact is confirmed by >= 2 independent
  channels (the variant's evidence_url, the variant's Discogs ID,
  the edition's Discogs ID, the edition's VGMdb ID — at least two
  of these present);
- 'unknown': everything else. 推測付与は禁止 (no speculative grants).

Priority order (acceptance #9): overseas R4/R5 first, then the
wantlist, then promo-reviewed editions. Because the seed data
carries NO country for any edition, R4/R5 editions with unknown
country are swept immediately after the known-overseas tier —
otherwise the headline tier would be empty forever on real data.

Grants/revokes are recorded as system_events with from/to values.
"""
from __future__ import annotations

import json

from . import clock


def priority_editions(db, limit: int = 200) -> list[str]:
    """Editions in the agreement-23 priority order."""
    seen: list[str] = []

    def add(rows):
        for (eid,) in rows:
            if eid and eid not in seen:
                seen.append(eid)

    add(db.execute(
        "SELECT id FROM editions WHERE country IS NOT NULL AND"
        " country NOT IN ('Japan','JP','日本') AND rarity IN ('R4','R5')"
        " ORDER BY id").fetchall())
    add(db.execute(
        "SELECT id FROM editions WHERE country IS NULL AND"
        " rarity IN ('R4','R5') ORDER BY id").fetchall())
    add(db.execute(
        "SELECT DISTINCT edition_id FROM wantlist WHERE edition_id IS"
        " NOT NULL ORDER BY edition_id").fetchall())
    add(db.execute(
        "SELECT DISTINCT id FROM editions WHERE id IN"
        " (SELECT edition_id FROM match_decisions WHERE veto LIKE"
        " '%promo%') ORDER BY id").fetchall())
    return seen[:limit]


def evaluate_completeness(db, edition_id: str) -> str:
    rows = db.execute(
        "SELECT status, evidence_url, discogs_id FROM edition_variants"
        " WHERE edition_id=?", (edition_id,)).fetchall()
    confirmed = [r for r in rows if r[0] == "confirmed"]
    if len(confirmed) >= 2:
        return "multiple"
    if len(rows) == 1 and len(confirmed) == 1:
        ed = db.execute(
            "SELECT discogs_id, vgmdb_id FROM editions WHERE id=?",
            (edition_id,)).fetchone()
        # W-check B3 (wholesaler 1 / maniac B D-2): channels are
        # counted by IDENTITY, not by field. The variant's Discogs
        # ID and the edition's Discogs ID pointing at the SAME
        # release are one channel — one Discogs page — not two
        # independent systems; counting both granted
        # single_confirmed (and with it the P2 price gate) on a
        # single source, below the evidence level the rule exists
        # to guarantee.
        channels: set = set()
        if confirmed[0][1]:  # variant evidence_url
            # R14 (wholesaler 3): a Discogs RELEASE URL is the
            # Discogs channel, not a second independent one — the
            # B3 identity rule normalised the ID pair but not the
            # URL form of the same page, so one Discogs page still
            # counted twice and granted single_confirmed alone.
            import re as _re
            m = _re.search(r"discogs\.com/(?:[^/]+/)?release/(\d+)",
                           confirmed[0][1])
            if m:
                channels.add(("discogs", int(m.group(1))))
            else:
                channels.add(("url", confirmed[0][1]))
        if confirmed[0][2]:  # variant discogs_id
            channels.add(("discogs", confirmed[0][2]))
        if ed is not None and ed[0]:
            channels.add(("discogs", ed[0]))
        if ed is not None and ed[1]:
            channels.add(("vgmdb", ed[1]))
        if len(channels) >= 2:
            return "single_confirmed"
    return "unknown"


def _sweep_cursor(db) -> str | None:
    row = db.execute(
        "SELECT value FROM settings WHERE key='variant_sweep_cursor'"
    ).fetchone()
    return row[0] if row else None


def _set_sweep_cursor(db, edition_id: str) -> None:
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        "('variant_sweep_cursor', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (edition_id,))


def run_sweep(db, limit: int = 200) -> dict:
    # W-check B3 (wholesaler 7): the priority list was sliced at
    # `limit` from the TOP every quarter, so with R4+R5 occupying
    # 193 of the 200 slots the wantlist and promo tiers were not
    # "later" — they were permanently starved by the same faces.
    # The sweep now rotates: each run continues after the edition
    # the previous run stopped at (wrapping around), so every tier
    # is reached in turn while the priority order within a rotation
    # is unchanged.
    full = priority_editions(db, limit=1_000_000)
    cursor = _sweep_cursor(db)
    if full and cursor in full:
        start = full.index(cursor) + 1
        ordered = full[start:] + full[:start]
    else:
        ordered = full
    batch = ordered[:limit]
    granted = revoked = processed = 0
    for edition_id in batch:
        row = db.execute(
            "SELECT variant_completeness FROM editions WHERE id=?",
            (edition_id,)).fetchone()
        if row is None:
            continue
        processed += 1
        new_value = evaluate_completeness(db, edition_id)
        old_value = row[0] or "unknown"
        if new_value == old_value:
            continue
        db.execute(
            "UPDATE editions SET variant_completeness=? WHERE id=?",
            (new_value, edition_id))
        if new_value in ("single_confirmed", "multiple"):
            kind = "variant_completeness_granted"
            granted += 1
        else:
            kind = "variant_completeness_revoked"
            revoked += 1
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES(?, ?, ?)",
            (kind, json.dumps({"edition_id": edition_id,
                               "from": old_value, "to": new_value}),
             clock.now_iso(db)))
    if batch:
        _set_sweep_cursor(db, batch[-1])
    db.commit()
    return {"processed": processed, "granted": granted,
            "revoked": revoked}


def unconfirmed_count(db) -> int:
    """Variant ROWS not yet confirmed (review fix: the first version
    counted EDITIONS whose completeness != 'complete' — a value
    outside the domain — so confirmed editions were reported as
    unconfirmed and the unit was wrong too)."""
    return db.execute(
        "SELECT COUNT(*) FROM edition_variants WHERE status IS NULL"
        " OR status != 'confirmed'").fetchone()[0]
