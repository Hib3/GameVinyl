"""Shared fetcher machinery (spec section 3): one module for low-rate
sequential fetching, fetch_runs recording, evidence packs and health
reporting across all fetcher kinds (API / RSS / HTML / INGEST / GOODS /
DOUJIN_EVENT / BIBLIO / OFFICIAL_ALERT).
"""
from __future__ import annotations

import time

import httpx

from .. import clock, health
from ..evidence import normalized_text_hash, save_raw_evidence

FETCHER_KINDS = {"API", "RSS", "HTML", "INGEST", "GOODS", "DOUJIN_EVENT",
                 "BIBLIO", "OFFICIAL_ALERT"}


def api_price(value) -> float | None:
    """Validate a price coming from an external API payload.

    Review fix (adversarial QA): clients used to coerce blindly —
    True became 1, 4200.9 was truncated to 4200, and NaN/inf passed
    through as prices. A usable price is a real, finite number,
    kept at full precision.

    W-check B3 (adversarial 1): the value must also be POSITIVE —
    a negative 'price' passed the finiteness check, landed in
    listings, and then poisoned bucket minimums (suppressing real
    drops) or fabricated a "…円 → -500円" drop notification."""
    import math
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def api_list(data: dict, key: str, source: str) -> list:
    """An API list field with the FetchError contract kept: present
    but not a list is a PARSE_ERROR, never a raw TypeError."""
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise FetchError("PARSE_ERROR",
                         f"{source} payload field {key!r} is not a list")
    return value


def classify_http(status: int) -> str:
    if status in (401, 403, 429, 451):
        return "BLOCKED"
    if status >= 400:
        return "HTTP_ERROR"
    return "OK"


class FetchError(Exception):
    def __init__(self, result: str, detail: str = ""):
        super().__init__(detail or result)
        self.result = result
        self.detail = detail
        # Raw-hash of a response body received before the failure
        # (W-check B3 evidence wiring): lets the run record retain
        # the failure body without the fetcher re-fetching it.
        self.evidence_hash: str | None = None


# W-check B5 (adversarial QA 中-2): response bodies had NO size
# cap anywhere on the plain path — httpx buffered the whole body,
# so one pathological response from an approved source (a broken
# CDN, a misconfigured redirect target, a plain-text 'XML bomb')
# could exhaust the resident process's memory and take every job
# (digests and deadline alerts included) down with it, once per
# fetch cycle. The gzip path already capped decompression at
# 25MB; the plain body now caps at the same 25MB, enforced WHILE
# STREAMING so the excess is never held in memory at all.
MAX_BODY_BYTES = 25 * 1024 * 1024


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> httpx.Response:
    try:
        with httpx.stream("GET", url, headers=headers or {},
                          timeout=timeout,
                          follow_redirects=True) as resp:
            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    raise FetchError(
                        "RESPONSE_TOO_LARGE",
                        f"response body exceeded {MAX_BODY_BYTES}"
                        f" bytes for {url}")
                chunks.append(chunk)
            body = b"".join(chunks)
            status = resp.status_code
            # iter_bytes already decoded the HTTP representation.
            resp_headers = httpx.Headers(resp.headers)
            for name in ("Content-Encoding", "Content-Length", "Transfer-Encoding"):
                resp_headers.pop(name, None)
    except FetchError:
        raise
    except httpx.TimeoutException as exc:
        raise FetchError("TIMEOUT", str(exc))
    except httpx.HTTPError as exc:
        raise FetchError("NETWORK", str(exc))
    resp = httpx.Response(status, content=body, headers=resp_headers)
    result = classify_http(resp.status_code)
    if result != "OK":
        err = FetchError(result, f"HTTP {resp.status_code} for {url}")
        try:
            from ..evidence import save_raw_evidence
            ev = save_raw_evidence("http", url, resp.text or "",
                                   keep_body=False)
            err.evidence_hash = ev.get("raw_hash")
        except Exception:
            pass
        raise err
    return resp


