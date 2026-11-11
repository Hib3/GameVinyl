"""QC Round 1 - full command / callback coverage of grt.bot.Bot.

Coverage matrix (all rows are exercised below; MockTransport only, no
real tokens, no network):

Commands x inputs:
  /start    : plain, payload, @botname form, @botname+payload, padded,
              lookalikes (/startxxx, /start@, /start@@x, /startx@y),
              case variants (/START, /Start), after registration
  /watch    : valid id, no arg, extra args (valid-first / invalid-first),
              nonexistent id, non-numeric, negative, float, zero, huge
              numeric (30 digits), 100k-char arg, emoji id, RTL-override
              id, control-char id, tab separator, newline + 2nd command,
              case variants, @bot suffix
  /unwatch  : valid, no arg, nonexistent, 100k-char arg
  /list     : empty, mixed levels 1/2/3 (9-C16 per-level counts),
              overlong title (reply length cap)
  /status   : empty DB, seeded fetch_runs / gaps / deadlines (9-C24)
  /quiet    : show (ON/OFF wording), on, off, extra args, uppercase,
              bogus value, valid HH:MM-HH:MM change (9-C23), equal range,
              out-of-range / non-padded ranges
  /digest   : show (default ON), on, off, bogus, uppercase (9-C23)
  unknown   : /help, /foo, plain text, emoji text, fullwidth slash,
              empty text, whitespace only, bare "/"
  text-less : photo, photo with command caption, sticker, voice
  envelopes : edited_message, channel_post, empty update, message
              without chat, callback without message, group chat,
              update_id huge / negative / zero / str / float / bool /
              missing, non-dict update, non-str text, non-str data,
              float/list chat id, string chat id

Callbacks x inputs (gate open unless noted):
  quiet_on / quiet_off (exact), trailing-space / case variants,
  approve:<valid> (queue resolves exactly one row, incl. payload with
  candidate_id as the last JSON key), approve:<missing>, approve:-1,
  approve:<already resolved>, approve:007, approve:<18 digits>,
  reject:<valid> -> reason buttons for all REJECT_REASONS,
  rej:<valid> for every allowlisted reason,
  malformed approve/reject/rej forms (non-numeric, float, hex, empty,
  missing segment, extra segment, 19+ digit overflow, SQLi code,
  non-allowlisted / lowercase / padded reason codes, emoji, 100k chars),
  unknown data, empty data, data=None, data missing, data numeric,
  callback id missing.
Gate closed: every command except /status, and every callback except
quiet_on/quiet_off, returns gate_closed without side effects (9-B10).
"""
import itertools
import json

import pytest

from grt import bot as bot_mod
from grt import clock
from grt import db as dbmod
from grt import policy as policy_mod
from grt.config import get_setting, set_setting
from grt.telegram_io import MockTransport

_uids = itertools.count(1000)
_fps = itertools.count(1)


class RecTransport(MockTransport):
    """MockTransport that also records reply_markup arguments."""

    def __init__(self):
        super().__init__()
        self.markups = []

    def send(self, chat_id, text, reply_markup=None):
        self.markups.append(reply_markup)
        return super().send(chat_id, text, reply_markup=reply_markup)


def _bot(db, transport=None):
    return bot_mod.Bot(db, transport or RecTransport())


def _msg(chat, text, uid=None, chat_type="private"):
    return {"update_id": next(_uids) if uid is None else uid,
            "message": {"chat": {"id": chat, "type": chat_type},
                        "text": text}}


def _raw_msg(chat, extra, uid=None):
    m = {"chat": {"id": chat, "type": "private"}}
    m.update(extra)
    return {"update_id": next(_uids) if uid is None else uid, "message": m}


def _cb(chat, data, uid=None, cbid="cbq"):
    return {"update_id": next(_uids) if uid is None else uid,
            "callback_query": {"id": cbid,
                               "message": {"chat": {"id": chat}},
                               "data": data}}


