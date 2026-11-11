"""Hourly fallback for Windows installations where Task Scheduler registration fails."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("GRT_DATA_DIR") or ROOT / "data")


def run_once() -> int:
    log = DATA / "site" / "freshness_loop.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > 5_000_000:
        tail = log.read_bytes()[-1_000_000:]
        cut = tail.find(b"\n")
        log.write_bytes(tail[cut + 1:] if cut >= 0 else tail)
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "site" / "update_site.py")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600)
        output = result.stdout + result.stderr
        code = result.returncode
    except Exception as exc:
        output, code = f"{type(exc).__name__}: {exc}", 1
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"[{stamp}] exit={code} elapsed={time.monotonic()-started:.1f}s\n{output}\n")
    return code


def main() -> None:
    while True:
        started = time.monotonic()
        run_once()
        time.sleep(max(1, 3600 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
