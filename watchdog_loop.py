#!/usr/bin/env python3
"""Looping watchdog — used ONLY by the Startup-folder fallback.

The normal install registers watchdog.py with Task Scheduler every
15 minutes. The Startup-folder fallback has no scheduler, so this
script stays resident instead: it runs one watchdog tick, sleeps
15 minutes, and repeats. It is started at logon by the
GameRecordToolWatchdog shortcut (via pythonw, no console window).

Stdlib only. Never raises: a failed tick must not kill the loop.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TICK_SECONDS = 900  # 15 minutes, matching the Task Scheduler cadence
START_DELAY_SECONDS = 120  # allow the main process to refresh a stale heartbeat


def _python() -> Path:
    scripts = ROOT / ".venv" / "Scripts"
    for name in (("pythonw.exe", "python.exe") if os.name == "nt" else ("python.exe",)):
        candidate = scripts / name
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def tick() -> None:
    try:
        subprocess.run([str(_python()), str(ROOT / "watchdog.py")],
                       cwd=str(ROOT), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=600,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:
        pass


def main():
    sys.path.insert(0, str(ROOT / "src"))
    from grt import singleinstance
    lock_dir = Path(os.environ.get("GRT_DATA_DIR") or ROOT / "data")
    lock = singleinstance.acquire(lock_dir / "watchdog_loop.lock")
    if lock is None:
        return 0
    time.sleep(START_DELAY_SECONDS)
    while True:
        tick()
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
