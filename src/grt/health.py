"""Health layer (agreement-07): fetch ledger, source state machine,
zero-item anomaly, silence detection, blocked shelf, observation gaps,
heartbeat. All time reads go through grt.clock so tests can offset time.
"""
from __future__ import annotations

import statistics
from datetime import timedelta

from . import clock

ERROR_RESULTS = {"HTTP_ERROR", "BLOCKED", "TIMEOUT", "PARSE_EMPTY",
                 "PARSE_ERROR", "NETWORK", "UNKNOWN"}

# Fetcher-kind taxonomy (fetchers/base.FETCHER_KINDS). This is a
# different taxonomy from sources.kind on purpose (spec section 3.2):
# sources.kind says what a source *is* (db/shop/community/...), the
# fetcher kind says *how it was fetched* (API/RSS/...). The daily digest
# must aggregate by the latter (9-B13), so every run records it.
FETCHER_KINDS = {"API", "RSS", "HTML", "INGEST", "GOODS", "DOUJIN_EVENT",
                 "BIBLIO", "OFFICIAL_ALERT"}


def ensure_fetcher_kind_column(db) -> bool:
    """Add ``fetch_runs.fetcher_kind`` when missing (schema.sql predates
    it and is owned outside this change). Returns True when usable."""
    try:
        cols = [r[1] for r in db.execute(
            "PRAGMA table_info(fetch_runs)").fetchall()]
        if "fetcher_kind" not in cols:
            db.execute("ALTER TABLE fetch_runs ADD COLUMN fetcher_kind TEXT")
            db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def record_fetch_run(db, source_id: int, *, result: str, http_status=None,
                     items_extracted=None, started_at=None, finished_at=None,
                     error_detail=None, raw_hash=None, watch_rule_id=None,
                     fetcher_kind=None, normalized_hash=None,
                     body_path=None) -> int:
    started = started_at or clock.now_iso(db)
    finished = finished_at or started
    kind = fetcher_kind if fetcher_kind in FETCHER_KINDS else None
    # W-check B3: raw_hash is the RAW response hash (the retained
    # evidence file's name derives from it); the normalized page
    # hash and the failure-body path ride in their own columns.
    # The fetcher_kind / normalized_hash / body_path columns are
    # late columns — each is included only when the DB carries it,
    # so a pre-convergence DB never fails a run over bookkeeping.
    cols = ["source_id", "watch_rule_id", "started_at", "finished_at",
            "http_status", "items_extracted", "result", "error_detail",
            "raw_hash"]
    vals = [source_id, watch_rule_id, started, finished, http_status,
            items_extracted, result, error_detail, raw_hash]
    have = {row[1] for row in db.execute("PRAGMA table_info(fetch_runs)")}
    if kind is not None and "fetcher_kind" in have:
        cols.append("fetcher_kind")
        vals.append(kind)
    if "normalized_hash" in have:
        cols.append("normalized_hash")
        vals.append(normalized_hash)
    if "body_path" in have:
        cols.append("body_path")
        vals.append(body_path)
    cur = db.execute(
        "INSERT INTO fetch_runs(%s) VALUES(%s)"
        % (", ".join(cols), ", ".join("?" * len(cols))),
        vals)
    run_id = cur.lastrowid
    _update_health(db, source_id, result, items_extracted)
    if result == "BLOCKED":
        _shelf_blocked(db, source_id)
    db.commit()
    return run_id


