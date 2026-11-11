"""Game Record Tool - 診断ツール

Bot が返事をしない等のトラブル時に実行すると、設定と接続の状態を
日本語で表示します。トークンの値そのものは表示しません（長さと
形だけを確認します）。

使い方: check.bat をダブルクリックする（推奨）。
結果は画面表示に加えて data/logs/diagnose.txt にも保存されます。

登録リセット: `diagnose.py --reset-chat` を実行すると、許可 chat の
登録（settings.allowed_chat_id）だけを消去します。Telegram アカウントを
作り直した等で chat_id が変わった場合に使い、本体起動後に新しい
アカウントから /start を送れば再登録できます。許可リストの正は
DB の settings であり、.env の ALLOWED_CHAT_ID は使われません
（仕様 §11）。引数なしの通常診断では登録は変更されません。
"""
from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# W-check B1 (collector D-4): these imports used to be unconditional,
# so in exactly the state diagnose exists for — a mixed-version or
# damaged tree where grt itself fails to import — diagnose died
# before section 1 and diagnose.txt was never written. The imports
# are now guarded; main() reports the failure and still runs every
# section that can run, saving output incrementally.
try:
    import httpx  # noqa: E402
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore[assignment]
try:
    from grt import config  # noqa: E402
    from grt import logtrim  # noqa: E402
    from grt import version as version_mod  # noqa: E402
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001
    config = None  # type: ignore[assignment]
    logtrim = None  # type: ignore[assignment]
    version_mod = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc

LINES: list[str] = []


def out(text: str = "") -> None:
    print(text, flush=True)
    LINES.append(text)


def ng_items() -> list[str]:
    return [ln for ln in LINES if ln.startswith("[NG]")]


def check_env_and_tokens() -> tuple[str, str]:
    out("== 1. 設定ファイル（data\\.env） ==")
    config.load_dotenv()
    env_path = config.data_dir() / ".env"
    if env_path.exists():
        out(f"[OK] data\\.env があります（{env_path}）")
    else:
        out("[NG] data\\.env がありません。setup.bat を実行してください。")
        out("     ※ .env.template の方を編集しても反映されません。")
    # W-check B1 (maniac A #3): show WHERE the data actually lives.
    # A copied .env can carry GRT_DATA_DIR pointing at the OLD
    # folder; the new code then silently keeps using the old data
    # and this diagnosis would look at the same wrong place without
    # ever saying so.
    data_dir = config.data_dir().resolve()
    project_root = Path(__file__).resolve().parent
    out(f"     データの場所: {data_dir}")
    if data_dir != (project_root / "data").resolve():
        out("[--] データの場所が通常（このフォルダの data）と違います。")
        out("     .env の GRT_DATA_DIR が別の場所を指定している可能性があります。")
        out("     方法Aで更新した直後なら、旧フォルダを指したままになっていないか")
        out("     確認してください（旧フォルダを消すと空のDBで起動します）。")
    # W-check B1 (collector D-5): optional API keys — presence and
    # length only, so a typo'd or unlisted key is visible instead of
    # silently ignored (the v2.0.1 accident class).
    import os
    out("")
    out("-- 任意の API キー（市場 API 用） --")
    for key in ("EBAY_OAUTH_TOKEN", "RAKUTEN_APP_ID", "YAHOO_APP_ID"):
        value = os.environ.get(key, "")
        if value and "ここに" not in value:
            out(f"[OK] {key} が設定されています（{len(value)}文字）。")
        else:
            out(f"[--] {key} は未設定です（市場 API を使う場合のみ必要）。")
    # W-check B2 (seller D-5 / wholesaler D-9): two invisible killers
    # get a surface here — disk exhaustion (which failed silently in
    # several paths at once) and a nonzero test-clock offset in
    # settings (which shifts every deadline/quiet judgement without
    # any visible trace).
    out("")
    out("-- ディスクと DB --")
    try:
        import shutil
        usage = shutil.disk_usage(str(data_dir))
        free_mb = usage.free // (1024 * 1024)
        if usage.free < 500 * 1024 * 1024:
            out(f"[NG] ディスクの空き容量が {free_mb}MB しかありません。"
                "記録や通知が失敗し始めます。不要なファイルを整理してください。")
        else:
            out(f"[OK] ディスクの空き容量: {free_mb}MB")
    except Exception as exc:
        out(f"[--] ディスク容量を確認できませんでした（{exc!r}）")
    try:
        db_file = config.db_path()
        if db_file.exists():
            out(f"     DB ファイル: {db_file.stat().st_size // 1024}KB"
                f"（{db_file}）")
    except Exception:
        pass
    try:
        from grt import db as _dbmod
        _c = _dbmod.connect()
        try:
            row = _c.execute(
                "SELECT value FROM settings"
                " WHERE key='test_clock_offset_seconds'").fetchone()
            off = int(row[0]) if row and row[0] not in (None, "") else 0
        finally:
            _c.close()
        if off:
            out(f"[NG] test_clock_offset_seconds が {off} に設定されています。"
                "全ての時刻判定（締切・静穏）がずれます。テスト用DBでない限り"
                "この行は残っているべきではありません。")
        else:
            out("[OK] テスト用時計オフセットは 0 です（時刻判定は実時計通り）。")
    except Exception:
        out("[--] 時計オフセットを確認できませんでした（DB未作成の可能性）。")
    tg = config.telegram_bot_token() or ""
    dg = ""
    try:
        dg = config.discogs_token() or ""
    except AttributeError:
        import os
        dg = os.environ.get("DISCOGS_TOKEN", "")
    out("")
    out("-- Telegram トークン --")
    if not tg:
        out("[NG] TELEGRAM_BOT_TOKEN が未設定です（空欄）。")
    elif "ここに" in tg:
        out("[NG] TELEGRAM_BOT_TOKEN が雛形のまま（「ここに貼り付け」）です。")
        out("     BotFather のトークンに貼り替えてください（ガイドのステップ4〜5）。")
    elif not re.fullmatch(r"\d+:[A-Za-z0-9_\-]{20,}", tg):
        out("[NG] TELEGRAM_BOT_TOKEN の形が想定と違います。")
        out("     正しい形: 数字 : 英数字の長い文字列（コロンごと全部を貼る）。")
        out("     スペースや改行が混ざっていないか確認してください。")
    else:
        out(f"[OK] TELEGRAM_BOT_TOKEN が設定されています（{len(tg)}文字、形は正常）。")
    out("")
    out("-- Discogs トークン --")
    if not dg:
        out("[NG] DISCOGS_TOKEN が未設定です（空欄）。")
    elif "ここに" in dg:
        out("[NG] DISCOGS_TOKEN が雛形のまま（「ここに貼り付け」）です。")
        out("     Discogs のトークンに貼り替えてください（ガイドのステップ6）。")
    else:
        out(f"[OK] DISCOGS_TOKEN が設定されています（{len(dg)}文字）。")
    return tg, dg


