"""Regression tests for the multi-perspective review of Phase 2/3
(2026-11-08, maniac-B + adversarial-QA persona — bot commands,
market API clients, webpages fetchers, goods, doujin events)."""
import gzip
from datetime import timedelta

import pytest

from grt import clock, deadlines_ops
from grt.bot import Bot
from grt.fetchers import webpages
from grt.fetchers.base import FetchError
from grt.fetchers.doujin_event import extract_event_items
from grt.fetchers.goods import GoodsFetcher, extract_goods_items
from grt.fetchers.webpages import (extract_jsonld_products,
                                   is_product_like_url,
                                   parse_sitemap_bytes)


class _Transport:
    def __call__(self, *a, **kw):
        return {"ok": True}


def _bot(db, monkeypatch):
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('allowed_chat_id', '555'),"
        " ('deadline_override_quiet_selected', '1')")
    db.commit()
    bot = Bot(db, _Transport())
    sent = []
    monkeypatch.setattr(bot, "_send",
                        lambda chat, text, reply_markup=None:
                        sent.append((text, reply_markup)))
    return bot, sent


def _cmd(bot, text, uid=1):
    return bot.handle_update({"update_id": uid, "message": {
        "chat": {"id": 555}, "text": text}})


# B1: /queue presents the top pending candidate with buttons.
def test_queue_presents_candidate_with_buttons(db, monkeypatch):
    import json as _json
    for i, prio in enumerate((90, 10, 50)):
        cur = db.execute(
            "INSERT INTO candidate_editions(title, fingerprint,"
            " existence_confidence, price_confidence, found_via,"
            " status) VALUES(?, ?, '中', '低', 'rss', 'pending')",
            (f"Cand{i}", f"fp{i}"))
        db.execute(
            "INSERT INTO review_queue(kind, payload_json, priority,"
            " created_at) VALUES('candidate_review', ?, ?,"
            " '2026-11-08')",
            (_json.dumps({"candidate_id": int(cur.lastrowid)}), prio))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/queue")
    texts = [t for t, _ in sent]
    assert any("承認待ち候補: 3件" in t for t in texts)
    marked = [m for _, m in sent if m]
    assert marked, "no button message was sent"
    buttons = marked[0]["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("approve:")
    assert buttons[1]["callback_data"].startswith("reject:")
    shown = [t for t in texts if "次の候補" in t][0]
    assert "Cand0" in shown  # highest priority (90) first


# B2: /deadlines names candidates, discloses overflow, lists rows.
def test_deadlines_names_candidates_and_overflow(db, monkeypatch):
    future = "2999-01-01T00:00:00+09:00"
    deadlines_ops.set_schedule(
        db, candidate_id=77, channel="shop", deadline_raw="予約締切",
        deadline_tz="Asia/Tokyo", deadline_jst=future,
        deadline_confidence="confirmed", deadline_kind="preorder")
    for i in range(11):
        deadlines_ops.set_schedule(
            db, edition_id=f"AR-{i}", channel="shop",
            deadline_raw="締切", deadline_tz="Asia/Tokyo",
            deadline_jst=future, deadline_confidence="confirmed",
            deadline_kind="hard_cutoff")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/deadlines")
    joined = "\n".join(t for t, _ in sent)
    assert "None" not in joined
    assert "候補#77" in joined
    assert "他" in joined and "件" in joined  # overflow disclosed


# B2b: an unconfirmed row with a parseable raw gets TZ buttons,
# and the tzfix callback confirms it.
def test_deadlines_tzfix_flow(db, monkeypatch):
    sid = deadlines_ops.set_schedule(
        db, edition_id="AR-9", channel="shop",
        deadline_raw="2027-01-01T00:00:00", deadline_tz=None,
        deadline_jst="2027-01-01T00:00:00",
        deadline_confidence="tz_unknown", deadline_kind="hard_cutoff")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/deadlines")
    marked = [m for _, m in sent if m]
    assert marked and "tzfix:" in str(marked[0])
    bot.handle_update({"update_id": 9, "callback_query": {
        "id": "cb1", "from": {"id": 555},
        "message": {"chat": {"id": 555}},
        "data": f"tzfix:{sid}:0"}})  # Asia/Tokyo
    row = db.execute(
        "SELECT deadline_confidence, deadline_jst FROM"
        " release_schedule WHERE id=?", (sid,)).fetchone()
    assert row[0] == "confirmed"
    assert row[1] == "2027-01-01T00:00:00+09:00"


# B4: /price median of an even count + NULL latest price label.
def test_price_median_and_null_latest(db, monkeypatch):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    now = clock.now(db)
    for i, price in enumerate((100.0, 200.0, 300.0, 400.0)):
        db.execute(
            "INSERT INTO price_snapshots(edition_id, source, taken_at,"
            " num_for_sale, lowest_price, currency) VALUES('AR-1',"
            " 'discogs', ?, 1, ?, 'USD')",
            (clock.to_iso(now - timedelta(hours=4 - i)), price))
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price, currency) VALUES('AR-1',"
        " 'discogs', ?, 0, NULL, 'USD')",
        (clock.to_iso(now),))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/price AR-1")
    joined = "\n".join(t for t, _ in sent)
    # W-check B3 display update: amounts are formatted without a
    # forced fractional part (250, not the raw float repr 250.0).
    assert "中央値 250" in joined
    assert "最安 None" not in joined and "最安なし" in joined


