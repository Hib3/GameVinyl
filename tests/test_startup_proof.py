"""Startup provability (debugger round, 2026-11-08).

Field symptom: on the user's Windows PC, run.py appeared to do
nothing and /start was only ever marked read — and there was no
on-disk evidence of whether the current code had started at all.
These tests pin the proof chain end to end:

- a VERSION file is the single source of truth for the build;
- a successful boot appends a verifiable record (version, time,
  DB path, gate state) to data/logs/startup.log;
- a boot that dies on an early exception STILL leaves a traceback
  in startup.log (run.py entry hardening);
- /status reports the running version;
- diagnose.py leads with version + code mtime + last boot record.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from grt import bot as bot_mod
from grt import version as version_mod
from grt.config import set_setting
from grt.telegram_io import MockTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAT = 424242


def _msg(uid, text, chat=CHAT):
    return {"update_id": uid,
            "message": {"message_id": uid,
                        "chat": {"id": chat, "type": "private"},
                        "text": text}}


def _clean_env(extra):
    env = dict(os.environ)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("DISCOGS_TOKEN", None)
    env.update(extra)
    return env


def test_version_file_is_source_of_truth():
    version_file = PROJECT_ROOT / "VERSION"
    assert version_file.exists(), "VERSION file missing at project root"
    expected = version_file.read_text(encoding="utf-8").strip()
    assert expected, "VERSION file is empty"
    assert version_mod.app_version() == expected


def test_code_mtime_is_reportable():
    stamp = version_mod.code_mtime_iso()
    assert stamp != "unknown"
    assert stamp.endswith("+09:00")


def test_write_startup_log_records_proof(db, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("GRT_DATA_DIR", str(data_dir))
    db_file = tmp_path / "grt.db"

    path = version_mod.write_startup_log(
        db_file=db_file, gate_open=bot_mod.gate_open(db))
    assert path is not None and path.exists()
    first = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert f"version={version_mod.app_version()}" in first
    assert f"db={db_file}" in first
    assert "gate=closed" in first
    assert first.startswith("[") and "+09:00" in first

    # Gate open is recorded just as explicitly (no silent ambiguity).
    set_setting(db, "deadline_override_quiet_selected", "1")
    version_mod.write_startup_log(
        db_file=db_file, gate_open=bot_mod.gate_open(db))
    last = version_mod.last_startup_record()
    assert last is not None and "gate=open" in last


def test_status_reports_version(db):
    transport = MockTransport()
    bot = bot_mod.Bot(db, transport)
    assert bot.handle_update(_msg(2001, "/start")) == "registered"
    assert bot.handle_update(_msg(2002, "/status")) == "status"
    status_texts = [text for _, text in transport.sent
                    if "【ステータス】" in text]
    assert status_texts, "no /status reply was sent"
    assert ("バージョン: " + version_mod.app_version()) in status_texts[-1]


def test_runpy_fatal_startup_leaves_traceback(tmp_path):
    """run.py dying before/at DB connect must still leave a trace."""
    data_dir = tmp_path / "data"
    bad_db = tmp_path / "a_directory"
    bad_db.mkdir()  # sqlite cannot open a directory as a database
    env = _clean_env({
        "GRT_DATA_DIR": str(data_dir),
        "GRT_DB_PATH": str(bad_db),
    })
    proc = subprocess.run(
        [sys.executable, "run.py"], cwd=PROJECT_ROOT, env=env,
        capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    log = data_dir / "logs" / "startup.log"
    assert log.exists(), "fatal startup left no startup.log"
    content = log.read_text(encoding="utf-8")
    assert "STARTUP FAILURE" in content
    assert "Traceback" in content
    assert "OperationalError" in content


def test_runpy_successful_boot_writes_startup_record(tmp_path):
    """A real run.py boot writes its proof line, then stays resident."""
    data_dir = tmp_path / "data"
    env = _clean_env({"GRT_DATA_DIR": str(data_dir)})
    proc = subprocess.Popen(
        [sys.executable, "run.py"], cwd=PROJECT_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        log = data_dir / "logs" / "startup.log"
        deadline = time.time() + 30
        content = ""
        while time.time() < deadline:
            if log.exists():
                content = log.read_text(encoding="utf-8")
                if "START version=" in content:
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        assert "START version=" + version_mod.app_version() in content
        assert "gate=closed" in content
        assert str(data_dir / "grt.db") in content
        assert (data_dir / "grt.db").exists()
        assert proc.poll() is None, "resident process exited on its own"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)


def test_diagnose_header_shows_version_and_last_boot(tmp_path):
    data_dir = tmp_path / "data"
    env = _clean_env({"GRT_DATA_DIR": str(data_dir),
                      "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.run(
        [sys.executable, "diagnose.py"], cwd=PROJECT_ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, proc.stderr[-2000:]
    head = "\n".join(proc.stdout.splitlines()[:10])
    assert "バージョン: " + version_mod.app_version() in head
    assert "コード最終更新:" in head
    assert "最終起動記録: なし" in head
