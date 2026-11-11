"""Digest builders and the 21:45 deadline sweep (agreements 07/13/18).

The daily health digest is sent every morning, including zero-notification
days. The discovery digest never truncates by count; overflow is carried
to the next day with the count reported.
"""
from __future__ import annotations

from datetime import timedelta

from . import clock, health, outbox as outbox_mod
from .config import get_setting

FLEA_NOTE = ("注記: フリマ系ソース（メルカリ／Yahoo!オークション等）の新着検出は"
             "公式の保存検索アラート依存であり、本システムの観測対象外です。")

# Fallback for fetch_runs recorded before fetcher_kind existed (or by
# direct health.record_fetch_run calls): map the well-known Phase 1
# fetcher source names to their fetcher kind. Anything else is reported
# as 'unknown' — NEVER as sources.kind, whose taxonomy (db/shop/
# community/...) is a different system the spec forbids mixing in.
_LEGACY_FETCHER_KIND_BY_SOURCE_NAME = {"discogs": "API", "blipblop": "RSS"}


def _fetcher_kind_counts(db, since_iso: str) -> list[tuple[str, int, int, int]]:
    """(fetcher_kind, ok, fail, blocked) over the window, fetcher-kind
    taxonomy only (9-B13)."""
    health.ensure_fetcher_kind_column(db)
    rows = db.execute(
        "SELECT f.fetcher_kind AS fk, s.name AS source_name, f.result AS result"
        " FROM fetch_runs f LEFT JOIN sources s ON s.id=f.source_id"
        " WHERE f.started_at>=?", (since_iso,)).fetchall()
    agg: dict[str, list[int]] = {}
    for r in rows:
        kind = r["fk"] or _LEGACY_FETCHER_KIND_BY_SOURCE_NAME.get(
            r["source_name"] or "", "unknown")
        if kind not in health.FETCHER_KINDS:
            kind = "unknown"
        bucket = agg.setdefault(kind, [0, 0, 0])
        if r["result"] == "OK":
            bucket[0] += 1
        elif r["result"] == "BLOCKED":
            bucket[2] += 1
        else:
            bucket[1] += 1
    return [(k, v[0], v[1], v[2]) for k, v in sorted(agg.items())]


