"""release_schedule operations (agreement-22, Phase 2 operation).

The integrated deadline model holds three things together: the
original text (deadline_raw), the timezone it was stated in
(deadline_tz), and the converted JST instant (deadline_jst).

Review fixes (2026-11-08, wholesaler persona — the first version
stored the caller's string unconverted, ignored deadline_kind, and
used the wrong stage set):

- deadline_jst is COMPUTED with zoneinfo: a naive local time plus a
  known deadline_tz is interpreted as wall time in that zone and
  converted to JST deterministically. An offset-carrying value is
  normalised to JST. No TZ -> deadline_jst is always NULL
  (acceptance #6), whatever the caller passed.
- The alert table is the spec's single table (C-10.3):
    hard_cutoff      7d digest / 72h instant / 24h instant
    guarantee_cutoff 7d digest / 72h digest / 24h digest (never
                     instant; the digest line carries the kind and
                     the "orders may continue" note)
    campaign_end     digest only
    unknown          never alerted; counted until a human resolves
  Each stage fires at most once (idempotency per stage); when the
  job was down across several stages, only the LATEST stage fires,
  so a late alert never wears a stale "72時間前" label.
- Arrival check vocabulary is the spec's: confirmed_arrived /
  not_found / unknown. A not_found row gets exactly ONE recheck at
  deadline + 7 days. Candidate-only rows (no edition to look up)
  record 'unknown', not a false not_found. The listing comparison
  uses the deadline instant, not the calendar date.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import clock
from . import outbox as outbox_mod

# (hours before deadline, mode) — mode: instant | digest
STAGES = {
    "hard_cutoff": [(168, "digest"), (72, "instant"), (24, "instant")],
    "guarantee_cutoff": [(168, "digest"), (72, "digest"),
                         (24, "digest")],
    "campaign_end": [(168, "digest")],
    "unknown": [],
}
RECHECK_AFTER_DAYS = 7


def _to_jst_iso(dt: datetime) -> str:
    return dt.astimezone(clock.JST).isoformat()


def _wall_time_ambiguous(aware: datetime, tz: ZoneInfo) -> bool:
    """True when the wall time is in a DST fold (exists twice) or a
    DST gap (does not exist). fold=0 would otherwise settle both
    silently, up to an hour wrong (W-check B1, wholesaler #3)."""
    fold0 = aware.replace(fold=0)
    fold1 = aware.replace(fold=1)
    if fold0.utcoffset() != fold1.utcoffset():
        return True
    roundtrip = fold0.astimezone(clock.JST).astimezone(tz)
    return roundtrip.replace(tzinfo=None) != aware.replace(tzinfo=None)


def _offset_label(dt: datetime) -> str:
    """'+09:00'-style label for an offset-carrying datetime — what
    deadline_tz records when the original text carried its own
    offset (W-check B4, adversarial: the button's tz name used to
    be stored while the conversion actually used the text's
    offset, leaving the three-point set internally inconsistent).
    """
    off = dt.utcoffset()
    if off is None:
        return ""
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def set_schedule(db, *, edition_id: str | None = None,
                 candidate_id: int | None = None, channel: str = "",
                 deadline_raw: str = "", deadline_tz: str | None = None,
                 deadline_jst: str | None = None,
                 deadline_confidence: str = "tz_unknown",
                 deadline_kind: str = "unknown",
                 deadline_kind_source_url: str = "") -> int:
    """Insert one schedule row under the TZ discipline.

    deadline_jst semantics: when deadline_tz is known, a naive
    value is read as wall time in deadline_tz and converted via
    zoneinfo; an offset-carrying value is normalised to JST. When
    deadline_tz is unknown the value is discarded (NULL).

    W-check B4 additions (maniac A / adversarial QA):
    - kind vocabulary: a kind outside STAGES is filed 'unknown'
      (an unvalidated kind was alert-invisible in staged/digest
      yet sweep-visible — asymmetric silence);
    - a 'confirmed' confidence with NO computable instant is
      downgraded to 'tz_unknown' — such a row was invisible to
      every consumer AND to the unconfirmed counts;
    - supersede: a live row for the same (target, channel, kind)
      is replaced, not duplicated. Postponements are the most
      common deadline change; the old always-INSERT left the stale
      date alerting. The replaced row is marked superseded_by and
      written to deadline_history. Re-setting the SAME instant is
      a no-op returning the existing row's id.

    W-check B4 R19 additions (AI engineer / adversarial QA /
    collector / seller):
    - an instant-less re-registration NEVER supersedes a row that
      HAS an instant: a vague re-observation ("近日中", TZ omitted)
      used to silently destroy a confirmed deadline. The vague row
      is still recorded (visible as unconfirmed, tzfix-able); the
      confirmed row keeps alerting.
    - the same-instant no-op is slot-scoped (same channel):
      a different channel is a distinct registration; double
      alerts across channels are prevented at the alert layer
      (sibling-stage skip in staged_alerts / deadline_sweep).
    - rows with BOTH ids None have no slot identity: they never
      supersede and are never superseded (two unrelated id-less
      registrations used to replace each other silently).
    - a same-instant no-op also supersedes any OTHER live
      duplicates in the slot (legacy double rows), keeping the
      returned row as the single live one.
    - transaction discipline: when the caller already holds an
      open transaction, set_schedule does not commit it (the
      caller owns its unit of work); standalone calls commit.
    """
    was_in_tx = db.in_transaction
    if deadline_kind not in STAGES:
        deadline_kind = "unknown"
    jst_value = None
    recorded_tz = deadline_tz
    # W-check B5 R24 (wholesaler 軽微-C): an offset-carrying
    # deadline_jst determines its own instant, but the old gate
    # required deadline_tz as well and silently discarded such
    # values to tz_unknown. A NAIVE value still needs the tz
    # argument; without one it stays instant-less, as before.
    _parsed0 = _parse_any(deadline_jst) if deadline_jst else None
    if deadline_jst and (deadline_tz or (
            _parsed0 is not None and _parsed0.tzinfo is not None)):
        parsed = _parsed0
        if parsed is not None:
            if parsed.tzinfo is None:
                try:
                    tz = ZoneInfo(deadline_tz)
                    parsed = parsed.replace(tzinfo=tz)
                except Exception:
                    parsed = None
                else:
                    # W-check B1 (wholesaler #3): see
                    # _wall_time_ambiguous — DST fold/gap wall times
                    # are filed 'ambiguous', never silently settled.
                    # W-check B5 (adversarial QA): the ambiguity
                    # probe and the JST conversion do timezone
                    # arithmetic that OVERFLOWS at the datetime
                    # range edge (year 1 minus an LMT offset goes
                    # below year 1) — /deadline 0001-01-01 died with
                    # no reply at all. An unconvertible instant is
                    # no instant: the row files as unconfirmed
                    # instead of raising out of the producer.
                    try:
                        if _wall_time_ambiguous(parsed, tz):
                            deadline_confidence = "ambiguous"
                    except (OverflowError, ValueError, OSError):
                        parsed = None
            else:
                recorded_tz = _offset_label(parsed)
            if parsed is not None:
                try:
                    jst_value = _to_jst_iso(parsed)
                except (OverflowError, ValueError, OSError):
                    jst_value = None
    if jst_value is None and deadline_confidence == "confirmed":
        deadline_confidence = "tz_unknown"
    has_target = edition_id is not None or candidate_id is not None
    existing = []
    if has_target:
        existing = db.execute(
            "SELECT * FROM release_schedule WHERE edition_id IS ?"
            " AND candidate_id IS ? AND channel=? AND deadline_kind=?"
            " AND superseded_by IS NULL",
            (edition_id, candidate_id, channel,
             deadline_kind)).fetchall()
    # Same instant in the SAME slot is a no-op (handled by the
    # loop below over `existing`, which is slot-scoped). A same-
    # instant row in a DIFFERENT channel is a distinct
    # registration — channels are separate commitments (the
    # arrival-check model relies on this). The double-ALERT risk
    # that pairing creates is handled at the alert layer:
    # staged_alerts and deadline_sweep skip a row when a same-
    # target, same-instant, same-kind sibling already alerted.
    for old in existing:
        if old["deadline_jst"] == jst_value:
            keep = int(old["id"])
            for other in existing:
                if int(other["id"]) != keep:
                    _mark_superseded(db, other, keep)
            if not was_in_tx:
                db.commit()
            return keep
    # W-check B5 R24 (wholesaler 中-A): cancellation is a
    # TOMBSTONE. A cancelled row is self-superseded, hence
    # invisible to the slot lookup above — so the doujin lane's
    # next routine harvest re-registered the very deadline the
    # user had cancelled, and it resumed alerting (while the
    # cancel reply had promised it would not). An automatic
    # re-registration of the SAME instant for the same target +
    # kind is suppressed while a tombstone stands; an explicit
    # manual registration (channel 'manual', i.e. the user
    # re-entering it via /deadline) is the deliberate revival
    # path and proceeds. The suppression is recorded once per
    # tombstone.
    if jst_value is not None and has_target and channel != "manual":
        tomb = db.execute(
            "SELECT id FROM release_schedule WHERE"
            " edition_id IS ? AND candidate_id IS ?"
            " AND deadline_kind=? AND deadline_jst=?"
            " AND superseded_by=id",
            (edition_id, candidate_id, deadline_kind,
             jst_value)).fetchone()
        if tomb is not None:
            try:
                from .config import get_setting
                _tkey = f"deadline_tombstone_seen|{int(tomb[0])}"
                if get_setting(db, _tkey) is None:
                    db.execute(
                        "INSERT INTO system_events(kind,"
                        " detail_json, created_at) VALUES("
                        "'deadline_reregister_suppressed', ?, ?)",
                        (json.dumps(
                            {"tombstone_id": int(tomb[0]),
                             "channel": channel,
                             "deadline_jst": jst_value},
                            ensure_ascii=False),
                         clock.now_iso(db)))
                    # Plain SQL, not config.set_setting: that
                    # helper commits, and set_schedule must not
                    # commit a caller's open transaction.
                    db.execute(
                        "INSERT INTO settings(key, value)"
                        " VALUES(?, '1') ON CONFLICT(key)"
                        " DO UPDATE SET value='1'", (_tkey,))
            except Exception:
                pass
            if not was_in_tx:
                db.commit()
            return int(tomb[0])
    cur = db.execute(
        "INSERT INTO release_schedule(edition_id, candidate_id, channel,"
        " deadline_raw, deadline_tz, deadline_jst, deadline_confidence,"
        " deadline_kind, deadline_kind_source_url, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (edition_id, candidate_id, channel, deadline_raw, recorded_tz,
         jst_value, deadline_confidence, deadline_kind,
         deadline_kind_source_url, clock.now_iso(db)))
    new_id = int(cur.lastrowid)
    moved_from = []
    for old in existing:
        # R19: an instant-less (or confidence-less) re-registration
        # must not destroy a row that HAS an instant — only rows at
        # the same information level are replaced.
        if jst_value is None and old["deadline_jst"] is not None:
            continue
        _mark_superseded(db, old, new_id)
        if old["deadline_jst"] is not None \
                and old["deadline_jst"] != jst_value:
            moved_from.append(old["deadline_jst"])
    # W-check B5 (wholesaler 中-3): a postponement observed in ONE
    # channel now retires the SAME deadline registered through
    # another channel at the OLD instant. Channels stay distinct
    # registrations (different instants coexist untouched), but
    # two channels naming one deadline at one instant are the
    # same commitment — when it moves in one channel, the stale
    # copy in the other channel kept alerting at the old date.
    if moved_from and has_target:
        for old_jst in moved_from:
            sibs = db.execute(
                "SELECT * FROM release_schedule WHERE"
                " edition_id IS ? AND candidate_id IS ?"
                " AND deadline_kind=? AND deadline_jst=?"
                " AND superseded_by IS NULL AND id!=?",
                (edition_id, candidate_id, deadline_kind,
                 old_jst, new_id)).fetchall()
            for sib in sibs:
                _mark_superseded(db, sib, new_id)
    # W-check B5 R24 (wholesaler 中-B): the cross-channel
    # propagation above fires only when the MOVING channel itself
    # held the old instant live. When the moving channel's first
    # observation is already the new date (manual holds 02-07,
    # the event page is first seen at 02-14), the two channels
    # silently coexist at different instants and the stale one
    # still alerts. Auto-superseding another channel's row here
    # would kill what may be a genuinely distinct commitment, so
    # the conflict is SURFACED instead: a system_event at
    # registration (once per instant-set) and a conflict line in
    # the deadline digest, where the user can cancel the wrong
    # row with /deadline_cancel.
    if jst_value is not None and has_target:
        others = db.execute(
            "SELECT id, channel, deadline_jst FROM"
            " release_schedule WHERE edition_id IS ?"
            " AND candidate_id IS ? AND deadline_kind=?"
            " AND superseded_by IS NULL AND deadline_jst IS NOT NULL"
            " AND deadline_jst!=? AND id!=?",
            (edition_id, candidate_id, deadline_kind, jst_value,
             new_id)).fetchall()
        if others:
            try:
                from .config import get_setting
                _instants = sorted(
                    {jst_value} | {o[2] for o in others})
                _tgt = edition_id or f"c{candidate_id}"
                _ckey = ("deadline_conflict|" + _tgt + "|"
                         + deadline_kind + "|"
                         + "|".join(_instants))
                if get_setting(db, _ckey) is None:
                    db.execute(
                        "INSERT INTO system_events(kind,"
                        " detail_json, created_at) VALUES("
                        "'deadline_channel_conflict', ?, ?)",
                        (json.dumps(
                            {"edition_id": edition_id,
                             "candidate_id": candidate_id,
                             "kind": deadline_kind,
                             "instants": _instants,
                             "channels": sorted(
                                 {channel} | {o[1] for o in others})},
                            ensure_ascii=False),
                         clock.now_iso(db)))
                    db.execute(
                        "INSERT INTO settings(key, value)"
                        " VALUES(?, '1') ON CONFLICT(key)"
                        " DO UPDATE SET value='1'", (_ckey,))
            except Exception:
                pass
    if not was_in_tx:
        db.commit()
    return new_id


