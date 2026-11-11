"""INGEST fetcher (agreement-12): whole-list local matching.

Query-mode shops are asked one title at a time. Ingest-mode shops
hand over their entire new-arrivals / stock list page; EVERY item
on it is turned into a listing row and run through the local
matcher (matcher.match_listing). This catches items nobody thought
to query for — the point of the conversion.

Review fixes (2026-11-08, used-seller persona):
- Extraction is CONTAINER-based: a product is a container element
  (li/article/tr/.item/.product/.goods-item); the title is its
  first titled anchor and the price is the first price INSIDE THE
  SAME container. The first version searched the anchor's parent
  text, which pasted a neighbouring product's price — or a
  free-shipping banner's threshold — onto items, and turned
  navigation anchors ("view cart") into products.
- Match results are WRITTEN BACK: a unique top match with
  existence confidence 高 sets listings.edition_id (and variant_id
  when the edition has exactly one variant). Before this, match
  decisions lived only in match_decisions and every downstream
  Phase 2 feature (bundling, price drops) silently saw edition_id
  NULL forever.
- cond_rank gets a writer: a deterministic grade ladder applied to
  the item's media/sleeve condition text when a source provides it.
- A relisted item REVIVES: status back to active, absent streak
  reset, total_jpy refreshed — a returned copy must not stay
  invisible as 'ended' with a stale total.
- Price-drop detection is wired in: bucket comparison for new
  listings, same-copy comparison for price changes (pricedrop).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import clock, matcher, pricedrop
from .base import BaseFetcher

_PRICE_RE = re.compile(r"[¥￥]\s*([0-9][0-9,]*)")
_CONTAINER_SEL = "li, article, tr, .item, .product, .goods-item"
_NAV_TEXTS = {"カートを見る", "カートに入れる", "詳細を見る", "詳しく見る",
              "一覧を見る", "もっと見る", "次へ", "前へ", "トップへ"}

# Price-context markers (W-check B3, seller D5): a container can
# hold several ¥ amounts — the list price, a coupon value, a
# shipping fee — and the FIRST one is not necessarily the selling
# price. Amounts are classified by the text hugging them: shipping
# and coupon amounts are not prices at all; a list-price amount is
# evidence of the MSRP, not of what the copy costs.
_LIST_PRICE_MARKERS = ("定価", "通常価格", "希望小売価格", "メーカー価格")
_COUPON_MARKERS = ("クーポン",)
_SHIPPING_MARKERS = ("送料",)
_SALE_MARKERS = ("販売価格", "特価", "セール価格", "税込価格", "価格")
_SOLDOUT_MARKERS = ("売り切れ", "SOLD OUT", "在庫なし", "在庫切れ",
                    "販売終了")


def _extract_price(text: str) -> float | None:
    """The selling price inside one product container, or None.

    Deterministic priority: an amount tagged as a sale price wins;
    otherwise the first untagged amount; a container whose only
    amounts are list prices / coupons / shipping has NO observable
    selling price and yields None (the item is skipped, exactly
    like a container with no ¥ at all)."""
    sale = plain = None
    for m in _PRICE_RE.finditer(text):
        # The context window hugs the amount (10 before / 10 after):
        # a marker labels the amount it stands next to. A wider
        # window let the SHIPPING amount's label bleed onto the
        # selling price after it ("送料 ¥500 本体 ¥9,800" lost its
        # price entirely). "送料無料" (free shipping) is a badge,
        # not a fee label, and is stripped before classification —
        # "送料無料 ¥9,800" is the commonest price layout in
        # Japanese shops and must keep its price.
        before = text[max(0, m.start() - 10):m.start()]
        after = text[m.end():m.end() + 10]
        # 送料込み (shipping included in THIS price) and 送料別
        # (shipping charged separately) annotate the price itself —
        # they are not fee amounts (R14, AI-engineer F4: both used
        # to discard the selling price entirely). Strip them like
        # 送料無料 before classifying.
        ctx = (before + "|" + after)
        for phrase in ("送料無料", "送料込み", "送料別"):
            ctx = ctx.replace(phrase, "")
        value = float(m.group(1).replace(",", ""))
        # R14 (adversarial 中-2 / 軽微-4): a non-finite or
        # non-positive "price" is not a price. A 309+ digit string
        # saturates float() to inf, which then poisoned a bucket
        # baseline and fabricated a drop notice; ¥0 is a placeholder,
        # consistent with _clean_price's rule in webpages.
        import math
        if not math.isfinite(value) or value <= 0:
            continue
        if any(k in ctx for k in _COUPON_MARKERS):
            continue  # the coupon's own amount, not a price
        if "割引価格" in ctx:
            # A discounted PRICE is a sale price (R14 F4); a bare
            # 割引 next to an amount is the discount amount itself.
            if sale is None:
                sale = value
            continue
        if "割引" in ctx:
            continue
        if any(k in ctx for k in _SHIPPING_MARKERS):
            continue
        if any(k in ctx for k in _LIST_PRICE_MARKERS):
            continue
        if any(k in ctx for k in _SALE_MARKERS):
            if sale is None:
                sale = value
        elif plain is None:
            plain = value
    return sale if sale is not None else plain


_INSTOCK_MARKERS = ("在庫あり", "在庫有", "販売中", "購入できます",
                    "購入可能")


def _is_soldout(text: str) -> bool:
    t = (text or "").upper()
    # R14 (seller): the check was range-blind — a variant table's
    # "カラー違いは在庫なし" (the OTHER colour is out of stock)
    # marked the whole container sold out, and the absence
    # machinery then ended a listing that was in stock all along.
    # An explicit in-stock statement for the product outweighs a
    # sold-out marker elsewhere in the container.
    if any(k.upper() in t for k in _INSTOCK_MARKERS):
        return False
    return any(k.upper() in t for k in _SOLDOUT_MARKERS)


def _canonical_url(u: str) -> str:
    """Listing identity URL (W-check B3, seller D10): the same
    product linked as /p/1 and /p/1?from=new used to register as
    two listings of one copy. The implementation lives in
    normalize.canonical_url (R14) so the absence machinery in
    evidence.py compares exactly the form register writes."""
    from ..normalize import canonical_url
    return canonical_url(u)


# Condition text extraction from a container (W-check B3, seller
# D2): the grade ladder had NO producer — no fetcher ever set
# media_cond / sleeve_cond, so every ingest listing was cond NULL
# and the pricedrop / bundling lanes were dead. Japanese shop
# containers commonly carry 盤：… / ジャケット：… / 状態：… fields;
# those are extracted deterministically here. Unrecognised shapes
# simply stay unset.
_COND_PATTERNS = (
    ("media_cond",
     re.compile(r"(?:盤|メディア|ディスク)[:：]?\s*([^\s、,／/（(]{1,10})")),
    ("sleeve_cond",
     re.compile(r"(?:ジャケット|スリーブ|ケース)[:：]?\s*([^\s、,／/（(]{1,10})")),
    ("media_cond",
     re.compile(r"状態[:：]\s*([^\s、,／/（(]{1,10})")),
)


# Captures that are not conditions at all (R14, seller): the key
# nouns appear inside ordinary product prose — ディスク組 (a disc
# SET), ボーナスディスク付 (WITH a bonus disc), 帯付/帯なし (obi
# notes in the sleeve slot), ジャケットに… (prose where the particle
# に follows the key). Treating those captures as a "provided but
# ungradeable" side discarded the whole pair's cond; they are
# extraction noise, not condition text.
_COND_JUNK_VALUES = {"組", "付", "付き", "枚"}


def _extract_conditions(text: str) -> dict:
    out: dict[str, str] = {}
    for key, pat in _COND_PATTERNS:
        if key in out:
            continue
        for m in pat.finditer(text or ""):
            value = m.group(1)
            if value in _COND_JUNK_VALUES \
                    or value.startswith(("に", "帯")):
                continue
            out[key] = value
            break
    return out


# Deterministic condition ladder (worst of media/sleeve wins).
_GRADE_LADDER = [
    (0, ("ジャンク", "poor", "破損")),
    (1, ("難あり", "fair")),
    (2, ("可", "good")),
    (3, ("並", "良品", "very good", "vg")),
    (4, ("良好", "美品", "excellent", "ex", "nm")),
    (5, ("極上", "ほぼ新品", "near mint")),
    (6, ("未開封", "未使用", "新品", "mint")),
]

_NEGATION_RE = r"[^\s、]{0,3}?(?:ではありません|ではない|じゃありません|でない)"


def _grade_of(text: str) -> int | None:
    """Grade ONE condition text, or None when unrecognised.

    W-check B3 (seller D3): the ladder used to be a bare substring
    scan, blind to negation and to one-character collisions —
    '未開封ではありません' (NOT sealed) graded as mint, 'ジャンク品
    ではありません' graded as junk, the 並 of 並行輸入 (parallel
    import) graded as 'average', and the 可 of 不可 (not accepted)
    graded as 'good'. Negated mentions are stripped first (a grade
    word followed within a few characters by a negation suffix),
    並行 phrasing is removed before grading, and bare 可 counts
    only as a standalone token (after a separator / at a boundary),
    never inside 不可・可能.
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    t = t.replace("並行輸入", "").replace("並行", "")
    for _value, words in _GRADE_LADDER:
        for w in words:
            t = re.sub(re.escape(w) + _NEGATION_RE, "", t)
    # R14 (seller): 新品同様 ("like new") is the top USED grade —
    # an opened, used copy. The bare substring 新品 used to grade
    # it 6, level with a sealed copy, mixing it into the sealed
    # bucket. It is rank 5 (checked after negation stripping, so
    # 新品同様ではありません still grades None).
    if "新品同様" in t:
        return 5
    for value, words in _GRADE_LADDER:
        for w in words:
            if w == "可":
                if re.search(r"(?:^|[：:、\s])可(?:$|[、\s）)])", t):
                    return value
            elif w in t:
                return value
    return None


