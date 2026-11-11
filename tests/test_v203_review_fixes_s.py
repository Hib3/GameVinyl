"""Regression tests for the multi-perspective review of Phase 2/3
(2026-11-08, used-seller persona — bundling / ingest / storewatch /
pricedrop wiring)."""
from grt import bundling, pricedrop, storewatch
from grt.fetchers import ingest as ingest_mod
from grt.fetchers.ingest import IngestFetcher


def _listing(db, url, price, cond, source="shopA", edition="AR-1"):
    cur = db.execute(
        "INSERT INTO listings(edition_id, variant_id, source, url,"
        " price, total_jpy, cond_rank, status, first_seen, last_seen)"
        " VALUES(?, NULL, ?, ?, ?, ?, ?, 'active', '2026-11-08',"
        " '2026-11-08')", (edition, source, url, price, price, cond))
    db.commit()
    return int(cur.lastrowid)


def _group(db):
    db.execute(
        "INSERT INTO seller_groups(seller_group, source) VALUES"
        " ('G1', 'shopA'), ('G1', 'shopB'), ('G1', 'shopC')")
    db.commit()


# S-D3a: two simultaneous listings at ONE source are two copies.
def test_bundling_same_source_never_grouped(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    _listing(db, "https://x/1", 10000, 3)
    _listing(db, "https://x/2", 10500, 3)
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 0
    assert bundling.count_physical_copies(db, "AR-1") == 2


def test_suspended_ebay_rows_are_not_bundled_or_counted(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    _group(db)
    _listing(db, "https://x/a", 10000, 3, source="shopA")
    _listing(db, "https://x/b", 10200, 3, source="shopB")
    _listing(db, "https://ebay.example/i", 10000, 3,
             source="eBay Browse used")
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 1
    assert bundling.count_physical_copies(db, "AR-1") == 1
    members = db.execute(
        "SELECT l.source FROM listing_copy_members m JOIN listings l"
        " ON l.id=m.listing_id ORDER BY l.source").fetchall()
    assert [r[0] for r in members] == ["shopA", "shopB"]


# S-D4: a later relisting from a new source JOINs the group.
def test_bundling_late_member_joins(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    _group(db)
    _listing(db, "https://x/1", 10000, 3, source="shopA")
    _listing(db, "https://x/2", 10200, 3, source="shopB")
    out = bundling.propose_bundles(db)
    assert out["grouped"] == 1
    _listing(db, "https://x/3", 10100, 3, source="shopC")
    out = bundling.propose_bundles(db)
    assert out["joined"] == 1
    assert bundling.count_physical_copies(db, "AR-1") == 1


# S-D5: the NULL-cond queue entry is not duplicated by daily runs.
def test_bundling_queue_deduped(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    _listing(db, "https://x/1", 10000, None)
    _listing(db, "https://x/2", 10000, 3)
    for _ in range(3):
        bundling.propose_bundles(db)
    rows = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='copy_bundle_review'").fetchone()[0]
    assert rows == 1


# S-D6: container-scoped extraction — no banner prices, no nav items.
def test_ingest_container_extraction(db, monkeypatch):
    html = """<html><body>
    <header>¥5,000以上で送料無料 <a href="/cart">カートを見る</a></header>
    <ul>
      <li><a href="/p/1">LP A</a><span>¥9,800</span></li>
      <li><a href="/p/2">LP B</a><span>¥3,200</span></li>
    </ul></body></html>"""

    class _Resp:
        text = html
        status_code = 200

    monkeypatch.setattr(ingest_mod, "http_get", None, raising=False)
    import grt.fetchers.base as base
    monkeypatch.setattr(base, "http_get", lambda url: _Resp())
    sid = _mk_source(db)
    items, _ = IngestFetcher(db, sid).fetch_items("https://shop.jp/l")
    by_title = {i["title"]: i["price"] for i in items}
    assert by_title == {"LP A": 9800.0, "LP B": 3200.0}


def _mk_source(db):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES('Shop',"
        " 'STORE', 'approved', 'https://shop.jp/l')")
    db.commit()
    return int(cur.lastrowid)


# S-D1/D2/D7/D8: ingest writes the match back, revives relistings,
# and a single copy's own price drop notifies.
def test_ingest_writeback_revive_and_drop(db, monkeypatch):
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm, label,"
        " year, format, country, distribution_type, watch_level)"
        " VALUES('AR-1', 'Game OST LP', 'KICA-1001', 'KICA1001',"
        " 'King Records', 1995, 'LP', 'JP', 'retail', 0)")
    db.commit()
    sid = _mk_source(db)
    f = IngestFetcher(db, sid)
    item = {"title": "Game OST LP", "raw_title": "Game OST LP",
            "url": "https://shop.jp/p/1", "price": 10000.0,
            "currency": "JPY", "catalog_raw": "KICA-1001",
            "label": "King Records", "year": 1995, "format": "LP",
            "country": "JP", "distribution_type": "retail",
            "route": "retail", "description": "",
            "media_cond": "美品", "sleeve_cond": "美品"}
    out = f.register_ingested("Shop", [item])
    assert out["created"] == 1
    row = db.execute("SELECT edition_id FROM listings").fetchone()
    assert row[0] == "AR-1"  # match written back (was always NULL)
    # W-check B3 contract change: this pair judges price confidence
    # 中 (no variant discrimination, not single_confirmed), and the
    # 9-C12 gate now bars 中 prices from drop evidence — a -10%
    # same-copy fall while still 中 notifies nothing.
    f.register_ingested("Shop", [{**item, "price": 9000.0}])
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='price_drop'").fetchone()[0] == 0
    # The copy ends (two absences), then returns cheaper — still
    # judged 中 on re-match, so the revival itself notifies nothing.
    db.execute("UPDATE listings SET price=10000.0, total_jpy=10000.0,"
               " status='ended', absent_streak=2")
    db.commit()
    cheaper = {**item, "price": 5000.0}
    f.register_ingested("Shop", [cheaper])
    row = db.execute(
        "SELECT status, absent_streak, total_jpy FROM listings"
    ).fetchone()
    assert row[0] == "active" and row[1] == 0 and row[2] == 5000.0
    assert db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='price_drop'").fetchone()[0] == 0
    # A review approval records a superseding price-高 decision
    # (the latest decision governs the 9-C12 gate); the very same
    # price history now qualifies as drop evidence.
    lid = db.execute("SELECT id FROM listings").fetchone()[0]
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id,"
        " existence_confidence, price_confidence)"
        " VALUES(?, 'AR-1', '高', '高')", (lid,))
    db.commit()
    listing = dict(db.execute(
        "SELECT * FROM listings WHERE id=?", (lid,)).fetchone())
    out = pricedrop.on_listing_price_update(db, listing, 10000.0, 5000.0)
    assert out["notified"] is True
    drops = db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE"
        " event_type='price_drop'").fetchone()[0]
    assert drops >= 1  # single-copy drop detected via its own history


# S-D9: a date-only change does not fire the store watch.
def test_storewatch_ignores_date_rollover(db):
    cur = db.execute(
        "INSERT INTO sources(name, url, kind, status) VALUES('Label',"
        " 'https://label.jp/shop', 'STORE', 'approved')")
    db.commit()
    sid = int(cur.lastrowid)

    class _R:
        def __init__(self, t):
            self.text = t
            self.status_code = 200

    class _C:
        pages = {}

        def get(self, url):
            return _R(self.pages[url])

    c = _C()
    c.pages["https://label.jp/shop"] = (
        "<html><body><p>新譜 LP ¥5,400</p>"
        "<p>2026年11月8日 更新</p></body></html>")
    out = storewatch.check_source(db, sid, client=c)
    assert out["result"] == "BASELINE"
    c.pages["https://label.jp/shop"] = (
        "<html><body><p>新譜 LP ¥5,400</p>"
        "<p>2026年11月9日 更新</p></body></html>")
    out = storewatch.check_source(db, sid, client=c)
    assert out["changed"] is False  # date rollover is noise
    c.pages["https://label.jp/shop"] = (
        "<html><body><p>新譜 LP ¥4,900</p>"
        "<p>2026年11月9日 更新</p></body></html>")
    out = storewatch.check_source(db, sid, client=c)
    assert out["changed"] is True  # a price change is signal


def test_pricedrop_same_copy_update(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    lid = _listing(db, "https://x/1", 10000, 3)
    # W-check B3 fixture update: the same-copy path enforces the
    # 9-C12 evidence gate — the listing needs a price-高 decision.
    db.execute(
        "INSERT INTO match_decisions(listing_id, edition_id,"
        " existence_confidence, price_confidence)"
        " VALUES(?, 'AR-1', '高', '高')", (lid,))
    db.commit()
    row = dict(db.execute("SELECT * FROM listings WHERE id=?",
                          (lid,)).fetchone())
    out = pricedrop.on_listing_price_update(db, row, 10000.0, 9500.0)
    assert out["notified"] is False  # 5% is below the policy floor
    out = pricedrop.on_listing_price_update(db, row, 10000.0, 5000.0)
    assert out["notified"] is True