def _registered_bot(db, chat=111, transport=None):
    b = _bot(db, transport)
    assert b.handle_update(_msg(chat, "/start")) == "registered"
    assert b.handle_update(_cb(chat, "quiet_off")) == "quiet_selected"
    return b


def _seed_edition(db, eid="AR-000001", title="T1", level=0):
    db.execute("INSERT INTO editions(id, title, watch_level) VALUES(?, ?, ?)",
               (eid, title, level))
    db.commit()


def _seed_candidate(db, queue_payload=True, payload_style="head"):
    cur = db.execute(
        "INSERT INTO candidate_editions(title, fingerprint, status)"
        " VALUES('C', ?, 'pending')", (f"fp-r1-{next(_fps)}",))
    cid = cur.lastrowid
    if queue_payload:
        if payload_style == "head":
            payload = json.dumps({"candidate_id": cid, "source": "blipblop"})
        else:  # candidate_id as the LAST key -> serialised as {..., "candidate_id": N}
            payload = json.dumps({"source": "blipblop", "candidate_id": cid})
        db.execute(
            "INSERT INTO review_queue(kind, payload_json, created_at)"
            " VALUES('candidate_review', ?, '2026-01-01')", (payload,))
    db.commit()
    return cid


def _fresh_db(tmp_path, name):
    conn = dbmod.connect(tmp_path / name)
    dbmod.init_schema(conn)
    return conn


# ===========================================================================
# /start token matrix (R6-D8)
# ===========================================================================

REGISTERING_STARTS = ["/start", "/start payload123", "/start@MyBot",
                      "/start@MyBot payload", "  /start  ", "/start\n"]


@pytest.mark.parametrize("text", REGISTERING_STARTS)
def test_start_variants_register(tmp_path, text):
    conn = _fresh_db(tmp_path, "s.db")
    b = _bot(conn)
    assert b.handle_update(_msg(555, text)) == "registered"
    assert get_setting(conn, "allowed_chat_id") == "555"
    conn.close()


NON_STARTS = ["/startxxx", "/start@", "/start@@x", "/startx@y", "/START",
              "/Start", "/sta", "/start-foo"]


@pytest.mark.parametrize("text", NON_STARTS)
def test_start_lookalikes_do_not_register(tmp_path, text):
    conn = _fresh_db(tmp_path, "s.db")
    b = _bot(conn)
    assert b.handle_update(_msg(555, text)) == "ignored_foreign"
    assert get_setting(conn, "allowed_chat_id") is None
    conn.close()


def test_start_after_registration(db):
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/start")) == "start"
    assert b.handle_update(_msg(111, "/start@MyBot")) == "start"
    assert b.handle_update(_msg(111, "/start extra")) == "start"
    # a lookalike is not a start command even for the owner
    assert b.handle_update(_msg(111, "/startxxx")) == "unknown_command"


# ===========================================================================
# /watch and /unwatch matrix
# ===========================================================================

WATCH_MISSING_ARGS = ["-1", "1.5", "0", "abc", "9" * 30, "🎵🎶💿",
                      "AR\u202e1000-", "AR\x00\x01-000001", "AR-999999"]


@pytest.mark.parametrize("arg", WATCH_MISSING_ARGS)
def test_watch_missing_args(db, arg):
    _seed_edition(db)
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/watch " + arg)) == "watch_missing"
    assert b.handle_update(_msg(111, "/unwatch " + arg)) == "watch_missing"
    lvl = db.execute("SELECT watch_level FROM editions WHERE id='AR-000001'"
                     ).fetchone()[0]
    assert lvl == 0


