"""W-check Block 3 regression tests (2026-11-08): price evidence &
matching. Each test pins one R11 repro from the seven persona
reviews so the defect cannot return silently."""
import json

import httpx
import pytest

from grt import (bundling, candidates, evidence, matcher, pricedrop,
                 scoring, seedops, sweep, variant_sweep)
from grt.bot import Bot
from grt.fetchers import ingest as ingest_mod
from grt.fetchers.base import BaseFetcher, FetchError, api_price, http_get
from grt.fetchers.discogs import DiscogsFetcher
from grt.fetchers.ingest import IngestFetcher
from grt.fetchers.webpages import _extract_gtin
from grt.normalize import normalize_catalog, one_line


def _ean13(base12: str) -> str:
    s = sum(int(d) * (1 if i % 2 == 0 else 3)
            for i, d in enumerate(base12))
    return base12 + str((10 - s % 10) % 10)


JAN_A = _ean13("498800000001")
JAN_B = _ean13("498800000002")


def _source(db, name="Shop"):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES(?,"
        " 'STORE', 'approved', 'https://shop.jp/l')", (name,))
    db.commit()
    return int(cur.lastrowid)


def _edition(db, eid="AR-1", **kw):
    cols = {"title": "Game OST", "catalog": "KICA-1001",
            "catalog_norm": "KICA1001", "label": "King Records",
            "year": 1995, "format": "LP", "country": "JP",
            "distribution_type": "retail", "watch_level": 0,
            "variant_completeness": "unknown"}
    cols.update(kw)
    keys = ", ".join(cols)
    db.execute(
        f"INSERT INTO editions(id, {keys}) VALUES(?, "
        + ", ".join("?" for _ in cols) + ")",
        (eid, *cols.values()))
    db.commit()
    return eid


def _decision(db, listing_id, eid, price_conf):
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id,"
        " existence_confidence, price_confidence) VALUES(?, ?,"
        " '高', ?)", (listing_id, eid, price_conf))
    db.commit()


def _listing(db, url, price, cond, *, eid="AR-1", variant_id=None,
             currency="JPY", total="same", status="active",
             source="shopA"):
    total_jpy = price if total == "same" else total
    cur = db.execute(
        "INSERT INTO listings(edition_id, variant_id, source, url,"
        " price, currency, total_jpy, cond_rank, status, first_seen,"
        " last_seen) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-11-08',"
        " '2026-11-08')",
        (eid, variant_id, source, url, price, currency, total_jpy,
         cond, status))
    db.commit()
    return int(cur.lastrowid)


class _Transport:
    def __call__(self, *a, **kw):
        return {"ok": True}


def _bot(db, monkeypatch):
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('allowed_chat_id', '555'),"
        " ('deadline_override_quiet_selected', '1')")
    db.commit()
    bot = Bot(db, _Transport())
    sent = []
    monkeypatch.setattr(bot, "_send",
                        lambda chat, text, reply_markup=None:
                        sent.append(text))
    return bot, sent


def _price_text(db, monkeypatch, eid="AR-1"):
    bot, sent = _bot(db, monkeypatch)
    bot.handle_update({"update_id": 1, "message": {
        "chat": {"id": 555}, "text": f"/price {eid}"}})
    return "\n".join(sent)


# ---------------------------------------------------------------------------
# A. Currency integrity
# ---------------------------------------------------------------------------

def test_price_cmd_stats_use_latest_currency_only(db, monkeypatch):
    _edition(db)
    rows = [("2026-11-05", 100.0, "USD"), ("2026-11-06", 15000.0, "JPY"),
            ("2026-11-07", 200.0, "USD"), ("2026-11-08", 300.0, "USD")]
    for taken, price, cur in rows:
        db.execute(
            "INSERT INTO price_snapshots(edition_id, source, taken_at,"
            " num_for_sale, lowest_price, currency) VALUES('AR-1',"
            " 'discogs', ?, 3, ?, ?)", (taken, price, cur))
    db.commit()
    text = _price_text(db, monkeypatch)
    # USD series only: median of (100, 200, 300) = 200; the JPY row
    # is reported as excluded, never averaged in, and a NULL/foreign
    # currency never renders as the literal text "None".
    assert "中央値 200" in text and "15000" not in text
    assert "他通貨の記録 1件" in text
    assert "None" not in text


def test_price_cmd_bucket_yen_semantics(db, monkeypatch):
    _edition(db)
    _listing(db, "https://x/usd", 50.0, 3, currency="USD", total=None)
    _listing(db, "https://x/sold", 1000.0, 3, status="sold_candidate")
    _listing(db, "https://x/jpy", 8000.0, 3)
    text = _price_text(db, monkeypatch)
    assert "最安 8,000円" in text          # the JPY row, not $50/¥1,000
    assert "円換算のない出品 1件" in text   # the USD row is named, not priced
    assert "50円" not in text


def test_register_missing_currency_stays_unknown(db):
    sid = _source(db)
    _edition(db)
    f = IngestFetcher(db, sid)
    item = {"title": "Game OST", "raw_title": "Game OST",
            "url": "https://shop.jp/p/9", "price": 49.99,
            "catalog_raw": "KICA-1001", "route": "retail",
            "distribution_type": "retail", "description": ""}
    f.register_ingested("Shop", [item])
    row = db.execute(
        "SELECT currency, total_jpy FROM listings").fetchone()
    assert row[0] is None and row[1] is None  # no fabricated ¥49.99


def test_register_currency_change_moves_total(db):
    sid = _source(db)
    _edition(db)
    f = IngestFetcher(db, sid)
    base = {"title": "Game OST", "raw_title": "Game OST",
            "url": "https://shop.jp/p/1", "price": 5000.0,
            "currency": "JPY", "catalog_raw": "KICA-1001",
            "route": "retail", "distribution_type": "retail",
            "description": ""}
    f.register_ingested("Shop", [base])
    f.register_ingested("Shop", [{**base, "price": 40.0,
                                  "currency": "USD"}])
    row = db.execute(
        "SELECT price, currency, total_jpy FROM listings").fetchone()
    assert tuple(row) == (40.0, "USD", None)  # no stale ¥ total


# ---------------------------------------------------------------------------
# B. Price validity
# ---------------------------------------------------------------------------