def cancel_schedule(db, schedule_id) -> bool:
    """Cancel one live schedule row (W-check B5, wholesaler 中-4).

    There was NO cancellation path at all: a cancelled (not
    postponed) event's deadline kept alerting at 72h/24h and in
    the 21:45 sweep until it arrived, and the only remedy was
    editing the database by hand. Cancellation reuses the
    supersede machinery — the row points at ITSELF as its
    successor (no successor exists), a deadline_history row
    records changed_via='cancelled', and every consumer's
    superseded_by IS NULL filter retires it at once. Returns
    False when the row does not exist or is not live."""
    try:
        sid = int(schedule_id)
    except (TypeError, ValueError):
        return False
    row = db.execute(
        "SELECT * FROM release_schedule WHERE id=? AND"
        " superseded_by IS NULL", (sid,)).fetchone()
    if row is None:
        return False
    was_in_tx = db.in_transaction
    db.execute(
        "UPDATE release_schedule SET superseded_by=? WHERE id=?",
        (sid, sid))
    db.execute(
        "INSERT INTO deadline_history(schedule_id, old_value_json,"
        " changed_at, changed_via) VALUES(?,?,?,?)",
        (sid,
         json.dumps({"deadline_raw": row["deadline_raw"],
                     "deadline_tz": row["deadline_tz"],
                     "deadline_jst": row["deadline_jst"],
                     "deadline_confidence":
                         row["deadline_confidence"]}),
         clock.now_iso(db), "cancelled"))
    if not was_in_tx:
        db.commit()
    return True


