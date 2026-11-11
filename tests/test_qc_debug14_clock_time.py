"""Debugger-14 regression tests: clock/time value domains.

Field hypothesis under test: an exception in time handling (test clock
offset, quiet-hours values, stored timestamps) for the user's real
setting values silences the bot. Verdict after the value-domain battery
(138 cases): RULED OUT for every value a production code path can
write — but the battery confirmed latent OverflowError/ValueError
paths for corrupt values, fixed defensively:

- clock offsets beyond ±365 days (or with too many digits) raised
  OverflowError inside clock.now(); under such a setting every update
  degraded to a silent "invalid_update". Offsets are now bounded.
- policy.get_policy raised on corrupt/non-dict stored policy_json and
  outbox.quiet_not_before raised on malformed quiet values; both now
  fall back to defaults.
- digest.deadline_sweep, digest.build_health_digest and
  health.evaluate_time_based_health died on one unparseable stored
  timestamp; they now skip the bad value and keep working.
"""
from datetime import timedelta

import pytest

from grt import clock
from grt import db as dbmod
from grt import digest as digest_mod
from grt import health as health_mod
from grt import outbox as outbox_mod
from grt import policy as policy_mod
from grt.bot import Bot
from grt.config import set_setting
from grt.telegram_io import MockTransport


def _db(tmp_path):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    return db


def _upd(uid, text, chat=424242):
    return {"update_id": uid, "message": {
        "message_id": uid, "chat": {"id": chat, "type": "private"},
        "text": text}}


@pytest.fixture(autouse=True)
def _clean_offset_env(monkeypatch):
    monkeypatch.delenv("GRT_TEST_CLOCK_OFFSET_SECONDS", raising=False)
    clock.set_local_offset(0)
    yield
    clock.set_local_offset(0)


# ------------------------------------------------------------------ offsets
@pytest.mark.parametrize("value", [
    "0", "1", "-1", "3600", "-86400", " 3600 ", "+3600", "abc", "1.5",
    "3600.0", "", "0x10", "None", "null", "  ",
])
def test_offset_garbage_values_never_raise(tmp_path, value):
    db = _db(tmp_path)
    set_setting(db, "test_clock_offset_seconds", value)
    db.commit()
    result = clock.now_iso(db)  # must not raise
    assert result.startswith("20")  # a sane current timestamp


@pytest.mark.parametrize("value", [
    "99999999999999999999999999", "8640000000000", "-8640000000000",
    "31536001", "-31536001",
])
def test_offset_absurd_values_are_ignored(tmp_path, value):
    db = _db(tmp_path)
    set_setting(db, "test_clock_offset_seconds", value)
    db.commit()
    now = clock.now(db)  # must not raise OverflowError
    # ignored => effectively no offset (within a minute of real time)
    assert abs((now - clock.now()).total_seconds()) < 120


def test_offset_env_absurd_value_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("GRT_TEST_CLOCK_OFFSET_SECONDS",
                       "99999999999999999999999999")
    db = _db(tmp_path)
    now = clock.now(db)
    assert abs((now - clock.now()).total_seconds()) < 120


def test_offset_within_bounds_still_applies(tmp_path):
    db = _db(tmp_path)
    set_setting(db, "test_clock_offset_seconds", "86400")
    db.commit()
    delta = clock.now(db) - clock.now()
    assert timedelta(hours=23) < delta < timedelta(hours=25)


def test_bot_start_survives_absurd_offset_setting(tmp_path):
    db = _db(tmp_path)
    set_setting(db, "test_clock_offset_seconds", "8640000000000")
    db.commit()
    bot = Bot(db, MockTransport())
    action = bot.handle_update(_upd(1, "/start"))
    assert action == "registered"
    assert len(bot.transport.sent) == 2


# ------------------------------------------------------------------ policy
@pytest.mark.parametrize("raw", [
    "{invalid json", '"just a string"', "5", "true",
    '{"quiet":"22:00"}', '{"quiet":2200}',
])
def test_get_policy_tolerates_corrupt_stored_json(tmp_path, raw):
    db = _db(tmp_path)
    set_setting(db, "policy_json", raw)
    db.commit()
    pol = policy_mod.get_policy(db)  # must not raise
    assert pol["quiet"] == {"start": "22:00", "end": "06:30"}


@pytest.mark.parametrize("raw", [
    '{"quiet":{"start":"25:00","end":"06:30"}}',
    '{"quiet":{"start":"22:60","end":"06:30"}}',
    '{"quiet":{"start":"","end":"06:30"}}',
    '{"quiet":{"start":"22","end":"06:30"}}',
    '{"quiet":{"start":"22:00:00","end":"06:30"}}',
    '{"quiet":{"start":2200,"end":"06:30"}}',
    '{"quiet":{"start":null,"end":"06:30"}}',
    '{"quiet":{"start":"22:00","end":"99:99"}}',
])
def test_quiet_not_before_tolerates_malformed_quiet(tmp_path, raw):
    db = _db(tmp_path)
    set_setting(db, "policy_json", raw)
    db.commit()
    nb = outbox_mod.quiet_not_before(db)  # must not raise
    assert nb is not None


def test_quiet_not_before_valid_values_still_work(tmp_path):
    from datetime import datetime
    db = _db(tmp_path)
    set_setting(db, "policy_json",
                '{"quiet":{"start":"00:00","end":"23:59"}}')
    db.commit()
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=clock.JST)
    nb = outbox_mod.quiet_not_before(db, when=noon)
    assert nb > noon  # inside the wide quiet window -> held to its end


# ------------------------------------------------------------------ digest
def _sweep_row(db, deadline_jst, kind="hard_cutoff"):
    db.execute(
        "INSERT INTO release_schedule(edition_id, candidate_id, channel,"
        " deadline_raw, deadline_tz, deadline_jst, deadline_confidence,"
        " deadline_kind) VALUES(NULL, NULL, 'test', 'raw', 'JST', ?,"
        " 'confirmed', ?)", (deadline_jst, kind))
    db.commit()


def test_deadline_sweep_survives_unparseable_in_window_deadline(tmp_path):
    from conftest import set_clock_to
    set_clock_to(21, 45)
    db = _db(tmp_path)
    now = clock.now(db)
    good = clock.to_iso(now + timedelta(hours=1))
    bad = good[:11] + "99:00:00" + good[19:]
    _sweep_row(db, bad)
    _sweep_row(db, good)
    sent = digest_mod.deadline_sweep(db)  # must not raise
    assert sent == 2  # bad row notified from stored text, good row too


def test_health_digest_survives_unparseable_review_created_at(tmp_path):
    db = _db(tmp_path)
    for _ in range(60):
        db.execute(
            "INSERT INTO review_queue(kind, payload_json, created_at)"
            " VALUES('candidate', '{}', 'not-a-date')")
    db.commit()
    text = digest_mod.build_health_digest(db)  # must not raise
    assert "レビュー未処理" in text


def test_health_evaluation_survives_unparseable_timestamps(tmp_path):
    db = _db(tmp_path)
    db.execute(
        "INSERT INTO sources(id, name, url, kind, status, has_ingest_list)"
        " VALUES(1, 's', '', 'api', 'approved', 'unknown')")
    db.execute(
        "INSERT INTO source_health(source_id, state,"
        " consecutive_failures, last_success_at)"
        " VALUES(1, 'OK', 0, 'garbage')")
    db.execute(
        "INSERT INTO fetch_runs(source_id, started_at, result)"
        " VALUES(1, 'also-garbage', 'error')")
    db.commit()
    events = health_mod.evaluate_time_based_health(db)  # must not raise
    assert isinstance(events, list)