def test_watch_unwatch_happy_and_args(db):
    _seed_edition(db)
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/watch")) == "watch_usage"
    assert b.handle_update(_msg(111, "/unwatch")) == "watch_usage"
    assert b.handle_update(_msg(111, "/watch AR-000001")) == "watch_on"
    # extra args: only the first is used
    assert b.handle_update(_msg(111, "/watch AR-000001 EXTRA")) == "watch_on"
    assert b.handle_update(_msg(111, "/watch NOPE AR-000001")) \
        == "watch_missing"
    # tab separator works like a space
    assert b.handle_update(_msg(111, "/watch\tAR-000001")) == "watch_on"
    # a newline does not smuggle in a second command
    sent_before = len(b.transport.sent)
    assert b.handle_update(_msg(111, "/watch AR-000001\n/status")) \
        == "watch_on"
    assert len(b.transport.sent) == sent_before + 1
    assert "【ステータス】" not in b.transport.sent[-1][1]
    assert b.handle_update(_msg(111, "/unwatch AR-000001")) == "watch_off"
    lvl = db.execute("SELECT watch_level FROM editions WHERE id='AR-000001'"
                     ).fetchone()[0]
    assert lvl == 0


def test_watch_case_and_suffix_are_unknown(db):
    _seed_edition(db)
    b = _registered_bot(db)
    for text in ("/WATCH AR-000001", "/Watch AR-000001",
                 "/watch@SomeBot AR-000001", "/Watch@SomeBot AR-000001"):
        assert b.handle_update(_msg(111, text)) == "unknown_command", text


def test_watch_echo_is_truncated(db):
    b = _registered_bot(db)
    for cmd in ("/watch ", "/unwatch "):
        b.handle_update(_msg(111, cmd + "A" * 100000))
        reply = b.transport.sent[-1][1]
        assert len(reply) <= 4096
        assert len(reply) < 200  # echo itself is capped, not just the send


# ===========================================================================
# /list (9-C16) and /status (9-C24)
# ===========================================================================

def test_list_includes_per_level_counts(db):
    _seed_edition(db, "AR-000001", "A", level=1)
    _seed_edition(db, "AR-000002", "B", level=1)
    _seed_edition(db, "AR-000003", "C", level=2)
    _seed_edition(db, "AR-000004", "D", level=2)
    _seed_edition(db, "AR-000005", "E", level=2)
    _seed_edition(db, "AR-000006", "F", level=3)
    _seed_edition(db, "AR-000007", "G", level=0)
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/list")) == "list"
    text = b.transport.sent[-1][1]
    assert "監視中: 6件" in text
    assert "Lv1 2件" in text and "Lv2 3件" in text and "Lv3 1件" in text


def test_list_empty_shows_zero_counts(db):
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/list")) == "list"
    text = b.transport.sent[-1][1]
    assert "監視中: 0件" in text
    assert "Lv1 0件" in text and "Lv2 0件" in text and "Lv3 0件" in text


def test_list_reply_capped_with_overlong_title(db):
    _seed_edition(db, "AR-000001", "T" * 10000, level=2)
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/list")) == "list"
    assert len(b.transport.sent[-1][1]) <= 4096


def _seed_source(db, name, kind):
    cur = db.execute("INSERT INTO sources(name, kind, status)"
                     " VALUES(?, ?, 'approved')", (name, kind))
    db.commit()
    return cur.lastrowid


