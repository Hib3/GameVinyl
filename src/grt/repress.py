"""Announcement layer + REPRESS detection (agreement-13).

Independent public signals that a title is being re-pressed are
collected as repress_signals. The spec fixes THREE counted systems
(C-10.2): (1) variant_stock sold_out -> in_stock transitions,
(2) announcements with event_stage='repress_announced',
(3) discogs_new_release — a newer-year release under the same
Discogs Master with an exact catalog-number match.

Review fixes (2026-11-08, wholesaler persona):
- The first version counted 'retail'/'community' (not spec
  systems) and 'discogs_stats' — the marketplace 0->1 appearance
  the spec explicitly says is NOT a repress signal. discogs_stats
  rows are still recorded and shown as digest notes, but they never
  count toward the threshold.
- Signals now keep their real timestamps (an optional `at`), and
  the 30-day window compares full instants, not truncated dates
  (the old code shaved the signal time to midnight and let ~31-day
  gaps combine).
- Notification idempotency no longer includes the date: the same
  system set never re-fires on later days; a notification fires
  again only when the set of counted systems grows.
- Same-system/same-day duplicates are detected in code (the table's
  UNIQUE key contains nullable columns and never fired), and a
  failed insert is logged as a system_event instead of vanishing.
- Producers exist for the counted systems: signal_announcement()
  and signal_variant_stock() are the entry points the announcement
  layer / store fronts call; evaluate() runs there too.
"""
from __future__ import annotations

import json
from datetime import timedelta

from . import clock
from . import outbox as outbox_mod

WINDOW_DAYS = 30
COUNTED_SYSTEMS = ("variant_stock", "announcement",
                   "discogs_new_release")
KNOWN_SYSTEMS = COUNTED_SYSTEMS + ("discogs_stats",)
SYSTEMS = KNOWN_SYSTEMS  # backwards-compatible name


def register_signal(db, *, system: str, edition_id: str | None = None,
                    candidate_id: int | None = None,
                    detail: str = "", at: str | None = None) -> bool:
    """Record one signal (deduped per system per day).

    `at` is the signal's real observation time (ISO); defaults to
    now. Returns True only when a new row was written.

    W-check B4 (AI engineer / adversarial QA): the intake was
    undefended — a FUTURE `at` (or even "zzzz") satisfied the
    window's string comparison and fired notifications for events
    that had not happened; a non-string `at` raised TypeError
    outside the guarded region; and a signal with BOTH ids None
    could be written but was invisible to every window query while
    its dedupe never matched, piling up ghost rows. Now: `at` must
    be a parseable ISO string (anything else is refused), a future
    `at` is clamped to now (clock skew is real; prophecy is not),
    and at least one of edition_id / candidate_id is required.
    The failure path also uses a SAVEPOINT — the old full
    db.rollback() could erase a caller's uncommitted registration
    work mid-loop.
    """
    if system not in KNOWN_SYSTEMS:
        raise ValueError(f"unknown repress signal system: {system}")
    if edition_id is None and candidate_id is None:
        return False
    # R19 (AI engineer / adversarial QA): the clamp and the window
    # compared STRINGS, so the same instant written with a non-JST
    # offset ("2026-11-08T12:07+00:00" = 21:07 JST) sorted as
    # 'past' while being hours in the future, and one instant in
    # two representations deduped as two different days. The value
    # is now parsed to an aware datetime, compared as an instant,
    # and STORED canonicalised to JST — one instant, one string.
    was_in_tx = db.in_transaction
    now_dt = clock.now(db)
    now_iso = clock.to_iso(now_dt)
    if at is None:
        at = now_iso
    if not isinstance(at, str):
        return False
    try:
        from datetime import datetime
        at_dt = datetime.fromisoformat(at)
    except ValueError:
        return False
    if at_dt.tzinfo is None:
        at_dt = at_dt.replace(tzinfo=clock.JST)
    if at_dt > now_dt:
        at_dt = now_dt
    at = clock.to_iso(at_dt)
    day = at[:10]
    try:
        db.execute("SAVEPOINT grt_repress")
        rows = db.execute(
            "SELECT at FROM repress_signals WHERE system=? AND"
            " ((? IS NOT NULL AND edition_id=?) OR"
            "  (? IS NOT NULL AND candidate_id=?))",
            (system, edition_id, edition_id, candidate_id,
             candidate_id)).fetchall()
        if any((r[0] or "")[:10] == day for r in rows):
            db.execute("RELEASE SAVEPOINT grt_repress")
            return False
        db.execute(
            "INSERT INTO repress_signals(edition_id, candidate_id,"
            " system, detail, at) VALUES(?,?,?,?,?)",
            (edition_id, candidate_id, system, detail, at))
        db.execute("RELEASE SAVEPOINT grt_repress")
        # R19 (AI engineer / adversarial QA): the old code committed
        # even inside a caller's transaction, making the caller's
        # unit of work un-rollback-able (a fetcher's per-item
        # rollback silently kept the candidate this signal rode in
        # on). Standalone calls commit; nested calls leave the
        # commit to their caller — the same contract outbox learned
        # in B2.
        if not was_in_tx:
            db.commit()
        return True
    except Exception as exc:
        try:
            db.execute("ROLLBACK TO SAVEPOINT grt_repress")
            db.execute("RELEASE SAVEPOINT grt_repress")
        except Exception:
            pass
        try:
            db.execute(
                "INSERT INTO system_events(kind, detail_json,"
                " created_at) VALUES('repress_signal_failed', ?, ?)",
                (json.dumps({"system": system, "error": str(exc)}),
                 clock.now_iso(db)))
            if not was_in_tx:
                db.commit()
        except Exception:
            db.rollback()
        return False