def build_health_digest(db) -> str:
    now = clock.now(db)
    since = clock.to_iso(now - timedelta(hours=24))
    lines = ["【日次ヘルスレポート】" + now.strftime("%Y-%m-%d")]

    # 9-A2(2): DOWN sources are pinned at the TOP of the digest, before
    # every other section — not buried after the fetch results.
    down = db.execute(
        "SELECT s.name FROM source_health h JOIN sources s ON s.id=h.source_id"
        " WHERE h.state='DOWN'").fetchall()
    if down:
        lines.append("■ DOWN中のソース: " + ", ".join(r[0] for r in down))

    counts = _fetcher_kind_counts(db, since)
    lines.append("■ 取得結果（過去24h・種別ごと 成功/失敗/BLOCKED）")
    if counts:
        for kind, ok, fail, blocked in counts:
            lines.append("  " + kind + ": " + str(ok) + "/" + str(fail)
                         + "/" + str(blocked))
    else:
        lines.append("  （取得実績なし）")

    dead = outbox_mod.dead_letter_count(db)
    backlog = outbox_mod.pending_backlog(db)
    lines.append("■ 通知: dead_letter " + str(dead) + "件 / 未送信バックログ "
                 + str(backlog) + "件")

    # W-check B5 (maniac B F-7): duplicate_suspects is stacked by
    # the matcher 'for human review' (agreement-02 collisions),
    # but NO surface ever read it — no bot command, no digest, no
    # diagnose line — so catalogue collisions accumulated
    # invisibly forever. The count is surfaced here (the daily
    # artefact the user actually reads); a resolution UI remains
    # unbuilt by design and is recorded as such.
    # R24 (maniac B N-4): the label says 未確認 (unresolved) —
    # count only unresolved rows, so a future resolution path
    # (or a manual fix) cannot leave the count permanently
    # inflated.
    _suspects = db.execute(
        "SELECT COUNT(*) FROM duplicate_suspects"
        " WHERE resolved_at IS NULL").fetchone()[0]
    if _suspects:
        lines.append("■ 目録の衝突疑い（未確認）: " + str(_suspects)
                     + "件（matcherが人間確認用に記録。解消UIは"
                     "未実装）")

    unproc = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL"
    ).fetchone()[0]
    oldest = db.execute(
        "SELECT MIN(created_at) FROM review_queue WHERE resolved_at IS NULL"
    ).fetchone()[0]
    lines.append("■ レビュー未処理: " + str(unproc) + "件")
    # W-check B4 (maniac B): the headline mixed every kind into one
    # number with no breakdown, so structurally-unresolvable kinds
    # (which B4 gave exits to) could not be told apart from real,
    # actionable backlog in the morning digest. /queue shows the
    # same breakdown; now the digest does too.
    for kind_name, cnt in db.execute(
            "SELECT kind, COUNT(*) FROM review_queue"
            " WHERE resolved_at IS NULL GROUP BY kind"
            " ORDER BY kind").fetchall():
        lines.append("  - " + str(kind_name) + ": " + str(cnt) + "件")
    if unproc > 50:
        lines.append("  警告: バックログ警告: 未処理が50件を超えています")
    if oldest:
        try:
            age_days = (now - clock.parse_iso(oldest)).days
        except ValueError:
            # An unparseable stored timestamp must not kill the daily
            # digest (debugger-14 finding, 2026-11-08); skip the age
            # warning for it.
            age_days = 0
        if age_days > 7:
            lines.append("  警告: バックログ警告: 最古の未処理が"
                         + str(age_days) + "日経過")

    demotions = db.execute(
        "SELECT detail_json FROM system_events WHERE kind IN"
        " ('auto_demotion','source_degraded','freq_demotion',"
        "'freq_promotion') AND created_at>=?",
        (since,)).fetchall()
    if demotions:
        lines.append("■ 自動降格・劣化報告: " + str(len(demotions)) + "件")
        for r in demotions:
            lines.append("  " + str(r[0]))

    # W-check B2 (collector C-B2-10): this count used to be a
    # LIFETIME total — an override fired once was reported in every
    # digest forever, next to sections explicitly labelled 'last
    # 24h'. notification_outbox has no created_at; for override rows
    # not_before IS the enqueue time (the override bypasses quiet
    # holding), so the period filter uses it.
    ov_total = db.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE quiet_override=1"
        " AND not_before>=?", (since,)).fetchone()[0]
    if ov_total:
        lines.append("■ 静穏例外の発動: " + str(ov_total) + "件")
        reasons = db.execute(
            "SELECT quiet_override_reason, COUNT(*) FROM notification_outbox"
            " WHERE quiet_override=1 AND not_before>=?"
            " GROUP BY quiet_override_reason", (since,)).fetchall()
        for reason, cnt in reasons:
            lines.append("  理由: " + str(reason) + " x" + str(cnt))

    # W-check B4 (collector): the headline counted only the
    # confidence axis (confidence != 'confirmed') while the
    # breakdown below partitions TWO axes — one row could make the
    # breakdown total 2 against a headline of 1. The headline is
    # now the distinct-row count under the same definition the
    # consumers use: a row is unconfirmed unless it is confirmed
    # AND kind-classified AND carries an instant (and not
    # superseded).
    from . import deadlines_ops as _dops
    unconf = _dops.unconfirmed_count(db)
    lines.append("■ 締切未確定（要確認）: " + str(unconf) + "件")
    from . import deadlines_ops
    by_kind = deadlines_ops.unconfirmed_summary(db)
    for kind_name, cnt in sorted(by_kind.items()):
        lines.append("  - " + str(kind_name) + ": " + str(cnt) + "件"
                     "（アラート対象外・要確認のまま再掲）")
    for dline in deadlines_ops.deadline_digest_lines(db):
        lines.append("■ " + dline)

    # Phase 2 acceptance #10: an event inside its harvest window
    # with ZERO successful fetches is a mandatory declaration.
    # Review fix (AI-engineer persona): the check is PER EVENT and
    # spans the whole harvest window — the first version pooled all
    # events' runs over 24h, so one healthy event hid another
    # event's total failure.
    today = now.strftime("%Y-%m-%d")
    events = db.execute(
        "SELECT id, name, harvest_from FROM event_catalog WHERE"
        " harvest_from<=? AND harvest_to>=?", (today, today)).fetchall()
    for ev in events:
        src = db.execute(
            "SELECT id FROM sources WHERE name=?",
            ("doujin_event:" + str(ev["name"]),)).fetchone()
        ok_runs = 0
        if src is not None:
            ok_runs = db.execute(
                "SELECT COUNT(*) FROM fetch_runs WHERE result='OK'"
                " AND fetcher_kind='DOUJIN_EVENT' AND source_id=?"
                " AND started_at>=?",
                (src[0], str(ev["harvest_from"]))).fetchone()[0]
        # W-check B4 (maniac A): the check keys on the event NAME —
        # renaming an event (a postponement usually renames it)
        # made the declaration fire against the new name until its
        # first successful harvest, crying wolf in the most-trusted
        # section of the digest. Grace: before any harvest has even
        # been ATTEMPTED under the current name (no source row) the
        # declaration waits until the window has been open 2 days;
        # once attempts exist under this name, zero successes is
        # the real anomaly and declares immediately.
        if ok_runs == 0 and src is None:
            try:
                from datetime import date as _date
                opened = _date.fromisoformat(str(ev["harvest_from"]))
                if (now.date() - opened).days < 2:
                    continue
            except ValueError:
                pass
            # R19 (maniac A): the grace keyed only on harvest_from,
            # so a rename OBSERVED mid-window (the harvest loop
            # records 'event_renamed' when an event id's name
            # changes) still false-fired once the window was 2+
            # days old. The grace restarts at the observed rename.
            # A rename no harvest has observed yet is
            # indistinguishable from a never-harvested event and
            # still declares — that is the mandatory declaration
            # working as specified.
            import json as _json
            renamed_recently = False
            for (detail,) in db.execute(
                    "SELECT detail_json FROM system_events"
                    " WHERE kind='event_renamed'").fetchall():
                try:
                    d = _json.loads(detail or "")
                except (ValueError, TypeError):
                    continue
                if not isinstance(d, dict) \
                        or d.get("event_id") != ev["id"]:
                    continue
                try:
                    seen = clock.parse_iso(d.get("at") or "")
                except (ValueError, TypeError, AttributeError):
                    continue
                if seen is not None \
                        and (now - seen).days < 2:
                    renamed_recently = True
            if renamed_recently:
                continue
        if ok_runs == 0:
            lines.append("■ イベントharvest異常（必須申告）: "
                         + str(ev["name"])
                         + " の収穫期間中ですが成功取得が0回です")

    gaps = db.execute(
        "SELECT scope, gap_start FROM observation_gaps WHERE declared_at>=?",
        (since,)).fetchall()
    if gaps:
        lines.append("■ 観測空白の発生:")
        for g in gaps:
            lines.append("  " + str(g["scope"]) + "（" + str(g["gap_start"]) + "〜）")
        lines.append("  ※空白期間中に出て消えた出品は見逃している可能性があります。")

    blocked = db.execute("SELECT COUNT(*) FROM blocked_sources").fetchone()[0]
    lines.append("■ ブロック中ソース棚: " + str(blocked)
                 + "件（存在しないのではなく到達不能）")
    lines.append(FLEA_NOTE)
    return "\n".join(lines)


