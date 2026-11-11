"""GOODS and DOUJIN_EVENT fetchers (Phase 2 kinds).

Covers: goods HTML extraction + the goods candidate lane (status /
distribution_type stay 'goods', never mixed into edition flows),
doujin event extraction + circle-aware fingerprints, the
event_catalog harvest window (in / before / after / open / none),
and fetch failure recording through BaseFetcher.run().

Transport is faked by monkeypatching httpx.get, the same pattern as
tests/test_qc_r5_faults.py.
"""
from datetime import timedelta

import httpx
import pytest

from grt import clock
from grt.fetchers.doujin_event import (DoujinEventFetcher,
                                       extract_event_items,
                                       harvest_event_catalog)
from grt.fetchers.goods import GoodsFetcher, extract_goods_items

GOODS_URL = "https://shop.example.com/list"
GOODS_HTML = """<html><body>
<header><a href="/">TOP</a></header>
<ul class="products">
  <li class="product"><a href="/goods/tee">ゲームOST Tシャツ</a>
    <span class="price">¥3,300</span></li>
  <li class="product"><a href="/goods/tote">サントラ トートバッグ</a>
    <span class="price">2,200円</span></li>
  <li class="product"><a href="https://shop.example.com/goods/badge">缶バッジ セット</a></li>
</ul>
</body></html>"""

EVENT_URL = "https://event.example.com/catalog"
EVENT_HTML = """<html><body>
<div class="circle-entry"><span class="circle-name">波動サークル</span>
  <a href="/works/1">幻想サウンドファイル</a></div>
<ul>
  <li><span class="circle">星屑工房</span>
    <a href="/works/2">星屑メロディーズ</a></li>
  <li data-circle="月光堂"><a href="/works/3">月光ピアノ集</a></li>
  <li><a href="/works/4">サークル不明の作品</a></li>
</ul>
</body></html>"""


class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text
        # W-check B5 fixture support: http_get now streams
        # (25MB body cap), so the fake also answers httpx.stream.
        self.headers = {}

    def iter_bytes(self):
        yield (self.text or "").encode("utf-8")


class _StreamOf:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


def _source(db, name, kind):
    cur = db.execute(
        "INSERT INTO sources(name, kind, status) VALUES(?, ?, 'approved')",
        (name, kind))
    db.commit()
    return cur.lastrowid


def _serve(monkeypatch, text, status=200):
    _resp = _Resp(status=status, text=text)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp)
    monkeypatch.setattr(httpx, "stream",
                        lambda *a, **k: _StreamOf(_resp))


def _last_fetch_run(db):
    return db.execute(
        "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()


def _goods(db):
    f = GoodsFetcher(db, _source(db, "test_goods_shop", "shop"))
    f.min_gap_seconds = 0
    return f


def _event_fetcher(db, name="event_src"):
    f = DoujinEventFetcher(db, _source(db, name, "event"))
    f.min_gap_seconds = 0
    return f


# ---------------------------------------------------------------- goods

def test_goods_extraction_from_html():
    items = extract_goods_items(GOODS_HTML, GOODS_URL)
    assert [i["title"] for i in items] == [
        "ゲームOST Tシャツ", "サントラ トートバッグ", "缶バッジ セット"]
    assert items[0]["url"] == "https://shop.example.com/goods/tee"
    assert items[0]["price_text"] == "¥3,300"
    assert items[1]["price_text"] == "2,200円"
    assert items[2]["price_text"] is None
    assert items[0]["raw_title"] == "ゲームOST Tシャツ"
    # The header nav link is not inside a product container and the
    # fallback pass must not kick in when containers produced items.
    assert all("example.com/goods" in i["url"] or
               i["url"].endswith("badge") for i in items)


def test_goods_run_ok_records_fetch_run(db, monkeypatch):
    _serve(monkeypatch, GOODS_HTML)
    res = _goods(db).run(GOODS_URL)
    assert res["ok"] is True and res["result"] == "OK"
    assert len(res["items"]) == 3
    run = _last_fetch_run(db)
    assert run["result"] == "OK" and run["fetcher_kind"] == "GOODS"
    assert run["items_extracted"] == 3


def test_goods_registration_keeps_goods_lane(db, monkeypatch):
    _serve(monkeypatch, GOODS_HTML)
    f = _goods(db)
    items, _ev = f.fetch_items(GOODS_URL)
    reg = f.register_goods(items)
    # W-check B4: the containment fix added the per-item 'failed'
    # count to the return contract.
    assert reg == {"created": 3, "duplicates": 0, "failed": 0}
    rows = db.execute(
        "SELECT * FROM candidate_editions ORDER BY id").fetchall()
    assert len(rows) == 3
    for row in rows:
        # v2.0.3 review fix (adversarial QA / agreement-09): the
        # lane marker is distribution_type='goods'; the status is
        # 'pending' because goods hits go through the review queue
        # like every other candidate. The old assertions pinned the
        # opposite (queue-free 'goods' status) as correct.
        assert row["status"] == "pending"
        assert row["distribution_type"] == "goods"
        assert row["found_via"] == "test_goods_shop"
    # Price strings stay raw page text, never a structured price.
    by_title = {r["title"]: r for r in rows}
    assert by_title["ゲームOST Tシャツ"]["raw_description"] == "¥3,300"
    # Nothing leaked into the edition flow.
    assert db.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 0
    # Every goods candidate is in the review queue (agreement-09).
    assert db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE"
        " kind='candidate_review'").fetchone()[0] == 3
    # Second registration is fully deduped by fingerprint.
    # (B4: return contract includes 'failed' — see above.)
    assert f.register_goods(items) == {"created": 0, "duplicates": 3,
                                       "failed": 0}
    assert db.execute(
        "SELECT COUNT(*) FROM candidate_editions").fetchone()[0] == 3