def _mark_superseded(db, old, new_id: int) -> None:
    db.execute(
        "UPDATE release_schedule SET superseded_by=? WHERE id=?",
        (new_id, old["id"]))
    db.execute(
        "INSERT INTO deadline_history(schedule_id, old_value_json,"
        " changed_at, changed_via) VALUES(?,?,?,?)",
        (old["id"],
         json.dumps({"deadline_raw": old["deadline_raw"],
                     "deadline_tz": old["deadline_tz"],
                     "deadline_jst": old["deadline_jst"],
                     "deadline_confidence":
                         old["deadline_confidence"]}),
         clock.now_iso(db), "superseded"))


def _parse_any(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _parse_jst(value: str | None) -> datetime | None:
    dt = _parse_any(value)
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=clock.JST)
    return dt


def _due_stage(row, now: datetime) -> tuple[int, str] | None:
    """The latest stage whose time has come (and deadline not past)."""
    dt = _parse_jst(row["deadline_jst"])
    if dt is None or now >= dt:
        return None
    due = None
    for hours, mode in STAGES.get(row["deadline_kind"] or "unknown",
                                  []):
        if now >= dt - timedelta(hours=hours):
            due = (hours, mode)  # stages ascend in urgency; keep last
    return due


MISSED_WINDOW_DAYS = 7


