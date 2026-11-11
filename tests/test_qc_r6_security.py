"""QC Round 6 - security & privacy regression tests.

Scope: token leakage paths, allowlist enforcement, SQL injection,
malformed/unexpected input, and file-path resolution.

Conventions (per QC assignment):
- Tests that pin CORRECT current behaviour pass now and guard regressions.
- Tests that encode DESIRED behaviour for a confirmed defect FAIL now on
  purpose; each is marked with a [DEFECT R6-Dn] comment explaining what
  is wrong and what the fix should be. See /tmp/qc/round6.md.
- No real tokens, no network: dummy tokens + MockTransport + monkeypatched
  httpx entry points only.
"""
import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

from grt import bot as bot_mod
from grt import config, digest, outbox
from grt.config import get_setting, set_setting
from grt.telegram_io import HttpTransport, MockTransport, fetch_updates

DUMMY_TG = "123456789:AA_DUMMYTOKEN-DO-NOT-USE-1234567890xyz"
DUMMY_DG = "DUMMYDISCOGS-TOKEN-9876543210abcdef"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers

def _bot(db):
    return bot_mod.Bot(db, MockTransport())


def _msg(uid, chat, text):
    return {"update_id": uid,
            "message": {"chat": {"id": chat}, "text": text}}


def _cb(uid, chat, data, cbid=None):
    return {"update_id": uid,
            "callback_query": {
                "id": cbid if cbid is not None else f"cb-{uid}",
                "message": {"chat": {"id": chat}}, "data": data}}


def _registered_bot(db, chat=111):
    """Bot with `chat` registered and the setup gate opened."""
    b = _bot(db)
    b.handle_update(_msg(1, chat, "/start"))
    b.handle_update(_cb(2, chat, "quiet_off"))
    return b


def _seed_edition(db, eid="AR-000001", title="T1", level=0):
    db.execute("INSERT INTO editions(id, title, watch_level) VALUES(?, ?, ?)",
               (eid, title, level))
    db.commit()


def _tables(db):
    return [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " ORDER BY name").fetchall()]


