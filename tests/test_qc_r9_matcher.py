"""Round-9 matcher fixes (9-A4 / 9-A5 / 9-A11).

Pins, with no real tokens and no network:
- blocking reaches the evaluation core via the catalog numeric-part key,
  the VGMdb ID key and the doujin composite key (9-A4(a));
- collisions are stacked into duplicate_suspects, normal matches are
  not (9-A4(c), agreement-02);
- normalize step 5 produces dual keys only for registered prefixes, and
  catalog_prefix_dict stays unseeded because its contents are not
  determined by the spec or the seed data (9-A4(b)(d));
- the price-drop rule compares only inside one
  Edition x variant x cond_rank bucket (9-A11, agreement-11);
- variant_completeness carries its prior definition in code (9-A5,
  agreement-23).
"""
import json

from grt import matcher
from grt.normalize import normalize_catalog_keys
from grt.scoring import price_bucket_key, price_drop_qualifies_bucketed


def _edition(db, eid, *, title="テスト盤", catalog="KICA-1001",
             catalog_norm="KICA1001", label="King Records", year=1995,
             fmt="LP", country="JP", discogs_id=None, vgmdb_id=None,
             completeness="unknown"):
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm, label,"
        " year, format, country, discogs_id, vgmdb_id, distribution_type,"
        " variant_completeness, watch_level)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'retail', ?, 0)",
        (eid, title, catalog, catalog_norm, label, year, fmt, country,
         discogs_id, vgmdb_id, completeness))
    db.commit()


def _listing(**kw):
    base = {"raw_title": "テスト盤", "catalog_raw": "KICA-1001",
            "label": "King Records", "year": 1995, "format": "LP",
            "country": "JP", "route": "retail",
            "distribution_type": "retail", "description": ""}
    base.update(kw)
    return base


def _suspects(db):
    return db.execute("SELECT * FROM duplicate_suspects").fetchall()


# ---------------------------------------------------------------------------
# 9-A4(a) blocking

def test_blocking_numeric_part_reaches_other_prefix(db):
    _edition(db, "AR-N1", catalog="XYZ-1001", catalog_norm="XYZ1001")
    _edition(db, "AR-N2", catalog="ABC-2002", catalog_norm="ABC2002")
    # Catalog-only listing: no other blocking key can fire.
    got = matcher.find_candidate_editions(db, {"catalog_raw": "ABC-1001"})
    ids = {e["id"] for e in got}
    assert "AR-N1" in ids          # numeric part 1001 matches across prefixes
    assert "AR-N2" not in ids


def test_blocking_numeric_too_short_does_not_flood(db):
    _edition(db, "AR-S1", catalog="CD-12", catalog_norm="CD12")
    got = matcher.find_candidate_editions(db, {"catalog_raw": "AB-12"})
    assert got == []               # digit run "12" (< 3) is not a blocking key


def test_blocking_numeric_flood_suppression(db):
    for i in range(30):
        n = 3000 + i
        _edition(db, f"AR-F{i}", title=f"無関係盤{i}", catalog=f"ZZZ-{n}",
                 catalog_norm=f"ZZZ{n}")
    _edition(db, "AR-TGT", catalog="XYZ-1001", catalog_norm="XYZ1001")
    got = matcher.find_candidate_editions(db, {"catalog_raw": "ABC-1001"})
    assert {e["id"] for e in got} == {"AR-TGT"}


def test_blocking_vgmdb_id(db):
    _edition(db, "AR-V1", catalog=None, catalog_norm=None, vgmdb_id=777)
    _edition(db, "AR-V2", catalog=None, catalog_norm=None, vgmdb_id=888)
    got = matcher.find_candidate_editions(
        db, {"raw_title": None, "catalog_raw": None, "vgmdb_id": 777})
    assert {e["id"] for e in got} == {"AR-V1"}


def test_blocking_doujin_circle_title_without_year(db):
    _edition(db, "AR-D1", title="同人アレンジ盤", catalog=None,
             catalog_norm=None, year=2020)
    _edition(db, "AR-D2", title="別の同人盤", catalog=None,
             catalog_norm=None, year=2020)
    # No year on the listing: the classic title+year key cannot fire, the
    # doujin composite key (circle + title) must still collect AR-D1.
    got = matcher.find_candidate_editions(
        db, {"raw_title": "同人アレンジ盤", "catalog_raw": None,
             "circle": "音戯工房"})
    assert {e["id"] for e in got} == {"AR-D1"}


# ---------------------------------------------------------------------------
# 9-A4(c) duplicate_suspects (agreement-02)

def test_duplicate_edition_key_collision_stacked(db):
    _edition(db, "AR-C1")
    _edition(db, "AR-C2", title="テスト盤 別プレス")
    results = matcher.match_listing(db, _listing(), listing_id=1)
    assert results
    rows = [r for r in _suspects(db)
            if r["kind"] == matcher.DUPLICATE_KIND_EDITION]
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["edition_ids"] == ["AR-C1", "AR-C2"]


def test_duplicate_normal_match_stacks_nothing(db):
    _edition(db, "AR-OK1")
    results = matcher.match_listing(db, _listing(), listing_id=2)
    assert results
    assert _suspects(db) == []