_BAND_SECTIONS = [
    ("immediate", "■ 即時帯（スコア60以上／floor適用）"),
    ("digest_top", "■ ダイジェスト先頭帯（スコア30-59）"),
    ("digest", "■ 通常ダイジェスト帯（スコア30未満）"),
]


def _candidate_band(db, row) -> tuple[str, int]:
    """Deterministic band for a stored candidate, recomputed with the
    same scoring entry point registration used (agreement-15)."""
    from . import scoring
    # W-check B4 (wholesaler): two scoring inputs existed only at
    # registration time, so the digest band silently diverged from
    # the registration band — the announcement stage (the
    # candidate's latest announcements row) and the independent
    # source count (its aggregated hosts) are reconstructed here.
    # R19 (wholesaler): the LATEST announcement row decides the
    # current stage, including a later stage=NULL row (a follow-up
    # article that is not itself an announcement) — filtering to
    # non-NULL rows kept the highest stage the candidate ever had.
    stage = None
    ann = db.execute(
        "SELECT event_stage FROM announcements WHERE candidate_id=?"
        " ORDER BY id DESC LIMIT 1",
        (row["id"],)).fetchone()
    if ann is not None:
        stage = ann[0]
    from .candidates import independent_source_count
    cand = {
        "title": row["raw_title"] or row["title"],
        "catalog_norm": row["catalog_norm"],
        "existence_confidence": row["existence_confidence"],
        "distribution_type": row["distribution_type"],
        "event_stage": stage,
        "independent_sources": independent_source_count(db, row["id"]),
        "country": None,
    }
    sc = scoring.discovery_score(db, cand)
    return sc["band"], sc["score"]


