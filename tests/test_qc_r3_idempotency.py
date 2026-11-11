"""QC Round 3 - idempotency and restart resilience.

File-backed DBs throughout: a "restart" means closing the connection
(process death) and opening a NEW connection / Bot / resident process
against the same database file, exactly what a Windows logon restart
does. Network edges are stubbed the same way as round 2 (MockTransport,
stub fetch_updates, fake fetchers); no real tokens anywhere.

Covers:
1. Kill mid-outbox-send ('sending' row) -> restart recovery.
2. Same Telegram update redelivered across a restart, migration run
   twice, schema/seed/diagnose-reset re-runs: no state corruption.
3. settings / watch_level / policy_versions surviving a Bot restart.
Plus the R6-D3 fix: diagnose.py --reset-chat.
"""
import importlib.util
import json
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from grt import bot as bot_mod
from grt import clock
from grt import db as dbmod
from grt import health, migrate, outbox
from grt import main as main_mod
from grt.config import get_setting, set_setting
from grt.policy import apply_change, get_policy
from grt.telegram_io import MockTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DUMMY_TOKEN = "123456789:AA_QC-R3-DUMMY-TOKEN-0000000000000"


def _file_db(path):
    db = dbmod.connect(path)
    dbmod.init_schema(db)
    migrate.seed_dictionaries(db)
    return db


def _msg(uid, chat, text):
    return {"update_id": uid,
            "message": {"chat": {"id": chat}, "text": text}}


def _cb(uid, chat, data, cbid=None):
    return {"update_id": uid,
            "callback_query": {
                "id": cbid if cbid is not None else f"cb-{uid}",
                "message": {"chat": {"id": chat}}, "data": data}}


def _load_diagnose():
    spec = importlib.util.spec_from_file_location(
        "qc_r3_diagnose", PROJECT_ROOT / "diagnose.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. Kill during outbox send: 'sending' must not wedge forever

def test_sending_row_wedges_under_process_outbox_alone(db):
    """Defect mechanism, documented: process_outbox selects only
    pending/failed_retryable, so a row frozen in 'sending' by a kill
    is never picked up again by the sender itself."""
    set_setting(db, "allowed_chat_id", "111")
    oid = outbox.enqueue(db, event_type="new_listing", body="送信中の通知",
                         edition_id="AR-000001", source="discogs",
                         listing_url="https://example.invalid/sending",
                         quiet_exempt=True)
    db.execute("UPDATE notification_outbox SET state='sending' WHERE id=?",
               (oid,))
    db.commit()
    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] == 0 and t.sent == []
    assert db.execute("SELECT state FROM notification_outbox WHERE id=?",
                      (oid,)).fetchone()[0] == "sending"
    assert outbox.pending_backlog(db) == 1  # counted, but undeliverable


def test_recover_orphaned_sending_unit(db):
    set_setting(db, "allowed_chat_id", "111")
    ids = []
    for i in range(2):
        oid = outbox.enqueue(
            db, event_type="new_listing", body=f"復旧対象{i}",
            edition_id="AR-000001", source="discogs",
            listing_url=f"https://example.invalid/rec/{i}",
            quiet_exempt=True)
        ids.append(oid)
    db.execute("UPDATE notification_outbox SET state='sending'")
    db.commit()
    assert main_mod.recover_orphaned_sending(db) == 2
    states = [r[0] for r in db.execute(
        "SELECT state FROM notification_outbox ORDER BY id").fetchall()]
    assert states == ["pending", "pending"]
    # W-check B2 (maniac B D-5): recovery now CONSUMES an attempt —
    # the old reset-to-zero let a crash-loop resend one notification
    # on every restart forever. Startup recovery still redelivers
    # immediately (state pending, no backoff); the attempt budget is
    # the bound.
    attempts = [r[0] for r in db.execute(
        "SELECT attempts FROM notification_outbox ORDER BY id").fetchall()]
    assert attempts == [1, 1]
    ev = db.execute("SELECT detail_json FROM system_events"
                    " WHERE kind='outbox_sending_recovered'").fetchall()
    assert len(ev) == 1
    assert json.loads(ev[0][0])["recovered_ids"] == ids
    # idempotent: nothing in 'sending' -> no recovery, no new event
    assert main_mod.recover_orphaned_sending(db) == 0
    assert db.execute("SELECT COUNT(*) FROM system_events"
                      " WHERE kind='outbox_sending_recovered'"
                      ).fetchone()[0] == 1
    # and the recovered rows really deliver now
    t = MockTransport()
    assert outbox.process_outbox(db, t)["sent"] == 2