# B5: /coverage ignores snapshots of nonexistent editions.
def test_coverage_ignores_ghost_snapshots(db, monkeypatch):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    for eid, taken_at in (("AR-1", clock.now_iso(db)),
                          ("GHOST-404", "2026-11-08")):
        db.execute(
            "INSERT INTO price_snapshots(edition_id, source, taken_at,"
            " num_for_sale, lowest_price) VALUES(?, 'discogs', ?, 1, 100.0)",
            (eid, taken_at))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/coverage")
    joined = "\n".join(t for t, _ in sent)
    assert "価格証拠あり: 1件" in joined and "100.0%" in joined


def test_coverage_excludes_expired_discogs_snapshot(db, monkeypatch):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price) VALUES('AR-1', 'discogs',"
        " '2026-11-08', 1, 100.0)")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    _cmd(bot, "/coverage")
    joined = "\n".join(t for t, _ in sent)
    assert "価格証拠あり: 0件" in joined and "0.0%" in joined


# B6: reversed rarity range is an error, not a 0-item success.
def test_watch_reversed_range_rejected(db, monkeypatch):
    db.execute(
        "INSERT INTO editions(id, title, rarity) VALUES"
        " ('AR-1','T','R4')")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    action = _cmd(bot, "/watch R5-R4")
    assert action == "watch_range_invalid"
    lvl = db.execute(
        "SELECT watch_level FROM editions WHERE id='AR-1'"
    ).fetchone()[0]
    assert not lvl


# B9: a truncated feed is a parse failure, not a ghost success.
def test_truncated_feed_is_parse_error(db, monkeypatch):
    import grt.fetchers.webpages as wp

    class _Resp:
        text = "<rss><channel><item>"
        status_code = 200

    monkeypatch.setattr(wp, "http_get", lambda url, headers=None: _Resp())
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES('F',"
        " 'RSS', 'approved', 'https://f.jp/rss')")
    db.commit()
    f = wp.FeedFetcher(db, int(cur.lastrowid))
    with pytest.raises(FetchError):
        f.fetch_items("https://f.jp/rss")


# B10: namespace-less sitemaps parse.
def test_sitemap_without_namespace():
    entries, children = parse_sitemap_bytes(
        b"<urlset><url><loc>https://shop.jp/p1</loc></url></urlset>")
    assert [e["url"] for e in entries] == ["https://shop.jp/p1"]