def _load_diagnose():
    spec = importlib.util.spec_from_file_location(
        "qc_diagnose", PROJECT_ROOT / "diagnose.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# 1. Token leakage
# ===========================================================================

def test_token_never_in_bot_messages_db_or_digest(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TG)
    monkeypatch.setenv("DISCOGS_TOKEN", DUMMY_DG)
    _seed_edition(db)
    b = _registered_bot(db)
    b.handle_update(_msg(3, 111, "/watch AR-000001"))
    b.handle_update(_msg(4, 111, "/status"))
    b.handle_update(_msg(5, 111, "/list"))
    outbox.enqueue(db, event_type="new_listing", body="通知本文",
                   edition_id="AR-000001", source="discogs",
                   listing_url="https://example.com/9", quiet_exempt=True)
    outbox.process_outbox(db, b.transport)
    sent = "\n".join(t for _, t in b.transport.sent)
    assert DUMMY_TG not in sent and DUMMY_DG not in sent
    assert DUMMY_TG not in digest.build_health_digest(db)
    dump = []
    for (tbl,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in db.execute(f"SELECT * FROM {tbl}").fetchall():
            dump.append(str(tuple(row)))
    joined = "\n".join(dump)
    assert DUMMY_TG not in joined, "telegram token persisted in DB"
    assert DUMMY_DG not in joined, "discogs token persisted in DB"
    keys = [r[0] for r in db.execute("SELECT key FROM settings").fetchall()]
    assert not any("TOKEN" in k.upper() for k in keys)


def test_token_not_in_transport_repr(db):
    t = HttpTransport(token=DUMMY_TG)
    assert DUMMY_TG not in repr(t)


def test_fetch_updates_network_error_does_not_leak_token(monkeypatch):
    def fake_get(url, **kw):
        raise httpx.ConnectError("connection refused",
                                 request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(Exception) as ei:
        fetch_updates(DUMMY_TG, offset=0, timeout=1)
    assert DUMMY_TG not in str(ei.value)
    assert DUMMY_TG not in repr(ei.value)


def test_http_transport_network_error_does_not_leak_token(monkeypatch):
    def fake_post(url, **kw):
        raise httpx.ConnectError("network unreachable",
                                 request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    t = HttpTransport(token=DUMMY_TG)
    with pytest.raises(Exception) as ei:
        t.send("111", "hi")
    assert DUMMY_TG not in str(ei.value)
    assert DUMMY_TG not in repr(ei.value)


def test_outbox_last_error_does_not_persist_token(db):
    set_setting(db, "allowed_chat_id", "111")
    outbox.enqueue(db, event_type="new_listing", body="x",
                   edition_id="AR-1", source="s",
                   listing_url="https://x/1", quiet_exempt=True)

    class TokenUrlTransport:
        def send(self, chat_id, text, reply_markup=None):
            raise httpx.ConnectError(
                "boom", request=httpx.Request(
                    "POST",
                    f"https://api.telegram.org/bot{DUMMY_TG}/sendMessage"))

    outbox.process_outbox(db, TokenUrlTransport())
    err = db.execute(
        "SELECT last_error FROM notification_outbox").fetchone()[0]
    assert DUMMY_TG not in (err or "")


def test_diagnose_shows_token_length_and_shape_only(db, monkeypatch,
                                                    tmp_path):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TG)
    monkeypatch.setenv("DISCOGS_TOKEN", DUMMY_DG)
    dmod = _load_diagnose()
    dmod.LINES.clear()
    dmod.check_env_and_tokens()
    out_text = "\n".join(dmod.LINES)
    assert DUMMY_TG not in out_text
    assert DUMMY_DG not in out_text
    assert f"{len(DUMMY_TG)}文字" in out_text  # length is shown by design
    assert "形は正常" in out_text


def test_diagnose_telegram_failure_does_not_leak_token(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            raise httpx.ConnectError(
                "dial failed", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "Client", FakeClient)
    dmod = _load_diagnose()
    dmod.LINES.clear()
    dmod.check_telegram(DUMMY_TG)
    assert DUMMY_TG not in "\n".join(dmod.LINES)


def test_diagnose_does_not_print_allowed_chat_id_value(db, monkeypatch,
                                                       tmp_path):
    # Privacy: diagnose prints THAT a chat is registered, never its id.
    src = (PROJECT_ROOT / "diagnose.py").read_text(encoding="utf-8")
    assert 'get_setting(db, "allowed_chat_id")' in src
    tail = src.split('allowed = get_setting')[1].split("def check_tasks")[0]
    assert "allowed}" not in tail and "{allowed" not in tail


# ===========================================================================
# 2. Allowlist / foreign chats
# ===========================================================================

FOREIGN_PROBES = ["/status", "/list", "/watch AR-000001",
                  "/unwatch AR-000001", "/quiet on", "/quiet off",
                  "/start", "hello"]


def test_foreign_chat_gets_no_reply_and_no_state_change(db):
    _seed_edition(db)
    b = _registered_bot(db)
    sent_before = len(b.transport.sent)
    answered_before = list(b.transport.answered)
    for i, text in enumerate(FOREIGN_PROBES):
        assert b.handle_update(_msg(100 + i, 999, text)) == "ignored_foreign"
    assert b.handle_update(_cb(200, 999, "quiet_on")) == "ignored_foreign"
    assert b.handle_update(_cb(201, 999, "approve:1")) == "ignored_foreign"
    assert len(b.transport.sent) == sent_before, "foreign chat got a reply"
    assert b.transport.answered == answered_before
    assert all(chat == "111" for chat, _ in b.transport.sent)
    assert get_setting(db, "allowed_chat_id") == "111"
    lvl = db.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'"
    ).fetchone()[0]
    assert lvl == 0, "foreign chat changed watch state"


def test_foreign_chat_before_registration_is_ignored(db):
    _seed_edition(db)
    b = _bot(db)
    for i, text in enumerate(["/status", "/list", "/watch AR-000001",
                              "/quiet on"]):
        assert b.handle_update(_msg(1 + i, 999, text)) == "ignored_foreign"
    assert b.handle_update(_cb(9, 999, "quiet_off")) == "ignored_foreign"
    assert b.transport.sent == []
    assert get_setting(db, "allowed_chat_id") is None
    assert not bot_mod.gate_open(db)


def test_second_start_from_other_chat_does_not_take_over(db):
    b = _bot(db)
    assert b.handle_update(_msg(1, 555, "/start")) == "registered"
    assert b.handle_update(_msg(2, 666, "/start")) == "ignored_foreign"
    assert get_setting(db, "allowed_chat_id") == "555"


# [REVISED - R6-D3 premise reversed by the specification audit]
# Round 6 originally treated the documented ALLOWED_CHAT_ID env var as
# authoritative. The specification audit (QC round 9, item 9-C2) ruled
# the opposite: per spec section 11 the allowlist's source of truth is
# settings.allowed_chat_id ("許可リストは settings.allowed_chat_id"),
# and the implementation following it is spec-correct; the .env template
# / setup guide wording is the side that is wrong. This test therefore
# now pins the SPEC behaviour: the ALLOWED_CHAT_ID environment variable
# has NO effect on registration - it neither pre-registers a chat nor
# blocks a first /start from another chat; settings.allowed_chat_id,
# written by the first /start, is the only authority.
def test_allowed_chat_id_env_is_honoured(db, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_ID", "424242")
    _seed_edition(db)
    b = _bot(db)
    # The env var alone pre-registers nobody: settings stays empty and
    # the env-named chat gets no service before registration.
    assert get_setting(db, "allowed_chat_id") is None
    assert b.handle_update(_msg(1, 424242, "/status")) == "ignored_foreign"
    # The first /start wins regardless of the env var (settings is the
    # only authority), even from a chat different from the env value.
    assert b.handle_update(_msg(2, 999, "/start")) == "registered"
    assert get_setting(db, "allowed_chat_id") == "999"
    # Afterwards the env-named chat is simply a foreign chat.
    assert b.handle_update(_msg(3, 424242, "/status")) == "ignored_foreign"


# [DEFECT R6-D8] Registration matches text.startswith("/start"), so a
# non-command string such as "/startxxx" also claims the owner slot.
# Desired: only "/start", "/start <payload>" and "/start@<bot>" register.
def test_start_prefix_lookalike_does_not_register(db):
    b = _bot(db)
    assert b.handle_update(_msg(1, 555, "/startxxx")) == "ignored_foreign"
    assert get_setting(db, "allowed_chat_id") is None


# ===========================================================================
# 3. SQL injection
# ===========================================================================

SQLI_ARGS = [
    "';DROP/**/TABLE/**/editions;--",  # space-free (bot splits on spaces)
    "';DROP_TABLE_editions;--",
    "AR-000001'--",
    "'OR'1'='1",
    "'UNION_SELECT_sql_FROM_sqlite_master--",
    "AR-000001;DELETE_FROM_editions",
    "%",
    "_",
    "AR-%",
    "AR_000001",
    "\\",
    "AR-000001\x00junk",
]


@pytest.mark.parametrize("atk", SQLI_ARGS)
def test_sqli_via_watch_is_inert(db, atk):
    _seed_edition(db)
    before = _tables(db)
    b = _registered_bot(db)
    action = b.handle_update(_msg(10, 111, f"/watch {atk}"))
    assert action == "watch_missing"
    assert _tables(db) == before
    assert db.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1
    lvl = db.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'"
    ).fetchone()[0]
    assert lvl == 0, f"injection/wildcard changed watch_level: {atk!r}"


def test_sqli_full_phrase_first_token_only_and_inert(db):
    # Phrases with spaces are truncated to the first whitespace token by
    # the command splitter; that token must still be inert.
    _seed_edition(db)
    b = _registered_bot(db)
    before = _tables(db)
    action = b.handle_update(
        _msg(10, 111, "/watch '; DROP TABLE editions;--"))
    assert action == "watch_missing"
    assert _tables(db) == before
    assert db.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1


def test_sqli_via_unwatch_and_quiet_and_list(db):
    _seed_edition(db, level=2)
    b = _registered_bot(db)
    before = _tables(db)
    assert b.handle_update(_msg(10, 111, "/unwatch 'OR'1'='1")) \
        == "watch_missing"
    lvl = db.execute(
        "SELECT watch_level FROM editions WHERE id='AR-000001'"
    ).fetchone()[0]
    assert lvl == 2
    assert b.handle_update(_msg(11, 111, "/list")) == "list"
    assert _tables(db) == before


def test_reject_callback_sqli_code_does_not_damage_tables(db):
    _seed_edition(db)
    cur = db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status)"
        " VALUES('C', 'fp-sqli', 'pending')")
    cid = cur.lastrowid
    db.commit()
    before = _tables(db)
    b = _registered_bot(db)
    b.handle_update(_cb(10, 111, f"rej:{cid}:X';DROP/**/TABLE/**/editions;--"))
    assert _tables(db) == before
    assert db.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1


# [DEFECT R6-D2] Bot._resolve_candidate resolves review_queue rows with
#   payload_json LIKE '%"candidate_id": <cid>%'
# The trailing % makes the match a PREFIX match on the JSON number:
# resolving candidate 1 also resolves the queue rows of candidates 10,
# 11, 12, ... (verified: 1 -> 10/11/12 all resolved). Those candidates
# stay 'pending' while their queue rows claim resolved, so they silently
# vanish from the review flow. Fix: match the exact serialised value
# ('%"candidate_id": 1,%' / '...: 1}%') or, better, parse the JSON / add
# a candidate_id column.
def test_resolve_candidate_does_not_resolve_prefix_siblings(db):
    set_setting(db, "allowed_chat_id", "111")
    set_setting(db, "deadline_override_quiet_selected", "1")
    for i in range(12):
        cur = db.execute(
            "INSERT INTO candidate_editions(title, fingerprint, status)"
            " VALUES(?, ?, 'pending')", (f"C{i}", f"fp-like-{i}"))
        cid = cur.lastrowid
        db.execute(
            "INSERT INTO review_queue(kind, payload_json, created_at)"
            " VALUES('candidate_review', ?, '2026-01-01')",
            (f'{{"candidate_id": {cid}, "source": "blipblop"}}',))
    db.commit()
    b = _bot(db)
    assert b.handle_update(_cb(50, 111, "approve:1")) == "candidate_approved"
    resolved = db.execute(
        "SELECT payload_json FROM review_queue"
        " WHERE resolved_at IS NOT NULL").fetchall()
    assert len(resolved) == 1, (
        f"expected only candidate 1's queue row resolved, got "
        f"{[r[0] for r in resolved]}")
    assert '"candidate_id": 1,' in resolved[0][0]


# [DEFECT R6-D4] The rej: callback accepts ANY reason code verbatim
# (stored into candidate_editions.reject_reason); it is never checked
# against bot.REJECT_REASONS, so a crafted/stale button can persist
# arbitrary strings as a "reason". Fix: reject codes not in
# REJECT_REASONS (return unknown_callback, leave the candidate pending).
def test_reject_reason_must_be_allowlisted(db):
    cur = db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status)"
        " VALUES('C', 'fp-rej', 'pending')")
    cid = cur.lastrowid
    db.commit()
    b = _registered_bot(db)
    action = b.handle_update(_cb(10, 111, f"rej:{cid}:TOTALLY_MADE_UP"))
    row = db.execute(
        "SELECT status, reject_reason FROM candidate_editions WHERE id=?",
        (cid,)).fetchone()
    assert action != "candidate_rejected"
    assert row[0] == "pending" and row[1] is None