def _carry_over_count(db) -> int:
    """Pending candidates found before today: they were already shown in
    an earlier discovery digest and are carried into today's (9-C8)."""
    now = clock.now(db)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for r in db.execute(
            "SELECT found_at FROM candidate_editions WHERE status='pending'"
    ).fetchall():
        try:
            found = clock.parse_iso(r[0])
        except (ValueError, TypeError):
            # W-check B2 (collector C-B2-8): one candidate with a
            # damaged found_at killed the discovery digest EVERY day
            # (the health digest's twin code was guarded; this one
            # was not). A corrupt timestamp is skipped, not fatal.
            continue
        if found is not None and found < today_start:
            n += 1
    return n


def build_discovery_digest(db, carry_over: int | None = None) -> str:
    now = clock.now(db)
    if carry_over is None:
        carry_over = _carry_over_count(db)
    lines = ["【発見ダイジェスト】" + now.strftime("%Y-%m-%d")]
    if carry_over:
        lines.append("（前日からの繰越: " + str(carry_over) + "件）")
    rows = db.execute(
        "SELECT * FROM candidate_editions WHERE status='pending'"
        " ORDER BY found_at ASC").fetchall()
    lines.append("未承認の発見候補: " + str(len(rows))
                 + "件（件数で切り捨てません。全件表示）")
    banded: dict[str, list[tuple]] = {b: [] for b, _ in _BAND_SECTIONS}
    for r in rows:
        band, score = _candidate_band(db, r)
        banded.setdefault(band, []).append((r, score))
    for band, heading in _BAND_SECTIONS:
        entries = banded.get(band, [])
        lines.append(heading + ": " + str(len(entries)) + "件")
        for r, score in entries:
            # W-check B3 (adversarial 6): candidate titles are
            # third-party text; a title with embedded newlines used
            # to insert fake alert lines into this digest body.
            # W-check B5 (adversarial QA 軽微-3): the title was
            # also LENGTH-unbounded here — one 200,000-char title
            # from an approved feed turned the daily digest into
            # ~52 chunked messages, re-sent every morning while
            # unresolved. The push path caps at 120 chars; so does
            # the digest now.
            from .normalize import one_line
            _title = one_line(r["raw_title"] or r["title"])
            if len(_title) > 120:
                _title = _title[:120] + "…"
            lines.append("- [" + str(r["id"]) + "] "
                         + _title
                         + " 存在:" + str(r["existence_confidence"])
                         + " 価格:" + str(r["price_confidence"])
                         + " スコア:" + str(score)
                         + " " + str(r["source_url"] or ""))
    if carry_over:
        lines.append("■ 繰越申告: 前日以前に発見され未処理のまま"
                     "繰り越された候補 " + str(carry_over)
                     + "件を本ダイジェストに再掲しました")
    from . import repress
    notes = repress.digest_notes(db)
    if notes:
        lines.append("■ REPRESS信号（単独系統は即時通知せず様子見）")
        for note in notes:
            lines.append("  " + note)
    return "\n".join(lines)


