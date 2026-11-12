"""Read-only Collector watch events -> existing Bot match-review queue."""
from contextlib import closing
import json
import logging
import math
import re
import sqlite3
from urllib.parse import urlsplit

from . import clock, config, matcher, outbox
from .normalize import one_line

CURSOR = "collector_watch_event_cursor"
SOURCE = "collector_watch"
EVENT = "match_review"
KEY = "collector_match_review|"
ACTIVE = {"AUCTION_OPEN", "IN_STOCK_STRUCTURED", "PURCHASE_BUTTON_OBSERVED",
          "PREORDER_STRUCTURED"}


def _open_collector():
    path = config.data_dir() / "collector" / "collector.sqlite3"
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        db.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        db.close()
        raise
    return db


def _facts(payload):
    if not isinstance(payload, str) or len(payload) > 65536:
        raise ValueError("Invalid Collector facts size")
    f = json.loads(payload)
    if (not isinstance(f, dict) or not isinstance(f.get("availability"), str)
            or not isinstance(f.get("page_type"), str)):
        raise ValueError("Invalid Collector facts")
    ids = f.get("watch_ids", [])
    if (not isinstance(ids, list) or len(ids) > 100
            or any(not isinstance(i, str) or not re.fullmatch(r"AR-[0-9]{6}", i) for i in ids)):
        raise ValueError("Invalid Collector Edition hints")
    if not isinstance(f.get("title"), str):
        raise ValueError("Invalid Collector title")
    return f, set(ids)


def _valid_url(url):
    if not isinstance(url, str) or len(url) > 1024 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise ValueError("Invalid Collector URL")
    p = urlsplit(url)
    if (p.scheme not in ("https", "http") or not p.hostname or "." not in p.hostname
            or p.username is not None or p.password is not None or p.port not in (None, 80, 443)):
        raise ValueError("Invalid Collector URL")


def _epoch(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Invalid Collector timestamp")
    return value


def _active(f, now):
    if f.get("page_type") != "product" or f["availability"] not in ACTIVE:
        return False
    return f["availability"] != "AUCTION_OPEN" or _epoch(f.get("auction_end")) > now


def _latest(source, url, now):
    row = source.execute("""
        SELECT j.source,j.status,j.last_error,o.observed,o.expires,
               CASE WHEN length(o.facts)<=65536 THEN o.facts END
        FROM observations o JOIN jobs j ON j.url=o.url
        WHERE o.url=? ORDER BY o.id DESC LIMIT 1
        """, (url,)).fetchone()
    if row is None:
        raise ValueError("Missing Collector observation")
    name, status, error, observed, expires, payload = row
    if status == "running":
        raise RuntimeError("Collector is rechecking the listing")
    if status != "ok" or error or not _epoch(observed) <= now < _epoch(expires):
        return None
    f, ids = _facts(payload)
    return (name, f, ids) if _active(f, now) else None


def _enqueue_reviews(db, source, url, ids, facts):
    title = one_line(facts["title"])[:160]
    name = one_line(source)[:60]
    now = clock.now_iso(db)
    # This is a review link, not a price import (an auction bid is not a sale price).
    db.execute("""INSERT INTO listings(source,url,status,first_seen,last_seen,raw_title)
                  VALUES(?,?,'pending_review',?,?,?) ON CONFLICT(source,url) DO NOTHING""",
               (SOURCE, url, now, now, title))
    listing_id, = db.execute("SELECT id FROM listings WHERE source=? AND url=?",
                             (SOURCE, url)).fetchone()
    listing = {"source": SOURCE, "url": url, "raw_title": title}
    matcher.match_listing(db, listing, listing_id=listing_id,
                          require_human_review=True, review_edition_ids=tuple(sorted(ids)),
                          commit=False)
    count = 0
    for edition in sorted(ids):
        q = db.execute("""SELECT id FROM review_queue WHERE kind='match_review'
                          AND fingerprint=? AND resolved_at IS NULL AND
                          CASE WHEN json_valid(payload_json) THEN
                            json_extract(payload_json,'$.listing.source') END=?
                          ORDER BY id LIMIT 1""",(f"{url}|{edition}",SOURCE)).fetchone()
        if not q:
            continue
        qid = q[0]
        key = KEY + str(qid)
        if db.execute("SELECT 1 FROM notification_outbox WHERE idempotency_key=?", (key,)).fetchone():
            continue
        if outbox.enqueue(db, event_type=EVENT, edition_id=edition, idempotency_key=key,
                          body=f"出品レビュー（版は未確認）\n{title}\n出典: {name}\n{url}\n候補Edition: {edition}",
                          score=45, require_transaction=True, reply_markup={"inline_keyboard": [[
                              {"text":"承認","callback_data":f"qok:{qid}"},
                              {"text":"却下","callback_data":f"qno:{qid}"}
                          ]]}) is None:
            raise RuntimeError("Collector review enqueue failed")
        count += 1
    return count


def poll(db):
    """Retry from the first uncommitted event after any failure."""
    count = 0
    try:
        cursor = int(config.get_setting(db, CURSOR, "0"))
        with closing(_open_collector()) as source:
            events = source.execute("""
                SELECT id,url,created,CASE WHEN length(detail)<=65536 THEN detail END
                FROM events INDEXED BY watch_event_url
                WHERE kind='watch_new' AND id>? ORDER BY id LIMIT 20
                """, (cursor,)).fetchall()
            for event_id, url, created, detail in events:
                _valid_url(url)
                f, hints = _facts(detail)
                if "watch_ids" not in f:
                    raise ValueError("Collector event has no Edition hints")
                now = clock.now(db).timestamp()
                created = _epoch(created)
                # shortcut: original event age is capped at 24h to avoid replaying old alerts.
                latest = _latest(source,url,now) if now-86400 <= created <= now and hints and _active(f,now) else None
                db.execute("BEGIN IMMEDIATE")
                if event_id <= int(config.get_setting(db,CURSOR,"0")):
                    db.rollback()
                    continue
                added = 0
                if latest:
                    name, current_facts, current_ids = latest
                    ids = {i for i in hints & current_ids if db.execute(
                        "SELECT 1 FROM editions WHERE id=?", (i,)).fetchone()}
                    if ids:
                        added = _enqueue_reviews(db, name, url, ids, current_facts)
                db.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                              ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                           (CURSOR,str(event_id)))
                db.commit()
                count += added
    except (sqlite3.Error, ValueError, RuntimeError, OSError) as exc:
        try:
            db.rollback()
        except sqlite3.Error:
            pass
        logging.getLogger(__name__).warning("Collector watch poll deferred (%s)",type(exc).__name__)
    return count


def delivery_allowed(db, key, edition):
    """Deferred sends must not turn ended or expired observations into alerts."""
    qid = int(key.removeprefix(KEY))
    q = db.execute("SELECT payload_json FROM review_queue WHERE id=? AND kind='match_review'"
                   " AND resolved_at IS NULL", (qid,)).fetchone()
    if not q or not db.execute("SELECT 1 FROM editions WHERE id=?", (edition,)).fetchone():
        return False
    url = json.loads(q[0])["listing"]["url"]
    _valid_url(url)
    with closing(_open_collector()) as source:
        latest = _latest(source,url,clock.now(db).timestamp())
        return latest is not None and edition in latest[2]
