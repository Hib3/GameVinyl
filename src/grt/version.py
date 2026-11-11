"""Version identity and the startup proof log.

Why this exists: a resident process that starts correctly used to
leave no externally checkable trace of WHICH code booted, or that it
booted at all — when the bot stayed silent there was nothing on the
user's disk to distinguish "new code running" from "old code still
running" or "never started". This module fixes that:

- the shipped version lives in the ``VERSION`` file at the project
  root (single source of truth, readable without running anything);
- :func:`write_startup_log` appends one boot record per process
  start to ``data/logs/startup.log`` (version, time, DB path, gate
  state, pid, interpreter);
- :func:`code_mtime_iso` reports the newest source-file mtime so the
  diagnose tool can show whether the code on disk is the code the
  user believes they installed.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from . import clock, config

_cached_version: str | None = None


def app_version() -> str:
    """The shipped version string from the project-root VERSION file.

    Falls back to "unknown" (never raises) when the file is missing,
    so a damaged install still boots and still says so honestly.
    """
    global _cached_version
    if _cached_version is None:
        try:
            text = (config.PROJECT_ROOT / "VERSION").read_text(
                encoding="utf-8").strip()
            _cached_version = text.splitlines()[0].strip() if text else "unknown"
        except Exception:
            _cached_version = "unknown"
    return _cached_version


def code_mtime() -> datetime | None:
    """Newest mtime among the shipped code files, or None if none found."""
    candidates: list[Path] = []
    src = config.PROJECT_ROOT / "src"
    if src.exists():
        candidates.extend(src.rglob("*.py"))
    for name in ("run.py", "watchdog.py", "diagnose.py", "setup_tasks.py",
                 "wait_start.py", "watchdog_loop.py"):
        p = config.PROJECT_ROOT / name
        if p.exists():
            candidates.append(p)
    newest: float | None = None
    for p in candidates:
        try:
            ts = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, clock.JST)


def code_mtime_iso() -> str:
    mtime = code_mtime()
    return mtime.isoformat(timespec="seconds") if mtime else "unknown"


def startup_log_path() -> Path:
    return config.log_dir() / "startup.log"


def write_startup_log(*, db_file, gate_open: bool,
                      note: str = "boot") -> Path | None:
    """Append one boot record to data/logs/startup.log.

    Record content (single line, key=value): timestamp (JST, via the
    app clock), version, python version, pid, DB path, gate state.
    This is the on-disk proof that THIS build started. Never raises:
    if the log cannot be written the failure is echoed to stderr
    instead of killing the startup it was meant to witness.
    """
    stamp = clock.now_iso()
    gate = "open" if gate_open else "closed"
    line = (
        f"[{stamp}] START version={app_version()}"
        f" python={sys.version.split()[0]} pid={os.getpid()}"
        f" db={db_file} gate={gate} note={note}"
    )
    try:
        path = startup_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        from . import logtrim
        if not logtrim.append_text(path, line + "\n"):
            raise OSError("log append failed")
        return path
    except Exception as exc:
        print(f"[startup] could not write startup log: {exc!r}",
              file=sys.stderr, flush=True)
        return None


def last_startup_record() -> str | None:
    """The most recent non-empty line of startup.log, or None."""
    try:
        path = startup_log_path()
        if not path.exists():
            return None
        # Tail-only read (W-check B2, seller D-4): never load the
        # whole log to find its last line.
        from . import logtrim
        lines = logtrim.tail_lines(path, 1)
        return lines[-1] if lines else None
    except Exception:
        return None
