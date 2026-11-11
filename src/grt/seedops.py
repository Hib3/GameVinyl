"""Doujin CD seeds (agreement-17) + candidate cross-checks.

Seeds are advance records of doujin CDs that may one day surface as
analog editions. They live in candidate_editions with status='seed'
under strict separation (Phase 3 acceptance #1):

- seeds are NEVER counted in edition/candidate totals — every
  counting query in this module (and /coverage) keeps them apart;
- seeds carry NO price evidence (price fields stay NULL);
- promotion happens only when an analog release is found AND the
  doujin composite key matches AND a human approves in the review
  queue — promote_seed() refuses without an approval record.

Retrospective inventory: past-event seeds that never reached
consignment are re-listed periodically (retrospective_report) so
the dig-up work stays visible instead of rotting in the table.

Cross-check （裏取り）: a candidate's confidence may rise ONLY by
re-running the shared matcher score table against corroborating
records (Discogs/VGMdb by catalog number) and meeting the same
thresholds — there is deliberately no cross-check-only bonus path
(acceptance #5).
"""
from __future__ import annotations

import json

from . import clock
from .normalize import normalize_catalog, normalize_title


def import_seed(db, *, title: str, circle: str = "",
                catalog_norm: str = "", event_name: str = "",
                source_url: str = "") -> int | None:
    """Insert one doujin seed. Returns candidate id (None if dup).

    W-check B3 (maniac B D-9): the fingerprint and the composite
    comparison used to run on RAW strings (a private strip/lower
    normalisation), while the rest of the system keys on
    normalize_title / normalize_catalog — the same seed imported
    with a stray space became a second seed, and a legitimate
    promotion failed on a whitespace difference alone. Both now
    use the system normalisation. (Fingerprints of seeds imported
    before this change keep their old form; re-importing such a
    seed creates the normalised row and the stale raw one simply
    never matches — fail-safe, no false promotion.)
    """
    # R14 (maniac B D-2): the fingerprint gains the normalised
    # catalog when one is provided — two pressings of the same
    # title/circle/event differing only in catalog used to collapse
    # into one seed, the second silently dropped.
    # W-check B5 (maniac A 軽微-5): the duplicate check used the
    # raw normalised catalog, so WAVE-001 and WAVE-1 — the same
    # number by the composite gate's own numeric rule — imported
    # as TWO seeds. The fingerprint's catalog segment now folds
    # the digit run's leading zeros the same way (prefix and any
    # suffix are kept, so distinct suffixes stay distinct).
    _cat_norm = normalize_catalog(catalog_norm)["norm"] \
        if catalog_norm else ""
    if _cat_norm:
        import re as _re
        _m = _re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", _cat_norm)
        if _m:
            _cat_norm = (_m.group(1)
                         + (_m.group(2).lstrip("0") or "0")
                         + _m.group(3))
    fingerprint = (
        f"seed|{normalize_title(title)}|{normalize_title(circle)}"
        f"|{normalize_title(event_name)}"
        f"|{_cat_norm}")
    existing = db.execute(
        "SELECT id FROM candidate_editions WHERE fingerprint=?",
        (fingerprint,)).fetchone()
    if existing:
        return None
    cur = db.execute(
        "INSERT INTO candidate_editions(title, raw_title, catalog_norm,"
        " source_url, fingerprint, distribution_type, found_at,"
        " first_seen, last_seen, found_via, status, variant_hints_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?, 'seed', ?)",
        (title, title, catalog_norm, source_url, fingerprint,
         "doujin", clock.now_iso(db), clock.now_iso(db),
         clock.now_iso(db), f"doujin_seed:{event_name}",
         json.dumps({"circle": circle, "event": event_name})))
    db.commit()
    return int(cur.lastrowid)


def seed_count(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM candidate_editions WHERE status='seed'"
    ).fetchone()[0]


def active_candidate_count(db) -> int:
    """Non-seed candidates — the count seeds must never inflate."""
    return db.execute(
        "SELECT COUNT(*) FROM candidate_editions WHERE"
        " status IS NOT 'seed'").fetchone()[0]