def test_killed_mid_send_row_is_delivered_after_resident_restart(
        tmp_path, monkeypatch):
    """Full path: DB holds a 'sending' row (the kill moment), then the
    real main.run() starts; its startup recovery must requeue the row
    and the outbox job must deliver it."""
    from test_qc_r2_resident import _Resident

    def preseed(db):
        set_setting(db, "allowed_chat_id", "111")
        set_setting(db, "deadline_override_quiet_selected", "1")
        oid = outbox.enqueue(
            db, event_type="new_listing", body="強制終了された送信",
            edition_id="AR-000001", source="discogs",
            listing_url="https://example.invalid/killed-mid-send",
            quiet_exempt=True)
        # exactly what process_outbox commits right before transport.send
        db.execute("UPDATE notification_outbox SET state='sending'"
                   " WHERE id=?", (oid,))
        db.commit()

    res = _Resident(tmp_path, monkeypatch, preseed=preseed)
    res.start()
    try:
        deadline = time.monotonic() + 15
        state = None
        while time.monotonic() < deadline:
            rows = res.query(
                "SELECT state FROM notification_outbox"
                " WHERE body='強制終了された送信'")
            if rows and rows[0][0] == "sent":
                state = "sent"
                break
            time.sleep(0.1)
        assert state == "sent", (
            f"recovered row was not delivered: {rows}, "
            f"events={res.query('SELECT kind FROM system_events')}")
        assert any("強制終了された送信" in t for _, t in res.transport.sent)
        assert res.query(
            "SELECT COUNT(*) FROM system_events"
            " WHERE kind='outbox_sending_recovered'")[0][0] == 1
    finally:
        res.stop()


# ---------------------------------------------------------------------------
# 2a. Same Telegram update redelivered across a restart

def test_update_redelivery_across_bot_restart(tmp_path):
    path = tmp_path / "grt.db"
    db = _file_db(path)
    db.execute("INSERT INTO editions(id, title) VALUES('AR-000001', 'T')")
    cur = db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status)"
        " VALUES('C', 'fp-r3', 'pending')")
    cid = cur.lastrowid
    db.commit()
    t1 = MockTransport()
    bot1 = bot_mod.Bot(db, t1)
    bot1.handle_update(_msg(1, 111, "/start"))
    bot1.handle_update(_cb(2, 111, "quiet_off"))
    watch_update = _msg(3, 111, "/watch AR-000001")
    assert bot1.handle_update(watch_update) == "watch_on"
    approve_update = _cb(4, 111, f"approve:{cid}")
    assert bot1.handle_update(approve_update) == "candidate_approved"
    sent_before = len(t1.sent)
    db.close()  # <-- process death / restart boundary

    db2 = _file_db(path)
    t2 = MockTransport()
    bot2 = bot_mod.Bot(db2, t2)
    # Telegram redelivers both updates after the restart
    assert bot2.handle_update(dict(watch_update)) == "duplicate_ignored"
    assert bot2.handle_update(dict(approve_update)) == "duplicate_ignored"
    # even a NEW update repeating the approve is a no-op second resolution
    assert bot2.handle_update(_cb(5, 111, f"approve:{cid}")) \
        == "already_resolved"
    assert db2.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'"
    ).fetchone()[0] == 2
    assert db2.execute(
        "SELECT status FROM candidate_editions WHERE id=?", (cid,)
    ).fetchone()[0] == "approved"
    assert db2.execute(
        "SELECT COUNT(*) FROM telegram_updates").fetchone()[0] == 5
    db2.close()


# ---------------------------------------------------------------------------
# 2b. Migration / schema / seed re-runs across restarts

def test_migration_and_setup_parts_are_idempotent_across_restart(tmp_path):
    path = tmp_path / "grt.db"
    db = _file_db(path)
    r1 = migrate.run_migration(db)
    assert r1["after"]["editions"] == 3777
    # user state created after the first migration
    db.execute("UPDATE editions SET watch_level=2 WHERE id='AR-000001'")
    set_setting(db, "allowed_chat_id", "111")
    db.commit()
    db.close()  # restart boundary

    db2 = _file_db(path)  # init_schema + seed_dictionaries again
    r2 = migrate.run_migration(db2)
    assert r2["inserted_this_run"] == 0
    assert r2["after"] == r1["after"]
    # migration must not clobber user state
    assert db2.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'"
    ).fetchone()[0] == 2
    assert get_setting(db2, "allowed_chat_id") == "111"
    assert db2.execute("SELECT COUNT(*) FROM policy_versions"
                       ).fetchone()[0] == 1  # baseline not duplicated
    # a third run is equally inert
    r3 = migrate.run_migration(db2)
    assert r3["inserted_this_run"] == 0 and r3["after"] == r1["after"]
    db2.close()


def test_startup_helpers_second_run_is_inert(db):
    # gap: recorded once for a stale heartbeat, then the boot stamps a
    # fresh heartbeat and a second startup sees nothing to record
    stale = clock.to_iso(clock.now(db) - timedelta(hours=2))
    db.execute(
        "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
        " VALUES(1, ?, ?, ?)", (stale, stale, stale))
    db.commit()
    assert main_mod.record_startup_gap(db) is not None
    health.touch_heartbeat(db)
    assert main_mod.record_startup_gap(db) is None
    assert db.execute("SELECT COUNT(*) FROM observation_gaps"
                      ).fetchone()[0] == 1
    # schema re-init on a populated DB keeps every row
    before = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("editions", "settings", "heartbeat")}
    dbmod.init_schema(db)
    migrate.seed_dictionaries(db)
    after = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("editions", "settings", "heartbeat")}
    assert after == before


