"""Bounded, persistent sweep of used Yahoo Shopping listings."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
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


def _upsert_listing(db, item: dict, now: str, *,
                    source_name=SOURCE_NAME, listing_host="shopping.yahoo.co.jp",
                    image_host="yimg.jp") -> int | None:
    url = item.get("url")
    if not _https_host(url, listing_host):
        return None
    title = item.get("raw_title") or item.get("title") or ""
    if not isinstance(title, str):
        title = ""
    image = item.get("image_url")
    if not _https_host(image, image_host):
        image = None
    price = item.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) \
            or not math.isfinite(price) or price <= 0:
        price = None

    currency = item.get("currency") or "JPY"
    if not isinstance(currency, str) or len(currency) != 3 \
            or not currency.isalpha():
        currency = None
    currency = currency.upper() if currency else None
    total_jpy = price if currency == "JPY" else None
    existing = db.execute(
        "SELECT id FROM listings WHERE source=? AND url=?",
        (source_name, url)).fetchone()
    if existing:
        listing_id = int(existing[0])
        db.execute(
            "UPDATE listings SET price=?, currency=?, total_jpy=?"
            " , last_seen=?, raw_title=?, image_url=? WHERE id=?",
            (price, currency, total_jpy, now, title, image, listing_id))
    else:
        cur = db.execute(
            "INSERT INTO listings(source, url, price, currency, total_jpy,"
            " status, first_seen, last_seen, raw_title, image_url)"
            " VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
            (source_name, url, price, currency, total_jpy,
             now, now, title, image))
        listing_id = int(cur.lastrowid)
    db.commit()
    return listing_id


def run_market_api_sweep(db, provider: str, batch_size: int = 20, *,
                         fetcher_factory=None) -> dict:
    """Fair Rakuten API search; eBay stays off until expiry handling exists."""
    if provider not in ("ebay", "rakuten"):
        raise ValueError("provider must be 'ebay' or 'rakuten'")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) \
            or not 1 <= batch_size <= 20:
        raise ValueError("batch_size must be between 1 and 20")
    # eBay listing data cannot enter persistent review storage until its
    # six-hour expiry and Telegram deletion lifecycle is implemented.
    if provider == "ebay":
        return {"status": "disabled", "checked": 0,
                "listings": 0, "errors": 0}
    from .fetchers.rakuten import (RakutenFetcher, rakuten_access_key,
                                   rakuten_app_id)
    credentials = {"app_id": rakuten_app_id(),
                   "access_key": rakuten_access_key()}
    name = "Rakuten Ichiba"
    api_url = ("https://openapi.rakuten.co.jp/ichibams/api/"
               "IchibaItem/Search/20260701")
    listing_host, image_host = "rakuten.co.jp", "image.rakuten.co.jp"
    default_factory = RakutenFetcher
    if not all(credentials.values()):
        return {"status": "disabled", "checked": 0, "listings": 0,
                "errors": 0}

    now = clock.now_iso(db)
    block = db.execute(
        "SELECT blocked_until FROM market_api_source_state WHERE provider=?",
        (provider,)).fetchone()
    if block and block[0]:
        try:
            if datetime.fromisoformat(block[0]) > datetime.fromisoformat(now):
                return {"status": "blocked", "checked": 0,
                        "listings": 0, "errors": 0}
        except ValueError:
            pass
    source = db.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()
    if source:
        source_id = int(source[0])
    else:
        cur = db.execute(
            "INSERT INTO sources(name,url,kind,status)"
            " VALUES(?,?,'API','approved')", (name, api_url))
        source_id = int(cur.lastrowid)
    db.execute(
        "INSERT OR IGNORE INTO market_api_search_state(provider,edition_id)"
        " SELECT ?,id FROM editions", (provider,))
    db.commit()
    rows = db.execute(
        "SELECT e.id,e.title,e.catalog FROM editions e"
        " JOIN market_api_search_state q ON q.edition_id=e.id"
        " WHERE q.provider=? ORDER BY CASE WHEN q.last_checked IS NULL"
        " THEN 0 ELSE 1 END,q.last_checked,e.year,e.id LIMIT ?",
        (provider,batch_size)).fetchall()
    fetcher = (fetcher_factory or default_factory)(
        db, source_id, **credentials)
    checked = listings = errors = 0
    for row in rows:
        raw = row[2] or row[1] or ""
        query = raw.strip() if isinstance(raw, str) else ""
        observed = clock.now_iso(db)
        result_code, error_code = "NO_QUERY", None
        if query:
            try:
                result = fetcher.run(query)
                if not isinstance(result, dict):
                    raise TypeError("fetcher result must be an object")
                result_code = str(result.get("result") or "ERROR")
                if result.get("ok"):
                    for item in result.get("items", []):
                        if not isinstance(item, dict) or not _https_host(
                                item.get("url"), listing_host):
                            continue
                        listing_id = _upsert_listing(
                            db, item, observed, source_name=name,
                            listing_host=listing_host, image_host=image_host)
                        if listing_id is None:
                            continue
                        matcher.match_listing(
                            db, {**item, "source": name},
                            listing_id=listing_id, require_human_review=True)
                        listings += 1
                else:
                    errors += 1
                    error_code = result_code
            except Exception as exc:
                db.rollback()
                result_code, error_code = "ERROR", type(exc).__name__
                errors += 1
        columns = {"OK": "last_success", "BLOCKED": "last_blocked",
                   "NO_QUERY": "last_no_query"}
        if result_code in {"ERROR", "HTTP_ERROR", "TIMEOUT", "NETWORK",
                           "PARSE_ERROR"}:
            columns[result_code] = "last_error"
        timestamp_column = columns.get(result_code)
        update = ("UPDATE market_api_search_state SET last_checked=?"
                  ",last_result=?,error_code=?,attempts=attempts+?")
        values = [observed,result_code,error_code,1 if query else 0]
        if timestamp_column:
            update += f",{timestamp_column}=?"
            values.append(observed)
        update += " WHERE provider=? AND edition_id=?"
        values.extend((provider,row[0]))
        db.execute(update, values)
        db.commit()
        checked += 1
        if result_code == "BLOCKED":
            blocked_until = (datetime.fromisoformat(observed)
                             + timedelta(hours=6)).isoformat()
            db.execute(
                "INSERT INTO market_api_source_state(provider,blocked_until,last_error)"
                " VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET"
                " blocked_until=excluded.blocked_until,last_error=excluded.last_error",
                (provider,blocked_until,error_code or result_code))
            db.commit()
            break
        if result_code == "OK":
            db.execute(
                "INSERT INTO market_api_source_state(provider,blocked_until,last_error)"
                " VALUES(?,NULL,NULL) ON CONFLICT(provider) DO UPDATE SET"
                " blocked_until=NULL,last_error=NULL", (provider,))
            db.commit()
    return {"status": "partial" if errors else "ok", "checked": checked,
            "listings": listings, "errors": errors}


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
