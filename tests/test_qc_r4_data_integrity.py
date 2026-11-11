"""QC round 4 — data integrity regression tests.

Covers:
- Full-population (not sampled) reconciliation of seed/records.json
  against the migrated editions table, field by field.
- deals_current.json linkage as implemented in Phase 1 (discogs_id
  backfill + watch_level), per BUILD_REPORT_PHASE1.md section 5.
- music_type_audit.json reflection into editions.music_type.
- Schema constraint behaviour: PK / UNIQUE / CHECK enforcement and the
  round-4 fix that NULL identity values are rejected (SQLite TEXT
  PRIMARY KEY does not imply NOT NULL).

No network and no real tokens are used.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from grt import migrate
from grt.normalize import normalize_catalog

ROOT = Path(__file__).resolve().parents[1]


def _seed(name):
    return json.loads((ROOT / "seed" / name).read_text(encoding="utf-8"))


@pytest.fixture()
def migrated(db):
    migrate.run_migration(db)
    return db


def _deal_maps():
    deals = _seed("deals_current.json")["deals"]
    ids, discogs = set(), {}
    for d in deals:
        rid = str(d.get("record_id") or "").strip().strip("'")
        if rid:
            ids.add(rid)
            did = str(d.get("discogs_release_id") or "").strip().strip("'")
            if did.isdigit():
                discogs[rid] = int(did)
    return deals, ids, discogs


# --- 1. records.json full reconciliation -----------------------------------

def test_r4_records_full_field_reconciliation(migrated):
    records = _seed("records.json")
    audit = _seed("music_type_audit.json")["by_record_id"]
    _, deal_ids, deal_discogs = _deal_maps()
    rows = {r["id"]: dict(r) for r in migrated.execute("SELECT * FROM editions")}
    assert len(records) == 3777
    assert len(rows) == len(records)
    assert set(rows) == {r["id"] for r in records}
    for rec in records:
        rid = rec["id"]
        got = rows[rid]
        expected = {
            "title": rec.get("title"),
            "catalog": rec.get("catalog"),
            "catalog_norm": normalize_catalog(rec.get("catalog"))["norm"] or None,
            "label": rec.get("label"),
            "year": rec.get("year") if isinstance(rec.get("year"), int) else None,
            "format": rec.get("format"),
            "country": None,
            "game": rec.get("game"),
            "rarity": rec.get("rarity"),
            "discogs_id": deal_discogs.get(rid),
            "jan_status": "none",
            "music_type": audit[rid]["music_type"],
            "distribution_type": "unknown",
            "distribution_channel": "unknown",
            "variant_completeness": "unknown",
            "watch_level": 2 if rec.get("rarity") in ("R4", "R5")
            else (1 if rid in deal_ids else 0),
        }
        for field, want in expected.items():
            assert got[field] == want, f"{rid}.{field}: db={got[field]!r} seed={want!r}"


def test_r4_migration_idempotent_and_preserves_user_state(migrated):
    migrated.execute("UPDATE editions SET watch_level=0 WHERE id='AR-000001'")
    migrated.commit()
    report = migrate.run_migration(migrated)
    assert report["inserted_this_run"] == 0
    assert report["after"]["editions"] == 3777
    level = migrated.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'").fetchone()[0]
    assert level == 0  # user-changed state is never overwritten


# --- 2. deals_current.json linkage ------------------------------------------

def test_r4_deals_linkage_discogs_and_watch(migrated):
    deals, deal_ids, deal_discogs = _deal_maps()
    assert len(deals) == 284 and len(deal_ids) == 284
    rows = {r["id"]: dict(r) for r in migrated.execute(
        "SELECT id, discogs_id, watch_level FROM editions")}
    for rid, did in deal_discogs.items():
        assert rows[rid]["discogs_id"] == did
        assert rows[rid]["watch_level"] >= 1
    with_discogs = [rid for rid, r in rows.items() if r["discogs_id"] is not None]
    assert len(with_discogs) == 284
    # Phase-1 design (BUILD_REPORT sec.5): deal prices are NOT persisted.
    assert migrated.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert migrated.execute(
        "SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 0


# --- 3. music_type_audit.json reflection -------------------------------------

def test_r4_music_type_audit_reflected_in_full(migrated):
    audit = _seed("music_type_audit.json")
    by_id = audit["by_record_id"]
    assert len(by_id) == 3777
    mismatches = [
        rid for rid, v in by_id.items()
        if migrated.execute("SELECT music_type FROM editions WHERE id=?",
                            (rid,)).fetchone()[0] != v["music_type"]]
    assert mismatches == []
    dist = dict(migrated.execute(
        "SELECT music_type, COUNT(*) FROM editions GROUP BY music_type"))
    assert dist == audit["counts"]["music_type"]


# --- 4. schema constraints ----------------------------------------------------

def test_r4_pk_unique_check_enforced(db):
    db.execute("INSERT INTO editions(id) VALUES('AR-000001')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO editions(id) VALUES('AR-000001')")
    db.rollback()
    db.execute("INSERT INTO candidate_editions(fingerprint) VALUES('fp-1')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO candidate_editions(fingerprint) VALUES('fp-1')")
    db.rollback()
    db.execute("INSERT INTO notification_outbox(idempotency_key) VALUES('k-1')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO notification_outbox(idempotency_key) VALUES('k-1')")
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO heartbeat(id) VALUES(2)")
    db.rollback()
    db.execute(
        "INSERT INTO catalog_prefix_ledger(prefix, number) VALUES('AY25', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO catalog_prefix_ledger(prefix, number) VALUES('AY25', 1)")
    db.rollback()


def test_r4_null_identity_values_rejected(db):
    """Round-4 defect: TEXT PRIMARY KEY accepted NULL (SQLite quirk)."""
    statements = [
        "INSERT INTO editions(id) VALUES(NULL)",
        "INSERT INTO settings(key, value) VALUES(NULL, 'v')",
        "INSERT INTO catalog_prefix_dict(prefix) VALUES(NULL)",
        "INSERT INTO priority_catalog_prefixes(prefix) VALUES(NULL)",
        "INSERT INTO promo_clues(clue) VALUES(NULL)",
        "INSERT INTO deadline_kind_dict(pattern) VALUES(NULL)",
        "INSERT INTO stage_keywords(keyword) VALUES(NULL)",
        "INSERT INTO catalog_prefix_ledger(prefix, number) VALUES(NULL, 1)",
        "INSERT INTO catalog_prefix_ledger(prefix, number) VALUES('AY25', NULL)",
        "INSERT INTO condition_dict(source, raw_pattern, field)"
        " VALUES(NULL, 'M', 'media')",
        "INSERT INTO listing_copy_members(group_id, listing_id) VALUES(NULL, 1)",
        "INSERT INTO listing_copy_members(group_id, listing_id) VALUES(1, NULL)",
        "INSERT INTO notification_outbox(idempotency_key) VALUES(NULL)",
    ]
    for sql in statements:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
        db.rollback()
    assert db.execute("SELECT COUNT(*) FROM editions WHERE id IS NULL"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM settings WHERE key IS NULL"
                      ).fetchone()[0] == 0


def test_r4_candidate_fingerprint_nullable_until_fingerprinted(db):
    """candidate_editions.fingerprint stays nullable by design: the
    first-run journey registers a candidate before any fingerprint
    exists (tests/test_journey_integration.py). Duplicate non-NULL
    fingerprints are still rejected (see test_r4_pk_unique_check_enforced).
    A NOT NULL attempt in round 4 broke that journey and was reverted."""
    db.execute("INSERT INTO candidate_editions(title, status)"
               " VALUES('Cand', 'pending')")
    db.execute("INSERT INTO candidate_editions(title, status)"
               " VALUES('Cand2', 'pending')")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM candidate_editions"
                      " WHERE fingerprint IS NULL").fetchone()[0] == 2


def test_r4_foreign_keys_pragma_state_characterized(db):
    """PRAGMA foreign_keys is ON for app connections, but schema.sql (per
    spec v3 C-15) declares no REFERENCES, so no FK enforcement exists;
    referential integrity is application-layer by design. This pins the
    verified state so any future change is deliberate."""
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_list(listings)").fetchall() == []
    assert db.execute("PRAGMA foreign_key_list(editions)").fetchall() == []
