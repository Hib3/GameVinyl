"""Idempotent seed migration (agreement-24, Phase 1 acceptance #1).

Takes the bundled seed (seed/records.json, 3,777 editions) into SQLite.
Safe to run any number of times: every insert is INSERT OR IGNORE and the
migration never overwrites user-changed state (watch levels set later via
Telegram, settings, resolutions).

Backfill policy (spec): rows without a source URL stay 'unknown'; nothing
is bulk-assigned by guesswork. music_type / discogs_id are only filled
from the project's own existing data files when bundled in seed/.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import clock, config, db as dbmod
from .normalize import normalize_catalog

# NO_ROUTE concentrated prefixes named in the spec (agreement-05).
PRIORITY_PREFIXES = ["AY25", "07SH", "ETP", "28BLC", "K25G", "SJX", "DBR"]

# Promo clue words listed in the spec text (agreement-10).
PROMO_CLUES = [
    ("見本", "mihon", "spec agreement-10"),
    ("非売品", "not_for_sale", "spec agreement-10"),
    ("白ラベル", "white_label", "spec agreement-10"),
    ("PROMO", "promo", "spec agreement-10"),
    ("NOT FOR SALE", "not_for_sale", "spec agreement-10"),
    ("店頭用", "store_demo", "spec agreement-10"),
]

# Stage keywords listed in the spec text (agreement-13). 'restock' is
# deliberately NOT mapped: the spec lists it as a keyword example but does
# not fix its stage, so unmapped text falls back to 'announced' + review.
STAGE_KEYWORDS = [
    ("PRE-ORDER", "preorder_open"),
    ("PREORDER", "preorder_open"),
    ("REPRESS", "repress_announced"),
    ("SOLD OUT", "sold_out"),
]

# Generic condition grades defined by the spec (agreement-11 rank table).
CONDITION_GENERIC = [
    ("M", "M"), ("EX", "EX"), ("VG+", "VG+"), ("VG", "VG"), ("G", "G"),
]

COND_RANK = {"M": 5, "EX": 4, "VG+": 3, "VG": 2, "G": 1}


def _clean(v):
    if isinstance(v, str):
        return v.strip().strip("'")
    return v


def _load_seed_json(name: str):
    p = config.PROJECT_ROOT / "seed" / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def seed_dictionaries(db) -> None:
    # catalog_prefix_dict is intentionally NOT seeded (round-9 item
    # 9-A4(d)). Neither spec v3 (C-5.1) nor prompt v2 fixes its contents
    # or a derivation rule -- only the table shape (prefix, label). The
    # seed data does not determine one either: of the 482 non-empty
    # catalog prefixes in seed/records.json, 52 map to >= 2 distinct
    # label strings covering 1,809 rows (e.g. DATA -> 'Data Airlines' /
    # 'Data Discs'; K -> King Records / Nexus / Starchild / Victor), and
    # the label field carries combined co-credit strings and spelling
    # variants, so no prefix -> label function exists to seed from.
    # Seeding becomes possible when an authoritative prefix->label list
    # is approved, or the spec fixes an extraction rule for multi-catalog
    # strings plus an ambiguity policy and a label-normalization rule.
    # Until then the table stays empty, scoring's known_label floor
    # cannot fire, and normalize step 5 yields no stripped keys (see
    # normalize.normalize_catalog_keys). Do not guess rows into it.
    for prefix in PRIORITY_PREFIXES:
        db.execute(
            "INSERT OR IGNORE INTO priority_catalog_prefixes(prefix) VALUES(?)",
            (prefix,),
        )
    for clue, code, note in PROMO_CLUES:
        db.execute(
            "INSERT OR IGNORE INTO promo_clues(clue, clue_code, source_note)"
            " VALUES(?, ?, ?)",
            (clue, code, note),
        )
    for kw, stage in STAGE_KEYWORDS:
        db.execute(
            "INSERT OR IGNORE INTO stage_keywords(keyword, stage) VALUES(?, ?)",
            (kw, stage),
        )
    for raw, norm in CONDITION_GENERIC:
        db.execute(
            "INSERT OR IGNORE INTO condition_dict(source, raw_pattern, normalized, field)"
            " VALUES('generic', ?, ?, 'media')",
            (raw, norm),
        )
    db.commit()


def counts(db) -> dict:
    editions = db.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
    variants = db.execute("SELECT COUNT(*) FROM edition_variants").fetchone()[0]
    price_links = db.execute(
        "SELECT (SELECT COUNT(*) FROM price_snapshots WHERE edition_id IS NOT NULL)"
        " + (SELECT COUNT(*) FROM listings WHERE edition_id IS NOT NULL)"
    ).fetchone()[0]
    return {"editions": editions, "variants": variants, "price_links": price_links}


def run_migration(db, records_path: Path | None = None) -> dict:
    dbmod.init_schema(db)
    seed_dictionaries(db)

    before = counts(db)

    records = json.loads(
        (records_path or config.seed_records_path()).read_text(encoding="utf-8")
    )

    audit = _load_seed_json("music_type_audit.json") or {}
    music_types = audit.get("by_record_id", {}) if isinstance(audit, dict) else {}

    deals_data = _load_seed_json("deals_current.json") or {}
    deals = deals_data.get("deals", []) if isinstance(deals_data, dict) else []
    deal_ids: set[str] = set()
    deal_discogs: dict[str, int] = {}
    for d in deals:
        rid = _clean(d.get("record_id"))
        if rid:
            deal_ids.add(rid)
            did = _clean(d.get("discogs_release_id"))
            if did and str(did).isdigit():
                deal_discogs[rid] = int(did)

    inserted = 0
    for r in records:
        rid = r["id"]
        rarity = r.get("rarity")
        if rarity in ("R4", "R5"):
            watch_level = 2
        elif rid in deal_ids:
            watch_level = 1
        else:
            watch_level = 0
        mt = None
        if rid in music_types:
            mt = music_types[rid].get("music_type")
        cur = db.execute(
            """INSERT OR IGNORE INTO editions(
                 id, title, catalog, catalog_norm, label, year, format,
                 country, game, rarity, discogs_id, jan_status, music_type,
                 distribution_type, distribution_channel,
                 variant_completeness, watch_level)
               VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'none', ?, 'unknown',
                      'unknown', 'unknown', ?)""",
            (
                rid,
                r.get("title"),
                r.get("catalog"),
                normalize_catalog(r.get("catalog"))["norm"] or None,
                r.get("label"),
                r.get("year") if isinstance(r.get("year"), int) else None,
                r.get("format"),
                r.get("game"),
                rarity,
                deal_discogs.get(rid),
                mt,
                watch_level,
            ),
        )
        inserted += cur.rowcount

    # Baseline policy version (agreement-24): version 1 = as-shipped defaults.
    row = db.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0]
    if row == 0:
        db.execute(
            "INSERT INTO policy_versions(version_no, changed_at, changed_via,"
            " diff_json, note) VALUES(1, ?, 'migration', '{}', 'baseline defaults')",
            (clock.now_iso(db),),
        )
    db.commit()

    after = counts(db)
    report = {
        "before": before,
        "after": after,
        "inserted_this_run": inserted,
        "seed_records": len(records),
        "at": clock.now_iso(db),
    }
    log_dir = config.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "migration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def current_policy_version(db) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(version_no), 1) FROM policy_versions"
    ).fetchone()
    return int(row[0])


def main() -> None:
    db = dbmod.connect()
    report = run_migration(db)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