def test_goods_fetch_failure_returns_not_ok(db, monkeypatch):
    _serve(monkeypatch, "forbidden", status=403)
    res = _goods(db).run(GOODS_URL)
    assert res["ok"] is False and res["result"] == "BLOCKED"
    assert res["items"] == []
    run = _last_fetch_run(db)
    assert run["result"] == "BLOCKED" and run["fetcher_kind"] == "GOODS"


def test_goods_empty_page_is_parse_error(db, monkeypatch):
    _serve(monkeypatch, "   ")
    res = _goods(db).run(GOODS_URL)
    assert res["ok"] is False and res["result"] == "PARSE_ERROR"


# --------------------------------------------------------------- doujin

def test_doujin_extraction_from_html():
    items = extract_event_items(EVENT_HTML, EVENT_URL)
    assert [(i["title"], i["circle"]) for i in items] == [
        ("幻想サウンドファイル", "波動サークル"),
        ("星屑メロディーズ", "星屑工房"),
        ("月光ピアノ集", "月光堂"),
        ("サークル不明の作品", None),
    ]
    assert items[0]["url"] == "https://event.example.com/works/1"
    assert items[0]["raw_title"] == "幻想サウンドファイル"


def _add_event(db, name, url=EVENT_URL, harvest_from=None, harvest_to=None):
    cur = db.execute(
        "INSERT INTO event_catalog(name, kind, url, harvest_from,"
        " harvest_to, status) VALUES(?, 'doujin', ?, ?, ?, 'scheduled')",
        (name, url, harvest_from, harvest_to))
    db.commit()
    return cur.lastrowid


def test_doujin_registration_lane_and_circle_fingerprint(db, monkeypatch):
    _serve(monkeypatch, EVENT_HTML)
    event_id = _add_event(db, "テスト即売会")
    f = _event_fetcher(db)
    items, _ev = f.fetch_items(EVENT_URL)
    reg = f.register_event_items(items, event_id)
    assert reg["created"] == 4 and reg["duplicates"] == 0
    assert reg["found_via"] == "doujin_event:テスト即売会"
    rows = db.execute("SELECT * FROM candidate_editions").fetchall()
    assert len(rows) == 4
    # W-check B4: doujin candidates register in the normal review
    # flow (status='pending'); the old 'doujin_event' status was
    # invisible to every consumer and could never be resolved.
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["found_via"] == "doujin_event:テスト即売会" for r in rows)
    # Same title from another circle is a DIFFERENT candidate
    # (the circle is part of the fingerprint); same circle+title
    # collapses.
    extra = [{"title": "幻想サウンドファイル", "url": "/works/9",
              "circle": "別サークル", "raw_title": "幻想サウンドファイル"},
             {"title": "幻想サウンドファイル", "url": "/works/1",
              "circle": "波動サークル", "raw_title": "幻想サウンドファイル"}]
    reg2 = f.register_event_items(extra, event_id)
    # (B4: return contract includes the containment 'failed' count.)
    assert reg2 == {"created": 1, "duplicates": 1, "failed": 0,
                    "found_via": "doujin_event:テスト即売会"}
    assert db.execute(
        "SELECT COUNT(*) FROM candidate_editions").fetchone()[0] == 5