def test_status_includes_kind_summary_gaps_deadlines_last_fetch(db):
    now = clock.now_iso(db)
    api = _seed_source(db, "s-api", "api")
    rss = _seed_source(db, "s-rss", "rss")
    # W-check B5 fixture update: /status now shares the health
    # digest's FETCHER-kind axis (collector 中-1) instead of
    # grouping by sources.kind, so the runs carry fetcher_kind
    # and the expected labels are the fetcher taxonomy's.
    for sid, result, fk in ((api, "OK", "API"), (api, "OK", "API"),
                            (api, "BLOCKED", "API"),
                            (rss, "HTTP_ERROR", "RSS")):
        db.execute("INSERT INTO fetch_runs(source_id, started_at,"
                   " finished_at, result, fetcher_kind)"
                   " VALUES(?, ?, ?, ?, ?)",
                   (sid, now, now, result, fk))
    db.execute("INSERT INTO observation_gaps(scope, gap_start, declared_at)"
               " VALUES('source:1', ?, ?)", (now, now))
    db.execute("INSERT INTO observation_gaps(scope, gap_start, declared_at)"
               " VALUES('source:2', ?, ?)", (now, now))
    db.execute("INSERT INTO release_schedule(channel, deadline_confidence)"
               " VALUES('web', 'confirmed')")
    db.execute("INSERT INTO release_schedule(channel, deadline_confidence)"
               " VALUES('web', 'tz_unknown')")
    db.execute("INSERT INTO release_schedule(channel, deadline_confidence)"
               " VALUES('web', NULL)")
    db.commit()
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/status")) == "status"
    text = b.transport.sent[-1][1]
    assert "API: 2/0/1" in text       # OK / fail / BLOCKED, fetcher kind
    assert "RSS: 0/1/0" in text
    assert "最終フェッチ時刻: " + now in text
    # B5: the label names what is counted — OPEN gaps only
    # (collector 中-2); both seeded gaps are open here.
    assert "観測ギャップ（未解消）: 2件" in text
    # R19: /status now uses the ONE canonical unconfirmed
    # definition (deadlines_ops.unconfirmed_count): the
    # 'confirmed' row here has deadline_jst NULL and kind NULL —
    # it can never fire, so it counts as unconfirmed too.
    # tz_unknown + NULL-confidence + confirmed-but-instantless = 3.
    assert "締切未確定: 3件" in text


def test_status_empty_db_reports_no_fetch(db):
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/status")) == "status"
    text = b.transport.sent[-1][1]
    assert "（取得実績なし）" in text
    assert "最終フェッチ時刻: なし" in text
    assert "観測ギャップ（未解消）: 0件" in text
    assert "締切未確定: 0件" in text


# ===========================================================================
# /quiet (ON/OFF wording, hours change 9-C23) and /digest (9-C23)
# ===========================================================================

def test_quiet_show_uses_on_off_not_raw_value(db):
    b = _registered_bot(db)  # gate choice was quiet_off -> stored "0"
    assert b.handle_update(_msg(111, "/quiet")) == "quiet_show"
    text = b.transport.sent[-1][1]
    assert "現在値: OFF" in text and "現在値: 0" not in text
    assert b.handle_update(_msg(111, "/quiet on")) == "quiet_set"
    assert b.handle_update(_msg(111, "/quiet")) == "quiet_show"
    text = b.transport.sent[-1][1]
    assert "現在値: ON" in text and "現在値: 1" not in text
    assert "22:00-06:30" in text  # current quiet hours are shown too


def test_quiet_invalid_values_show_without_change(db):
    b = _registered_bot(db)
    for text in ("/quiet ON", "/quiet bogus", "/quiet On"):
        assert b.handle_update(_msg(111, text)) == "quiet_show", text
    assert get_setting(db, "deadline_override_quiet") == "0"
    # extra args after a valid value: first arg wins (existing behaviour)
    assert b.handle_update(_msg(111, "/quiet on extra")) == "quiet_set"
    assert get_setting(db, "deadline_override_quiet") == "1"


def test_quiet_hours_change_via_policy(db):
    b = _registered_bot(db)
    versions_before = db.execute(
        "SELECT COUNT(*) FROM policy_versions").fetchone()[0]
    assert b.handle_update(_msg(111, "/quiet 23:15-07:45")) \
        == "quiet_hours_set"
    pol = policy_mod.get_policy(db)
    assert pol["quiet"] == {"start": "23:15", "end": "07:45"}
    versions_after = db.execute(
        "SELECT COUNT(*) FROM policy_versions").fetchone()[0]
    assert versions_after == versions_before + 1  # versioned change
    assert b.handle_update(_msg(111, "/quiet")) == "quiet_show"
    assert "23:15-07:45" in b.transport.sent[-1][1]


