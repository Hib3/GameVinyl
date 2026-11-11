"""Label / store GOODS fetcher (fetcher kind: GOODS).

GOODS are non-music merchandise (apparel, figures, printed matter,
...) sold on a label or store page. They are tracked so they are not
lost, but they are NOT music editions: registered goods land in
candidate_editions with distribution_type='goods' (the lane marker)
and confidence capped at 中, prices never auto-attached.

Review fix (2026-11-08, adversarial QA): agreement-09 states goods
hits go through the review queue like every other candidate
(「すべてレビューキュー経由」) — the first version's docstring
claimed the opposite and registered them queue-free, and no
scheduler job ever ran the fetcher. Goods candidates now enter
with status='pending' plus a candidate_review queue row and the
button notification, exactly like other discovery candidates;
the lane stays distinguishable via distribution_type='goods'.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import clock
from ..evidence import normalized_text_hash, save_raw_evidence
from ..normalize import fingerprint_for
from .base import BaseFetcher, FetchError, http_get

_PRICE_RE = re.compile(r"(?:¥|￥)\s*[\d,]+|[\d,]+\s*円")
_CONTAINER_SELECTOR = "li, article, tr, .item, .product, .goods-item"


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _first_price(container) -> str | None:
    m = _PRICE_RE.search(container.get_text(" ", strip=True))
    return m.group(0) if m else None


_NAV_TEXTS = {"home", "ホーム", "トップ", "トップページ", "ログイン",
              "ログアウト", "カート", "カートを見る", "マイページ",
              "メニュー", "お問い合わせ", "会社概要", "特定商取引法",
              "プライバシーポリシー", "サイトマップ", "検索"}


def _is_nav_anchor(a) -> bool:
    return _clean(a.get_text()).lower() in _NAV_TEXTS


def extract_goods_items(html: str, page_url: str) -> list[dict]:
    """Extract goods entries from a store page's HTML.

    Primary pass: one item per product-like container, using its
    first NON-NAVIGATION anchor with visible text; the price text
    is the first price-looking string inside the same container
    (goods prices are optional page text, never required).
    Fallback pass (only when no container yielded anything): every
    non-navigation anchor with meaningful text.

    Review fix (adversarial QA): a lone navigation <li> used to
    win the primary pass and suppress both the real products and
    the fallback, so pages lost every actual item.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_urls: set[str] = set()

    def add(title: str, raw_title: str, href: str, price_text) -> None:
        url = urljoin(page_url, href)
        if url in seen_urls:
            return
        seen_urls.add(url)
        items.append({"title": title, "url": url,
                      "price_text": price_text, "raw_title": raw_title})

    for container in soup.select(_CONTAINER_SELECTOR):
        anchor = None
        for a in container.find_all("a", href=True):
            if _clean(a.get_text()) and not _is_nav_anchor(a):
                anchor = a
                break
        if anchor is None:
            continue
        add(_clean(anchor.get_text()), anchor.get_text().strip(),
            anchor["href"], _first_price(container))

    if not items:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            title = _clean(a.get_text())
            if len(title) < 2 or _is_nav_anchor(a):
                continue
            add(title, a.get_text().strip(), href, None)
    return items


class GoodsFetcher(BaseFetcher):
    kind = "GOODS"

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        self._polite()
        resp = http_get(url, headers={"User-Agent": "GameRecordTool/1.0"})
        body = resp.text
        if not body or not body.strip():
            raise FetchError("PARSE_ERROR", "empty goods page response")
        ev = save_raw_evidence("goods", url, body, keep_body=False)
        self._stash_ev(ev)
        items = extract_goods_items(body, url)
        ev["normalized_hash"] = normalized_text_hash(body)
        return items, ev

    def register_goods(self, items) -> dict:
        """Record goods items as reviewable candidates in the goods
        lane (distribution_type='goods', status='pending' + a
        candidate_review queue row, per agreement-09).

        Review fix (adversarial QA): the fingerprint carries the
        goods lane marker, so a same-title candidate from another
        lane (e.g. an RSS discovery) no longer swallows the goods
        record — lanes stay separated (agreement-21) while
        duplicates WITHIN the goods lane still collapse. The page's
        price string is kept as raw page text in raw_description
        only; it is never promoted to a structured price.
        """
        row = self.db.execute(
            "SELECT name FROM sources WHERE id=?",
            (self.source_id,)).fetchone()
        found_via = (row["name"] if row and row["name"] else None) or "goods"
        created, duplicates, failed = 0, 0, 0
        for item in items:
            # W-check B4 (adversarial QA): per-item containment, as
            # in the ingest lane — one poisoned item must not stop
            # this lane's registration at the same position forever.
            try:
                c, d = self._register_one(item, found_via)
                created += c
                duplicates += d
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                failed += 1
                try:
                    import json as _json
                    self.db.execute(
                        "INSERT INTO system_events(kind, detail_json,"
                        " created_at) VALUES('candidate_register_failed',"
                        " ?, ?)",
                        (_json.dumps({"lane": "goods",
                                      "error": str(exc)[:200]}),
                         clock.now_iso(self.db)))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
        return {"created": created, "duplicates": duplicates,
                "failed": failed}

    def _register_one(self, item, found_via) -> tuple[int, int]:
        fp = fingerprint_for(item["title"], None, "__goods__")
        now = clock.now_iso(self.db)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO candidate_editions("
            " title, raw_title, raw_description, source_url,"
            " fingerprint, distribution_type,"
            " existence_confidence, price_confidence,"
            " found_at, first_seen, last_seen, found_via, status)"
            " VALUES(?, ?, ?, ?, ?, 'goods', '中', '低', ?, ?, ?, ?,"
            " 'pending')",
            (item["title"], item.get("raw_title") or item["title"],
             item.get("price_text"), item["url"], fp,
             now, now, now, found_via))
        if cur.rowcount == 0:
            from ..candidates import note_candidate_source
            note_candidate_source(self.db, fp, item.get("url"))
            return 0, 1
        cid = cur.lastrowid
        self.db.execute(
            "INSERT INTO review_queue(kind, payload_json,"
            " fingerprint, priority, created_at) VALUES"
            "('candidate_review', ?, ?, 30, ?)",
            (json.dumps({"candidate_id": cid,
                         "source": found_via}), fp, now))
        from .. import outbox as _outbox
        _outbox.enqueue_candidate_review(self.db, cid)
        return 1, 0
