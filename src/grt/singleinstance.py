"""Single-instance guard for the resident process.

Nothing else in the system prevents two copies of run.py from running
at once — the logon task, a watchdog-spawned restart, and a manual
start can all coexist, and every copy polls the same Telegram bot
token, so updates are split between processes and getUpdates calls
conflict (HTTP 409). The watchdog's restart is the automated source
of duplicates, but the guard belongs at the entry point so every
launch path is covered.

The lock is an OS file lock on ``data/main.lock`` (msvcrt on Windows,
fcntl on POSIX) held for the whole process lifetime. It is released
by the OS when the holding process dies, so a crashed instance never
leaves a stale lock behind — unlike a pidfile, there is nothing to
clean up and no way for a leftover file to block a legitimate start.
"""
from __future__ import annotations

import os
from pathlib import Path

_lock_handle = None


def acquire(lock_path: Path):
    """Try to take the instance lock.

    Returns an open file handle that the caller must keep referenced
    for the process lifetime, or None when another live instance
    already holds the lock.
    """
    global _lock_handle
    if _lock_handle is not None:
        return _lock_handle
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    _lock_handle = fh
    return fh


def release() -> None:
    """Drop the lock (used by tests; a real process just exits)."""
    global _lock_handle
    if _lock_handle is not None:
        try:
            _lock_handle.close()
        except Exception:
            pass
        _lock_handle = None
