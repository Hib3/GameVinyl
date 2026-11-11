"""Telegram bot: commands, candidate approval buttons, initial setup gate.

Phase 1 command set: /start /watch /unwatch /list /status (+ /quiet for the
quiet-exception setting and quiet hours, /digest for the daily-digest
flag). Only the allowed chat_id is served; other chats are silently
ignored. Update handling is idempotent via telegram_updates
(agreement-08): reprocessing the same update never double-executes.
Initial operation does not start until the quiet-exception choice is made
(agreement-28 / acceptance #10) - the gate state is always detectable.

Robustness (QC round 6, R6-D1/D5/D6/D8 and round 1 coverage): update
envelopes, command tokens and callback data are parsed defensively.
Malformed updates / callbacks never raise out of handle_update; they are
classified ("invalid_update" / "unknown_callback" / ...) and, where a
chat is known, answered with an error message. Arguments echoed back to
the user are truncated so a reply can never exceed Telegram's 4,096
character limit.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import timedelta

from . import clock, outbox as outbox_mod
from . import policy as policy_mod
from . import version as version_mod
from .config import get_setting, set_setting
from .migrate import current_policy_version
from .telegram_io import TelegramError


class TransientDeliveryError(Exception):
    """A reply send failed TRANSIENTLY (Telegram 429/5xx, timeout).

    W-check B2 (adversarial QA D-3): _send failures used to fall into
    handle_update's generic containment and be classified
    'invalid_update' — recorded as handled while the poll loop had
    already advanced the offset, so the user's command and its reply
    were lost permanently. A transient failure now un-records the
    update and returns 'transient_error', and the poll loop keeps the
    offset behind it so Telegram redelivers it.
    """

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after

TZ_CHOICES = ["Asia/Tokyo", "UTC", "America/New_York", "Europe/London"]

REJECT_REASONS = ["NOT_GAME_MUSIC", "NOT_VINYL", "DUPLICATE",
                  "INSUFFICIENT_EVIDENCE", "OTHER"]

TELEGRAM_MESSAGE_LIMIT = 4096
ECHO_LIMIT = 64

_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1

# /start is a command only as a whole token: "/start" or "/start@<botname>".
# Lookalikes such as "/startxxx" must never register (R6-D8).
_START_TOKEN_RE = re.compile(r"^/start(@[A-Za-z0-9_]+)?$")
# Candidate ids in callbacks: an optionally negative decimal integer of at
# most 18 digits (always inside SQLite's INTEGER range; R6-D1).
# W-check B5 (adversarial QA): the digit class is ASCII [0-9], not \d —
# Python's \d matches Unicode decimal digits and int() parses them, so
# a full-width '１' used to be accepted as candidate id 1, against the
# strict-grammar discipline applied to every other callback (B4 tzfix).
_CANDIDATE_ID_RE = re.compile(r"^-?[0-9]{1,18}$")
# Quiet-hours range for /quiet HH:MM-HH:MM (24h clock, zero padded).
_RARITY_RANGE_RE = re.compile(r"^R([1-5])\s*[-~]?\s*(?:\.\.)?\s*R?([1-5])$")

_QUIET_RANGE_RE = re.compile(
    r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")

_GATE_MESSAGE = ("初期設定が未完了です。まず静穏例外（締切が近い場合に"
                 "夜間でも即時通知するか）の選択を完了してください。")
_CALLBACK_ERROR_MESSAGE = "ボタンの形式が不正です。操作を受け付けませんでした。"
_DISABLED_REVIEW_SOURCES = frozenset({"eBay Browse used"})


def gate_open(db) -> bool:
    return get_setting(db, "deadline_override_quiet_selected") == "1"


def _is_disabled_api_review(payload) -> bool:
    listing = payload.get("listing") if isinstance(payload, dict) else None
    return (isinstance(listing, dict)
            and listing.get("source") in _DISABLED_REVIEW_SOURCES)


def _short(value, limit: int = ECHO_LIMIT) -> str:
    """Truncate a user-supplied value before echoing it in a reply."""
    text = str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _is_valid_update_id(uid) -> bool:
    return (isinstance(uid, int) and not isinstance(uid, bool)
            and _SQLITE_INT_MIN <= uid <= _SQLITE_INT_MAX)


def _parse_candidate_id(text) -> int | None:
    if not isinstance(text, str) or not _CANDIDATE_ID_RE.match(text):
        return None
    try:
        return int(text)
    except (ValueError, OverflowError):
        return None


class Bot:
    def __init__(self, db, transport):
        self.db = db
        self.transport = transport

    # -- plumbing ---------------------------------------------------------
    def _send(self, chat_id, text, reply_markup=None):
        if isinstance(text, str):
            # W-check B5 (adversarial QA 中-1): the production
            # transport serialises with httpx json=, which raises
            # UnicodeEncodeError on a lone surrogate — an exception
            # that is NOT a TelegramError, so it escaped this
            # method and the reply vanished silently (a poisoned
            # /queue presentation wedged the queue head forever).
            # Strip surrogates at the send boundary; one_line also
            # drops them for composed bodies.
            from .normalize import strip_surrogates
            text = strip_surrogates(text)
        if isinstance(text, str) and len(text) > TELEGRAM_MESSAGE_LIMIT:
            text = text[:TELEGRAM_MESSAGE_LIMIT - 1] + "…"
        try:
            try:
                self.transport.send(str(chat_id), text,
                                    reply_markup=reply_markup)
            except TypeError:
                self.transport.send(str(chat_id), text)
        except TelegramError as exc:
            raise TransientDeliveryError(
                str(exc), retry_after=exc.retry_after) from exc

    def handle_update(self, update: dict) -> str:
        if not isinstance(update, dict):
            return "invalid_update"
        uid = update.get("update_id")
        recorded = False
        if uid is not None:
            if not _is_valid_update_id(uid):
                # R6-D6: an update_id that is not an int inside SQLite's
                # INTEGER range cannot be recorded or deduplicated.
                return "invalid_update"
            try:
                self.db.execute(
                    "INSERT INTO telegram_updates(update_id, received_at)"
                    " VALUES(?, ?)", (uid, clock.now_iso(self.db)))
                self.db.commit()
                recorded = True
            except sqlite3.IntegrityError:
                self.db.rollback()
                return "duplicate_ignored"
            except Exception:
                # A DB hiccup (lock past the busy timeout, disk full)
                # while RECORDING the update is transient, not a
                # malformed update: the caller must not advance past
                # it (adversarial QA D-3 case B).
                self.db.rollback()
                return "transient_error"
        try:
            action = self._dispatch(update)
        except TransientDeliveryError:
            # The command may have partially executed (an early reply
            # sent, a later one failed). Un-record the update so the
            # redelivery re-runs it; command handlers are written to
            # answer 'already processed' states rather than corrupt
            # anything on a second run.
            if recorded:
                try:
                    self.db.execute(
                        "DELETE FROM telegram_updates WHERE update_id=?",
                        (uid,))
                    self.db.commit()
                except Exception:
                    try:
                        self.db.rollback()
                    except Exception:
                        pass
            return "transient_error"
        except Exception:
            # Last-resort containment: one malformed update must never
            # propagate an exception to the caller (R6-D1/D6). All known
            # malformed shapes are classified inside _dispatch; this only
            # guards shapes not anticipated there.
            try:
                self.db.rollback()
            except Exception:
                pass
            action = "invalid_update"
        if recorded:
            try:
                self.db.execute(
                    "UPDATE telegram_updates SET handled_at=?, action=?"
                    " WHERE update_id=?",
                    (clock.now_iso(self.db), action, uid))
                self.db.commit()
            except Exception:
                try:
                    self.db.rollback()
                except Exception:
                    pass
        return action

    def _dispatch(self, update: dict) -> str:
        raw_msg = update.get("message")
        raw_cb = update.get("callback_query")
        msg = raw_msg if isinstance(raw_msg, dict) else {}
        cb = raw_cb if isinstance(raw_cb, dict) else {}

        if not msg and not cb:
            if any(k in update for k in
                   ("edited_message", "channel_post", "edited_channel_post",
                    "my_chat_member", "chat_member")):
                # Update kinds the bot does not act on. In particular an
                # edited message must never re-execute a command.
                return "unsupported_update"
            return "no_chat"

        if cb:
            cb_msg = cb.get("message")
            cb_msg = cb_msg if isinstance(cb_msg, dict) else {}
            chat = cb_msg.get("chat")
            chat = chat if isinstance(chat, dict) else {}
            chat_id = chat.get("id")
            data_raw = cb.get("data")
            if data_raw is None:
                data = ""
            elif isinstance(data_raw, str):
                # R24 (adversarial QA 軽微-1): strip surrogates
                # once, at the entry — see the text branch below.
                from .normalize import strip_surrogates
                data = strip_surrogates(data_raw)
            else:
                return "invalid_update"
            text = ""
        else:
            chat = msg.get("chat")
            chat = chat if isinstance(chat, dict) else {}
            chat_id = chat.get("id")
            data = ""
            text_raw = msg.get("text")
            if text_raw is None:
                # Photo / sticker / other text-less messages: no command.
                text = ""
            elif isinstance(text_raw, str):
                # W-check B5 R24 (adversarial QA 軽微-1): the
                # B5 surrogate fix guarded the SEND boundary, but
                # command handlers bind arguments to sqlite —
                # CPython's sqlite3 raises UnicodeEncodeError on
                # a lone-surrogate parameter, so /watch, /price
                # and /deadline with a poisoned argument still
                # died silently in the HANDLER layer. Sanitising
                # the text/data once here closes the whole class.
                from .normalize import strip_surrogates
                text = strip_surrogates(text_raw.strip())
            else:
                return "invalid_update"

        if chat_id is None:
            return "no_chat"
        if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
            return "invalid_update"
        if isinstance(chat_id, str) and not chat_id.strip():
            return "invalid_update"

        allowed = get_setting(self.db, "allowed_chat_id")
        token = text.split(maxsplit=1)[0] if (text and not cb) else ""
        is_start = bool(token) and bool(_START_TOKEN_RE.match(token))

        # /start registers the very first chat as the allowed one.
        if is_start and not allowed:
            set_setting(self.db, "allowed_chat_id", str(chat_id))
            allowed = str(chat_id)
            self._send(chat_id, "登録しました。このチャットが操作用チャットです。")
            if not gate_open(self.db):
                self._ask_quiet_choice(chat_id)
            return "registered"

        if not allowed or str(chat_id) != str(allowed):
            return "ignored_foreign"

        if cb:
            self._answer_callback(cb.get("id"))
            if not gate_open(self.db) and data not in ("quiet_on", "quiet_off"):
                # 9-B10 note: while the setup gate is closed, candidate
                # callbacks are NOT processed either; only the quiet
                # choice itself goes through.
                self._send(chat_id, _GATE_MESSAGE)
                self._ask_quiet_choice(chat_id)
                return "gate_closed"
            return self._handle_callback(chat_id, data)

        if is_start:
            if not gate_open(self.db):
                self._ask_quiet_choice(chat_id)
            else:
                self._send(chat_id, "稼働中です。/status で状態を確認できます。")
            return "start"

        parts = text.split()
        cmd = parts[0] if parts else ""
        args = parts[1:]

        if not gate_open(self.db) and cmd != "/status":
            self._send(chat_id, _GATE_MESSAGE)
            self._ask_quiet_choice(chat_id)
            return "gate_closed"

        if cmd == "/watch":
            return self._cmd_watch(chat_id, args, on=True)
        if cmd == "/unwatch":
            return self._cmd_watch(chat_id, args, on=False)
        if cmd == "/list":
            return self._cmd_list(chat_id)
        if cmd == "/status":
            return self._cmd_status(chat_id)
        if cmd == "/quiet":
            return self._cmd_quiet(chat_id, args)
        if cmd == "/digest":
            return self._cmd_digest(chat_id, args)
        if cmd == "/price":
            return self._cmd_price(chat_id, args)
        if cmd == "/queue":
            return self._cmd_queue(chat_id)
        if cmd == "/deadlines":
            return self._cmd_deadlines(chat_id)
        if cmd == "/deadline":
            return self._cmd_deadline(chat_id, args)
        if cmd == "/deadline_cancel":
            return self._cmd_deadline_cancel(chat_id, args)
        if cmd == "/coverage":
            return self._cmd_coverage(chat_id)
        self._send(chat_id,
                   "不明なコマンドです。/watch /unwatch /list /status"
                   " /quiet /digest /price /queue /deadlines /deadline"
                   " /deadline_cancel /coverage")
        return "unknown_command"

    # -- setup ------------------------------------------------------------
    def _quiet_hours(self) -> tuple[str, str]:
        try:
            quiet = policy_mod.get_policy(self.db).get("quiet", {})
            return (str(quiet.get("start", "22:00")),
                    str(quiet.get("end", "06:30")))
        except Exception:
            return "22:00", "06:30"

    def _ask_quiet_choice(self, chat_id):
        start, end = self._quiet_hours()
        markup = {"inline_keyboard": [[
            {"text": "静穏例外 OFF（推奨）", "callback_data": "quiet_off"},
            {"text": "静穏例外 ON", "callback_data": "quiet_on"},
        ]]}
        self._send(
            chat_id,
            "初期設定: 静穏時間（" + start + "-" + end + "）の例外通知を"
            "設定してください。\n"
            "ONにすると、確定締切が24時間以内・受注生産または最優先ウォッチの"
            "場合に限り、夜間でも即時通知します。推奨は OFF です。",
            reply_markup=markup)

    def _set_quiet(self, on: bool, via: str) -> None:
        set_setting(self.db, "deadline_override_quiet", "1" if on else "0")
        set_setting(self.db, "deadline_override_quiet_selected", "1")
        self.db.execute(
            "INSERT INTO policy_versions(version_no, changed_at, changed_via,"
            " diff_json, note) VALUES(?, ?, ?, ?, ?)",
            (current_policy_version(self.db) + 1, clock.now_iso(self.db), via,
             json.dumps({"quiet_exception": on}), "quiet exception selection"))
        self.db.commit()

    def _answer_callback(self, callback_query_id) -> None:
        """Stop the Telegram client's button spinner for this tap.

        Best effort: answering an old/expired query legitimately fails,
        and the handling result is always reported by message as well,
        so a failed answer must never block the handling itself.
        """
        if callback_query_id is None:
            return
        answer = getattr(self.transport, "answer_callback_query", None)
        if answer is None:
            return
        try:
            answer(str(callback_query_id))
        except Exception:
            pass

    # -- callbacks ----------------------------------------------------------
    def _callback_error(self, chat_id) -> str:
        self._send(chat_id, _CALLBACK_ERROR_MESSAGE)
        return "unknown_callback"

    def _handle_callback(self, chat_id, data: str) -> str:
        if data is None:
            data = ""
        if not isinstance(data, str):
            return self._callback_error(chat_id)
        if data in ("quiet_on", "quiet_off"):
            self._set_quiet(data == "quiet_on", "telegram_setup")
            self._send(chat_id,
                       "静穏例外を " + ("ON" if data == "quiet_on" else "OFF")
                       + " に設定しました。通常運用を開始できます。")
            return "quiet_selected"
        if data.startswith("tzfix:"):
            parts = data.split(":")
            if len(parts) != 3:
                return self._callback_error(chat_id)
            # W-check B4 (adversarial QA): this branch's numeric
            # grammar was looser than the candidate callbacks' —
            # int() accepted "1_0" as 10 and huge ids raised
            # OverflowError with no user-facing reply, and a
            # NEGATIVE index wrapped TZ_CHOICES[-1] to London.
            # Same strict grammar + explicit range checks now.
            sid = _parse_candidate_id(parts[1])
            idx = _parse_candidate_id(parts[2])
            if sid is None or sid < 0 or idx is None \
                    or not (0 <= idx < len(TZ_CHOICES)):
                return self._callback_error(chat_id)
            tz = TZ_CHOICES[idx]
            from . import deadlines_ops
            if deadlines_ops.confirm_with_tz(self.db, sid, tz):
                row = self.db.execute(
                    "SELECT deadline_jst FROM release_schedule"
                    " WHERE id=?", (sid,)).fetchone()
                self._send(chat_id,
                           f"締切 #{sid} を {tz} として確定しました"
                           f"（JST: {row[0]}）。")
                return "deadline_confirmed"
            # R19 (adversarial QA): EVERY refusal used to blame the
            # raw text ("解析できず") — including refusals because
            # the row was already confirmed, where the text parses
            # fine and the message sent the user debugging the
            # wrong thing. Name the actual reason.
            state = self.db.execute(
                "SELECT deadline_confidence, superseded_by FROM"
                " release_schedule WHERE id=?", (sid,)).fetchone()
            if state is not None and (state[0] == "confirmed"
                                      or state[1] is not None):
                self._send(chat_id,
                           f"締切 #{sid} は既に確定済み（または"
                           "置換済み）のため、変更できません。")
            else:
                self._send(chat_id,
                           f"締切 #{sid} の原文を日時として解析"
                           "できず、確定できませんでした。")
            return "deadline_confirm_failed"
        if data.startswith("qok:") or data.startswith("qno:"):
            # W-check B4: generic review-queue items (match_review /
            # sweep_hit / copy_bundle_review / seed_promotion) are
            # resolved by QUEUE ROW id — these kinds had no exit
            # from Telegram at all before this.
            qid = _parse_candidate_id(data[4:])
            if qid is None or qid < 0:
                return self._callback_error(chat_id)
            return self._resolve_queue_item(
                chat_id, qid, data.startswith("qok:"))
        if data.startswith("approve:"):
            cid = _parse_candidate_id(data[len("approve:"):])
            if cid is None:
                return self._callback_error(chat_id)
            return self._resolve_candidate(chat_id, cid, "approved", None)
        if data.startswith("reject:"):
            cid = _parse_candidate_id(data[len("reject:"):])
            if cid is None:
                return self._callback_error(chat_id)
            row = self.db.execute(
                "SELECT status FROM candidate_editions WHERE id=?",
                (cid,)).fetchone()
            if row is None:
                self._send(chat_id, "候補#" + str(cid) + " が見つかりません。")
                return "candidate_missing"
            if row["status"] != "pending":
                self._send(chat_id,
                           "候補#" + str(cid) + " は既に処理済みです（"
                           + str(row["status"]) + "）。二重処理はしません。")
                return "already_resolved"
            markup = {"inline_keyboard": [[
                {"text": code, "callback_data": "rej:" + str(cid) + ":" + code}
            ] for code in REJECT_REASONS]}
            self._send(chat_id, "候補#" + str(cid) + " の却下理由を選んでください。",
                       reply_markup=markup)
            return "reject_reason_asked"
        if data.startswith("rej:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                return self._callback_error(chat_id)
            cid = _parse_candidate_id(parts[1])
            code = parts[2]
            # R6-D4: the reason code must be one of REJECT_REASONS;
            # anything else is rejected without touching the candidate.
            if cid is None or code not in REJECT_REASONS:
                return self._callback_error(chat_id)
            return self._resolve_candidate(chat_id, cid, "rejected", code)
        return self._callback_error(chat_id)

    def _resolve_candidate(self, chat_id, cid: int, status: str,
                           reason) -> str:
        row = self.db.execute(
            "SELECT * FROM candidate_editions WHERE id=?", (cid,)).fetchone()
        if row is None:
            self._send(chat_id, "候補#" + str(cid) + " が見つかりません。")
            return "candidate_missing"
        if row["status"] != "pending":
            self._send(chat_id,
                       "候補#" + str(cid) + " は既に処理済みです（"
                       + str(row["status"]) + "）。二重処理はしません。")
            return "already_resolved"
        self.db.execute(
            "UPDATE candidate_editions SET status=?, reject_reason=?"
            " WHERE id=?", (status, reason, cid))
        if status == "rejected":
            # R19 (seller): the candidate's own REPRESS signals are
            # withdrawn with it — a bridged announcement signal
            # keyed to an edition kept printing in the digest notes
            # for 30 days after the human rejected the discovery
            # that produced it.
            self.db.execute(
                "DELETE FROM repress_signals WHERE candidate_id=?",
                (cid,))
        # W-check B4 (maniac B): approval used to be a dead end —
        # status flipped to 'approved', a value NO code reads, and
        # the discovery vanished (no edition, no watch, no list).
        # Approval now CATALOGUES the candidate: an editions row is
        # created (watch_level 0 — the user raises it with /watch),
        # the candidate records the link, its REPRESS signals
        # migrate to the edition's key space, and its schedules
        # gain the edition id.
        promoted_id = None
        if status == "approved":
            promoted_id = self._catalogue_candidate(row)
        # R6-D2: resolve ONLY the queue rows whose payload actually
        # carries this candidate_id. The payloads are produced with
        # json.dumps (e.g. '{"candidate_id": 1, "source": "blipblop"}'),
        # so compare the parsed value exactly instead of a LIKE prefix
        # match, which also caught candidates 10, 11, 12, ... for id 1.
        # W-check B4 (adversarial QA): the resolver was KIND-BLIND —
        # a seed_promotion row whose payload happened to carry a
        # candidate_id would be resolved 'approved' by an unrelated
        # candidate approval, silently satisfying promote_seed's
        # Gate 1. Only candidate_review rows resolve here.
        now = clock.now_iso(self.db)
        queue_rows = self.db.execute(
            "SELECT id, payload_json FROM review_queue"
            " WHERE resolved_at IS NULL AND kind='candidate_review'"
        ).fetchall()
        for q in queue_rows:
            try:
                payload = json.loads(q["payload_json"] or "")
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) \
                    and isinstance(payload.get("candidate_id"), int) \
                    and not isinstance(payload.get("candidate_id"), bool) \
                    and payload.get("candidate_id") == cid:
                self.db.execute(
                    "UPDATE review_queue SET resolved_at=?, resolution=?"
                    " WHERE id=?", (now, status, q["id"]))
        self.db.commit()
        label = "承認" if status == "approved" else "却下（" + str(reason) + "）"
        msg = "候補#" + str(cid) + " を" + label + "しました。"
        if promoted_id:
            msg += ("\n目録に登録しました: " + promoted_id
                    + "（監視レベル0 — /watch で有効化できます）")
        self._send(chat_id, msg)
        return "candidate_" + status

    def _catalogue_candidate(self, cand) -> str | None:
        """Create (or find) the editions row for an approved
        candidate (W-check B4). Idempotent: a candidate already
        catalogued returns its existing edition id.

        R19 (seller / AI engineer / adversarial QA): approval used
        to mint a fresh AR-C row UNCONDITIONALLY — approving a
        candidate whose announcement had already bridged to a
        catalogued edition created a SECOND edition of the same
        work and split its price evidence, watch and deadlines
        across both; and an AR-C id already taken by a different
        work was silently adopted as the promotion target. The
        catalogue step now resolves, in order: (1) the candidate's
        own recorded link, verified to exist; (2) the edition its
        REPRESS signals already name; (3) a UNIQUE folded-title
        match against the catalogue; (4) a fresh AR-C row — with
        the id checked against different-work collisions."""
        cid = cand["id"]
        if cand["promoted_edition_id"]:
            exists = self.db.execute(
                "SELECT 1 FROM editions WHERE id=?",
                (cand["promoted_edition_id"],)).fetchone()
            if exists is not None:
                return cand["promoted_edition_id"]
            # Stale pointer (edition row gone): fall through and
            # catalogue afresh instead of reporting a phantom id.
        edition_id = None
        sig = self.db.execute(
            "SELECT DISTINCT edition_id FROM repress_signals"
            " WHERE candidate_id=? AND edition_id IS NOT NULL",
            (cid,)).fetchall()
        if len(sig) == 1:
            eid = sig[0][0]
            if self.db.execute(
                    "SELECT 1 FROM editions WHERE id=?",
                    (eid,)).fetchone() is not None:
                edition_id = eid
        if edition_id is None:
            from .normalize import normalize_title
            want = normalize_title(cand["title"]).replace("・", "")
            if want:
                hits = [r[0] for r in self.db.execute(
                    "SELECT id, title FROM editions").fetchall()
                    if normalize_title(r[1]).replace("・", "")
                    == want]
                if len(hits) == 1:
                    edition_id = hits[0]
        if edition_id is None:
            from .normalize import normalize_title
            base = f"AR-C{cid:05d}"
            edition_id = base
            suffix = ""
            while True:
                ex = self.db.execute(
                    "SELECT title FROM editions WHERE id=?",
                    (edition_id,)).fetchone()
                if ex is None:
                    self.db.execute(
                        "INSERT INTO editions(id, title, catalog,"
                        " catalog_norm, distribution_type,"
                        " distribution_channel, channel_source_url,"
                        " watch_level) VALUES(?,?,?,?,?,?,?,0)",
                        (edition_id, cand["title"],
                         cand["catalog_norm"], cand["catalog_norm"],
                         cand["distribution_type"],
                         cand["distribution_channel"],
                         cand["channel_source_url"]
                         or cand["source_url"]))
                    break
                if normalize_title(ex[0]).replace("・", "") \
                        == normalize_title(cand["title"]) \
                        .replace("・", ""):
                    break  # our own row from an earlier approval
                suffix = chr(ord(suffix) + 1) if suffix else "B"
                edition_id = base + suffix
        self.db.execute(
            "UPDATE candidate_editions SET promoted_edition_id=?"
            " WHERE id=?", (edition_id, cid))
        from . import repress as _repress
        _repress.migrate_candidate_signals(self.db, cid, edition_id)
        self.db.execute(
            "UPDATE release_schedule SET edition_id=?"
            " WHERE candidate_id=? AND edition_id IS NULL",
            (edition_id, cid))
        return edition_id

    # -- generic review-queue items (W-check B4) ---------------------
    # Before B4 the queue had exactly one exit: candidate_review's
    # status flip. match_review / sweep_hit / copy_bundle_review /
    # seed_promotion rows could not be resolved from Telegram at
    # all, and resolving them by hand changed nothing (no consumer
    # read the resolution). Each kind now has a resolver here, and
    # /queue presents all of them.

    def _settle_queue_row(self, qid: int, resolution: str) -> None:
        self.db.execute(
            "UPDATE review_queue SET resolved_at=?, resolution=?"
            " WHERE id=?",
            (clock.now_iso(self.db), resolution, qid))

    def _resolve_queue_item(self, chat_id, qid: int,
                            approve: bool) -> str:
        row = self.db.execute(
            "SELECT * FROM review_queue WHERE id=?", (qid,)).fetchone()
        if row is None:
            self._send(chat_id, "キュー項目#" + str(qid)
                       + " が見つかりません。")
            return "queue_missing"
        if row["resolved_at"] is not None:
            self._send(chat_id, "キュー項目#" + str(qid)
                       + " は既に処理済みです。二重処理はしません。")
            return "already_resolved"
        try:
            payload = json.loads(row["payload_json"] or "")
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        kind = row["kind"]
        if kind == "match_review" and _is_disabled_api_review(payload):
            self._settle_queue_row(qid, "source_disabled")
            self.db.commit()
            self._send(chat_id, "この出品元の自動レビューは停止中です。"
                       "価格証拠としては登録していません。")
            return "source_disabled"
        if kind == "match_review":
            return self._consume_match_review(chat_id, row, payload,
                                              approve)
        if kind == "sweep_hit":
            return self._consume_sweep_hit(chat_id, row, payload,
                                           approve)
        if kind == "copy_bundle_review":
            return self._consume_copy_bundle(chat_id, row, payload,
                                             approve)
        if kind == "seed_promotion":
            return self._consume_seed_promotion(chat_id, row, payload,
                                                approve)
        self._send(chat_id, "この項目はこのボタンでは処理できません"
                   "（種別: " + str(kind) + "）。")
        return "queue_wrong_kind"

    def _match_review_resolvable(self, payload) -> bool:
        """Read-only mirror of _consume_match_review's resolution
        checks (W-check B5, maniac B F-1). The R19 anti-false-
        success verification ran BEFORE the approve/reject branch,
        so a dict-shaped but unresolvable payload could never
        settle at all — rejection mints nothing and must not need
        verification. This probe lets rejection settle such rows
        while approval keeps the full verified path."""
        listing_p = payload.get("listing")
        result = payload.get("result")
        if not isinstance(listing_p, dict) \
                or not isinstance(result, dict):
            return False
        edition_id = result.get("edition_id")
        score = result.get("score")
        # W-check B5 R24 (maniac B N-1/N-6): the probe — and the
        # consumer below — bound listing.source / listing.url to
        # sqlite with no type check, so a payload whose source or
        # url is a JSON array/object raised ProgrammingError at
        # bind time and the row could again settle by NEITHER
        # button (the F-1 shape, one value class over); and a
        # NaN score passed the isinstance check and minted a
        # human decision with a NULL score. Non-scalar identity
        # fields and non-finite scores are simply unresolvable.
        if not isinstance(edition_id, str) or not edition_id \
                or isinstance(score, bool) \
                or not isinstance(score, (int, float)) \
                or not math.isfinite(score) \
                or self.db.execute(
                    "SELECT 1 FROM editions WHERE id=?",
                    (edition_id,)).fetchone() is None:
            return False
        listing_id = None
        dec_id = result.get("decision_id")
        if dec_id is not None:
            if not isinstance(dec_id, int) \
                    or isinstance(dec_id, bool):
                return False
            d = self.db.execute(
                "SELECT listing_id, edition_id FROM match_decisions"
                " WHERE id=?", (dec_id,)).fetchone()
            if d is None or d[1] != edition_id:
                return False
            listing_id = d[0]
        if listing_id is None:
            _src = listing_p.get("source")
            _url = listing_p.get("url")
            if isinstance(_src, str) and isinstance(_url, str) \
                    and _url:
                d = self.db.execute(
                    "SELECT id FROM listings WHERE source=? AND"
                    " url=?", (_src, _url)).fetchone()
                if d is not None:
                    listing_id = d[0]
        return listing_id is not None

    def _consume_match_review(self, chat_id, row, payload,
                              approve: bool) -> str:
        """The human's match adjudication becomes a match_decision:
        approval mints a superseding price-高 decision (the 9-C12
        gate reads the LATEST decision, so the E1 price becomes
        usable evidence — spec C-5.4's 'price attaches only after
        review approval' finally has its consumer); rejection
        records a 低 decision so the price stays unusable.

        R19 (AI engineer / maniac B / collector / adversarial QA):
        the consumer trusted its payload blindly and reported
        success unconditionally — a payload whose decision_id was
        dangling, whose result named a different edition than the
        decision it pointed at, or whose listing could not be
        resolved at all minted NOTHING, yet the row settled as
        approved and the user was told the price had become
        evidence. Resolution is now verified end to end (decision
        row exists and agrees on the edition; the edition exists;
        the score is numeric), and an unresolvable payload leaves
        the row UNRESOLVED with an honest failure message.
        W-check B5 (maniac B F-1): that verification also blocked
        REJECTION of an unresolvable row — the row could never
        settle by either button and wedged /queue presentation
        forever. Rejection mints nothing, so an unresolvable
        payload now settles as rejected (with an honest note that
        nothing was recorded); approval keeps the verified path."""
        if not approve and not self._match_review_resolvable(payload):
            self._settle_queue_row(row["id"], "rejected")
            self.db.commit()
            self._send(chat_id, "出品レビューの内容を検証できま"
                       "せんでした。却下として処理しました"
                       "（価格証拠にはなりません）。")
            return "match_review_rejected"
        listing_p = payload.get("listing")
        result = payload.get("result")
        if not isinstance(listing_p, dict) \
                or not isinstance(result, dict):
            self._send(chat_id, "出品レビューの内容が読み取れません"
                       "でした。項目は未処理のまま残しています。")
            return "match_review_unresolvable"
        edition_id = result.get("edition_id")
        score = result.get("score")
        if not isinstance(edition_id, str) or not edition_id \
                or isinstance(score, bool) \
                or not isinstance(score, (int, float)) \
                or not math.isfinite(score) \
                or self.db.execute(
                    "SELECT 1 FROM editions WHERE id=?",
                    (edition_id,)).fetchone() is None:
            self._send(chat_id, "出品レビューの対象Editionが確認"
                       "できませんでした。項目は未処理のまま"
                       "残しています。")
            return "match_review_unresolvable"
        listing_id = None
        dec_id = result.get("decision_id")
        if dec_id is not None:
            if not isinstance(dec_id, int) \
                    or isinstance(dec_id, bool):
                self._send(chat_id, "出品レビューの決定参照が不正"
                           "でした。項目は未処理のまま残しています。")
                return "match_review_unresolvable"
            d = self.db.execute(
                "SELECT listing_id, edition_id FROM match_decisions"
                " WHERE id=?", (dec_id,)).fetchone()
            if d is None or d[1] != edition_id:
                self._send(chat_id, "出品レビューの決定参照が実在"
                           "しないか、対象Editionと一致しません。"
                           "項目は未処理のまま残しています。")
                return "match_review_unresolvable"
            listing_id = d[0]
        if listing_id is None:
            # R24 (maniac B N-1): same non-scalar bind guard as
            # the probe — a JSON array/object source or url is
            # unresolvable, never a ProgrammingError.
            _src = listing_p.get("source")
            _url = listing_p.get("url")
            if isinstance(_src, str) and isinstance(_url, str) \
                    and _url:
                d = self.db.execute(
                    "SELECT id FROM listings WHERE source=? AND"
                    " url=?", (_src, _url)).fetchone()
                if d is not None:
                    listing_id = d[0]
        if listing_id is None:
            self._send(chat_id, "対象の出品が見つからないため、"
                       "価格証拠化できませんでした。項目は未処理"
                       "のまま残しています。")
            return "match_review_unresolvable"
        now = clock.now_iso(self.db)
        if approve:
            breakdown = result.get("breakdown")
            if not isinstance(breakdown, dict):
                breakdown = {}
            breakdown = dict(breakdown)
            breakdown["review_approved"] = True
            variant_id = result.get("variant_id") \
                if result.get("variant_gate") else None
            # R24: variant_id and barcode_source_tier are bound
            # into the minted decision — a non-scalar payload
            # value would raise at bind time on the APPROVE path
            # (the same class as N-1). Non-scalars become None.
            if isinstance(variant_id, bool) or not isinstance(
                    variant_id, (int, str)):
                variant_id = None
            _tier = listing_p.get("barcode_source_tier")
            if isinstance(_tier, bool) or not isinstance(
                    _tier, (int, str)):
                _tier = None
            _exist = result.get("existence_confidence")
            if not isinstance(_exist, str) or not _exist:
                _exist = "高"
            self.db.execute(
                "INSERT INTO match_decisions(listing_id,"
                " edition_id, variant_id, score,"
                " score_breakdown_json, veto,"
                " existence_confidence, price_confidence,"
                " barcode_only, barcode_source_tier,"
                " matcher_version, policy_version, decided_at)"
                " VALUES(?,?,?,?,?,NULL,?,?,?,?,'human-review-1',"
                " ?,?)",
                (listing_id, edition_id, variant_id,
                 score,
                 json.dumps(breakdown, ensure_ascii=False),
                 _exist,
                 "高", 1 if result.get("barcode_only") else 0,
                 _tier,
                 current_policy_version(self.db), now))
            self.db.execute(
                "UPDATE listings SET edition_id=?,"
                " variant_id=COALESCE(?, variant_id) WHERE id=?",
                (edition_id, variant_id, listing_id))
        else:
            self.db.execute(
                "INSERT INTO match_decisions(listing_id,"
                " edition_id, variant_id, score,"
                " score_breakdown_json, veto,"
                " existence_confidence, price_confidence,"
                " barcode_only, barcode_source_tier,"
                " matcher_version, policy_version, decided_at)"
                " VALUES(?,?,NULL,0,?,NULL,'低','低',0,NULL,"
                " 'human-review-1',?,?)",
                (listing_id, edition_id,
                 json.dumps({"review_rejected": True}),
                 current_policy_version(self.db), now))
        self._settle_queue_row(row["id"],
                               "approved" if approve else "rejected")
        self.db.commit()
        if approve:
            self._send(chat_id, "出品レビューを承認しました。"
                       "この出品の価格が証拠として使えるように"
                       "なりました。")
            return "match_review_approved"
        self._send(chat_id, "出品レビューを却下しました。"
                   "この出品の価格は証拠として使われません。")
        return "match_review_rejected"

    def _consume_sweep_hit(self, chat_id, row, payload,
                           approve: bool) -> str:
        """A catalog-sweep hit becomes a normal discovery candidate
        (which then flows through candidate_review like any other
        lane) — before B4 the hit had no path into the catalogue
        at all."""
        if not approve:
            self._settle_queue_row(row["id"], "rejected")
            self.db.commit()
            self._send(chat_id, "スイープヒットを却下しました。")
            return "sweep_hit_rejected"
        catalog = payload.get("catalog")
        if not isinstance(catalog, str) or not catalog:
            self._send(chat_id, "ヒットの型番が読み取れませんでした。")
            return "sweep_hit_unreadable"
        from .normalize import fingerprint_for, normalize_catalog
        cat_norm = normalize_catalog(catalog)["norm"]
        fp = fingerprint_for(catalog, cat_norm)
        now = clock.now_iso(self.db)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO candidate_editions("
            " title, raw_title, fingerprint, catalog_norm,"
            " existence_confidence, price_confidence,"
            " found_at, first_seen, last_seen, found_via, status)"
            " VALUES(?, ?, ?, ?, '中', '低', ?, ?, ?, 'catalog_sweep',"
            " 'pending')",
            (catalog, catalog, fp, cat_norm, now, now, now))
        if cur.rowcount:
            cid = cur.lastrowid
            self.db.execute(
                "INSERT INTO review_queue(kind, payload_json,"
                " fingerprint, priority, created_at) VALUES"
                "('candidate_review', ?, ?, 40, ?)",
                (json.dumps({"candidate_id": cid,
                             "source": "catalog_sweep"}), fp, now))
            outbox_mod.enqueue_candidate_review(self.db, cid)
            self._settle_queue_row(row["id"], "approved")
            self.db.commit()
            self._send(chat_id, "スイープヒット " + str(catalog)
                       + " を候補として登録しました。")
            return "sweep_hit_approved"
        # R19 (maniac B / collector): the INSERT was ignored
        # because the fingerprint already exists — the old code
        # still announced success, so approving a hit whose
        # candidate had been REJECTED silently buried the user's
        # change of mind (and the sweep's once-ever dedupe meant
        # the hit could never resurface). A rejected twin is
        # reopened as a fresh review; anything else is reported
        # truthfully as already registered.
        existing = self.db.execute(
            "SELECT id, status FROM candidate_editions"
            " WHERE fingerprint=?", (fp,)).fetchone()
        self._settle_queue_row(row["id"], "approved")
        if existing is not None and existing[1] == "rejected":
            cid = int(existing[0])
            self.db.execute(
                "UPDATE candidate_editions SET status='pending',"
                " reject_reason=NULL WHERE id=?", (cid,))
            self.db.execute(
                "INSERT INTO review_queue(kind, payload_json,"
                " fingerprint, priority, created_at) VALUES"
                "('candidate_review', ?, ?, 40, ?)",
                (json.dumps({"candidate_id": cid,
                             "source": "catalog_sweep"}), fp, now))
            outbox_mod.enqueue_candidate_review(
                self.db, cid, idem_suffix="|sweep-reopen")
            self.db.commit()
            self._send(chat_id, "スイープヒット " + str(catalog)
                       + " は却下済みの候補と一致したため、候補"
                       "として再登録しました（再レビュー対象）。")
            return "sweep_hit_reopened"
        self.db.commit()
        self._send(chat_id, "スイープヒット " + str(catalog)
                   + " は既に候補として登録されています。")
        return "sweep_hit_already_exists"

    def _consume_copy_bundle(self, chat_id, row, payload,
                             approve: bool) -> str:
        """Approval EXECUTES the bundling (bundling.apply_approved_
        bundle) — before B4 the 'review' was a gate with nothing
        behind it: approving changed nothing and the pair was
        re-queued forever."""
        if payload.get("reason") != "cond_rank_unknown":
            self._send(chat_id, "この項目はこのボタンでは処理できません。")
            return "queue_wrong_kind"
        if not approve:
            self._settle_queue_row(row["id"], "rejected")
            self.db.commit()
            self._send(chat_id, "束ね提案を却下しました。")
            return "copy_bundle_rejected"
        from . import bundling
        grouped, joined = bundling.apply_approved_bundle(self.db,
                                                         payload)
        self._settle_queue_row(row["id"], "approved")
        self.db.commit()
        if grouped or joined:
            self._send(chat_id, "束ねを実行しました（新規グループ "
                       + str(grouped) + "件 / 参加 " + str(joined)
                       + "件）。")
        else:
            self._send(chat_id, "承認しましたが、価格のばらつき等の"
                       "条件を満たさず束ねられませんでした。")
        return "copy_bundle_approved"

    def _consume_seed_promotion(self, chat_id, row, payload,
                                approve: bool) -> str:
        """Approval is Gate 1 of seedops.promote_seed: the row is
        settled 'approved' FIRST (that record is what the gate
        verifies), then promote_seed runs with the found record
        stored in the payload by the producer."""
        seed_id = payload.get("seed_id")
        if not isinstance(seed_id, int) or isinstance(seed_id, bool):
            self._send(chat_id, "seed の指定が読み取れませんでした。")
            return "seed_promotion_unreadable"
        if not approve:
            self._settle_queue_row(row["id"], "rejected")
            self.db.commit()
            self._send(chat_id, "seed昇格を却下しました。")
            return "seed_promotion_rejected"
        # R19 (AI engineer / maniac B): the row used to settle
        # 'approved' BEFORE any existence check, so an approval for
        # a nonexistent (or no longer seed) id was recorded as a
        # valid Gate 1 stamp for a promotion that never happened.
        # Validate first; a stale item fails honestly and stays
        # unresolved.
        seed = self.db.execute(
            "SELECT status FROM candidate_editions WHERE id=?",
            (seed_id,)).fetchone()
        if seed is None or seed[0] != "seed":
            self._send(chat_id, "seed#" + str(seed_id)
                       + " が見つからないか、既にseedではありま"
                       "せん。項目は未処理のまま残しています。")
            return "seed_promotion_invalid"
        found = payload.get("found")
        if not isinstance(found, dict):
            found = {}
        self._settle_queue_row(row["id"], "approved")
        self.db.commit()
        from . import seedops
        res = seedops.promote_seed(
            self.db, seed_id, found=found,
            review_queue_id=row["id"])
        if res.get("promoted"):
            self._send(chat_id, "seed#" + str(seed_id)
                       + " を昇格しました（通常の候補レビューに"
                       "進みました）。")
            return "seed_promoted"
        self._send(chat_id, "承認は記録しましたが、昇格できません"
                   "でした（" + str(res.get("reason")) + "）。")
        return "seed_promote_failed"

    # -- commands -----------------------------------------------------------
    def _cmd_watch(self, chat_id, args, on: bool) -> str:
        if not args:
            self._send(chat_id,
                       "使い方: /watch <Edition ID> / /unwatch <Edition ID>"
                       " / /watch R4-R5（レアリティ範囲の一括指定）")
            return "watch_usage"
        m = _RARITY_RANGE_RE.match(args[0])
        if m:
            lo, hi = m.group(1), m.group(2)
            if int(lo) > int(hi):
                # Review fix (adversarial QA): a reversed range used
                # to report success for 0 editions.
                self._send(chat_id,
                           f"レアリティ範囲 R{lo}-R{hi} は逆転しています。"
                           "小さい方から指定してください。")
                return "watch_range_invalid"
            rarities = [f"R{i}" for i in range(int(lo), int(hi) + 1)]
            level = 2 if on else 0
            cur = self.db.execute(
                "UPDATE editions SET watch_level=? WHERE rarity IN (%s)"
                % ",".join("?" * len(rarities)), [level] + rarities)
            self.db.commit()
            self._send(chat_id, f"レアリティ R{lo}-R{hi} の {cur.rowcount}件"
                       + ("の監視を開始" if on else "の監視を解除")
                       + "しました。")
            return "watch_range"
        eid = args[0]
        row = self.db.execute("SELECT id FROM editions WHERE id=?",
                              (eid,)).fetchone()
        if row is None:
            # R6-D5: never echo the raw argument in full.
            self._send(chat_id, "Edition " + _short(eid) + " は存在しません。")
            return "watch_missing"
        level = 2 if on else 0
        self.db.execute("UPDATE editions SET watch_level=? WHERE id=?",
                        (level, eid))
        self.db.commit()
        self._send(chat_id, _short(eid) + " の監視を"
                   + ("開始" if on else "解除")
                   + "しました（レベル" + str(level) + "）。")
        return "watch_on" if on else "watch_off"

    def _cmd_list(self, chat_id) -> str:
        rows = self.db.execute(
            "SELECT id, title, watch_level FROM editions WHERE watch_level>0"
            " ORDER BY watch_level DESC, id LIMIT 50").fetchall()
        total = self.db.execute(
            "SELECT COUNT(*) FROM editions WHERE watch_level>0").fetchone()[0]
        lines = ["監視中: " + str(total) + "件（先頭50件まで表示）"]
        # 9-C16: per-level counts.
        count_rows = self.db.execute(
            "SELECT watch_level, COUNT(*) FROM editions WHERE watch_level>0"
            " GROUP BY watch_level").fetchall()
        level_counts = {r[0]: r[1] for r in count_rows}
        levels = sorted(set([1, 2, 3]) | set(level_counts))
        lines.append("レベル別: " + " / ".join(
            "Lv" + str(lv) + " " + str(level_counts.get(lv, 0)) + "件"
            for lv in levels))
        from .normalize import one_line
        for r in rows:
            # R19 (adversarial QA): edition titles can now be
            # external text (catalogued candidates) — /list was a
            # remaining verbatim sink where a newline in a title
            # injected fake watch entries.
            lines.append("- " + r["id"] + " Lv" + str(r["watch_level"])
                         + " " + one_line(r["title"]))
        self._send(chat_id, "\n".join(lines))
        return "list"


    def _cmd_price(self, chat_id, args) -> str:
        """Phase 2: price evidence per bucket + history trend."""
        from . import bundling
        if not args:
            self._send(chat_id, "使い方: /price <Edition ID>")
            return "price_usage"
        eid = args[0]
        row = self.db.execute(
            "SELECT id, title, catalog FROM editions WHERE id=?",
            (eid,)).fetchone()
        if row is None:
            self._send(chat_id, "Edition " + _short(eid) + " は存在しません。")
            return "price_missing"
        from .normalize import one_line
        lines = ["【価格】" + one_line(row["title"]) + "（" + eid + "）"]

        import math

        def fmt_amount(v):
            # W-check B5 (AI engineer 中-2): the bucket minimum
            # below is a SQL MIN over total_jpy, which returns a
            # legacy TEXT value verbatim when every row in the
            # bucket is TEXT — float('abc') raised and /price
            # answered with silence. A non-numeric amount formats
            # as None and the caller renders 'no yen price'.
            # W-check B5 R24 (AI engineer 中-B): the guard covered
            # only the float() call — a REAL Inf amount (9e999 in
            # an affinity column) then raised OverflowError at
            # int(f) OUTSIDE the try, and /price went silent
            # again, in a different value class. Non-finite is
            # not an amount, and the whole conversion is guarded.
            try:
                f = float(v)
                if not math.isfinite(f):
                    return None
                if f == int(f):
                    return f"{f:,.0f}"
            except (TypeError, ValueError, OverflowError):
                return None
            return f"{f:,.2f}".rstrip("0").rstrip(".")

        cutoff = clock.to_iso(clock.now(self.db) - timedelta(hours=6))
        snaps = self.db.execute(
            "SELECT taken_at, num_for_sale, lowest_price, currency FROM"
            " price_snapshots WHERE edition_id=? AND source='discogs'"
            " AND julianday(taken_at)>=julianday(?)"
            " ORDER BY taken_at DESC, rowid DESC LIMIT 12",
            (eid, cutoff)).fetchall()
        if snaps:
            latest = snaps[0]
            # W-check B3 (collector D-1/D-4, wholesaler 5, maniac A
            # D-6): the history statistics used to pool every
            # snapshot's lowest_price regardless of currency, so a
            # JPY/USD mix produced a meaningless "median" (7555.0
            # from (110+15000)/2) and a confident trend arrow, and a
            # NULL currency printed as the literal text "None".
            # Statistics now run over one currency series only;
            # other currencies are reported as excluded counts,
            # never silently averaged in. Non-numeric legacy
            # values are ignored defensively.
            # R14 (collector E1): the series currency is the latest
            # KNOWN currency, not the latest row's — a sold-out
            # snapshot records currency NULL, and keying the series
            # to it erased the whole price history at exactly the
            # moment it mattered, mislabelled as "other currency".
            # Currencies compare normalised ('jpy' is JPY).
            from .normalize import normalize_currency

            def as_float(v):
                # R24 (AI engineer 中-B): symmetric with
                # scoring.yen_value — a non-finite snapshot value
                # (REAL Inf) is not a price observation; it used
                # to flow into the series and detonate fmt_amount.
                try:
                    _f = float(v)
                except (TypeError, ValueError, OverflowError):
                    return None
                return _f if math.isfinite(_f) else None

            cur = None
            for s in snaps:
                c = normalize_currency(s["currency"])
                if c:
                    cur = c
                    break
            cur_label = cur if cur else "通貨不明"
            series = [as_float(s["lowest_price"]) for s in snaps
                      if normalize_currency(s["currency"]) == cur
                      and as_float(s["lowest_price"]) is not None]
            others = sum(1 for s in snaps
                         if normalize_currency(s["currency"])
                         not in (None, cur)
                         and s["lowest_price"] is not None)
            latest_price = as_float(latest["lowest_price"])
            latest_txt = ("最安なし（価格未観測）"
                          if latest_price is None
                          else f"最安 {fmt_amount(latest_price)} "
                               f"{cur_label}")
            # W-check B5 (AI engineer 軽微-2): num_for_sale is
            # INTEGER-affinity but a legacy TEXT value ('many')
            # used to print verbatim — '出品 many件'. The B3
            # defensive reading covered lowest_price, not the
            # count. Non-numeric counts render as 不明.
            try:
                _nfs_txt = str(int(latest["num_for_sale"]))
            except (TypeError, ValueError, OverflowError):
                _nfs_txt = "不明"
            lines.append(
                f"Discogs: 出品 {_nfs_txt}件 / "
                f"{latest_txt}（{latest['taken_at'][:10]}）")
            if len(series) >= 2:
                ordered = sorted(series)
                mid = len(ordered) // 2
                median = ordered[mid] if len(ordered) % 2 \
                    else (ordered[mid - 1] + ordered[mid]) / 2
                lines.append(
                    f"最安値の履歴（直近{len(series)}回 / {cur_label}）: "
                    f"最安 {fmt_amount(min(series))} / "
                    f"中央値 {fmt_amount(median)} / "
                    f"最高 {fmt_amount(max(series))}")
                trend = series[0] - series[-1]
                lines.append("推移: " + ("上昇" if trend > 0 else
                                        "下落" if trend < 0 else "横ばい")
                             + f"（{fmt_amount(abs(trend))}）")
            if others:
                lines.append(
                    f"※ 他通貨の記録 {others}件は集計に含めていません")
        # Buckets are edition x variant x cond_rank (spec C-7.2).
        # W-check B3 (collector D-2/D-3/D-4): the minimum used to be
        # MIN(COALESCE(total_jpy, price)) over every live row — a
        # foreign bare price was displayed as yen ("最安 50.0円" for
        # a $50 listing), sold_candidate rows posed as the current
        # cheapest although pricedrop excludes them, and an
        # all-NULL bucket printed the literal "None円". The minimum
        # is now over yen values only (total_jpy, or price when the
        # row's currency is JPY), sold_candidate is excluded like
        # everywhere else, and rows without a yen value are counted
        # and named instead of silently priced.
        buckets = self.db.execute(
            "SELECT variant_id, cond_rank,"
            " SUM(CASE WHEN price IS NOT NULL THEN 1 ELSE 0 END),"
            " MIN(CASE WHEN total_jpy IS NOT NULL THEN total_jpy"
            " WHEN UPPER(TRIM(currency))='JPY' THEN price END),"
            " SUM(CASE WHEN price IS NOT NULL AND total_jpy IS NULL"
            " AND COALESCE(UPPER(TRIM(currency)), '') IS NOT 'JPY'"
            " THEN 1 ELSE 0 END)"
            " FROM listings WHERE edition_id=?"
            " AND status NOT IN ('ended', 'sold_candidate')"
            " GROUP BY variant_id, cond_rank"
            " ORDER BY cond_rank DESC",
            (eid,)).fetchall()
        for b in buckets:
            label = f"状態ランク{b[1]}" if b[1] is not None else "状態不明"
            label += f"（バリアント{b[0]}）" if b[0] is not None \
                else "（バリアント不明）"
            _amt = fmt_amount(b[3]) if b[3] is not None else None
            if _amt is None:
                cheapest = "最安 -（円価格なし）"
            else:
                cheapest = f"最安 {_amt}円"
            extra = f" / 円換算のない出品 {b[4]}件" if b[4] else ""
            lines.append(f"{label}: {b[2]}件 / {cheapest}{extra}")
        copies = bundling.count_physical_copies(self.db, eid)
        lines.append(f"物理個体数（束ね済み）: {copies}個")
        if not snaps and not buckets:
            lines.append("価格証拠はまだありません。")
        self._send(chat_id, "\n".join(lines))
        return "price"

    def _cmd_queue(self, chat_id) -> str:
        """Spec §6.3: counts by kind, then the single highest-
        priority pending item, one at a time, with buttons.

        W-check B4 (maniac B / adversarial QA): the presentation
        used to consider ONLY candidate_review rows, and only the
        single top one — a stale or poisoned head row (candidate
        gone, or candidate_id=10**30 raising OverflowError at bind
        time) wedged /queue permanently, and every other kind was
        invisible here despite being counted in the header. The
        scan now walks unresolved rows in priority order, settles
        stale candidate rows as 'stale' on sight, and presents the
        first PRESENTABLE item of ANY kind with kind-appropriate
        buttons (qok:/qno: for the generic kinds)."""
        # Suppress existing eBay review payloads before reporting counts or
        # presenting an item. Their source-specific expiry lifecycle is
        # not available in this version.
        for stale in self.db.execute(
                "SELECT id,payload_json FROM review_queue WHERE"
                " kind='match_review' AND resolved_at IS NULL").fetchall():
            try:
                stale_payload = json.loads(stale["payload_json"] or "")
            except (ValueError, TypeError):
                stale_payload = {}
            if _is_disabled_api_review(stale_payload):
                self._settle_queue_row(stale["id"], "source_disabled")
        self.db.commit()
        rows = self.db.execute(
            "SELECT kind, COUNT(*) FROM review_queue WHERE"
            " resolved_at IS NULL GROUP BY kind ORDER BY kind").fetchall()
        total = sum(r[1] for r in rows)
        lines = [f"レビューキュー: 未処理 {total}件"]
        for r in rows:
            lines.append(f"- {r[0]}: {r[1]}件")
        cands = self.db.execute(
            "SELECT COUNT(*) FROM candidate_editions WHERE"
            " status='pending'").fetchone()[0]
        lines.append(f"承認待ち候補: {cands}件")
        self._send(chat_id, "\n".join(lines))
        # R19 (maniac B): the scan window was a single LIMIT 50 —
        # 51+ stale rows (or a wall of unknown-kind rows, which
        # present as False without settling) meant one /queue call
        # presented NOTHING even though a presentable item sat
        # right behind them. The scan now walks up to 200 rows in
        # this one call; stale rows settle as it passes.
        pending = self.db.execute(
            "SELECT * FROM review_queue WHERE resolved_at IS NULL"
            " ORDER BY priority DESC, id ASC LIMIT 200").fetchall()
        for q in pending:
            if self._present_queue_item(chat_id, q):
                break
        return "queue"

    def _present_queue_item(self, chat_id, q) -> bool:
        """Present one queue row with its buttons. Returns True
        when something was presented; False to keep scanning
        (stale rows are settled on sight)."""
        from .normalize import one_line
        try:
            payload = json.loads(q["payload_json"] or "")
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        kind = q["kind"]
        qid = q["id"]
        markup = {"inline_keyboard": [[
            {"text": "承認", "callback_data": f"qok:{qid}"},
            {"text": "却下", "callback_data": f"qno:{qid}"},
        ]]}
        if kind == "candidate_review":
            cid = payload.get("candidate_id")
            if not isinstance(cid, int) or isinstance(cid, bool) \
                    or abs(cid) >= 2 ** 63:
                self._settle_queue_row(qid, "stale")
                self.db.commit()
                return False
            cand = self.db.execute(
                "SELECT * FROM candidate_editions WHERE id=? AND"
                " status='pending'", (cid,)).fetchone()
            if cand is None:
                self._settle_queue_row(qid, "stale")
                self.db.commit()
                return False
            markup = {"inline_keyboard": [[
                {"text": "承認",
                 "callback_data": f"approve:{cid}"},
                {"text": "却下",
                 "callback_data": f"reject:{cid}"},
            ]]}
            # W-check B5 (collector 軽微-4): when a REPRESS signal
            # already bridges this candidate to an existing
            # edition, say so BEFORE the tap — approval will link
            # there instead of minting an AR-C number, and that is
            # decision material, not a surprise.
            # W-check B5 R24 (collector 中-A): the hint took the
            # OLDEST signal unconditionally and asserted the
            # link, but _catalogue_candidate links only when the
            # DISTINCT signalled editions are exactly one AND it
            # exists — with two signalled editions, or a signal
            # pointing at a missing edition, approval actually
            # mints an AR-C row while the hint promised a link.
            # The presentation now mirrors the resolution rule.
            _sigs = [r[0] for r in self.db.execute(
                "SELECT DISTINCT edition_id FROM repress_signals"
                " WHERE candidate_id=? AND edition_id IS NOT NULL",
                (cid,)).fetchall()]
            bridge_line = ""
            if len(_sigs) == 1:
                _exists = self.db.execute(
                    "SELECT 1 FROM editions WHERE id=?",
                    (_sigs[0],)).fetchone()
                if _exists is not None:
                    bridge_line = (
                        f"\n既存目録との関連: {_sigs[0]}"
                        "（承認するとこの盤に紐付きます）")
                else:
                    bridge_line = (
                        f"\nシグナル上の関連: {_sigs[0]}"
                        "（目録に実在しないため、承認時は新規"
                        "登録されます）")
            elif len(_sigs) > 1:
                bridge_line = (
                    "\n既存目録との関連: 複数の盤へのシグナルが"
                    "あります（承認時の紐付け先は内容で決まります）")
            self._send(
                chat_id,
                f"次の候補（優先度順・1件ずつ）:\n"
                f"#{cid} {one_line(cand['title'])}\n"
                f"確信度: 存在 {cand['existence_confidence']}"
                f" / 価格 {cand['price_confidence']}\n"
                f"経由: {cand['found_via'] or '-'}"
                f"{bridge_line}\n"
                f"{one_line(cand['source_url'] or '')}",
                reply_markup=markup)
            return True
        if kind == "match_review":
            listing_p = payload.get("listing")
            result = payload.get("result")
            if not isinstance(listing_p, dict) \
                    or not isinstance(result, dict):
                # R19 (maniac B / adversarial QA): a nested-poison
                # row (listing/result not objects) raised
                # AttributeError out of the scan and wedged /queue
                # permanently — the head row never settled and no
                # later row was ever presented. A payload that can
                # never be presented or consumed settles as stale,
                # like the candidate lane's poison rows.
                self._settle_queue_row(qid, "stale")
                self.db.commit()
                return False
            # W-check B5 R24 (maniac B N-5): the tail promised
            # approval would work even for rows the consumer
            # cannot resolve (the F-4 text/reality divergence,
            # left on this kind). Probe first; an unresolvable
            # row says so and names its one working exit.
            if self._match_review_resolvable(payload):
                _tail = ("\n承認すると、この出品の価格が証拠として"
                         "使えるようになります。")
            else:
                _tail = ("\n内容を検証できないため承認はできません。"
                         "却下で片付けることができます。")
            self._send(
                chat_id,
                "次の出品レビュー（優先度順・1件ずつ）:\n"
                + one_line(listing_p.get("raw_title")
                           or listing_p.get("title") or "") + "\n"
                + "対象Edition: " + str(result.get("edition_id"))
                + " / スコア " + str(result.get("score"))
                + "\n確信度: 存在 "
                + str(result.get("existence_confidence"))
                + " / 価格 " + str(result.get("price_confidence"))
                + "\n" + one_line(str(listing_p.get("url") or ""))
                + _tail,
                reply_markup=markup)
            return True
        if kind == "sweep_hit":
            # W-check B5 (maniac B F-5): a hit without a catalog
            # number presents as a blank 型番 and can never become
            # a candidate (the consumer refuses it) — settle it
            # stale instead of presenting an empty question.
            _cat = payload.get("catalog")
            if not isinstance(_cat, str) or not _cat.strip():
                self._settle_queue_row(qid, "stale")
                self.db.commit()
                return False
            self._send(
                chat_id,
                "次の型番スイープヒット（優先度順・1件ずつ）:\n"
                "型番: " + one_line(_cat)
                + "\n承認すると候補として登録され、通常の候補"
                "レビューに進みます。",
                reply_markup=markup)
            return True
        if kind == "copy_bundle_review":
            if payload.get("reason") == "group_drift":
                # Dissolution notices are inserted pre-resolved;
                # an unresolved one is legacy — settle it as the
                # notice it is and keep scanning.
                self._settle_queue_row(qid, "dissolved_notice")
                self.db.commit()
                return False
            _ids = payload.get("listing_ids")
            self._send(
                chat_id,
                "次の同一個体束ねの確認（優先度順・1件ずつ）:\n"
                "Edition: " + str(payload.get("edition_id"))
                + " / 対象出品 "
                + str(len(_ids) if isinstance(_ids, list) else 0)
                + "件"
                + "\n状態が不明なため自動で束ねていません。"
                "承認すると束ねを実行します。",
                reply_markup=markup)
            return True
        if kind == "seed_promotion":
            seed_id = payload.get("seed_id")
            seed = None
            if isinstance(seed_id, int) \
                    and not isinstance(seed_id, bool) \
                    and abs(seed_id) < 2 ** 63:
                seed = self.db.execute(
                    "SELECT * FROM candidate_editions WHERE id=?",
                    (seed_id,)).fetchone()
            if seed is None:
                self._settle_queue_row(qid, "stale")
                self.db.commit()
                return False
            # W-check B5 (maniac B F-4): existence was the only
            # check, so an already-promoted seed's row kept
            # presenting 「昇格できます」 while the consumer
            # (correctly) refused it — text and reality diverged
            # and the row could only be cleared by rejecting a
            # promotion that had already happened. A row whose
            # seed is no longer a seed settles as stale.
            if seed["status"] != "seed":
                self._settle_queue_row(qid, "stale")
                self.db.commit()
                return False
            found = payload.get("found")
            if not isinstance(found, dict):
                found = {}
            self._send(
                chat_id,
                "次のseed昇格の確認（優先度順・1件ずつ）:\n"
                "seed#" + str(seed_id) + " "
                + one_line(seed["title"]) + "\n"
                + "目撃: " + one_line(found.get("title") or "")
                + "（サークル: "
                + one_line(found.get("circle") or "") + "）"
                + "\n承認すると通常の候補レビューに進みます。",
                reply_markup=markup)
            return True
        # W-check B5 (maniac B F-3): an UNKNOWN kind (a legacy or
        # migration row — no current producer emits one) returned
        # False without settling, so 200+ such rows permanently
        # wedged the scan window: nothing behind them was ever
        # presented. This build can neither present nor consume
        # them; settle on sight like every other dead row.
        self._settle_queue_row(qid, "stale")
        self.db.commit()
        return False

    def _cmd_deadlines(self, chat_id) -> str:
        """Spec §6.3: confirmed deadlines with kind + confidence;
        unconfirmed rows listed individually, with timezone-fix
        buttons where the raw text is parseable (review fix,
        adversarial QA — candidate rows used to render as 'None'
        and overflow rows vanished silently)."""
        from . import deadlines_ops
        # W-check B4 (collector): the filter compared against the
        # DATE PREFIX of now, so a deadline earlier TODAY (already
        # past) still listed as upcoming. Full-instant comparison,
        # and superseded rows (replaced by a postponement) never
        # list.
        rows = self.db.execute(
            "SELECT id, edition_id, candidate_id, deadline_raw,"
            " deadline_jst, deadline_kind FROM release_schedule WHERE"
            " deadline_confidence='confirmed'"
            " AND deadline_jst IS NOT NULL AND deadline_jst>?"
            " AND superseded_by IS NULL"
            " ORDER BY deadline_jst LIMIT 10",
            (clock.now_iso(self.db),)).fetchall()
        total_confirmed = self.db.execute(
            "SELECT COUNT(*) FROM release_schedule WHERE"
            " deadline_confidence='confirmed'"
            " AND deadline_jst IS NOT NULL AND deadline_jst>?"
            " AND superseded_by IS NULL",
            (clock.now_iso(self.db),)).fetchone()[0]
        lines = ["【確定締切（直近）】"]
        if rows:
            for r in rows:
                # W-check B5 R24 (collector 軽微-エ): this list
                # still named bare IDs while every notification
                # surface got titles in B5 — reuse _target_label.
                who = deadlines_ops._target_label(self.db, r)
                lines.append(f"- {who}: {r[3]}（{r[4]}／{r[5]}）")
            if total_confirmed > len(rows):
                lines.append(f"（他 {total_confirmed - len(rows)} 件）")
        else:
            lines.append("直近の確定締切はありません。")
        summary = deadlines_ops.unconfirmed_summary(self.db)
        if summary:
            lines.append("未確定（要確認）: "
                         + " / ".join(f"{k} {v}件"
                                      for k, v in summary.items()))
        self._send(chat_id, "\n".join(lines))
        # R19 (maniac A): the individual listing uses the SAME
        # unconfirmed definition as the summary above it — the old
        # predicate never listed no_instant rows (confirmed with a
        # NULL instant), so they were counted but could never be
        # seen or fixed here.
        unconf = self.db.execute(
            "SELECT id, edition_id, candidate_id, deadline_raw,"
            " deadline_confidence, deadline_kind FROM"
            " release_schedule WHERE"
            " (COALESCE(deadline_confidence,'')!='confirmed'"
            "  OR COALESCE(deadline_kind,'unknown')='unknown'"
            "  OR deadline_jst IS NULL)"
            " AND superseded_by IS NULL"
            " ORDER BY id LIMIT 3").fetchall()
        for r in unconf:
            who = r[1] or f"候補#{r[2]}"
            text = (f"未確定締切 #{r[0]}: {who}\n原文: {r[3]}\n"
                    f"状態: TZ {r[4]} / 種別 {r[5]}")
            markup = None
            if deadlines_ops.raw_parseable(r[3]):
                markup = {"inline_keyboard": [[
                    {"text": f"{tz} として確定",
                     "callback_data": f"tzfix:{r[0]}:{i}"}
                ] for i, tz in enumerate(TZ_CHOICES)]}
                text += "\n原文のタイムゾーンを選ぶと確定できます。"
            else:
                text += "\n（原文が日時として解析できないため手動確認が必要です）"
            self._send(chat_id, text, reply_markup=markup)
        # W-check B5 (collector 軽微-1): only the first 3
        # unconfirmed rows are shown individually; the rest used
        # to vanish without a word (the summary line carries the
        # total, but nothing said more rows exist). Name the
        # remainder explicitly.
        total_unconf = deadlines_ops.unconfirmed_count(self.db)
        if total_unconf > len(unconf):
            # W-check B5 R24 (collector 軽微-ウ): the old note
            # promised 'process the above and the next appear' —
            # untrue when the top rows are unparseable (they have
            # no buttons; nothing on this screen moves them). The
            # exits that actually advance the list are the tzfix
            # buttons AND cancellation; name both.
            self._send(chat_id,
                       f"（ほか {total_unconf - len(unconf)} 件の"
                       "未確定締切があります。上の項目を確定ボタン"
                       "または /deadline_cancel #番号 の取消で処理"
                       "すると次が表示されます）")
        return "deadlines"

    def _cmd_deadline(self, chat_id, args) -> str:
        """Manual deadline registration (W-check B4, collector):
        the deadline machinery had NO producer and NO manual path
        at all — /deadlines could only ever answer 'no deadlines',
        indistinguishable from the feature being broken, with no
        workaround. Usage:
          /deadline <Edition ID | #候補ID> <日時テキスト> [TZ] [種別]
        The datetime text must itself be a parseable datetime
        (e.g. 2027-02-07 23:59 or 2027-02-07T23:59:00+09:00); a
        trailing TZ (Asia/Tokyo / UTC / ...) and kind (hard /
        guarantee / campaign) are optional. Without a TZ the row
        stays unconfirmed and /deadlines offers the tzfix buttons,
        as with any unconfirmed row."""
        from . import deadlines_ops
        usage = ("使い方: /deadline <Edition ID | #候補ID> "
                 "<日時テキスト> [TZ] [hard|guarantee|campaign]\n"
                 "例: /deadline AR-000123 2027-02-07 23:59 Asia/Tokyo hard")
        if len(args) < 2:
            self._send(chat_id, usage)
            return "deadline_usage"
        target = args[0]
        # R19 (collector): the bot's own messages write candidates
        # as 候補#n — that form is accepted as a target too, not
        # only the bare #n the usage line shows.
        if target.startswith("候補#"):
            target = target[len("候補"):]
        rest = list(args[1:])
        kind = "unknown"
        if rest and rest[-1] in ("hard", "guarantee", "campaign"):
            kind = {"hard": "hard_cutoff",
                    "guarantee": "guarantee_cutoff",
                    "campaign": "campaign_end"}[rest.pop()]
        tz = None
        if rest and (rest[-1] in TZ_CHOICES or "/" in rest[-1]):
            tz = rest.pop()
            # R19 (collector / adversarial QA): any token with a
            # '/' used to be accepted as a timezone and silently
            # filed tz_unknown — the user was never told their TZ
            # was invalid. Validate it like any other zone name.
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(tz)
            except Exception:
                self._send(chat_id, "タイムゾーン「" + tz
                           + "」が無効です。Asia/Tokyo のような"
                           "正式名称で指定してください。\n" + usage)
                return "deadline_bad_tz"
        text = " ".join(rest).strip()
        if not text or not deadlines_ops.raw_parseable(text):
            self._send(chat_id, "日時として解析できるテキストで"
                       "指定してください。\n" + usage)
            return "deadline_unparseable"
        # W-check B5 (adversarial QA 軽微-2): a year-0001 text
        # PARSES fine, then died inside the timezone conversion
        # (arithmetic below year 1) and the command answered with
        # silence. set_schedule now survives it, but a deadline in
        # year 1 is a typo, not a plan — refuse it explicitly,
        # like every other bad input.
        from datetime import datetime as _dt
        try:
            _parsed_year = _dt.fromisoformat(text).year
        except ValueError:
            _parsed_year = None
        if _parsed_year is not None and not (1970 <= _parsed_year
                                             <= 2100):
            self._send(chat_id, "日付の年が範囲外です"
                       "（1970〜2100）。\n" + usage)
            return "deadline_out_of_range"
        edition_id = None
        candidate_id = None
        if target.startswith("#"):
            cid = _parse_candidate_id(target[1:])
            if cid is None or self.db.execute(
                    "SELECT 1 FROM candidate_editions WHERE id=?",
                    (cid,)).fetchone() is None:
                self._send(chat_id, "候補 " + target
                           + " が見つかりません。")
                return "deadline_target_missing"
            candidate_id = cid
        else:
            if self.db.execute(
                    "SELECT 1 FROM editions WHERE id=?",
                    (target,)).fetchone() is None:
                self._send(chat_id, "Edition " + target
                           + " が見つかりません。")
                return "deadline_target_missing"
            edition_id = target
        # An offset-carrying text determines its instant by itself;
        # set_schedule needs a tz argument to run its conversion,
        # and records the offset (not this placeholder) as the tz.
        carrier_tz = tz
        has_offset = False
        if carrier_tz is None:
            from datetime import datetime
            try:
                has_offset = datetime.fromisoformat(text).tzinfo \
                    is not None
            except ValueError:
                pass
            if has_offset:
                carrier_tz = "UTC"
        sid = deadlines_ops.set_schedule(
            self.db, edition_id=edition_id, candidate_id=candidate_id,
            channel="manual", deadline_raw=text, deadline_tz=carrier_tz,
            deadline_jst=text,
            deadline_confidence="confirmed" if (tz or has_offset)
            else "tz_unknown",
            deadline_kind=kind)
        row = self.db.execute(
            "SELECT deadline_jst, deadline_confidence FROM"
            " release_schedule WHERE id=?", (sid,)).fetchone()
        msg = "締切 #" + str(sid) + " を登録しました（対象: " \
            + target + "）。"
        if row[0]:
            msg += "\nJST: " + str(row[0]) + "（状態: " \
                + str(row[1]) + "）"
        else:
            msg += "\nタイムゾーン未確定のため、/deadlines で" \
                "TZ確定ボタンから確定してください。"
        self._send(chat_id, msg)
        return "deadline_registered"

    def _cmd_deadline_cancel(self, chat_id, args) -> str:
        """Cancel one live deadline by its schedule number
        (W-check B5, wholesaler 中-4): postponement had the
        supersede path, but a CANCELLED event had no remedy at
        all — its deadline kept alerting until it arrived.
        Usage: /deadline_cancel <#番号> (the number /deadline
        answered with and the sweep messages print)."""
        from . import deadlines_ops
        usage = ("使い方: /deadline_cancel <#番号>\n"
                 "例: /deadline_cancel #12")
        if len(args) != 1:
            self._send(chat_id, usage)
            return "deadline_cancel_usage"
        token = args[0].lstrip("#")
        sid = _parse_candidate_id(token)
        if sid is None:
            self._send(chat_id, usage)
            return "deadline_cancel_usage"
        row = self.db.execute(
            "SELECT deadline_raw FROM release_schedule WHERE id=?"
            " AND superseded_by IS NULL", (sid,)).fetchone()
        if row is None:
            self._send(chat_id, "締切 #" + str(sid)
                       + " が見つからないか、既に無効です。")
            return "deadline_cancel_missing"
        if not deadlines_ops.cancel_schedule(self.db, sid):
            self._send(chat_id, "締切 #" + str(sid)
                       + " を取り消せませんでした。")
            return "deadline_cancel_missing"
        from .normalize import one_line
        self._send(chat_id, "締切 #" + str(sid) + "（"
                   + one_line(row[0] or "") + "）を取り消しました。"
                   # R24 (wholesaler 中-A): with the cancellation
                   # tombstone, automatic re-harvest no longer
                   # revives the deadline; an explicit manual
                   # re-registration remains the revival path —
                   # the message says exactly that much.
                   "自動収穫で再登録されることはありません"
                   "（手動で /deadline 登録すると再開します）。")
        return "deadline_cancelled"

    def _cmd_coverage(self, chat_id) -> str:
        from . import seedops, sweep as sweep_mod, variant_sweep
        total = self.db.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
        # Price evidence means an actual price was observed: a
        # snapshot row with num_for_sale=0 and lowest_price NULL is
        # evidence of ABSENCE, not of price (collector review fix —
        # the old count inflated the coverage rate).
        # W-check B4 (collector): the numerator counted ONLY
        # Discogs price_snapshots, so an edition whose price
        # evidence is a store listing (the Japan-first main lane)
        # counted as having NO price evidence — contradicting
        # /price, which shows that listing's price. Both evidence
        # families count now, with the same yen discipline as
        # everywhere else (a foreign amount without a yen total is
        # not yen evidence).
        # R19 (collector): zero and negative amounts are not
        # prices — a 0-yen listing or a -100 total used to count
        # as price evidence. Positive values only, on both axes.
        # W-check B5 (seller 軽微-3 / 中-2): two more ghost
        # shapes counted as evidence — a snapshot that observed
        # ZERO items for sale but carried a stale lowest_price,
        # and an 'active' listing whose source stopped being
        # observed months ago (no absence round ever retires it).
        # The snapshot must have seen items on sale; the listing
        # must have been seen within 30 days (the same freshness
        # pricedrop.bucket_best now requires of a baseline).
        _fresh = clock.to_iso(clock.now(self.db)
                              - timedelta(days=30))
        with_price = self.db.execute(
            "SELECT COUNT(*) FROM editions e WHERE"
            " EXISTS (SELECT 1 FROM price_snapshots s"
            "  WHERE s.edition_id=e.id"
            "  AND s.lowest_price IS NOT NULL"
            "  AND typeof(s.lowest_price) IN ('integer', 'real')"
            "  AND s.lowest_price > 0"
            "  AND NOT (typeof(s.num_for_sale)"
            "   IN ('integer', 'real') AND s.num_for_sale <= 0))"
            " OR EXISTS (SELECT 1 FROM listings l"
            "  WHERE l.edition_id=e.id AND l.status='active'"
            "  AND l.last_seen IS NOT NULL AND l.last_seen>=?"
            # W-check B5 R24 (seller F-3/F-4): the listings side
            # lacked the snapshot side's discipline twice over —
            # a TEXT amount ('abc' in a REAL-affinity column)
            # satisfies `> 0` by SQLite type ordering (TEXT sorts
            # above INTEGER) and counted as price evidence, and
            # a garbage last_seen string compared 'fresh' by
            # lexicographic accident. Type-guard the amounts and
            # require an ISO-shaped last_seen.
            "  AND l.last_seen GLOB"
            "   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
            "  AND ((typeof(l.total_jpy) IN ('integer', 'real')"
            "        AND l.total_jpy > 0)"
            "   OR (UPPER(TRIM(COALESCE(l.currency,'')))='JPY'"
            "       AND typeof(l.price) IN ('integer', 'real')"
            "       AND l.price > 0)))",
            (_fresh,)
        ).fetchone()[0]
        lines = ["【カバレッジ】",
                 f"Edition: {total}件",
                 f"価格証拠あり: {with_price}件"
                 f"（{100.0 * with_price / max(total, 1):.1f}%）",
                 f"未確定バリアント: "
                 f"{variant_sweep.unconfirmed_count(self.db)}件",
                 f"未探査の型番帯: "
                 f"{sweep_mod.unsearched_band_count(self.db)}件",
                 f"同人seed（別枠）: {seedops.seed_count(self.db)}件"]
        paper_total = self.db.execute(
            "SELECT COUNT(*) FROM paper_coverage").fetchone()[0]
        paper_done = self.db.execute(
            "SELECT COUNT(*) FROM paper_coverage WHERE"
            " checked_at IS NOT NULL").fetchone()[0]
        lines.append(f"紙媒体: 確認済 {paper_done}/{paper_total}件"
                     f"（未確認 {paper_total - paper_done}件）")
        src = self.db.execute(
            "SELECT status, COUNT(*) FROM sources GROUP BY status"
        ).fetchall()
        if src:
            lines.append("ソース: " + " / ".join(
                f"{r[0]} {r[1]}" for r in src))
        self._send(chat_id, "\n".join(lines))
        return "coverage"

    def _cmd_quiet(self, chat_id, args) -> str:
        if args and args[0] in ("on", "off"):
            self._set_quiet(args[0] == "on", "telegram_command")
            self._send(chat_id, "静穏例外を " + args[0].upper() + " にしました。")
            return "quiet_set"
        if args:
            m = _QUIET_RANGE_RE.match(args[0])
            if m:
                start = m.group(1) + ":" + m.group(2)
                end = m.group(3) + ":" + m.group(4)
                if start != end:
                    # 9-C23: quiet hours live in the policy stored in
                    # settings (policy_json quiet.start / quiet.end) and
                    # are read back by outbox via policy.get_policy.
                    policy_mod.apply_change(
                        self.db, {"quiet": {"start": start, "end": end}},
                        "telegram_command", "quiet hours change")
                    self._send(chat_id, "静穏時間帯を " + start + "-" + end
                               + " にしました。")
                    return "quiet_hours_set"
        raw = get_setting(self.db, "deadline_override_quiet")
        cur = {"1": "ON", "0": "OFF"}.get(raw, "未選択")
        start, end = self._quiet_hours()
        self._send(chat_id, "静穏例外の現在値: " + cur
                   + "／静穏時間帯: " + start + "-" + end
                   + "（/quiet on|off、/quiet HH:MM-HH:MM で変更）")
        return "quiet_show"

    def _cmd_digest(self, chat_id, args) -> str:
        # 9-C23: /digest on/off toggles the daily-digest enable flag,
        # stored as settings.digest_enabled ("1"/"0"). Unset means ON,
        # matching the behaviour so far (digests are sent every day).
        if args and args[0] in ("on", "off"):
            enabled = args[0] == "on"
            set_setting(self.db, "digest_enabled", "1" if enabled else "0")
            self._send(chat_id, "日次ダイジェストを "
                       + ("ON" if enabled else "OFF") + " にしました。")
            return "digest_set"
        raw = get_setting(self.db, "digest_enabled")
        cur = "OFF" if raw == "0" else "ON"
        self._send(chat_id, "日次ダイジェストの現在値: " + cur
                   + "（/digest on|off で変更）")
        return "digest_show"

    def _cmd_status(self, chat_id) -> str:
        db = self.db
        lines = []
        anomaly = db.execute(
            "SELECT detail_json, created_at FROM system_events"
            " WHERE kind='watchdog_anomaly' ORDER BY id DESC LIMIT 1").fetchone()
        recovered_after = 0
        if anomaly:
            recovered_after = db.execute(
                "SELECT COUNT(*) FROM system_events"
                " WHERE kind='watchdog_recovered'").fetchone()[0]
        if anomaly and not recovered_after:
            lines.append("異常: " + str(anomaly["created_at"])
                         + " に死活異常を検出（" + str(anomaly["detail_json"]) + "）")
        lines.append("【ステータス】")
        lines.append("バージョン: " + version_mod.app_version())
        lines.append("初期設定（静穏例外選択）: "
                     + ("完了" if gate_open(db) else "未完了"))
        watched = db.execute(
            "SELECT COUNT(*) FROM editions WHERE watch_level>0").fetchone()[0]
        lines.append("監視中 Edition: " + str(watched) + "件")
        pending = db.execute(
            "SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL"
        ).fetchone()[0]
        lines.append("レビュー未処理: " + str(pending) + "件")
        lines.append("dead_letter: " + str(outbox_mod.dead_letter_count(db)) + "件")
        lines.append("通知バックログ: " + str(outbox_mod.pending_backlog(db)) + "件")
        hb = db.execute("SELECT alive_at FROM heartbeat WHERE id=1").fetchone()
        lines.append("最終生存確認: " + (hb[0] if hb else "なし"))
        health_rows = db.execute(
            "SELECT state, COUNT(*) FROM source_health GROUP BY state").fetchall()
        if health_rows:
            lines.append("ソース状態: "
                         + ", ".join(r[0] + "x" + str(r[1]) for r in health_rows))
        self._send(chat_id, "\n".join(lines + self._status_extra_lines(db)))
        return "status"

    def _status_extra_lines(self, db) -> list:
        # 9-C24: items deterministically available from the DB - the
        # per-kind recent fetch summary, the last fetch time, the
        # observation-gap count and the unconfirmed-deadline count.
        lines = []
        since = clock.to_iso(clock.now(db) - timedelta(hours=24))
        # W-check B5 (collector 中-1): this summary grouped by
        # sources.kind (the db/shop/community taxonomy) while the
        # health digest groups by FETCHER kind (digest.
        # _fetcher_kind_counts, the 9-B13 taxonomy) — the same 24h
        # of fetches showed as two different breakdowns under the
        # same 「種別」 label, so the two screens could not be
        # cross-checked. One shared axis now: the digest's.
        from . import digest as _digest_mod
        kind_counts = _digest_mod._fetcher_kind_counts(db, since)
        if kind_counts:
            lines.append("直近24hの取得（種別 成功/失敗/BLOCKED）: "
                         + "; ".join(
                             f"{k}: {ok}/{fail}/{blocked}"
                             for k, ok, fail, blocked in
                             kind_counts))
        else:
            lines.append("直近24hの取得: （取得実績なし）")
        last_fetch = db.execute(
            "SELECT MAX(COALESCE(finished_at, started_at)) FROM fetch_runs"
        ).fetchone()[0]
        lines.append("最終フェッチ時刻: " + (str(last_fetch) if last_fetch
                                            else "なし"))
        # W-check B5 (collector 中-2): the gap count was the
        # ALL-TIME total including long-closed gaps — a number
        # that only ever grows and reads as 'observation is
        # missing right now'. health.py's own semantics are the
        # OPEN gaps (gap_end IS NULL); count those.
        gaps = db.execute(
            "SELECT COUNT(*) FROM observation_gaps"
            " WHERE gap_end IS NULL").fetchone()[0]
        lines.append("観測ギャップ（未解消）: " + str(gaps) + "件")
        # R19 (maniac A): this count had NO superseded filter and
        # a confidence-only axis, so /status disagreed with both
        # the health digest headline and /deadlines. One shared
        # definition (deadlines_ops.unconfirmed_count) now.
        from . import deadlines_ops as _dops
        lines.append("締切未確定: "
                     + str(_dops.unconfirmed_count(db)) + "件")
        return lines