def cond_rank_for(media_cond: str | None,
                  sleeve_cond: str | None) -> int | None:
    """Map condition text to a rank, or None when unrecognised.

    Deliberately conservative: when a PROVIDED side's text is
    unrecognised the pair is None — the unknown side could be the
    worse one, and the ladder's rule is 'worst of the two wins',
    which is undefined over an unknown. (W-check B3: the seller
    flagged that this discards the recognised side's grade; that is
    the intended fail-safe, kept and recorded rather than silently
    changed.)"""
    ranks = []
    for text in (media_cond, sleeve_cond):
        if not text:
            continue
        rank = _grade_of(text)
        if rank is None:
            return None
        ranks.append(rank)
    return min(ranks) if ranks else None


class IngestFetcher(BaseFetcher):
    kind = "INGEST"

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        from .base import http_get
        from ..evidence import normalized_text_hash, save_raw_evidence
        resp = http_get(url)
        ev = save_raw_evidence("ingest", url, resp.text, keep_body=False)
        self._stash_ev(ev)
        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[dict] = []
        seen = set()
        for box in soup.select(_CONTAINER_SEL):
            anchor = None
            for a in box.find_all("a", href=True):
                text = (a.get_text() or "").strip()
                if text and text not in _NAV_TEXTS:
                    anchor = (a, text)
                    break
            if anchor is None:
                continue
            a, title = anchor
            href = _canonical_url(urljoin(url, a["href"]))
            if href in seen:
                continue
            box_text = box.get_text(" ", strip=True)
            # W-check B3 (seller D6): a sold-out container is not a
            # buyable listing — it used to register as 'active'
            # whenever it carried a price.
            if _is_soldout(box_text):
                continue
            price = _extract_price(box_text)
            if price is None:
                continue  # no selling price in this container
            seen.add(href)
            item = {
                "title": title, "raw_title": title, "url": href,
                "price": price,
                "currency": "JPY",
            }
            item.update(_extract_conditions(box_text))
            items.append(item)
        # W-check B3 (maniac A D-7): raw_hash keeps the RAW hash
        # (the evidence file's name derives from it); the normalized
        # page-diff hash travels under its own key.
        ev["normalized_hash"] = normalized_text_hash(resp.text)
        return items, ev

    def register_ingested(self, source_name: str,
                          items: list[dict]) -> dict:
        """Insert every item as a listing, match it, wire the results.

        W-check B3 (adversarial 3 / AI-engineer 5): each item is
        contained — one poisoned item (an unparseable year, a dirty
        identifier) used to raise out of the matcher and abort the
        whole page batch at the same position on EVERY run, so the
        rest of the page's items were never ingested at all. A
        failed item is rolled back, recorded, and skipped; the
        batch proceeds.
        """
        matched = created = errors = skipped = 0
        for item in items:
            try:
                outcome = self._register_one(source_name, item)
            except Exception as exc:
                errors += 1
                try:
                    self.db.rollback()
                except Exception:
                    pass
                try:
                    import json as _json
                    self.db.execute(
                        "INSERT INTO system_events(kind, detail_json,"
                        " created_at) VALUES('ingest_item_error', ?, ?)",
                        (_json.dumps(
                            {"source": source_name,
                             "url": item.get("url"),
                             "error": str(exc)[:300]},
                            ensure_ascii=False),
                         clock.now_iso(self.db)))
                    self.db.commit()
                except Exception:
                    try:
                        self.db.rollback()
                    except Exception:
                        pass
                continue
            created += outcome.get("created", 0)
            matched += outcome.get("matched", 0)
            skipped += outcome.get("skipped", 0)
        return {"created": created, "matched": matched,
                "errors": errors, "skipped": skipped,
                "total": len(items)}

    def _register_one(self, source_name: str, item: dict) -> dict:
        item = dict(item)
        item["url"] = _canonical_url(item.get("url") or "")
        # Structured availability (W-check B3, seller D6 sibling):
        # an item the shop itself marks out of stock is not an
        # active listing. A known listing degrades to sold_candidate
        # (the absence machinery's first step); a new one is not
        # created at all.
        availability = str(item.get("availability") or "")
        out_of_stock = availability.endswith(
            ("OutOfStock", "SoldOut", "Discontinued"))
        existing = self.db.execute(
            "SELECT * FROM listings WHERE source=? AND url=?",
            (source_name, item["url"])).fetchone()
        if existing is None:
            # R14 (seller): rows registered before URL
            # canonicalisation carry tracking parameters; the exact
            # lookup misses them and the row would duplicate (and
            # the absence machinery would later call the legacy row
            # a ghost). Fall back to a canonical comparison over the
            # source's rows sharing the URL prefix, and heal the
            # stored URL to the canonical form while here.
            for cand in self.db.execute(
                    "SELECT * FROM listings WHERE source=? AND"
                    " url LIKE ?", (source_name, item["url"] + "%")
            ).fetchall():
                if _canonical_url(cand["url"] or "") == item["url"]:
                    existing = cand
                    try:
                        self.db.execute(
                            "UPDATE listings SET url=? WHERE id=?",
                            (item["url"], cand["id"]))
                    except Exception:
                        pass  # canonical twin exists; keep legacy
                    break
        old_yen = None
        prev_status = None
        created = matched = skipped = 0
        if existing:
            listing_id = existing["id"]
            prev_status = existing["status"]
            from ..normalize import normalize_currency
            from ..scoring import yen_value
            old_yen = yen_value(existing["total_jpy"],
                                existing["price"],
                                existing["currency"])
            # Currency and total move together (B3, maniac A D-4),
            # normalised (R14, maniac A: 'jpy'/'JPY ' stored raw
            # silently broke every yen comparison downstream).
            item_cur = normalize_currency(item.get("currency"))
            new_currency = item_cur or normalize_currency(
                existing["currency"])
            if item.get("price") is None:
                # R14 (maniac A): a price-less re-observation (the
                # page's price failed to extract this time) must not
                # ERASE the observed price evidence.
                new_price = existing["price"]
                new_total = existing["total_jpy"]
                if item_cur is None:
                    new_currency = normalize_currency(
                        existing["currency"])
            elif new_currency == "JPY":
                # R14 (AI-engineer F5): the invariant is "JPY row ⇒
                # total_jpy = current price" — it also holds when
                # the currency was omitted this time and inherited,
                # not only when it was restated.
                new_price = item["price"]
                new_total = item["price"]
            elif item_cur:
                new_price = item["price"]
                new_total = None  # known foreign: no yen total
            else:
                new_price = item["price"]
                new_total = existing["total_jpy"]
            # R19 (seller): a shop-DECLARED out-of-stock sighting
            # is a not-buyable OBSERVATION, not a fresh sighting —
            # it used to reset absent_streak=0, so an alternating
            # sold-display / page-absent pattern never reached
            # 'ended' and the eventual return never signalled; and
            # a declared sold_out -> in_stock transition was
            # suppressed as a 'flap' although the shop itself had
            # declared both states. Declared OOS now advances the
            # streak like an absence (ended at 2), and sets
            # oos_declared so the return can tell a declared
            # transition from an inferred one-round absence (which
            # stays unarmed, per the B4 flap rule).
            if out_of_stock:
                new_streak = int(existing["absent_streak"] or 0) + 1
                new_status = "ended" if new_streak >= 2 \
                    else "sold_candidate"
                new_oos = 1
            else:
                new_streak = 0
                new_status = "active"
                new_oos = 0
            self.db.execute(
                "UPDATE listings SET last_seen=?, price=?,"
                " total_jpy=?, currency=?, status=?, absent_streak=?,"
                " oos_declared=?"
                " WHERE id=?",
                (clock.now_iso(self.db), new_price, new_total,
                 new_currency, new_status, new_streak, new_oos,
                 listing_id))
            # W-check B5 (seller 軽微-4): the absence path writes a
            # 'listing_ended' audit event when a listing ends; the
            # declared-OOS path reached 'ended' WITHOUT one, so the
            # audit trail depended on which road the listing took.
            if new_status == "ended" \
                    and existing["status"] != "ended":
                import json as _json
                self.db.execute(
                    "INSERT INTO system_events(kind, detail_json,"
                    " created_at) VALUES('listing_ended', ?, ?)",
                    (_json.dumps(
                        {"listing_id": listing_id,
                         "note": "終了（店舗の在庫なし申告による）"}),
                     clock.now_iso(self.db)))
        else:
            if out_of_stock:
                return {"skipped": 1}
            # W-check B3 (maniac A D-3): a missing currency used to
            # DEFAULT to JPY, so a currency-less $49.99 JSON-LD price
            # was recorded as ¥49.99 of evidence, total_jpy included.
            # Unknown currency stays unknown (NULL) and carries no
            # yen total; only an explicit JPY earns total_jpy=price.
            from ..normalize import normalize_currency
            currency = normalize_currency(item.get("currency"))
            cur = self.db.execute(
                "INSERT INTO listings(source, url, price, currency,"
                " total_jpy, status, first_seen, last_seen, raw_title,"
                " barcode_source_tier)"
                " VALUES(?,?,?,?,?, 'active', ?,?,?,?)",
                (source_name, item["url"], item["price"], currency,
                 item["price"] if currency == "JPY" else None,
                 clock.now_iso(self.db), clock.now_iso(self.db),
                 item["raw_title"], item.get("barcode_source_tier")))
            listing_id = int(cur.lastrowid)
            created = 1
        self.db.commit()
        if out_of_stock:
            return {"created": created, "skipped": 1}
        results = matcher.match_listing(
            self.db, {**item, "source": source_name},
            listing_id=listing_id)
        # Identity write-back rule: a UNIQUE top result whose
        # EXISTENCE confidence is 高. ('auto' requires price
        # confidence 高 as well, which ingest items without a
        # verified barcode rarely reach — the review disposition
        # in those cases is about price evidence, not identity.
        # Price use stays governed by price_confidence in
        # match_decisions — and since W-check B3 pricedrop actually
        # READS it (price_evidence_ok), instead of the comment being
        # the only enforcement.)
        decided = []
        if results:
            top_score = max(r["score"] for r in results)
            tops = [r for r in results if r["score"] == top_score]
            if len(tops) == 1 \
                    and tops[0].get("existence_confidence") == "高" \
                    and tops[0].get("disposition") in ("auto",
                                                       "review"):
                decided = tops
        if len(decided) == 1:
            matched = 1
            edition_id = decided[0]["edition_id"]
            # R14 (collector A): the winning decision names the
            # variant it EVALUATED — but when the judgement did not
            # actually discriminate the variant (variant_gate false:
            # an E1-style 高 existence / 中 price), writing that id
            # onto the listing MANUFACTURES an attribution: /price
            # then presents the price as that variant's bucket
            # minimum although nobody established which variant the
            # copy is. Attribution is written only when the variant
            # was discriminated, or when the edition has exactly one
            # variant (attribution is then forced). An earlier
            # discriminated attribution is never erased by a later
            # undiscriminated sighting (COALESCE).
            variant_id = None
            if decided[0].get("variant_gate"):
                variant_id = decided[0].get("variant_id")
            if variant_id is None:
                variants = self.db.execute(
                    "SELECT id FROM edition_variants WHERE edition_id=?",
                    (edition_id,)).fetchall()
                if len(variants) == 1:
                    variant_id = variants[0][0]
            cond = cond_rank_for(item.get("media_cond"),
                                 item.get("sleeve_cond"))
            # cond, unlike variant, tracks the CURRENT page text:
            # a shop that stops stating a condition must not leave
            # a stale rank behind forever (R14, seller — the old
            # COALESCE did exactly that), so it is assigned, not
            # coalesced.
            self.db.execute(
                "UPDATE listings SET edition_id=?,"
                " variant_id=COALESCE(?, variant_id), cond_rank=?"
                " WHERE id=?",
                (edition_id, variant_id, cond, listing_id))
            self.db.commit()
        # REPRESS producer wiring (review fix): a copy that had
        # left the active set returning to active is the stock
        # transition the storefront exposes — feed it to the
        # variant_stock system when the edition is known.
        # W-check B4 (seller): only an 'ended' return arms the
        # signal. 'ended' means the absence machinery confirmed the
        # copy gone across two rounds; a 'sold_candidate' return is
        # a one-round flap, and counting it made a flapping used
        # one-off a standing REPRESS ingredient.
        row = self.db.execute(
            "SELECT * FROM listings WHERE id=?",
            (listing_id,)).fetchone()
        listing = dict(row)
        # R19 (seller): a DECLARED sold_out -> in_stock transition
        # (prev sold_candidate with oos_declared set) arms the
        # signal too — only the INFERRED one-round absence (flap)
        # stays unarmed.
        prev_oos = 0
        if existing is not None \
                and "oos_declared" in existing.keys():
            prev_oos = int(existing["oos_declared"] or 0)
        if (prev_status == "ended"
                or (prev_status == "sold_candidate" and prev_oos)) \
                and listing.get("edition_id"):
            from .. import repress
            repress.signal_variant_stock(
                self.db, edition_id=listing["edition_id"],
                was="sold_out", now="in_stock",
                title=listing.get("raw_title") or "")
        # R19 (seller): a copy re-listed under a NEW URL at the
        # same store never signalled — the transition lived on the
        # old row, which stays 'ended' forever. The variant_stock
        # system is variant-level, not copy-level: a fresh active
        # listing for an edition that has an ended listing at the
        # same source IS the stock returning.
        if created == 1 and listing.get("edition_id"):
            twin = self.db.execute(
                "SELECT 1 FROM listings WHERE source=? AND"
                " edition_id=? AND status='ended' AND id!=?"
                " LIMIT 1",
                (source_name, listing["edition_id"],
                 listing_id)).fetchone()
            if twin is not None:
                from .. import repress
                repress.signal_variant_stock(
                    self.db, edition_id=listing["edition_id"],
                    was="sold_out", now="in_stock",
                    title=listing.get("raw_title") or "")
        # Price-drop wiring: bucket comparison, plus the same-copy
        # path when a known listing's own YEN value just fell. The
        # same-copy path's contract is yen amounts (R14): the raw
        # price column of a foreign listing is not yen, and passing
        # it used to print "$499.99 -> $299.99" as "500円 -> 300円".
        pricedrop.on_listing_price(self.db, listing)
        from ..scoring import yen_value
        new_yen = yen_value(listing.get("total_jpy"),
                            listing.get("price"),
                            listing.get("currency"))
        if old_yen is not None and new_yen is not None \
                and new_yen < old_yen:
            pricedrop.on_listing_price_update(
                self.db, listing, old_yen, new_yen)
        return {"created": created, "matched": matched,
                "skipped": skipped}
