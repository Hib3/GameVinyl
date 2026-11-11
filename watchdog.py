#!/usr/bin/env python3
"""Entry point: one watchdog tick (Task Scheduler, every 15 minutes).

Entry hardening (W-check B1, AI engineer #1 / adversarial QA D-3):
the watchdog previously had NO failure logging at all — both launch
routes discard stderr, so a watchdog that died on a damaged DB left
no trace anywhere and silently stopped guarding. Any exception here
is now recorded in data/logs/watchdog_errors.log with a full
traceback; if the data directory itself is unwritable, the record
falls back to watchdog_failure.txt next to this file. The logger is
deliberately self-contained (stdlib only), like run.py's.
"""
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _append_capped(path: Path, text: str) -> None:
    """Append with a size cap (W-check B2, seller D-4). This file is
    deliberately stdlib-only, so the trim is inlined here rather
    than imported from grt.logtrim."""
    try:
        if path.exists() and path.stat().st_size > 5_000_000:
            data = path.read_bytes()[-1_000_000:]
            nl = data.find(b"\n")
            path.write_bytes(data[nl + 1:] if nl >= 0 else data)
    except Exception:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def _log_watchdog_failure(exc: BaseException) -> None:
    """Append the fatal traceback to watchdog_errors.log. Never raises."""
    stamp = datetime.now(timezone.utc).isoformat()
    text = (f"[{stamp}] WATCHDOG FAILURE: {exc!r}\n"
            + "".join(traceback.format_exception(exc)) + "\n")
    data_dir = os.environ.get("GRT_DATA_DIR")
    base = Path(data_dir) if data_dir else PROJECT_ROOT / "data"
    try:
        log_path = base / "logs" / "watchdog_errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _append_capped(log_path, text)
        return
    except Exception:
        pass
    try:
        _append_capped(PROJECT_ROOT / "watchdog_failure.txt", text)
    except Exception:
        pass
    print(f"[watchdog] fatal: {exc!r}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        from grt.watchdog import main

        main()
    except BaseException as exc:  # the guard must not die silently
        _log_watchdog_failure(exc)
        raise SystemExit(1)
