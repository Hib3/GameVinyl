"""Blip Blop RSS fetcher (fetcher kind: RSS).

New-release candidates from the Blip Blop feed are registered as
candidate_editions + review-queue entries and scored with the discovery
score method (agreement-15). Duplicates collapse on the fingerprint.
"""
from __future__ import annotations

import json

import feedparser

from .. import clock, outbox, scoring
from ..evidence import normalized_text_hash, save_raw_evidence
from ..normalize import fingerprint_for
from .base import BaseFetcher, FetchError, http_get

FEED_URL = "https://blipblop.net/feed/"


class BlipBlopFetcher(BaseFetcher):
    kind = "RSS"
    feed_url = FEED_URL

    def fetch_items(self, url=None):
        self._polite()
        resp = http_get(url or self.feed_url,
                        headers={"User-Agent": "GameRecordTool/1.0"})
        body = resp.text
        if not body or not body.strip():
            # An empty response is a failed fetch, never a success with
            # zero items (feedparser reports bozo=False for empty input,
            # so this must be rejected before parsing).
            raise FetchError("PARSE_ERROR", "empty feed response")
        ev = save_raw_evidence("blipblop", url or self.feed_url, body,
                               keep_body=False)
        self._stash_ev(ev)
        parsed = feedparser.parse(body)
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            raise FetchError("PARSE_ERROR", "feed parse failed")
        items = []
        for e in parsed.entries:
            items.append({
                "title": e.get("title", ""),
                "url": e.get("link", ""),
                "summary": e.get("summary", ""),
                "published": e.get("published", ""),
            })
        ev["normalized_hash"] = normalized_text_hash(body)
        return items, ev

    # Deterministic announcement-stage classification: a title or
    # summary announcing a re-press / reissue is the repress signal
    # system's input (spec C-10.2 system 2). Pattern matching only —
    # no AI in the product.
    _REPRESS_WORDS = ("再プレス", "リイシュー", "再発", "reissue",
                      "repress", "re-press")
    _PREORDER_WORDS = ("予約受付", "予約開始", "プレオーダー",
                       "preorder", "pre-order")
    # W-check B4 (seller): bare substring matching announced a
    # repress for 「再発予定はありません」 (an explicit NON-repress)
    # and for 「完売していたLP、再発売が決定」 (a resale of existing
    # stock — 再発売 — not a new pressing). Negation phrases veto
    # the stage, and 再発 only counts when it is not 再発売.
    # Residual limit, recorded honestly: a purely historical
    # mention (「1998年の再発盤と聴き比べ」) still classifies —
    # distinguishing mention from announcement needs reading
    # comprehension, which this product deliberately does not embed.
    _NEGATIONS = ("予定はありません", "予定はありませ",
                  "再発なし", "再プレスなし", "リイシューなし",
                  "ございません", "行いません", "行われません",
                  "しません", "未定", "予定なし")

    @classmethod
    def _event_stage(cls, item) -> str | None:
        import re as _re
        text = ((item.get("title") or "") + " "
                + (item.get("summary") or ""))
        # R19 (seller): the negation veto used to apply to the
        # WHOLE article — one "前作の再発予定はありません" clause
        # vetoed the same article's genuine announcement — and the
        # fixed 5-phrase list missed common forms (ございません /
        # 行いません / しません / 未定 / 予定なし). Classification
        # is now per CLAUSE: a clause carrying both a repress word
        # and a negation does not announce; any other clause with
        # a repress word does. 再発・販売開始 (a resale notice
        # with a nakaguro) no longer slips past the 再発売 guard.
        clauses = [c for c in _re.split(r"[。！？!?\n、,]", text)
                   if c.strip()]
        if not clauses:
            clauses = [text]
        for clause in clauses:
            low = clause.lower()
            if any(n in clause for n in cls._NEGATIONS):
                continue
            for w in cls._REPRESS_WORDS:
                if w == "再発":
                    if _re.search(r"再発(?!売|・)", clause):
                        return "repress_announced"
                elif w.lower() in low:
                    return "repress_announced"
        if any(w.lower() in text.lower()
               for w in cls._PREORDER_WORDS):
            return "preorder_open"
        return None

    # Titles that name a FORMAT, not a work: an announcement
    # titled only 'オリジナルサウンドトラック' cannot identify
    # WHICH game's disc it means, and bridging it by string
    # equality keyed another game's repress signal onto an
    # unrelated edition (R19, seller — reproduced firing a
    # repress_suspected for the wrong work).
    _GENERIC_TITLES = {"オリジナルサウンドトラック", "サウンドトラック",
                       "ORIGINALSOUNDTRACK", "SOUNDTRACK",
                       "ゲームミュージック", "GAMEMUSIC"}

    def _announcement_edition_id(self, title: str):
        """REPRESS key bridge (W-check B4): when an announcement's
        title EXACTLY names one catalogued edition (normalised
        equality, unique), the signal belongs to that edition's
        key space — otherwise the announcement system and the
        variant_stock system could never combine for one work.

        R19 (seller): the comparison now uses the same folded
        form as candidate fingerprints (・ folded) — the bridge
        used to miss editions the fingerprint lane considered the
        same work ('セガ・ゲーム…' vs 'セガゲーム…') — and generic
        format titles never bridge. Duplicate titles still do not
        bridge: with 706 duplicate groups in the seed catalogue a
        title alone cannot say WHICH pressing was announced, and
        a wrong bridge manufactures evidence for the wrong work;
        those announcements stay candidate-keyed and combine when
        the candidate is catalogued."""
        from ..normalize import normalize_title
        import re as _re
        # Headline decorations are not part of the work's name
        # (R19, seller): 【再プレス】X, X（再プレス盤）, X - 再プレス
        # 決定, X LP all name the same edition as X.
        t = (title or "").strip()
        for _ in range(2):
            t = _re.sub(r"^[【\[][^】\]]*[】\]]\s*", "", t)
            t = _re.sub(r"[（(][^）)]*[）)]\s*$", "", t).strip()
        t = _re.sub(r"\s+-\s+.*$", "", t)
        t = _re.sub(r"\s+LP$", "", t, flags=_re.IGNORECASE)
        want = normalize_title(t).replace("・", "")
        if not want or want in self._GENERIC_TITLES:
            return None
        hits = [r[0] for r in self.db.execute(
            "SELECT id, title FROM editions").fetchall()
            if normalize_title(r[1]).replace("・", "") == want]
        return hits[0] if len(hits) == 1 else None

    def register_candidates(self, items) -> dict:
        created, notified, failed = 0, 0, 0
        for item in items:
            # W-check B4 (adversarial QA): this lane had NO per-item
            # containment — one poisoned entry (a lone-surrogate
            # title raising UnicodeEncodeError at bind time) killed
            # the whole batch at the same position every cycle, so
            # everything behind it NEVER registered. Mirror the
            # ingest lane: contain, log, continue; commit per item.
            try:
                c, n = self._register_one(item)
                created += c
                notified += n
                self.db.commit()
            except Exception as exc:
                self.db.rollback()
                failed += 1
                try:
                    self.db.execute(
                        "INSERT INTO system_events(kind, detail_json,"
                        " created_at) VALUES('candidate_register_failed',"
                        " ?, ?)",
                        (json.dumps({"lane": "blipblop",
                                     "error": str(exc)[:200]}),
                         clock.now_iso(self.db)))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
        return {"created": created, "notified_immediate": notified,
                "failed": failed}

    def _register_one(self, item) -> tuple[int, int]:
        fp = fingerprint_for(item["title"], None)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO candidate_editions("
            " title, raw_title, raw_description, source_url,"
            " fingerprint, existence_confidence, price_confidence,"
            " found_at, first_seen, last_seen, found_via, status)"
            " VALUES(?, ?, ?, ?, ?, '中', '低', ?, ?, ?, 'blipblop_rss',"
            " 'pending')",
            (item["title"], item["title"], item.get("summary"),
             item["url"], fp, clock.now_iso(self.db),
             clock.now_iso(self.db), clock.now_iso(self.db)))
        if cur.rowcount == 0:
            from ..candidates import note_candidate_source
            note_candidate_source(self.db, fp, item.get("url"))
            # R19 (seller, 重大): a FOLLOW-UP article about an
            # already-known candidate (the usual shape of repress
            # news — first a release notice, later the repress
            # decision) used to end here: no announcements row, no
            # REPRESS signal, the follow-up was silently dropped.
            # The announcement layer records it like a first
            # sighting (deduped per article URL), and a
            # repress_announced stage signals as usual.
            crow = self.db.execute(
                "SELECT id FROM candidate_editions WHERE"
                " fingerprint=?", (fp,)).fetchone()
            if crow is not None:
                cid0 = int(crow[0])
                stage0 = self._event_stage(item)
                seen = self.db.execute(
                    "SELECT 1 FROM announcements WHERE"
                    " candidate_id=? AND url=?",
                    (cid0, item["url"])).fetchone()
                if seen is None:
                    self.db.execute(
                        "INSERT INTO announcements(source, url,"
                        " title_raw, found_at, candidate_id,"
                        " event_stage) VALUES('blipblop', ?, ?, ?,"
                        " ?, ?)",
                        (item["url"], item["title"],
                         clock.now_iso(self.db), cid0, stage0))
                if stage0 == "repress_announced":
                    from .. import repress
                    ed = self._announcement_edition_id(item["title"])
                    if ed is not None:
                        repress.signal_announcement(
                            self.db, event_stage=stage0,
                            edition_id=ed, candidate_id=cid0,
                            title=item["title"])
                    else:
                        repress.signal_announcement(
                            self.db, event_stage=stage0,
                            candidate_id=cid0, title=item["title"])
            return 0, 0
        cid = cur.lastrowid
        stage = self._event_stage(item)
        self.db.execute(
            "INSERT INTO announcements(source, url, title_raw, found_at,"
            " candidate_id, event_stage) VALUES('blipblop', ?, ?, ?,"
            " ?, ?)",
            (item["url"], item["title"], clock.now_iso(self.db), cid,
             stage))
        if stage == "repress_announced":
            # REPRESS producer wiring (review fix): the counted
            # announcement system now has a real caller. W-check B4:
            # when the title uniquely names a catalogued edition the
            # signal is keyed to the EDITION (the space variant_stock
            # lives in); only otherwise does it stay candidate-keyed.
            from .. import repress
            ed = self._announcement_edition_id(item["title"])
            if ed is not None:
                # R19: the row carries BOTH keys — the edition key
                # is what combines with variant_stock, the
                # candidate key is the provenance (it is what lets
                # a later rejection withdraw exactly this
                # candidate's contribution). The candidate window
                # counts candidate-ONLY rows, so the shared row is
                # never double-counted.
                repress.signal_announcement(
                    self.db, event_stage=stage, edition_id=ed,
                    candidate_id=cid, title=item["title"])
            else:
                repress.signal_announcement(
                    self.db, event_stage=stage, candidate_id=cid,
                    title=item["title"])
        # W-check B4 (wholesaler): the computed stage never reached
        # scoring — the cand dict omitted event_stage, so the stage
        # floor/boost was dead on this lane (a repress announcement
        # scored the generic 40). It is passed now.
        cand = {"title": item["title"], "country": None,
                "source_country": None, "existence_confidence": "中",
                "event_stage": stage}
        sc = scoring.discovery_score(self.db, cand)
        payload = json.dumps({"candidate_id": cid, "source": "blipblop"})
        self.db.execute(
            "INSERT INTO review_queue(kind, payload_json, fingerprint,"
            " priority, created_at) VALUES('candidate_review', ?, ?, ?, ?)",
            (payload, fp, scoring.review_priority(self.db, cand),
             clock.now_iso(self.db)))
        # 9-C22/C4: every queued candidate review also queues ONE
        # button-carrying outbox message (approve:/reject: callbacks
        # that bot.py already handles). This only *queues* — actual
        # delivery follows the existing outbox gate/quiet rules.
        outbox.enqueue_candidate_review(self.db, cid)
        notified = 0
        if sc["band"] == "immediate":
            from ..normalize import one_line
            body = ("【新譜候補】" + one_line(item["title"]) + "\n"
                    + "イベント: 新譜告知（Blip Blop）\nリンク: "
                    + str(item["url"]) + "\n"
                    + "確信度: 存在・中 / 価格・低（スコア"
                    + str(sc["score"]) + "）")
            if outbox.enqueue(
                    self.db, event_type="announcement", body=body,
                    idempotency_key="announcement|blipblop|" + str(item["url"]),
                    score=sc["score"], floor_applied=sc["floor_applied"]):
                notified = 1
        return 1, notified