def test_doujin_fetch_failure_returns_not_ok(db, monkeypatch):
    _serve(monkeypatch, "server error", status=500)
    res = _event_fetcher(db).run(EVENT_URL)
    assert res["ok"] is False and res["result"] == "HTTP_ERROR"
    run = _last_fetch_run(db)
    assert run["result"] == "HTTP_ERROR"
    assert run["fetcher_kind"] == "DOUJIN_EVENT"


# --------------------------------------------------------------- harvest

def _day(offset: int) -> str:
    return (clock.now().date() + timedelta(days=offset)).isoformat()


def test_harvest_window_in_and_out(db, monkeypatch):
    _serve(monkeypatch, EVENT_HTML)
    in_id = _add_event(db, "開催中イベント",
                       harvest_from=_day(-1), harvest_to=_day(1))
    past_id = _add_event(db, "終了イベント",
                         harvest_from=_day(-10), harvest_to=_day(-5))
    future_id = _add_event(db, "未来イベント",
                           harvest_from=_day(5), harvest_to=_day(10))
    open_id = _add_event(db, "開始未定イベント",
                         harvest_from=None, harvest_to=_day(0))
    none_id = _add_event(db, "窓なしイベント")

    factory_calls = []

    def factory(db_, row):
        factory_calls.append(row["id"])
        f = DoujinEventFetcher(
            db_, _source(db_, f"event:{row['name']}", "event"))
        f.min_gap_seconds = 0
        return f

    summaries = harvest_event_catalog(db, factory)
    by_id = {s["event_id"]: s for s in summaries}
    assert set(by_id) == {in_id, past_id, future_id, open_id, none_id}

    assert by_id[in_id]["ok"] is True and by_id[in_id]["skipped"] is False
    assert by_id[in_id]["items"] == 4 and by_id[in_id]["created"] == 4
    # Open-start window (harvest_to == today) is in-window too; the
    # same fixture items dedupe against the first event's rows.
    assert by_id[open_id]["ok"] is True and by_id[open_id]["items"] == 4
    assert by_id[open_id]["created"] == 0

    assert by_id[past_id]["skipped"] is True
    assert by_id[past_id]["reason"] == "window_after"
    assert by_id[future_id]["skipped"] is True
    assert by_id[future_id]["reason"] == "window_before"
    assert by_id[none_id]["skipped"] is True
    assert by_id[none_id]["reason"] == "window_no_window"

    # The factory is never invoked for out-of-window events.
    assert sorted(factory_calls) == sorted([in_id, open_id])
    # All harvested rows carry the doujin_event lane and the FIRST
    # event's found_via (later duplicates collapse onto it).
    rows = db.execute("SELECT * FROM candidate_editions").fetchall()
    assert len(rows) == 4
    # W-check B4: status is 'pending' now (see the lane test above);
    # the lane identity lives in found_via.
    assert all(r["status"] == "pending" for r in rows)
    assert {r["found_via"] for r in rows} == {"doujin_event:開催中イベント"}


def test_harvest_fetch_failure_is_summary_not_exception(db, monkeypatch):
    _serve(monkeypatch, "gone", status=404)
    _add_event(db, "落ちているイベント",
               harvest_from=_day(-1), harvest_to=_day(1))

    def factory(db_, row):
        f = DoujinEventFetcher(db_, _source(db_, "ev", "event"))
        f.min_gap_seconds = 0
        return f

    summaries = harvest_event_catalog(db, factory)
    assert len(summaries) == 1
    s = summaries[0]
    assert s["ok"] is False and s["skipped"] is False
    assert s["reason"] == "HTTP_ERROR" and s["created"] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM candidate_editions").fetchone()[0] == 0