# ---------------------------------------------------------------------------
# 3. settings / watch_level / policy_versions survive a Bot restart

def test_state_persists_across_bot_restart(tmp_path):
    path = tmp_path / "grt.db"
    db = _file_db(path)
    migrate.run_migration(db)
    t1 = MockTransport()
    bot1 = bot_mod.Bot(db, t1)
    bot1.handle_update(_msg(1, 111, "/start"))
    # quiet ON this time: bumps policy_versions and stores the choice
    assert bot1.handle_update(_cb(2, 111, "quiet_on")) == "quiet_selected"
    assert bot1.handle_update(_msg(3, 111, "/watch AR-000001")) == "watch_on"
    # a policy change through the policy layer (versioned)
    v_before = db.execute("SELECT MAX(version_no) FROM policy_versions"
                          ).fetchone()[0]
    new_v = apply_change(db, {"price_drop_rate": 0.2}, "qc_r3", "test")
    assert new_v == v_before + 1
    versions_before = [tuple(r) for r in db.execute(
        "SELECT version_no, changed_via FROM policy_versions"
        " ORDER BY version_no").fetchall()]
    db.close()  # <-- restart boundary

    db2 = _file_db(path)
    t2 = MockTransport()
    bot2 = bot_mod.Bot(db2, t2)
    assert get_setting(db2, "allowed_chat_id") == "111"
    assert get_setting(db2, "deadline_override_quiet") == "1"
    assert bot_mod.gate_open(db2), "gate choice must survive the restart"
    assert db2.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'"
    ).fetchone()[0] == 2
    versions_after = [tuple(r) for r in db2.execute(
        "SELECT version_no, changed_via FROM policy_versions"
        " ORDER BY version_no").fetchall()]
    assert versions_after == versions_before
    assert get_policy(db2)["price_drop_rate"] == 0.2
    # the restarted bot serves commands immediately, no re-registration
    assert bot2.handle_update(_msg(10, 111, "/status")) == "status"
    assert bot2.handle_update(_msg(11, 111, "/list")) == "list"
    assert any("AR-000001" in text for _, text in t2.sent)
    # and a foreign chat is still foreign after the restart
    assert bot2.handle_update(_msg(12, 999, "/status")) == "ignored_foreign"
    db2.close()


# ---------------------------------------------------------------------------
# R6-D3 fix: diagnose.py --reset-chat (re-registration recovery path)

def _diagnose_env(tmp_path, monkeypatch, chat="111"):
    db_file = tmp_path / "grt.db"
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRT_DB_PATH", str(db_file))
    db = _file_db(db_file)
    if chat is not None:
        set_setting(db, "allowed_chat_id", chat)
    db.close()
    return db_file


def test_diagnose_reset_chat_clears_registration(tmp_path, monkeypatch,
                                                 capsys):
    db_file = _diagnose_env(tmp_path, monkeypatch)
    dmod = _load_diagnose()
    dmod.LINES.clear()
    rc = dmod.main(["--reset-chat"])
    assert rc == 0
    out_text = "\n".join(dmod.LINES)
    assert "登録済み" in out_text          # state BEFORE, shown
    assert "登録を消去しました" in out_text  # the erasure, stated plainly
    db = dbmod.connect(db_file)
    try:
        assert get_setting(db, "allowed_chat_id") is None
    finally:
        db.close()
    # the recovery the guide promises now works: a NEW account's /start
    # re-registers after the reset
    db = _file_db(db_file)
    bot = bot_mod.Bot(db, MockTransport())
    assert bot.handle_update(_msg(1, 777, "/start")) == "registered"
    assert get_setting(db, "allowed_chat_id") == "777"
    db.close()


def test_diagnose_reset_chat_when_unregistered_is_harmless(
        tmp_path, monkeypatch):
    db_file = _diagnose_env(tmp_path, monkeypatch, chat=None)
    dmod = _load_diagnose()
    dmod.LINES.clear()
    assert dmod.main(["--reset-chat"]) == 0
    out_text = "\n".join(dmod.LINES)
    assert "未登録" in out_text and "変更なし" in out_text
    db = dbmod.connect(db_file)
    try:
        assert get_setting(db, "allowed_chat_id") is None
    finally:
        db.close()


def test_diagnose_without_flag_does_not_reset(tmp_path, monkeypatch):
    db_file = _diagnose_env(tmp_path, monkeypatch)
    dmod = _load_diagnose()
    dmod.LINES.clear()
    # normal diagnosis path: tokens absent -> network checks skip,
    # schtasks is absent on this Linux VM -> reported as unconfirmable.
    # What matters here: the registration must be untouched.
    rc = dmod.main([])
    assert rc == 0
    db = dbmod.connect(db_file)
    try:
        assert get_setting(db, "allowed_chat_id") == "111"
    finally:
        db.close()
    out_text = "\n".join(dmod.LINES)
    assert "診断結果" in out_text and "消去しました" not in out_text
