"""Label direct-store / reservation-list HTML watching.

Spec Phase 2: label shops and pre-order listing pages are watched
with our own fetcher + a normalized-text hash comparison — no
external change-detection service (the product stays free and
self-contained). A page whose normalized text changed since the
last check produces one 'store_page_changed' system event and, for
approved sources, one outbox notification; identical pages are
silent. Volatile noise (dates, times, session tokens, script/style
bodies) is stripped by volatile_stripped_hash before hashing.
"""
from __future__ import annotations

import json
import re

import httpx
from bs4 import BeautifulSoup

from . import clock
from . import outbox as outbox_mod
from .evidence import normalized_text_hash
from .fetchers.base import FetchError, classify_http, http_get

# Volatile-noise stripping (review fix, seller D9): the first
# version hashed the raw page after whitespace normalisation only,
# so a date rolling over — or a banner/session token changing —
# fired a "page changed" notification. The hash input is now the
# visible text with scripts/styles removed and date/time/session
# tokens stripped. Prices and product text are KEPT: a price or
# lineup change is exactly what this watch exists for.
_DATE_RE = re.compile(
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}日"
    r"|\d{1,2}:\d{2}(:\d{2})?")
_SESSION_RE = re.compile(r"\b[0-9a-f]{24,}\b|\bsession[^<\s]*", re.I)


def volatile_stripped_hash(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = _DATE_RE.sub("", text)
    text = _SESSION_RE.sub("", text)
    return normalized_text_hash(text)


def check_source(db, source_id: int, *, client=None) -> dict:
    src = db.execute("SELECT * FROM sources WHERE id=?",
                     (source_id,)).fetchone()
    if src is None or not src["url"]:
        return {"ok": False, "result": "NO_SOURCE"}
    url = src["url"]
    try:
        if client is not None:
            resp = client.get(url)
        else:
            # W-check B5 (adversarial QA 中-2, extended): this
            # lane fetches arbitrary shop pages and used to buffer
            # them with a bare httpx.get — the same unbounded-body
            # exposure http_get now caps at 25MB. FetchError
            # carries the classification (BLOCKED / HTTP_ERROR /
            # RESPONSE_TOO_LARGE / ...), so map it through.
            resp = http_get(url)
    except FetchError as exc:
        return {"ok": False, "result": exc.result,
                "detail": (exc.detail or "")[:200]}
    except Exception as exc:
        return {"ok": False, "result": "NETWORK", "detail": str(exc)[:200]}
    result = classify_http(resp.status_code)
    if result != "OK":
        return {"ok": False, "result": result}
    digest = volatile_stripped_hash(resp.text or "")
    state = db.execute(
        "SELECT * FROM store_page_state WHERE source_id=?",
        (source_id,)).fetchone()
    now = clock.now_iso(db)
    if state is None:
        db.execute(
            "INSERT INTO store_page_state(source_id, last_hash,"
            " last_checked, last_changed) VALUES(?,?,?,NULL)",
            (source_id, digest, now))
        db.commit()
        return {"ok": True, "result": "BASELINE", "changed": False}
    changed = state["last_hash"] != digest
    db.execute(
        "UPDATE store_page_state SET last_hash=?, last_checked=?"
        + (", last_changed=?" if changed else "")
        + " WHERE source_id=?",
        (digest, now, now, source_id) if changed
        else (digest, now, source_id))
    if changed:
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('store_page_changed', ?, ?)",
            (json.dumps({"source_id": source_id, "url": url}), now))
        if src["status"] == "approved":
            outbox_mod.enqueue(
                db, event_type="store_page_changed",
                body=f"直販/予約ページに変化: {src['name']}\n{url}",
                idempotency_key=f"storechg|{source_id}|{digest[:16]}")
    db.commit()
    return {"ok": True, "result": "OK", "changed": changed}


def run_all(db, *, client=None) -> list[dict]:
    """Check every approved HTML store/reservation source that is
    DUE under the frequency ladder (review fix: the ladder used to
    be record-only; run_all now consults freqpolicy.is_due).

    W-check B1 (AI engineer #3): one source's unexpected exception
    (a response object of an unexpected shape, a DB write error)
    previously aborted the whole run — every later source silently
    skipped, every 15 minutes, forever, with nothing recorded.
    Per-source containment: the failing source yields an ERROR
    result plus a system_events record; the rest still run."""
    from . import freqpolicy, health
    rows = db.execute(
        "SELECT id FROM sources WHERE status='approved' AND"
        " kind IN ('STORE','LABEL') AND url IS NOT NULL").fetchall()
    results = []
    for r in rows:
        if not freqpolicy.is_due(db, r[0]):
            continue
        # Blocked shelf (R9 maniac A, D-8 scope): like the Discogs
        # lane, this lane does not pass through BaseFetcher.run, so
        # it consults the shelf itself — a shelved source is not
        # probed again until the 24h window passes.
        try:
            if health.is_shelved(db, r[0]):
                continue
        except Exception:
            pass
        started = clock.now_iso(db)
        try:
            result = check_source(db, r[0], client=client)
            result.setdefault("source_id", r[0])
            results.append(result)
            # W-check B2 (maniac A D-4 / wholesaler D-1): storewatch
            # never wrote fetch_runs, so is_due's evidence base never
            # grew — a source demoted to the 1440-minute tier was
            # still fetched every 15-minute tick (up to 96x the
            # designed load on small label shops), and a string of
            # 403s left no trace in health, the blocked shelf or the
            # ladder. Every check is now a ledgered run like any
            # other lane's.
            recorded = "OK" if result.get("ok") else str(
                result.get("result") or "PARSE_ERROR")
            if recorded == "BASELINE":
                recorded = "OK"
            try:
                health.record_fetch_run(
                    db, r[0], result=recorded, started_at=started,
                    finished_at=clock.now_iso(db), fetcher_kind="HTML")
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            freqpolicy.apply_run_outcome(db, r[0], bool(result.get("ok")))
        except Exception as exc:
            db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('storewatch_error', ?, ?)",
                (json.dumps({"source_id": r[0], "error": repr(exc)}),
                 clock.now_iso(db)))
            db.commit()
            try:
                health.record_fetch_run(
                    db, r[0], result="PARSE_ERROR", started_at=started,
                    finished_at=clock.now_iso(db),
                    error_detail=str(exc)[:300], fetcher_kind="HTML")
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            freqpolicy.apply_run_outcome(db, r[0], False)
            results.append({"ok": False, "result": "ERROR",
                            "source_id": r[0], "error": repr(exc)})
    return results
