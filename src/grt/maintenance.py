"""Weekly housekeeping (W-check B2, seller D-1/D-5/D-6).

The tool shipped with no retention anywhere: operational history
(fetch_runs, system_events, telegram_updates) grew without bound,
the evidence module's documented 30-day retention had no
implementation, and nothing watched the disk — a full disk failed
silently in several paths at once (notifications lost with the same
return value as a duplicate, error recording itself failing
silently, the heartbeat still updating so the watchdog saw health).

This job prunes operational telemetry and expired Discogs marketplace
snapshots. Other archive data — listings, match decisions, candidates,
and the outbox itself (whose idempotency keys are a designed
permanence) — is never touched. Discogs stats are retained only during
the six-hour service window; they are also pruned by the 15-minute
health job. Other retention horizons are operational defaults:

- fetch_runs: 180 days (is_due needs the latest run; the health
  medians need the last few dozen)
- system_events: 180 days (digests read recent windows)
- telegram_updates: 90 days (dedup only matters for redelivery,
  which happens within days at most)
- evidence body files: 30 days (the module's own stated policy)
- disk guard: warn once per day below 500 MB free
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import timedelta

from . import clock, config

FETCH_RUNS_DAYS = 180
SYSTEM_EVENTS_DAYS = 180
TELEGRAM_UPDATES_DAYS = 90
EVIDENCE_DAYS = 30
DISK_LOW_BYTES = 500 * 1024 * 1024
DISCOGS_SNAPSHOT_HOURS = 6


def _prune_table(db, table: str, column: str, days: int, now) -> int:
    cutoff = clock.to_iso(now - timedelta(days=days))
    cur = db.execute(f"DELETE FROM {table} WHERE {column}<?", (cutoff,))
    return cur.rowcount or 0


def prune_discogs_snapshots(db, now=None) -> int:
    """Discard Discogs marketplace stats outside their six-hour window."""
    now = now or clock.now(db)
    cutoff = clock.to_iso(now - timedelta(hours=DISCOGS_SNAPSHOT_HOURS))
    cur = db.execute(
        "DELETE FROM price_snapshots WHERE source='discogs'"
        " AND julianday(taken_at)<julianday(?)", (cutoff,))
    db.commit()
    return cur.rowcount or 0


def _prune_evidence(now) -> int:
    d = config.data_dir() / "evidence"
    if not d.exists():
        return 0
    cutoff = time.time() - EVIDENCE_DAYS * 86400
    removed = 0
    for p in d.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def _disk_check(db, now) -> dict:
    try:
        usage = shutil.disk_usage(str(config.data_dir()))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    info = {"ok": True, "free_bytes": usage.free}
    if usage.free < DISK_LOW_BYTES:
        from . import outbox
        day = now.strftime("%Y-%m-%d")
        outbox.enqueue(
            db, event_type="disk_low",
            body=("【ディスク残量警告】データ保存先の空き容量が "
                  + f"{usage.free // (1024 * 1024)}MB まで減っています。"
                  "古いログや不要なファイルの整理を検討してください。"),
            idempotency_key="disk_low|" + day)
        info["warned"] = True
    return info


def run_maintenance(db) -> dict:
    """Run every housekeeping step, isolating failures per step.

    R9 seller N-1: the steps used to run unguarded in series, so one
    broken table (e.g. a legacy DB missing a column) aborted every
    later step — including the disk guard — and left no
    maintenance_run event, failing at the same place every week.
    Each step now records either its count or its error, the summary
    event is always attempted, and one step's failure never takes
    the others with it.
    """
    now = clock.now(db)
    result: dict = {}
    errors: dict = {}

    def step(name, fn):
        try:
            result[name] = fn()
        except Exception as exc:
            errors[name] = repr(exc)[:300]
            try:
                db.rollback()
            except Exception:
                pass

    step("fetch_runs", lambda: _prune_table(
        db, "fetch_runs", "started_at", FETCH_RUNS_DAYS, now))
    step("system_events", lambda: _prune_table(
        db, "system_events", "created_at", SYSTEM_EVENTS_DAYS, now))
    step("telegram_updates", lambda: _prune_table(
        db, "telegram_updates", "received_at", TELEGRAM_UPDATES_DAYS, now))
    step("discogs_snapshots", lambda: prune_discogs_snapshots(db, now))
    try:
        db.commit()
    except Exception as exc:
        errors["commit"] = repr(exc)[:300]
        try:
            db.rollback()
        except Exception:
            pass
    step("evidence_files", lambda: _prune_evidence(now))
    step("disk", lambda: _disk_check(db, now))
    if errors:
        result["errors"] = errors
    try:
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('maintenance_run', ?, ?)",
            (json.dumps({k: v for k, v in result.items() if k != "disk"}),
             clock.now_iso(db)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return result
