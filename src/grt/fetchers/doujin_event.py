"""Doujin event catalogue fetcher (fetcher kind: DOUJIN_EVENT).

Event pages (doujin event / circle catalogues) list circle products
that often never reach any store. Items harvested from an event are
registered as candidate_editions in the normal review flow
(status='pending' + a candidate_review queue row + button
notification, like every other lane) with a found_via naming the
event, fingerprinted with the circle as part of the key (two
circles may sell works under the same title).

W-check B4: the lane originally registered status='doujin_event',
which no consumer reads — the candidates were invisible in the
digest and /queue and could not be approved or rejected at all
(the resolver refuses non-'pending'). The lane also gained its
downstream producers here: an event-date deadline per candidate
(deadlines_ops.set_schedule), a seed-promotion request when a
harvested item composite-matches a doujin seed, and a sighting
record when a known work is re-distributed at a different event.

harvest_event_catalog() walks the event_catalog table and harvests
only the events whose harvest window covers today.
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import clock
from ..evidence import normalized_text_hash, save_raw_evidence
from ..normalize import fingerprint_for
from .base import BaseFetcher, FetchError, http_get

_CONTAINER_SELECTOR = "li, article, tr, .entry, .circle-entry, .item"


def _parse_event_date(value):
    """Tolerant event-date parse (W-check B5, maniac A 中-5 /
    軽微-1). event_catalog is hand-edited OUTSIDE the product, so
    its dates arrive as '2026/10/01', '2026年10月1日', '2026-10-1'
    or full datetimes, not only ISO. The harvest window used to
    compare the RAW STRINGS lexicographically against today's
    ISO date: '2026/10/01' sorted AFTER every ISO 'in' range, so
    the event was silently never harvested; a non-ISO starts_on
    was concatenated into 'T00:00:00' producing an unparseable
    instant that filed as tz_unknown with no word about why.
    Parse to a real date; None when unparseable (callers treat
    the bound as open and record the fact)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    from datetime import datetime as _dt, date as _date
    try:
        return _dt.fromisoformat(s).date()
    except ValueError:
        pass
    import re as _re
    m = _re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)),
                         int(m.group(3)))
        except ValueError:
            return None
    return None


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _circle_of(container, anchor) -> str | None:
    """Circle name for one container: an explicit data-circle
    attribute wins, then the first element whose class mentions
    'circle' (other than the product anchor itself)."""
    for el in (container, anchor):
        value = el.get("data-circle") if hasattr(el, "get") else None
        if value and _clean(value):
            return _clean(value)
    for el in container.find_all(True):
        if el is anchor:
            continue
        classes = " ".join(el.get("class") or [])
        if "circle" in classes and _clean(el.get_text()):
            return _clean(el.get_text())
    return None


def _is_circle_anchor(a) -> bool:
    href = (a.get("href") or "").lower()
    return "/circle" in href or "circle" in (a.get("class") or [])


# Anchor texts that are page navigation, never a work's title —
# event catalogue pages carry them in the same <li> containers as
# the works themselves (R19, maniac A).
_NAV_TEXTS = {"商品一覧", "一覧", "購入ガイド", "ガイド", "ヘルプ",
              "ヘルプ詳細", "詳細を見る", "詳しく見る", "もっと見る",
              "トップ", "ホーム", "詳細", "見る"}


def _anchor_text(a) -> str:
    return a.get("_grt_text") or _clean(a.get_text())


def _is_work_href(href: str) -> bool:
    """True when a PATH SEGMENT names a work page (R19): segment
    equality, not substring — '/item/list' still counts via its
    'item' segment but is then refused by its navigation text,
    and a query string can no longer fake a work path."""
    from urllib.parse import urlsplit
    try:
        path = urlsplit(href).path
    except ValueError:
        return False
    segments = {s.lower() for s in path.split("/") if s}
    return bool(segments & {"work", "works", "item", "items",
                            "product", "products", "detail",
                            "details"})