# B11: one broken child sitemap does not discard healthy children.
def test_sitemap_broken_child_skipped(db, monkeypatch):
    import grt.fetchers.webpages as wp
    pages = {
        "https://s.jp/index.xml": (
            b"<sitemapindex"
            b" xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            b"<sitemap><loc>https://s.jp/good.xml</loc></sitemap>"
            b"<sitemap><loc>https://s.jp/bad.xml</loc></sitemap>"
            b"</sitemapindex>"),
        "https://s.jp/good.xml": (
            b"<urlset"
            b" xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            b"<url><loc>https://s.jp/p1</loc></url></urlset>"),
        "https://s.jp/bad.xml": b"<urlset><url><loc>broken",
    }

    class _Resp:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(
        wp, "http_get",
        lambda url, headers=None: _Resp(pages[url]))
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES('S',"
        " 'SITEMAP', 'approved', 'https://s.jp/index.xml')")
    db.commit()
    f = wp.SitemapFetcher(db, int(cur.lastrowid))
    items, _ = f.fetch_items("https://s.jp/index.xml")
    assert [i["url"] for i in items] == ["https://s.jp/p1"]


# B12: gzip expansion is capped.
def test_sitemap_gzip_bomb_capped():
    payload = b"<urlset>" + b" " * (30 * 1024 * 1024) + b"</urlset>"
    with pytest.raises(ValueError):
        parse_sitemap_bytes(gzip.compress(payload))


# B13: non-product sitemap paths are filtered out.
def test_product_like_url_filter():
    assert is_product_like_url("https://shop.jp/products/lp-1")
    assert not is_product_like_url("https://shop.jp/about")
    assert not is_product_like_url("https://shop.jp/privacy")
    assert not is_product_like_url("https://shop.jp/")


# B14/B15: JSON-LD nesting, offer lists, bool prices, GTIN->barcode.
def test_jsonld_nested_and_offer_list():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","mainEntity":
      {"@type":"Product","name":"Nested LP","gtin13":"4900000000002",
       "offers":[{"@type":"Offer"},
                 {"@type":"Offer","price":true},
                 {"@type":"Offer","price":"5400",
                  "priceCurrency":"JPY"}]}}
    </script></head></html>"""
    items = extract_jsonld_products(html, "https://shop.jp/p/1")
    assert len(items) == 1
    assert items[0]["price"] == 5400.0
    assert items[0]["barcode"] == "4900000000002"
    assert items[0]["gtin"] == "4900000000002"


# B17: a navigation <li> does not suppress real goods.
def test_goods_nav_does_not_suppress_products():
    html = """<html><body>
    <ul><li><a href="/home">Home</a></li></ul>
    <div><a href="/goods/tshirt">OST Tシャツ</a></div>
    </body></html>"""
    items = extract_goods_items(html, "https://shop.jp/")
    titles = [i["title"] for i in items]
    assert "OST Tシャツ" in titles and "Home" not in titles


# B18: a same-title candidate in another lane does not swallow goods.
def test_goods_lane_fingerprint_separation(db):
    db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status,"
        " found_via) VALUES('Shared Title', 'shared title||',"
        " 'pending', 'rss')")
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES('G',"
        " 'GOODS', 'approved', 'https://g.jp')")
    db.commit()
    f = GoodsFetcher(db, int(cur.lastrowid))
    out = f.register_goods([{"title": "Shared Title",
                             "raw_title": "Shared Title",
                             "url": "https://g.jp/item/1",
                             "price_text": "¥1,000"}])
    assert out["created"] == 1
    lanes = db.execute(
        "SELECT COUNT(*) FROM candidate_editions WHERE"
        " distribution_type='goods'").fetchone()[0]
    assert lanes == 1


# B19: the circle link is not the work title.
def test_doujin_circle_link_not_title():
    html = """<html><body><ul>
    <li><a href="/circle/9">Circle Nine</a>
        <a href="/works/9">Real Work Title</a></li>
    </ul></body></html>"""
    items = extract_event_items(html, "https://event.jp/")
    assert len(items) == 1
    assert items[0]["title"] == "Real Work Title"
    assert items[0]["circle"] == "Circle Nine"


# B7/B8: API clients keep the FetchError contract and validate prices.
def test_api_clients_contract_and_prices(db, monkeypatch):
    import httpx

    from grt.fetchers import ebay as ebay_mod
    from grt.fetchers import rakuten as rakuten_mod
    from grt.fetchers import yahoo_shopping as yahoo_mod

    class _Resp:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    cases = [
        (ebay_mod, "EbayFetcher", "EBAY_OAUTH_TOKEN",
         {"itemSummaries": 123}, {"itemSummaries": [
             {"title": "X", "itemWebUrl": "https://e/1",
              "price": {"value": "nan", "currency": "USD"}}]}),
        (rakuten_mod, "RakutenFetcher", "RAKUTEN_APP_ID",
         {"items": 123}, {"items": [
             {"itemName": "X", "itemUrl": "https://r/1",
              "itemPrice": True},
             {"itemName": "Y", "itemUrl": "https://r/2",
              "itemPrice": 4200.9}]}),
        (yahoo_mod, "YahooShoppingFetcher", "YAHOO_APP_ID",
         {"hits": 123}, {"hits": [
             {"name": "X", "url": "https://y/1", "price": True},
             {"name": "Y", "url": "https://y/2", "price": 4200.9}]}),
    ]
    for mod, cls_name, key, bad, good in cases:
        monkeypatch.setenv(key, "dummy")
        if mod is rakuten_mod:
            monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "dummy")
        fetcher_cls = getattr(mod, cls_name)
        cur = db.execute(
            "INSERT INTO sources(name, kind, status) VALUES(?, 'API',"
            " 'approved')", (mod.__name__,))
        db.commit()
        f = fetcher_cls(db, int(cur.lastrowid))
        monkeypatch.setattr(
            mod.httpx, "get", lambda *a, **kw: _Resp(bad))
        with pytest.raises(FetchError):
            f.search_catalog("ABC-100")
        monkeypatch.setattr(
            mod.httpx, "get", lambda *a, **kw: _Resp(good))
        items = f.search_catalog("ABC-100")
        prices = [i["price"] for i in items]
        assert all(p is None or p == 4200.9 for p in prices), prices
        assert 1 not in [p for p in prices if p is not None]
