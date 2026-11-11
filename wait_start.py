#!/usr/bin/env python3
"""Wait until the Game Record Tool main process has actually booted.

setup.bat starts the process (via setup_tasks.py --start) and then
runs this script. Setup may call itself COMPLETE only when this
script exits 0 — i.e. when there is fresh on-disk proof of a boot:

  - a new "[...] START version=..." record in data/logs/startup.log
    whose version equals this package's VERSION, or
  - a heartbeat.boot_at at or after the start request (alive_at is
    NOT evidence: any live process bound to this data dir, including
    the old version still running, keeps it fresh),

with a timestamp at or after the moment the start was requested
(setup_tasks.py --start stamps data/logs/start_requested_at.txt).
Anything older is the PREVIOUS run and does not count — that
distinction is the whole point: through v1.6, installs looked fine
on paper while no process had ever started.

Usage: python wait_start.py [--timeout 120]
Exit 0 = boot confirmed; exit 1 = timed out (state is printed).
Stdlib only. Never raises.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.environ.get("GRT_DATA_DIR") or (ROOT / "data"))


def _parse_iso(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text.strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


def _since() -> datetime:
    """The moment the start was requested (with a small grace window)."""
    stamp_file = DATA_DIR / "logs" / "start_requested_at.txt"
    try:
        epoch = float(stamp_file.read_text(encoding="utf-8").strip())
        return datetime.fromtimestamp(epoch, JST) - timedelta(seconds=5)
    except Exception:
        return datetime.now(JST) - timedelta(seconds=30)


def _current_version() -> str | None:
    try:
        return (ROOT / "VERSION").read_text(
            encoding="utf-8").strip() or None
    except Exception:
        return None


def _startup_log_evidence(since: datetime) -> str | None:
    path = DATA_DIR / "logs" / "startup.log"
    expected = _current_version()
    try:
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
            if " START version=" not in line or not line.startswith("["):
                continue
            # W-check B1 (maniac A #4): the evidence line's version
            # must be THIS package's version. An inherited startup.log
            # (Method A carries data/ over) holds the OLD version's
            # START lines; with a stale start stamp they used to be
            # accepted as proof of a boot that never happened here.
            if expected is not None:
                marker = " START version="
                vstart = line.find(marker) + len(marker)
                vend = line.find(" ", vstart)
                found = line[vstart:vend if vend >= 0 else None].strip()
                if found != expected:
                    continue
            end = line.find("]")
            if end < 0:
                continue
            ts = _parse_iso(line[1:end])
            if ts is not None and ts >= since:
                return line.strip()
    except Exception:
        return None
    return None


def _heartbeat_evidence(since: datetime) -> str | None:
    db_path = DATA_DIR / "grt.db"
    try:
        if not db_path.exists():
            return None
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                              timeout=5)
        try:
            row = con.execute(
                "SELECT alive_at, boot_at FROM heartbeat WHERE id=1"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    # W-check B1: only boot_at counts as heartbeat evidence.
    # alive_at advances every 5 minutes for ANY live process bound
    # to this data dir — including the OLD version still running
    # after a re-setup — so it cannot prove THIS boot happened.
    value = row[1]
    if value:
        ts = _parse_iso(str(value))
        if ts is not None and ts >= since:
            return f"heartbeat boot_at={value}"
    return None


def wait_for_boot(timeout: int = 120) -> str | None:
    since = _since()
    deadline = time.time() + timeout
    while True:
        evidence = (_startup_log_evidence(since)
                    or _heartbeat_evidence(since))
        if evidence:
            return evidence
        if time.time() >= deadline:
            return None
        time.sleep(2)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    timeout = 120
    if "--timeout" in argv:
        try:
            timeout = int(argv[argv.index("--timeout") + 1])
        except Exception:
            pass
    print(f"起動確認中です（最大 {timeout} 秒）...")
    evidence = wait_for_boot(timeout)
    if evidence:
        print("起動を確認しました: " + evidence)
        return 0
    print("制限時間内に起動を確認できませんでした。")
    print("data\\logs\\startup.log と data\\logs\\setup_tasks.log を確認してください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