def _target_label(db, row) -> str:
    """Human label for a schedule row's target (W-check B5,
    collector 中-3): 'AR-000123 タイトル' or '候補#3 タイトル'.
    The staged alerts and digest lines used to name only the
    bare id — for candidates, a bare NUMBER in a different
    number space than the schedule id the sweep prints — so the
    user had to look the deadline up before being able to act
    on an alert whose whole point is a fast decision."""
    from .normalize import one_line

    def _cap(s: str) -> str:
        # R24 (collector 軽微-ア): the same 120-char title cap
        # as the other notification surfaces.
        return s[:120] + "…" if len(s) > 120 else s

    if row["edition_id"]:
        t = db.execute("SELECT title FROM editions WHERE id=?",
                       (row["edition_id"],)).fetchone()
        title = _cap(one_line(t[0])) if t and t[0] else ""
        return row["edition_id"] + (f" {title}" if title else "")
    if row["candidate_id"] is not None:
        t = db.execute(
            "SELECT title FROM candidate_editions WHERE id=?",
            (row["candidate_id"],)).fetchone()
        title = _cap(one_line(t[0])) if t and t[0] else ""
        return f"候補#{row['candidate_id']}" + \
            (f" {title}" if title else "")
    return "（対象不明）"


def _sibling_ids(db, row) -> list[int]:
    """Live rows naming the SAME deadline through another channel:
    same target, same kind, same instant, different row id. They
    are distinct registrations (channels are separate commitments)
    but they are one deadline for alerting purposes — R19."""
    rows = db.execute(
        "SELECT id FROM release_schedule WHERE edition_id IS ?"
        " AND candidate_id IS ? AND deadline_kind IS ?"
        " AND deadline_jst=? AND superseded_by IS NULL AND id!=?",
        (row["edition_id"], row["candidate_id"],
         row["deadline_kind"], row["deadline_jst"],
         row["id"])).fetchall()
    return [int(r[0]) for r in rows]