def signal_announcement(db, *, event_stage: str,
                        edition_id: str | None = None,
                        candidate_id: int | None = None,
                        title: str = "") -> dict | None:
    """Producer: an announcement only counts at repress_announced."""
    if event_stage != "repress_announced":
        return None
    register_signal(db, system="announcement", edition_id=edition_id,
                    candidate_id=candidate_id,
                    detail="event_stage=repress_announced")
    return evaluate(db, edition_id=edition_id,
                    candidate_id=candidate_id, title=title)


def signal_variant_stock(db, *, edition_id: str, was: str, now: str,
                         title: str = "") -> dict | None:
    """Producer: only a sold_out -> in_stock transition counts."""
    if not (was == "sold_out" and now == "in_stock"):
        return None
    register_signal(db, system="variant_stock", edition_id=edition_id,
                    detail="variant_stock sold_out->in_stock")
    return evaluate(db, edition_id=edition_id, title=title)


def _window_start(db) -> str:
    return (clock.now(db) - timedelta(days=WINDOW_DAYS)).isoformat()


def systems_in_window(db, *, edition_id: str | None = None,
                      candidate_id: int | None = None) -> list[str]:
    """COUNTED systems with signals inside the 30-day window.

    W-check B4: the window had a lower bound only — a signal dated
    in the future (or garbage that sorts high) counted as 'in
    window' for as long as it took real time to catch up. The
    upper bound is now."""
    since = _window_start(db)
    now_iso = clock.now_iso(db)
    placeholders = ",".join("?" for _ in COUNTED_SYSTEMS)
    if edition_id is not None:
        rows = db.execute(
            f"SELECT DISTINCT system FROM repress_signals WHERE"
            f" edition_id=? AND at>=? AND at<=? AND system IN"
            f" ({placeholders})",
            (edition_id, since, now_iso, *COUNTED_SYSTEMS)).fetchall()
    else:
        # R19 (AI engineer): the candidate window counts only
        # candidate-ONLY rows. A bridged/migrated row carries both
        # keys for one work; counting it in the candidate window
        # too let one work fire repress_suspected twice (once per
        # key space) off a single shared announcement.
        rows = db.execute(
            f"SELECT DISTINCT system FROM repress_signals WHERE"
            f" candidate_id=? AND edition_id IS NULL AND at>=?"
            f" AND at<=? AND system IN ({placeholders})",
            (candidate_id, since, now_iso, *COUNTED_SYSTEMS)).fetchall()
    return sorted(r[0] for r in rows)


def evaluate(db, *, edition_id: str | None = None,
             candidate_id: int | None = None,
             title: str = "") -> dict:
    """Fire repress_suspected iff >=2 counted systems in window."""
    systems = systems_in_window(db, edition_id=edition_id,
                                candidate_id=candidate_id)
    suspected = len(systems) >= 2
    notified = False
    if suspected:
        key_id = edition_id or f"cand:{candidate_id}"
        # R14 (adversarial 中-3): this title is external text (feed
        # titles, shop anchor text) and was the last notification
        # sink composing it verbatim — newlines could inject fake
        # alert lines into the body. one_line, like everywhere else.
        from .normalize import one_line
        body = (f"REPRESS疑い: {one_line(title) or key_id}\n"
                f"根拠（{WINDOW_DAYS}日以内・{len(systems)}系統）: "
                + " / ".join(systems))
        row_id = outbox_mod.enqueue(
            db, event_type="repress_suspected", body=body,
            edition_id=edition_id,
            idempotency_key=f"repress|{key_id}|" + ",".join(systems))
        notified = row_id is not None
    return {"systems": systems, "suspected": suspected,
            "notified": notified}