def send_daily_digests(db) -> dict:
    day = clock.now(db).strftime("%Y-%m-%d")
    health_id = outbox_mod.enqueue(
        db, event_type="health_digest", body=build_health_digest(db),
        idempotency_key="health_digest|" + day, quiet_exempt=True)
    disc_id = outbox_mod.enqueue(
        db, event_type="discovery_digest",
        body=build_discovery_digest(db, carry_over=_carry_over_count(db)),
        idempotency_key="discovery_digest|" + day, quiet_exempt=True)
    return {"health": health_id, "discovery": disc_id}


def _quiet_override_for_sweep(db, row, deadline_dt, quiet_end) -> bool:
    """Agreement-28 quiet-exception wiring for one sweep row.

    Deterministic narrow conditions, all required: the user explicitly
    selected the exception ON (setting deadline_override_quiet='1');
    deadline_kind is hard_cutoff (the only kind eligible); the deadline
    is within 24h; the edition is made_to_order OR matched by a
    priority-1 want; and the deadline falls no later than 3h after the
    quiet period ends (waiting out the quiet period would leave <3h —
    or no time at all — before the deadline).
    """
    if get_setting(db, "deadline_override_quiet") != "1":
        return False
    if row["deadline_kind"] != "hard_cutoff":
        return False
    now = clock.now(db)
    if not (now < deadline_dt <= now + timedelta(hours=24)):
        return False
    if deadline_dt > quiet_end + timedelta(hours=3):
        return False
    edition_id = row["edition_id"]
    if edition_id:
        ed = db.execute(
            "SELECT distribution_channel FROM editions WHERE id=?",
            (edition_id,)).fetchone()
        if ed is not None and ed[0] == "made_to_order":
            return True
        want = db.execute(
            "SELECT 1 FROM wantlist WHERE edition_id=? AND priority=1",
            (edition_id,)).fetchone()
        if want:
            return True
    candidate_id = row["candidate_id"]
    if candidate_id:
        cand = db.execute(
            "SELECT distribution_channel FROM candidate_editions WHERE id=?",
            (candidate_id,)).fetchone()
        if cand is not None and cand[0] == "made_to_order":
            return True
    return False