def _any_key_in_outbox(db, keys: list[str]) -> bool:
    if not keys:
        return False
    marks = ",".join("?" * len(keys))
    return db.execute(
        f"SELECT 1 FROM notification_outbox WHERE"
        f" idempotency_key IN ({marks}) LIMIT 1",
        keys).fetchone() is not None


def staged_alerts(db) -> int:
    """Enqueue INSTANT deadline alerts (hard_cutoff 72h/24h only).

    Returns the number created. Fires only the latest due stage per
    row, once (idempotency key per stage).

    Missed-window declaration (review fix, AI-engineer persona): if
    the process was down across a hard_cutoff deadline and no alert
    ever went out, the miss itself is notified once — silently
    expiring was the old behaviour, and 'we only learned after the
    deadline' is exactly the failure agreement-18 exists to stop.

    W-check B2 (wholesaler D-3): the declaration covered hard_cutoff
    only. A guarantee_cutoff whose 21:45 sweep was slept through got
    NOTHING — no sweep notice, no staged alert (its stages are
    digest-mode by design), no post-hoc declaration. The declaration
    now covers guarantee_cutoff too; the 'alerted' probe below checks
    for the sweep's own key as well, so a deadline the sweep DID
    notify is never re-declared as missed."""
    now = clock.now(db)
    created = 0
    rows = db.execute(
        "SELECT * FROM release_schedule WHERE"
        " deadline_confidence='confirmed' AND deadline_jst IS NOT NULL"
        " AND superseded_by IS NULL"
    ).fetchall()
    for r in rows:
        if r["edition_id"] is None \
                and candidate_is_rejected(db, r["candidate_id"]):
            continue
        dt = _parse_jst(r["deadline_jst"])
        # W-check B5 (maniac A 中-4): the missed declaration
        # asserts the process was DOWN when the deadline passed.
        # A row REGISTERED after its own deadline (a past event
        # harvested late, a backfilled announcement) satisfies
        # every other condition the moment it exists — the bot
        # solemnly declared '停止中に締切を過ぎた' for deadlines
        # created after the fact, potentially dozens per event.
        # A row created after its deadline was never missable.
        # (Legacy rows carry created_at NULL and keep the old
        # behaviour; postpone-superseded copies are filtered by
        # the live-row predicate already.)
        _created = None
        try:
            _created = _parse_jst(r["created_at"])
        except Exception:
            _created = None
        if dt is not None and now >= dt \
                and not (_created is not None and _created > dt) \
                and (r["deadline_kind"] or "unknown") in (
                    "hard_cutoff", "guarantee_cutoff") \
                and now - dt <= timedelta(days=MISSED_WINDOW_DAYS):
            # W-check B4 (collector): the probe omitted the 72h
            # stage key — a deadline that WAS alerted at 72h (but
            # whose 24h stage never fired, e.g. the process woke
            # inside the last day) was post-hoc declared
            # '締切経過（未通知）', a false missed-declaration
            # against a notification that demonstrably went out.
            # R19: the probe also covers same-deadline SIBLING
            # rows (another channel's registration of this exact
            # deadline) — if any of them alerted, this deadline
            # was notified, and a missed-declaration would be a
            # false alarm about a notification that went out.
            probe_keys = [
                f"deadline_alert|{r['id']}|72h",
                f"deadline_alert|{r['id']}|24h",
                f"deadline_missed|{r['id']}",
                f"deadline_sweep|{r['id']}|{r['deadline_jst']}"]
            for sid in _sibling_ids(db, r):
                probe_keys += [
                    f"deadline_alert|{sid}|72h",
                    f"deadline_alert|{sid}|24h",
                    f"deadline_missed|{sid}",
                    f"deadline_sweep|{sid}|{r['deadline_jst']}"]
            if not _any_key_in_outbox(db, probe_keys):
                body = (f"締切経過（未通知）: "
                        f"{_target_label(db, r)}\n"
                        f"締切: {r['deadline_raw']}（{r['deadline_jst']}）\n"
                        f"停止中に締切を過ぎたため、事後申告します。")
                row_id = outbox_mod.enqueue(
                    db, event_type="deadline_alert", body=body,
                    edition_id=r["edition_id"],
                    idempotency_key=f"deadline_missed|{r['id']}")
                if row_id is not None:
                    created += 1
            continue
        due = _due_stage(r, now)
        if due is None:
            continue
        hours, mode = due
        if mode != "instant":
            continue
        # R19: a sibling row (same deadline, another channel) has
        # already fired this stage — one deadline, one alert.
        sib_keys = [f"deadline_alert|{sid}|{hours}h"
                    for sid in _sibling_ids(db, r)]
        if _any_key_in_outbox(db, sib_keys):
            continue
        body = (f"締切アラート（{hours}時間前）: "
                f"{_target_label(db, r)}\n"
                f"締切: {r['deadline_raw']}（{r['deadline_jst']}）\n"
                f"種別: {r['deadline_kind']} / 経路: {r['channel']}")
        row_id = outbox_mod.enqueue(
            db, event_type="deadline_alert", body=body,
            edition_id=r["edition_id"],
            idempotency_key=f"deadline_alert|{r['id']}|{hours}h")
        if row_id is not None:
            created += 1
    return created


