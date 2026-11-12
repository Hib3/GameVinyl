"""eBay Browse API fetcher (fetcher kind: API).

Phase 3 source: searches current eBay listings by catalog number /
keyword through the free Browse API (item_summary/search). The Browse
API requires an OAuth *app* token (client-credentials grant). This
module only READS a token the operator supplies via the
EBAY_OAUTH_TOKEN environment variable — it never creates, refreshes,
or invents credentials. Without a token the fetcher is disabled and
says so; it never fabricates data.
"""
from __future__ import annotations

import os

import httpx

from .. import clock, config
from ..evidence import normalized_text_hash, save_raw_evidence
from .base import (BaseFetcher, FetchError, api_list,
                   api_price, classify_http)

API = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def ebay_oauth_token() -> str | None:
    """EBAY_OAUTH_TOKEN from the environment (config-style getter).

    Mirrors config.discogs_token(): load_dotenv() first, then read
    os.environ. Returns None when unset — the disabled state.
    """
    config.load_dotenv()
    return os.environ.get("EBAY_OAUTH_TOKEN") or None


class EbayFetcher(BaseFetcher):
    kind = "API"
    min_gap_seconds = 1.1  # sequential, low-rate by design

    def __init__(self, db, source_id: int, token=None):
        super().__init__(db, source_id)
        self.token = token if token is not None else ebay_oauth_token()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------- search

    def search_catalog(self, catalog: str) -> list[dict]:
        """Search current listings for a catalog number / keyword.

        Returns items of the shape
        {title, url, price, currency, raw_title}; price is a float
        (None when the listing carries no price). When the fetcher is
        disabled this returns [] WITHOUT any network call.
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
                params={"q": catalog, "limit": "50",
                        "filter": "conditions:{USED},deliveryCountry:JP"},
                headers={
                    "Authorization": "Bearer " + str(self.token),
                    "Accept": "application/json",
                    "User-Agent": "GameRecordTool/1.0",
                },
                timeout=30)
        except httpx.TimeoutException as exc:
            raise FetchError("TIMEOUT", str(exc))
        except httpx.HTTPError as exc:
            raise FetchError("NETWORK", str(exc))
        result = classify_http(resp.status_code)
        if result != "OK":
            raise FetchError(result, "HTTP " + str(resp.status_code)
                             + " for eBay catalog search",
                             http_status=resp.status_code)
        try:
            data = resp.json()
        except Exception as exc:  # broken / empty JSON body
            raise FetchError("PARSE_ERROR",
                             "eBay JSON parse failed: " + str(exc)[:300])
        if not isinstance(data, dict):
            raise FetchError("PARSE_ERROR", "eBay payload is not an object")
        items = []
        for entry in api_list(data, "itemSummaries", "eBay"):
            if not isinstance(entry, dict):
                continue
            title = entry.get("title") or ""
            price_obj = entry.get("price") or {}
            price = api_price(price_obj.get("value")) \
                if isinstance(price_obj, dict) else None
            items.append({
                "title": title,
                "url": entry.get("itemWebUrl") or "",
                "image_url": ((entry.get("image") or {}).get("imageUrl")
                              if isinstance(entry.get("image"), dict)
                              else ""),
                "price": price,
                "currency": (price_obj.get("currency")
                             if isinstance(price_obj, dict) else None),
                "raw_title": title,
            })
        ev = save_raw_evidence("ebay", API + "?q=" + str(catalog),
                               resp.text or "", keep_body=False)
        ev["normalized_hash"] = normalized_text_hash(resp.text or "")
        return items, ev

    # ------------------------------------------------------ BaseFetcher

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        """BaseFetcher contract: `url` is a catalog number / keyword
        (or a full search URL, whose q= term is extracted)."""
        if not self.enabled:
            return [], {"disabled": True, "reason": "EBAY_OAUTH_TOKEN not set",
                        "raw_hash": None}
        catalog = url
        if url.startswith("http"):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(url).query)
            catalog = (qs.get("q") or [url])[0]
        return self._search(catalog)

    def run(self, url: str) -> dict:
        if not self.enabled:
            self.db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('fetcher_disabled', ?, ?)",
                ('{"fetcher": "ebay", "reason": "EBAY_OAUTH_TOKEN not set"}',
                 clock.now_iso(self.db)))
            self.db.commit()
            return {"ok": False, "enabled": False, "result": "DISABLED",
                    "items": []}
        return super().run(url)
