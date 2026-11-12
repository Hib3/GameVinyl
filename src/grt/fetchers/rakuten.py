"""Rakuten Ichiba Item Search API fetcher (fetcher kind: API).

Phase 3 source: searches current Rakuten Ichiba listings by catalog
number / keyword through the free Rakuten Web Service. Without both
operator credentials the fetcher is disabled and makes no network call.
"""
from __future__ import annotations

import os

import httpx

from .. import clock, config
from ..evidence import normalized_text_hash, save_raw_evidence
from .base import (BaseFetcher, FetchError, api_list,
                   api_price, classify_http)

API = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"


def rakuten_app_id() -> str | None:
    """RAKUTEN_APP_ID from the environment (config-style getter).

    Mirrors config.discogs_token(): load_dotenv() first, then read
    os.environ. Returns None when unset — the disabled state.
    """
    config.load_dotenv()
    return os.environ.get("RAKUTEN_APP_ID") or None


def rakuten_access_key() -> str | None:
    config.load_dotenv()
    return os.environ.get("RAKUTEN_ACCESS_KEY") or None


class RakutenFetcher(BaseFetcher):
    kind = "API"
    min_gap_seconds = 1.1  # Rakuten guideline: at most ~1 request/second

    def __init__(self, db, source_id: int, app_id=None, access_key=None):
        super().__init__(db, source_id)
        self.app_id = app_id if app_id is not None else rakuten_app_id()
        self.access_key = (access_key if access_key is not None
                           else rakuten_access_key())

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.access_key)

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

    def _search(self, catalog: str) -> tuple[list[dict], dict]:
        self._polite()
        try:
            resp = httpx.get(
                API,
                params={"applicationId": self.app_id, "keyword": catalog,
                        "hits": "30", "format": "json",
                        "formatVersion": "2"},
                headers={"User-Agent": "GameRecordTool/1.0",
                         "accessKey": self.access_key},
                timeout=30)
        except httpx.TimeoutException as exc:
            raise FetchError("TIMEOUT", str(exc))
        except httpx.HTTPError as exc:
            raise FetchError("NETWORK", str(exc))
        result = classify_http(resp.status_code)
        if result != "OK":
            raise FetchError(result, "HTTP " + str(resp.status_code)
                             + " for Rakuten catalog search",
                             http_status=resp.status_code)
        try:
            data = resp.json()
        except Exception as exc:  # broken / empty JSON body
            raise FetchError("PARSE_ERROR",
                             "Rakuten JSON parse failed: " + str(exc)[:300])
        if not isinstance(data, dict):
            raise FetchError("PARSE_ERROR", "Rakuten payload is not an object")
        items = []
        for entry in api_list(data, "items", "Rakuten"):
            if not isinstance(entry, dict):
                continue
            title = entry.get("itemName") or ""
            price = api_price(entry.get("itemPrice"))
            items.append({
                "title": title,
                "url": entry.get("itemUrl") or "",
                "image_url": next(iter(entry.get("mediumImageUrls") or []), ""),
                "price": price,
                "currency": "JPY",
                "raw_title": title,
            })
        ev = save_raw_evidence("rakuten", API + "?keyword=" + str(catalog),
                               resp.text or "", keep_body=False)
        ev["normalized_hash"] = normalized_text_hash(resp.text or "")
        return items, ev

    # ------------------------------------------------------ BaseFetcher

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        """BaseFetcher contract: `url` is a catalog number / keyword
        (or a full search URL, whose keyword= term is extracted)."""
        if not self.enabled:
            return [], {"disabled": True,
                        "reason": "Rakuten API credentials not set",
                        "raw_hash": None}
        catalog = url
        if url.startswith("http"):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(url).query)
            catalog = (qs.get("keyword") or [url])[0]
        return self._search(catalog)

    def run(self, url: str) -> dict:
        if not self.enabled:
            self.db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('fetcher_disabled', ?, ?)",
                ('{"fetcher": "rakuten", "reason": "credentials not set"}',
                 clock.now_iso(self.db)))
            self.db.commit()
            return {"ok": False, "enabled": False, "result": "DISABLED",
                    "items": []}
        return super().run(url)
