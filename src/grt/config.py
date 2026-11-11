"""Configuration: paths, environment (.env in data/), and settings access.

Layout rule (user decision): code lives in the project tree; user data
(SQLite DB, .env, logs) lives under ``data/`` so that replacing the code
never touches user data.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    env = os.environ.get("GRT_DATA_DIR")
    if env:
        return Path(env)
    return PROJECT_ROOT / "data"


def db_path() -> Path:
    env = os.environ.get("GRT_DB_PATH")
    if env:
        return Path(env)
    return data_dir() / "grt.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def seed_records_path() -> Path:
    return PROJECT_ROOT / "seed" / "records.json"


# Keys that data/.env is allowed to supply (QC R6-D7 fix). Only keys the
# production code actually reads from the environment belong here. In
# particular the test-clock offset GRT_TEST_CLOCK_OFFSET_SECONDS is NOT
# in this list on purpose: its legitimate supply routes are (a) a real
# environment variable set by the test runner itself, (b) the settings
# table key ``test_clock_offset_seconds`` (the spec's defined route), or
# (c) the process-local fallback in grt.clock. A line in the operator-
# editable .env file must never be able to shift every timestamp.
# ALLOWED_CHAT_ID is likewise absent on purpose (spec section 11): the
# allowlist's source of truth is the settings table, not the env.
DOTENV_ALLOWED_KEYS = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "DISCOGS_TOKEN",
    "EBAY_OAUTH_TOKEN",
    "RAKUTEN_APP_ID",
    "RAKUTEN_ACCESS_KEY",
    "YAHOO_APP_ID",
    "GRT_DATA_DIR",
    "GRT_DB_PATH",
})


def read_env_text(env_file) -> str:
    """Decode a user-edited .env file without ever raising.

    W-check B1 (adversarial QA D-1/D-2): the user edits data/.env in
    Windows editors. Notepad can save UTF-16 ("Unicode") or a
    Japanese editor can save Shift_JIS; a BOM makes the first key
    unreadable. Strict UTF-8 decoding raised UnicodeDecodeError and
    crash-looped the bot via the watchdog, and a BOM silently hid
    the token. Decode chain: UTF-16 BOM -> utf-16; UTF-8 BOM ->
    utf-8-sig; strict UTF-8; cp932 fallback; finally UTF-8 with
    replacement so a damaged file degrades to "keys not found"
    (the setup gate's honest answer) instead of an exception.
    """
    try:
        raw = env_file.read_bytes()
    except Exception:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    # BOM-less UTF-16 (some editors save it): ASCII-heavy UTF-16
    # text is full of NUL bytes on one side of each character.
    head = raw[:200]
    if head.count(0) >= 8:
        odd_nuls = sum(1 for i in range(1, len(head), 2)
                       if head[i] == 0)
        even_nuls = sum(1 for i in range(0, len(head), 2)
                        if head[i] == 0)
        if odd_nuls > even_nuls:
            return raw.decode("utf-16-le", errors="replace")
        if even_nuls > odd_nuls:
            return raw.decode("utf-16-be", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def load_dotenv() -> None:
    """Load data/.env into os.environ (without overriding real env vars).

    Only DOTENV_ALLOWED_KEYS are imported; every other key in the file
    is ignored.
    """
    env_file = data_dir() / ".env"
    if not env_file.exists():
        return
    for line in read_env_text(env_file).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in DOTENV_ALLOWED_KEYS and key not in os.environ:
            os.environ[key] = value


def telegram_bot_token() -> str | None:
    load_dotenv()
    return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def discogs_token() -> str | None:
    load_dotenv()
    return os.environ.get("DISCOGS_TOKEN") or None


# ---------------------------------------------------------------------------
# settings table helpers

def get_setting(db, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def set_setting(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()
