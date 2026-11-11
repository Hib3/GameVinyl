"""Size-capped plain-text logs.

W-check B2 (seller D-4): every log the tool writes was append-only
with no rotation — poll_errors.log alone grows ~5 MiB/day during a
persistent failure episode (5-second poll retry, one traceback per
failure), and the readers (diagnose, last_startup_record) loaded the
WHOLE file to show a tail, so the diagnostic path itself degraded
exactly when it was needed. Appends here trim the file to a tail
when it passes the cap, and tail reads only ever load the end of
the file.
"""
from __future__ import annotations

from pathlib import Path

MAX_LOG_BYTES = 5_000_000
KEEP_TAIL_BYTES = 1_000_000


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return
        with open(path, "rb") as fh:
            fh.seek(-KEEP_TAIL_BYTES, 2)
            tail = fh.read()
        # Drop the partial first line so the file still starts on a
        # line boundary.
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        tmp = path.with_suffix(path.suffix + ".trim-tmp")
        tmp.write_bytes(tail)
        tmp.replace(path)
    except Exception:
        pass


def append_text(path, text: str) -> bool:
    """Append text to a log file, trimming first when oversized.

    Never raises (a logging failure must not kill the code path that
    was trying to record a failure). Returns True when written.
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            _trim(p)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except Exception:
        return False


def tail_lines(path, count: int, *, max_bytes: int = 262_144) -> list[str]:
    """The last `count` lines of a file, reading only its tail."""
    try:
        p = Path(path)
        if not p.exists():
            return []
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, 2)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-count:] if count > 0 else []
    except Exception:
        return []
