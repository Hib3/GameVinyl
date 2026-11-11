"""Single clock abstraction for the whole system.

All "current time" reads in the application MUST go through :func:`now` so
that acceptance tests can move time forward deterministically with the
``test_clock_offset_seconds`` setting (production default: 0).

Timestamps are stored as ISO-8601 strings in JST (+09:00).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# Process-local fallback offset (tests may set this directly).
_local_offset_seconds = 0

# Test offsets simulate hours/days of clock movement, never years. A
# stored value outside ±365 days is corrupt, not a request: accepting it
# would make datetime arithmetic raise OverflowError inside now() and
# silently kill every caller (debugger-14 finding, 2026-11-08).
_MAX_OFFSET_SECONDS = 31_536_000  # 365 days


def _bounded_offset(raw) -> int | None:
    """Parse an offset supply value; None when unparsable or absurd."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if abs(value) > _MAX_OFFSET_SECONDS:
        return None
    return value


def set_local_offset(seconds: int) -> None:
    global _local_offset_seconds
    _local_offset_seconds = int(seconds)


def _offset_seconds(db=None) -> int:
    # Environment variable wins (used by test runners), then the settings
    # table, then the process-local fallback. The environment variable is
    # honoured only when set in the real process environment:
    # config.load_dotenv deliberately refuses to import this key from
    # data/.env (see DOTENV_ALLOWED_KEYS in grt.config, QC R6-D7), so a
    # stray line in the production .env cannot shift the clock. The
    # settings-table route (key ``test_clock_offset_seconds``) is the
    # spec-defined in-app supply path.
    env = os.environ.get("GRT_TEST_CLOCK_OFFSET_SECONDS")
    if env not in (None, ""):
        bounded = _bounded_offset(env)
        if bounded is not None:
            return bounded
    if db is not None:
        try:
            row = db.execute(
                "SELECT value FROM settings WHERE key='test_clock_offset_seconds'"
            ).fetchone()
            if row and row[0] not in (None, ""):
                bounded = _bounded_offset(row[0])
                if bounded is not None:
                    return bounded
        except Exception:
            pass
    return max(-_MAX_OFFSET_SECONDS,
               min(_MAX_OFFSET_SECONDS, _local_offset_seconds))


def now(db=None) -> datetime:
    """Current time as an aware datetime in JST, including any test offset."""
    return datetime.now(JST) + timedelta(seconds=_offset_seconds(db))


def now_iso(db=None) -> str:
    return now(db).isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST).isoformat(timespec="seconds")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt


def offset_seconds(db=None) -> int:
    """Public view of the currently effective test-clock offset.

    W-check B2 (wholesaler D-9): the settings-table offset is a live
    production input, and before this accessor nothing anywhere
    surfaced it — a stray row shifted every deadline and quiet
    judgement invisibly. Startup (main) and diagnose now report any
    nonzero value loudly.
    """
    return _offset_seconds(db)