def extract_event_items(html: str, page_url: str) -> list[dict]:
    """Extract circle/product entries from an event catalogue page.

    One item per product-like container. The product anchor is the
    first anchor that does NOT point at a circle page (preferring
    work/item/product hrefs); a circle-page anchor supplies the
    circle name instead. A container with only a circle link has
    no work to register and is skipped.

    Review fix (adversarial QA): the first anchor used to win
    unconditionally, so a circle link listed before the work link
    turned the CIRCLE into the candidate and the real work was
    never extracted.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen_urls: set[str] = set()
    for container in soup.select(_CONTAINER_SELECTOR):
        # R19 (maniac A): image-only work links (a jacket image
        # with alt text and no anchor text — the common circle
        # catalogue shape) were dropped entirely; the alt/title
        # text stands in for the anchor text.
        anchors = []
        for a in container.find_all("a", href=True):
            text = _clean(a.get_text())
            if not text:
                img = a.find("img")
                if img is not None:
                    text = _clean(img.get("alt") or "")
                if not text:
                    text = _clean(a.get("title") or "")
            if text:
                a["_grt_text"] = text
                anchors.append(a)
        if not anchors:
            continue
        circle_anchor = next(
            (a for a in anchors if _is_circle_anchor(a)), None)
        work = [a for a in anchors if a is not circle_anchor]
        if not work:
            continue
        # W-check B4 (maniac A): one booth commonly lists several
        # works (new + back catalogue) in ONE container; the old
        # single-pick extracted only the first, so the rest never
        # even became candidates. Every explicit work anchor in the
        # container is an item now; containers without an explicit
        # work href keep the old single-pick behaviour.
        # R19 (maniac A): 'explicit' matched SUBSTRINGS of the
        # href, so navigation (/item/list 商品一覧, /product/guide
        # 購入ガイド, /detail/help, even a search URL whose QUERY
        # contained /work/55) became works. Matching is now on
        # path SEGMENTS, and generic navigation anchor texts are
        # never works.
        explicit = [a for a in work
                    if _is_work_href(a.get("href") or "")
                    and _anchor_text(a) not in _NAV_TEXTS]
        fallback = [a for a in work
                    if _anchor_text(a) not in _NAV_TEXTS]
        chosen = explicit if explicit else fallback[:1]
        circle = _anchor_text(circle_anchor) \
            if circle_anchor is not None \
            else _circle_of(container, chosen[0])
        for anchor in chosen:
            url = urljoin(page_url, anchor["href"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            items.append({
                "title": _anchor_text(anchor),
                "url": url,
                "circle": circle,
                "raw_title": _anchor_text(anchor),
            })
    return items


class DoujinEventFetcher(BaseFetcher):
    kind = "DOUJIN_EVENT"

    def fetch_items(self, url: str) -> tuple[list[dict], dict]:
        self._polite()
        resp = http_get(url, headers={"User-Agent": "GameRecordTool/1.0"})
        body = resp.text
        if not body or not body.strip():
            raise FetchError("PARSE_ERROR", "empty event page response")
        ev = save_raw_evidence("doujin_event", url, body, keep_body=False)
        self._stash_ev(ev)
        items = extract_event_items(body, url)
        ev["normalized_hash"] = normalized_text_hash(body)
        return items, ev

    def register_event_items(self, items, event_id: int) -> dict:
        """Record event items as candidate_editions in the doujin
        lane. found_via names the event ('doujin_event:<event
        name>'); the fingerprint includes the circle, so identical
        titles from different circles stay distinct while the same
        circle/title collapses to one row."""
        row = self.db.execute(
            "SELECT name, starts_on FROM event_catalog WHERE id=?",
            (event_id,)).fetchone()
        event_name = (row["name"] if row and row["name"] else None) \
            or f"event#{event_id}"
        # W-check B5 (maniac A 軽微-1): a non-ISO starts_on used
        # to be concatenated blindly into the deadline instant
        # ('2027/02/07T00:00:00' → unparseable → silently filed
        # tz_unknown). Normalise through the tolerant parser; a
        # value that cannot be parsed at all records WHY there
        # is no deadline instead of vanishing.
        _raw_starts = row["starts_on"] if row else None
        _parsed_start = _parse_event_date(_raw_starts)
        if _raw_starts and str(_raw_starts).strip() \
                and _parsed_start is None:
            # W-check B5 R24 (maniac A 軽微): this recording had
            # no dedupe — the window-side recording below dedupes
            # per distinct value via a settings flag, but this one
            # added a row on EVERY harvest of an unparseable
            # event (system_events are kept 180 days). Same
            # discipline: once per (event, raw value); a changed
            # or fixed-then-broken-again value records again.
            try:
                import json as _json
                from ..config import get_setting, set_setting
                _skey = f"event_start_bad|{event_id}"
                if get_setting(self.db, _skey) \
                        != str(_raw_starts):
                    self.db.execute(
                        "INSERT INTO system_events(kind,"
                        " detail_json, created_at) VALUES("
                        "'event_start_unparseable', ?, ?)",
                        (_json.dumps(
                            {"event_id": event_id,
                             "starts_on": str(_raw_starts)}),
                         clock.now_iso(self.db)))
                    set_setting(self.db, _skey, str(_raw_starts))
                    self.db.commit()
            except Exception:
                self.db.rollback()
        starts_on = _parsed_start.isoformat() \
            if _parsed_start is not None else None
        found_via = f"doujin_event:{event_name}"
        created, duplicates, failed = 0, 0, 0
        for item in items:
            # W-check B4 (adversarial QA): per-item containment, as
            # in the ingest lane.
            try:
                c, d = self._register_one(item, found_via, event_name,
                                          starts_on, event_id)
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
                        (_json.dumps({"lane": "doujin_event",
                                      "error": str(exc)[:200]}),
                         clock.now_iso(self.db)))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
        return {"created": created, "duplicates": duplicates,
                "failed": failed, "found_via": found_via}

    def _register_one(self, item, found_via, event_name,
                      starts_on, event_id) -> tuple[int, int]:
        # W-check B3 (maniac B D-7): with circle unknown the
        # fingerprint used to be identical to the generic lanes'
        # (title only), so a doujin item whose circle failed to
        # extract collided with a same-titled Blip Blop / feed
        # candidate and NEVER existed as a doujin candidate
        # (and vice versa). The doujin lane now carries its own
        # sentinel when the circle is unknown — the same device
        # the goods lane already used ('__goods__').
        fp = fingerprint_for(item["title"], None,
                             item.get("circle") or "__doujin__")
        now = clock.now_iso(self.db)
        # R19 (maniac A): a harvested item that composite-matches a
        # doujin SEED is the seed's sighting, not a second work —
        # creating a harvest candidate as well split one work into
        # two pending candidates, with the deadline and signals on
        # the harvest side and the promotion on the seed side, and
        # the user judging the same work twice. The sighting goes
        # through the promotion request only.
        # W-check B5 (maniac A 中-2 / 軽微-3): 'promotion request
        # only' also meant the seed-path work got NO deadline (the
        # event date — its one hard deadline — was never
        # registered for the seed candidate) and NO sighting
        # record when it was re-distributed at a different event.
        # The deadline and the sighting now attach to the SEED
        # row, exactly as they do to a harvest candidate; when the
        # seed is promoted, its schedules migrate with it.
        seed = self._matching_seed(item)
        if seed is not None:
            self._maybe_seed_promotion(item)
            if starts_on:
                from .. import deadlines_ops
                deadlines_ops.set_schedule(
                    self.db, candidate_id=int(seed["id"]),
                    channel="doujin_event",
                    deadline_raw=f"{event_name} 開催 {starts_on}",
                    deadline_tz="Asia/Tokyo",
                    deadline_jst=f"{starts_on}T00:00:00",
                    deadline_confidence="confirmed",
                    deadline_kind="hard_cutoff")
            self._record_sighting(seed, fp, event_name,
                                  event_id, now)
            return 0, 1
        # W-check B5 (maniac A 中-1): a seed that was ALREADY
        # promoted keeps the 'seed|...' fingerprint under status
        # 'pending', which the doujin lane's own fingerprint never
        # matches — re-harvesting the same event after promotion
        # created a SECOND pending candidate for the same work
        # (mitigated only by approval linking both to one
        # edition). A composite match against a promoted seed row
        # is a duplicate sighting of THAT row: aggregate the
        # source, register the (possibly postponed) deadline and
        # record the sighting on it.
        promoted = self._promoted_match(item)
        if promoted is not None:
            from ..candidates import note_candidate_source
            note_candidate_source(self.db,
                                  promoted["fingerprint"],
                                  item.get("url"))
            if starts_on:
                from .. import deadlines_ops
                deadlines_ops.set_schedule(
                    self.db, candidate_id=int(promoted["id"]),
                    channel="doujin_event",
                    deadline_raw=f"{event_name} 開催 {starts_on}",
                    deadline_tz="Asia/Tokyo",
                    deadline_jst=f"{starts_on}T00:00:00",
                    deadline_confidence="confirmed",
                    deadline_kind="hard_cutoff")
            self._record_sighting(promoted, fp, event_name,
                                  event_id, now)
            return 0, 1
        import json as _json
        cur = self.db.execute(
            "INSERT OR IGNORE INTO candidate_editions("
            " title, raw_title, source_url, fingerprint,"
            " existence_confidence, price_confidence,"
            " found_at, first_seen, last_seen, found_via, status,"
            " variant_hints_json)"
            " VALUES(?, ?, ?, ?, '中', '低', ?, ?, ?, ?, 'pending', ?)",
            (item["title"], item.get("raw_title") or item["title"],
             item["url"], fp, now, now, now, found_via,
             _json.dumps({"doujin_event_id": event_id})))
        if cur.rowcount == 0:
            from ..candidates import note_candidate_source
            note_candidate_source(self.db, fp, item.get("url"))
            cand = self.db.execute(
                "SELECT id, found_via, variant_hints_json FROM"
                " candidate_editions WHERE fingerprint=?",
                (fp,)).fetchone()
            if cand is not None:
                # R19 (four personas): the deadline producer only
                # ran on the creation path, so a POSTPONED event
                # (starts_on moved, or filled in later) never
                # reached set_schedule for existing candidates —
                # the old date kept alerting and the new date never
                # did. The duplicate path registers the deadline
                # too; set_schedule's no-op/supersede rules decide.
                if starts_on:
                    from .. import deadlines_ops
                    deadlines_ops.set_schedule(
                        self.db, candidate_id=int(cand["id"]),
                        channel="doujin_event",
                        deadline_raw=f"{event_name} 開催 {starts_on}",
                        deadline_tz="Asia/Tokyo",
                        deadline_jst=f"{starts_on}T00:00:00",
                        deadline_confidence="confirmed",
                        deadline_kind="hard_cutoff")
                self._record_sighting(cand, fp, event_name,
                                      event_id, now)
            self._maybe_seed_promotion(item)
            return 0, 1
        cid = cur.lastrowid
        self.db.execute(
            "INSERT INTO review_queue(kind, payload_json, fingerprint,"
            " priority, created_at) VALUES('candidate_review', ?, ?,"
            " 30, ?)",
            (_json.dumps({"candidate_id": cid, "source": found_via}),
             fp, now))
        from .. import outbox as _outbox
        _outbox.enqueue_candidate_review(self.db, cid)
        # Deadline producer (W-check B4): the event's start date is
        # the one hard deadline this lane knows — the work is
        # distributed ON that day and then, for event-only items,
        # effectively gone. Registered as a confirmed hard_cutoff
        # at 00:00 JST of the start date; set_schedule's supersede
        # rule keeps postponements from double-alerting.
        if starts_on:
            from .. import deadlines_ops
            deadlines_ops.set_schedule(
                self.db, candidate_id=cid, channel="doujin_event",
                deadline_raw=f"{event_name} 開催 {starts_on}",
                deadline_tz="Asia/Tokyo",
                deadline_jst=f"{starts_on}T00:00:00",
                deadline_confidence="confirmed",
                deadline_kind="hard_cutoff")
        self._maybe_seed_promotion(item)
        return 1, 0

    def _matching_seed(self, item):
        """The doujin seed this harvested item composite-matches,
        if any (shared gate with seedops.promote_seed)."""
        from .. import seedops
        found = {"title": item.get("title") or "",
                 "circle": item.get("circle") or "",
                 "catalog_norm": item.get("catalog_norm") or ""}
        for s in self.db.execute(
                "SELECT * FROM candidate_editions WHERE status='seed'"
        ).fetchall():
            if seedops.composite_key_matches(s, found):
                return s
        return None

    def _promoted_match(self, item):
        """An already-promoted seed row (status pending/approved,
        fingerprint still 'seed|...') this item composite-matches
        (W-check B5, maniac A 中-1)."""
        from .. import seedops
        found = {"title": item.get("title") or "",
                 "circle": item.get("circle") or "",
                 "catalog_norm": item.get("catalog_norm") or ""}
        for s in self.db.execute(
                "SELECT * FROM candidate_editions WHERE"
                " status IN ('pending', 'approved')"
                " AND fingerprint LIKE 'seed|%'").fetchall():
            if seedops.composite_key_matches(s, found):
                return s
        return None

    def _record_sighting(self, cand, fp, event_name, event_id,
                         now) -> None:
        """Re-distribution record (W-check B4, maniac A; R19
        rework): keyed by EVENT ID, not by the event's name string
        — a rename of the same event (the postponement pattern)
        is not a re-distribution, two distinct events sharing a
        name are, and one (candidate, event) pair records exactly
        once no matter how many harvests re-observe it. The detail
        carries the candidate id and both event names so the
        sighting can be traced afterwards."""
        import json as _json
        origin_id = None
        try:
            hints = _json.loads(cand["variant_hints_json"] or "{}")
            if isinstance(hints, dict):
                origin_id = hints.get("doujin_event_id")
        except (ValueError, TypeError):
            hints = {}
        if origin_id is None:
            # Legacy rows (created before the origin id was
            # recorded): resolve the origin event by its name.
            # W-check B5: seed rows name their origin the same way
            # under the 'doujin_seed:' prefix — seed-path sightings
            # (a seed re-distributed at a different event) resolve
            # through it too.
            suffix = (cand["found_via"] or "")
            for prefix in ("doujin_event:", "doujin_seed:"):
                if suffix.startswith(prefix):
                    suffix = suffix[len(prefix):]
                    break
            row = self.db.execute(
                "SELECT id FROM event_catalog WHERE name=?",
                (suffix,)).fetchone()
            origin_id = row[0] if row else None
        if origin_id is not None:
            if int(origin_id) == int(event_id):
                return
        elif cand["found_via"] == f"doujin_event:{event_name}":
            return  # legacy row, same event by name
        cid = int(cand["id"])
        for (detail,) in self.db.execute(
                "SELECT detail_json FROM system_events"
                " WHERE kind='doujin_event_sighting'").fetchall():
            try:
                d = _json.loads(detail or "")
            except (ValueError, TypeError):
                continue
            if isinstance(d, dict) \
                    and d.get("candidate_id") == cid \
                    and d.get("event_id") == int(event_id):
                return  # already recorded for this pair
        self.db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('doujin_event_sighting', ?, ?)",
            (_json.dumps({"fingerprint": fp,
                          "candidate_id": cid,
                          "event_id": int(event_id),
                          "event": event_name,
                          "from_event": cand["found_via"]}),
             now))

    def _maybe_seed_promotion(self, item) -> None:
        """Seed link (W-check B4, maniac A): a harvested item that
        composite-matches a doujin seed is exactly the 'analog
        sighting' the promotion flow exists for — request the
        promotion (deduped inside seedops) instead of leaving the
        seed and the sighting permanently unlinked."""
        from .. import seedops
        found = {"title": item.get("title") or "",
                 "circle": item.get("circle") or "",
                 "catalog_norm": item.get("catalog_norm") or ""}
        seeds = self.db.execute(
            "SELECT * FROM candidate_editions WHERE status='seed'"
        ).fetchall()
        for s in seeds:
            if seedops.composite_key_matches(s, found):
                seedops.request_seed_promotion(
                    self.db, int(s["id"]), found=found)
                # W-check B5 (maniac A 中-3): seed identity is
                # work x event (import_seed's fingerprint carries
                # the event name), so one work legitimately has
                # several seeds — but ONE sighting used to request
                # promotion for EVERY matching seed, and approving
                # them all minted several pending candidates for
                # one work. One sighting asks about the first
                # matching seed; the others are asked about by
                # their own events' sightings.
                break


def _window_state(row, today: str) -> str:
    """'in', 'before', 'after' or 'no_window' for one event row.

    W-check B5 (maniac A 中-5): the comparison used to be RAW
    STRING order against today's ISO date — '2026/10/01' sorted
    after every ISO range (judged 'before', harvest never ran,
    silently), '2026/10/31' judged 'in' past its end, '2026-11-1'
    misjudged on padding. Bounds now parse tolerantly and
    compare as dates; an unparseable bound is treated as OPEN
    (fail-open: the failure mode being fixed is silent
    non-harvest) and harvest_event_catalog records it."""
    start_raw = (row["harvest_from"] or "").strip()
    end_raw = (row["harvest_to"] or "").strip()
    if not start_raw and not end_raw:
        return "no_window"
    today_d = _parse_event_date(today)
    start = _parse_event_date(start_raw) if start_raw else None
    end = _parse_event_date(end_raw) if end_raw else None
    if start is not None and today_d is not None \
            and today_d < start:
        return "before"
    if end is not None and today_d is not None and today_d > end:
        return "after"
    return "in"


def harvest_event_catalog(db, fetcher_factory) -> list[dict]:
    """Harvest every event whose harvest window includes today.

    fetcher_factory is called as fetcher_factory(db, event_row) for
    each in-window event and must return a DoujinEventFetcher (or
    None to skip the event). ISO dates compare lexicographically;
    a missing bound is an open end, and an event with no window at
    all is never harvested. Returns one summary per event row:
    {"event_id", "name", "ok", "skipped", "reason", "items",
    "created"}.
    """
    today = clock.now(db).date().isoformat()
    summaries: list[dict] = []
    rows = db.execute(
        "SELECT * FROM event_catalog ORDER BY id").fetchall()
    for row in rows:
        # R19 (maniac A): observe renames. event_catalog is edited
        # outside the product, so a rename is only knowable when a
        # harvest pass sees the same event id under a new name;
        # that observation is recorded ('event_renamed') and the
        # health digest's harvest-anomaly grace restarts from it —
        # keying the grace on harvest_from alone false-fired on
        # every mid-window postponement rename.
        try:
            from ..config import get_setting, set_setting
            seen_key = f"event_name_seen|{row['id']}"
            seen_name = get_setting(db, seen_key)
            if seen_name is not None and seen_name != row["name"]:
                import json as _json
                db.execute(
                    "INSERT INTO system_events(kind, detail_json,"
                    " created_at) VALUES('event_renamed', ?, ?)",
                    (_json.dumps({"event_id": row["id"],
                                  "old": seen_name,
                                  "new": row["name"],
                                  "at": clock.now_iso(db)}),
                     clock.now_iso(db)))
            if seen_name != row["name"]:
                set_setting(db, seen_key, row["name"])
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        # W-check B5 (maniac A 中-5): an unparseable window bound
        # is treated as open by _window_state (fail-open) — but
        # not silently: record it once per distinct raw value so
        # the hand-edited catalogue gets fixed at the source.
        try:
            from ..config import get_setting, set_setting
            import json as _json
            for _col in ("harvest_from", "harvest_to"):
                _raw = (row[_col] or "").strip()
                if _raw and _parse_event_date(_raw) is None:
                    _key = f"event_window_bad|{row['id']}|{_col}"
                    if get_setting(db, _key) != _raw:
                        db.execute(
                            "INSERT INTO system_events(kind,"
                            " detail_json, created_at)"
                            " VALUES('event_window_unparseable',"
                            " ?, ?)",
                            (_json.dumps(
                                {"event_id": row["id"],
                                 "column": _col, "value": _raw}),
                             clock.now_iso(db)))
                        set_setting(db, _key, _raw)
                        db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        summary = {"event_id": row["id"], "name": row["name"],
                   "ok": False, "skipped": False, "reason": None,
                   "items": 0, "created": 0}
        state = _window_state(row, today)
        if state != "in":
            summary["skipped"] = True
            summary["reason"] = f"window_{state}"
            summaries.append(summary)
            continue
        fetcher = fetcher_factory(db, row)
        if fetcher is None:
            summary["skipped"] = True
            summary["reason"] = "no_fetcher"
            summaries.append(summary)
            continue
        res = fetcher.run(row["url"])
        if not res["ok"]:
            summary["reason"] = res["result"]
            summaries.append(summary)
            continue
        reg = fetcher.register_event_items(res["items"], row["id"])
        summary.update({"ok": True, "items": len(res["items"]),
                        "created": reg["created"]})
        summaries.append(summary)
    return summaries