# ===========================================================================
# 4. Unexpected / malformed input
# ===========================================================================

def test_extreme_and_odd_watch_args_do_not_crash(db):
    _seed_edition(db)
    b = _registered_bot(db)
    cases = {
        "/watch -1": "watch_missing",
        "/watch 1.5": "watch_missing",
        "/watch 0": "watch_missing",
        "/watch 🎵🎶💿": "watch_missing",
        "/watch AR\u202e1000-": "watch_missing",   # RTL override in id
        "/watch AR\x00\x01\x02-000001": "watch_missing",  # control chars
        "/watch " + "A" * 100000: "watch_missing",       # 100k-char arg
        "/watch": "watch_usage",
    }
    for i, (text, expected) in enumerate(cases.items()):
        assert b.handle_update(_msg(100 + i, 111, text)) == expected, text[:40]


# [DEFECT R6-D5] /watch and /unwatch echo the raw argument in full:
# "Edition <arg> は存在しません。" A 100,000-char argument produces a
# 100,017-char reply, far beyond Telegram's 4,096-char message limit, so
# with the real HttpTransport sendMessage would fail (HTTP 400) and the
# exception would escape Bot._send (only TypeError is caught) into the
# poll loop's error log. Control/RTL characters are likewise echoed
# unsanitised. Fix: truncate the echoed id (e.g. to 64 chars) in the
# reply, or do not echo the raw argument at all.
def test_watch_reply_never_exceeds_telegram_limit(db):
    b = _registered_bot(db)
    b.handle_update(_msg(10, 111, "/watch " + "A" * 100000))
    reply = b.transport.sent[-1][1]
    assert len(reply) <= 4096


