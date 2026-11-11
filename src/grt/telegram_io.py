"""Telegram transport layer.

Real transport uses the Telegram Bot API over HTTPS (free). Tests inject
MockTransport. No token is ever stored in code; it comes from the
environment / data/.env on the user's own machine.
"""
from __future__ import annotations

import httpx

from . import config

API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """A Telegram API failure.

    W-check B2 (adversarial QA D-5): the first version flattened every
    failure to "HTTP <code>" and threw the response body away, so the
    server's own instructions — above all 429's ``retry_after`` — were
    lost and callers retried on a fixed schedule against an explicit
    server directive. The parsed ``error_code`` / ``description`` /
    ``retry_after`` now travel with the exception.
    """

    def __init__(self, message: str, *, error_code: int | None = None,
                 retry_after: int | None = None,
                 description: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.description = description


def _error_from_response(what: str, resp) -> TelegramError:
    """Build a TelegramError from a non-OK Bot API response, keeping
    whatever structured detail the body carries."""
    code = None
    description = None
    retry_after = None
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        code = data.get("error_code")
        description = data.get("description")
        params = data.get("parameters") or {}
        if isinstance(params, dict):
            ra = params.get("retry_after")
            if isinstance(ra, (int, float)) and ra > 0:
                retry_after = int(ra)
    msg = f"{what} HTTP {resp.status_code}"
    if description:
        msg += f": {description}"
    return TelegramError(msg, error_code=code or resp.status_code,
                         retry_after=retry_after,
                         description=description)


def _ok_result(what: str, resp) -> dict:
    if resp.status_code != 200:
        raise _error_from_response(what, resp)
    try:
        data = resp.json()
    except Exception as exc:
        raise TelegramError(f"{what} returned an unreadable body: {exc}")
    if not isinstance(data, dict) or not data.get("ok"):
        # HTTP 200 with ok=false still carries the structured error
        # fields; extract them the same way as for non-200 responses.
        code = data.get("error_code") if isinstance(data, dict) else None
        description = (data.get("description")
                       if isinstance(data, dict) else None)
        params = (data.get("parameters") or {}) \
            if isinstance(data, dict) else {}
        retry_after = None
        if isinstance(params, dict):
            ra = params.get("retry_after")
            if isinstance(ra, (int, float)) and ra > 0:
                retry_after = int(ra)
        msg = f"{what} failed: {description or data}"
        raise TelegramError(msg, error_code=code,
                            retry_after=retry_after,
                            description=description)
    if "result" not in data:
        # R9 adversarial QA N-1: {"ok": true} with no result used to
        # raise a raw KeyError. On the fetch path the poll loop
        # happened to absorb it as transient, but on the SEND path it
        # escaped bot._send's TelegramError-only transient wrapping
        # and the update was classified invalid — the user's command
        # was permanently lost, the exact D-3 shape. A malformed
        # success is a TelegramError like any other bad response.
        raise TelegramError(f"{what} returned ok without a result")
    return data["result"]


class HttpTransport:
    def __init__(self, token: str | None = None):
        self.token = token or config.telegram_bot_token()

    def send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = httpx.post(
            f"{API_BASE}/bot{self.token}/sendMessage", json=payload, timeout=30)
        return _ok_result("sendMessage", resp)

    def answer_callback_query(self, callback_query_id: str,
                              text: str | None = None) -> dict:
        """Answer an inline-button callback so the client stops showing
        the button's loading spinner. Without this call a tapped button
        appears to glow/spin forever even when the tap was processed."""
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        resp = httpx.post(
            f"{API_BASE}/bot{self.token}/answerCallbackQuery",
            json=payload, timeout=15)
        return _ok_result("answerCallbackQuery", resp)


class MockTransport:
    """Test double recording every successful send."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.answered: list[str] = []
        self._failures_left = 0

    def answer_callback_query(self, callback_query_id: str,
                              text: str | None = None) -> dict:
        self.answered.append(str(callback_query_id))
        return {"ok": True}

    def fail_next(self, n: int) -> None:
        self._failures_left = n

    def send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise TelegramError("mock delivery failure")
        self.sent.append((str(chat_id), text))
        return {"ok": True, "chat_id": chat_id}


def fetch_updates(token: str, offset: int | None = None,
                  timeout: int = 30) -> list[dict]:
    """Long-polling getUpdates (used by the resident bot loop).

    W-check B2 (adversarial QA D-9): the response used to be trusted
    blindly — an unparseable body raised a raw ValueError and a
    non-list ``result`` was handed to the poll loop, which then
    manufactured per-"update" errors out of dict keys. Bad shapes are
    now normalised to TelegramError, which the poll loop already
    treats as a transient fetch failure.
    """
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = httpx.get(f"{API_BASE}/bot{token}/getUpdates",
                     params=params, timeout=timeout + 10)
    result = _ok_result("getUpdates", resp)
    if not isinstance(result, list):
        raise TelegramError(
            f"getUpdates result is not a list: {type(result).__name__}")
    return result