def test_duplicate_discogs_conflict_stacked(db):
    _edition(db, "AR-G1", discogs_id=111)
    matcher.match_listing(db, _listing(discogs_id=222), listing_id=3)
    rows = [r for r in _suspects(db)
            if r["kind"] == matcher.DUPLICATE_KIND_DISCOGS]
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["listing_discogs_id"] == 222
    assert payload["edition_discogs_id"] == 111


def test_duplicate_variant_key_collision_stacked(db):
    _edition(db, "AR-W1")
    for _ in range(2):
        db.execute(
            "INSERT INTO edition_variants(edition_id, discogs_id,"
            " variant_kind) VALUES('AR-W1', 555, 'retail')")
    db.commit()
    matcher.match_listing(db, _listing(), listing_id=4)
    rows = [r for r in _suspects(db)
            if r["kind"] == matcher.DUPLICATE_KIND_VARIANT]
    assert len(rows) == 1


def test_duplicate_suspect_not_restacked_on_repeat(db):
    _edition(db, "AR-C1")
    _edition(db, "AR-C2", title="テスト盤 別プレス")
    matcher.match_listing(db, _listing(), listing_id=1)
    matcher.match_listing(db, _listing(), listing_id=1)
    rows = [r for r in _suspects(db)
            if r["kind"] == matcher.DUPLICATE_KIND_EDITION]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 9-A4(b)(d) catalog_prefix_dict / normalize step 5

def test_normalize_step5_dual_keys_with_registered_prefix():
    keys = normalize_catalog_keys("KICA-1001", prefixes=["KICA"])
    assert keys["norm"] == "KICA1001"
    assert keys["matched_prefix"] == "KICA"
    assert keys["stripped"] == "1001"


def test_normalize_step5_inert_without_registry():
    keys = normalize_catalog_keys("KICA-1001")
    assert keys["norm"] == "KICA1001"
    assert keys["matched_prefix"] is None
    assert keys["stripped"] is None
    other = normalize_catalog_keys("XYZW-1001", prefixes=["KICA"])
    assert other["stripped"] is None


def test_catalog_prefix_dict_left_unseeded(db):
    # Deliberate: contents are undetermined (see migrate.seed_dictionaries
    # note); the fix records the decision instead of inventing rows.
    n = db.execute("SELECT COUNT(*) FROM catalog_prefix_dict").fetchone()[0]
    assert n == 0


# ---------------------------------------------------------------------------
# 9-A11 price-drop bucket rule (agreement-11)

def test_bucket_key_none_for_null_cond_rank():
    assert price_bucket_key("AR-1", 5, None) is None
    assert price_bucket_key("AR-1", 5, 3) == ("AR-1", 5, 3)
    assert price_bucket_key("AR-1", None, 3) == ("AR-1", None, 3)


def test_bucket_null_cond_rank_never_qualifies(db):
    assert price_drop_qualifies_bucketed(
        db, "AR-1", 100, variant_id=5, cond_rank=None,
        baseline_price=10000) is False


def test_bucket_drop_within_bucket_explicit_baseline(db):
    q = price_drop_qualifies_bucketed
    assert q(db, "AR-1", 9000, variant_id=5, cond_rank=3,
             baseline_price=10000) is True    # exactly -10%
    assert q(db, "AR-1", 9001, variant_id=5, cond_rank=3,
             baseline_price=10000) is False   # only -9.99%
    assert q(db, "AR-1", 10000, variant_id=5, cond_rank=3,
             baseline_price=10000) is False   # no change
    assert q(db, "AR-1", 11000, variant_id=5, cond_rank=3,
             baseline_price=10000) is False   # price rose


def test_bucket_db_baseline_same_bucket_only(db):
    # W-check B3 fixture update: the fallback baseline is computed
    # over YEN values only, so the baseline row must state its
    # currency — a currency-less price is no longer assumed yen
    # (that assumption is what priced foreign listings in 円).
    db.execute(
        "INSERT INTO listings(edition_id, variant_id, cond_rank, price,"
        " currency, status) VALUES('AR-1', 5, 3, 10000, 'JPY',"
        " 'active')")
    db.commit()
    q = price_drop_qualifies_bucketed
    assert q(db, "AR-1", 8000, variant_id=5, cond_rank=3) is True
    # Another variant's bucket has no baseline: the AR-1/5/3 price must
    # NOT be borrowed across buckets (the 9-A11 defect).
    assert q(db, "AR-1", 8000, variant_id=6, cond_rank=3) is False
    assert q(db, "AR-1", 8000, variant_id=5, cond_rank=4) is False
    assert q(db, "AR-1", 8000, variant_id=None, cond_rank=3) is False


# ---------------------------------------------------------------------------
# 9-A5 variant_completeness prior definition (agreement-23)

def test_variant_completeness_prior_definition_present():
    assert matcher.VARIANT_COMPLETENESS_VALUES == (
        "unknown", "single_confirmed", "multiple")
    assert matcher.VARIANT_COMPLETENESS_DEFAULT == "unknown"
    rules = matcher.VARIANT_COMPLETENESS_RULES
    assert "two or more independent" in rules["single_confirmed"]
    assert "prohibited" in rules["no_speculative_grant"]
    # The undetermined remainder is named in code, not silently invented.
    assert len(rules["undetermined"]) >= 3