def request_seed_promotion(db, seed_id: int,
                           found: dict | None = None) -> int | None:
    """Enqueue the review item a promotion approval is recorded on.

    W-check B4: this had NO production caller and NO dedupe (two
    calls made two rows), and the found record was not stored, so
    even a wired-up approval could not have supplied promote_seed's
    `found`. The doujin harvest now calls it on a composite match;
    the found record rides in the payload for the bot's approval
    to hand to promote_seed. Returns None when an unresolved row
    for this seed already exists."""
    rows = db.execute(
        "SELECT payload_json FROM review_queue WHERE"
        " kind='seed_promotion' AND resolved_at IS NULL").fetchall()
    for r in rows:
        try:
            p = json.loads(r[0] or "")
        except (ValueError, TypeError):
            continue
        if isinstance(p, dict) and p.get("seed_id") == seed_id:
            return None
    cur = db.execute(
        "INSERT INTO review_queue(kind, payload_json, priority,"
        " created_at) VALUES('seed_promotion', ?, 35, ?)",
        (json.dumps({"seed_id": seed_id, "found": found or {}},
                    ensure_ascii=False), clock.now_iso(db)))
    db.commit()
    return int(cur.lastrowid)


def _composite_key_matches(seed_row, found: dict) -> bool:
    """Composite-key check, computed here — never caller-asserted.

    Doujin composite key = catalog number when both sides have one,
    otherwise (normalized title AND circle). (Review D5: the first
    implementation trusted two caller-supplied booleans, so any
    caller could promote any seed with no evidence at all.)"""
    seed_cat = normalize_catalog(seed_row["catalog_norm"])["norm"]
    found_cat = normalize_catalog(found.get("catalog_norm"))["norm"]
    if seed_cat and found_cat:
        if seed_cat == found_cat:
            return True
        # R14 (maniac B D-1): doujin catalogs are written XXX-001
        # by the circle and XXX-1 by shops; the matcher's numeric
        # axis treats those as the same number but this gate
        # compared the raw normalised strings and silently refused
        # legitimate promotions. Same rule as the matcher: equal
        # numeric part under an equal prefix matches.
        import re as _re

        def parts(n):
            m = _re.fullmatch(r"([A-Z]*)(\d+)([A-Z]*)", n)
            return (m.group(1), m.group(2).lstrip("0") or "0") \
                if m else None
        sp, fp = parts(seed_cat), parts(found_cat)
        if sp and fp:
            return sp == fp
        return False
    try:
        hints = json.loads(seed_row["variant_hints_json"] or "{}")
    except (ValueError, TypeError):
        hints = {}
    if not isinstance(hints, dict):
        # W-check B4 (adversarial): a JSON array hints value made
        # .get raise AttributeError out of promote_seed, wedging the
        # seed forever; a non-dict hints value is simply no match.
        hints = {}
    seed_circle = normalize_title(hints.get("circle"))
    found_circle = normalize_title(found.get("circle"))
    seed_title = normalize_title(seed_row["title"])
    found_title = normalize_title(found.get("title"))
    return bool(seed_title and found_title and seed_circle
                and found_circle and seed_title == found_title
                and seed_circle == found_circle)


def composite_key_matches(seed_row, found: dict) -> bool:
    """Public name for the composite-key gate (the doujin harvest
    pre-checks sightings with the same function promote_seed
    verifies with — one implementation, two call sites)."""
    return _composite_key_matches(seed_row, found)


