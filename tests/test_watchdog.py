"""Acceptance criterion 3: dead-man's switch.

The main process is 'killed' by simply never touching the heartbeat;
time is advanced via the clock offset (never real waiting). The restart
is an injected callable and the recovery Telegram delivery goes through
MockTransport — the real-Telegram delivery itself is untested here
(no token in this environment) and is reported as such.
"""
from conftest import advance
from grt import health, outbox, watchdog
from grt.config import set_setting
from grt.telegram_io import MockTransport


def test_criterion3_watchdog_anomaly_and_recovery(db):
    set_setting(db, "allowed_chat_id", "111")
    health.touch_heartbeat(db)
    assert watchdog.check(db, restart=lambda: True)["status"] == "alive"

    advance(21 * 60)  # heartbeat now >20 minutes stale
    result = watchdog.check(db, restart=lambda: True)
    assert result["status"] == "anomaly"
    assert result["restart_attempted"] is True
    n = db.execute("SELECT COUNT(*) FROM system_events"
                   " WHERE kind='watchdog_anomaly'").fetchone()[0]
    assert n == 1

    # Main process comes back: fresh heartbeat, next tick closes the
    # anomaly and queues a recovery notice naming the downtime window.
    health.touch_heartbeat(db)
    result = watchdog.check(db, restart=lambda: True)
    assert result["status"] == "recovered"
    row = db.execute(
        "SELECT body FROM notification_outbox WHERE event_type='recovery_notice'"
    ).fetchone()
    assert row is not None and "停止期間" in row[0]

    t = MockTransport()
    stats = outbox.process_outbox(db, t)
    assert stats["sent"] >= 1
    assert any("復旧通知" in body for _, body in t.sent)