def _health_row(db, source_id: int):
    row = db.execute("SELECT * FROM source_health WHERE source_id=?",
                     (source_id,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO source_health(source_id, state, consecutive_failures,"
            " last_success_at) VALUES(?, 'HEALTHY', 0, NULL)", (source_id,))
        row = db.execute("SELECT * FROM source_health WHERE source_id=?",
                         (source_id,)).fetchone()
    return row


def _median_recent_items(db, source_id: int, limit: int = 14) -> float:
    rows = db.execute(
        "SELECT items_extracted FROM fetch_runs WHERE source_id=? AND result='OK'"
        " AND items_extracted IS NOT NULL ORDER BY id DESC LIMIT ?",
        (source_id, limit)).fetchall()
    vals = [r[0] for r in rows]
    return float(statistics.median(vals)) if vals else 0.0


def _update_health(db, source_id: int, result: str, items_extracted) -> None:
    now = clock.now(db)
    h = _health_row(db, source_id)
    state = h["state"]
    fails = h["consecutive_failures"]

    if result == "OK":
        # Zero-item anomaly: 0 items on two consecutive successful runs
        # after the source demonstrably produced items -> PARSE_EMPTY /
        # SUSPECT. The first version required median >= 5 over recent
        # successes, which left exactly the smallest shops — and any
        # source broken since its first day — permanently invisible:
        # a 1-item shop going to 0 stayed HEALTHY forever (W-check B2,
        # wholesaler D-5 / maniac A D10). Any nonzero yield in recent
        # history now qualifies the source for the check.
        if items_extracted == 0:
            prev = db.execute(
                "SELECT items_extracted FROM fetch_runs WHERE source_id=?"
                " AND result='OK' ORDER BY id DESC LIMIT 1 OFFSET 1",
                (source_id,)).fetchone()
            median = _median_recent_items(db, source_id)
            had_nonzero = db.execute(
                "SELECT 1 FROM fetch_runs WHERE source_id=? AND"
                " result='OK' AND items_extracted>0 LIMIT 1",
                (source_id,)).fetchone() is not None
            if prev is not None and prev[0] == 0 and \
                    (median >= 5 or had_nonzero):
                db.execute(
                    "UPDATE fetch_runs SET result='PARSE_EMPTY' WHERE id="
                    "(SELECT MAX(id) FROM fetch_runs WHERE source_id=?)",
                    (source_id,))
                db.execute(
                    "UPDATE source_health SET state='SUSPECT',"
                    " consecutive_failures=0, last_success_at=? WHERE source_id=?",
                    (clock.now_iso(db), source_id))
                _event(db, "parse_empty_suspect",
                       {"source_id": source_id, "median_items": median})
                return
        fails = 0
        # Recovery: two consecutive successes -> HEALTHY.
        prev = db.execute(
            "SELECT result FROM fetch_runs WHERE source_id=? ORDER BY id DESC"
            " LIMIT 1 OFFSET 1", (source_id,)).fetchone()
        if state != "HEALTHY" and prev is not None and prev[0] == "OK":
            state = "HEALTHY"
            _event(db, "source_recovered", {"source_id": source_id})
        db.execute(
            "UPDATE source_health SET state=?, consecutive_failures=0,"
            " last_success_at=? WHERE source_id=?",
            (state, clock.now_iso(db), source_id))
        return

    # Failure side.
    fails += 1
    degraded = False
    episode_marker = h["last_success_at"] or "never"
    if fails >= 3 and state == "HEALTHY":
        state = "DEGRADED"
        degraded = True
        _event(db, "source_degraded",
               {"source_id": source_id, "consecutive_failures": fails})
    db.execute(
        "UPDATE source_health SET state=?, consecutive_failures=?"
        " WHERE source_id=?", (state, fails, source_id))
    if degraded:
        _alert_degraded(db, source_id, fails, episode_marker)


def _alert_degraded(db, source_id: int, fails: int, episode_marker: str) -> None:
    """Telegram warning for the HEALTHY -> DEGRADED transition (9-A2/9-B4).

    Sent exactly once per degradation episode through the outbox: the
    state machine only enters DEGRADED from HEALTHY, and the
    idempotency key additionally pins (source, last-success episode), so
    repeated failures inside the same episode queue no second warning,
    while a recovery followed by a new degradation does warn again.
    Best effort: a failure to queue must never break health recording
    itself, so it is logged as a system_event instead of raising.
    """
    try:
        from . import outbox  # local import: outbox must stay import-light
        src = db.execute("SELECT name FROM sources WHERE id=?",
                         (source_id,)).fetchone()
        name = src[0] if src and src[0] else str(source_id)
        body = ("【ソース劣化警告】" + str(name)
                + "（source_id=" + str(source_id) + "）が DEGRADED に"
                "遷移しました。連続失敗: " + str(fails) + "回。"
                "取得は継続していますが、復旧しない場合は DOWN になります。")
        outbox.enqueue(
            db, event_type="source_degraded", body=body,
            idempotency_key=("source_degraded|" + str(source_id) + "|"
                             + str(episode_marker)),
            source=str(name))
    except Exception as exc:  # never let alerting break the ledger
        try:
            _event(db, "source_degraded_alert_failed",
                   {"source_id": source_id, "error": str(exc)[:300]})
        except Exception:
            pass


def _enqueue_health_alert(db, source_id: int, event_type: str,
                          key: str, body: str) -> None:
    """Best-effort immediate outbox alert for a health transition.

    W-check B2 (wholesaler D-6): DEGRADED got an immediate Telegram
    warning but the more severe DOWN — and silence detection — only
    wrote system_events, so their first report was the next morning's
    digest, up to ~23h late. All three transitions now alert
    immediately, once per episode (the idempotency key pins the
    episode marker). A failure to queue never breaks health recording.
    """
    try:
        from . import outbox  # local import: outbox must stay import-light
        outbox.enqueue(db, event_type=event_type, body=body,
                       idempotency_key=key)
    except Exception as exc:
        try:
            _event(db, event_type + "_alert_failed",
                   {"source_id": source_id, "error": str(exc)[:300]})
        except Exception:
            pass


def _source_name(db, source_id: int) -> str:
    src = db.execute("SELECT name FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    return src[0] if src and src[0] else str(source_id)


def is_shelved(db, source_id: int) -> bool:
    """True while the source sits on the blocked shelf: >=3 consecutive
    BLOCKED results with the last one inside 24h.

    W-check B2 (maniac A D-8): the shelf used to be write-only —
    recorded and counted in the digest, but nothing ever READ it, so
    a shelved source kept being fetched whenever its lane ignored the
    frequency ladder. BaseFetcher.run consults this before any
    network call; after 24h one probe run is allowed through, and a
    fresh BLOCKED re-shelves it.
    """
    row = db.execute(
        "SELECT consecutive_count, last_blocked_at FROM blocked_sources"
        " WHERE source=?", (_source_name(db, source_id),)).fetchone()
    if row is None or int(row[0] or 0) < 3:
        return False
    last = _parse_or_none(row[1])
    if last is None:
        return False
    return clock.now(db) - last < timedelta(hours=24)


def _shelf_blocked(db, source_id: int) -> None:
    src = db.execute("SELECT name FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    name = src[0] if src else str(source_id)
    now = clock.now_iso(db)
    row = db.execute("SELECT * FROM blocked_sources WHERE source=?",
                     (name,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO blocked_sources(source, first_blocked_at,"
            " last_blocked_at, consecutive_count, note)"
            " VALUES(?, ?, ?, 1, 'auto-shelved')", (name, now, now))
    else:
        db.execute(
            "UPDATE blocked_sources SET last_blocked_at=?,"
            " consecutive_count=consecutive_count+1 WHERE source=?",
            (now, name))


def _parse_or_none(text):
    """parse_iso that tolerates malformed stored timestamps.

    One corrupt timestamp must not kill the whole health evaluation
    (debugger-14 finding, 2026-11-08); an unparseable value is treated
    as "no timestamp recorded".
    """
    if not text:
        return None
    try:
        return clock.parse_iso(text)
    except ValueError:
        return None


def evaluate_time_based_health(db) -> list[dict]:
    """24h-without-success -> DOWN; silence beyond 2x cadence -> gap."""
    from . import freqpolicy

    now = clock.now(db)
    events = []
    for h in db.execute("SELECT * FROM source_health").fetchall():
        last = _parse_or_none(h["last_success_at"])
        runs = db.execute(
            "SELECT COUNT(*) FROM fetch_runs WHERE source_id=?",
            (h["source_id"],)).fetchone()[0]
        if last is None and runs:
            # Never succeeded: the 24h clock runs from the first attempt.
            first = db.execute(
                "SELECT MIN(started_at) FROM fetch_runs WHERE source_id=?",
                (h["source_id"],)).fetchone()[0]
            last = _parse_or_none(first)
        if runs and last is not None and now - last > timedelta(hours=24) \
                and h["state"] != "DOWN":
            db.execute("UPDATE source_health SET state='DOWN' WHERE source_id=?",
                       (h["source_id"],))
            ev = _event(db, "source_down", {"source_id": h["source_id"]})
            events.append(ev)
            name = _source_name(db, h["source_id"])
            _enqueue_health_alert(
                db, h["source_id"], "source_down",
                "source_down|" + str(h["source_id"]) + "|"
                + str(h["last_success_at"] or "never"),
                "【ソース停止警告】" + str(name)
                + "（source_id=" + str(h["source_id"]) + "）が DOWN に"
                "遷移しました。24時間以上、成功した取得がありません。")
    # Close observation gaps whose source has succeeded since the gap
    # opened (W-check B2, wholesaler D-4b): gap_end was never written
    # by anyone, so one silence permanently suppressed every later
    # silence for that source — the open gap row itself was the check.
    for g in db.execute(
            "SELECT * FROM observation_gaps WHERE gap_end IS NULL"
            " AND scope LIKE 'source:%'").fetchall():
        try:
            sid = int(str(g["scope"]).split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        h = db.execute("SELECT * FROM source_health WHERE source_id=?",
                       (sid,)).fetchone()
        if not h or not h["last_success_at"]:
            continue
        last = _parse_or_none(h["last_success_at"])
        start = _parse_or_none(g["gap_start"])
        if last is not None and start is not None and last > start:
            db.execute(
                "UPDATE observation_gaps SET gap_end=? WHERE id=?",
                (h["last_success_at"], g["id"]))
    for s in db.execute(
            "SELECT * FROM sources WHERE expected_cadence_days IS NOT NULL").fetchall():
        h = db.execute("SELECT * FROM source_health WHERE source_id=?",
                       (s["id"],)).fetchone()
        if not h or not h["last_success_at"]:
            continue
        last = _parse_or_none(h["last_success_at"])
        cadence_days = max(
            float(s["expected_cadence_days"]),
            freqpolicy.source_min_tier(db, s["id"]) / 1440.0)
        if last is not None and \
                now - last > timedelta(days=2 * cadence_days):
            gap = db.execute(
                "SELECT * FROM observation_gaps WHERE scope=? AND gap_end IS NULL",
                (f"source:{s['id']}",)).fetchone()
            if gap is None:
                db.execute(
                    "INSERT INTO observation_gaps(scope, gap_start, gap_end,"
                    " declared_at) VALUES(?, ?, NULL, ?)",
                    (f"source:{s['id']}", h["last_success_at"], clock.now_iso(db)))
                ev = _event(db, "silence_detected",
                            {"source_id": s["id"], "since": h["last_success_at"]})
                events.append(ev)
                _enqueue_health_alert(
                    db, s["id"], "silence_detected",
                    "silence_detected|" + str(s["id"]) + "|"
                    + str(h["last_success_at"]),
                    "【ソース沈黙警告】" + _source_name(db, s["id"])
                    + "（source_id=" + str(s["id"]) + "）が想定間隔の"
                    "2倍以上沈黙しています。最終成功: "
                    + str(h["last_success_at"]))
    db.commit()
    return events


def touch_heartbeat(db, job: bool = False) -> None:
    now = clock.now_iso(db)
    row = db.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    if row is None:
        db.execute(
            "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
            " VALUES(1, ?, ?, ?)", (now, now, now))
    elif job:
        db.execute("UPDATE heartbeat SET alive_at=?, last_job_at=? WHERE id=1",
                   (now, now))
    else:
        db.execute("UPDATE heartbeat SET alive_at=? WHERE id=1", (now,))
    db.commit()


def _event(db, kind: str, detail: dict) -> dict:
    import json
    db.execute(
        "INSERT INTO system_events(kind, detail_json, created_at) VALUES(?, ?, ?)",
        (kind, json.dumps(detail, ensure_ascii=False), clock.now_iso(db)))
    return {"kind": kind, **detail}
