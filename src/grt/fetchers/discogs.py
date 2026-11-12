"""Discogs API fetcher (fetcher kind: API).

Watches marketplace stats for editions that have a Discogs Release ID:
records price_snapshots (with the previous successful observation time)
and detects 0 -> 1+ listing appearances. Without a DISCOGS_TOKEN the
fetcher is disabled and says so - it never fabricates data.
"""
from __future__ import annotations

import json

import httpx

from .. import clock, config, health, outbox
from .base import BaseFetcher, FetchError, classify_http

API = "https://api.discogs.com"


class DiscogsFetcher(BaseFetcher):
    kind = "API"
    min_gap_seconds = 1.1  # sequential, low-rate by design

    def __init__(self, db, source_id: int, token=None):
        super().__init__(db, source_id)
        self.token = token if token is not None else config.discogs_token()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def stats_for_release(self, release_id: int) -> dict:
        self._polite()
        try:
            resp = httpx.get(
                API + "/marketplace/stats/" + str(release_id),
                headers={"Authorization": "Discogs token=" + str(self.token),
                         "User-Agent": "GameRecordTool/1.0"},
                timeout=30)
        except httpx.TimeoutException as exc:
            raise FetchError("TIMEOUT", str(exc))
        except httpx.HTTPError as exc:
            raise FetchError("NETWORK", str(exc))
        result = classify_http(resp.status_code)
        if result != "OK":
            raise FetchError(result, "HTTP " + str(resp.status_code),
                             http_status=resp.status_code)
        try:
            data = resp.json()
        except Exception as exc:  # broken / empty JSON body
            raise FetchError("PARSE_ERROR",
                             "stats JSON parse failed: " + str(exc)[:300])
        if not isinstance(data, dict):
            # An empty or non-object payload is not a successful stats
            # response and must never be recorded as one.
            raise FetchError("PARSE_ERROR", "stats payload is not an object")
        num = data.get("num_for_sale")
        if not isinstance(num, int) or isinstance(num, bool) \
                or num < 0:
            # W-check B2 (maniac A D-9): a 200 with an empty object
            # used to snapshot num/price as NULL and count as a
            # success — the price history gained 'observed blanks'.
            # The marketplace stats contract always carries
            # num_for_sale; without it the payload is not stats.
            # R14: a NEGATIVE count is not a count either.
            raise FetchError(
                "PARSE_ERROR",
                "stats payload has no integer num_for_sale")
        lowest = data.get("lowest_price")
        if lowest is not None and not isinstance(lowest, dict):
            # R9 maniac A observation 1: an abnormal lowest_price
            # shape (list etc.) used to surface later as a raw
            # AttributeError in snapshot_edition; validate it here,
            # symmetric with num_for_sale, so the failure is a
            # classified FetchError from the start.
            raise FetchError(
                "PARSE_ERROR",
                "stats payload lowest_price is not an object")
        if isinstance(lowest, dict):
            value = lowest.get("value")
            if value is not None:
                # W-check B3 (adversarial 2 / maniac A D-5): the
                # value itself was never validated — `true` was
                # recorded as a 1.0 price, "1,234" / "ask" were
                # stored as TEXT in the REAL column (and later
                # crashed /price's sorted() with a TypeError for
                # that edition, permanently), and Infinity flowed
                # into medians. A stats value is a JSON number or
                # the payload is not stats.
                from .base import api_price
                if isinstance(value, bool) \
                        or not isinstance(value, (int, float)):
                    raise FetchError(
                        "PARSE_ERROR",
                        "stats payload lowest_price.value is not a"
                        " usable number")
                # R14 (maniac A): value == 0 is an anomaly, but
                # discarding the whole payload also discarded a
                # valid num_for_sale observation (and blinded
                # appearance detection while it lasted). Zero is
                # normalised to "no price" in snapshot_edition;
                # negative / non-finite values remain corrupt.
                if value != 0 and api_price(value) is None:
                    raise FetchError(
                        "PARSE_ERROR",
                        "stats payload lowest_price.value is not a"
                        " usable number")
        return data

    def snapshot_edition(self, edition_id: str):
        """Take one stats snapshot for an edition. Returns the snapshot
        values, or None when the edition has no Discogs ID."""
        row = self.db.execute(
            "SELECT * FROM editions WHERE id=?", (edition_id,)).fetchone()
        if row is None or not row["discogs_id"]:
            return None
        # price_snapshots has no id column (schema) — the rowid is
        # the row's identity, and it is what the appearance episode
        # key below is built from.
        prev = self.db.execute(
            "SELECT rowid AS id, taken_at, num_for_sale FROM"
            " price_snapshots WHERE edition_id=? AND source='discogs'"
            " ORDER BY taken_at DESC, rowid DESC LIMIT 1",
            (edition_id,)).fetchone()
        try:
            release_id = int(row["discogs_id"])
        except (TypeError, ValueError):
            # R14 (adversarial): a dirty seed value ('12-345') used
            # to raise out of here on every run; an unparseable ID
            # is "no Discogs ID" for this edition.
            return None
        stats = self.stats_for_release(release_id)
        lowest_obj = stats.get("lowest_price") or {}
        lowest = lowest_obj.get("value")
        # R14 (maniac A): currency is sanitised, not trusted — a
        # non-string currency (dict) used to kill the whole
        # observation at INSERT time, and 'jpy' stored raw split
        # the /price series. A missing/unusable currency simply
        # means the price's currency is unknown.
        from ..normalize import normalize_currency
        raw_cur = lowest_obj.get("currency")
        currency = normalize_currency(raw_cur) \
            if isinstance(raw_cur, str) else None
        num = stats.get("num_for_sale")
        if lowest == 0:
            lowest, currency = None, None  # zero price = no price
        if (num or 0) == 0:
            # No copies for sale: there is no lowest price to
            # observe, whatever the payload claims (R14, maniac A).
            lowest, currency = None, None
        taken = clock.now_iso(self.db)
        self.db.execute(
            "INSERT INTO price_snapshots(edition_id, source, taken_at,"
            " prev_success_at, num_for_sale, lowest_price, currency)"
            " VALUES(?, 'discogs', ?, ?, ?, ?, ?)",
            (edition_id, taken, prev["taken_at"] if prev else None,
             num, lowest, currency))
        self.db.commit()
        if prev is not None and prev["num_for_sale"] is not None \
                and prev["num_for_sale"] == 0 and (num or 0) >= 1:
            # R14 (maniac A observation): a legacy NULL num_for_sale
            # is "not observed", not "observed zero" — appearance
            # requires an explicit zero on the previous snapshot.
            # W-check B3 (maniac A D-8): the episode marker is the
            # previous snapshot's ROW ID, not its timestamp — two
            # snapshots inside the same second shared a timestamp,
            # so the second appearance's key collided with the
            # first's and was suppressed as a duplicate.
            self._notify_appearance(row, num, lowest, currency,
                                    episode=prev["id"])
            # Agreement-13: a Discogs 0->1 is ONE repress signal
            # system. It can never fire repress_suspected alone —
            # repress.evaluate requires a second, independent system.
            try:
                from .. import repress
                repress.register_signal(
                    self.db, system="discogs_stats",
                    edition_id=edition_id,
                    detail=f"0->1 appearance, num_for_sale={num}")
                repress.evaluate(self.db, edition_id=edition_id,
                                 title=row["title"])
            except Exception:
                pass
        return {"num_for_sale": num, "lowest_price": lowest,
                "currency": currency, "taken_at": taken}

    def _notify_appearance(self, edition_row, num, lowest, currency,
                           episode: str | None = None) -> None:
        level = edition_row["watch_level"] or 0
        if level < 2:
            return  # price-record level: snapshot only, no notification
        url = ("https://www.discogs.com/sell/release/"
               + str(edition_row["discogs_id"]))
        # 9-C12 (evidence rule, spec sections 2-2 / 6.2): a price may be
        # attached to a notification body ONLY when price_confidence is
        # '高'. This appearance path never goes through the matcher, so
        # no price confidence exists here — the notification is an
        # existence notification: no price in the body, and nothing is
        # recorded into last_notified_price either. The observed lowest
        # price stays where it belongs: price_snapshots (history only).
        body = ("【出品出現】" + str(edition_row["title"]) + "\n"
                + "カタログ: " + str(edition_row["catalog"] or "-") + "\n"
                + "イベント: 新規出品（0→" + str(num) + "件）\n"
                + "リンク: " + url + "\n確信度: 存在・高 / 価格・-")
        # W-check B2 (collector C-B2-1 / maniac B D-9(ii)): the
        # idempotency key used to be fixed per edition+URL, so only
        # the FIRST appearance in the tool's lifetime was ever
        # notified — a sell-out followed by a re-listing months later
        # (the exact moment a collector wants to hear about) was
        # suppressed as a 'duplicate'. The key now carries the
        # episode marker: the timestamp of the zero-listing snapshot
        # that preceded this appearance. Distinct episodes have
        # distinct zero snapshots; one episode cannot re-fire,
        # because after it the latest snapshot is non-zero.
        key = outbox.listing_event_key(edition_row["id"], "discogs", url,
                                       "new_listing")
        if episode:
            key += "|" + str(episode)
        outbox.enqueue(
            self.db, event_type="new_listing", body=body,
            edition_id=edition_row["id"], source="discogs", listing_url=url,
            idempotency_key=key)

    def run_watchlist(self) -> dict:
        """Snapshot every watched edition that has a Discogs ID."""
        if not self.enabled:
            self.db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('fetcher_disabled', ?, ?)",
                ('{"fetcher": "discogs", "reason": "DISCOGS_TOKEN not set"}',
                 clock.now_iso(self.db)))
            self.db.commit()
            return {"enabled": False, "snapshotted": 0}
        # Blocked shelf (R9 maniac A, D-8 scope): this lane does not
        # go through BaseFetcher.run, so it must consult the shelf
        # itself — a shelved source is not probed again until the
        # 24h window passes. No ledger row: a skipped run is not a
        # fetch outcome.
        try:
            if health.is_shelved(self.db, self.source_id):
                return {"enabled": True, "snapshotted": 0,
                        "shelved": True}
        except Exception:
            pass
        started = clock.now_iso(self.db)
        rows = self.db.execute(
            "SELECT id FROM editions WHERE watch_level>0"
            " AND discogs_id IS NOT NULL").fetchall()
        done, failures = 0, 0
        result = "OK"
        for r in rows:
            try:
                self.snapshot_edition(r["id"])
                done += 1
            except FetchError as exc:
                failures += 1
                result = exc.result
                if exc.result == "BLOCKED":
                    # W-check B2 (maniac A D-1): 401/403/429 is a
                    # LANE-level stop signal (rate limit, dead token),
                    # not one edition's problem. The loop used to keep
                    # firing at every remaining edition — hundreds of
                    # requests against an API that just said stop.
                    self._item_error(r["id"], exc)
                    break
            except Exception as exc:  # fault containment: one bad edition
                # (broken payload shape, DB hiccup, ...) must not abort
                # the whole watchlist run or lose the failure record.
                failures += 1
                result = "PARSE_ERROR"
                self.db.rollback()
                self._item_error(r["id"], exc)
        # W-check B2 (maniac A D-2): the run's ledger entry used to
        # take the LAST failure's result for the whole run, so a run
        # that snapshotted 465 of 466 editions recorded total failure,
        # last_success_at never moved, and the lane marched to a false
        # DOWN. The run is OK when it produced snapshots; failures are
        # carried in error_detail and per-edition events. The lane now
        # also feeds the frequency ladder like every other lane, so
        # sustained failure actually backs it off.
        try:
            health.record_fetch_run(
                self.db, self.source_id,
                result="OK" if done > 0 else result,
                items_extracted=done, started_at=started,
                finished_at=clock.now_iso(self.db),
                error_detail=("failures=" + str(failures)) if failures
                else None,
                fetcher_kind="API")
        except Exception:
            try:
                self.db.rollback()
            except Exception:
                pass
        self._freq_outcome(done > 0 or failures == 0)
        return {"enabled": True, "snapshotted": done, "failures": failures}

    def _item_error(self, edition_id, exc: BaseException) -> None:
        try:
            self.db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('fetcher_item_error', ?, ?)",
                (json.dumps({"fetcher": "discogs",
                             "edition_id": str(edition_id),
                             "error": str(exc)[:300]},
                            ensure_ascii=False),
                 clock.now_iso(self.db)))
            self.db.commit()
        except Exception:
            try:
                self.db.rollback()
            except Exception:
                pass
