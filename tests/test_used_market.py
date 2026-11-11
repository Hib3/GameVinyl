import json
from grt import clock

from grt.used_market import run_market_api_sweep, run_yahoo_used_sweep


class FakeFetcher:
    queries = []
    items = []

    def __init__(self, db, source_id, app_id=None):
        self.db = db

    def run(self, query):
        self.queries.append(query)
        return {"ok": True, "result": "OK", "items": self.items}


class FakeApiFetcher:
    queries = []
    response = {"ok": True, "result": "OK", "items": []}

    def __init__(self, db, source_id, **credentials):
        self.db = db

    def run(self, query):
        self.queries.append(query)
        return self.response


def _edition(db, eid, title, catalog, year):
    db.execute(
        "INSERT INTO editions(id, title, catalog, catalog_norm, year)"
        " VALUES(?, ?, ?, ?, ?)",
        (eid, title, catalog, catalog.replace("-", ""), year))
    db.commit()


def test_used_market_is_disabled_without_app_id(db):
    def unexpected_fetcher(*args, **kwargs):
        raise AssertionError("disabled sweep must not create a fetcher")

    result = run_yahoo_used_sweep(
        db, app_id=None, fetcher_factory=unexpected_fetcher)

    assert result == {"status": "disabled", "checked": 0,
                      "listings": 0, "errors": 0}
    assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM used_market_search_state").fetchone()[0] == 0


def test_rakuten_market_api_lane_rotates_and_requires_human_review(
        db, monkeypatch, tmp_path):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    for key in ("EBAY_OAUTH_TOKEN", "RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RAKUTEN_APP_ID", "test-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-key")
    for i in range(3):
        _edition(db, f"API-{i}", f"Record {i}", f"CAT-{i}", 1980+i)
    FakeApiFetcher.queries = []
    FakeApiFetcher.response = {"ok": True, "result": "OK", "items": [{
        "title": "Record 0", "raw_title": "Record 0 CAT-0",
        "url": "https://item.rakuten.co.jp/shop/item/",
        "image_url": "https://thumbnail.image.rakuten.co.jp/@0_mall/shop/a.jpg",
        "price": 12.5, "currency": "JPY",
    }]}
    seen = set()
    for _ in range(3):
        result = run_market_api_sweep(
            db, "rakuten", batch_size=1, fetcher_factory=FakeApiFetcher)
        assert result["checked"] == 1
        seen.update(FakeApiFetcher.queries)
    assert seen == {"CAT-0", "CAT-1", "CAT-2"}
    row = db.execute(
        "SELECT id,edition_id,price,currency,total_jpy,image_url FROM listings"
        " WHERE source='Rakuten Ichiba'").fetchone()
    assert row is not None and row["edition_id"] is None
    assert row["price"] == 12.5
    assert row["total_jpy"] == 12.5
    assert row["image_url"] == "https://thumbnail.image.rakuten.co.jp/@0_mall/shop/a.jpg"
    assert db.execute(
        "SELECT COUNT(*) FROM market_api_search_state WHERE provider=?",
        ("rakuten",)).fetchone()[0] == 3
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE kind='match_review'"
        " AND resolved_at IS NULL").fetchone()[0] > 0


def test_ebay_market_api_lane_is_disabled_before_credentials_or_state(
        db, monkeypatch):
    from grt.fetchers import ebay

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled eBay lane must not inspect credentials or fetch")

    monkeypatch.setattr(ebay, "ebay_oauth_token", forbidden)
    result = run_market_api_sweep(
        db, "ebay", fetcher_factory=forbidden)

    assert result == {"status": "disabled", "checked": 0,
                      "listings": 0, "errors": 0}
    assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM market_api_search_state").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_market_api_missing_credentials_does_not_create_or_advance_queue(
        db, monkeypatch, tmp_path):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    for key in ("EBAY_OAUTH_TOKEN", "RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled market API lane must not run")
    for provider in ("ebay", "rakuten"):
        result = run_market_api_sweep(
            db, provider, fetcher_factory=forbidden)
        assert result["status"] == "disabled"
    assert db.execute(
        "SELECT COUNT(*) FROM market_api_search_state").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_market_api_two_full_rotations_cover_all_3777_editions(
        db, monkeypatch, tmp_path):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    for key in ("EBAY_OAUTH_TOKEN", "RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RAKUTEN_APP_ID", "test-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-key")
    db.executemany(
        "INSERT INTO editions(id,title,catalog,year) VALUES(?,?,?,?)",
        [(f"ROT-{i:04d}", f"Record {i}", f"CAT-{i:04d}", 1970+i%60)
         for i in range(3777)])
    db.commit()
    FakeApiFetcher.queries = []
    FakeApiFetcher.response = {"ok": True, "result": "OK", "items": []}
    offset = 0
    for _ in range(2):
        for batch in range(189):
            batch_size = 17 if batch == 188 else 20
            result = run_market_api_sweep(
                db, "rakuten", batch_size=batch_size,
                fetcher_factory=FakeApiFetcher)
            assert result["status"] == "ok"
            offset += 900
            clock.set_local_offset(offset)
    assert len(FakeApiFetcher.queries) == 7554
    assert db.execute(
        "SELECT COUNT(*) FROM market_api_search_state WHERE provider=?",
        ("rakuten",)).fetchone()[0] == 3777
    attempts = db.execute(
        "SELECT MIN(attempts),MAX(attempts) FROM market_api_search_state"
        " WHERE provider='rakuten'").fetchone()
    assert attempts[0] == attempts[1] == 2
    assert db.execute(
        "SELECT COUNT(*) FROM market_api_search_state WHERE provider=?"
        " AND last_success IS NOT NULL", ("rakuten",)).fetchone()[0] == 3777


