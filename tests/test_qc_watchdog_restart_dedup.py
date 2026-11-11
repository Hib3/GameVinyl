"""Regression tests: watchdog restart dedup per stale episode.

Field context (debugger 12/20, getUpdates contention): watchdog.check()
used to attempt a restart on EVERY tick while the heartbeat was stale.
Each successful spawn is another resident run.py, and concurrent
getUpdates long-polls for one bot fail with HTTP 409 ("terminated by
other getUpdates request"), churning update delivery. One successful
restart per stale episode (keyed by the heartbeat alive_at) is enough;
failed spawns are still retried on the next tick.
"""
from datetime import timedelta

from grt import clock, db as dbmod, watchdog


def _db_with_stale_heartbeat(tmp_path, minutes=60):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    old = clock.now(db) - timedelta(minutes=minutes)
    db.execute("INSERT INTO heartbeat(id, alive_at) VALUES(1, ?)",
               (clock.to_iso(old),))
    db.commit()
    return db, clock.to_iso(old)


def test_successful_restart_not_repeated_within_one_episode(tmp_path):
    db, _ = _db_with_stale_heartbeat(tmp_path)
    calls = []

    def fake_restart():
        calls.append(1)
        return True

    for _ in range(3):
        res = watchdog.check(db, restart=fake_restart)
        assert res["status"] == "anomaly"
        assert res["restart_ok"] is True
    assert len(calls) == 1


def test_failed_restart_is_retried_next_tick(tmp_path):
    db, _ = _db_with_stale_heartbeat(tmp_path)
    calls = []

    def failing_restart():
        calls.append(1)
        return False

    watchdog.check(db, restart=failing_restart)
    watchdog.check(db, restart=failing_restart)
    assert len(calls) == 2


def test_new_stale_episode_gets_a_new_restart(tmp_path):
    db, _ = _db_with_stale_heartbeat(tmp_path)
    calls = []

    def fake_restart():
        calls.append(1)
        return True

    watchdog.check(db, restart=fake_restart)
    assert len(calls) == 1
    # A later, distinct stale episode (heartbeat advanced, then stalled
    # again) must not be suppressed by the earlier episode's restart.
    newer = clock.now(db) - timedelta(minutes=30)
    db.execute("UPDATE heartbeat SET alive_at=? WHERE id=1",
               (clock.to_iso(newer),))
    db.commit()
    watchdog.check(db, restart=fake_restart)
    assert len(calls) == 2