def deadline_digest_lines(db) -> list[str]:
    """Digest-stage deadline lines (all kinds except unknown)."""
    now = clock.now(db)
    lines = []
    rows = db.execute(
        "SELECT * FROM release_schedule WHERE"
        " deadline_confidence='confirmed' AND deadline_jst IS NOT NULL"
        " AND superseded_by IS NULL"
        " ORDER BY deadline_jst").fetchall()
    # W-check B5 (wholesaler 軽微-4): the sibling skip that R19
    # added to the instant alerts and the sweep never reached
    # this digest path — one deadline registered through two
    # channels printed TWICE in the morning digest. Same rule
    # here: one (target, kind, instant) is one line.
    seen: set = set()
    # W-check B5 R24 (wholesaler 中-B): groups of live rows for
    # one (target, kind) holding DIFFERENT instants across
    # channels are a conflict the user must resolve — surfaced
    # below the ordinary lines whenever any member is digest-due.
    _groups: dict = {}
    _due_groups: set = set()
    for r in rows:
        _groups.setdefault(
            (r["edition_id"], r["candidate_id"],
             r["deadline_kind"]), []).append(r)
        if r["edition_id"] is None \
                and candidate_is_rejected(db, r["candidate_id"]):
            continue
        due = _due_stage(r, now)
        if due is None:
            continue
        hours, mode = due
        if mode != "digest":
            continue
        key = (r["edition_id"], r["candidate_id"],
               r["deadline_kind"], r["deadline_jst"])
        if key in seen:
            continue
        seen.add(key)
        _due_groups.add((r["edition_id"], r["candidate_id"],
                         r["deadline_kind"]))
        label = {168: "7日前", 72: "3日前", 24: "24時間前"}[hours]
        line = (f"締切（{label}・種別 {r['deadline_kind']}）: "
                f"{_target_label(db, r)} — "
                f"{r['deadline_raw']}（{r['deadline_jst']}）")
        if r["deadline_kind"] == "guarantee_cutoff":
            line += " ※注文自体は継続する場合があります"
        lines.append(line)
    for _gk in sorted(_due_groups, key=str):
        _members = _groups.get(_gk) or []
        _instants = sorted({m["deadline_jst"] for m in _members})
        if len(_instants) < 2:
            continue
        _detail = " / ".join(
            f"#{m['id']} {m['deadline_jst'][:10]}"
            for m in sorted(_members,
                            key=lambda m: m["deadline_jst"]))
        lines.append(
            f"締切の競合: {_target_label(db, _members[0])} に"
            f"異なる日時の締切が複数あります（{_detail}）— "
            "正しい方を残し、他方は /deadline_cancel #番号 で"
            "取り消してください")
    return lines


