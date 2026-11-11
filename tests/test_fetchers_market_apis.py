"""Phase 3 market API fetchers: eBay Browse / Rakuten Ichiba /
Yahoo! Shopping.

All network access is monkeypatched (httpx.get) with canned JSON —
no real API keys exist in the test environment and none are needed:
each fetcher must be DISABLED without its env credential (no network
call, clear disabled result), map its API's payload onto the shared
item shape when enabled, and classify HTTP failures through
base.classify_http (403 -> BLOCKED FetchError).
"""
import json

import httpx
import pytest

from grt.fetchers.base import FetchError
from grt.fetchers.ebay import EbayFetcher
from grt.fetchers.rakuten import RakutenFetcher
from grt.fetchers.yahoo_shopping import YahooShoppingFetcher

CATALOG = "TGCS-123"


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _no_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("network call attempted while disabled")
    monkeypatch.setattr(httpx, "get", boom)


def _source_id(db, name):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status) VALUES(?, 'shop', 'approved')",
        (name,))
    db.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ eBay

EBAY_PAYLOAD = {
    "total": 2,
    "itemSummaries": [
        {"title": "Game OST Vinyl TGCS-123",
         "itemWebUrl": "https://www.ebay.com/itm/111",
         "price": {"value": "45.50", "currency": "USD"}},
        {"title": "TGCS-123 loose disc, no price listed",
         "itemWebUrl": "https://www.ebay.com/itm/222"},
    ],
}


def test_ebay_disabled_without_token(db, monkeypatch):
    monkeypatch.delenv("EBAY_OAUTH_TOKEN", raising=False)
    _no_network(monkeypatch)
    f = EbayFetcher(db, _source_id(db, "ebay"))
    assert f.enabled is False
    assert f.search_catalog(CATALOG) == []
    result = f.run(CATALOG)
    assert result["enabled"] is False
    assert result["result"] == "DISABLED"
    row = db.execute(
        "SELECT detail_json FROM system_events"
        " WHERE kind='fetcher_disabled'").fetchone()
    assert row is not None and "ebay" in row[0]


def test_ebay_item_mapping(db, monkeypatch):
    calls = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.update(url=url, params=params, headers=headers)
        return FakeResp(200, EBAY_PAYLOAD)

    monkeypatch.setattr(httpx, "get", fake_get)
    f = EbayFetcher(db, _source_id(db, "ebay"), token="test-token")
    assert f.enabled is True
    items = f.search_catalog(CATALOG)
    assert calls["params"]["q"] == CATALOG
    assert calls["headers"]["Authorization"] == "Bearer test-token"
    assert items[0] == {
        "title": "Game OST Vinyl TGCS-123",
        "url": "https://www.ebay.com/itm/111",
        "price": 45.50,
        "currency": "USD",
        "raw_title": "Game OST Vinyl TGCS-123",
    }
    assert items[1]["price"] is None  # no fabricated price


def test_ebay_403_is_blocked(db, monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: FakeResp(403, {"errors": []}))
    f = EbayFetcher(db, _source_id(db, "ebay"), token="test-token")
    with pytest.raises(FetchError) as ei:
        f.search_catalog(CATALOG)
    assert ei.value.result == "BLOCKED"
    result = f.run(CATALOG)
    assert result["ok"] is False and result["result"] == "BLOCKED"


def test_ebay_env_token_enables(db, monkeypatch):
    monkeypatch.setenv("EBAY_OAUTH_TOKEN", "env-token")
    f = EbayFetcher(db, _source_id(db, "ebay"))
    assert f.enabled is True


# --------------------------------------------------------------- Rakuten

RAKUTEN_PAYLOAD = {
    "count": 1, "page": 1, "hits": 1,
    "Items": [
        {"Item": {"itemName": "ゲーム サントラ TGCS-123",
                   "itemUrl": "https://item.rakuten.co.jp/shop/abc/",
                   "itemPrice": 3980}},
    ],
}


def test_rakuten_disabled_without_app_id(db, monkeypatch):
    monkeypatch.delenv("RAKUTEN_APP_ID", raising=False)
    _no_network(monkeypatch)
    f = RakutenFetcher(db, _source_id(db, "rakuten"))
    assert f.enabled is False
    assert f.search_catalog(CATALOG) == []
    result = f.run(CATALOG)
    assert result["enabled"] is False
    assert result["result"] == "DISABLED"


def test_rakuten_item_mapping(db, monkeypatch):
    calls = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.update(url=url, params=params)
        return FakeResp(200, RAKUTEN_PAYLOAD)

    monkeypatch.setattr(httpx, "get", fake_get)
    f = RakutenFetcher(db, _source_id(db, "rakuten"), app_id="test-app-id")
    assert f.enabled is True
    items = f.search_catalog(CATALOG)
    assert calls["params"]["applicationId"] == "test-app-id"
    assert calls["params"]["keyword"] == CATALOG
    assert items == [{
        "title": "ゲーム サントラ TGCS-123",
        "url": "https://item.rakuten.co.jp/shop/abc/",
        "price": 3980,
        "currency": "JPY",
        "raw_title": "ゲーム サントラ TGCS-123",
    }]