def migrate_candidate_signals(db, candidate_id: int,
                              edition_id: str) -> int:
    """Re-key a candidate's signals onto its catalogued edition
    (W-check B4, AI engineer / seller): announcement signals lived
    in the candidate ID space while variant_stock lives in the
    edition space, and the two could never combine into the
    2-system threshold for one work. Called when a candidate is
    catalogued (approval) — the candidate_id is kept on the rows
    for audit, so the edition window now sees them."""
    cur = db.execute(
        "UPDATE repress_signals SET edition_id=? WHERE candidate_id=?"
        " AND (edition_id IS NULL OR edition_id!=?)",
        (edition_id, candidate_id, edition_id))
    return cur.rowcount or 0


def digest_notes(db) -> list[str]:
    """Signals below the threshold: digest-only lines (#4).

    Includes discogs_stats appearances, which are evidence of
    market movement but never of a repress by themselves.

    W-check B4 (seller): the lines were identical for the whole
    30-day window (no date, so day 0 and day 29 read the same),
    rejected candidates' signals kept printing, and id-less ghost
    rows produced a 'cand:None' noise line. Notes now carry the
    latest signal date, skip id-less rows, and skip candidates the
    human already rejected."""
    since = _window_start(db)
    now_iso = clock.now_iso(db)
    # R19 (seller / AI engineer): grouping by the (edition,
    # candidate) PAIR split one work into two '様子見' lines after
    # a signal migration (the migrated row kept its candidate_id),
    # and a work whose notification had already fired kept
    # appearing as below-threshold in the other group. Grouping is
    # by WORK: the edition key when a row has one, else the
    # candidate key.
    rows = db.execute(
        "SELECT edition_id, candidate_id, system, at FROM"
        " repress_signals WHERE at>=? AND at<=?"
        " AND (edition_id IS NOT NULL OR candidate_id IS NOT NULL)",
        (since, now_iso)).fetchall()
    groups: dict[str, dict] = {}
    for edition_id, candidate_id, system, at in rows:
        key = edition_id or f"cand:{candidate_id}"
        g = groups.setdefault(key, {"systems": set(), "latest": "",
                                    "candidate_id": candidate_id,
                                    "edition_id": edition_id})
        g["systems"].add(system)
        if (at or "") > g["latest"]:
            g["latest"] = at or ""
    notes = []
    for key, g in groups.items():
        sys_list = sorted(g["systems"])
        counted = [s for s in sys_list if s in COUNTED_SYSTEMS]
        if len(counted) >= 2:
            continue  # already an instant notification, not a note
        if g["edition_id"] is None and g["candidate_id"] is not None:
            st = db.execute(
                "SELECT status FROM candidate_editions WHERE id=?",
                (g["candidate_id"],)).fetchone()
            if st is not None and st[0] == "rejected":
                continue
        # W-check B5 (collector 軽微-2): the note named only the
        # bare ID; the B4 date addition still left a lookup
        # between the user and any judgement. Carry the title
        # when the key resolves to a catalogue row.
        from .normalize import one_line
        _title = ""
        if g["edition_id"]:
            _t = db.execute("SELECT title FROM editions WHERE"
                            " id=?", (g["edition_id"],)).fetchone()
            _title = one_line(_t[0]) if _t and _t[0] else ""
        elif g["candidate_id"] is not None:
            _t = db.execute(
                "SELECT title FROM candidate_editions WHERE id=?",
                (g["candidate_id"],)).fetchone()
            _title = one_line(_t[0]) if _t and _t[0] else ""
        # W-check B5 R24 (collector 軽微-ア): same 120-char cap
        # as the other notification titles.
        if len(_title) > 120:
            _title = _title[:120] + "…"
        notes.append(f"REPRESS信号（様子見）: {key}"
                     + (f" {_title}" if _title else "") + " — "
                     + " / ".join(sys_list)
                     + f"（最終 {g['latest'][:10]}）")
    return notes
