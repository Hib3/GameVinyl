"""Yahoo! Shopping Web Service fetcher (fetcher kind: API).

Phase 3 source: searches current Yahoo! Shopping listings by catalog
number / keyword through the free itemSearch API (V3). The API key is
the operator's own Client ID (appid), supplied via the YAHOO_APP_ID
environment variable. Without it the fetcher is disabled and says
so — it never fabricates data.
"""
from __future__ import annotations

import os

import httpx

from .. import clock, config
from ..evidence import normalized_text_hash, save_raw_evidence
from .base import (BaseFetcher, FetchError, api_list,
                   api_price, classify_http)

API = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"


def yahoo_app_id() -> str | None:
    """YAHOO_APP_ID from the environment (config-style getter).

    Mirrors config.discogs_token(): load_dotenv() first, then read
    os.environ. Returns None when unset — the disabled state.
    """
    config.load_dotenv()
    return os.environ.get("YAHOO_APP_ID") or None


class YahooShoppingFetcher(BaseFetcher):
    kind = "API"
    min_gap_seconds = 1.1  # sequential, low-rate by design

    def __init__(self, db, source_id: int, app_id=None):
        super().__init__(db, source_id)
        self.app_id = app_id if app_id is not None else yahoo_app_id()

    @property
    def enabled(self) -> bool:
        return bool(self.app_id)

    # ------------------------------------------------------------- search

    def search_catalog(self, catalog: str) -> list[dict]:
        """Search current listings for a catalog number / keyword.

        Returns items of the shape
        {title, url, price, currency, raw_title} with price in JPY
        (int) and currency 'JPY'. When the fetcher is disabled this
        returns [] WITHOUT any network call.
        Raises FetchError on HTTP / network / parse failure.
        """
        if not self.enabled:
            return []
        items, _ev = self._search(catalog)
        return items

    def search_used_catalog(self, catalog: str) -> list[dict]:
        """Search only currently in-stock used listings.

        Yahoo's official itemSearch API supports both filters. When the
        application ID is absent this returns [] without a network call.
        """
        if not self.enabled:
            return []
        items, _ev = self._search(catalog, condition="used", in_stock=True)
        return items

    def _search(self, catalog: str, condition=None,
                in_stock=None) -> tuple[list[dict], dict]:
        self._polite()
        params = {"appid": self.app_id, "query": catalog, "results": "30"}
        if condition is not None:
            params["condition"] = condition
        if in_stock is not None:
            params["in_stock"] = str(in_stock).lower()
        try:
            resp = httpx.get(
                API,
                params=params,
                headers={"User-Agent": "GameRecordTool/1.0"},
                timeout=30)
        except httpx.TimeoutException as exc:
            raise FetchError("TIMEOUT", str(exc))
        except httpx.HTTPError as exc:
            raise FetchError("NETWORK", str(exc))
        result = classify_http(resp.status_code)
        if result != "OK":
            raise FetchError(result, "HTTP " + str(resp.status_code)
                             + " for Yahoo Shopping catalog search")
        try:
            data = resp.json()
        except Exception as exc:  # broken / empty JSON body
            raise FetchError("PARSE_ERROR",
                             "Yahoo Shopping JSON parse failed: "
                             + str(exc)[:300])
        if not isinstance(data, dict):
            raise FetchError("PARSE_ERROR",
                             "Yahoo Shopping payload is not an object")
        items = []
        for entry in api_list(data, "hits", "Yahoo Shopping"):
            if not isinstance(entry, dict):
                continue
            if condition is not None and entry.get("condition") != condition:
                continue
            if in_stock is not None and entry.get("inStock") is not in_stock:
                continue
            title = entry.get("name") or ""
            price = api_price(entry.get("price"))
            items.append({
                "title": title,
                "url": entry.get("url") or "",
                "price": price,
                "currency": "JPY",
                "raw_title": title,
            })
        ev = save_raw_evidence("yahoo_shopping",
                               API + "?query=" + str(catalog),
                               resp.text or "", keep_body=False)
        ev["normalized_hash"] = normalized_text_hash(resp.text or "")
        return items, ev

    # ------------------------------------------------------ BaseFetcher

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        """BaseFetcher contract: `url` is a catalog number / keyword
        (or a full search URL, whose query= term is extracted)."""
        if not self.enabled:
            return [], {"disabled": True, "reason": "YAHOO_APP_ID not set",
                        "raw_hash": None}
        catalog = url
        if url.startswith("http"):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(url).query)
            catalog = (qs.get("query") or [url])[0]
        return self._search(catalog)

    def run(self, url: str) -> dict:
        if not self.enabled:
            self.db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('fetcher_disabled', ?, ?)",
                ('{"fetcher": "yahoo_shopping",'
                 ' "reason": "YAHOO_APP_ID not set"}',
                 clock.now_iso(self.db)))
            self.db.commit()
            return {"ok": False, "enabled": False, "result": "DISABLED",
                    "items": []}
        return super().run(url)