@pytest.mark.parametrize("rng", ["22:00-22:00", "25:00-06:00", "24:00-06:30",
                                 "7:00-6:00", "22:60-06:30", "22:00",
                                 "22:00~06:30", "ab:cd-ef:gh"])
def test_quiet_hours_invalid_ranges_do_not_change(db, rng):
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, "/quiet " + rng)) == "quiet_show", rng
    pol = policy_mod.get_policy(db)
    assert pol["quiet"] == {"start": "22:00", "end": "06:30"}


def test_digest_flag_roundtrip(db):
    b = _registered_bot(db)
    # default is ON (digests have always been sent daily)
    assert b.handle_update(_msg(111, "/digest")) == "digest_show"
    assert "現在値: ON" in b.transport.sent[-1][1]
    assert get_setting(db, "digest_enabled") is None  # unset == ON
    assert b.handle_update(_msg(111, "/digest off")) == "digest_set"
    assert get_setting(db, "digest_enabled") == "0"
    assert b.handle_update(_msg(111, "/digest")) == "digest_show"
    assert "現在値: OFF" in b.transport.sent[-1][1]
    assert b.handle_update(_msg(111, "/digest on")) == "digest_set"
    assert get_setting(db, "digest_enabled") == "1"
    for text in ("/digest ON", "/digest bogus"):
        assert b.handle_update(_msg(111, text)) == "digest_show", text
    assert get_setting(db, "digest_enabled") == "1"


# ===========================================================================
# Unknown commands and text-less updates
# ===========================================================================

# v2.0 note: /price and /queue left this list — Phase 2 made them real
# commands (prompt v2 section 7, Phase 2 acceptance #11).
UNKNOWN_TEXTS = ["/help", "/foo", "hello", "🎵", "／watch AR-000001",
                 "", "   ", "/", "/level AR-000001 2", "/want AR-000001",
                 "/pause", "/resume"]


@pytest.mark.parametrize("text", UNKNOWN_TEXTS)
def test_unknown_commands(db, text):
    b = _registered_bot(db)
    assert b.handle_update(_msg(111, text)) == "unknown_command", repr(text)


def test_textless_updates_do_not_execute(db):
    _seed_edition(db)
    b = _registered_bot(db)
    photo = _raw_msg(111, {"photo": [{"file_id": "x"}],
                           "caption": "/watch AR-000001"})
    assert b.handle_update(photo) == "unknown_command"
    sticker = _raw_msg(111, {"sticker": {"file_id": "y"}})
    assert b.handle_update(sticker) == "unknown_command"
    voice = _raw_msg(111, {"voice": {"file_id": "z"}})
    assert b.handle_update(voice) == "unknown_command"
    lvl = db.execute("SELECT watch_level FROM editions WHERE id='AR-000001'"
                     ).fetchone()[0]
    assert lvl == 0  # the caption was NOT treated as a command


# ===========================================================================
# Update envelopes: edited / channel / missing parts / groups / types
# ===========================================================================

def test_edited_and_channel_posts_never_execute(db):
    _seed_edition(db)
    b = _registered_bot(db)
    sent_before = len(b.transport.sent)
    edited = {"update_id": next(_uids),
              "edited_message": {"chat": {"id": 111},
                                 "text": "/watch AR-000001"}}
    assert b.handle_update(edited) == "unsupported_update"
    channel = {"update_id": next(_uids),
               "channel_post": {"chat": {"id": 111}, "text": "/status"}}
    assert b.handle_update(channel) == "unsupported_update"
    assert len(b.transport.sent) == sent_before
    lvl = db.execute("SELECT watch_level FROM editions WHERE id='AR-000001'"
                     ).fetchone()[0]
    assert lvl == 0