def check_telegram(tg: str) -> None:
    out("")
    out("== 2. Telegram への接続確認（getMe） ==")
    if not tg or "ここに" in tg or not re.fullmatch(r"\d+:[A-Za-z0-9_\-]{20,}", tg):
        out("[--] トークンが未設定／不正なため、接続確認をスキップしました。")
        return
    if httpx is None:
        out("[--] httpx が読み込めないため、接続確認をスキップしました。")
        return
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            resp = client.get(f"https://api.telegram.org/bot{tg}/getMe")
    except Exception as exc:
        out(f"[NG] Telegram に接続できませんでした: {exc!r}")
        out("     ネット接続を確認してください。")
        return
    if resp.status_code == 401:
        out("[NG] Telegram がトークンを拒否しました（401）。")
        out("     トークンの貼り間違いか、BotFather で /revoke した古いトークンです。")
        out("     BotFather で /token を送り、現在のトークンを貼り直してください。")
        return
    # W-check B1 (collector D-4): a body that CLAIMS to be JSON but
    # is truncated must not kill the whole diagnosis.
    data = {}
    if resp.headers.get("content-type", "").startswith("application/json"):
        try:
            data = resp.json() or {}
        except Exception as exc:
            out(f"[--] Telegram の応答本文を解釈できませんでした: {exc!r}")
            data = {}
    if resp.status_code == 200 and data.get("ok"):
        uname = (data.get("result") or {}).get("username", "?")
        out(f"[OK] トークンは有効です。このトークンの Bot は @{uname} です。")
        out(f"     → /start を送る相手が @{uname} と一致しているか確認してください。")
    else:
        out(f"[NG] 予期しない応答でした（HTTP {resp.status_code}）。")