def test_command_case_and_extra_args_current_behaviour(db):
    _seed_edition(db)
    b = _registered_bot(db)
    # Commands are case-sensitive (Telegram convention): no crash, the
    # generic unknown-command reply is sent.
    assert b.handle_update(_msg(10, 111, "/START")) == "unknown_command"
    assert b.handle_update(_msg(11, 111, "/WATCH AR-000001")) \
        == "unknown_command"
    assert b.handle_update(_msg(12, 111, "/Watch AR-000001")) \
        == "unknown_command"
    # Group-style suffixed commands are not recognised (private-chat design).
    assert b.handle_update(_msg(13, 111, "/watch@SomeBot AR-000001")) \
        == "unknown_command"
    # Extra args are silently ignored; only the first arg is used.
    assert b.handle_update(_msg(14, 111, "/watch AR-000001 EXTRA")) \
        == "watch_on"
    assert b.handle_update(_msg(15, 111, "/watch NOPE AR-000001")) \
        == "watch_missing"
    # Empty / whitespace / bare slash: unknown command, no crash.
    for i, text in enumerate(["", "   ", "/"]):
        assert b.handle_update(_msg(20 + i, 111, text)) == "unknown_command"


MALFORMED_CALLBACKS = [
    "approve:", "approve:abc", "approve:1.5", "approve:0x10",
    "approve:999999999999999999999999999999",
    "reject:", "reject:abc",
    "rej:", "rej:1", "rej:abc:CODE", "rej::CODE",
    "rej:99999999999999999999999999:OTHER",
]


