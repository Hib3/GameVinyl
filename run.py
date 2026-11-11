#!/usr/bin/env python3
"""Entry point: resident main process (started at logon by Task Scheduler).

Entry hardening: any exception that escapes startup — including an
import failure inside grt itself — is recorded with a full traceback
in data/logs/startup.log before the process dies, so a silent
"nothing happened" start becomes a diagnosable one. The failure
logger here is deliberately self-contained (stdlib only, no grt
imports): it must still work when grt is exactly what is broken.
It resolves the data directory the same way grt.config.data_dir
does (GRT_DATA_DIR env, else <project>/data).
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


def _startup_log_path() -> Path:
    data_dir = os.environ.get("GRT_DATA_DIR")
    base = Path(data_dir) if data_dir else PROJECT_ROOT / "data"
    return base / "logs" / "startup.log"


def _log_startup_failure(exc: BaseException) -> None:
    """Append the fatal traceback to startup.log. Never raises.

    W-check B1 (adversarial QA D-4): if the data directory itself is
    the thing that is broken (read-only, locked, a file in the way),
    the record falls back to startup_failure.txt next to run.py so a
    pythonw launch still leaves SOME trace on disk.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    text = (f"[{stamp}] STARTUP FAILURE in run.py: {exc!r}\n"
            + "".join(traceback.format_exception(exc)) + "\n")
    try:
        path = _startup_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_capped(path, text)
        print(f"[startup] fatal: {exc!r} (see {path})",
              file=sys.stderr, flush=True)
        return
    except Exception:
        pass
    try:
        fallback = PROJECT_ROOT / "startup_failure.txt"
        _append_capped(fallback, text)
        print(f"[startup] fatal: {exc!r} (see {fallback})",
              file=sys.stderr, flush=True)
        return
    except Exception:
        pass
    print(f"[startup] fatal: {exc!r}", file=sys.stderr, flush=True)


def _refuse_second_instance() -> None:
    """Record a refused duplicate start. Never raises.

    The watchdog launches run.py with stdout/stderr discarded, so a
    duplicate start refused here would otherwise leave no trace at all.
    """
    msg = ("[startup] another Game Record Tool instance is already "
           "running; this second instance will exit.")
    try:
        from grt import config

        log_dir = config.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        _append_capped(
            log_dir / "duplicate_instance.log",
            f"[{stamp}] second instance refused "
            "(single-instance lock held by a live process)\n")
    except Exception:
        pass
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        # Single-instance guard (debugger 13/20): the logon task, a
        # watchdog-spawned restart and a manual start must never end
        # up as two resident processes polling the same bot token.
        # main.run() stays unguarded on purpose (in-process tests drive
        # it directly); the guard lives at this entry point only.
        from grt import config, singleinstance

        config.load_dotenv()
        _instance_lock = singleinstance.acquire(
            config.data_dir() / "main.lock")
        if _instance_lock is None:
            _refuse_second_instance()
            raise SystemExit(0)

        from grt.main import run

        run()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        _log_startup_failure(exc)
        raise
