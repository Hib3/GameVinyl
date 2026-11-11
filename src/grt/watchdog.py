"""Independent dead-man's switch (agreement-07).

Runs as its own lightweight process (Task Scheduler, every 15 minutes).
If the main process heartbeat is older than 20 minutes it (1) attempts a
restart, and (2) records the anomaly in system_events regardless of the
restart outcome. When the main process is alive again after an anomaly,
a recovery notification stating the stopped window is queued.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from . import clock, outbox

STALE_MINUTES = 20
RETRY_INTERVAL_MINUTES = 20


def check(db, restart=None) -> dict:
    """One watchdog tick. `restart` is an injectable callable returning
    True/False; the default tries to relaunch run.py detached."""
    now = clock.now(db)
    hb = db.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    if hb is None or not hb["alive_at"]:
        return {"status": "no_heartbeat_yet"}

    # W-check B1 (AI engineer #1): an unparseable alive_at (damaged
    # or hand-edited DB) must not kill the watchdog itself — an
    # unreadable heartbeat is treated as stale, never as a crash.
    try:
        alive_at = clock.parse_iso(hb["alive_at"])
    except (TypeError, ValueError):
        alive_at = None
    stale = (alive_at is None
             or now - alive_at > timedelta(minutes=STALE_MINUTES))
    open_anomaly = db.execute(
        "SELECT * FROM system_events WHERE kind='watchdog_anomaly'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    anomaly_open = False
    if open_anomaly is not None:
        closed = db.execute(
            "SELECT COUNT(*) FROM system_events WHERE kind='watchdog_recovered'"
            " AND id>?", (open_anomaly["id"],)).fetchone()[0]
        anomaly_open = closed == 0

    if stale:
        detail = {"alive_at": hb["alive_at"], "detected_at": clock.to_iso(now),
                  "stale_minutes": (int((now - alive_at).total_seconds() // 60)
                                    if alive_at is not None else None)}
        if alive_at is None:
            detail["alive_at_unparseable"] = True
        already = db.execute(
            "SELECT COUNT(*) FROM system_events WHERE kind='watchdog_anomaly'"
            " AND detail_json LIKE ?", ("%" + hb["alive_at"] + "%",)).fetchone()[0]
        if not already:
            db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('watchdog_anomaly', ?, ?)",
                (json.dumps(detail), clock.to_iso(now)))
        ok = False
        attempted = False
        if restart is not None:
            # Restart policy (W-check B1, AI engineer #2): a spawn
            # that "succeeded" only proves Popen did not raise — a
            # child that dies instantly leaves the heartbeat stale
            # forever, and the old rule (never retry after one
            # successful spawn) then disabled recovery exactly when
            # it was needed. New rule: skip a re-attempt only while
            # the last successful spawn for this episode is younger
            # than RETRY_INTERVAL_MINUTES; after that, a still-stale
            # heartbeat earns another attempt. The spacing (never
            # more than ~3 attempts/hour) is what prevents the
            # duplicate-spawn storm the old rule guarded against;
            # the single-instance lock remains the hard guard.
            prior_ok = False
            for prev_json, prev_created in db.execute(
                    "SELECT detail_json, created_at FROM system_events"
                    " WHERE kind='watchdog_restart'"
                    " ORDER BY id DESC LIMIT 100").fetchall():
                try:
                    prev = json.loads(prev_json)
                except (TypeError, ValueError):
                    continue
                if (prev.get("alive_at") == hb["alive_at"]
                        and prev.get("restart_ok")):
                    try:
                        last_try = clock.parse_iso(prev_created)
                    except (TypeError, ValueError):
                        last_try = None
                    if (last_try is not None and now - last_try
                            < timedelta(minutes=RETRY_INTERVAL_MINUTES)):
                        prior_ok = True
                    break
            if prior_ok:
                ok = True
                detail["restart_skipped_duplicate"] = True
            else:
                attempted = True
                try:
                    ok = bool(restart())
                except Exception:
                    ok = False
        detail["restart_attempted"] = attempted
        detail["restart_ok"] = ok
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('watchdog_restart', ?, ?)",
            (json.dumps(detail), clock.to_iso(now)))
        db.commit()
        return {"status": "anomaly", **detail}

    if anomaly_open and open_anomaly is not None:
        # W-check B1 R4 (AI engineer observation): a damaged
        # detail_json must not crash every tick either — fall back
        # to a generic recovery notice without the window details.
        try:
            anomaly = json.loads(open_anomaly["detail_json"])
        except (TypeError, ValueError):
            anomaly = {}
        body = ("【復旧通知】メイン処理が復旧しました。\n"
                + "停止期間: " + str(anomaly.get("alive_at")) + " 〜 "
                + clock.to_iso(now) + "\n"
                + "この間の通知は遅延・欠落している可能性があります。")
        outbox.enqueue(
            db, event_type="recovery_notice", body=body,
            idempotency_key="recovery|" + str(open_anomaly["id"]),
            quiet_exempt=True)
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('watchdog_recovered', ?, ?)",
            (json.dumps({"anomaly_id": open_anomaly["id"],
                         "recovered_at": clock.to_iso(now)}),
             clock.to_iso(now)))
        db.commit()
        return {"status": "recovered", "anomaly_id": open_anomaly["id"]}
    return {"status": "alive"}


def _default_restart() -> bool:
    root = Path(__file__).resolve().parents[2]
    try:
        subprocess.Popen(
            [sys.executable, str(root / "run.py")],
            cwd=str(root), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def main() -> None:
    from . import db as dbmod
    db = dbmod.connect()
    dbmod.init_schema(db)
    result = check(db, restart=_default_restart)
    print(result)


if __name__ == "__main__":
    main()
