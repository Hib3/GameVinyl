"""SQLite access and schema initialisation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql")


def connect(db_file: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_file) if db_file is not None else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False is REQUIRED: the resident process shares
    # connections across the Telegram poll thread and scheduler threads.
    # With the default (True), every cross-thread use raises
    # sqlite3.ProgrammingError and the bot silently dies on the first
    # update. timeout=30 gives SQLite a busy-wait budget under WAL.
    db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


# Columns added to schema.sql after the first shipped release. A DB
# created by an older version lacks them (CREATE TABLE IF NOT EXISTS
# never alters an existing table). The use sites also add them lazily
# (health.ensure_fetcher_kind_column, outbox._ensure_reply_markup_
# column); ensuring them here as well means an upgraded DB converges
# to the current schema at startup instead of depending on every
# future code path remembering the lazy ALTER.
_LATE_COLUMNS = (
    ("fetch_runs", "fetcher_kind", "TEXT"),
    ("notification_outbox", "reply_markup_json", "TEXT"),
    # W-check B2: 'sending' claim timestamp (timed orphan recovery)
    # and the producer's quiet-exemption flag (send-time quiet
    # re-judgement honours it).
    ("notification_outbox", "claimed_at", "TEXT"),
    ("notification_outbox", "quiet_exempt", "INTEGER DEFAULT 0"),
    # W-check B3: fetch_runs gains the normalized page hash under
    # its own name (the raw_hash column historically carried it,
    # mislabeled) and the retained failure-body path; copy groups
    # gain the condition rank a group was formed under; match
    # decisions record the winning variant.
    ("fetch_runs", "normalized_hash", "TEXT"),
    ("fetch_runs", "body_path", "TEXT"),
    ("listing_copy_groups", "cond_rank", "INTEGER"),
    ("match_decisions", "variant_id", "INTEGER"),
    # R14: per-suffix probe map on the sweep ledger (which suffixes
    # were probed for this number, at which dictionary version).
    ("catalog_prefix_ledger", "suffix_probe_json", "TEXT"),
    # W-check B4: candidate approval now catalogues the discovery —
    # the created edition's id is recorded on the candidate (also
    # the link REPRESS signals and schedules migrate through), and
    # a replaced deadline points at its successor instead of two
    # live rows both alerting.
    ("candidate_editions", "promoted_edition_id", "TEXT"),
    ("release_schedule", "superseded_by", "INTEGER"),
    # W-check B5: registration time of a schedule row. The
    # missed-deadline declaration asserts 'the process was down
    # when this passed' — a row REGISTERED after its own deadline
    # (a past event harvested late) must never trigger it.
    ("release_schedule", "created_at", "TEXT"),
    # W-check B4 R19: a shop-DECLARED out-of-stock (structured
    # availability) is a different fact from an inferred one-round
    # absence — the declared flag is what arms the variant_stock
    # REPRESS transition when the copy returns.
    ("listings", "oos_declared", "INTEGER DEFAULT 0"),
    ("listings", "image_url", "TEXT"),
)


def _ensure_column(db: sqlite3.Connection, table: str, column: str,
                   decl: str) -> None:
    cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if cols and column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# W-check B2 (seller D-1): the schema shipped with ZERO secondary
# indexes, so every hot lookup (previous snapshot, ingest listing
# dedupe, the outbox due-rows query run every minute) degraded to a
# full table scan as the history tables grew (~680k price_snapshots
# rows/year at the current watch size). Indexes are created here,
# one guarded statement at a time: a legacy DB missing a referenced
# column skips that index instead of failing the whole startup
# (the late-column ALTERs above run first, so real installs
# converge fully).
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_snapshots_edition"
    " ON price_snapshots(edition_id, source, taken_at)",
    "CREATE INDEX IF NOT EXISTS idx_listings_source_url"
    " ON listings(source, url)",
    "CREATE INDEX IF NOT EXISTS idx_listings_bucket"
    " ON listings(edition_id, variant_id, cond_rank)",
    "CREATE INDEX IF NOT EXISTS idx_fetch_runs_source_time"
    " ON fetch_runs(source_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_fetch_runs_source_result"
    " ON fetch_runs(source_id, result)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_due"
    " ON notification_outbox(state, not_before, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_system_events_kind_time"
    " ON system_events(kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_review_queue_dedupe"
    " ON review_queue(kind, fingerprint, resolved_at)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_status_found"
    " ON candidate_editions(status, found_at)",
    "CREATE INDEX IF NOT EXISTS idx_match_decisions_time"
    " ON match_decisions(decided_at)",
    "CREATE INDEX IF NOT EXISTS idx_used_market_search_checked"
    " ON used_market_search_state(last_checked, edition_id)",
    # W-check B3 (adversarial 4): listings identity is (source,
    # url); without a UNIQUE index the SELECT-then-INSERT in
    # register_ingested raced under concurrent lanes and produced
    # duplicate rows of one listing. Guarded like every index here:
    # a legacy DB that already carries duplicates skips the index
    # instead of failing startup.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_source_url_uq"
    " ON listings(source, url)",
)


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA.read_text(encoding="utf-8"))
    for table, column, decl in _LATE_COLUMNS:
        _ensure_column(db, table, column, decl)
    for stmt in _INDEXES:
        try:
            db.execute(stmt)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    # W-check B5 R24 (maniac B N-3): the match_review queue
    # priority rose 0 -> 45 in code, but rows already sitting in
    # a real DB from an older build kept priority 0 and the
    # starvation the change fixed. Idempotent backfill: the only
    # producer writes 45 now, so any unresolved 0-row is legacy.
    try:
        db.execute(
            "UPDATE review_queue SET priority=45 WHERE"
            " kind='match_review' AND priority=0"
            " AND resolved_at IS NULL")
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    db.commit()


def table_count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