def raw_parseable(raw: str | None) -> bool:
    """True when the original text carries a parseable datetime —
    the precondition for a one-tap timezone confirmation."""
    return _parse_any(raw) is not None


def confirm_with_tz(db, schedule_id: int, tz_name: str) -> bool:
    """Human confirmation of an unconfirmed row's timezone (/deadlines
    button): interpret the raw text in tz_name, convert to JST, and
    mark the row confirmed. Returns False when unparseable.

    W-check B4 (adversarial QA): this had NO state guard — an old
    inline keyboard left in the chat could be re-tapped with a
    different timezone and silently MOVE an already-confirmed
    deadline by hours, with no record. Confirmation is now refused
    for rows already 'confirmed' (and for superseded rows), and
    every accepted confirmation writes a deadline_history row.
    When the raw text carries its own offset, the instant is
    already determined by the text: deadline_tz records the offset
    itself, not the button's zone name, so the three-point set
    (raw / tz / jst) stays internally consistent."""
    row = db.execute(
        "SELECT deadline_raw, deadline_tz, deadline_jst,"
        " deadline_confidence, superseded_by FROM release_schedule"
        " WHERE id=?", (schedule_id,)).fetchone()
    if row is None:
        return False
    if row[3] == "confirmed" or row[4] is not None:
        return False
    parsed = _parse_any(row[0])
    if parsed is None:
        return False
    # R19 (adversarial QA): the tz name itself is validated even
    # when the raw text carries its own offset — an API caller
    # passing 'Foo/Bar' used to get True while the offset label was
    # recorded, so the validation the bot path gets from its
    # TZ_CHOICES index binding now exists at the function boundary.
    try:
        ZoneInfo(tz_name)
    except Exception:
        return False
    confidence = "confirmed"
    recorded_tz = tz_name
    if parsed.tzinfo is None:
        try:
            tz = ZoneInfo(tz_name)
            parsed = parsed.replace(tzinfo=tz)
        except Exception:
            return False
        # The human confirmed the TIMEZONE, not which of the two
        # fold occurrences was meant — a DST fold/gap wall time
        # stays 'ambiguous' even after the tzfix button.
        if _wall_time_ambiguous(parsed, tz):
            confidence = "ambiguous"
    else:
        recorded_tz = _offset_label(parsed)
    db.execute(
        "INSERT INTO deadline_history(schedule_id, old_value_json,"
        " changed_at, changed_via) VALUES(?,?,?,?)",
        (schedule_id,
         json.dumps({"deadline_tz": row[1], "deadline_jst": row[2],
                     "deadline_confidence": row[3]}),
         clock.now_iso(db), "tzfix"))
    db.execute(
        "UPDATE release_schedule SET deadline_tz=?, deadline_jst=?,"
        " deadline_confidence=? WHERE id=?",
        (recorded_tz, _to_jst_iso(parsed), confidence, schedule_id))
    db.commit()
    return True


_UNCONFIRMED_PREDICATE = (
    "(COALESCE(deadline_confidence, '') != 'confirmed'"
    " OR COALESCE(deadline_kind, 'unknown') = 'unknown'"
    " OR deadline_jst IS NULL) AND superseded_by IS NULL")