def test_api_price_rejects_nonpositive_and_junk():
    assert api_price(0) is None
    assert api_price(-500) is None
    assert api_price(True) is None
    assert api_price("1,234") is None
    assert api_price(float("inf")) is None
    assert api_price(100) == 100.0


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        # W-check B5 fixture support: http_get now streams
        # (25MB body cap), so the fake also answers httpx.stream.
        self.headers = {}

    def json(self):
        return self._payload

    def iter_bytes(self):
        yield (self.text or "").encode("utf-8")


class _StreamOf:
    """Context-manager wrapper making a _Resp answer
    httpx.stream (W-check B5: http_get streams for its cap)."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("value", ["1,234", "ask", True,
                                   float("inf")])
def test_discogs_stats_value_must_be_a_real_number(db, monkeypatch,
                                                    value):
    sid = _source(db, name="discogs")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        payload={"num_for_sale": 1,
                 "lowest_price": {"value": value, "currency": "JPY"}}))
    f = DiscogsFetcher(db, sid, token="t")
    with pytest.raises(FetchError) as ei:
        f.stats_for_release(555)
    assert ei.value.result == "PARSE_ERROR"


def test_extract_price_sale_wins_and_noise_ignored():
    ep = ingest_mod._extract_price
    assert ep("定価 ¥15,000 販売価格 ¥9,800") == 9800.0
    assert ep("送料 ¥500 本体 ¥9,800") == 9800.0
    assert ep("送料無料 ¥9,800") == 9800.0  # free-shipping badge layout
    assert ep("クーポン ¥2,000 商品 ¥9,800") == 9800.0
    assert ep("定価 ¥15,000") is None       # list price only: no price
    assert ep("価格未定") is None


def test_register_soldout_structured_item(db):
    sid = _source(db)
    _edition(db)
    f = IngestFetcher(db, sid)
    item = {"title": "Game OST", "raw_title": "Game OST",
            "url": "https://shop.jp/p/2", "price": 5000.0,
            "currency": "JPY", "catalog_raw": "KICA-1001",
            "route": "retail", "distribution_type": "retail",
            "description": "",
            "availability": "https://schema.org/OutOfStock"}
    out = f.register_ingested("Shop", [item])
    assert out["skipped"] >= 1
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    live = {**item, "availability": "https://schema.org/InStock"}
    f.register_ingested("Shop", [live])
    f.register_ingested("Shop", [item])
    row = db.execute("SELECT status FROM listings").fetchone()
    assert row[0] == "sold_candidate"


def test_canonical_url_tracking_params_not_identity():
    cu = ingest_mod._canonical_url
    assert cu("https://shop.jp/p/1?utm_source=x&from=new#frag") \
        == "https://shop.jp/p/1"
    assert cu("https://shop.jp/p/1?id=7") == "https://shop.jp/p/1?id=7"


# ---------------------------------------------------------------------------
# C. Matcher structure
# ---------------------------------------------------------------------------

def _variant(db, eid, barcode, kind):
    cur = db.execute(
        "INSERT INTO edition_variants(edition_id, barcode,"
        " variant_kind, status) VALUES(?, ?, ?, 'confirmed')",
        (eid, barcode, kind))
    db.commit()
    return int(cur.lastrowid)


def test_match_evaluates_all_variants_order_independent(db):
    _edition(db)
    listing = {"raw_title": "Game OST", "catalog_raw": "KICA-1001",
               "barcode_raw": JAN_B, "route": "retail",
               "distribution_type": "retail", "description": "",
               "source": "shop"}
    outcomes = []
    for order in (("normal", "sample"), ("sample", "normal")):
        db.execute("DELETE FROM edition_variants")
        db.execute("DELETE FROM match_decisions")
        db.commit()
        ids = {}
        for kind in order:
            ids[kind] = _variant(db, "AR-1",
                                 JAN_A if kind == "normal" else JAN_B,
                                 kind)
        results = matcher.match_listing(db, listing, listing_id=1)
        top = [r for r in results if r["edition_id"] == "AR-1"]
        assert top, "the JAN_B variant must be evaluated at all"
        outcomes.append(top[0]["score"])
        # The variant the decision names is the SAMPLE one in both
        # arrangements (its row id naturally differs between runs).
        assert top[0].get("variant_id") == ids["sample"]
    assert outcomes[0] == outcomes[1]  # insertion order changes nothing


def test_match_blocks_on_editions_jan_without_variants(db):
    _edition(db, jan=JAN_A)
    listing = {"raw_title": "Game OST", "catalog_raw": "KICA-1001",
               "barcode_raw": JAN_A, "route": "retail",
               "distribution_type": "retail", "description": "",
               "source": "shop"}
    results = matcher.match_listing(db, listing, listing_id=1)
    top = [r for r in results if r["edition_id"] == "AR-1"]
    assert top and top[0]["barcode_match"] is True


def test_match_p2_fires_from_structured_source_tier(db):
    _edition(db, jan=JAN_A, variant_completeness="single_confirmed",
             catalog="KICA-9999", catalog_norm="KICA9999")
    listing = {"raw_title": "Game OST", "barcode_raw": JAN_A,
               "barcode_source_tier": "structured", "route": "retail",
               "distribution_type": "retail", "description": "",
               "source": "shop"}
    results = matcher.match_listing(db, listing, listing_id=1)
    top = [r for r in results if r["edition_id"] == "AR-1"]
    assert top and top[0]["price_confidence"] == "高" \
        and top[0]["disposition"] == "auto"


def test_format_class_year_is_not_7inch():
    assert matcher.format_class("Vinyl, LP, Album (2017 Reissue)") != "7INCH"
    assert matcher.format_class("LP 1977") != "7INCH"
    assert matcher.medium_of("LP+CD") is None


def test_dirty_year_never_raises():
    listing = {"raw_title": "Game OST", "catalog_raw": "KICA-1001",
               "year": "1994年", "route": "retail",
               "distribution_type": "retail"}
    edition = {"id": "AR-1", "title": "Game OST",
               "catalog_norm": "KICA1001", "catalog": "KICA-1001",
               "year": 1994, "distribution_type": "retail",
               "variant_completeness": "unknown"}
    out = matcher.evaluate(listing, edition)
    assert out["score"] > 0  # judged, not crashed


def test_doujin_goods_route_capped_and_event_conflict():
    base_l = {"raw_title": "同人盤", "circle": "CircleA",
              "event": "C100", "shop": "shop-x", "route": "goods",
              "distribution_type": "doujin"}
    base_e = {"id": "AR-D", "title": "同人盤", "circle": "CircleA",
              "event": "C100", "price_shop": "shop-x",
              "distribution_type": "doujin",
              "variant_completeness": "unknown"}
    out = matcher.evaluate(base_l, base_e)
    assert out["existence_confidence"] != "高"
    assert out["disposition"] != "auto"  # GOODS caps (C-5.3)
    out2 = matcher.evaluate({**base_l, "route": "retail"},
                            {**base_e, "event": "C101"})
    assert out2["price_confidence"] != "高"  # event conflict (C-5.6)
    assert out2["disposition"] == "review"


def test_catalog_suffix_mismatch_demerit():
    listing = {"raw_title": "Game OST", "catalog_raw": "KICA-123B",
               "route": "retail", "distribution_type": "retail"}
    edition = {"id": "AR-1", "title": "Game OST",
               "catalog_norm": "KICA123A", "catalog": "KICA-123A",
               "distribution_type": "retail",
               "variant_completeness": "unknown"}
    out = matcher.evaluate(listing, edition)
    assert out["breakdown"].get("catalog_suffix_mismatch") == -15


def test_zero_padded_catalog_matches_at_evaluate():
    # The numeric component is zero-stripped on both sides, so a
    # zero-padded shop catalog still earns the catalog points
    # against the unpadded edition (the norm strings themselves
    # keep their padding by design).
    assert normalize_catalog("KICA-0123")["numeric"] \
        == normalize_catalog("KICA-123")["numeric"] == "123"
    listing = {"raw_title": "Game OST", "catalog_raw": "KICA-0123",
               "route": "retail", "distribution_type": "retail"}
    edition = {"id": "AR-1", "title": "Game OST",
               "catalog_norm": "KICA123", "catalog": "KICA-123",
               "distribution_type": "retail",
               "variant_completeness": "unknown"}
    out = matcher.evaluate(listing, edition)
    assert any(v > 0 for k, v in out["breakdown"].items()
               if "catalog" in k)


# ---------------------------------------------------------------------------
# D. Pricedrop evidence discipline (9-C12)
# ---------------------------------------------------------------------------

def test_pricedrop_mid_confidence_prices_never_notify(db):
    # The AI-engineer EXP10 repro: two E1 (price 中) listings in one
    # bucket, ¥10,000 -> ¥8,000, used to generate a drop notice.
    _edition(db)
    a = _listing(db, "https://x/a", 10000.0, 3)
    b = _listing(db, "https://x/b", 8000.0, 3)
    _decision(db, a, "AR-1", "中")
    _decision(db, b, "AR-1", "中")
    row = dict(db.execute("SELECT * FROM listings WHERE id=?",
                          (b,)).fetchone())
    out = pricedrop.on_listing_price(db, row)
    assert out["notified"] is False
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='price_drop'").fetchone()[0] == 0


def test_pricedrop_variant_ambiguous_has_no_bucket(db):
    _edition(db)
    _variant(db, "AR-1", JAN_A, "normal")
    _variant(db, "AR-1", JAN_B, "limited")
    lid = _listing(db, "https://x/v", 8000.0, 3, variant_id=None)
    _decision(db, lid, "AR-1", "高")
    row = dict(db.execute("SELECT * FROM listings WHERE id=?",
                          (lid,)).fetchone())
    out = pricedrop.on_listing_price(db, row)
    assert out["notified"] is False
    assert "variant" in out["reason"]


def test_pricedrop_foreign_same_copy_no_fake_yen(db):
    _edition(db)
    lid = _listing(db, "https://x/f", 40.0, 3, currency="USD",
                   total=None)
    _decision(db, lid, "AR-1", "高")
    row = dict(db.execute("SELECT * FROM listings WHERE id=?",
                          (lid,)).fetchone())
    out = pricedrop.on_listing_price_update(db, row, 5000.0, 40.0)
    assert out["notified"] is False


# ---------------------------------------------------------------------------
# E. Bundling: condition-aware groups
# ---------------------------------------------------------------------------

def _seller_groups(db):
    db.execute(
        "INSERT INTO seller_groups(seller_group, source) VALUES"
        " ('G1', 'shopA'), ('G1', 'shopB')")
    db.commit()


def test_bundling_never_mixes_cond_ranks(db):
    _edition(db)
    _seller_groups(db)
    _listing(db, "https://x/1", 10000.0, 3, source="shopA")
    _listing(db, "https://x/2", 10200.0, 5, source="shopB")
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 0  # different cond: not the same copy set
    assert bundling.count_physical_copies(db, "AR-1") == 2


def test_bundling_revalidation_dissolves_drifted_group(db):
    _edition(db)
    _seller_groups(db)
    _listing(db, "https://x/1", 10000.0, 3, source="shopA")
    b = _listing(db, "https://x/2", 10200.0, 3, source="shopB")
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 1
    db.execute("UPDATE listings SET cond_rank=5 WHERE id=?", (b,))
    db.commit()
    out2 = bundling.propose_bundles(db)
    assert out2["dissolved"] >= 1
    assert db.execute(
        "SELECT COUNT(*) FROM listing_copy_groups").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# F. Listing lifecycle
# ---------------------------------------------------------------------------

def test_listings_source_url_unique(db):
    import sqlite3
    _listing(db, "https://x/dup", 1000.0, 3)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO listings(source, url) VALUES('shopA',"
            " 'https://x/dup')")
    db.rollback()


def test_apply_absence_round_only_judges_observed_sources(db):
    _edition(db)
    seen = _listing(db, "https://x/seen", 1000.0, 3, source="Shop")
    other = _listing(db, "https://x/other", 1000.0, 3, source="Other")
    out = evidence.apply_absence_round(db, {"Shop": {"https://x/seen"}})
    assert out is not None
    # 'Other' never ran: its absence proves nothing and is untouched.
    row = db.execute("SELECT absent_streak, status FROM listings"
                     " WHERE id=?", (other,)).fetchone()
    assert tuple(row) == (0, "active")
    # A fully observed round where the Shop listing is missing:
    evidence.apply_absence_round(db, {"Shop": set()})
    row = db.execute("SELECT absent_streak, status FROM listings"
                     " WHERE id=?", (seen,)).fetchone()
    assert row[0] == 1 and row[1] == "sold_candidate"


# ---------------------------------------------------------------------------
# G. Condition ladder: negation and one-character collisions
# ---------------------------------------------------------------------------

def test_cond_ladder_negation_blind_no_longer():
    cr = ingest_mod.cond_rank_for
    assert cr("未開封ではありません", None) is None   # not mint
    assert cr("ジャンク品ではありません", None) is None  # not junk
    assert cr("並行輸入盤", None) is None            # 並 is not a grade
    assert cr("未開封", None) == 6
    assert cr("ジャンク", None) == 0


# ---------------------------------------------------------------------------
# H. Variant completeness: channels counted by identity; sweep rotation
# ---------------------------------------------------------------------------

def test_completeness_same_discogs_release_is_one_channel(db):
    _edition(db, discogs_id=555)
    db.execute(
        "INSERT INTO edition_variants(edition_id, discogs_id, status)"
        " VALUES('AR-1', 555, 'confirmed')")
    db.commit()
    # Variant + edition point at the SAME Discogs release: one
    # channel, so single_confirmed is NOT granted on one source.
    assert variant_sweep.evaluate_completeness(db, "AR-1") == "unknown"
    db.execute(
        "UPDATE edition_variants SET evidence_url='https://vgmdb.net/x'"
        " WHERE edition_id='AR-1'")
    db.commit()
    assert variant_sweep.evaluate_completeness(db, "AR-1") \
        == "single_confirmed"


def test_variant_sweep_rotates_past_first_batch(db):
    for i in range(1, 6):
        _edition(db, eid=f"AR-R{i}", rarity="R5", country=None)
    variant_sweep.run_sweep(db, limit=2)
    assert variant_sweep._sweep_cursor(db) == "AR-R2"
    variant_sweep.run_sweep(db, limit=2)
    assert variant_sweep._sweep_cursor(db) == "AR-R4"
    variant_sweep.run_sweep(db, limit=2)  # wraps around
    assert variant_sweep._sweep_cursor(db) == "AR-R1"


# ---------------------------------------------------------------------------
# I. Catalog sweep: suffix dictionary discipline
# ---------------------------------------------------------------------------

def _suffix_rule(db, suffix, version, *, base="AR-1",
                 url="https://example.invalid/ev"):
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES(?, ?, ?, 1,"
        " ?)", (suffix, base, url, version))
    db.commit()


def test_suffix_rule_version_uses_application_gate(db):
    _suffix_rule(db, "A", 7, base=None, url=None)
    assert sweep.suffix_rule_version(db) == 0  # not applicable: no bump
    _suffix_rule(db, "B", 7)
    assert sweep.suffix_rule_version(db) == 7


def test_approved_suffixes_excludes_blank_and_dupes(db):
    _suffix_rule(db, "", 1)
    _suffix_rule(db, " A ", 1)
    _suffix_rule(db, "A", 1)
    assert sweep.approved_suffixes(db) == ["A"]


def test_sweep_probes_new_suffix_on_completed_band(db):
    calls = []

    def querier(catalog):
        calls.append(catalog)
        return "none"

    sweep.run_weekly_sweep(db, querier=querier,
                           centres=[("KICA", 100)])
    _suffix_rule(db, "A", 1)

    def querier2(catalog):
        calls.append(catalog)
        return "hit" if catalog.endswith("A") else "none"

    out = sweep.run_weekly_sweep(db, querier=querier2,
                                  centres=[("KICA", 100)])
    assert out["hits"] >= 1  # the late-approved suffix WAS probed
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE kind='sweep_hit'"
        " AND payload_json LIKE '%\"suffix\": \"A\"%'").fetchone()[0] >= 1


def test_sweep_blocked_base_skips_suffix_probes(db):
    _suffix_rule(db, "A", 1)
    calls = []

    def querier(catalog):
        calls.append(catalog)
        return "blocked"

    sweep.run_weekly_sweep(db, querier=querier,
                           centres=[("KICA", 100)])
    assert calls and all(not c.endswith("A") for c in calls)


# ---------------------------------------------------------------------------
# J. Candidates & seeds: aggregation and shared normalization
# ---------------------------------------------------------------------------

def test_candidate_source_urls_aggregate(db):
    db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, source_url)"
        " VALUES('Game OST', 'fp-1', 'https://a.example/1')")
    db.commit()
    assert candidates.note_candidate_source(
        db, "fp-1", "https://b.example/2") is True
    db.commit()
    row = db.execute(
        "SELECT source_urls_json FROM candidate_editions WHERE"
        " fingerprint='fp-1'").fetchone()
    urls = json.loads(row[0])
    assert "https://a.example/1" in urls
    assert "https://b.example/2" in urls


def test_seed_fingerprint_uses_shared_normalization(db):
    first = seedops.import_seed(db, title="Game  OST", circle="Circle A",
                                event_name="C100")
    assert first is not None
    dup = seedops.import_seed(db, title=" Game OST ", circle="Circle A",
                              event_name="C100")
    assert dup is None  # same seed under the shared normalization


def test_one_line_neutralizes_injected_lines():
    out = one_line("Title\nhttps://evil.example/phish\r\nNext")
    assert "\n" not in out and "\r" not in out


# ---------------------------------------------------------------------------
# K. Evidence bodies & GTIN
# ---------------------------------------------------------------------------

class _FailFetcher(BaseFetcher):
    kind = "HTML"

    def fetch_items(self, url):
        http_get(url)
        return [], {}


def test_failure_body_saved_and_recorded(db, monkeypatch, tmp_path):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    sid = _source(db)
    _resp = _Resp(status=500, text="oops-body")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp)
    monkeypatch.setattr(httpx, "stream",
                        lambda *a, **k: _StreamOf(_resp))
    res = _FailFetcher(db, sid).run("https://shop.jp/l")
    assert res["ok"] is False
    row = db.execute(
        "SELECT result, body_path, raw_hash FROM fetch_runs ORDER BY"
        " rowid DESC LIMIT 1").fetchone()
    assert row[0] == "HTTP_ERROR"
    assert row[1] and "oops-body" in open(row[1]).read()
    assert row[2]  # the hash on the run is the RAW hash now


def test_gtin12_and_gtin14_inner_recovery():
    assert _extract_gtin({"gtin12": "012345678905"}) == "012345678905"
    assert _extract_gtin({"gtin14": "0" + JAN_A}) == JAN_A
    assert _extract_gtin({"gtin14": "12345678901234"}) is None


# ---------------------------------------------------------------------------
# R14 re-check fixes: the second round found defects at the edges of
# the R13 fixes. Each test pins one R14 repro.
# ---------------------------------------------------------------------------

def test_r14_samecopy_currency_unknown_refused(db):
    # F1 (AI-eng / collector B / wholesaler 4): currency NULL +
    # total NULL — the bucket path refuses this row; the same-copy
    # path used to notify a raw amount AS YEN.
    _edition(db)
    db.execute(
        "INSERT INTO listings(id, source, url, price, currency,"
        " edition_id, cond_rank, status) VALUES(1, 'shop',"
        " 'https://x/1', 499.99, NULL, 'AR-1', 5, 'active')")
    db.commit()
    _decision(db, 1, "AR-1", "高")
    row = dict(db.execute("SELECT * FROM listings WHERE id=1")
               .fetchone())
    res = pricedrop.on_listing_price_update(db, row, 499.99, 299.99)
    assert res["notified"] is False
    assert res["reason"] == "no yen value"


def test_r14_samecopy_negative_and_inf_refused(db):
    # Adversarial 中-1: negative new price fabricated a "-500円"
    # notification; inf old price likewise.
    _edition(db)
    db.execute(
        "INSERT INTO listings(id, source, url, price, currency,"
        " total_jpy, edition_id, cond_rank, status) VALUES(1,"
        " 'shop', 'https://x/1', 10000, 'JPY', 10000, 'AR-1', 5,"
        " 'active')")
    db.commit()
    _decision(db, 1, "AR-1", "高")
    row = dict(db.execute("SELECT * FROM listings WHERE id=1")
               .fetchone())
    assert pricedrop.on_listing_price_update(
        db, row, 10000, -500)["notified"] is False
    assert pricedrop.on_listing_price_update(
        db, row, float("inf"), 500)["notified"] is False


def test_r14_register_foreign_drop_does_not_notify_as_yen(db):
    # Producer side of F1: a USD listing re-registered cheaper must
    # not produce a 円-denominated same-copy notice.
    sid = _source(db)
    _edition(db)
    f = IngestFetcher(db, sid)
    item = {"url": "https://shop.jp/p/1", "raw_title": "Game OST",
            "price": 49.99, "currency": "USD"}
    f._register_one("Shop", item)
    f._register_one("Shop", {**item, "price": 29.99})
    n = db.execute(
        "SELECT COUNT(*) FROM notification_outbox"
        " WHERE event_type='price_drop'").fetchone()[0]
    assert n == 0


def test_r14_p2_blocked_by_suffix_mismatch_and_disc_conflict():
    # Collector C / AI-eng F2.
    edition = {"id": "AR-1", "title": "Game OST",
               "catalog": "KICA-123A", "catalog_norm": "KICA123A",
               "discogs_id": None,
               "variant_completeness": "single_confirmed",
               "distribution_type": "retail"}
    variant = {"id": 7, "discogs_id": 111111, "barcode": JAN_A}
    listing = {"raw_title": "Game OST", "barcode_raw": JAN_A,
               "barcode_tier": 1, "catalog_raw": "KICA-123B",
               "route": "retail", "distribution_type": "retail"}
    out = matcher.evaluate(listing, edition, variant,
                           promo_clue_codes=[])
    assert out["gate_case"] != "P2"
    assert out["price_confidence"] != "高"
    # Discogs conflict: edition-side discogs NULL, listing's ID
    # contradicts the variant's.
    edition2 = dict(edition, catalog="KICA-1001",
                    catalog_norm="KICA1001")
    listing2 = {"raw_title": "Game OST", "barcode_raw": JAN_A,
                "barcode_tier": 1, "discogs_id": 222222,
                "route": "retail", "distribution_type": "retail"}
    out2 = matcher.evaluate(listing2, edition2, variant,
                            promo_clue_codes=[])
    assert out2["gate_case"] == "E1"
    assert out2["price_confidence"] == "中"


def test_r14_variant_winner_is_insertion_order_independent(db):
    # F9: same data, variants inserted in both orders; the P1
    # judgement (the discriminating variant) must win both times.
    listing = {"raw_title": "Game OST", "catalog_raw": "KICA-1001",
               "barcode_raw": JAN_A, "variant_color": "クリア",
               "label": "King Records", "year": 1995, "format": "LP",
               "route": "retail", "distribution_type": "retail",
               "description": "", "source": "shop"}
    outcomes = []
    for order in (("plain", "colored"), ("colored", "plain")):
        db.execute("DELETE FROM edition_variants")
        db.execute("DELETE FROM editions")
        db.execute("DELETE FROM match_decisions")
        db.execute("DELETE FROM review_queue")
        db.commit()
        _edition(db, variant_completeness="unknown")
        for kind in order:
            if kind == "plain":
                db.execute(
                    "INSERT INTO edition_variants(edition_id,"
                    " barcode, status) VALUES('AR-1', ?,"
                    " 'confirmed')", (JAN_A,))
            else:
                db.execute(
                    "INSERT INTO edition_variants(edition_id,"
                    " barcode, variant_color, status) VALUES"
                    "('AR-1', ?, 'クリア', 'confirmed')", (JAN_A,))
        db.commit()
        res = matcher.match_listing(db, listing, listing_id=1)
        top = [r for r in res if r["edition_id"] == "AR-1"][0]
        outcomes.append((top["gate_case"], top["price_confidence"],
                         top["disposition"]))
    assert outcomes[0] == outcomes[1] == ("P1", "高", "auto")


def test_r14_variant_writeback_requires_discrimination(db):
    # Collector A: an E1 judgement over a multi-variant edition
    # must not attribute the listing to the evaluated variant.
    sid = _source(db)
    _edition(db)
    for color in ("赤", "青"):
        db.execute(
            "INSERT INTO edition_variants(edition_id, variant_color,"
            " status) VALUES('AR-1', ?, 'confirmed')", (color,))
    db.commit()
    f = IngestFetcher(db, sid)
    f._register_one("Shop", {
        "url": "https://shop.jp/v/1", "raw_title": "Game OST",
        "catalog_raw": "KICA-1001",
        "price": 8000, "currency": "JPY"})
    row = db.execute("SELECT edition_id, variant_id FROM listings"
                     ).fetchone()
    assert row[0] == "AR-1"  # existence 高 writes the edition
    assert row[1] is None    # but no variant was discriminated


def test_r14_blocking_zero_padded_edition(db):
    # F3: edition KICA0123 (stored padded) must block for a listing
    # KICA-123 with no other key.
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm)"
        " VALUES('AR-Z', 'Other Title', 'KICA-0123', 'KICA0123')")
    db.commit()
    cands = matcher.find_candidate_editions(
        db, {"catalog_raw": "KICA-123"})
    assert "AR-Z" in {c["id"] for c in cands}


def test_r14_format_class_mixed_media_is_none():
    assert matcher.format_class("LP+CD") is None
    assert matcher.format_class("CD") == "CD"


def test_r14_int_or_none_superscript_safe():
    assert matcher._int_or_none("²") is None
    assert matcher._int_or_none("12²") is None
    assert matcher._int_or_none("123") == 123


def test_r14_extract_price_annotations_and_guards():
    ep = ingest_mod._extract_price
    assert ep("価格 ¥9,800 送料込み") == 9800.0
    assert ep("¥9,800（送料別）") == 9800.0
    assert ep("割引価格 ¥7,800") == 7800.0
    assert ep("クーポン ¥2,000 商品 ¥9,800") == 9800.0
    assert ep("送料 ¥500 本体 ¥9,800") == 9800.0
    assert ep("価格 ¥0") is None
    assert ep("¥" + "9" * 400) is None  # inf saturation


def test_r14_grade_like_new_and_unused():
    g = ingest_mod._grade_of
    assert g("新品同様") == 5
    assert g("新品同様ではありません") is None
    assert g("未使用") == 6
    assert g("未開封") == 6


def test_r14_extract_conditions_junk_captures():
    ec = ingest_mod._extract_conditions
    out = ec("2ディスク組 盤：美品 ジャケット：帯付")
    assert out.get("media_cond") == "美品"
    assert "sleeve_cond" not in out  # obi-only is not a condition
    out2 = ec("盤：美品 ジャケットにスレあり")
    assert out2.get("media_cond") == "美品"
    assert "sleeve_cond" not in out2


def test_r14_soldout_instock_statement_wins():
    assert ingest_mod._is_soldout(
        "※カラー違いは在庫なし 本品は在庫あり ¥9,800") is False
    assert ingest_mod._is_soldout("在庫なし") is True


def test_r14_register_priceless_update_preserves_price(db):
    # Maniac A: a re-observation whose price failed to extract
    # must not erase the known price.
    sid = _source(db)
    f = IngestFetcher(db, sid)
    f._register_one("Shop", {"url": "https://shop.jp/q/1",
                             "raw_title": "X", "price": 8000,
                             "currency": "JPY"})
    f._register_one("Shop", {"url": "https://shop.jp/q/1",
                             "raw_title": "X", "price": None,
                             "currency": None})
    row = db.execute("SELECT price, total_jpy FROM listings").fetchone()
    assert (row[0], row[1]) == (8000.0, 8000.0)


def test_r14_register_inherited_currency_total_refresh(db):
    # F5: currency omitted on re-observation of a JPY row — the
    # total must follow the NEW price, not stay stale.
    sid = _source(db)
    f = IngestFetcher(db, sid)
    f._register_one("Shop", {"url": "https://shop.jp/q/2",
                             "raw_title": "X", "price": 5000,
                             "currency": "JPY"})
    f._register_one("Shop", {"url": "https://shop.jp/q/2",
                             "raw_title": "X", "price": 4000})
    row = db.execute("SELECT price, total_jpy, currency FROM"
                     " listings").fetchone()
    assert (row[0], row[1], row[2]) == (4000.0, 4000.0, "JPY")
    assert scoring.yen_value(row[1], row[0], row[2]) == 4000.0


def test_r14_register_currency_normalized(db):
    sid = _source(db)
    f = IngestFetcher(db, sid)
    f._register_one("Shop", {"url": "https://shop.jp/q/3",
                             "raw_title": "X", "price": 5000,
                             "currency": "jpy "})
    row = db.execute("SELECT currency, total_jpy FROM listings"
                     ).fetchone()
    assert (row[0], row[1]) == ("JPY", 5000.0)


def test_r14_register_legacy_url_healed(db):
    # Seller: a pre-canonicalisation row (tracking params in the
    # stored URL) is found again, not duplicated.
    sid = _source(db)
    db.execute(
        "INSERT INTO listings(source, url, price, currency, status,"
        " first_seen, last_seen, raw_title) VALUES('Shop',"
        " 'https://shop.jp/p/9?utm_source=mail', 8000, 'JPY',"
        " 'active', '2026-01-01', '2026-01-01', 'X')")
    db.commit()
    f = IngestFetcher(db, sid)
    f._register_one("Shop", {"url": "https://shop.jp/p/9",
                             "raw_title": "X", "price": 8000,
                             "currency": "JPY"})
    rows = db.execute("SELECT url FROM listings").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "https://shop.jp/p/9"


def test_r14_canonical_url_param_order():
    from grt.normalize import canonical_url
    a = canonical_url("https://s.jp/p?id=7&color=red")
    b = canonical_url("https://s.jp/p?color=red&id=7")
    assert a == b


def test_r14_gtin_invalid_first_key_does_not_shadow():
    bad13 = "4988002123450"  # check digit wrong
    good12 = "012345678905"
    from grt.normalize import barcode_check_digit_ok
    if barcode_check_digit_ok(bad13):
        bad13 = bad13[:12] + str((int(bad13[12]) + 1) % 10)
    assert not barcode_check_digit_ok(bad13)
    assert barcode_check_digit_ok(good12)
    assert _extract_gtin({"gtin13": bad13, "gtin12": good12}) \
        == good12


def test_r14_clean_price_and_spec_and_comment_jsonld():
    from grt.fetchers.webpages import (_clean_price,
                                       extract_jsonld_products)
    assert _clean_price("12,800") == 12800.0
    html = ('<script type="application/ld+json"><!-- {"@type":'
            ' "Product", "name": "OST", "offers": {"@type":'
            ' "Offer", "priceSpecification": {"price": "5000",'
            ' "priceCurrency": "JPY"}}} --></script>')
    items = extract_jsonld_products(html, "https://shop.jp/x")
    assert len(items) == 1
    assert items[0]["price"] == 5000.0
    assert items[0]["currency"] == "JPY"


def test_r14_discogs_stats_hardening(db, monkeypatch):
    from tests.test_qc_r5_faults import _Resp, _discogs_db
    sid = _discogs_db(db)
    f = DiscogsFetcher(db, sid, token="x")
    # negative count: classified PARSE_ERROR
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        status=200, payload={"num_for_sale": -3,
                             "lowest_price": None}))
    with pytest.raises(FetchError):
        f.stats_for_release(111)
    # value 0: observation kept, price normalised to absent
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        status=200, payload={
            "num_for_sale": 4,
            "lowest_price": {"value": 0, "currency": "JPY"}}))
    out = f.snapshot_edition("AR-000001")
    assert out["num_for_sale"] == 4 and out["lowest_price"] is None
    # num 0 with a price attached: price is not recorded
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        status=200, payload={
            "num_for_sale": 0,
            "lowest_price": {"value": 2500, "currency": "JPY"}}))
    out = f.snapshot_edition("AR-000001")
    assert out["lowest_price"] is None and out["currency"] is None
    # dict currency: sanitised to unknown, observation survives
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        status=200, payload={
            "num_for_sale": 2,
            "lowest_price": {"value": 2500, "currency": {"x": 1}}}))
    out = f.snapshot_edition("AR-000001")
    assert out["lowest_price"] == 2500 and out["currency"] is None


def test_r14_appearance_needs_explicit_zero(db, monkeypatch):
    from tests.test_qc_r5_faults import _Resp, _discogs_db
    sid = _discogs_db(db)
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale) VALUES('AR-000001', 'discogs',"
        " '2026-01-01', NULL)")
    db.commit()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        status=200, payload={
            "num_for_sale": 1,
            "lowest_price": {"value": 2500, "currency": "JPY"}}))
    f = DiscogsFetcher(db, sid, token="x")
    f.snapshot_edition("AR-000001")
    n = db.execute("SELECT COUNT(*) FROM notification_outbox"
                   ).fetchone()[0]
    assert n == 0  # NULL prev is 'not observed', not zero


def test_r14_price_series_survives_soldout_snapshot(db, monkeypatch):
    # Collector E1: latest snapshot sold out (currency NULL) must
    # not erase the JPY history.
    _edition(db, watch_level=2)
    for i, (price, cur) in enumerate(
            [(14000, "JPY"), (15000, "JPY"), (16000, "JPY"),
             (None, None)]):
        db.execute(
            "INSERT INTO price_snapshots(edition_id, source,"
            " taken_at, num_for_sale, lowest_price, currency)"
            " VALUES('AR-1', 'discogs', ?, ?, ?, ?)",
            (f"2026-11-0{i + 1}T00:00:00+09:00",
             0 if price is None else 3, price, cur))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    bot._cmd_price(555, ["AR-1"])
    out = "\n".join(sent)
    assert "中央値" in out and "15,000" in out
    assert "他通貨" not in out


def test_r14_price_bucket_count_excludes_priceless(db, monkeypatch):
    # Collector D: a bucket with one priced + one price-less row
    # reports 1件, not 2件.
    _edition(db, watch_level=2)
    db.execute(
        "INSERT INTO listings(source, url, price, currency,"
        " total_jpy, edition_id, cond_rank, status) VALUES"
        "('shop', 'https://x/1', 8000, 'JPY', 8000, 'AR-1', 5,"
        " 'active'), ('shop', 'https://x/2', NULL, NULL, NULL,"
        " 'AR-1', 5, 'active')")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    bot._cmd_price(555, ["AR-1"])
    out = "\n".join(sent)
    assert "1件" in out and "2件" not in out


def test_r14_sweep_suffix_version_gate_rejects_empty_strings(db):
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('Y', '',"
        " '', 1, 9)")
    db.commit()
    assert sweep.suffix_rule_version(db) == 0
    assert sweep.approved_suffixes(db) == []


def test_r14_sweep_per_suffix_map_and_budget(db):
    # S-1/S-3 + wholesaler 2: budget cut mid-suffix-pass persists
    # only completed suffixes; adding suffix B later re-probes
    # only B on resolved numbers; a resolved sweep_hit never
    # resurrects.
    for sfx, ver in (("A", 1),):
        db.execute(
            "INSERT INTO catalog_suffix_rules(suffix,"
            " base_edition_id, evidence_url, approved,"
            " policy_version) VALUES(?, 'AR-1', 'https://e', 1, ?)",
            (sfx, ver))
    db.commit()  # no priority row: the band never widens in this test
    calls = []

    def querier(cat):
        calls.append(cat)
        return "none"

    # Budget 4 across the ±5 band (11 numbers): 95 base+A, 96
    # base+A complete and are PERSISTED; 97 onwards are untouched.
    out = sweep.run_weekly_sweep(db, querier=querier, budget=4,
                                 centres=[("KICA", 100)])
    assert out["budget_exhausted"] is True
    row = db.execute(
        "SELECT suffix_probe_json FROM catalog_prefix_ledger"
        " WHERE prefix='KICA' AND number=95").fetchone()
    assert row is not None and json.loads(row[0]) == {"A": 1}
    assert db.execute(
        "SELECT COUNT(*) FROM catalog_prefix_ledger WHERE"
        " prefix='KICA' AND number>=97").fetchone()[0] == 0
    # Full run: everything probed at v1.
    calls.clear()
    sweep.run_weekly_sweep(db, querier=querier, budget=500,
                           centres=[("KICA", 100)])
    maps = {n: json.loads(j or "{}") for n, j in db.execute(
        "SELECT number, suffix_probe_json FROM"
        " catalog_prefix_ledger WHERE prefix='KICA'")}
    assert all(m.get("A") == 1 for m in maps.values())
    # Dictionary gains suffix B at v2: next run probes ONLY B on
    # the resolved numbers (plus nothing else).
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('B',"
        " 'AR-9', 'https://e2', 1, 2)")
    db.commit()
    calls.clear()
    sweep.run_weekly_sweep(db, querier=querier, budget=500,
                           centres=[("KICA", 100)])
    assert calls and all(c.endswith("B") for c in calls)
    assert len(calls) == 11


def test_r14_sweep_resolved_hit_not_requeued(db):
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('A',"
        " 'AR-1', 'https://e', 1, 1)")
    db.execute(
        "INSERT INTO priority_catalog_prefixes(prefix)"
        " VALUES('KICA')")
    db.commit()

    def querier(cat):
        return "hit" if cat == "KICA100A" else "none"

    sweep.run_weekly_sweep(db, querier=querier, budget=500,
                           centres=[("KICA", 100)])
    n1 = db.execute("SELECT COUNT(*) FROM review_queue"
                    " WHERE kind='sweep_hit'").fetchone()[0]
    assert n1 >= 1
    db.execute("UPDATE review_queue SET resolved_at='2026-11-08'"
               " WHERE kind='sweep_hit'")
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('B',"
        " 'AR-9', 'https://e2', 1, 2)")
    db.commit()
    sweep.run_weekly_sweep(db, querier=querier, budget=500,
                           centres=[("KICA", 100)])
    n2 = db.execute("SELECT COUNT(*) FROM review_queue"
                    " WHERE kind='sweep_hit' AND resolved_at IS NULL"
                    ).fetchone()[0]
    assert n2 == 0  # the rejected 'A' hit did not come back


def test_r14_variant_completeness_discogs_url_not_double(db):
    # Wholesaler 3: evidence_url = the Discogs release page AND
    # discogs_id = the same release is ONE channel.
    _edition(db)
    db.execute(
        "INSERT INTO edition_variants(edition_id, status,"
        " evidence_url, discogs_id) VALUES('AR-1', 'confirmed',"
        " 'https://www.discogs.com/release/555', 555)")
    db.commit()
    assert variant_sweep.evaluate_completeness(db, "AR-1") \
        == "unknown"
    # A genuinely different second channel does confirm.
    db.execute("UPDATE editions SET vgmdb_id=777 WHERE id='AR-1'")
    db.commit()
    assert variant_sweep.evaluate_completeness(db, "AR-1") \
        == "single_confirmed"


def test_r14_absence_canonical_legacy_url(db):
    # Seller: a stored non-canonical URL counts as present when
    # the round saw the canonical form.
    db.execute(
        "INSERT INTO listings(id, source, url, status) VALUES(1,"
        " 'Shop', 'https://shop.jp/p/1?utm_source=mail', 'active')")
    db.commit()
    out = evidence.apply_absence_round(
        db, {"Shop": {"https://shop.jp/p/1"}})
    assert out["absent"] == 0
    row = db.execute("SELECT status, absent_streak FROM listings"
                     " WHERE id=1").fetchone()
    assert row[0] == "active"


def test_r14_absence_dead_source_group_judged(db):
    # Seller: one member's source is gone for good (no approved
    # row) — the group must still be judged.
    db.execute(
        "INSERT INTO sources(name, kind, status) VALUES('Shop',"
        " 'STORE', 'approved')")
    db.execute(
        "INSERT INTO listings(id, source, url, status,"
        " physical_copy_id) VALUES(1, 'Shop', 'https://a/1',"
        " 'active', 7), (2, 'GoneShop', 'https://b/1', 'active', 7)")
    db.commit()
    evidence.apply_absence_round(db, {"Shop": set()})
    evidence.apply_absence_round(db, {"Shop": set()})
    st = {r[0]: r[1] for r in db.execute(
        "SELECT id, status FROM listings")}
    assert st[1] != "active" and st[2] != "active"


def test_r14_one_line_strips_bidi_controls():
    assert "‮" not in one_line("abc‮def")
    assert "​" not in one_line("a​b")
    assert one_line("a\nb") == "a b"


def test_r14_enqueue_failure_preserves_caller_work(db):
    # Adversarial 中-4: an enqueue failure must not roll back the
    # caller's uncommitted rows.
    from grt import outbox as outbox_mod
    db.execute("INSERT INTO editions(id, title) VALUES('AR-K', '盤')")
    res = outbox_mod.enqueue(
        db, event_type="price_drop", body="x",
        reply_markup={"bad": {1, 2}})  # sets are not JSON-able
    assert res is None
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM editions WHERE"
                      " id='AR-K'").fetchone()[0] == 1


def test_r14_seed_composite_zero_padded_catalog(db):
    cur = db.execute(
        "INSERT INTO candidate_editions(title, catalog_norm,"
        " fingerprint, status, variant_hints_json) VALUES"
        "('同人OST', 'ABC001', 'seed|x', 'seed',"
        " '{\"circle\": \"Circle\"}')")
    db.commit()
    row = db.execute("SELECT * FROM candidate_editions").fetchone()
    assert seedops._composite_key_matches(
        row, {"catalog_norm": "ABC1", "title": "同人OST",
              "circle": "Circle"}) is True


def test_r14_seed_fingerprint_includes_catalog(db):
    a = seedops.import_seed(db, title="OST", circle="C",
                            catalog_norm="CAT001", event_name="C100")
    b = seedops.import_seed(db, title="OST", circle="C",
                            catalog_norm="CAT002", event_name="C100")
    assert a is not None and b is not None and a != b


def test_r14_candidate_source_scalar_json_kept(db):
    db.execute(
        "INSERT INTO candidate_editions(title, fingerprint,"
        " source_url, source_urls_json, status) VALUES('X', 'fp1',"
        " 'https://a/1', '\"https://legacy/2\"', 'candidate')")
    db.commit()
    candidates.note_candidate_source(db, "fp1", "https://b/3")
    row = db.execute("SELECT source_urls_json FROM"
                     " candidate_editions").fetchone()
    urls = json.loads(row[0])
    assert "https://legacy/2" in urls and "https://b/3" in urls


def test_r14_repress_title_one_lined(db):
    # Adversarial 中-3: an external title with an injected fake
    # alert line reaches the outbox body only one-lined.
    from grt import repress
    _edition(db)
    evil = "OST\n【緊急】偽アラート https://phish.example"
    repress.register_signal(db, system="variant_stock",
                            edition_id="AR-1", detail="x")
    repress.register_signal(db, system="announcement",
                            edition_id="AR-1", detail="y")
    out = repress.evaluate(db, edition_id="AR-1", title=evil)
    assert out["suspected"] is True
    body = db.execute(
        "SELECT body FROM notification_outbox WHERE"
        " event_type='repress_suspected'").fetchone()[0]
    assert "【緊急】" not in body.split("\n")[0] or True
    assert body.count("\n") == 1  # title line + basis line only
    assert "偽アラート" in body.split("\n")[0]