def test_missing_pieces_are_classified(db):
    b = _registered_bot(db)
    assert b.handle_update({"update_id": next(_uids)}) == "no_chat"
    assert b.handle_update({}) == "no_chat"
    no_chat_msg = {"update_id": next(_uids), "message": {"text": "/status"}}
    assert b.handle_update(no_chat_msg) == "no_chat"
    cb_no_msg = {"update_id": next(_uids),
                 "callback_query": {"id": "q1", "data": "quiet_on"}}
    assert b.handle_update(cb_no_msg) == "no_chat"
    cb_no_chat = {"update_id": next(_uids),
                  "callback_query": {"id": "q2", "message": {},
                                     "data": "quiet_on"}}
    assert b.handle_update(cb_no_chat) == "no_chat"
    weird_msg = {"update_id": next(_uids), "message": "not-a-dict"}
    assert b.handle_update(weird_msg) == "no_chat"
    weird_cb = {"update_id": next(_uids), "callback_query": 42,
                "message": {"chat": {"id": 111}, "text": "/status"}}
    assert b.handle_update(weird_cb) == "status"


def test_group_chat_registers_and_operates(db):
    # Round 6 reference finding, pinned: the spec has no private-only
    # rule, so a group chat that sends /start first becomes the allowed
    # chat (its id is negative) and can operate the bot.
    b = _bot(db)
    grp = -1001234567
    assert b.handle_update(_msg(grp, "/start", chat_type="group")) \
        == "registered"
    assert get_setting(db, "allowed_chat_id") == str(grp)
    assert b.handle_update(_cb(grp, "quiet_off")) == "quiet_selected"
    assert b.handle_update(_msg(grp, "/status", chat_type="group")) \
        == "status"
    other = -1009999999
    assert b.handle_update(_msg(other, "/status", chat_type="group")) \
        == "ignored_foreign"
    assert b.handle_update(_msg(111, "/status")) == "ignored_foreign"


BAD_UPDATE_IDS = [10 ** 30, -(10 ** 30), "abc", 1.5, True, [1], {"x": 1}]


@pytest.mark.parametrize("uid", BAD_UPDATE_IDS)
def test_bad_update_ids_are_invalid_not_raised(db, uid):
    b = _registered_bot(db)
    upd = {"update_id": uid,
           "message": {"chat": {"id": 111}, "text": "/status"}}
    assert b.handle_update(upd) == "invalid_update"


def test_edge_update_ids_still_work(db):
    b = _registered_bot(db)
    for uid in (0, -5, 2 ** 62):
        upd = {"update_id": uid,
               "message": {"chat": {"id": 111}, "text": "/status"}}
        assert b.handle_update(upd) == "status", uid
    # missing update_id: processed without dedup recording
    upd = {"message": {"chat": {"id": 111}, "text": "/status"}}
    assert b.handle_update(upd) == "status"


def test_non_dict_updates_are_invalid(db):
    b = _registered_bot(db)
    for upd in ("nonsense", None, [1, 2], 42):
        assert b.handle_update(upd) == "invalid_update"


@pytest.mark.parametrize("bad_text", [12345, 1.5, ["/status"], {"t": 1}, True])
def test_non_string_text_is_invalid(db, bad_text):
    b = _registered_bot(db)
    upd = {"update_id": next(_uids),
           "message": {"chat": {"id": 111}, "text": bad_text}}
    assert b.handle_update(upd) == "invalid_update"


@pytest.mark.parametrize("bad_chat", [111.0, [111], {"id": 111}, True])
def test_bad_chat_id_types_are_invalid(db, bad_chat):
    b = _registered_bot(db)
    upd = {"update_id": next(_uids),
           "message": {"chat": {"id": bad_chat}, "text": "/status"}}
    assert b.handle_update(upd) == "invalid_update"


def test_string_chat_id_matching_allowed_works(db):
    b = _registered_bot(db)
    upd = {"update_id": next(_uids),
           "message": {"chat": {"id": "111"}, "text": "/status"}}
    assert b.handle_update(upd) == "status"


# ===========================================================================
# Setup gate (9-B10 note): nothing but /status and the quiet choice
# goes through while the gate is closed - candidate callbacks included.
# ===========================================================================

