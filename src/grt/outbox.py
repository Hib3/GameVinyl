"""Notification outbox (agreement-08).

Detection and outbox insertion happen in the same transaction. Listing
event idempotency keys exclude price. Price-drop events fire only when the
price actually changed AND is >=10% below the last notified price (a
non-zero-difference check; there is no absolute-amount threshold).
Quiet holding uses the not_before column, never in-memory state.
Retries: 1min -> 5min -> 30min -> 2h -> 12h between attempts; the 6th
failure dead-letters (its count is reported in the daily digest and
/status).

W-check B2 rework (2026-11-08). The first version's state machine had
five distinct holes, each reproduced by the review personas:

- the 12h retry step was unreachable (5 attempts over 5 delays meant
  the 5th failure dead-lettered instead of scheduling the 12h wait);
- the claim was not atomic, so two overlapping delivery passes could
  send the same row twice;
- a row stuck in 'sending' inside a LIVE process (send succeeded,
  the 'sent' bookkeeping failed) was only recovered at the next
  restart, and recovery itself never consumed an attempt, so a
  crash-loop could resend one notification forever;
- quiet hours were judged only at enqueue time, so a delayed pass
  delivered straight into the quiet window;
- dead_letter was terminal and its idempotency key was never
  released, so the underlying event could never be notified again,
  and enqueue committed / rolled back the CALLER's transaction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, time as dtime, timedelta

from . import clock
from .config import get_setting
from .migrate import current_policy_version
from .policy import get_policy

RETRY_DELAYS_MIN = [1, 5, 30, 120, 720]
MAX_ATTEMPTS = 6

# One delivery pass works through at most this many rows; the rest
# wait for the next minute's pass (adversarial QA D-7: an unbounded
# pass over a large backlog blocked every other flush for up to the
# sum of all send timeouts).
BATCH_LIMIT = 50

# Telegram's sendMessage text limit is 4096 characters; bodies are
# split at line boundaries below this size (collector C-B2-2: an
# over-limit body used to fail every retry and dead-letter, taking
# that day's discovery digest with it — permanently).
CHUNK_LIMIT = 3900

# A 'sending' claim older than this inside a live process is an
# orphan (a send takes seconds; the transport timeout is 30s).
STALE_SENDING_MINUTES = 10


def _attempts_of(value) -> int:
    """Defensive attempts counter (W-check B5, AI engineer).

    The attempts column has INTEGER affinity, but a legacy or
    hand-edited row can hold non-numeric TEXT ('abc'). int() on it
    raised ValueError in BOTH the send-failure handler (aborting
    the whole delivery pass mid-loop) and recover_sending (the
    poison row aborted recovery before every later orphan, every
    pass, forever — the row never even dead-lettered). A value
    that cannot be read as a number is treated as 0 attempts; the
    row then lives or dies by the normal budget.

    W-check B5 R24 (AI engineer 中-A): the helper's own margins
    reproduced the SAME symptoms in a different value class —
    int(float('inf')) raises OverflowError (not caught, and the
    send-failure handler calls this helper OUTSIDE its inner try,
    so one Inf row aborted the whole delivery pass), and a
    negative count passed through and later indexed
    RETRY_DELAYS_MIN[attempts - 1] (IndexError, rolling back the
    whole recovery pass). The helper is now total: it never
    raises, non-finite/unreadable values read as 0, and the
    result is clamped to >= 0."""
    try:
        n = int(value or 0)
    except Exception:
        return 0
    return max(0, n)


def _has_column(db, table: str, column: str) -> bool:
    try:
        cols = [r[1] for r in db.execute(
            f"PRAGMA table_info({table})").fetchall()]
        return column in cols
    except Exception:
        return False


def _ensure_reply_markup_column(db) -> bool:
    """Add ``reply_markup_json`` to notification_outbox when missing.

    schema.sql predates inline-button delivery through the outbox and is
    owned outside this change, so existing / test databases created from
    it lack the column. Adding it lazily here keeps the outbox the single
    place that knows buttons travel with a queued message. Returns True
    when the column is usable.
    """
    try:
        if not _has_column(db, "notification_outbox", "reply_markup_json"):
            db.execute(
                "ALTER TABLE notification_outbox"
                " ADD COLUMN reply_markup_json TEXT")
            db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _ensure_column(db, column: str, decl: str) -> bool:
    """Lazy-ALTER helper for the columns the B2 rework added
    (``claimed_at`` — when a 'sending' claim was taken;
    ``quiet_exempt`` — the producer asked for quiet exemption)."""
    try:
        if not _has_column(db, "notification_outbox", column):
            db.execute(
                f"ALTER TABLE notification_outbox ADD COLUMN {column} {decl}")
            db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def candidate_review_markup(candidate_id) -> dict:
    """Inline buttons for a candidate review (9-C22).

    The callback_data format is the one bot.py already handles on the
    receiving side: ``approve:<id>`` / ``reject:<id>``.
    """
    cid = int(candidate_id)
    return {"inline_keyboard": [[
        {"text": "承認", "callback_data": f"approve:{cid}"},
        {"text": "却下", "callback_data": f"reject:{cid}"},
    ]]}


def _url_key_part(url: str) -> str:
    """Idempotency-key form of a URL.

    W-check B2 (maniac B D-11): the URL used to go through
    ``normalize_title``, which strips hyphens/dots and upper-cases —
    title normalisation applied to a URL makes DIFFERENT listings
    collide (``AB-123`` vs ``ab123`` vs ``AB.123``). Only the scheme
    and host are case-folded; the path is case-sensitive and kept
    verbatim.
    """
    u = (url or "").strip()
    if "://" in u:
        scheme, rest = u.split("://", 1)
        host, sep, path = rest.partition("/")
        return scheme.lower() + "://" + host.lower() + (sep + path if sep
                                                        else "")
    return u


def listing_event_key(edition_id: str, source: str, listing_url: str,
                      event_type: str) -> str:
    return f"{edition_id}|{source}|{_url_key_part(listing_url)}|{event_type}"


def _parse_hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _quiet_window(db) -> tuple[dtime, dtime]:
    pol = get_policy(db).get("quiet") or {}
    try:
        return _parse_hm(pol["start"]), _parse_hm(pol["end"])
    except (KeyError, TypeError, ValueError, AttributeError):
        # Malformed quiet values must not kill enqueueing: fall back to
        # the default quiet window (debugger-14 finding, 2026-11-08).
        return _parse_hm("22:00"), _parse_hm("06:30")


def quiet_window_end(db, when=None):
    """If `when` is inside quiet hours, the datetime quiet ends; else None.

    Handles both window shapes: overnight (start > end, the default
    22:00-06:30) and same-day (start < end). The first version inlined
    this math inside quiet_not_before with an ``if t >= start: +1 day``
    step that was only correct for the overnight shape — a same-day
    band like 13:00-14:00 pushed not_before a full day out (maniac B
    D-7). Both enqueue-time holding and the delivery-time re-check
    (process_outbox) go through this one function now.
    """
    now = when or clock.now(db)
    start, end = _quiet_window(db)
    if start == end:
        return None
    t = now.time()
    if start > end:  # overnight window
        if t >= start:
            return datetime.combine(now.date() + timedelta(days=1), end,
                                    tzinfo=clock.JST)
        if t < end:
            return datetime.combine(now.date(), end, tzinfo=clock.JST)
        return None
    # same-day window
    if start <= t < end:
        return datetime.combine(now.date(), end, tzinfo=clock.JST)
    return None


def quiet_not_before(db, when=None, exempt: bool = False):
    """If `when` falls inside quiet hours, return the quiet end; else when."""
    now = when or clock.now(db)
    if exempt:
        return now
    end = quiet_window_end(db, now)
    return end if end is not None else now


def _record_enqueue_failure(db, event_type: str, exc: BaseException) -> None:
    """Best-effort visibility for an enqueue that failed operationally.

    enqueue returns None both for a genuine duplicate and for a real
    failure; without this record the two were indistinguishable and a
    notification could vanish silently (seller D-5 / maniac B D-12).
    """
    try:
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('outbox_enqueue_failed', ?, ?)",
            (json.dumps({"event_type": event_type,
                         "error": str(exc)[:300]}, ensure_ascii=False),
             clock.now_iso(db)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _revive_dead_letter(db, existing_id: int, *, body, score,
                        floor_applied, not_before, quiet_override,
                        quiet_override_reason, price, policy_version,
                        markup_json, quiet_exempt) -> int:
    """Reset a dead-lettered row for the same idempotency key back to
    pending with the fresh payload (collector C-B2-3 / maniac B
    D-9(iii)): a long outage used to dead-letter everything queued
    during it, and because the key stayed taken, re-detecting the
    event afterwards returned 'duplicate' and the notification was
    lost forever even though the underlying item (an unreviewed
    candidate, an approaching deadline) still existed."""
    cols = ("state='pending', attempts=0, next_attempt_at=NULL,"
            " claimed_at=NULL, sent_at=NULL, last_error=NULL,"
            " body=?, priority_score=?, floor_applied=?, not_before=?,"
            " quiet_override=?, quiet_override_reason=?,"
            " last_notified_price=?, policy_version=?, quiet_exempt=?")
    params = [body, score, 1 if floor_applied else 0,
              clock.to_iso(not_before), 1 if quiet_override else 0,
              quiet_override_reason, price, policy_version,
              1 if quiet_exempt else 0]
    if markup_json is not None and _has_column(db, "notification_outbox",
                                               "reply_markup_json"):
        cols += ", reply_markup_json=?"
        params.append(markup_json)
    params.append(existing_id)
    db.execute(f"UPDATE notification_outbox SET {cols} WHERE id=?", params)
    try:
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('outbox_dead_letter_revived', ?, ?)",
            (json.dumps({"outbox_id": existing_id}, ensure_ascii=False),
             clock.now_iso(db)))
    except Exception:
        pass
    # No commit here (R9 collector N-1): revival runs inside the
    # caller's transaction, exactly like the plain-insert path —
    # committing here used to flush the CALLER's unrelated
    # uncommitted work early, breaking the D-8 contract for this
    # path only.
    return existing_id


def enqueue(db, *, event_type: str, body: str, edition_id: str | None = None,
            idempotency_key: str | None = None, source: str | None = None,
            listing_url: str | None = None, score: int | None = None,
            floor_applied: bool = False, price: float | None = None,
            quiet_exempt: bool = False, quiet_override: bool = False,
            quiet_override_reason: str | None = None,
            reply_markup: dict | None = None) -> int | None:
    """Insert one outbox row. Returns the row id, or None on duplicate key.

    Failure contract (maniac B D-12): None means 'duplicate — or a
    recorded failure'. Operational failures are written to
    system_events as ``outbox_enqueue_failed`` before returning None;
    a genuine duplicate writes nothing.

    Transaction contract (maniac B D-8): the insert runs inside a
    SAVEPOINT, so a duplicate no longer rolls back the CALLER's
    uncommitted work (storewatch's page-state update was silently
    undone by exactly that), and a successful enqueue no longer
    commits the caller's work early. 'Detection and outbox insertion
    happen in the same transaction' is now literally true.
    """
    try:
        return _enqueue_inner(
            db, event_type=event_type, body=body, edition_id=edition_id,
            idempotency_key=idempotency_key, source=source,
            listing_url=listing_url, score=score,
            floor_applied=floor_applied, price=price,
            quiet_exempt=quiet_exempt, quiet_override=quiet_override,
            quiet_override_reason=quiet_override_reason,
            reply_markup=reply_markup)
    except Exception as exc:
        # R14 (adversarial 中-4): this used to db.rollback() — a FULL
        # rollback that silently erased the CALLER's uncommitted
        # work (blipblop's register loop commits at the end, so one
        # enqueue failure mid-loop wiped the items registered before
        # it while their counters said 'created'). The inner
        # operation's own work is bounded by its SAVEPOINT (rolled
        # back inside _enqueue_inner / its handlers); anything that
        # escaped before the savepoint was established wrote nothing
        # to the outbox. The caller's transaction is theirs.
        try:
            db.execute("ROLLBACK TO grt_enqueue")
            db.execute("RELEASE grt_enqueue")
        except Exception:
            pass  # no savepoint was live; nothing of ours to undo
        _record_enqueue_failure(db, event_type, exc)
        return None


def _enqueue_inner(db, *, event_type, body, edition_id, idempotency_key,
                   source, listing_url, score, floor_applied, price,
                   quiet_exempt, quiet_override, quiet_override_reason,
                   reply_markup) -> int | None:
    if idempotency_key is None:
        if edition_id and source and listing_url:
            idempotency_key = listing_event_key(edition_id, source,
                                                listing_url, event_type)
        else:
            # W-check B2 (maniac B D-10): the fallback key used to be
            # second-precision, so two different keyless events in the
            # same second collided and the second vanished as a
            # 'duplicate'. The body hash keeps them distinct.
            digest = hashlib.sha1(
                (body or "").encode("utf-8", "replace")).hexdigest()[:12]
            idempotency_key = (f"{event_type}|{edition_id or ''}|"
                               f"{clock.now_iso(db)}|{digest}")
    nb = quiet_not_before(db, exempt=quiet_exempt or quiet_override)
    markup_json = (json.dumps(reply_markup, ensure_ascii=False)
                   if reply_markup else None)
    # One-time lazy ALTERs run BEFORE the savepoint (an ALTER commits,
    # which would break the savepoint isolation). Steady state: both
    # columns exist and these are read-only PRAGMA checks.
    if markup_json and not _has_column(db, "notification_outbox",
                                       "reply_markup_json"):
        _ensure_reply_markup_column(db)
    if not _has_column(db, "notification_outbox", "quiet_exempt"):
        _ensure_column(db, "quiet_exempt", "INTEGER DEFAULT 0")
    has_markup_col = markup_json is not None and _has_column(
        db, "notification_outbox", "reply_markup_json")
    policy_version = current_policy_version(db)

    savepoint = False
    try:
        db.execute("SAVEPOINT grt_enqueue")
        savepoint = True
    except Exception:
        savepoint = False
    try:
        if has_markup_col:
            cur = db.execute(
                """INSERT INTO notification_outbox(
                     idempotency_key, edition_id, event_type, body,
                     priority_score, floor_applied, not_before, state,
                     attempts, quiet_override, quiet_override_reason,
                     last_notified_price, policy_version, reply_markup_json,
                     quiet_exempt)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
                (idempotency_key, edition_id, event_type, body, score,
                 1 if floor_applied else 0, clock.to_iso(nb),
                 1 if quiet_override else 0, quiet_override_reason, price,
                 policy_version, markup_json, 1 if quiet_exempt else 0))
        else:
            cur = db.execute(
                """INSERT INTO notification_outbox(
                     idempotency_key, edition_id, event_type, body, priority_score,
                     floor_applied, not_before, state, attempts, quiet_override,
                     quiet_override_reason, last_notified_price, policy_version,
                     quiet_exempt)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
                (idempotency_key, edition_id, event_type, body, score,
                 1 if floor_applied else 0, clock.to_iso(nb),
                 1 if quiet_override else 0, quiet_override_reason, price,
                 policy_version, 1 if quiet_exempt else 0))
        row_id = cur.lastrowid
        if savepoint:
            db.execute("RELEASE grt_enqueue")
        else:
            db.commit()
        return row_id
    except sqlite3.IntegrityError:
        # Duplicate idempotency key — the expected suppression path,
        # unless the existing row is a dead letter, in which case the
        # event gets a second life with the fresh payload.
        if savepoint:
            try:
                db.execute("ROLLBACK TO grt_enqueue")
                db.execute("RELEASE grt_enqueue")
            except Exception:
                pass
        else:
            db.rollback()
        existing = db.execute(
            "SELECT id, state FROM notification_outbox"
            " WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None and existing[1] == "dead_letter":
            return _revive_dead_letter(
                db, int(existing[0]), body=body, score=score,
                floor_applied=floor_applied, not_before=nb,
                quiet_override=quiet_override,
                quiet_override_reason=quiet_override_reason, price=price,
                policy_version=policy_version, markup_json=markup_json,
                quiet_exempt=quiet_exempt)
        return None


def enqueue_candidate_review(db, candidate_id, *, title: str | None = None,
                             source_url: str | None = None,
                             body: str | None = None,
                             idem_suffix: str = "") -> int | None:
    """Queue ONE button-carrying review message for a candidate (9-C22/C4).

    The message is only *queued* in the outbox: actual delivery still
    goes through process_outbox, which the resident process runs only
    after the initial-setup gate opens (main.outbox_job gate rule), and
    quiet-hours holding still applies via not_before. The idempotency
    key is fixed per candidate, so re-registering / re-sweeping the same
    candidate never queues a second review message.
    """
    # R19 (adversarial QA): int() coercion here silently mapped
    # True -> candidate 1, truncated 1.9 -> 1, and raised
    # OverflowError for 10**30 — the strict shape the B4 sweep
    # (enqueue_pending_candidate_reviews) already enforced now
    # applies at this boundary too. Not a real int -> no message.
    if not isinstance(candidate_id, int) \
            or isinstance(candidate_id, bool) \
            or abs(candidate_id) >= 2 ** 63:
        return None
    cid = int(candidate_id)
    if title is None or source_url is None:
        row = db.execute(
            "SELECT title, raw_title, source_url, existence_confidence,"
            " price_confidence FROM candidate_editions WHERE id=?",
            (cid,)).fetchone()
        if row is None:
            # No such candidate: never queue a review for a phantom id.
            return None
        if row is not None:
            if title is None:
                title = row["raw_title"] or row["title"]
            if source_url is None:
                source_url = row["source_url"]
            if body is None:
                # W-check B3 (adversarial 6): the title is external
                # text — one-line it before it enters a body.
                # W-check B4 (adversarial): one_line collapses
                # newlines but not LENGTH — a 100k-char title split
                # one review message into 26 chunks. Display titles
                # are capped; the full title stays in the DB.
                from .normalize import one_line
                disp = one_line(title)
                if len(disp) > 120:
                    disp = disp[:120] + "…"
                body = ("【候補レビュー】候補#" + str(cid) + " "
                        + disp + "\n"
                        + "確信度: 存在・" + str(row["existence_confidence"] or "-")
                        + " / 価格・" + str(row["price_confidence"] or "-") + "\n"
                        + "リンク: " + str(source_url or "") + "\n"
                        + "承認／却下は下のボタンから行えます"
                        "（コマンドでも同じ候補を処理できます）。")
    if body is None:
        from .normalize import one_line
        disp = one_line(title)
        if len(disp) > 120:
            disp = disp[:120] + "…"
        body = ("【候補レビュー】候補#" + str(cid) + " " + disp
                + "\nリンク: " + str(source_url or ""))
    # W-check B4: a candidate REOPENED after rejection (independent
    # corroboration arrived) must notify again — the original key
    # is spent, so the reopen path passes idem_suffix='|reopen'.
    return enqueue(
        db, event_type="candidate_review", body=body,
        idempotency_key="candidate_review|" + str(cid) + idem_suffix,
        reply_markup=candidate_review_markup(cid))


def enqueue_pending_candidate_reviews(db) -> int:
    """Queue review messages for candidate_review rows not yet notified.

    Covers review_queue rows inserted by any producer (not only the
    Blip Blop fetcher). Idempotent: returns the number of *new* outbox
    rows; a second sweep for the same candidates queues nothing.
    """
    created = 0
    rows = db.execute(
        "SELECT payload_json FROM review_queue"
        " WHERE kind='candidate_review' AND resolved_at IS NULL"
        " ORDER BY id").fetchall()
    for r in rows:
        try:
            payload = json.loads(r[0] or "{}")
            if isinstance(payload, dict) and payload.get("notify") is False:
                continue
            cid = payload.get("candidate_id") \
                if isinstance(payload, dict) else None
            # W-check B4 (adversarial): this sweep's type discipline
            # was looser than the bot's — int(True) silently became
            # candidate 1, 1.9 truncated to 1, and 10**30 raised
            # OverflowError OUTSIDE the guard, aborting the sweep
            # for every row behind the poisoned one. Strict shape:
            # a real int (not bool), inside sqlite's int64 range.
            if not isinstance(cid, int) or isinstance(cid, bool) \
                    or abs(cid) >= 2 ** 63:
                continue
        except Exception:
            # A malformed payload (non-numeric / missing candidate_id,
            # a JSON value that is not an object, ...) must not abort
            # the whole sweep: skip that row and keep sweeping.
            continue
        try:
            if enqueue_candidate_review(db, cid) is not None:
                created += 1
        except Exception:
            continue  # one bad row never stops the safety net
    return created


def price_drop_qualifies(db, edition_id: str, new_price: float) -> bool:
    """Deprecated legacy rule kept only as a forwarding shim.

    The bucket-respecting rule (agreement-11: comparisons only within
    the same Edition x variant x condition bucket) is
    ``grt.scoring.price_drop_qualifies_bucketed``. This edition-only
    version ignores buckets and must not be wired to notifications;
    it currently has no callers and remains only so any stale import
    fails safe (returns False) instead of silently notifying.
    """
    return False


def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split a body into Telegram-sized chunks at line boundaries.

    A single line longer than the limit is hard-split. Chunking (not
    truncation) is the fix for C-B2-2: the discovery digest is
    specified to show every candidate, so cutting it off would lose
    information — the parts together carry the full body.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _send_row(transport, chat_id, row) -> None:
    """Send one row (in chunks when over the Telegram limit),
    attaching its stored reply_markup to the LAST chunk when present.

    Transports without reply_markup support (older test doubles taking
    only (chat_id, text)) fall back to a plain send on TypeError, the
    same convention bot.Bot._send already uses. If a middle chunk
    fails, the row is retried as a whole on the next pass — earlier
    chunks may repeat; that is the at-least-once cost of not tracking
    per-chunk state.
    """
    markup = None
    try:
        raw = row["reply_markup_json"]
    except Exception:
        raw = None
    if raw:
        try:
            markup = json.loads(raw)
        except Exception:
            markup = None
    parts = split_text(row["body"] or "")
    markup_supported = True
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        if markup and is_last and markup_supported:
            try:
                transport.send(chat_id, part, reply_markup=markup)
                continue
            except TypeError:
                markup_supported = False
        transport.send(chat_id, part)


def recover_sending(db, *, stale_minutes: int | None = None) -> int:
    """Requeue outbox rows left in 'sending'.

    Two callers: startup (``stale_minutes=None`` — no sender can be
    running yet, so every 'sending' row is orphaned by construction)
    and process_outbox (``stale_minutes=STALE_SENDING_MINUTES`` — a
    claim older than that inside a live process is orphaned: the send
    finished or died but its bookkeeping never landed; collector
    C-B2-5 / maniac B D-2).

    W-check B2 (maniac B D-5): recovery used to reset rows to
    'pending, attempts untouched', so a fault that kills the process
    after Telegram accepted the message but before the state write
    looped forever — one duplicate per restart, no budget ever
    exhausted. Recovery now CONSUMES an attempt in both paths; a row
    that exhausts its attempts dead-letters like any other failure.
    The two paths differ only in timing: startup recovery redelivers
    immediately (a restarted bot should drain its queue, and the
    attempt budget is the bound that matters), while in-process
    recovery also applies the retry backoff so a flapping sender
    cannot hot-loop inside a healthy process.
    """
    now = clock.now(db)
    if stale_minutes is None:
        rows = db.execute(
            "SELECT id, attempts FROM notification_outbox"
            " WHERE state='sending' ORDER BY id").fetchall()
    else:
        if not _has_column(db, "notification_outbox", "claimed_at"):
            return 0
        cutoff = clock.to_iso(now - timedelta(minutes=stale_minutes))
        rows = db.execute(
            "SELECT id, attempts FROM notification_outbox"
            " WHERE state='sending' AND claimed_at IS NOT NULL"
            " AND claimed_at<=? ORDER BY id", (cutoff,)).fetchall()
    if not rows:
        return 0
    ids = []
    skipped = []
    for r in rows:
        # B5: per-row containment with the defensive counter — one
        # corrupt row must not abort the recovery of every later
        # orphan (it did: the loop died on int('abc') each pass).
        # R24: a skipped row is RECORDED in the recovery event
        # below — a silent skip left a permanently wedged row with
        # no trace anywhere.
        try:
            attempts = _attempts_of(r[1]) + 1
        except Exception:
            skipped.append(int(r[0]))
            continue
        ids.append(int(r[0]))
        if attempts >= MAX_ATTEMPTS:
            db.execute(
                "UPDATE notification_outbox SET state='dead_letter',"
                " attempts=?, claimed_at=NULL,"
                " last_error='sending recovery: attempt budget exhausted'"
                " WHERE id=?", (attempts, int(r[0])))
        elif stale_minutes is None:
            # Startup: redeliver on the next pass.
            db.execute(
                "UPDATE notification_outbox SET state='pending',"
                " attempts=?, next_attempt_at=NULL, claimed_at=NULL,"
                " last_error='sending recovery: outcome was never recorded'"
                " WHERE id=?", (attempts, int(r[0])))
        else:
            nxt = now + timedelta(
                minutes=RETRY_DELAYS_MIN[attempts - 1])
            db.execute(
                "UPDATE notification_outbox SET state='failed_retryable',"
                " attempts=?, next_attempt_at=?, claimed_at=NULL,"
                " last_error='sending recovery: outcome was never recorded'"
                " WHERE id=?",
                (attempts, clock.to_iso(nxt), int(r[0])))
    detail = {"recovered_ids": ids, "count": len(ids),
              "skipped_ids": skipped,
              "recovered_at": clock.now_iso(db),
              "attempts_incremented": True}
    db.execute(
        "INSERT INTO system_events(kind, detail_json, created_at)"
        " VALUES('outbox_sending_recovered', ?, ?)",
        (json.dumps(detail, ensure_ascii=False), clock.now_iso(db)))
    db.commit()
    return len(ids)


def _row_is_quiet_exempt(row) -> bool:
    try:
        keys = row.keys()
    except Exception:
        keys = []
    for col in ("floor_applied", "quiet_override", "quiet_exempt"):
        if col in keys and row[col]:
            return True
    return False


def process_outbox(db, transport, chat_id: str | None = None) -> dict:
    """Attempt delivery of due rows. `transport` needs .send(chat_id, text).

    Fault containment (QC round 5): a DB error on one row (e.g. a write
    lock held by another connection past the busy timeout) must not kill
    the whole delivery pass or strand the row in 'sending'. The row is
    rolled back to its pre-pass state, counted under ``db_errors``, and
    the remaining rows are still attempted; the next pass retries it.

    W-check B2 additions: stale 'sending' rows are recovered first
    (with an attempt consumed); claims are atomic (state-guarded
    UPDATE + rowcount), so overlapping passes cannot double-send;
    quiet hours are re-judged per row at SEND time, not only at
    enqueue time; delivery order honours priority_score; a 429's
    retry_after overrides the retry schedule.
    """
    if chat_id is None:
        chat_id = get_setting(db, "allowed_chat_id")
    _ensure_reply_markup_column(db)
    _ensure_column(db, "claimed_at", "TEXT")
    _ensure_column(db, "quiet_exempt", "INTEGER DEFAULT 0")
    stats = {"sent": 0, "failed": 0, "dead_letter": 0, "skipped_no_chat": 0,
             "db_errors": 0, "deferred_quiet": 0, "claim_lost": 0,
             "recovered": 0}
    try:
        stats["recovered"] = recover_sending(
            db, stale_minutes=STALE_SENDING_MINUTES)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    # W-check B5 (AI engineer): a corrupt not_before / next_
    # attempt_at (non-ISO TEXT from a legacy or hand-edited row)
    # compares GREATER than every real timestamp in the selection
    # below ('garbage' > '2026-...'), so the row was held forever
    # with no error recorded anywhere. Unparseable values are
    # cleared here (the row becomes due; the repair is recorded).
    try:
        suspects = db.execute(
            "SELECT id, not_before, next_attempt_at FROM"
            " notification_outbox WHERE state IN ('pending',"
            " 'failed_retryable') AND (not_before IS NOT NULL"
            " OR next_attempt_at IS NOT NULL)").fetchall()
        for sid, nb, na in suspects:
            bad = []
            for col, val in (("not_before", nb),
                             ("next_attempt_at", na)):
                if val is None:
                    continue
                try:
                    clock.parse_iso(val)
                except Exception:
                    bad.append(col)
            if bad:
                sets = ", ".join(f"{c}=NULL" for c in bad)
                db.execute(
                    f"UPDATE notification_outbox SET {sets}"
                    " WHERE id=?", (sid,))
                db.execute(
                    "INSERT INTO system_events(kind, detail_json,"
                    " created_at) VALUES('outbox_time_repaired',"
                    " ?, ?)",
                    (json.dumps({"outbox_id": sid, "cleared": bad}),
                     clock.now_iso(db)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    now = clock.now(db)
    rows = db.execute(
        "SELECT * FROM notification_outbox WHERE state IN ('pending',"
        " 'failed_retryable') AND (not_before IS NULL OR not_before<=?)"
        " AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
        " ORDER BY COALESCE(priority_score, 0) DESC, id ASC LIMIT ?",
        (clock.to_iso(now), clock.to_iso(now), BATCH_LIMIT)).fetchall()
    for row in rows:
        if not chat_id:
            stats["skipped_no_chat"] += 1
            continue
        # Send-time quiet re-judgement (collector C-B2-6 / maniac B
        # D-6): a row enqueued at 21:55 whose delivery slipped past
        # 22:00, a pass that starts before quiet and crosses into it,
        # and a quiet window widened after enqueue must ALL hold the
        # row. Exempt rows (floor / override / producer-exempt) pass.
        if not _row_is_quiet_exempt(row):
            qend = quiet_window_end(db)
            if qend is not None:
                try:
                    db.execute(
                        "UPDATE notification_outbox SET not_before=?"
                        " WHERE id=? AND state IN ('pending',"
                        " 'failed_retryable')",
                        (clock.to_iso(qend), row["id"]))
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    stats["db_errors"] += 1
                stats["deferred_quiet"] += 1
                continue
        try:
            cur = db.execute(
                "UPDATE notification_outbox SET state='sending',"
                " claimed_at=? WHERE id=? AND state IN ('pending',"
                " 'failed_retryable')",
                (clock.now_iso(db), row["id"]))
            db.commit()
            if cur.rowcount != 1:
                # Another pass claimed (or resolved) this row first.
                stats["claim_lost"] += 1
                continue
        except Exception:
            # Locked/unwritable DB: leave the row pending for a later
            # pass instead of crashing the delivery loop.
            try:
                db.rollback()
            except Exception:
                pass
            stats["db_errors"] += 1
            continue
        try:
            _send_row(transport, chat_id, row)
        except BaseException as exc:  # delivery failure -> retry schedule
            attempts = _attempts_of(row["attempts"]) + 1
            try:
                if attempts >= MAX_ATTEMPTS:
                    db.execute(
                        "UPDATE notification_outbox SET state='dead_letter',"
                        " attempts=?, claimed_at=NULL, last_error=?"
                        " WHERE id=?",
                        (attempts, str(exc)[:500], row["id"]))
                    stats["dead_letter"] += 1
                else:
                    delay = RETRY_DELAYS_MIN[attempts - 1]
                    retry_after = getattr(exc, "retry_after", None)
                    if isinstance(retry_after, (int, float)) \
                            and retry_after > 0:
                        # The server's explicit instruction wins over
                        # the local schedule (adversarial QA D-5).
                        delay = max(delay, int(retry_after) // 60 + 1)
                    nxt = clock.now(db) + timedelta(minutes=delay)
                    db.execute(
                        "UPDATE notification_outbox SET"
                        " state='failed_retryable', attempts=?,"
                        " next_attempt_at=?, claimed_at=NULL, last_error=?"
                        " WHERE id=?",
                        (attempts, clock.to_iso(nxt), str(exc)[:500],
                         row["id"]))
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                stats["db_errors"] += 1
            stats["failed"] += 1
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            continue
        try:
            db.execute(
                "UPDATE notification_outbox SET state='sent', sent_at=?,"
                " claimed_at=NULL WHERE id=?",
                (clock.now_iso(db), row["id"]))
            db.commit()
        except Exception:
            # The send DID happen; only the bookkeeping failed. The
            # row stays 'sending' with claimed_at set, and the timed
            # recovery above requeues it (consuming an attempt)
            # instead of waiting for a restart (collector C-B2-5).
            try:
                db.rollback()
            except Exception:
                pass
            stats["db_errors"] += 1
            continue
        stats["sent"] += 1
    return stats


def dead_letter_count(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE state='dead_letter'"
    ).fetchone()[0]


def pending_backlog(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE state IN"
        " ('pending', 'failed_retryable', 'sending')").fetchone()[0]