def unconfirmed_count(db) -> int:
    """Distinct live rows that are unconfirmed on ANY axis — the
    one definition every surface (health digest headline, bot
    /status, /deadlines) shares (R19, collector / maniac A: the
    headline, the breakdown and /status each counted a different
    set, and NULL confidence/kind rows fell out of all of them)."""
    return db.execute(
        "SELECT COUNT(*) FROM release_schedule WHERE "
        + _UNCONFIRMED_PREDICATE).fetchone()[0]


def unconfirmed_summary(db) -> dict:
    """Counts of rows that must stay visible until resolved (#5).

    Unconfirmed = timezone unconfirmed (tz_unknown / ambiguous) OR
    kind unconfirmed (deadline_kind='unknown') OR no instant —
    spec C-10.3 counts both axes; the first version silently
    dropped kind-unknown rows whose TZ was confirmed (review D9).

    R19: the summary is a PARTITION of unconfirmed_count — each
    unconfirmed row lands in exactly one bucket, in priority
    order: confidence axis first (by its value; NULL counts as
    tz_unknown), then kind_unknown, then no_instant. The buckets
    therefore always sum to the headline count."""
    out: dict[str, int] = {}
    rows = db.execute(
        "SELECT deadline_confidence, deadline_kind, deadline_jst"
        " FROM release_schedule WHERE " + _UNCONFIRMED_PREDICATE
    ).fetchall()
    for conf, kind, jst in rows:
        if (conf or "") != "confirmed":
            bucket = conf if conf in ("tz_unknown", "ambiguous") \
                else "tz_unknown"
        elif (kind or "unknown") == "unknown":
            bucket = "kind_unknown"
        else:
            bucket = "no_instant"
        out[bucket] = out.get(bucket, 0) + 1
    return out


def candidate_is_rejected(db, candidate_id) -> bool:
    """True when a schedule's candidate was rejected by the human:
    the discovery is dead, so its deadlines must stop alerting
    (R19, maniac A — staged alerts and the sweep had no candidate
    status join and kept firing for rejected candidates)."""
    if candidate_id is None:
        return False
    row = db.execute(
        "SELECT status FROM candidate_editions WHERE id=?",
        (candidate_id,)).fetchone()
    return row is not None and row[0] == "rejected"


def _arrival_events(db, schedule_id: int) -> list[dict]:
    out = []
    for (detail,) in db.execute(
            "SELECT detail_json FROM system_events WHERE"
            " kind='arrival_check'").fetchall():
        try:
            d = json.loads(detail or "")
        except (ValueError, TypeError):
            continue
        if d.get("schedule_id") == schedule_id:
            out.append(d)
    return out


def arrival_check(db) -> list[dict]:
    """Post-deadline arrival confirmation for confirmed rows.

    Outcomes: confirmed_arrived / not_found / unknown (spec C-10.4).
    not_found earns exactly one recheck at deadline + 7 days."""
    now = clock.now(db)
    results = []
    rows = db.execute(
        "SELECT * FROM release_schedule WHERE"
        " deadline_confidence='confirmed' AND deadline_jst IS NOT NULL"
        " AND superseded_by IS NULL"
    ).fetchall()
    for r in rows:
        dt = _parse_jst(r["deadline_jst"])
        if dt is None or dt > now:
            continue
        events = _arrival_events(db, r["id"])
        recheck = False
        if events:
            first = events[0]
            if first.get("outcome") == "not_found" \
                    and not any(e.get("recheck") for e in events) \
                    and now >= dt + timedelta(days=RECHECK_AFTER_DAYS):
                recheck = True
            else:
                continue
        if r["edition_id"]:
            arrived = db.execute(
                "SELECT 1 FROM listings WHERE edition_id=? AND"
                " first_seen>=? LIMIT 1",
                (r["edition_id"], _to_jst_iso(dt))).fetchone() \
                is not None
            outcome = "confirmed_arrived" if arrived else "not_found"
        else:
            outcome = "unknown"  # candidate-only: nothing to look up
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('arrival_check', ?, ?)",
            (json.dumps({"schedule_id": r["id"],
                         "edition_id": r["edition_id"],
                         "outcome": outcome, "recheck": recheck}),
             clock.now_iso(db)))
        db.commit()
        results.append({"schedule_id": r["id"], "outcome": outcome,
                        "recheck": recheck})
    return results