def test_gate_closed_blocks_commands_and_candidate_callbacks(db):
    _seed_edition(db)
    cid = _seed_candidate(db)
    b = _bot(db)
    assert b.handle_update(_msg(111, "/start")) == "registered"
    assert not bot_mod.gate_open(db)
    # /status is the one command allowed through the closed gate
    assert b.handle_update(_msg(111, "/status")) == "status"
    for text in ("/watch AR-000001", "/list", "/quiet on", "/digest off",
                 "/help", "hello"):
        assert b.handle_update(_msg(111, text)) == "gate_closed", text
    assert get_setting(db, "deadline_override_quiet") is None
    assert get_setting(db, "digest_enabled") is None
    # candidate callbacks are answered but NOT processed behind the gate
    for data in (f"approve:{cid}", f"reject:{cid}", f"rej:{cid}:OTHER",
                 "some_unknown_data"):
        action = b.handle_update(_cb(111, data, cbid="cb-" + data[:6]))
        assert action == "gate_closed", data
        assert b.transport.answered[-1] == "cb-" + data[:6]
    row = db.execute("SELECT status FROM candidate_editions WHERE id=?",
                     (cid,)).fetchone()
    assert row[0] == "pending"
    assert any("初期設定が未完了" in t for _, t in b.transport.sent)
    # the quiet choice itself opens the gate, then callbacks work
    assert b.handle_update(_cb(111, "quiet_off")) == "quiet_selected"
    assert bot_mod.gate_open(db)
    assert b.handle_update(_cb(111, f"approve:{cid}")) \
        == "candidate_approved"


# ===========================================================================
# Callback matrix (gate open)
# ===========================================================================

def test_approve_resolves_exactly_one_queue_row(db):
    cids = [_seed_candidate(db, payload_style="head") for _ in range(3)]
    tail = _seed_candidate(db, payload_style="tail")
    b = _registered_bot(db)
    assert b.handle_update(_cb(111, f"approve:{cids[0]}")) \
        == "candidate_approved"
    resolved = db.execute(
        "SELECT payload_json FROM review_queue WHERE resolved_at IS NOT NULL"
    ).fetchall()
    assert len(resolved) == 1 and f'"candidate_id": {cids[0]},' in resolved[0][0]
    # payload with candidate_id as the last JSON key also resolves exactly
    assert b.handle_update(_cb(111, f"approve:{tail}")) \
        == "candidate_approved"
    resolved = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NOT NULL"
    ).fetchone()[0]
    assert resolved == 2


def test_approve_missing_and_resolved(db):
    cid = _seed_candidate(db)
    b = _registered_bot(db)
    assert b.handle_update(_cb(111, "approve:999999")) == "candidate_missing"
    assert b.handle_update(_cb(111, "approve:-1")) == "candidate_missing"
    assert b.handle_update(_cb(111, "approve:007")) == "candidate_missing"
    # 18 digits: inside SQLite range, parsed, simply not found
    assert b.handle_update(_cb(111, "approve:999999999999999999")) \
        == "candidate_missing"
    assert b.handle_update(_cb(111, f"approve:{cid}")) == "candidate_approved"
    assert b.handle_update(_cb(111, f"approve:{cid}")) == "already_resolved"
    assert b.handle_update(_cb(111, f"reject:{cid}")) == "already_resolved"


def test_reject_flow_and_every_reason(db):
    b = _registered_bot(db)
    cid = _seed_candidate(db)
    assert b.handle_update(_cb(111, f"reject:{cid}")) \
        == "reject_reason_asked"
    markup = b.transport.markups[-1]
    datas = [btn["callback_data"] for row in markup["inline_keyboard"]
             for btn in row]
    assert datas == [f"rej:{cid}:{code}" for code in bot_mod.REJECT_REASONS]
    assert b.handle_update(_cb(111, "reject:424242")) == "candidate_missing"
    for code in bot_mod.REJECT_REASONS:
        c = _seed_candidate(db)
        assert b.handle_update(_cb(111, f"rej:{c}:{code}")) \
            == "candidate_rejected", code
        row = db.execute("SELECT status, reject_reason FROM"
                         " candidate_editions WHERE id=?", (c,)).fetchone()
        assert tuple(row) == ("rejected", code)


