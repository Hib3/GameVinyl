"""Unit spot checks for the matcher core (agreements 02/03/20)."""
from grt.matcher import evaluate
from grt.normalize import barcode_check_digit_ok

ED = {"title": "テスト サウンドトラック", "catalog_norm": "KICA1001",
      "label": "King Records", "year": 1995, "format": "LP", "country": "JP",
      "distribution_type": "retail", "variant_completeness": "unknown"}


def test_barcode_check_digit():
    assert barcode_check_digit_ok("4988000000006") in (True, False)  # smoke
    assert not barcode_check_digit_ok("12345")
    assert not barcode_check_digit_ok(None)


def test_catalog_exact_high_existence():
    listing = {"raw_title": "テスト サウンドトラック", "catalog_raw": "KICA-1001",
               "label": "King Records", "year": 1995, "format": "LP",
               "country": "JP", "route": "retail",
               "distribution_type": "retail"}
    r = evaluate(listing, dict(ED), None, promo_clue_codes=[])
    assert r["existence_confidence"] == "高"
    assert r["score"] >= 100


def test_media_veto_blocks_high():
    listing = {"raw_title": "テスト サウンドトラック", "catalog_raw": "KICA-1001",
               "label": "King Records", "year": 1995, "format": "CD",
               "country": "JP", "route": "retail",
               "distribution_type": "retail"}
    r = evaluate(listing, dict(ED), None, promo_clue_codes=[])
    assert "media_mismatch" in r["vetoes"]
    assert r["existence_confidence"] == "中"
    assert r["disposition"] == "review"


def test_below_threshold_is_low():
    listing = {"raw_title": None, "catalog_raw": "XXXX-9999",
               "route": "retail", "distribution_type": "retail"}
    r = evaluate(listing, dict(ED), None, promo_clue_codes=[])
    assert r["existence_confidence"] == "低"
    assert r["disposition"] == "candidate"


def test_promo_clue_forces_review():
    listing = {"raw_title": "テスト サウンドトラック", "catalog_raw": "KICA-1001",
               "label": "King Records", "year": 1995, "format": "LP",
               "country": "JP", "route": "retail",
               "distribution_type": "unknown",
               "description": "見本盤です"}
    ed = dict(ED, variant_completeness="single_confirmed")
    r = evaluate(listing, ed, None, promo_clue_codes=["mihon"])
    assert r["disposition"] == "review"
    assert r["price_confidence"] != "高"
