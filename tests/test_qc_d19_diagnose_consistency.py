"""QC debugger 19: diagnose.py / check.bat vs implementation consistency.

Field triage was misled twice by the diagnostic side itself:
(1) diagnose never read the heartbeat table, so it could not say
    whether the main process was alive — its only proxy
    (telegram_update_offset) is empty for a healthy-but-idle process
    and stays set forever after the process dies.
(2) the task check only tested existence (schtasks /Query exit code),
    so a disabled task, or a task still pointing at a previous install
    folder (the user moved folders mid-incident), read as [OK] while
    nothing could ever start.
These tests pin the diagnostic to the implementation's own signals:
the watchdog's STALE_MINUTES and clock, the heartbeat schema, the
setup.bat task names/targets, and the settings keys the bot writes.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import diagnose  # noqa: E402
from grt import db as dbmod  # noqa: E402
from grt.watchdog import STALE_MINUTES  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _db_with_heartbeat(tmp_path, alive_at: str | None):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    if alive_at is not None:
        db.execute(
            "INSERT OR REPLACE INTO heartbeat(id, alive_at, last_job_at,"
            " boot_at) VALUES(1, ?, ?, ?)", (alive_at, alive_at, alive_at))
        db.commit()
    return db


# ---------------------------------------------------------------- heartbeat

def test_heartbeat_fresh_is_alive(tmp_path):
    db = _db_with_heartbeat(tmp_path, _iso(NOW - timedelta(minutes=3)))
    verdict, _ = diagnose.heartbeat_status(db, now=NOW)
    assert verdict == "alive"


def test_heartbeat_at_watchdog_threshold_boundary(tmp_path):
    fresh = _db_with_heartbeat(
        tmp_path, _iso(NOW - timedelta(minutes=STALE_MINUTES - 1)))
    assert diagnose.heartbeat_status(fresh, now=NOW)[0] == "alive"
    stale = _db_with_heartbeat(
        tmp_path, _iso(NOW - timedelta(minutes=STALE_MINUTES + 5)))
    assert diagnose.heartbeat_status(stale, now=NOW)[0] == "stale"


def test_heartbeat_old_is_stale(tmp_path):
    db = _db_with_heartbeat(tmp_path, _iso(NOW - timedelta(hours=5)))
    verdict, detail = diagnose.heartbeat_status(db, now=NOW)
    assert verdict == "stale"
    assert "時間前" in detail


def test_heartbeat_no_row_is_none(tmp_path):
    db = _db_with_heartbeat(tmp_path, None)
    assert diagnose.heartbeat_status(db, now=NOW)[0] == "none"


def test_check_db_reports_liveness(tmp_path, monkeypatch):
    db_file = tmp_path / "grt.db"
    db = dbmod.connect(db_file)
    dbmod.init_schema(db)
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1', 'T')")
    db.execute(
        "INSERT INTO heartbeat(id, alive_at, last_job_at, boot_at)"
        " VALUES(1, ?, ?, ?)",
        (_iso(datetime.now(timezone.utc)),) * 3)
    db.commit()
    monkeypatch.setattr("grt.config.db_path", lambda: db_file)
    diagnose.LINES.clear()
    diagnose.check_db()
    text = "\n".join(diagnose.LINES)
    assert "ハートビート" in text
    assert "本体は起動しています" in text


# ---------------------------------------------------------------- task XML

def _task_xml(command: str, arguments: str, enabled: bool = True) -> str:
    en = "" if enabled else "<Enabled>false</Enabled>"
    # W-check B1 update: the fixture now carries the resident
    # Settings a correct registration produces (unlimited execution,
    # battery allowed). Before the W-check, diagnose did not look
    # at Settings at all; a fixture WITHOUT them now correctly
    # reads as the PT72H schema default and is flagged in §5.
    return (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        f"<Settings>{en}"
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "</Settings>"
        '<Actions Context="Author"><Exec>'
        f"<Command>{command}</Command><Arguments>{arguments}</Arguments>"
        "</Exec></Actions></Task>")


def test_parse_task_xml_enabled_and_disabled():
    ok = diagnose.parse_task_xml(_task_xml("C:\\x\\python.exe", "run.py"))
    # W-check B1 update: parse_task_xml now returns the Settings and
    # trigger facts as well (execution_time_limit etc.); the core
    # three fields keep their exact meaning.
    assert ok["enabled"] is True
    assert ok["command"] == "C:\\x\\python.exe"
    assert ok["arguments"] == "run.py"
    assert ok["execution_time_limit"] == "PT0S"
    off = diagnose.parse_task_xml(
        _task_xml("C:\\x\\python.exe", "run.py", enabled=False))
    assert off["enabled"] is False
    assert diagnose.parse_task_xml("not xml at all")["parse_error"] is True


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _run_check_tasks(monkeypatch, xml_by_name):
    def fake_run(cmd, **kwargs):
        name = cmd[cmd.index("/TN") + 1]
        return xml_by_name[name]

    monkeypatch.setattr(diagnose.subprocess, "run", fake_run)
    diagnose.LINES.clear()
    diagnose.check_tasks()
    return "\n".join(diagnose.LINES)


def test_check_tasks_ok_when_targets_match(monkeypatch):
    python_path, _, _ = diagnose.expected_task_targets()
    root = python_path.parent.parent.parent
    data_dir = root / "data"
    monkeypatch.setattr(diagnose.config, "data_dir", lambda: data_dir)
    tasks = {
        "GameRecordTool": "run.py",
        "GameRecordToolWatchdog": "watchdog.py",
        "GameRecordToolCollector": "collector/server.py",
        "GameRecordToolSiteFreshness": "site/update_site.py",
    }
    xml_by_name = {
        name: _Proc(0, _task_xml(
            str(python_path),
            f'"{root / "task_launcher.py"}" --data-dir "{data_dir}" '
            f'--script "{script}"'))
        for name, script in tasks.items()
    }
    text = _run_check_tasks(monkeypatch, xml_by_name)
    assert text.count("[OK]") == 4
    assert "[NG]" not in text


def test_check_tasks_flags_stale_folder(monkeypatch):
    xml = _task_xml("C:\\Users\\x\\OneDrive\\old\\.venv\\Scripts\\python.exe",
                    '"C:\\Users\\x\\OneDrive\\old\\run.py"')
    text = _run_check_tasks(monkeypatch, {
        "GameRecordTool": _Proc(0, xml),
        "GameRecordToolWatchdog": _Proc(1, "")})
    assert "このフォルダの本体を指していません" in text


def test_check_tasks_flags_stale_script_with_fresh_python(monkeypatch):
    # The v1.8 shim found this: correct venv python but the script
    # argument still pointing at an old folder must NOT read as [OK].
    python_path, _, _ = diagnose.expected_task_targets()
    xml = _task_xml(str(python_path), '"C:\\some\\old\\folder\\run.py"')
    xmlw = _task_xml(str(python_path), '"C:\\some\\old\\folder\\watchdog.py"')
    text = _run_check_tasks(monkeypatch, {
        "GameRecordTool": _Proc(0, xml),
        "GameRecordToolWatchdog": _Proc(0, xmlw)})
    assert "このフォルダの本体を指していません" in text
    assert "[OK]" not in text


def test_check_tasks_flags_disabled_and_missing(monkeypatch):
    python_path, _, _ = diagnose.expected_task_targets()
    xml = _task_xml(str(python_path), '"run.py"', enabled=False)
    text = _run_check_tasks(monkeypatch, {
        "GameRecordTool": _Proc(0, xml),
        "GameRecordToolWatchdog": _Proc(1, "")})
    assert "「無効」になっています" in text
    assert "登録されていません" in text


# ------------------------------------------------------- cross-source guards

def test_task_names_match_setup_bat():
    # v1.7: registration moved out of setup.bat into setup_tasks.py;
    # the task names must live there, setup.bat must delegate to it,
    # and diagnose.py must check the same names.
    setup = (ROOT / "setup.bat").read_text(encoding="utf-8")
    assert "setup_tasks.py" in setup
    tasks = (ROOT / "setup_tasks.py").read_text(encoding="utf-8")
    for name in ("GameRecordTool", "GameRecordToolWatchdog"):
        assert f'"{name}"' in tasks
        assert f'"{name}"' in Path(diagnose.__file__).read_text(
            encoding="utf-8")


def test_diagnose_settings_keys_exist_in_implementation():
    impl = ""
    for p in (ROOT / "src" / "grt").glob("*.py"):
        impl += p.read_text(encoding="utf-8")
    for key in ("allowed_chat_id", "telegram_update_offset",
                "deadline_override_quiet_selected"):
        assert key in impl


def test_poll_error_log_name_matches_main():
    main_src = (ROOT / "src" / "grt" / "main.py").read_text(encoding="utf-8")
    diag_src = Path(diagnose.__file__).read_text(encoding="utf-8")
    assert "poll_errors.log" in main_src
    assert "poll_errors.log" in diag_src


def test_check_bat_invokes_diagnose_with_venv_python():
    bat = (ROOT / "check.bat").read_text(encoding="ascii")
    assert ".venv\\Scripts\\python.exe" in bat
    assert "diagnose.py" in bat
    python_path, _, _ = diagnose.expected_task_targets()
    assert str(python_path).endswith(".venv\\Scripts\\pythonw.exe") or \
        str(python_path).endswith(".venv/Scripts/pythonw.exe")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