def _fold_harvest_candidate(db, seed_row) -> int | None:
    """Fold pending harvest candidates of the same work into the
    seed being promoted (R19). Identity is the fingerprint parts:
    folded title (segment 0) and folded circle (segment 2) — the
    same key the doujin lane aggregates on; a catalog conflict on
    either side refuses the fold.

    W-check B5 (maniac A 軽微-4): the fold stopped at the FIRST
    match — with the same work harvested at two events (two
    pending candidates), the second stayed pending after the
    promotion and the user judged the same work again. All
    matches fold now; the return value stays the first folded
    id (the promote result's merged_candidate_id)."""
    seed_title = normalize_title(seed_row["title"]).replace("・", "")
    try:
        hints = json.loads(seed_row["variant_hints_json"] or "{}")
    except (ValueError, TypeError):
        hints = {}
    if not isinstance(hints, dict):
        hints = {}
    seed_circle = normalize_title(hints.get("circle") or "") \
        .replace("・", "")
    seed_cat = normalize_catalog(seed_row["catalog_norm"])["norm"]
    first_folded = None
    for cand in db.execute(
            "SELECT * FROM candidate_editions WHERE status='pending'"
            " AND id!=?", (seed_row["id"],)).fetchall():
        parts = (cand["fingerprint"] or "").split("|")
        if len(parts) != 3 or parts[0] != seed_title:
            continue
        # W-check B5 R24 (maniac A 中): a circle-LESS seed folded
        # a pending candidate whose circle was KNOWN and
        # different — the candidate vanished from review (marked
        # merged, its schedule/signals migrated) without the user
        # ever judging it, on title equality alone. The guard now
        # fires whenever EITHER side names a circle and they
        # disagree; a fold needs both sides compatible (both
        # empty, or equal).
        if (seed_circle or parts[2]) and parts[2] != seed_circle:
            continue
        cand_cat = normalize_catalog(cand["catalog_norm"])["norm"] \
            if cand["catalog_norm"] else (parts[1] or "")
        if seed_cat and cand_cat and seed_cat != cand_cat:
            continue
        hid = int(cand["id"])
        db.execute(
            "UPDATE release_schedule SET candidate_id=?"
            " WHERE candidate_id=?", (seed_row["id"], hid))
        db.execute(
            "UPDATE repress_signals SET candidate_id=?"
            " WHERE candidate_id=?", (seed_row["id"], hid))
        for q in db.execute(
                "SELECT id, payload_json FROM review_queue"
                " WHERE resolved_at IS NULL").fetchall():
            try:
                p = json.loads(q["payload_json"] or "")
            except (ValueError, TypeError):
                continue
            if isinstance(p, dict) \
                    and p.get("candidate_id") == hid:
                db.execute(
                    "UPDATE review_queue SET resolved_at=?,"
                    " resolution='merged' WHERE id=?",
                    (clock.now_iso(db), q["id"]))
        db.execute(
            "UPDATE candidate_editions SET status='rejected',"
            " reject_reason=? WHERE id=?",
            (f"merged_into_seed:{seed_row['id']}", hid))
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('candidate_merged', ?, ?)",
            (json.dumps({"from_candidate_id": hid,
                         "into_seed_id": seed_row["id"]}),
             clock.now_iso(db)))
        if first_folded is None:
            first_folded = hid
    return first_folded