class BaseFetcher:
    kind = "HTML"
    min_gap_seconds = 2.0

    def __init__(self, db, source_id: int):
        self.db = db
        self.source_id = source_id
        self._last_request_at = 0.0
        self._last_ev: dict | None = None

    def _stash_ev(self, ev: dict) -> dict:
        """Remember the in-flight evidence dict so a parse failure
        later in fetch_items still lets run() retain the body and
        record the hashes (W-check B3). Fetchers call this right
        after save_raw_evidence, before parsing."""
        self._last_ev = ev
        return ev

    def _polite(self):
        gap = self.min_gap_seconds - (time.monotonic() - self._last_request_at)
        if gap > 0:
            time.sleep(gap)
        self._last_request_at = time.monotonic()

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        """Return (items, evidence). Raises FetchError on failure."""
        raise NotImplementedError

    def _record(self, **kwargs) -> str | None:
        """Record a fetch run, returning its EFFECTIVE result.

        Two W-check B2 fixes live here. (1) The health layer may
        rewrite a recorded OK into PARSE_EMPTY (zero-item anomaly);
        the caller-visible result must be the rewritten one, because
        the frequency ladder used to promote a source in the very run
        health flagged SUSPECT (maniac A D-5). (2) Recording must
        never destroy a completed fetch: on a full disk the ledger
        write itself raises, and the items already fetched are worth
        more than the bookkeeping (seller D-5) — the run proceeds and
        only the ledger entry is lost.
        """
        try:
            run_id = health.record_fetch_run(self.db, self.source_id,
                                             **kwargs)
        except Exception:
            try:
                self.db.rollback()
            except Exception:
                pass
            # The ledger entry is lost, but the fetch's own outcome
            # stands: report the intended result so the ladder does
            # not punish the source for a bookkeeping failure.
            return kwargs.get("result")
        try:
            row = self.db.execute(
                "SELECT result FROM fetch_runs WHERE id=?",
                (run_id,)).fetchone()
            return row[0] if row else kwargs.get("result")
        except Exception:
            return kwargs.get("result")

    def run(self, url: str) -> dict:
        """Fetch once, record the run (ledger + health), return a summary."""
        # Blocked shelf (maniac A D-8): a source shelved for repeated
        # BLOCKED results is not fetched at all until its shelf window
        # passes — the shelf used to be write-only.
        try:
            if health.is_shelved(self.db, self.source_id):
                return {"ok": False, "result": "BLOCKED_SHELF",
                        "items": []}
        except Exception:
            pass
        started = clock.now_iso(self.db)
        self._last_ev = None
        try:
            items, ev = self.fetch_items(url)
        except FetchError as exc:
            # W-check B3 (maniac A D-7): a failed fetch retains its
            # response body as evidence, and the fetch_runs row says
            # where it is. The body was registered by http_get or by
            # the fetcher's own save_raw_evidence (stashed) — commit
            # whichever hash is at hand.
            from .. import evidence as _evidence
            raw_hash = exc.evidence_hash or (
                (self._last_ev or {}).get("raw_hash"))
            body_path = _evidence.commit_body_for(raw_hash)
            effective = self._record(
                result=exc.result, started_at=started,
                finished_at=clock.now_iso(self.db),
                error_detail=exc.detail, fetcher_kind=self.kind,
                raw_hash=raw_hash,
                normalized_hash=(self._last_ev or {}).get(
                    "normalized_hash"),
                body_path=body_path)
            self._freq_outcome(effective == "OK")
            return {"ok": False, "result": exc.result, "items": []}
        except Exception as exc:  # parse errors etc.
            from .. import evidence as _evidence
            raw_hash = (self._last_ev or {}).get("raw_hash")
            body_path = _evidence.commit_body_for(raw_hash)
            effective = self._record(
                result="PARSE_ERROR", started_at=started,
                finished_at=clock.now_iso(self.db),
                error_detail=str(exc)[:500], fetcher_kind=self.kind,
                raw_hash=raw_hash,
                normalized_hash=(self._last_ev or {}).get(
                    "normalized_hash"),
                body_path=body_path)
            self._freq_outcome(effective == "OK")
            return {"ok": False, "result": "PARSE_ERROR", "items": []}
        effective = self._record(
            result="OK", items_extracted=len(items), started_at=started,
            finished_at=clock.now_iso(self.db),
            raw_hash=ev.get("raw_hash"),
            normalized_hash=ev.get("normalized_hash"),
            body_path=ev.get("body_path"), fetcher_kind=self.kind)
        self._freq_outcome(effective == "OK")
        # R9 maniac A observation 2: the returned result used to say
        # "OK" even when the recorded (effective) result had been
        # rewritten to PARSE_EMPTY — return what the ledger says.
        return {"ok": True, "result": effective, "items": items,
                "evidence": ev}

    def _freq_outcome(self, ok: bool) -> None:
        """Feed the frequency ladder (agreement-16). Never raises:
        cadence bookkeeping must not break the fetch it follows."""
        try:
            from .. import freqpolicy
            freqpolicy.apply_run_outcome(self.db, self.source_id, ok)
        except Exception:
            pass
