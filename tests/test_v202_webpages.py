"""No-API web acquisition tests (v2.0.2): generic feed, sitemap
walking (urlset + index + robots discovery), schema.org JSON-LD
extraction. All transport is faked; no network, no API keys."""
import gzip

from grt.fetchers import webpages
from grt.fetchers.webpages import (FeedFetcher, SitemapFetcher,
                                   StructuredFetcher,
                                   discover_sitemaps_from_robots,
                                   extract_jsonld_products,
                                   parse_sitemap_bytes)

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Shop News</title>
<item><title>New LP A</title><link>https://shop.jp/a</link>
<description>arrival</description></item>
<item><title>New LP B</title><link>https://shop.jp/b</link></item>
</channel></rss>"""

ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Shop News</title>
<entry><id>tag:shop.jp,2026:release-1</id><title>New LP C</title>
<link href="https://shop.jp/c"/><published>2026-11-09T12:00:00Z</published>
<updated>2026-11-10T08:00:00Z</updated></entry></feed>"""

URLSET = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://shop.jp/products/lp-1</loc><lastmod>2026-11-01</lastmod></url>
<url><loc>https://shop.jp/about</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://shop.jp/sitemap-products.xml</loc></sitemap>
</sitemapindex>"""

PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Game OST LP","gtin13":"4900000000002",
 "offers":{"@type":"Offer","price":"5400","priceCurrency":"JPY",
 "availability":"https://schema.org/InStock",
 "url":"https://shop.jp/products/lp-1"}}
</script></head><body>product</body></html>"""


class _Resp:
    def __init__(self, text="", content=None, status=200):
        self.text = text
        self.content = content if content is not None \
            else text.encode("utf-8")
        self.status_code = status


def _source(db, name, kind):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status, url) VALUES(?,?,"
        " 'approved', 'https://shop.jp/x')", (name, kind))
    db.commit()
    return int(cur.lastrowid)


def test_feed_fetcher_registers_candidates(db, monkeypatch):
    monkeypatch.setattr(webpages, "http_get",
                        lambda url, headers=None: _Resp(FEED))
    sid = _source(db, "shopfeed", "RSS")
    f = FeedFetcher(db, sid)
    res = f.run("https://shop.jp/feed")
    assert res["ok"] and len(res["items"]) == 2
    out = f.register_candidates(res["items"], found_via="feed:shopfeed")
    assert out["created"] == 2
    out2 = f.register_candidates(res["items"], found_via="feed:shopfeed")
    assert out2["created"] == 0  # fingerprint dedupe


def test_feed_fetcher_persists_publisher_dates_and_observation(db, monkeypatch):
    monkeypatch.setattr(webpages, "http_get",
                        lambda url, headers=None: _Resp(ATOM_FEED))
    sid = _source(db, "atomfeed", "RSS")
    f = FeedFetcher(db, sid)
    res = f.run("https://shop.jp/atom")
    item = res["items"][0]
    assert item["id"] == "tag:shop.jp,2026:release-1"
    assert item["published"] == "2026-11-09T12:00:00Z"
    assert item["updated"] == "2026-11-10T08:00:00Z"
    assert f.register_candidates(res["items"], found_via="feed:atomfeed")["created"] == 1
    row = db.execute(
        "select publisher_published_at,publisher_updated_at,first_seen_at,last_seen_at"
        " from feed_item_observations where source_id=?", (sid,)).fetchone()
    assert row[:2] == ("2026-11-09T21:00:00+09:00", "2026-11-10T17:00:00+09:00")
    assert row[2] == row[3]


def test_sitemap_parsing_and_robots_discovery():
    entries, children = parse_sitemap_bytes(URLSET.encode())
    assert [e["url"] for e in entries] == [
        "https://shop.jp/products/lp-1", "https://shop.jp/about"]
    assert entries[0]["lastmod"] == "2026-11-01"
    assert children == []
    entries2, children2 = parse_sitemap_bytes(
        gzip.compress(URLSET.encode()))
    assert len(entries2) == 2 and children2 == []
    robots = "User-agent: *\nSitemap: https://shop.jp/sitemap.xml\n"
    assert discover_sitemaps_from_robots(robots) == [
        "https://shop.jp/sitemap.xml"]


def test_sitemap_fetcher_walks_index_with_filter(db, monkeypatch):
    docs = {"https://shop.jp/sitemap.xml": INDEX,
            "https://shop.jp/sitemap-products.xml": URLSET}
    monkeypatch.setattr(
        webpages, "http_get",
        lambda url, headers=None: _Resp(docs[url]))
    sid = _source(db, "shopmap", "SITEMAP")
    f = SitemapFetcher(db, sid, filters=["/products/"])
    res = f.run("https://shop.jp/sitemap.xml")
    assert res["ok"]
    assert [i["url"] for i in res["items"]] == [
        "https://shop.jp/products/lp-1"]


def test_jsonld_product_extraction():
    items = extract_jsonld_products(PAGE, "https://shop.jp/p")
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Game OST LP"
    assert it["price"] == 5400.0 and it["currency"] == "JPY"
    assert it["gtin"] == "4900000000002"
    assert it["availability"].endswith("InStock")


def test_structured_fetcher_items(db, monkeypatch):
    monkeypatch.setattr(webpages, "http_get",
                        lambda url, headers=None: _Resp(PAGE))
    sid = _source(db, "shoppage", "STRUCTURED")
    res = StructuredFetcher(db, sid).run("https://shop.jp/products/lp-1")
    assert res["ok"] and len(res["items"]) == 1
