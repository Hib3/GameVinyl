"""Resident main process.

Jobs: heartbeat every 5 minutes; outbox delivery each minute; source
health time-based evaluation every 15 minutes; Discogs watchlist snapshot
and Blip Blop feed check on their watch rules; daily digests at 07:00;
the 21:45 deadline sweep; Telegram long polling in a worker thread.

Until the initial setup gate is open (quiet-exception explicitly chosen),
fetch/digest jobs do not run — only the bot stays up to receive the
choice (agreement-28 / acceptance #10).

W-check B2 rework (2026-11-08). The resident layer's structural holes,
each reproduced by the review personas:

- ALL scheduler jobs shared ONE sqlite connection across the
  scheduler's thread pool with no lock — concurrent execute/commit
  on one connection corrupts under load (182 errors / 1600 ops in
  the adversarial repro). Every job now opens its OWN connection
  for its run and closes it after.
- APScheduler's defaults (misfire_grace_time=1s, no listener, no
  logging) silently DROPPED the 07:00 digest and the 21:45 sweep
  whenever the PC slept through them, and nothing recorded the
  loss. Cron jobs now carry generous grace times, a listener
  records misses/errors as system events, and startup runs a
  catch-up for the digest / sweep / staged alerts.
- The poll loop persisted the update offset BEFORE handling the
  update, and a transient Telegram failure while replying was
  classified 'invalid_update' — commands were lost permanently.
  The offset now advances only after a non-transient outcome, and
  the fetch side backs off progressively, honouring retry_after.
- The fixed-period lanes (Discogs / Blip Blop / doujin harvest)
  never consulted the frequency ladder; they do now.
"""
from __future__ import annotations

import json
import re
import threading
import time
from contextlib import closing
from datetime import datetime

from apscheduler.events import (EVENT_JOB_ERROR, EVENT_JOB_MAX_INSTANCES,
                                EVENT_JOB_MISSED)
from apscheduler.schedulers.blocking import BlockingScheduler

from . import bot as bot_mod
from . import clock, config, db as dbmod, digest, health, logtrim, outbox
from . import version as version_mod
from .config import get_setting
from .fetchers.blipblop import BlipBlopFetcher
from .fetchers.discogs import DiscogsFetcher
from .telegram_io import HttpTransport, fetch_updates

_TELEGRAM_TOKEN_RE = re.compile(r"(?i)(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}")


def _redact_poll_error(text: str) -> str:
    return _TELEGRAM_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", text)

# The heartbeat job fires every 5 minutes. A startup that finds the
# previous heartbeat older than twice that interval knows scheduler
# runs were missed meanwhile (PC sleep, power off, a kill) — the same
# "2x the scheduled interval" rule the health layer uses for silence.
HEARTBEAT_INTERVAL_MINUTES = 5
STARTUP_GAP_THRESHOLD_MINUTES = 2 * HEARTBEAT_INTERVAL_MINUTES

# Scheduler misfire grace (seconds). Interval jobs refire within
# minutes anyway; the fixed-time cron jobs are the ones a sleeping
# PC loses, so they get hours — plus the startup catch-up below.
GRACE_INTERVAL_S = 300
GRACE_DAILY_CRON_S = 3 * 3600
GRACE_WEEKLY_CRON_S = 12 * 3600
GRACE_QUARTERLY_CRON_S = 24 * 3600