def deadline_sweep(db) -> int:
    """21:45 sweep: confirmed deadlines falling before next 06:30 are
    notified in the immediate band (agreement-18).

    Agreement-22 single table: only confirmed rows are inputs at all,
    and deadline_kind campaign_end / unknown (including NULL, which is
    unclassified) are NOT sweep targets — hard_cutoff and
    guarantee_cutoff are. The blanket quiet exemption is gone: rows go
    through the normal quiet holding, except rows meeting the
    agreement-28 narrow conditions while the quiet exception is ON,
    which are enqueued with quiet_override=1 and its reason recorded
    (and therefore reported in the next health digest).
    """
    now = clock.now(db)
    # W-check B2 (collector C-B2-7): the horizon used to be a
    # hard-coded next-06:30, ignoring the user's configured quiet
    # end — with quiet ending at 08:00, deadlines between 06:30 and
    # 08:00 were silently outside the sweep. The horizon is now the
    # end of the quiet window that starts tonight, computed from the
    # policy (with the default as fallback when tonight is not a
    # quiet night at all).
    probe = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if probe <= now:
        probe += timedelta(days=1)
    horizon = outbox_mod.quiet_window_end(db, probe)
    if horizon is None:
        horizon = now.replace(hour=6, minute=30, second=0, microsecond=0)
        if now.hour >= 7:
            horizon += timedelta(days=1)
    rows = db.execute(
        "SELECT * FROM release_schedule WHERE deadline_confidence='confirmed'"
        " AND COALESCE(deadline_kind, 'unknown') NOT IN ('campaign_end',"
        " 'unknown')"
        " AND deadline_jst IS NOT NULL AND deadline_jst>? AND deadline_jst<=?"
        " AND superseded_by IS NULL",
        (clock.to_iso(now), clock.to_iso(horizon))).fetchall()
    sent = 0
    for r in rows:
        # W-check B4 (collector): the sweep body named NO target —
        # just a schedule number the user then had to cross-check
        # in /deadlines before acting. The edition/candidate title
        # now rides in the body, like the staged alerts carry.
        # R19 (maniac A): a schedule whose candidate was rejected
        # is a dead discovery — the sweep must not alert for it.
        if not r["edition_id"]:
            from . import deadlines_ops as _dops
            if _dops.candidate_is_rejected(db, r["candidate_id"]):
                continue
        # R19: a sibling row (same deadline via another channel)
        # already swept tonight — one deadline, one sweep notice.
        from . import deadlines_ops as _dops2
        sib_swept = _dops2._any_key_in_outbox(db, [
            "deadline_sweep|" + str(sid) + "|" + str(r["deadline_jst"])
            for sid in _dops2._sibling_ids(db, r)])
        if sib_swept:
            continue
        who = ""
        if r["edition_id"]:
            t = db.execute("SELECT title FROM editions WHERE id=?",
                           (r["edition_id"],)).fetchone()
            # R19 (collector): (t[0] if t else "") is None when the
            # row exists but its title is NULL — one such edition
            # crashed the whole sweep with a TypeError and NO
            # deadline was notified that night.
            who = str(r["edition_id"]) + " " \
                + ((t[0] or "") if t else "")
        elif r["candidate_id"]:
            t = db.execute(
                "SELECT title FROM candidate_editions WHERE id=?",
                (r["candidate_id"],)).fetchone()
            who = "候補#" + str(r["candidate_id"]) + " " \
                + ((t[0] or "") if t else "")
        from .normalize import one_line
        body = ("【締切間近】" + one_line(who).strip()
                + " スケジュール#" + str(r["id"])
                + " チャネル:" + str(r["channel"])
                + " 種別:" + str(r["deadline_kind"])
                + " 締切:" + str(r["deadline_jst"])
                + "（原文:" + one_line(str(r["deadline_raw"])) + "）")
        try:
            deadline_dt = clock.parse_iso(r["deadline_jst"])
        except ValueError:
            # One unparseable stored deadline must not kill the whole
            # sweep (debugger-14 finding, 2026-11-08): the row is still
            # notified from its stored text, just without the
            # quiet-override evaluation.
            deadline_dt = None
        override = deadline_dt is not None and _quiet_override_for_sweep(
            db, r, deadline_dt, horizon)
        oid = outbox_mod.enqueue(
            db, event_type="deadline_sweep", body=body,
            edition_id=r["edition_id"],
            idempotency_key="deadline_sweep|" + str(r["id"]) + "|"
            + str(r["deadline_jst"]),
            quiet_override=override,
            quiet_override_reason=(
                "deadline_sweep:hard_cutoff_within_24h" if override else None))
        if oid:
            sent += 1
    # Run marker: with zero matching deadlines a sweep leaves no
    # outbox trace, so the startup catch-up (main) needs this event
    # to know tonight's sweep already ran (W-check B2, wholesaler
    # D-2/D-3).
    try:
        db.execute(
            "INSERT INTO system_events(kind, detail_json, created_at)"
            " VALUES('deadline_sweep_run', ?, ?)",
            ('{"date": "' + now.strftime("%Y-%m-%d") + '", "sent": '
             + str(sent) + "}", clock.to_iso(now)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    # Slot record (R9 adversarial QA, D-4 residual): the catch-up
    # logic keys on the 21:45 SLOT a run belongs to, not the run's
    # calendar date — a sweep executed at 01:00 covers yesterday's
    # slot, and only a slot-keyed record lets the next catch-up see
    # that it already ran.
    try:
        from .config import set_setting
        set_setting(db, "last_deadline_sweep_slot",
                    sweep_slot_date(now))
    except Exception:
        pass
    return sent


def sweep_slot_date(now) -> str:
    """The 21:45 slot a moment belongs to: today's date once 21:45
    has passed, yesterday's date before that."""
    if (now.hour, now.minute) >= (21, 45):
        return now.strftime("%Y-%m-%d")
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")