def test_rakuten_403_is_blocked(db, monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: FakeResp(403, {"errors": []}))
    f = RakutenFetcher(db, _source_id(db, "rakuten"), app_id="test-app-id")
    with pytest.raises(FetchError) as ei:
        f.search_catalog(CATALOG)
    assert ei.value.result == "BLOCKED"
    result = f.run(CATALOG)
    assert result["ok"] is False and result["result"] == "BLOCKED"


# --------------------------------------------------------- Yahoo Shopping

YAHOO_PAYLOAD = {
    "totalResultsAvailable": 1, "firstResultPosition": 1,
    "hits": [
        {"name": "ゲーム サントラ TGCS-123",
         "url": "https://store.shopping.yahoo.co.jp/shop/abc.html",
         "price": 4200},
    ],
}


def test_yahoo_disabled_without_app_id(db, monkeypatch):
    monkeypatch.delenv("YAHOO_APP_ID", raising=False)
    _no_network(monkeypatch)
    f = YahooShoppingFetcher(db, _source_id(db, "yahoo"))
    assert f.enabled is False
    assert f.search_catalog(CATALOG) == []
    assert f.search_used_catalog(CATALOG) == []
    result = f.run(CATALOG)
    assert result["enabled"] is False
    assert result["result"] == "DISABLED"


def test_yahoo_item_mapping(db, monkeypatch):
    calls = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.update(url=url, params=params)
        return FakeResp(200, YAHOO_PAYLOAD)

    monkeypatch.setattr(httpx, "get", fake_get)
    f = YahooShoppingFetcher(db, _source_id(db, "yahoo"), app_id="test-app-id")
    assert f.enabled is True
    items = f.search_catalog(CATALOG)
    assert calls["params"]["appid"] == "test-app-id"
    assert calls["params"]["query"] == CATALOG
    assert items == [{
        "title": "ゲーム サントラ TGCS-123",
        "url": "https://store.shopping.yahoo.co.jp/shop/abc.html",
        "price": 4200,
        "currency": "JPY",
        "raw_title": "ゲーム サントラ TGCS-123",
    }]


def test_yahoo_used_search_filters_to_in_stock_used_items(db, monkeypatch):
    calls = {}
    payload = {"hits": [
        {"name": "used in stock", "url": "https://example.test/used",
         "price": 1000, "condition": "used", "inStock": True},
        {"name": "used out of stock", "url": "https://example.test/oos",
         "price": 800, "condition": "used", "inStock": False},
        {"name": "new in stock", "url": "https://example.test/new",
         "price": 1200, "condition": "new", "inStock": True},
    ]}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.update(url=url, params=params)
        return FakeResp(200, payload)

    monkeypatch.setattr(httpx, "get", fake_get)
    f = YahooShoppingFetcher(db, _source_id(db, "yahoo"), app_id="test-app-id")
    items = f.search_used_catalog(CATALOG)

    assert calls["params"]["condition"] == "used"
    assert calls["params"]["in_stock"] == "true"
    assert calls["params"]["query"] == CATALOG
    assert [item["title"] for item in items] == ["used in stock"]


def test_yahoo_403_is_blocked(db, monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: FakeResp(403, {"Error": {}}))
    f = YahooShoppingFetcher(db, _source_id(db, "yahoo"), app_id="test-app-id")
    with pytest.raises(FetchError) as ei:
        f.search_catalog(CATALOG)
    assert ei.value.result == "BLOCKED"
    result = f.run(CATALOG)
    assert result["ok"] is False and result["result"] == "BLOCKED"


def test_yahoo_run_records_ok_fetch_run(db, monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **kw: FakeResp(200, YAHOO_PAYLOAD))
    sid = _source_id(db, "yahoo")
    f = YahooShoppingFetcher(db, sid, app_id="test-app-id")
    result = f.run(CATALOG)
    assert result["ok"] is True
    assert len(result["items"]) == 1
    row = db.execute(
        "SELECT result, fetcher_kind, items_extracted FROM fetch_runs"
        " WHERE source_id=?", (sid,)).fetchone()
    assert row[0] == "OK" and row[1] == "API" and row[2] == 1


def test_market_api_keys_load_from_data_env(tmp_path, monkeypatch):
    """Regression for the v2.0 integration gap: the guide tells the
    operator to put the optional keys in data/.env, so load_dotenv's
    allowlist must actually admit all three (v2.0.1)."""
    from grt.fetchers.ebay import ebay_oauth_token
    from grt.fetchers.rakuten import rakuten_app_id
    from grt.fetchers.yahoo_shopping import yahoo_app_id

    (tmp_path / ".env").write_text(
        "EBAY_OAUTH_TOKEN=ebay-from-env-file\n"
        "RAKUTEN_APP_ID=rakuten-from-env-file\n"
        "YAHOO_APP_ID=yahoo-from-env-file\n",
        encoding="utf-8")
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    for key in ("EBAY_OAUTH_TOKEN", "RAKUTEN_APP_ID", "YAHOO_APP_ID"):
        monkeypatch.delenv(key, raising=False)

    assert ebay_oauth_token() == "ebay-from-env-file"
    assert rakuten_app_id() == "rakuten-from-env-file"
    assert yahoo_app_id() == "yahoo-from-env-file"