def check_discogs(dg: str) -> None:
    out("")
    out("== 3. Discogs への接続確認 ==")
    if not dg or "ここに" in dg:
        out("[--] トークンが未設定のため、接続確認をスキップしました。")
        return
    if httpx is None:
        out("[--] httpx が読み込めないため、接続確認をスキップしました。")
        return
    try:
        with httpx.Client(trust_env=False, timeout=15) as client:
            resp = client.get(
                "https://api.discogs.com/oauth/identity",
                headers={"Authorization": f"Discogs token={dg}",
                         "User-Agent": "GameRecordTool/1.0 diagnose"})
    except Exception as exc:
        out(f"[NG] Discogs に接続できませんでした: {exc!r}")
        return
    if resp.status_code == 200:
        try:
            uname = (resp.json() or {}).get("username", "?")
        except Exception:
            uname = "?"
        out(f"[OK] トークンは有効です（Discogs ユーザー: {uname}）。")
    elif resp.status_code == 401:
        out("[NG] Discogs がトークンを拒否しました（401）。")
        out("     再発行した場合は、古いトークンが無効になっています。")
        out("     https://www.discogs.com/settings/developers で現在のトークンを確認してください。")
    else:
        out(f"[--] 応答は HTTP {resp.status_code} でした（トークン以外の問題の可能性）。")


def heartbeat_status(db, now: datetime | None = None) -> tuple[str, str]:
    """Judge the main process's liveness from the heartbeat table.

    Returns (verdict, detail): verdict is "alive", "stale", "none" or
    "unknown". The staleness threshold is the watchdog's own
    STALE_MINUTES, and time reads go through grt.clock.now(db) exactly
    like the watchdog, so this diagnosis and the watchdog can never
    disagree about whether the process counts as alive. (The
    telegram_update_offset alone cannot answer this: it stays empty
    while a healthy process simply has not received an update yet,
    and stays non-empty forever after the process dies.)
    """
    from grt import clock
    from grt.watchdog import STALE_MINUTES
    try:
        row = db.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
    except Exception:
        return "unknown", "heartbeat テーブルを読めませんでした"
    if row is None or not row["alive_at"]:
        return "none", "本体の起動記録がありません"
    try:
        alive_at = clock.parse_iso(row["alive_at"])
    except (TypeError, ValueError):
        alive_at = None
    if alive_at is None:
        return "unknown", f"alive_at を解釈できませんでした: {row['alive_at']!r}"
    current = now if now is not None else clock.now(db)
    age_minutes = (current - alive_at).total_seconds() / 60.0
    boot = f"（最終起動: {row['boot_at']}）" if row["boot_at"] else ""
    if age_minutes <= STALE_MINUTES:
        return "alive", f"最終ハートビート {max(age_minutes, 0):.0f} 分前{boot}"
    if age_minutes < 60:
        return "stale", f"最終ハートビート {age_minutes:.0f} 分前{boot}"
    return "stale", f"最終ハートビート {age_minutes / 60:.1f} 時間前{boot}"


def poll_liveness_lines(db) -> list[str]:
    """Bot (poll thread) liveness, separate from process liveness.

    W-check B2 (AI-engineer #3): the poll thread could die while the
    scheduler-side heartbeat kept beating, so 'process alive' said
    nothing about 'bot alive'. The poll loop stamps poll_alive_at on
    every successful fetch cycle; the heartbeat job also restarts a
    dead poll thread (system_events kinds poll_thread_dead /
    poll_thread_restart_exhausted), so a fresh stamp plus no recent
    restart events means the bot is genuinely receiving.
    """
    from grt import clock
    try:
        row = db.execute("SELECT value FROM settings"
                         " WHERE key='poll_alive_at'").fetchone()
    except Exception:
        return []
    if row is None or not row[0]:
        return []
    try:
        stamp = clock.parse_iso(row[0])
    except (TypeError, ValueError):
        stamp = None
    if stamp is None:
        return [f"[--] Bot の受信記録（poll_alive_at）を解釈できません"
                f"でした: {row[0]!r}"]
    age_minutes = (clock.now(db) - stamp).total_seconds() / 60.0
    if age_minutes <= 3:
        return [f"[OK] Bot は受信中です（最終ポーリング "
                f"{max(age_minutes, 0):.0f} 分前）。"]
    return [f"[--] Bot の受信が止まっている可能性があります"
            f"（最終ポーリング {age_minutes:.0f} 分前）。"
            "本体プロセスが生きていてもポーリングスレッドだけが"
            "落ちている状態です。",
            "     ハートビートジョブが自動で再起動を試みます"
            "（system_events の poll_thread_dead を確認）。"
            "繰り返す場合は == 6 == の poll_errors.log を"
            "確認してください。"]


def heartbeat_report_lines(db) -> list[str]:
    verdict, detail = heartbeat_status(db)
    lines = poll_liveness_lines(db)
    if verdict == "alive":
        return [f"[OK] 本体は起動しています（{detail}）。"] + lines
    if verdict == "stale":
        return ["[--] 本体は止まっている可能性が高いです"
                f"（{detail}）。",
                "     PC が起動していても、タスク未登録・無効化や起動時エラーで"
                "本体が動いていないことがあります。",
                "     == 5 == のタスク状態と == 6 == のエラーログを確認してください。"]
    if verdict == "none":
        return ["[--] " + detail + "。",
                "     本体が一度も起動していないか、別のデータベースを"
                "見ている可能性があります。"]
    return [f"[--] 稼働状態を判定できませんでした（{detail}）。"]