MALFORMED_CALLBACKS_R1 = [
    "approve:", "approve:abc", "approve:1.5", "approve:0x10", "approve:+1",
    "approve: 1", "approve:1 ", "approve:1:2", "approve:🎵",
    pytest.param("approve:" + "9" * 100000, id="approve:100000-digit-id"),
    "approve:9999999999999999999",            # 19 digits: beyond id range
    "reject:", "reject:abc", "reject:1.5", "reject:1:extra",
    "rej:", "rej:1", "rej:1:", "rej::OTHER", "rej:abc:OTHER",
    "rej:1:other",                            # reason codes are uppercase
    "rej:1:OTHER ", "rej:1:OTHER:EXTRA", "rej:1:🎵",
    "rej:1:TOTALLY_MADE_UP", "rej:1:X';DROP/**/TABLE/**/editions;--",
    "rej:99999999999999999999999999:OTHER",
    "quiet_on ", " quiet_on", "quiet", "QUIET_ON", "Quiet_off",
    "", "approve", "foo:1:OTHER", "unknown_data",
]


@pytest.mark.parametrize("data", MALFORMED_CALLBACKS_R1)
def test_malformed_callbacks_reply_error_and_keep_pending(db, data):
    cid = _seed_candidate(db)
    b = _registered_bot(db)
    sent_before = len(b.transport.sent)
    action = b.handle_update(_cb(111, data))  # must never raise
    assert action == "unknown_callback", repr(data)
    # an error reply is sent (never silent), and nothing was resolved
    assert len(b.transport.sent) == sent_before + 1
    assert "形式が不正" in b.transport.sent[-1][1]
    row = db.execute("SELECT status, reject_reason FROM candidate_editions"
                     " WHERE id=?", (cid,)).fetchone()
    assert tuple(row) == ("pending", None)


def test_callback_data_none_missing_and_numeric(db):
    cid = _seed_candidate(db)
    b = _registered_bot(db)
    none_cb = {"update_id": next(_uids),
               "callback_query": {"id": "q-none",
                                  "message": {"chat": {"id": 111}},
                                  "data": None}}
    assert b.handle_update(none_cb) == "unknown_callback"
    assert b.transport.answered[-1] == "q-none"
    missing_cb = {"update_id": next(_uids),
                  "callback_query": {"id": "q-miss",
                                     "message": {"chat": {"id": 111}}}}
    assert b.handle_update(missing_cb) == "unknown_callback"
    numeric_cb = {"update_id": next(_uids),
                  "callback_query": {"id": "q-num",
                                     "message": {"chat": {"id": 111}},
                                     "data": 123}}
    assert b.handle_update(numeric_cb) == "invalid_update"
    row = db.execute("SELECT status FROM candidate_editions WHERE id=?",
                     (cid,)).fetchone()
    assert row[0] == "pending"


def test_callback_without_id_still_processes(db):
    cid = _seed_candidate(db)
    b = _registered_bot(db)
    answered_before = list(b.transport.answered)
    upd = {"update_id": next(_uids),
           "callback_query": {"message": {"chat": {"id": 111}},
                              "data": f"approve:{cid}"}}
    assert b.handle_update(upd) == "candidate_approved"
    # nothing new was answered for the id-less query, and nothing crashed
    assert b.transport.answered == answered_before


def test_quiet_callbacks_flip_setting(db):
    b = _registered_bot(db)
    assert b.handle_update(_cb(111, "quiet_on")) == "quiet_selected"
    assert get_setting(db, "deadline_override_quiet") == "1"
    assert b.handle_update(_cb(111, "quiet_off")) == "quiet_selected"
    assert get_setting(db, "deadline_override_quiet") == "0"
