import json

from grt.used_market import run_yahoo_used_sweep


class FakeFetcher:
    queries = []
    items = []

    def __init__(self, db, source_id, app_id=None):
        self.db = db

    def run(self, query):
        self.queries.append(query)
        return {"ok": True, "result": "OK", "items": self.items}


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