# [DEFECT R6-D1] Bot._handle_callback parses callback data with int()
# and tuple unpacking WITHOUT validation. Malformed data (stale/forged
# buttons, or any client bug) raises ValueError/OverflowError out of
# Bot.handle_update. In production main.poll_loop catches it per-update
# (and the offset was already advanced), so the poll thread survives,
# but the update is left half-recorded in telegram_updates (no action),
# no reply is sent, and every direct caller sees a crash. Fix: parse
# defensively (try/except + SQLite-range check) and return
# "unknown_callback" / "candidate_missing" instead of raising.
@pytest.mark.parametrize("data", MALFORMED_CALLBACKS)
def test_malformed_callback_never_raises(db, data):
    b = _registered_bot(db)
    action = b.handle_update(_cb(10, 111, data))  # must not raise
    assert isinstance(action, str) and action


MALFORMED_UPDATES = [
    # update_id far beyond SQLite INTEGER range -> OverflowError today
    {"update_id": 10 ** 30,
     "message": {"chat": {"id": 111}, "text": "/status"}},
    # non-string text -> AttributeError (.strip) today
    {"update_id": 901,
     "message": {"chat": {"id": 111}, "text": 12345}},
    # callback data None -> AttributeError (.startswith) today
    {"update_id": 902,
     "callback_query": {"id": "q",
                        "message": {"chat": {"id": 111}}, "data": None}},
]