def check_db() -> None:
    out("")
    out("== 4. データベースと登録状態 ==")
    db_path = config.db_path()
    if not db_path.exists():
        out("[NG] データベース（data\\grt.db）がありません。")
        out("     setup.bat の [5/6]（DB初期化）が完了していない可能性があります。")
        out("     setup.bat をもう一度実行してください。")
        return
    from grt import db as dbmod
    from grt.config import get_setting
    db = dbmod.connect()
    try:
        n = db.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
        out(f"[OK] データベースを開けました（カタログ {n} 件）。")
    except Exception as exc:
        out(f"[NG] editions テーブルを読めませんでした: {exc!r}")
        return
    allowed = get_setting(db, "allowed_chat_id")
    if allowed:
        out(f"[OK] /start の登録は済んでいます（許可 chat_id が登録済み）。")
        out("     それでも返事が無い場合、二重起動か送信側のエラーの可能性があります。")
        out("     data\\logs\\poll_errors.log（下記 6.）を確認してください。")
    else:
        out("[--] /start の登録はまだ記録されていません。")
        out("     本体が起動している状態で、Bot に /start を送ってください。")
    offset = get_setting(db, "telegram_update_offset")
    if offset:
        out("[OK] Telegram の新着取得は進んでいます（本体がメッセージを受け取った形跡あり）。")
    else:
        out("[--] Telegram の新着取得の形跡がありません。")
        out("     本体が一度も正常にポーリングできていない可能性があります。")
    gate = get_setting(db, "deadline_override_quiet_selected")
    if gate == "1":
        out("[OK] 静音例外の初期選択は完了しています（通常運用中）。")
    else:
        out("[--] 静音例外の初期選択はまだです。Bot の質問にボタンで答えてください。")
        out("     ※ この選択が終わるまで、通常コマンドは案内メッセージのみ返します。")
    out("")
    out("-- 本体の稼働状態（ハートビート） --")
    for line in heartbeat_report_lines(db):
        out(line)


def reset_chat_registration() -> int:
    """Clear settings.allowed_chat_id so the next /start re-registers.

    Shows the registration state before and after, and states plainly
    that the registration was erased. Only the allowed_chat_id setting
    is touched (watch levels, policy versions, history all stay).
    """
    out("============================================================")
    out(" Game Record Tool - 許可 chat 登録のリセット")
    out("============================================================")
    if config is None:
        out(f"[NG] 本体コード（grt）を読み込めませんでした: {_IMPORT_ERROR!r}")
        out("     方法Aで入れ直してから再実行してください。")
        return 1
    config.load_dotenv()
    db_path = config.db_path()
    if not db_path.exists():
        out("[NG] データベース（data\\grt.db）がありません。")
        out("     消去する登録がありません。setup.bat を先に実行してください。")
        return 1
    from grt import db as dbmod
    from grt.config import get_setting
    db = dbmod.connect()
    dbmod.init_schema(db)
    allowed = get_setting(db, "allowed_chat_id")
    if allowed:
        out("[OK] 現在の登録状態: 許可 chat は登録済みです。")
    else:
        out("[--] 現在の登録状態: 許可 chat は未登録です。")
    db.execute("DELETE FROM settings WHERE key='allowed_chat_id'")
    db.commit()
    after = get_setting(db, "allowed_chat_id")
    if after:
        out("[NG] 消去に失敗しました（登録が残っています）。")
        return 1
    if allowed:
        out("[OK] 許可 chat の登録を消去しました。")
    else:
        out("[OK] 登録は元々ありませんでした（変更なし）。")
    out("     次に本体を起動した状態で、新しいアカウントから Bot に")
    out("     /start を送ると、その chat が許可 chat として再登録されます。")
    out("============================================================")
    try:
        log_dir = config.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "diagnose.txt").write_text(
            "\n".join(LINES), encoding="utf-8")
    except Exception:
        pass
    return 0


def _xml_settings_flag(root, ns: str, tag: str) -> str | None:
    el = root.find(f".//{ns}Settings/{ns}{tag}")
    if el is None or el.text is None:
        return None
    return el.text.strip().lower()