def promote_seed(db, seed_id: int, *, found: dict,
                 review_queue_id: int) -> dict:
    """Promote a seed — both gates VERIFIED against records.

    Gate 1: a review_queue row of kind 'seed_promotion' for this
    seed, resolved with resolution='approved'.
    Gate 2: the composite key of the found analog record, computed
    by _composite_key_matches. Caller assertions are not accepted.
    """
    row = db.execute(
        "SELECT * FROM candidate_editions WHERE id=?",
        (seed_id,)).fetchone()
    if row is None or row["status"] != "seed":
        return {"promoted": False, "reason": "not a seed"}
    if not isinstance(found, dict):
        return {"promoted": False, "reason": "found record invalid"}
    q = db.execute(
        "SELECT payload_json, resolved_at, resolution FROM"
        " review_queue WHERE id=? AND kind='seed_promotion'",
        (review_queue_id,)).fetchone()
    approved = False
    payload: dict = {}
    if q is not None and q["resolved_at"] is not None \
            and q["resolution"] == "approved":
        try:
            payload = json.loads(q["payload_json"] or "")
        except (ValueError, TypeError):
            payload = {}
        # R19 (AI engineer / maniac B): Gate 1 was a reusable
        # stamp — it verified only THAT the row was approved, not
        # WHAT was approved, and it was never consumed, so the
        # same approval could promote with a found record the
        # human never saw, more than once. The gate now also
        # requires: not previously consumed, and — when the
        # approval carried a found record — that the found record
        # presented here is that record (composite-relevant fields).
        approved = isinstance(payload, dict) \
            and payload.get("seed_id") == seed_id \
            and not payload.get("_consumed")
        if approved:
            seen_found = payload.get("found")
            if isinstance(seen_found, dict) and seen_found:
                for key in ("title", "circle", "catalog_norm"):
                    if normalize_title(seen_found.get(key) or "") \
                            != normalize_title(found.get(key) or ""):
                        approved = False
    if not approved:
        return {"promoted": False, "reason": "review approval required"}
    if not _composite_key_matches(row, found or {}):
        return {"promoted": False, "reason": "composite key mismatch"}
    # Consume the approval: it promotes THIS sighting once.
    payload["_consumed"] = True
    db.execute(
        "UPDATE review_queue SET payload_json=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), review_queue_id))
    # R19 (maniac A): if the same work ALSO exists as a harvest
    # candidate (the seed was imported after the sighting, so the
    # harvest could not route to it), fold that candidate into the
    # seed before promotion — its schedules and signals move over,
    # its queue rows resolve as 'merged', and the user judges the
    # work once, on the promoted seed's row.
    merged_id = _fold_harvest_candidate(db, row)
    # W-check B4 (maniac A): promotion used to land the candidate in
    # status='candidate_review' — a value no consumer reads (every
    # lane keys on 'pending'), so a promoted seed fell into a second
    # limbo with no queue row and no notification. It now enters the
    # normal review flow: status 'pending' + candidate_review row +
    # button message, exactly like a fresh discovery.
    db.execute(
        "UPDATE candidate_editions SET status='pending'"
        " WHERE id=?", (seed_id,))
    db.execute(
        "INSERT INTO review_queue(kind, payload_json, fingerprint,"
        " priority, created_at) VALUES('candidate_review', ?, ?, 35,"
        " ?)",
        (json.dumps({"candidate_id": seed_id,
                     "source": "seed_promotion"}),
         row["fingerprint"], clock.now_iso(db)))
    from . import outbox as _outbox
    _outbox.enqueue_candidate_review(db, seed_id)
    db.execute(
        "INSERT INTO system_events(kind, detail_json, created_at)"
        " VALUES('seed_promoted', ?, ?)",
        (json.dumps({"seed_id": seed_id,
                     "review_queue_id": review_queue_id}),
         clock.now_iso(db)))
    db.commit()
    return {"promoted": True, "merged_candidate_id": merged_id}


def retrospective_report(db) -> list[dict]:
    """Seeds from past events with no analog sighting (dig-up list).

    W-check B4: sightings recorded by the doujin harvest
    (seed_promotion queue rows, resolved or not) link a seed to a
    sighting, so a sighted seed leaves this list; and a corrupt
    variant_hints_json no longer raises out of the report
    (adversarial QA — the same column is guarded in
    _composite_key_matches; this reader was not)."""
    sighted = set()
    # W-check B5 (maniac A 軽微-2): a seed whose promotion was
    # REJECTED (a mistaken sighting — wrong circle, a namesake
    # work) used to leave this list forever: the sighted set
    # counted rejected rows too. Only an unresolved or APPROVED
    # promotion is a sighting that counts; a rejected-only seed
    # returns to the dig-up list.
    for payload, resolution in db.execute(
            "SELECT payload_json, resolution FROM review_queue"
            " WHERE kind='seed_promotion'").fetchall():
        if resolution == "rejected":
            continue
        try:
            p = json.loads(payload or "")
        except (ValueError, TypeError):
            continue
        if isinstance(p, dict) and isinstance(p.get("seed_id"), int):
            sighted.add(p["seed_id"])
    rows = db.execute(
        "SELECT id, title, variant_hints_json, found_at FROM"
        " candidate_editions WHERE status='seed' ORDER BY found_at"
    ).fetchall()
    out = []
    for r in rows:
        if r[0] in sighted:
            continue
        try:
            hints = json.loads(r[2] or "{}")
        except (ValueError, TypeError):
            hints = {}
        if not isinstance(hints, dict):
            hints = {}
        out.append({"id": r[0], "title": r[1], "hints": hints,
                    "found_at": r[3]})
    return out


def crosscheck_candidate(db, candidate_id: int, corroborations: list[dict],
                         *, matcher_evaluate, edition: dict) -> dict:
    """Re-score a candidate with corroborating records merged in.

    `corroborations` are listing-shaped dicts found by catalog
    number on Discogs/VGMdb. Each is scored with the SAME matcher
    table as any listing; the candidate's existence confidence is
    updated only when a corroboration's own evaluation reaches the
    shared 'high' threshold. No other path raises confidence here.
    """
    # Review D8: seeds live in a separate frame — a cross-check must
    # never raise a seed's confidence through the back door.
    srow = db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (candidate_id,)).fetchone()
    if srow is not None and srow[0] == "seed":
        return {"raised": False, "reason": "seed frame is separate"}
    best = None
    for item in corroborations:
        result = matcher_evaluate(item, edition)
        if best is None or (result.get("score") or 0) > \
                (best.get("score") or 0):
            best = result
    if best is None:
        return {"raised": False, "reason": "no corroborations"}
    if best.get("existence_confidence") == "高":
        db.execute(
            "UPDATE candidate_editions SET existence_confidence='高'"
            " WHERE id=?", (candidate_id,))
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('candidate_crosschecked', ?, ?)",
            (json.dumps({"candidate_id": candidate_id,
                         "score": best.get("score")}),
             clock.now_iso(db)))
        db.commit()
        return {"raised": True, "score": best.get("score")}
    return {"raised": False, "score": best.get("score"),
            "existence_confidence": best.get("existence_confidence")}
