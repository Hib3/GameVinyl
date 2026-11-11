"""Bounded, persistent sweep of used Yahoo Shopping listings."""
from __future__ import annotations

import math
from urllib.parse import urlsplit

from . import clock, matcher
from .fetchers.yahoo_shopping import (API, YahooShoppingFetcher,
                                      yahoo_app_id)

SOURCE_NAME = "Yahoo Shopping used"
BATCH_SIZE = 40
_UNSET = object()


def _https_host(value, suffix: str) -> bool:
    try:
        parsed = urlsplit(value or "")
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and bool(host)
                and not parsed.username and not parsed.password
                and (host == suffix or host.endswith("." + suffix)))
    except (TypeError, ValueError):
        return False


def _source_id(db) -> int:
    row = db.execute("SELECT id FROM sources WHERE name=?",
                     (SOURCE_NAME,)).fetchone()
    if row:
        return int(row[0])
    cur = db.execute(
        "INSERT INTO sources(name, url, kind, status)"
        " VALUES(?, ?, 'API', 'approved')", (SOURCE_NAME, API))
    db.commit()
    return int(cur.lastrowid)


def _upsert_listing(db, item: dict, now: str) -> int | None:
    url = item.get("url")
    if not _https_host(url, "shopping.yahoo.co.jp"):
        return None
    title = item.get("raw_title") or item.get("title") or ""
    if not isinstance(title, str):
        title = ""
    image = item.get("image_url")
    if not _https_host(image, "yimg.jp"):
        image = None
    price = item.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) \
            or not math.isfinite(price) or price <= 0:
        price = None

    existing = db.execute(
        "SELECT id FROM listings WHERE source=? AND url=?",
        (SOURCE_NAME, url)).fetchone()
    if existing:
        listing_id = int(existing[0])
        db.execute(
            "UPDATE listings SET price=?, currency='JPY', total_jpy=?"
            " , last_seen=?, raw_title=?, image_url=? WHERE id=?",
            (price, price, now, title, image, listing_id))
    else:
        cur = db.execute(
            "INSERT INTO listings(source, url, price, currency, total_jpy,"
            " status, first_seen, last_seen, raw_title, image_url)"
            " VALUES(?, ?, ?, 'JPY', ?, 'active', ?, ?, ?, ?)",
            (SOURCE_NAME, url, price, price, now, now, title, image))
        listing_id = int(cur.lastrowid)
    db.commit()
    return listing_id


def run_yahoo_used_sweep(db, batch_size: int = BATCH_SIZE, *,
                         app_id=_UNSET, fetcher_factory=None) -> dict:
    """Search a bounded fair batch; every match still needs a human."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) \
            or not 1 <= batch_size <= BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {BATCH_SIZE}")
    if app_id is _UNSET:
        app_id = yahoo_app_id()
    if not app_id:
        return {"status": "disabled", "checked": 0, "listings": 0,
                "errors": 0}

    source_id = _source_id(db)
    db.execute(
        "INSERT OR IGNORE INTO used_market_search_state(edition_id)"
        " SELECT id FROM editions")
    db.commit()
    rows = db.execute(
        "SELECT e.id, e.title, e.catalog FROM editions e"
        " JOIN used_market_search_state q ON q.edition_id=e.id"
        " ORDER BY CASE WHEN q.last_checked IS NULL THEN 0 ELSE 1 END,"
        " q.last_checked, e.year, e.id LIMIT ?", (batch_size,)).fetchall()
    if not rows:
        return {"status": "empty", "checked": 0, "listings": 0,
                "errors": 0}

    make_fetcher = fetcher_factory or YahooShoppingFetcher
    fetcher = make_fetcher(db, source_id, app_id=app_id)
    checked = listings = errors = 0
    for row in rows:
        raw_query = row[2] or row[1] or ""
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        now = clock.now_iso(db)
        result_code = "NO_QUERY"
        error_code = None
        if query:
            try:
                result = fetcher.run(query)
                if not isinstance(result, dict):
                    raise TypeError("fetcher result must be an object")
                result_code = str(result.get("result") or "ERROR")
                if result.get("ok"):
                    for item in result.get("items", []):
                        if not isinstance(item, dict):
                            continue
                        listing_id = _upsert_listing(db, item, now)
                        if listing_id is None:
                            continue
                        matcher.match_listing(
                            db, {**item, "source": SOURCE_NAME},
                            listing_id=listing_id,
                            require_human_review=True)
                        listings += 1
                else:
                    errors += 1
                    error_code = result_code
            except Exception as exc:
                db.rollback()
                result_code = "ERROR"
                error_code = type(exc).__name__
                errors += 1
        db.execute(
            "UPDATE used_market_search_state SET last_checked=?,"
            " last_result=?, error_code=?, attempts=attempts+?"
            " WHERE edition_id=?",
            (now, result_code, error_code, 1 if query else 0, row[0]))
        db.commit()
        checked += 1
    return {"status": "partial" if errors else "ok",
            "checked": checked, "listings": listings, "errors": errors}