def parse_task_xml(xml_text: str) -> dict:
    """Extract task facts from `schtasks /Query /XML` output.

    XML is used instead of the localised list output so the checks do
    not depend on the Windows display language. Any parse problem
    yields {"parse_error": True} and the caller degrades gracefully.

    W-check B1: kept in step with setup_tasks.parse_task_xml — it
    also reads the trigger-level Enabled flags and the Settings
    that decide whether a resident process survives (a missing
    ExecutionTimeLimit element means the PT72H schema default).
    tests/test_wcheck_b1.py exercises this copy against fakewin so
    the two implementations cannot drift apart silently.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {"parse_error": True}
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[: root.tag.index("}") + 1]
    enabled_el = root.find(f".//{ns}Settings/{ns}Enabled")
    enabled = True
    if enabled_el is not None and (enabled_el.text or "").strip().lower() == "false":
        enabled = False
    triggers_enabled = True
    for trig in root.findall(f".//{ns}Triggers/*"):
        en = trig.find(f"{ns}Enabled")
        if en is not None and (en.text or "").strip().lower() == "false":
            triggers_enabled = False
    limit_el = root.find(f".//{ns}Settings/{ns}ExecutionTimeLimit")
    limit = ((limit_el.text or "").strip().upper()
             if limit_el is not None else "PT72H")
    command = ""
    arguments = ""
    exec_el = root.find(f".//{ns}Actions/{ns}Exec")
    if exec_el is not None:
        cmd_el = exec_el.find(f"{ns}Command")
        arg_el = exec_el.find(f"{ns}Arguments")
        command = (cmd_el.text or "") if cmd_el is not None else ""
        arguments = (arg_el.text or "") if arg_el is not None else ""
    return {"enabled": enabled and triggers_enabled,
            "settings_enabled": enabled,
            "triggers_enabled": triggers_enabled,
            "command": command, "arguments": arguments,
            "execution_time_limit": limit,
            "disallow_on_batteries":
                _xml_settings_flag(root, ns, "DisallowStartIfOnBatteries"),
            "stop_on_batteries":
                _xml_settings_flag(root, ns, "StopIfGoingOnBatteries"),
            "start_when_available":
                _xml_settings_flag(root, ns, "StartWhenAvailable")}


_ZERO_LIMITS = {"PT0S", "PT0M", "PT0H", "P0D", "P0DT0H0M0S", ""}


def task_settings_problems(info: dict) -> list[str]:
    """Why this task's Settings are wrong for a resident process
    (same rules as setup_tasks.settings_problems)."""
    problems = []
    if info.get("execution_time_limit", "PT72H") not in _ZERO_LIMITS:
        problems.append(
            f"実行時間の上限が {info.get('execution_time_limit')} に"
            "設定されています（常駐プロセスが途中で停止されます。"
            "無制限 PT0S が必要です）")
    if info.get("disallow_on_batteries") == "true":
        problems.append("バッテリー駆動中は開始しない設定です")
    if info.get("stop_on_batteries") == "true":
        problems.append("バッテリー駆動に切り替わると停止する設定です")
    return problems


def expected_task_targets() -> tuple[Path, str, str]:
    """(venv python, main script name, watchdog script name) for THIS
    install folder — the reference the registered tasks must match."""
    root = Path(__file__).resolve().parent
    return root / ".venv" / "Scripts" / "pythonw.exe", "run.py", "watchdog.py"


def _norm_path(p: str) -> str:
    return p.strip().strip('"').replace("/", "\\").lower()


def _autostart_mode() -> str | None:
    try:
        text = (config.log_dir() / "autostart_mode.txt").read_text(
            encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def _lnk_target_hint(path: Path) -> str | None:
    """Best-effort read of a .lnk file's target path.

    A .lnk is a binary Shell Link; the target path appears inside
    it as a UTF-16LE (and often also ANSI) string. This reads the
    raw bytes and looks for a path ending in one of the tool's
    script names. Returns the found path, or None when no path-like
    string can be recovered (verification then degrades to
    existence-only and SAYS so, instead of claiming [OK]).
    """
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    texts = []
    for enc in ("utf-16le", "latin-1"):
        try:
            texts.append(raw.decode(enc, errors="ignore"))
        except Exception:
            pass
    for text in texts:
        for script in ("task_launcher.py", "run.py", "watchdog_loop.py", "watchdog.py"):
            idx = text.lower().find(script)
            while idx >= 0:
                start = idx
                while start > 0 and text[start - 1] not in '\x00<>|"\r\n':
                    start -= 1
                candidate = text[start:idx + len(script)].strip()
                if ":\\" in candidate or ":/" in candidate:
                    return candidate
                idx = text.lower().find(script, idx + 1)
    return None


def _check_startup_shortcuts() -> bool:
    """Startup-folder fallback: both .lnk files must exist AND point
    at THIS folder (W-check B1, collector D-2 — existence-only used
    to print [OK] even for a shortcut left pointing at an old
    folder, the exact stale-target failure the schtasks side was
    fixed for in v1.8)."""
    import os
    appdata = os.environ.get("APPDATA") or str(
        Path.home() / "AppData" / "Roaming")
    folder = (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
              / "Programs" / "Startup")
    root = Path(__file__).resolve().parent
    expected = {
        "GameRecordTool": ("run.py", root / "task_launcher.py"),
        "GameRecordToolWatchdog": ("watchdog_loop.py", root / "task_launcher.py"),
        "GameRecordToolCollector": ("collector/server.py", root / "task_launcher.py"),
        "GameRecordToolSiteFreshness": ("site/freshness_loop.py", root / "task_launcher.py"),
    }
    ok = True
    for name, (script, launcher_path) in expected.items():
        path = folder / f"{name}.lnk"
        if not path.exists():
            out(f"[NG] スタートアップ フォルダに {name}.lnk がありません。")
            out("     setup.bat を再実行してください。")
            ok = False
            continue
        target = _lnk_target_hint(path)
        if target is None:
            out(f"[--] {name}.lnk はありますが、リンク先を確認できませんでした"
                "（存在のみ確認）。")
            out("     旧フォルダを指している可能性を排除できません。")
            continue
        try:
            raw = path.read_bytes()
            embedded = raw.decode("utf-16le", errors="ignore").lower()
        except Exception:
            embedded = ""
        data_arg_ok = _norm_path(str(config.data_dir())) in _norm_path(embedded)
        script_arg_ok = f'--script "{script}"' in embedded
        if (_norm_path(target) == _norm_path(str(launcher_path))
                and data_arg_ok and script_arg_ok):
            out(f"[OK] スタートアップ フォルダに {name}.lnk があり、"
                "このフォルダと共有データを指しています。")
        else:
            out(f"[NG] {name}.lnk はありますが、このフォルダの本体を"
                "指していません。")
            out(f"     リンク先: {target}")
            out(f"     本来の向く先: {launcher_path} --data-dir {config.data_dir()} --script {script}")
            out("     setup.bat を再実行してください。")
            ok = False
    return ok


def check_tasks() -> None:
    out("")
    out("== 5. タスクスケジューラ登録 ==")
    if _autostart_mode() == "startup-folder":
        out("このインストールは「スタートアップ フォルダ方式」で自動起動します")
        out("（タスク スケジューラが使えない環境向けの方式です）。")
        _check_startup_shortcuts()
        out("[--] この方式では、タスク スケジューラ側に残った古い登録は"
            "ここに表示されません。")
        out("     二重起動が疑われる場合は setup.bat を再実行してください"
            "（登録し直しで整理されます）。")
        return
    python_path, _, _ = expected_task_targets()
    root = Path(__file__).resolve().parent
    expected = (("GameRecordTool", "run.py"),
                ("GameRecordToolWatchdog", "watchdog.py"),
                ("GameRecordToolCollector", "collector/server.py"),
                ("GameRecordToolSiteFreshness", "site/update_site.py"))
    for name, script in expected:
        try:
            proc = subprocess.run(
                ["schtasks", "/Query", "/TN", name, "/XML"],
                capture_output=True, text=True, timeout=20)
        except Exception as exc:
            out(f"[--] タスク {name} の確認ができませんでした: {exc!r}")
            continue
        if proc.returncode != 0:
            out(f"[NG] タスク {name} が登録されていません。")
            out("     setup.bat を実行してください（管理者権限は不要です）。失敗した場合は下記 == 8 == のログに理由が残ります。")
            continue
        info = parse_task_xml(proc.stdout or "")
        if info.get("parse_error"):
            out(f"[--] タスク {name} は登録されていますが、詳細を確認できませんでした。")
            continue
        if not info["enabled"]:
            out(f"[NG] タスク {name} は登録されていますが「無効」になっています。")
            out("     タスク スケジューラで右クリック →「有効化」してください。")
            continue
        cmd_ok = _norm_path(info["command"]) == _norm_path(str(python_path))
        args_norm = _norm_path(info["arguments"])
        arg_ok = (_norm_path(str(root / "task_launcher.py")) in args_norm
                  and _norm_path(str(config.data_dir())) in args_norm
                  and f'--script "{script}"' in args_norm)
        problems = task_settings_problems(info)
        if problems:
            out(f"[NG] タスク {name} の設定が常駐用になっていません:")
            for problem in problems:
                out(f"     - {problem}")
            out("     setup.bat を再実行してください（登録し直しで直ります）。")
        if cmd_ok and arg_ok:
            if not problems:
                out(f"[OK] タスク {name} は登録されています（このフォルダの本体を指しています）。")
        else:
            out(f"[NG] タスク {name} はありますが、このフォルダの本体を指していません。")
            out(f"     登録先: {info['command']} {info['arguments']}")
            out(f"     本来の向く先: {python_path} {script}")
            out("     フォルダを移動した場合は古い登録が残っています。")
            out("     setup.bat を実行してください（管理者権限は不要です）。失敗した場合は下記 == 8 == のログに理由が残ります。")


def check_logs() -> None:
    out("")
    out("== 6. エラーログ ==")
    log_path = config.log_dir() / "poll_errors.log"
    if not log_path.exists():
        out("[--] poll_errors.log はありません（ポーリングの例外は記録されていません）。")
    else:
        # Tail-only read (W-check B2, seller D-4): the whole file was
        # loaded to show 30 lines; on a multi-MB log the diagnosis
        # itself became the slow part.
        tail = logtrim.tail_lines(log_path, 30) if logtrim else []
        out(f"poll_errors.log（最後の {len(tail)} 行）:")
        for ln in tail:
            out("     " + ln)
    # W-check B1 (collector D-1/D-3): job_errors.log and
    # watchdog_errors.log were WRITTEN by the program but never
    # SHOWN here — the same hole as the v1.7 setup_tasks.log
    # omission, re-opened by the very fixes that added the logs.
    _dump_log_tail(
        "job_errors.log", 30,
        "定期ジョブの例外は記録されていません。")
    _dump_log_tail(
        "watchdog_errors.log", 20,
        "ウォッチドッグ自体の例外は記録されていません。")


def check_manifest() -> None:
    """納品ファイルの完全性（版の混在チェック）。

    MANIFEST.sha256（納品時に生成）と実ファイルを照合する。
    上書きコピーによる更新では「新版で削除された旧ファイル」が
    残り、版が混ざったまま動く事故が起こる。欠落・相違・余分な
    コードファイルをここで検出する。data/ と .venv は対象外。
    """
    import hashlib

    out("")
    out("== 7. コードの完全性（版の混在チェック） ==")
    root = Path(__file__).resolve().parent
    manifest_path = root / "MANIFEST.sha256"
    if not manifest_path.exists():
        out("[--] MANIFEST.sha256 がありません（開発用ツリー等では対象外）。")
        return
    expected: dict[str, str] = {}
    for line in manifest_path.read_text(
            encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            expected[parts[1].lstrip("*").strip()] = parts[0].lower()
    problems = 0
    for rel, want in sorted(expected.items()):
        target = root / rel
        if not target.exists():
            out(f"[NG] 納品ファイルがありません: {rel}")
            problems += 1
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != want:
            out(f"[NG] 内容が納品時と異なります（版の混在・破損の可能性）: {rel}")
            problems += 1
    extras: list[str] = []
    runtime_outputs = {"startup_failure.txt", "watchdog_failure.txt"}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        if (rel == "MANIFEST.sha256" or parts[0] in {
                "data", ".venv", ".git", ".playwright-cli"}
                or "__pycache__" in parts or ".pytest_cache" in parts
                or rel in runtime_outputs):
            continue
        if rel not in expected:
            extras.append(rel)
    for rel in extras[:20]:
        out(f"[NG] 納品物に無いファイルがあります（旧版の残りの可能性）: {rel}")
        problems += 1
    if problems == 0:
        out(f"[OK] 全 {len(expected)} ファイルが納品時と一致しています（版の混在なし）。")
    else:
        out("     対処: 新しいフォルダに解凍し直し、data フォルダだけを移す")
        out("           「方法A」で入れ直してください（上書きコピーは残留ファイルで")
        out("           版が混ざるため非推奨です）。SETUP_GUIDE.md の更新手順を参照。")


def _dump_log_tail(name: str, tail: int, missing_note: str) -> None:
    path = config.log_dir() / name
    if not path.exists():
        out(f"     {name}: ありません — {missing_note}")
        return
    if logtrim is not None:
        lines = logtrim.tail_lines(path, tail)
    else:
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()[-tail:]
        except Exception as exc:
            out(f"     {name}: 読めませんでした（{exc!r}）")
            return
    out(f"     {name}（最後の {len(lines)} 行）:")
    for ln in lines:
        out("       " + ln)


def check_setup_logs() -> None:
    """セットアップ系ログの一式を診断に含める。

    v1.7 までの診断は setup の登録エラーを含んでおらず、失敗の
    理由がどこにも無いまま「タスク未登録」だけが繰り返し報告
    された。v1.8 からは setup.bat 自身のログ・登録ツールの
    ログ・起動ログをこの診断1通に含める。
    """
    out("")
    out("== 8. セットアップのログ（失敗理由はここに出ます） ==")
    out("     ※ 方法Aで data を引き継いだ場合、旧フォルダ時代の記録も")
    out("       混ざって表示されます。日付・時刻で判別してください。")
    _dump_log_tail(
        "setup_bat.log", 40,
        "このフォルダで setup.bat（v1.8以降）はまだ実行されていません。")
    _dump_log_tail(
        "setup_tasks.log", 60,
        "自動起動の登録ステップはまだ実行されていません。")
    _dump_log_tail(
        "startup.log", 10,
        "本体の起動記録はまだありません。")
    # W-check B1 (collector D-3): duplicate-instance refusals were
    # recorded precisely so a 409 fight would leave a trace — and
    # then never shown in the one file the user sends.
    _dump_log_tail(
        "duplicate_instance.log", 10,
        "二重起動の拒否は記録されていません。")


_SAVE_OK = False


def _save_diagnose_txt() -> bool:
    """Persist whatever has been collected SO FAR (never raises).

    W-check B1 (collector D-4): diagnose.txt used to be written only
    at the very end, so one section's exception erased the whole
    diagnosis. It is now rewritten after every section.

    R4 residue: when grt itself fails to import, config is
    unavailable — the save path is then resolved the same
    stdlib-only way run.py resolves it (GRT_DATA_DIR env, else
    <project>/data), and the result is REPORTED so the closing
    banner never claims a save that did not happen.
    """
    global _SAVE_OK
    try:
        if config is not None:
            log_dir = config.log_dir()
        else:
            import os
            base = os.environ.get("GRT_DATA_DIR")
            root = Path(__file__).resolve().parent
            log_dir = (Path(base) if base else root / "data") / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "diagnose.txt").write_text(
            "\n".join(LINES), encoding="utf-8")
        _SAVE_OK = True
    except Exception:
        _SAVE_OK = False
    return _SAVE_OK


def _run_section(fn, *args):
    """Run one diagnosis section in isolation: an exception becomes
    an inline [NG] line, never a dead diagnosis."""
    try:
        return fn(*args)
    except Exception as exc:
        out(f"[NG] この診断項目の実行中にエラーが発生しました: {exc!r}")
        return None
    finally:
        _save_diagnose_txt()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--reset-chat" in args:
        return reset_chat_registration()
    out("============================================================")
    out(" Game Record Tool - 診断結果")
    out("============================================================")
    if _IMPORT_ERROR is not None:
        out(f"[NG] 本体コード（grt）を読み込めませんでした: {_IMPORT_ERROR!r}")
        out("     版の混在・破損の可能性があります。下記 == 7 == の")
        out("     完全性チェックと、この診断の残りを確認してください。")
    elif version_mod is not None:
        out(f" バージョン: {version_mod.app_version()}")
        out(f" コード最終更新: {version_mod.code_mtime_iso()}")
        last_boot = version_mod.last_startup_record()
        if last_boot:
            out(f" 最終起動記録: {last_boot}")
            # W-check B1 R4 (maniac A #6 residue): an inherited
            # startup.log can show the OLD version's record here —
            # say so when the versions visibly differ.
            import re as _re
            m = _re.search(r"version=([0-9][^\s]*)", last_boot)
            if m and m.group(1) != version_mod.app_version():
                out("     ※ この記録は旧バージョンのものです（方法Aで"
                    "引き継いだ直後は旧フォルダの記録が残ります）。")
        else:
            out(" 最終起動記録: なし（data\\logs\\startup.log がありません）")
            out("     ※ 本体が一度も正常に起動していない可能性があります。")
    out("============================================================")
    _save_diagnose_txt()
    tg = dg = ""
    result = _run_section(check_env_and_tokens)
    if result:
        tg, dg = result
    _run_section(check_telegram, tg)
    _run_section(check_discogs, dg)
    _run_section(check_db)
    _run_section(check_tasks)
    _run_section(check_logs)
    _run_section(check_manifest)
    _run_section(check_setup_logs)
    out("")
    out("============================================================")
    ng = ng_items()
    if ng:
        out(f" 要対応は {len(ng)} 件です。各 [NG] の下の対処を行ってください。")
    else:
        out(" [NG] はありませんでした。")
    saved = _save_diagnose_txt()
    if saved:
        out(" この画面の内容は data\\logs\\diagnose.txt にも保存されています。")
    else:
        out(" ※ 診断結果を diagnose.txt に保存できませんでした。")
        out("   この画面の内容をそのまま Muse に送ってください。")
    out(" 解決しない場合は、この内容をそのまま Muse に送ってください。")
    out("============================================================")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    raise SystemExit(main())