def test_market_api_block_applies_source_cooldown(db, monkeypatch, tmp_path):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAKUTEN_APP_ID", "test-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "test-key")
    _edition(db, "API-1", "Record", "CAT-1", 1980)
    FakeApiFetcher.queries = []
    FakeApiFetcher.response = {"ok": False, "result": "BLOCKED", "items": []}
    first = run_market_api_sweep(db, "rakuten", batch_size=1,
                                 fetcher_factory=FakeApiFetcher)
    assert first["status"] == "partial" and first["checked"] == 1
    assert db.execute(
        "SELECT last_blocked FROM market_api_search_state"
        " WHERE provider='rakuten' AND edition_id='API-1'").fetchone()[0]
    calls = len(FakeApiFetcher.queries)
    second = run_market_api_sweep(db, "rakuten", batch_size=1,
                                  fetcher_factory=FakeApiFetcher)
    assert second["status"] == "blocked" and second["checked"] == 0
    assert len(FakeApiFetcher.queries) == calls


def test_used_market_sweep_prioritizes_unchecked_editions(db):
    _edition(db, "UM-3", "Third", "CAT-3", 2000)
    _edition(db, "UM-2", "Second", "CAT-2", 1990)
    _edition(db, "UM-1", "First", "CAT-1", 1980)
    FakeFetcher.queries = []
    FakeFetcher.items = []

    first = run_yahoo_used_sweep(
        db, batch_size=2, app_id="mock", fetcher_factory=FakeFetcher)
    second = run_yahoo_used_sweep(
        db, batch_size=2, app_id="mock", fetcher_factory=FakeFetcher)

    assert first["checked"] == second["checked"] == 2
    assert FakeFetcher.queries == ["CAT-1", "CAT-2", "CAT-3", "CAT-1"]
    assert db.execute(
        "SELECT COUNT(*) FROM used_market_search_state"
    ).fetchone()[0] == 3


def test_used_market_full_catalog_advances_without_starvation(db):
    db.executemany(
        "INSERT INTO editions(id, title, catalog, year) VALUES(?, ?, ?, ?)",
        [(f"ED-{i:04d}", f"Record {i}", f"CAT-{i:04d}",
          1970 + i % 60) for i in range(3777)])
    db.commit()
    FakeFetcher.queries = []
    FakeFetcher.items = []

    first = run_yahoo_used_sweep(
        db, app_id="mock", fetcher_factory=FakeFetcher)
    first_queries = set(FakeFetcher.queries)
    second = run_yahoo_used_sweep(
        db, app_id="mock", fetcher_factory=FakeFetcher)
    second_queries = set(FakeFetcher.queries) - first_queries

    assert first["checked"] == second["checked"] == 40
    assert len(first_queries) == len(second_queries) == 40
    assert not first_queries.intersection(second_queries)
    assert db.execute(
        "SELECT COUNT(*) FROM used_market_search_state"
    ).fetchone()[0] == 3777


def test_used_market_listing_requires_human_match_approval(db, monkeypatch):
    _edition(db, "UM-1", "Test Record", "CAT-1", 1980)
    FakeFetcher.queries = []
    FakeFetcher.items = [{
        "title": "Test Record", "raw_title": "Test Record CAT-1",
        "url": "https://store.shopping.yahoo.co.jp/shop/record.html",
        "image_url": "https://item-shopping.c.yimg.jp/i/g/record",
        "price": 4200, "currency": "JPY",
    }, {
        "title": "untrusted", "raw_title": "untrusted",
        "url": "https://attacker.example/item", "price": 1,
        "currency": "JPY",
    }]

    result = run_yahoo_used_sweep(
        db, batch_size=1, app_id="mock", fetcher_factory=FakeFetcher)

    listing = db.execute(
        "SELECT id, edition_id, image_url FROM listings"
        " WHERE source='Yahoo Shopping used'").fetchone()
    assert result["listings"] == 1
    assert listing["edition_id"] is None
    assert listing["image_url"] == \
        "https://item-shopping.c.yimg.jp/i/g/record"
    queue = db.execute(
        "SELECT payload_json FROM review_queue WHERE kind='match_review'"
        " AND resolved_at IS NULL").fetchall()
    assert queue
    assert all(json.loads(row[0])["result"]["disposition"]
               == "review" for row in queue)
    from grt.pricedrop import price_evidence_ok
    assert price_evidence_ok(db, listing["id"], "UM-1") is False
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('allowed_chat_id', '555'),"
        " ('deadline_override_quiet_selected', '1')")
    db.commit()
    from grt.bot import Bot
    bot = Bot(db, lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(bot, "_send", lambda *args, **kwargs: None)
    qid = db.execute(
        "SELECT id FROM review_queue WHERE kind='match_review'"
        " AND resolved_at IS NULL ORDER BY id LIMIT 1").fetchone()[0]
    action = bot.handle_update({
        "update_id": 900001,
        "callback_query": {"id": "market-review",
                           "message": {"chat": {"id": 555}},
                           "data": f"qok:{qid}"}})
    assert action == "match_review_approved"
    assert db.execute(
        "SELECT edition_id FROM listings WHERE id=?",
        (listing["id"],)).fetchone()[0] == "UM-1"
    assert price_evidence_ok(db, listing["id"], "UM-1") is True
    assert db.execute(
        "SELECT last_result FROM used_market_search_state"
        " WHERE edition_id='UM-1'").fetchone()[0] == "OK"
