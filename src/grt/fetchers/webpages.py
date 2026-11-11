"""No-API web acquisition (v2.0.2): feeds, sitemaps, structured data.

Most shops and labels publish NO API and NO official feed — the
Japan source survey confirmed this — so the acquisition layer must
work from what every site already exposes:

- RSS/Atom feeds where they exist (generic FeedFetcher; Blip Blop
  has its own specialised subclass upstream);
- XML sitemaps, discovered via robots.txt `Sitemap:` lines or a
  known URL, including sitemap indexes and gzip files (the sitemap
  protocol caps one file at 50,000 URLs / 50MB);
- schema.org JSON-LD Product/Offer blocks embedded in ordinary
  product pages (name / price / currency / availability / GTIN) —
  structured data the store publishes for search engines, readable
  with no key and no JavaScript.

Frameworks evaluated and deliberately NOT embedded in the resident
core (see BUILD_V20_REPORT v2.0.2 addendum): Scrapy (strong batch
crawler, no JS rendering, heavy Twisted stack for a resident
monitor), Playwright/Selenium (real browser rendering for JS-only
pages, but a large browser download and a second runtime to keep
alive on the user's PC), Crawlee (capable, younger Python line).
The resident process stays on httpx + BeautifulSoup + feedparser;
a source that truly needs rendering can be added later behind the
same BaseFetcher contract without touching the core.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import feedparser
from bs4 import BeautifulSoup

from .. import clock
from ..evidence import normalized_text_hash, save_raw_evidence
from ..normalize import fingerprint_for, normalize_currency
from .base import BaseFetcher, FetchError, http_get

MAX_SITEMAP_URLS = 5000
MAX_SITEMAP_CHILDREN = 25
MAX_SITEMAP_GZIP_BYTES = 25_000_000

# Non-product paths that must never become discovery candidates
# when a whole sitemap is registered (review fix, adversarial QA:
# the production job used to candidatise /about and /privacy).
_NON_PRODUCT_SEGMENTS = {
    "about", "privacy", "privacypolicy", "company", "contact",
    "terms", "tos", "faq", "help", "login", "signin", "cart",
    "checkout", "account", "mypage", "recruit", "ir", "news",
    "press", "sitemap", "search", "law", "shipping", "returns",
}


def is_product_like_url(url: str) -> bool:
    """Heuristic gate for sitemap candidate registration: deny
    known non-product sections; everything else passes."""
    from urllib.parse import urlparse
    parts = [p for p in urlparse(url).path.lower().split("/") if p]
    if not parts:
        return False
    return not any(p in _NON_PRODUCT_SEGMENTS for p in parts)


def _clean_price(value) -> float | None:
    """A usable price: a real, finite, POSITIVE number (never a
    bool). W-check B3 (adversarial 1 / wholesaler 9): zero and
    negative 'prices' used to pass the finiteness check and then
    poison every minimum they touched."""
    import math
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        # R14 (maniac A): shop templates write prices as formatted
        # strings ("12,800", "¥12,800") at least as often as JSON
        # numbers; float() alone discarded every one of them.
        value = value.replace(",", "").replace("¥", "") \
            .replace("￥", "").strip()
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


# ------------------------------------------------------------------
# Pure parsers (unit-tested without network)
# ------------------------------------------------------------------

def discover_sitemaps_from_robots(robots_text: str) -> list[str]:
    out = []
    for line in (robots_text or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap" and value.strip():
            out.append(value.strip())
    return out


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(elem, name: str) -> str | None:
    for child in elem:
        if _localname(child.tag) == name:
            return child.text
    return None


def _decompress_limited(data: bytes) -> bytes:
    """gzip expansion with a hard size cap (review fix: a 19KB
    bomb expanded to 20MB unchecked; larger ones could exhaust
    memory on the user's PC)."""
    import io
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > MAX_SITEMAP_GZIP_BYTES:
                raise ValueError("sitemap gzip exceeds size cap")
    return bytes(out)


def parse_sitemap_bytes(data: bytes) -> tuple[list[dict], list[str]]:
    """Parse one sitemap file. Returns (url entries, child sitemaps).

    Namespace-agnostic (review fix): many real sitemaps omit the
    standard namespace, and those used to parse as 'success, 0
    entries' — every URL silently missed."""
    if data[:2] == b"\x1f\x8b":
        data = _decompress_limited(data)
    root = ET.fromstring(data)
    entries: list[dict] = []
    children: list[str] = []
    if _localname(root.tag) == "sitemapindex":
        for sm in root.iter():
            if _localname(sm.tag) == "sitemap":
                loc = _child_text(sm, "loc")
                if loc:
                    children.append(loc.strip())
    else:
        for u in root.iter():
            if _localname(u.tag) == "url":
                loc = _child_text(u, "loc")
                if loc:
                    entries.append(
                        {"url": loc.strip(),
                         "lastmod": _child_text(u, "lastmod")})
    return entries, children


def _find_products(data) -> list[dict]:
    """Every dict whose @type includes Product, wherever it nests
    (@graph, mainEntity, itemListElement, ...). Review fix: the
    first version only looked at top-level nodes and @graph, so the
    most common real-world nestings were missed entirely."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            types = node.get("@type")
            if isinstance(types, str):
                types = [types]
            if types and "Product" in types:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _best_offer(offers) -> dict:
    """First offer that actually carries a price (review fix: an
    offers list whose first entry is priceless used to discard the
    priced ones). AggregateOffer falls back to lowPrice."""
    if isinstance(offers, dict):
        candidates = [offers]
    elif isinstance(offers, list):
        candidates = [o for o in offers if isinstance(o, dict)]
    else:
        return {}
    for offer in candidates:
        if _clean_price(offer.get("price")) is not None:
            return offer
    for offer in candidates:
        # R14 (maniac A): the price often lives one level down, in
        # priceSpecification (schema.org's other standard place).
        spec = offer.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec]
        for s in specs:
            if isinstance(s, dict) \
                    and _clean_price(s.get("price")) is not None:
                merged = dict(offer)
                merged["price"] = s.get("price")
                if not merged.get("priceCurrency"):
                    merged["priceCurrency"] = s.get("priceCurrency")
                return merged
    for offer in candidates:
        if _clean_price(offer.get("lowPrice")) is not None:
            merged = dict(offer)
            merged["price"] = offer.get("lowPrice")
            return merged
    return candidates[0] if candidates else {}


def extract_jsonld_products(html: str, page_url: str) -> list[dict]:
    """schema.org Product blocks -> listing-shaped items."""
    soup = BeautifulSoup(html or "", "html.parser")
    items: list[dict] = []
    for script in soup.find_all("script",
                                attrs={"type": "application/ld+json"}):
        raw = (script.string or "").strip()
        # R14 (maniac A): older shop templates wrap the JSON in an
        # HTML comment (<!-- {...} -->); json.loads failed on the
        # markers and the whole block was silently skipped.
        if raw.startswith("<!--"):
            raw = raw[4:]
        if raw.endswith("-->"):
            raw = raw[:-3]
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in _find_products(data):
            offer = _best_offer(node.get("offers"))
            gtin = _extract_gtin(node)
            item = {
                "title": node.get("name") or "",
                "raw_title": node.get("name") or "",
                "url": offer.get("url") or page_url,
                "price": _clean_price(offer.get("price")),
                # R14 (maniac A): currency codes are normalised at
                # the boundary like everywhere else ('jpy' stored
                # raw silently broke every yen comparison).
                "currency": normalize_currency(
                    offer.get("priceCurrency")),
                "availability": offer.get("availability"),
                "gtin": gtin,
                # The matcher reads barcode/barcode_raw (agreement-20
                # gate): expose the GTIN under that key too, or the
                # extracted barcode never reaches the gate.
                "barcode": gtin,
                # The GTIN comes from the page's own structured
                # markup: barcode source tier 'structured' (tier 1),
                # which is what lets match_listing derive the P2
                # barcode_tier at all (W-check B3, AI-engineer 2c).
                "barcode_source_tier": "structured",
                "sku": node.get("sku") or node.get("mpn"),
            }
            # schema.org itemCondition feeds the condition ladder
            # (W-check B3, seller D2): only the two unambiguous
            # mappings are made — NewCondition is the ladder's
            # '新品', DamagedCondition its '破損'. A generic
            # UsedCondition names no grade and is left unset rather
            # than assigned an invented one.
            cond = offer.get("itemCondition") \
                or node.get("itemCondition") or ""
            if str(cond).endswith("NewCondition"):
                item["media_cond"] = "新品"
            elif str(cond).endswith("DamagedCondition"):
                item["media_cond"] = "破損"
            items.append(item)
    return [i for i in items if i["title"]]


def _extract_gtin(node: dict):
    """The product's GTIN from a JSON-LD node (W-check B3, maniac A
    D-9): gtin12 (UPC) used to be ignored entirely, and gtin14 was
    passed through raw although the check-digit gate only accepts
    12/13 digits — a GTIN-14 with a leading 0 wraps a GTIN-13,
    which is recovered when it is itself check-valid."""
    from ..normalize import barcode_check_digit_ok, normalize_barcode
    # R14 (maniac A): the first present key used to win UNVALIDATED,
    # so a corrupt gtin13 permanently shadowed a valid gtin12 /
    # recoverable gtin14 on the same page — the barcode could never
    # pass the matcher's check-digit gate. The first VALID value
    # wins; when nothing on the page is check-valid, the first
    # present value is still returned (the matcher's gate will
    # simply never pass it, exactly as before this change).
    first_raw = None
    for key in ("gtin13", "gtin", "gtin12"):
        if node.get(key):
            digits = normalize_barcode(str(node[key]))
            if first_raw is None:
                first_raw = digits
            if digits and barcode_check_digit_ok(digits):
                return digits
    gtin14 = node.get("gtin14")
    if gtin14:
        digits = normalize_barcode(str(gtin14))
        if len(digits) == 14 and digits.startswith("0") \
                and barcode_check_digit_ok(digits[1:]):
            return digits[1:]
        # A non-recoverable GTIN-14 names a case/pack level, not
        # the retail unit, and the check-digit gate accepts only
        # 12/13 digits — passing it through raw would store a
        # barcode that can never match anything.
    return first_raw


# ------------------------------------------------------------------
# Fetchers
# ------------------------------------------------------------------

class FeedFetcher(BaseFetcher):
    """Generic RSS/Atom fetcher for any approved feed source."""
    kind = "RSS"

    def initial_scan_state(self):
        """Persist the first-scan cutoff before fetching so retries stay quiet."""
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS feed_initial_scans ("
            " source_id INTEGER PRIMARY KEY, cutoff TEXT NOT NULL,"
            " completed INTEGER NOT NULL DEFAULT 0)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS feed_backfill_items ("
            " source_id INTEGER NOT NULL, item_key TEXT NOT NULL,"
            " title TEXT, url TEXT, published TEXT, classification TEXT NOT NULL,"
            " PRIMARY KEY(source_id,item_key))")
        state = self.db.execute(
            "SELECT cutoff,completed FROM feed_initial_scans WHERE source_id=?",
            (self.source_id,)).fetchone()
        if state is None:
            prior = self.db.execute(
                "SELECT 1 FROM fetch_runs WHERE source_id=? AND result='OK' LIMIT 1",
                (self.source_id,)).fetchone()
            cutoff = clock.to_iso(clock.now(self.db) - timedelta(hours=24))
            self.db.execute(
                "INSERT OR IGNORE INTO feed_initial_scans(source_id,cutoff,completed)"
                " VALUES(?,?,?)", (self.source_id, cutoff, int(prior is not None)))
            self.db.commit()
            state = self.db.execute(
                "SELECT cutoff,completed FROM feed_initial_scans WHERE source_id=?",
                (self.source_id,)).fetchone()
        return None if state[1] else state[0]

    def finish_initial_scan(self):
        self.db.execute("UPDATE feed_initial_scans SET completed=1 WHERE source_id=?",
                        (self.source_id,))
        self.db.commit()

    def _ensure_item_observation_schema(self):
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS feed_item_observations ("
            " source_id INTEGER NOT NULL, item_key TEXT NOT NULL,"
            " candidate_fingerprint TEXT NOT NULL, title TEXT NOT NULL,"
            " item_url TEXT, publisher_published_raw TEXT,"
            " publisher_published_at TEXT, publisher_updated_raw TEXT,"
            " publisher_updated_at TEXT, first_seen_at TEXT NOT NULL,"
            " last_seen_at TEXT NOT NULL, PRIMARY KEY(source_id,item_key))")
        self.db.commit()

    @staticmethod
    def _publisher_timestamp(raw):
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            dt = parsedate_to_datetime(raw)
            if dt is None:
                raise ValueError("invalid published date")
        except (ValueError, TypeError, OverflowError):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (ValueError, TypeError, OverflowError):
                return None
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return clock.to_iso(dt)

    def _save_item_observation(self, item):
        identity = next((v.strip() for v in
                         (item.get("id"), item.get("guid"), item.get("url"))
                         if isinstance(v, str) and v.strip()), None)
        if identity is None:
            # shortcut: feeds without ID or link can merge equal titles; upgrade when the publisher supplies a stable item ID.
            identity = json.dumps([item.get("title") or "",
                                   item.get("summary") or ""],
                                  ensure_ascii=True)
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        published = item.get("published")
        updated = item.get("updated")
        now = clock.now_iso(self.db)
        self.db.execute(
            "INSERT INTO feed_item_observations(source_id,item_key,"
            "candidate_fingerprint,title,item_url,publisher_published_raw,"
            "publisher_published_at,publisher_updated_raw,"
            "publisher_updated_at,first_seen_at,last_seen_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id,item_key) DO UPDATE SET"
            " candidate_fingerprint=excluded.candidate_fingerprint,"
            " title=excluded.title,item_url=excluded.item_url,"
            " publisher_published_raw=coalesce(nullif(excluded.publisher_published_raw,''),publisher_published_raw),"
            " publisher_published_at=coalesce(excluded.publisher_published_at,publisher_published_at),"
            " publisher_updated_raw=coalesce(nullif(excluded.publisher_updated_raw,''),publisher_updated_raw),"
            " publisher_updated_at=coalesce(excluded.publisher_updated_at,publisher_updated_at),"
            " last_seen_at=excluded.last_seen_at",
            (self.source_id,key,fingerprint_for(item["title"],None),
             str(item["title"]).strip()[:240],str(item.get("url") or "")[:2000] or None,
             str(published or "")[:250] or None,self._publisher_timestamp(published),
             str(updated or "")[:250] or None,self._publisher_timestamp(updated),
             now,now))

    def _first_scan_notify(self, item, cutoff):
        normalized = self._publisher_timestamp(item.get("published"))
        if normalized is None:
            return False
        dt = clock.parse_iso(normalized)
        now = clock.now(self.db)
        threshold = max(clock.parse_iso(cutoff), now - timedelta(hours=24))
        return threshold <= dt <= now

    def _save_backfill(self, item):
        title = item.get("title") or ""
        url = item.get("url") or ""
        published = item.get("published") or ""
        key = hashlib.sha256(json.dumps([title,url,published],
                                       ensure_ascii=True).encode("ascii")).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO feed_backfill_items"
            "(source_id,item_key,title,url,published,classification)"
            " VALUES(?,?,?,?,?,'needs_review')",
            (self.source_id,key,title,url,published))

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        self._polite()
        resp = http_get(url, headers={"User-Agent": "GameRecordTool/2.0"})
        body = resp.text
        if not body or not body.strip():
            raise FetchError("PARSE_ERROR", "empty feed response")
        ev = save_raw_evidence("feed", url, body, keep_body=False)
        self._stash_ev(ev)
        parsed = feedparser.parse(body)
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            raise FetchError("PARSE_ERROR", "feed parse failed")
        items = [{"title": e.get("title", ""), "url": e.get("link", ""),
                  "raw_title": e.get("title", ""),
                  "summary": e.get("summary", ""),
                  "id": e.get("id", ""),
                  "published": e.get("published", ""),
                  "updated": e.get("updated", "")}
                 for e in parsed.entries]
        # Review fix (adversarial QA): a truncated feed used to
        # yield ONE ghost item with empty title/url and be recorded
        # as a successful 1-item run. Empty shells are dropped, and
        # a feed whose entries are all shells is a parse failure.
        items = [i for i in items if i["title"] or i["url"]]
        if parsed.entries and not items:
            raise FetchError("PARSE_ERROR",
                             "feed entries carried no title or link")
        ev["normalized_hash"] = normalized_text_hash(body)
        return items, ev

    def register_candidates(self, items, *, found_via: str, initial_cutoff=None) -> dict:
        self._ensure_item_observation_schema()
        created, failed = 0, 0
        for item in items:
            # W-check B4 (adversarial QA): per-item containment, as
            # in the ingest lane — one poisoned entry must not stop
            # the lane at the same position every cycle.
            # R19 (adversarial QA): the title pre-check sat OUTSIDE
            # the try, so a single non-dict item raised
            # AttributeError and ended the whole lane — the one
            # lane of four where containment did not hold.
            try:
                if not isinstance(item, dict) \
                        or not item.get("title"):
                    if not isinstance(item, dict):
                        raise TypeError("feed item is not an object")
                    continue
                self._save_item_observation(item)
                notify = initial_cutoff is None or self._first_scan_notify(item, initial_cutoff)
                if not notify:
                    self._save_backfill(item)
                created += self._register_one(item, found_via, notify=notify)
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                failed += 1
                try:
                    self.db.execute(
                        "INSERT INTO system_events(kind, detail_json,"
                        " created_at) VALUES('candidate_register_failed',"
                        " ?, ?)",
                        (json.dumps({"lane": "feed",
                                     "error": str(exc)[:200]}),
                         clock.now_iso(self.db)))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
        return {"created": created, "failed": failed}

    def _register_one(self, item, found_via: str, *, notify=True) -> int:
        fp = fingerprint_for(item["title"], None)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO candidate_editions("
            " title, raw_title, raw_description, source_url,"
            " fingerprint, existence_confidence, price_confidence,"
            " found_at, first_seen, last_seen, found_via, status)"
            " VALUES(?, ?, ?, ?, ?, '中', '低', ?, ?, ?, ?, 'pending')",
            (item["title"], item["title"], item.get("summary"),
             item.get("url"), fp, clock.now_iso(self.db),
             clock.now_iso(self.db), clock.now_iso(self.db),
             found_via))
        if not cur.rowcount:
            if not notify:
                return 0
            # Re-discovery: aggregate the source URL onto the
            # existing candidate (spec C-5.2) instead of
            # discarding it.
            from ..candidates import note_candidate_source
            note_candidate_source(self.db, fp, item.get("url"))
            return 0
        cid = cur.lastrowid
        self.db.execute(
            "INSERT INTO review_queue(kind, payload_json,"
            " fingerprint, priority, created_at) VALUES"
            "('candidate_review', ?, ?, 30, ?)",
            (json.dumps({"candidate_id": cid,
                         "source": found_via, "notify": notify}), fp,
             clock.now_iso(self.db)))
        # Same button-carrying notification the Blip Blop
        # path sends, so feed/sitemap candidates are visible
        # without polling /queue (review follow-up).
        from .. import outbox as _outbox
        if notify:
            _outbox.enqueue_candidate_review(self.db, cid)
        return 1


class SitemapFetcher(BaseFetcher):
    """Sitemap walker: urlset + one index level, substring filter."""
    kind = "HTML"

    def __init__(self, db, source_id: int, filters: list[str] | None = None,
                 max_urls: int = MAX_SITEMAP_URLS):
        super().__init__(db, source_id)
        self.filters = [f.lower() for f in (filters or [])]
        self.max_urls = max_urls

    def _fetch_doc(self, url: str) -> tuple[list[dict], list[str]]:
        resp = http_get(url, headers={"User-Agent": "GameRecordTool/2.0"})
        return parse_sitemap_bytes(resp.content)

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        self._polite()
        entries, children = self._fetch_doc(url)
        # Review fixes (adversarial QA): child sitemaps are deduped
        # and capped (a self-referencing index listed 30 times used
        # to fetch 31 documents), and ONE broken child (XML parse
        # error, bad gzip) no longer discards the healthy children
        # fetched before it — any exception skips just that child.
        seen_children: set[str] = set()
        for child in children:
            if len(entries) >= self.max_urls:
                break
            if child in seen_children:
                continue
            seen_children.add(child)
            if len(seen_children) > MAX_SITEMAP_CHILDREN:
                break
            try:
                # W-check B2 (maniac A D-6): child documents used to
                # be fetched back-to-back with NO polite gap — an
                # index plus 25 children meant 26 requests to one
                # (often small) host at full speed, contradicting the
                # fetcher layer's own 'low-rate sequential' design.
                self._polite()
                more, _ = self._fetch_doc(child)
            except Exception:
                continue
            entries.extend(more)
        items = []
        for e in entries[: self.max_urls]:
            if self.filters and not any(f in e["url"].lower()
                                        for f in self.filters):
                continue
            items.append({"title": e["url"], "raw_title": e["url"],
                          "url": e["url"], "lastmod": e.get("lastmod")})
        # W-check B4 (wholesaler): sitemap items key on their URL
        # string, so the same page as http/https, www/no-www, or
        # with a trailing slash became up to four candidates. The
        # identity form is canonicalised at extraction; every
        # downstream consumer (the main job's known-fingerprint
        # skip, registration) then agrees on one candidate.
        from ..normalize import page_identity_url
        for it in items:
            ident = page_identity_url(it["url"])
            it["url"] = ident
            it["title"] = ident
            it["raw_title"] = ident
        # R19 (wholesaler): identity dedupe happens HERE too — the
        # normalised list used to keep every variant, so one page
        # in three spellings consumed three of the main job's 50
        # fresh slots (registration collapsed them to one anyway).
        deduped: list[dict] = []
        seen_idents: set[str] = set()
        for it in items:
            if it["url"] in seen_idents:
                continue
            seen_idents.add(it["url"])
            deduped.append(it)
        items = deduped
        # A sitemap walk has no single response body; the synthetic
        # digest is a change marker, recorded as the normalized hash.
        ev = {"normalized_hash": normalized_text_hash(
            url + str(len(items)))}
        return items, ev


class StructuredFetcher(BaseFetcher):
    """Product page -> schema.org JSON-LD items (no key, no JS)."""
    kind = "HTML"

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        self._polite()
        resp = http_get(url, headers={"User-Agent": "GameRecordTool/2.0"})
        ev = save_raw_evidence("structured", url, resp.text,
                               keep_body=False)
        self._stash_ev(ev)
        items = extract_jsonld_products(resp.text, url)
        ev["normalized_hash"] = normalized_text_hash(resp.text)
        return items, ev