def record_startup_gap(db, now=None) -> dict | None:
    """Record the scheduled-vs-actual gap left by the previous process.

    QC round-9 item 9-A2(3) / agreement-07: scheduler misses (sleep
    etc.) must be detected at the NEXT startup as the difference
    between the scheduled and actual run times and recorded as an
    observation gap. Reads the heartbeat row BEFORE run() overwrites
    it; a stale heartbeat (>2x the heartbeat interval) produces one
    observation_gaps row (scope='main_process', gap_start=previous
    alive_at, gap_end=this boot) plus one system_events row
    (kind='startup_observation_gap'). Returns the recorded detail, or
    None when there is no previous heartbeat / it is not stale / this
    exact gap was already recorded. Idempotent per gap_start.
    """
    row = db.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    if row is None or not row["alive_at"]:
        return None
    try:
        prev = clock.parse_iso(row["alive_at"])
    except (ValueError, TypeError):
        # A heartbeat timestamp no shipped writer produces (hand-edited
        # or damaged DB) must not kill startup: this bookkeeping runs
        # before polling, unguarded, by design of the prologue — so it
        # has to be robust against malformed input on its own.
        return None
    if prev is None:
        return None
    current = now or clock.now(db)
    gap_seconds = (current - prev).total_seconds()
    if gap_seconds <= STARTUP_GAP_THRESHOLD_MINUTES * 60:
        return None
    gap_start = row["alive_at"]
    existing = db.execute(
        "SELECT id FROM observation_gaps WHERE scope='main_process'"
        " AND gap_start=?", (gap_start,)).fetchone()
    if existing is not None:
        return None
    gap_end = clock.to_iso(current)
    detail = {
        "previous_alive_at": gap_start,
        "last_job_at": (row["last_job_at"]
                        if "last_job_at" in row.keys() else None),
        "boot_at": gap_end,
        "gap_seconds": int(gap_seconds),
        "gap_minutes": int(gap_seconds // 60),
        "threshold_minutes": STARTUP_GAP_THRESHOLD_MINUTES,
        "missed_heartbeat_intervals": int(
            gap_seconds // (HEARTBEAT_INTERVAL_MINUTES * 60)),
    }
    db.execute(
        "INSERT INTO observation_gaps(scope, gap_start, gap_end, declared_at)"
        " VALUES('main_process', ?, ?, ?)", (gap_start, gap_end, gap_end))
    db.execute(
        "INSERT INTO system_events(kind, detail_json, created_at)"
        " VALUES('startup_observation_gap', ?, ?)",
        (json.dumps(detail, ensure_ascii=False), gap_end))
    db.commit()
    return detail


def recover_orphaned_sending(db) -> int:
    """Requeue outbox rows left in 'sending' by a killed process.

    Delegates to outbox.recover_sending: at startup no sender is
    running yet (single resident process), so every 'sending' row is
    orphaned by construction. Delivery stays at-least-once — if the
    crash happened after Telegram accepted the message but before the
    state update, the user may see that one message twice; losing it
    silently was judged worse. W-check B2 (maniac B D-5): recovery
    now consumes an attempt and applies the retry backoff, so a
    crash-loop can no longer resend the same message on every
    restart without bound. Idempotent: a second call with no
    'sending' rows records nothing.
    """
    return outbox.recover_sending(db)


def _source_id(db, name: str, kind: str, url: str = "") -> int:
    row = db.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone()
    if row:
        return row[0]
    cur = db.execute(
        "INSERT INTO sources(name, url, kind, status, has_ingest_list)"
        " VALUES(?, ?, ?, 'approved', 'unknown')", (name, url, kind))
    db.commit()
    return cur.lastrowid


def _log_startup_step_failure(step: str, exc: BaseException) -> None:
    """Best-effort record of a failed startup bookkeeping step.

    Startup recovery must never take the whole boot down with it: the
    failure is appended to data/logs/startup.log and echoed to stderr,
    and startup continues on to the poll thread regardless.
    """
    import sys
    import traceback

    msg = f"[startup] {step} failed: {exc!r}"
    print(msg, file=sys.stderr, flush=True)
    logtrim.append_text(
        config.log_dir() / "startup.log",
        msg + "\n" + "".join(traceback.format_exception(exc)) + "\n")


def _record_system_event(kind: str, detail: dict) -> None:
    """Write one system_events row on a short-lived connection."""
    try:
        with closing(dbmod.connect()) as conn:
            conn.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES(?, ?, ?)",
                (kind, json.dumps(detail, ensure_ascii=False),
                 clock.now_iso(conn)))
            conn.commit()
    except Exception:
        pass


def _log_job_error(job: str, exc: BaseException,
                   source_id: int | None = None) -> None:
    # Scheduler-job failures must never vanish silently: record a
    # system_events row AND a job_errors.log line. W-check B2: this
    # uses its OWN short-lived connection — the old version wrote
    # through the shared job connection and rolled IT back on
    # failure, which could undo the job's own completed work.
    import traceback
    try:
        with closing(dbmod.connect()) as conn:
            conn.execute(
                "INSERT INTO system_events(kind, detail_json,"
                " created_at) VALUES('job_error', ?, ?)",
                (json.dumps({"job": job, "source_id": source_id,
                             "error": repr(exc)}),
                 clock.now_iso(conn)))
            conn.commit()
    except Exception:
        pass
    logtrim.append_text(
        config.log_dir() / "job_errors.log",
        f"[{clock.now_iso()}] {job} source={source_id}: {exc!r}\n"
        + "".join(traceback.format_exception(exc)) + "\n")


def _daily_catchup(db) -> None:
    """Run the daily pieces whose slot has passed but not yet run:
    the 07:00 digests and the 21:45 deadline sweep (W-check B2,
    wholesaler D-2/D-3).

    Called at startup AND hourly by the daily_catchup job — R9
    adversarial QA's D-4 residual was that a process which stayed
    alive but slept past the misfire grace got the miss RECORDED
    yet never re-executed, because catch-up existed only at
    startup. Idempotent throughout: digests by their per-day
    idempotency keys, the sweep by its slot record
    (last_deadline_sweep_slot, written by deadline_sweep itself —
    a sweep run at 01:00 covers YESTERDAY's 21:45 slot, so the
    check is slot-keyed, not calendar-date-keyed). Enqueue-side
    only; the 1-minute outbox job performs actual delivery.
    """
    if not bot_mod.gate_open(db):
        return
    now = clock.now(db)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= 7 and get_setting(db, "digest_enabled", "1") != "0":
        # R9 wholesaler N-1: the existence check used to look at the
        # health key only, so a process that died between the two
        # enqueues inside send_daily_digests lost the discovery
        # digest for the whole day. Both keys are required; the
        # producer is idempotent per key, so re-calling it when
        # either is missing is safe.
        present = {r[0] for r in db.execute(
            "SELECT idempotency_key FROM notification_outbox"
            " WHERE idempotency_key IN (?, ?)",
            ("health_digest|" + today, "discovery_digest|" + today))}
        if len(present) < 2:
            digest.send_daily_digests(db)
    slot = digest.sweep_slot_date(now)
    if get_setting(db, "last_deadline_sweep_slot") != slot:
        digest.deadline_sweep(db)


def _startup_catchup(db) -> None:
    """Startup catch-up: the daily pieces (_daily_catchup) plus the
    staged deadline alerts, so a deadline crossed during downtime
    is declared within minutes of the restart — including the
    missed-window declaration — instead of up to an hour later."""
    _daily_catchup(db)
    if not bot_mod.gate_open(db):
        return
    from . import deadlines_ops
    deadlines_ops.staged_alerts(db)


def run() -> None:
    config.load_dotenv()
    db = dbmod.connect()
    dbmod.init_schema(db)
    # Startup recovery, BEFORE the heartbeat is overwritten and before
    # any job can run: (1) record the observation gap left by the
    # previous process (9-A2(3)), (2) requeue outbox rows orphaned in
    # 'sending' by a mid-send kill, (3) stamp this boot. Each step is
    # contained: bookkeeping failure is logged, never fatal — the bot
    # coming up matters more than the bookkeeping being perfect.
    for step_name, step_fn in (
            ("record_startup_gap", record_startup_gap),
            ("recover_orphaned_sending", recover_orphaned_sending)):
        try:
            step_fn(db)
        except Exception as exc:
            _log_startup_step_failure(step_name, exc)
    # A test clock offset left in settings silently shifts EVERY
    # time judgement in the process (W-check B2, wholesaler D-9).
    # It is a legitimate test supply path, so it stays — but a
    # nonzero value at boot is now impossible to miss.
    try:
        offset = clock.offset_seconds(db)
        if offset:
            msg = (f"[startup] WARNING test_clock_offset_seconds={offset}"
                   " — all in-app time judgements are shifted")
            print(msg, file=__import__("sys").stderr, flush=True)
            logtrim.append_text(config.log_dir() / "startup.log",
                                msg + "\n")
            db.execute(
                "INSERT INTO system_events(kind, detail_json, created_at)"
                " VALUES('test_clock_offset_active', ?, ?)",
                (json.dumps({"offset_seconds": offset}),
                 clock.now_iso(db)))
            db.commit()
    except Exception:
        pass
    health.touch_heartbeat(db)
    db.execute("UPDATE heartbeat SET boot_at=? WHERE id=1",
               (clock.now_iso(db),))
    db.commit()
    # Startup proof: one line in data/logs/startup.log per boot, with
    # the running version, DB path and gate state — the externally
    # checkable evidence that this build actually started.
    version_mod.write_startup_log(
        db_file=config.db_path(), gate_open=bot_mod.gate_open(db))
    transport = HttpTransport()
    # The bot works in the poll thread; give it its own connection so it
    # never contends with the scheduler jobs' connections.
    bot_db = dbmod.connect()
    bot = bot_mod.Bot(bot_db, transport)
    try:
        _startup_catchup(db)
    except Exception as exc:
        _log_startup_step_failure("startup_catchup", exc)
    db.close()
    scheduler = BlockingScheduler(timezone=clock.JST)

    def _run_job(name, body, *, needs_gate=True):
        """One scheduler job run on its OWN connection (B2 D-1)."""
        conn = dbmod.connect()
        try:
            if needs_gate and not bot_mod.gate_open(conn):
                return
            body(conn)
        except Exception as exc:
            _log_job_error(name, exc)
        finally:
            conn.close()

    # Poll-thread supervision state (AI-engineer #3): the poll thread
    # used to be able to die silently — the heartbeat kept beating
    # (it is scheduler-side), the watchdog saw a live process, and
    # the bot was dead forever with every indicator green. The
    # heartbeat job now also watches the poll thread itself and
    # restarts it, with a restart budget so a thread that dies
    # instantly on every start cannot spin forever.
    poll_state = {"thread": None, "active": False, "restarts": [],
                  "exhausted_notified": False}

    def _supervise_poll_thread() -> None:
        thread = poll_state["thread"]
        if thread is None or not poll_state["active"] or stop.is_set():
            return
        if thread.is_alive():
            return
        now_mono = time.monotonic()
        poll_state["restarts"] = [
            t for t in poll_state["restarts"] if now_mono - t < 3600]
        if len(poll_state["restarts"]) < 5:
            # Budget has room again (R9 AI-engineer OBS-1): re-arm
            # the exhaustion notification so a SECOND exhaustion
            # episode is visible as its own event, not only as
            # further poll_thread_dead rows.
            poll_state["exhausted_notified"] = False
        if len(poll_state["restarts"]) >= 5:
            if not poll_state["exhausted_notified"]:
                poll_state["exhausted_notified"] = True
                _record_system_event(
                    "poll_thread_restart_exhausted",
                    {"restarts_last_hour": len(poll_state["restarts"])})
            return
        poll_state["restarts"].append(now_mono)
        _record_system_event(
            "poll_thread_dead",
            {"restart_number": len(poll_state["restarts"])})
        new_thread = threading.Thread(target=poll_loop, daemon=True)
        poll_state["thread"] = new_thread
        new_thread.start()

    def hb():
        conn = dbmod.connect()
        try:
            health.touch_heartbeat(conn)
        except Exception as exc:
            _log_job_error("heartbeat", exc)
        finally:
            conn.close()
        try:
            _supervise_poll_thread()
        except Exception as exc:
            _log_job_error("poll_supervision", exc)

    def outbox_job():
        conn = dbmod.connect()
        try:
            gated = bot_mod.gate_open(conn)
            if gated:
                outbox.process_outbox(conn, transport)
            # last_job_at must mean 'a job actually ran' (AI-engineer
            # #7): with the gate closed it used to advance anyway,
            # polluting the startup-gap detail that reads it.
            health.touch_heartbeat(conn, job=gated)
        except Exception as exc:
            _log_job_error("outbox", exc)
        finally:
            conn.close()

    def health_job():
        def body(conn):
            from . import maintenance
            try:
                maintenance.prune_discogs_snapshots(conn)
            except Exception as exc:
                _log_job_error("discogs_snapshot_retention", exc)
            health.evaluate_time_based_health(conn)
            # W-check B4 (AI engineer / maniac B): the outbox safety
            # net enqueue_pending_candidate_reviews existed but had
            # NO caller — a candidate_review row whose producer-side
            # enqueue failed (or a lane that never enqueues) would
            # stay silently unnotified forever. The 15-minute health
            # job is its home; it is idempotent, so rows already
            # notified cost nothing.
            outbox.enqueue_pending_candidate_reviews(conn)
        _run_job("health", body)

    def discogs_job():
        def body(conn):
            from . import freqpolicy
            sid = _source_id(conn, "discogs", "db")
            # The ladder applies to this lane too (maniac A D-3):
            # demoted to 1440 it must actually wait a day.
            if not freqpolicy.is_due(conn, sid):
                return
            DiscogsFetcher(conn, sid).run_watchlist()
        _run_job("discogs", body)

    def blipblop_job():
        def body(conn):
            from . import freqpolicy
            sid = _source_id(conn, "blipblop", "community",
                             "https://blipblop.net/feed/")
            if not freqpolicy.is_due(conn, sid):
                return
            f = BlipBlopFetcher(conn, sid)
            res = f.run(f.feed_url)
            if res["ok"]:
                f.register_candidates(res["items"])
        _run_job("blipblop", body)

    def digest_job():
        # /digest off (settings.digest_enabled == "0") suppresses the
        # daily digest send; the outbox flush itself is unaffected.
        def body(conn):
            if get_setting(conn, "digest_enabled", "1") == "0":
                return
            digest.send_daily_digests(conn)
            outbox.process_outbox(conn, transport)
        _run_job("digest", body)

    def sweep_job():
        def body(conn):
            digest.deadline_sweep(conn)
            outbox.process_outbox(conn, transport)
        _run_job("deadline_sweep", body)

    # ---- Phase 2/3 jobs (v2.0) -------------------------------------
    def storewatch_job():
        def body(conn):
            from . import storewatch
            storewatch.run_all(conn)
        _run_job("storewatch", body)

    def ingest_job():
        def body(conn):
            from . import evidence, freqpolicy
            from .fetchers.ingest import IngestFetcher
            rules = conn.execute(
                "SELECT * FROM watch_rules WHERE fetch_mode='ingest'"
                " AND enabled=1").fetchall()
            rounds: dict[str, set] = {}
            for rule in rules:
                src = conn.execute("SELECT * FROM sources WHERE name=?",
                                   (rule["source"],)).fetchone()
                if src is None or not src["url"]:
                    continue
                if not freqpolicy.is_due(conn, src["id"]):
                    continue
                try:
                    f = IngestFetcher(conn, src["id"])
                    res = f.run(src["url"])
                    if res["ok"]:
                        f.register_ingested(src["name"], res["items"])
                        # W-check B3 (seller D4): the absence
                        # lifecycle (agreement-06) finally gets its
                        # caller. Only a fully successful round —
                        # effective result OK, not a zero-item
                        # anomaly rewrite — counts as "the shop's
                        # whole list was seen"; anything less is not
                        # evidence that unlisted copies are gone.
                        if res.get("result") == "OK":
                            seen = rounds.setdefault(src["name"], set())
                            seen.update(i.get("url") for i in
                                        res["items"] if i.get("url"))
                except Exception as exc:
                    _log_job_error("ingest", exc, source_id=src["id"])
            if rounds:
                try:
                    evidence.apply_absence_round(conn, rounds)
                except Exception as exc:
                    _log_job_error("ingest_absence", exc)
        _run_job("ingest", body)

    def yahoo_used_job():
        def body(conn):
            from .used_market import run_yahoo_used_sweep
            run_yahoo_used_sweep(conn)
        _run_job("yahoo_used", body)

    def market_api_job():
        def body(conn):
            from .used_market import run_market_api_sweep
            try:
                run_market_api_sweep(conn, "rakuten")
            except Exception as exc:
                _log_job_error("market_api_rakuten", exc)
        _run_job("market_api", body)

    def goods_job():
        # GOODS lane (agreement-09): approved GOODS sources are
        # fetched on the ladder like other source jobs.
        def body(conn):
            from . import freqpolicy
            from .fetchers.goods import GoodsFetcher
            rows = conn.execute(
                "SELECT * FROM sources WHERE status='approved' AND"
                " kind='GOODS' AND url IS NOT NULL").fetchall()
            for src in rows:
                if not freqpolicy.is_due(conn, src["id"]):
                    continue
                try:
                    f = GoodsFetcher(conn, src["id"])
                    res = f.run(src["url"])
                    if res["ok"]:
                        f.register_goods(res["items"])
                except Exception as exc:
                    _log_job_error("goods", exc, source_id=src["id"])
        _run_job("goods", body)

    def deadlines_job():
        def body(conn):
            from . import deadlines_ops
            deadlines_ops.staged_alerts(conn)
        _run_job("deadlines", body)

    def arrival_job():
        def body(conn):
            from . import deadlines_ops
            deadlines_ops.arrival_check(conn)
        _run_job("arrival", body)

    def bundling_job():
        def body(conn):
            from . import bundling
            bundling.propose_bundles(conn)
        _run_job("bundling", body)

    def variant_sweep_job():
        def body(conn):
            from . import variant_sweep
            variant_sweep.run_sweep(conn)
        _run_job("variant_sweep", body)

    def prefix_sweep_job():
        def body(conn):
            from . import sweep as sweep_mod
            sweep_mod.run_weekly_sweep(conn)
        _run_job("prefix_sweep", body)

    def maintenance_job():
        def body(conn):
            from . import maintenance
            maintenance.run_maintenance(conn)
        _run_job("maintenance", body)

    def daily_catchup_job():
        # Hourly re-run of the digest/sweep catch-up (R9 adversarial
        # QA, D-4 residual): a live process that slept past a daily
        # slot's misfire grace recovers the slot within the hour
        # instead of only recording the miss.
        def body(conn):
            _daily_catchup(conn)
        _run_job("daily_catchup", body)

    def doujin_harvest_job():
        def body(conn):
            from . import freqpolicy
            from .fetchers.doujin_event import (DoujinEventFetcher,
                                                harvest_event_catalog)

            def _factory(db_, event_row):
                name = "doujin_event:" + str(event_row["name"])
                src = db_.execute(
                    "SELECT id FROM sources WHERE name=?",
                    (name,)).fetchone()
                if src is None:
                    cur = db_.execute(
                        "INSERT INTO sources(name, url, kind, status)"
                        " VALUES(?, ?, 'EVENT', 'approved')",
                        (name, event_row["url"]))
                    db_.commit()
                    source_id = int(cur.lastrowid)
                else:
                    source_id = src[0]
                # The ladder applies per event source too (maniac
                # A D-3): returning None makes the harvest skip it.
                if not freqpolicy.is_due(db_, source_id):
                    return None
                return DoujinEventFetcher(db_, source_id)

            harvest_event_catalog(conn, _factory)
        _run_job("doujin_harvest", body)

    def webpages_job():
        # No-API acquisition (v2.0.2): approved RSS / SITEMAP /
        # STRUCTURED sources are fetched with keyless web machinery.
        def body(conn):
            from . import evidence, freqpolicy
            from .fetchers.ingest import IngestFetcher
            from .fetchers.webpages import (FeedFetcher, SitemapFetcher,
                                            StructuredFetcher)
            rows = conn.execute(
                "SELECT * FROM sources WHERE status='approved' AND"
                " kind IN ('RSS','SITEMAP','STRUCTURED') AND"
                " url IS NOT NULL").fetchall()
            # W-check B5 (seller 中-2): the STRUCTURED lane never
            # joined an absence round — register_ingested was its
            # only call — so its listings froze 'active' forever,
            # even after the product vanished from the page.
            # Fully successful rounds (effective result OK, the
            # ingest lane's discipline) now feed apply_absence_
            # round for their source like every other lane.
            rounds: dict[str, set] = {}
            for src in rows:
                if not freqpolicy.is_due(conn, src["id"]):
                    continue
                try:
                    if src["kind"] == "RSS":
                        f = FeedFetcher(conn, src["id"])
                        initial_cutoff = f.initial_scan_state()
                        res = f.run(src["url"])
                        if res["ok"] and res["result"] == "OK":
                            registered = f.register_candidates(
                                res["items"],
                                found_via="feed:" + src["name"],
                                initial_cutoff=initial_cutoff)
                            if initial_cutoff is not None and registered["failed"] == 0:
                                f.finish_initial_scan()
                    elif src["kind"] == "SITEMAP":
                        f = SitemapFetcher(conn, src["id"])
                        res = f.run(src["url"])
                        if res["ok"]:
                            from .fetchers.webpages import (
                                is_product_like_url)
                            from .normalize import fingerprint_for
                            # W-check B2 (maniac A D-7): the old
                            # [:50] slice meant sitemap items past
                            # position 50 NEVER became candidates —
                            # not 'later', never, because each run
                            # re-sliced the same head. The cap now
                            # counts only NEW registrations: known
                            # fingerprints are skipped for free, so
                            # each run surfaces the next unseen items.
                            known = {r[0] for r in conn.execute(
                                "SELECT fingerprint FROM"
                                " candidate_editions").fetchall()}
                            fresh = []
                            for i in res["items"]:
                                if not is_product_like_url(i["url"]):
                                    continue
                                # R19 (wholesaler): the known set
                                # now grows with the fresh picks —
                                # duplicate fingerprints inside ONE
                                # sitemap used to pass the check
                                # repeatedly and eat the 50 slots.
                                _fp = fingerprint_for(i["title"], None)
                                if _fp in known:
                                    continue
                                known.add(_fp)
                                fresh.append(i)
                                if len(fresh) >= 50:
                                    break
                            if fresh:
                                FeedFetcher(conn, src["id"]) \
                                    .register_candidates(
                                        fresh,
                                        found_via="sitemap:" + src["name"])
                    else:  # STRUCTURED
                        f = StructuredFetcher(conn, src["id"])
                        res = f.run(src["url"])
                        if res["ok"] and res["items"]:
                            IngestFetcher(conn, src["id"]) \
                                .register_ingested(src["name"], res["items"])
                        if res["ok"] and res.get("result") == "OK":
                            seen = rounds.setdefault(src["name"], set())
                            seen.update(i.get("url") for i in
                                        res["items"] if i.get("url"))
                except Exception as exc:
                    _log_job_error("webpages", exc, source_id=src["id"])
            if rounds:
                try:
                    evidence.apply_absence_round(conn, rounds)
                except Exception as exc:
                    _log_job_error("webpages_absence", exc)
        _run_job("webpages", body)

    scheduler.add_job(hb, "interval", minutes=5, id="heartbeat",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(outbox_job, "interval", minutes=1, id="outbox",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(health_job, "interval", minutes=15, id="health",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(discogs_job, "interval", minutes=15, id="discogs",
                      next_run_time=datetime.now(clock.JST),
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(blipblop_job, "interval", minutes=15, id="blipblop",
                      next_run_time=datetime.now(clock.JST),
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(digest_job, "cron", hour=7, minute=0, id="digest",
                      misfire_grace_time=GRACE_DAILY_CRON_S)
    scheduler.add_job(sweep_job, "cron", hour=21, minute=45, id="sweep",
                      misfire_grace_time=GRACE_DAILY_CRON_S)
    # Phase 2/3 source jobs tick every 15 minutes and consult the
    # frequency ladder (freqpolicy.is_due) per source — the ladder
    # sets each source's real cadence.
    scheduler.add_job(storewatch_job, "interval", minutes=15,
                      id="storewatch", misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(ingest_job, "interval", minutes=15, id="ingest",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(yahoo_used_job, "interval", minutes=15,
                      id="yahoo_used", max_instances=1, coalesce=True,
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(market_api_job, "interval", minutes=15,
                      id="market_api", max_instances=1, coalesce=True,
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(goods_job, "interval", minutes=15, id="goods",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(deadlines_job, "interval", hours=1,
                      id="deadline_alerts",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(arrival_job, "cron", hour=8, minute=30,
                      id="arrival_check",
                      misfire_grace_time=GRACE_DAILY_CRON_S)
    scheduler.add_job(bundling_job, "cron", hour=3, minute=10,
                      id="bundling",
                      misfire_grace_time=GRACE_DAILY_CRON_S)
    scheduler.add_job(variant_sweep_job, "cron", month="1,4,7,10",
                      day=1, hour=4, minute=0, id="variant_sweep",
                      misfire_grace_time=GRACE_QUARTERLY_CRON_S)
    scheduler.add_job(prefix_sweep_job, "cron", day_of_week="sun",
                      hour=3, minute=0, id="prefix_sweep",
                      misfire_grace_time=GRACE_WEEKLY_CRON_S)
    scheduler.add_job(maintenance_job, "cron", day_of_week="sun",
                      hour=3, minute=30, id="maintenance",
                      misfire_grace_time=GRACE_WEEKLY_CRON_S)
    scheduler.add_job(daily_catchup_job, "interval", hours=1,
                      id="daily_catchup",
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(doujin_harvest_job, "interval", minutes=15,
                      id="doujin_harvest",
                      next_run_time=datetime.now(clock.JST),
                      misfire_grace_time=GRACE_INTERVAL_S)
    scheduler.add_job(webpages_job, "interval", minutes=15,
                      id="webpages", misfire_grace_time=GRACE_INTERVAL_S)

    def _scheduler_listener(event):
        # Misfires and job errors used to exist only in APScheduler's
        # own (unconfigured) logger — i.e. nowhere. They are system
        # events now (wholesaler D-2 / adversarial QA D-4).
        try:
            job_id = getattr(event, "job_id", "?")
            if event.code == EVENT_JOB_MISSED:
                _record_system_event(
                    "scheduler_job_missed",
                    {"job": job_id,
                     "scheduled_run_times": [
                         str(t) for t in
                         getattr(event, "scheduled_run_times", [])]})
            elif event.code == EVENT_JOB_MAX_INSTANCES:
                _record_system_event(
                    "scheduler_job_max_instances", {"job": job_id})
            elif event.code == EVENT_JOB_ERROR:
                _log_job_error(f"scheduler:{job_id}",
                               getattr(event, "exception", None)
                               or RuntimeError("unknown job error"))
        except Exception:
            pass

    # getattr guard: the resident test harness drives run() with a
    # minimal fake scheduler that predates listener support.
    _add_listener = getattr(scheduler, "add_listener", None)
    if _add_listener is not None:
        _add_listener(
            _scheduler_listener,
            EVENT_JOB_MISSED | EVENT_JOB_ERROR | EVENT_JOB_MAX_INSTANCES)

    stop = threading.Event()

    def _log_poll_error(context: str, exc: BaseException) -> None:
        # Poll errors must never vanish silently: append them to
        # data/logs/poll_errors.log and echo to stderr.
        import sys
        import traceback
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        detail = _redact_poll_error(
            f"{context}: {exc!r}\n"
            + "".join(traceback.format_exception(exc)))
        logtrim.append_text(
            config.log_dir() / "poll_errors.log",
            f"[{stamp}] {detail}\n")
        print(f"[poll] {detail}", file=sys.stderr, flush=True)

    def poll_loop():
        token = config.telegram_bot_token()
        if not token:
            return
        # From here the thread is expected to live: the heartbeat
        # job supervises it (see _supervise_poll_thread).
        poll_state["active"] = True
        offset = None
        try:
            raw = get_setting(bot_db, "telegram_update_offset")
            if raw:
                offset = int(raw)
        except Exception as exc:
            _log_poll_error("initial offset read", exc)
        failures = 0
        degraded = False
        while not stop.is_set():
            try:
                updates = fetch_updates(token, offset=offset, timeout=25)
            except Exception as exc:
                failures += 1
                _log_poll_error("fetch_updates", exc)
                if failures >= 10 and not degraded:
                    # 'Alive but dead' made visible (adversarial QA
                    # D-5): the heartbeat kept beating while a 401
                    # looped forever at a fixed 5s retry. One event
                    # per episode, plus progressive backoff below.
                    degraded = True
                    try:
                        bot_db.execute(
                            "INSERT INTO system_events(kind, detail_json,"
                            " created_at) VALUES('poll_degraded', ?, ?)",
                            (json.dumps(
                                {"consecutive_failures": failures,
                                 "error": str(exc)[:300]},
                                ensure_ascii=False),
                             clock.now_iso(bot_db)))
                        bot_db.commit()
                    except Exception:
                        try:
                            bot_db.rollback()
                        except Exception:
                            pass
                retry_after = getattr(exc, "retry_after", None)
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    delay = min(float(retry_after), 300.0)
                else:
                    delay = min(5.0 * (2 ** min(failures - 1, 6)), 300.0)
                stop.wait(delay)
                continue
            if failures:
                if degraded:
                    degraded = False
                    try:
                        bot_db.execute(
                            "INSERT INTO system_events(kind, detail_json,"
                            " created_at) VALUES('poll_recovered', ?, ?)",
                            (json.dumps({"after_failures": failures}),
                             clock.now_iso(bot_db)))
                        bot_db.commit()
                    except Exception:
                        try:
                            bot_db.rollback()
                        except Exception:
                            pass
                failures = 0
            # Liveness stamp (AI-engineer #3): diagnose reads
            # poll_alive_at to tell 'process alive, bot alive' from
            # 'process alive, bot dead'. One write per fetch cycle
            # (~25s long-poll) is cheap on the bot's own connection.
            try:
                from .config import set_setting as _set_setting
                _set_setting(bot_db, "poll_alive_at",
                             clock.now_iso(bot_db))
            except Exception:
                try:
                    bot_db.rollback()
                except Exception:
                    pass
            for upd in updates:
                # One bad update must never kill the poll thread.
                # W-check B2 (adversarial QA D-3): the offset used to
                # be persisted BEFORE handling, so any handling
                # failure — above all a transient Telegram error
                # while SENDING THE REPLY — lost the command forever.
                # Now: handle first; advance only on a settled
                # outcome. A transient outcome leaves the offset
                # behind the update, so Telegram redelivers it.
                uid = upd.get("update_id") if isinstance(upd, dict) \
                    else None
                try:
                    result = bot.handle_update(upd)
                except Exception as exc:
                    _log_poll_error(
                        f"handle_update update_id={uid}", exc)
                    result = "invalid_update"
                if result == "transient_error":
                    stop.wait(5)
                    break
                if uid is not None:
                    offset = uid + 1
                    try:
                        from .config import set_setting
                        set_setting(bot_db, "telegram_update_offset",
                                    str(offset))
                    except Exception as exc:
                        _log_poll_error("offset persist", exc)

    thread = threading.Thread(target=poll_loop, daemon=True)
    poll_state["thread"] = thread
    thread.start()
    try:
        scheduler.start()
    finally:
        stop.set()
        # Clean shutdown path (AI-engineer S-2): the finally used to
        # only set the stop flag, leaving the poll thread mid-write
        # as a daemon and the bot connection unclosed. Process exit
        # closes fds anyway, so this is hygiene, not a fix for a
        # proven loss — but a bounded join keeps a final offset
        # write from racing the exit.
        try:
            thread.join(timeout=2)
        except Exception:
            pass
        try:
            bot_db.close()
        except Exception:
            pass


if __name__ == "__main__":
    run()
