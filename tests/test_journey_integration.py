"""End-to-end user-journey integration test.

Drives the real Bot through the exact sequence a first-time user hits:
 /start registration -> quiet-exception button -> /status -> /watch ->
 /list -> /unwatch -> /quiet -> foreign chat -> duplicate update ->
 candidate approve / reject callbacks.

Added after field failures that single-function tests missed twice:
(1) cross-thread DB use killed update handling (see
    test_threading_regression.py);
(2) callbacks were never answered, so Telegram clients showed the
    button spinner forever ("the button glows but nothing happens").
Every callback MUST be answered via answerCallbackQuery.
"""
import threading

from grt import bot as bot_mod
from grt import db as dbmod
from grt.config import get_setting
from grt.telegram_io import MockTransport

CHAT = 424242


def _msg(uid, text, chat=CHAT):
    return {"update_id": uid,
            "message": {"message_id": uid,
                        "chat": {"id": chat, "type": "private"},
                        "text": text}}


def _cb(uid, data, cbid, chat=CHAT):
    return {"update_id": uid,
            "callback_query": {
                "id": cbid, "from": {"id": chat},
                "message": {"message_id": 5,
                            "chat": {"id": chat, "type": "private"}},
                "data": data}}


def test_full_first_run_journey(tmp_path):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    db.execute("INSERT INTO editions(id, title) VALUES('AR-000001', 'T')")
    cur = db.execute(
        "INSERT INTO candidate_editions(title, status)"
        " VALUES('Cand', 'pending')")
    db.commit()
    cand_id = cur.lastrowid
    transport = MockTransport()
    bot = bot_mod.Bot(db, transport)

    # 1. /start handled from a worker thread (the poll loop arrangement)
    result = {}

    def run_start():
        result["action"] = bot.handle_update(_msg(1001, "/start"))

    t = threading.Thread(target=run_start)
    t.start()
    t.join(timeout=20)
    assert result["action"] == "registered"
    assert get_setting(db, "allowed_chat_id") == str(CHAT)
    assert any("登録しました" in text for _, text in transport.sent)
    assert any("初期設定" in text for _, text in transport.sent)

    # 2. quiet-exception button: must be answered AND take effect
    action = bot.handle_update(_cb(1002, "quiet_off", "cbq-1"))
    assert action == "quiet_selected"
    assert transport.answered == ["cbq-1"]
    assert bot_mod.gate_open(db)
    assert any("OFF に設定しました" in text for _, text in transport.sent)

    # 3-6. commands work after the gate opens
    assert bot.handle_update(_msg(1003, "/status")) == "status"
    assert bot.handle_update(_msg(1004, "/watch AR-000001")) == "watch_on"
    assert bot.handle_update(_msg(1005, "/list")) == "list"
    assert bot.handle_update(_msg(1006, "/unwatch AR-000001")) == "watch_off"
    assert bot.handle_update(_msg(1007, "/quiet")) == "quiet_show"

    # 7. foreign chat is silently ignored and its callbacks unanswered
    assert bot.handle_update(_msg(1008, "/start", chat=999)) == "ignored_foreign"
    sent_before = len(transport.sent)
    assert bot.handle_update(_cb(1009, "quiet_on", "cbq-x", chat=999)) \
        == "ignored_foreign"
    assert len(transport.sent) == sent_before
    assert transport.answered == ["cbq-1"]

    # 8. duplicate update is not re-executed
    assert bot.handle_update(_msg(1001, "/start")) == "duplicate_ignored"

    # 9. candidate approve via button: answered + resolved exactly once
    action = bot.handle_update(_cb(1010, f"approve:{cand_id}", "cbq-2"))
    assert action == "candidate_approved"
    assert transport.answered[-1] == "cbq-2"
    row = db.execute("SELECT status FROM candidate_editions WHERE id=?",
                     (cand_id,)).fetchone()
    assert row[0] == "approved"

    # 10. reject on an already-resolved candidate reports the state
    #     instead of asking for a reason
    action = bot.handle_update(_cb(1011, f"reject:{cand_id}", "cbq-3"))
    assert action == "already_resolved"
    assert transport.answered[-1] == "cbq-3"