# [DEFECT R6-D6] Same robustness gap as R6-D1 at the update-envelope
# level: handle_update trusts Telegram's types blindly. Telegram itself
# always sends well-typed updates, so severity is low, but a single
# malformed update should degrade to an action string, not an exception.
@pytest.mark.parametrize("upd", MALFORMED_UPDATES)
def test_malformed_update_never_raises(db, upd):
    b = _registered_bot(db)
    action = b.handle_update(upd)  # must not raise
    assert isinstance(action, str) and action


def test_valid_callbacks_still_work(db):
    cur = db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status)"
        " VALUES('C', 'fp-ok', 'pending')")
    cid = cur.lastrowid
    db.commit()
    b = _registered_bot(db)
    assert b.handle_update(_cb(10, 111, f"approve:{cid}")) \
        == "candidate_approved"
    assert b.handle_update(_cb(11, 111, "approve:-1")) == "candidate_missing"
    assert b.handle_update(_cb(12, 111, "")) == "unknown_callback"


# ===========================================================================
# 5. File paths / config resolution
# ===========================================================================

def test_grt_data_dir_absolute_is_honoured_verbatim(tmp_path, monkeypatch):
    # Operator-controlled by design (local trust boundary): the env var
    # is taken verbatim, including absolute paths outside the project.
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("GRT_DATA_DIR", str(target))
    assert config.data_dir() == target
    assert config.db_path() == target / "grt.db"
    assert config.log_dir() == target / "logs"


def test_grt_data_dir_empty_falls_back_to_project_data(monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", "")
    assert config.data_dir() == config.PROJECT_ROOT / "data"


def test_grt_db_path_overrides_db_location_only(tmp_path, monkeypatch):
    custom = tmp_path / "custom.db"
    monkeypatch.setenv("GRT_DB_PATH", str(custom))
    monkeypatch.delenv("GRT_DATA_DIR", raising=False)
    assert config.db_path() == custom
    assert config.log_dir() == config.PROJECT_ROOT / "data" / "logs"


def test_evidence_filename_cannot_escape_evidence_dir(tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    from grt.evidence import save_raw_evidence
    for src in ["../../../etc/passwd", "..\\..\\win", "a/b/c", ""]:
        res = save_raw_evidence(src, "https://x", "body", keep_body=True)
        p = Path(res["body_path"])
        assert p.parent == tmp_path / "evidence"
        assert p.suffix == ".html"


# [DEFECT R6-D7] config.load_dotenv() copies EVERY key from data/.env
# into os.environ (only skipping keys already set). That includes the
# test-only clock backdoor GRT_TEST_CLOCK_OFFSET_SECONDS (read by
# grt.clock with top priority) and arbitrary unknown keys: a stray or
# crafted line in the production .env silently shifts every timestamp
# the system produces. Fix: allowlist the keys load_dotenv may set
# (TELEGRAM_BOT_TOKEN, DISCOGS_TOKEN, ALLOWED_CHAT_ID, GRT_DATA_DIR,
# GRT_DB_PATH) and ignore everything else.
def test_dotenv_cannot_set_test_clock_offset(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "GRT_TEST_CLOCK_OFFSET_SECONDS=999999\n"
        "TELEGRAM_BOT_TOKEN=" + DUMMY_TG + "\n", encoding="utf-8")
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GRT_TEST_CLOCK_OFFSET_SECONDS", raising=False)
    try:
        config.load_dotenv()
        assert os.environ.get("GRT_TEST_CLOCK_OFFSET_SECONDS") is None, (
            "load_dotenv imported the test-clock backdoor from .env")
        from grt import clock
        assert clock._offset_seconds() == 0
    finally:
        os.environ.pop("GRT_TEST_CLOCK_OFFSET_SECONDS", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)


def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=" + DUMMY_DG + "\n", encoding="utf-8")
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", DUMMY_TG)
    config.load_dotenv()
    assert os.environ["TELEGRAM_BOT_TOKEN"] == DUMMY_TG
