"""Regression test for the silent-bot failure found in the field
(2026-11-08): the DB connection was created in the main thread while
the Telegram poll thread used it, so sqlite3 raised ProgrammingError
("SQLite objects created in a thread can only be used in that same
thread") on the very first update and the bot never replied.

This test mirrors grt.main.run()'s arrangement exactly: connection and
Bot created in the main thread, update handled in a worker thread.
"""
import threading

from grt import bot as bot_mod
from grt import db as dbmod
from grt.config import get_setting, set_setting
from grt.telegram_io import MockTransport


def test_bot_handles_start_from_worker_thread(tmp_path):
    db = dbmod.connect(tmp_path / "t.db")  # main thread, like run()
    dbmod.init_schema(db)
    transport = MockTransport()
    bot = bot_mod.Bot(db, transport)

    update = {
        "update_id": 1001,
        "message": {
            "message_id": 1,
            "chat": {"id": 424242, "type": "private"},
            "text": "/start",
        },
    }
    errors: list[BaseException] = []

    def poll_one():
        # poll_loop's per-update sequence
        try:
            set_setting(db, "telegram_update_offset", "1002")
            bot.handle_update(update)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=poll_one)
    t.start()
    t.join(timeout=20)

    assert not t.is_alive()
    assert errors == []
    assert get_setting(db, "allowed_chat_id") == "424242"
    assert transport.sent, "bot must reply to /start"
    assert any("登録しました" in text for _, text in transport.sent)
