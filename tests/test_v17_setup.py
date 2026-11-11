"""v1.7 setup rebuild: registration routes, verification, boot proof.

The v1.6 field failure was that Task Scheduler registration failed
on the user's PC in every version while setup.bat discarded the
schtasks output, so the failure was invisible and setup still
presented itself as finished. These tests pin the v1.7 behaviour:
exact /TR construction, task-XML verification parsing, the token
shape gate for the boot check, and wait_start's insistence on FRESH
boot evidence (an old startup.log line must not count).
"""
import importlib.util
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


setup_tasks = _load("setup_tasks")
wait_start = _load("wait_start")


# ------------------------------------------------------------ setup_tasks

def test_build_tr_quotes_python_and_script():
    tr = setup_tasks.build_tr("run.py")
    assert tr.startswith('"') and tr.endswith('"')
    assert "pythonw.exe" in tr and "task_launcher.py" in tr
    assert '--data-dir' in tr and '--script "run.py"' in tr
    assert '" "' in tr  # two separately quoted parts


def test_default_data_dir_uses_shared_project_home(tmp_path, monkeypatch):
    home = tmp_path / "GameRecordTool"
    release = home / "releases" / "2.0.70" / "game-record-tool"
    release.mkdir(parents=True)
    monkeypatch.setattr(setup_tasks, "ROOT", release)
    assert setup_tasks._default_data_dir() == home / "data"


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Settings><Enabled>true</Enabled></Settings>
  <Actions><Exec><Command>C:\\tool\\.venv\\Scripts\\python.exe</Command>
  <Arguments>"C:\\tool\\run.py"</Arguments></Exec></Actions>
</Task>"""


def test_parse_task_xml_enabled():
    info = setup_tasks.parse_task_xml(TASK_XML)
    assert info["enabled"] is True
    assert info["command"].endswith("python.exe")
    assert "run.py" in info["arguments"]


def test_parse_task_xml_disabled_and_garbage():
    info = setup_tasks.parse_task_xml(
        TASK_XML.replace("<Enabled>true", "<Enabled>false"))
    assert info["enabled"] is False
    assert setup_tasks.parse_task_xml("not xml")["parse_error"] is True


def test_tokens_ready_shape_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_tasks, "DATA_DIR", tmp_path)
    monkeypatch.setattr(setup_tasks, "LOG_FILE",
                        tmp_path / "logs" / "setup_tasks.log")
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=placeholder\n", encoding="utf-8")
    assert setup_tasks.tokens_ready() == 1
    env.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AA" + "x" * 30 + "\n",
        encoding="utf-8")
    assert setup_tasks.tokens_ready() == 0
    env.unlink()
    assert setup_tasks.tokens_ready() == 1


def test_new_setup_files_ship_and_ps1_is_ascii():
    for name in ("task_launcher.py", "setup_tasks.py", "register_tasks.ps1",
                 "make_startup_shortcuts.ps1", "watchdog_loop.py",
                 "wait_start.py"):
        assert (ROOT / name).exists(), name
    assert "-MultipleInstances IgnoreNew" in (ROOT / "register_tasks.ps1").read_text(encoding="ascii")
    for name in ("register_tasks.ps1", "make_startup_shortcuts.ps1"):
        data = (ROOT / name).read_bytes()
        assert all(b < 128 for b in data), name


def test_setup_bat_delegates_and_stays_ascii_crlf():
    data = (ROOT / "setup.bat").read_bytes()
    assert all(b < 128 for b in data)
    assert data.count(b"\n") == data.count(b"\r\n")  # CRLF only
    text = data.decode("ascii")
    assert "setup_tasks.py" in text
    assert "wait_start.py" in text
    code_lines = [ln for ln in text.splitlines()
                  if not ln.lstrip().upper().startswith("REM")]
    assert not any("schtasks" in ln for ln in code_lines), \
        "no inline schtasks registration may remain in setup.bat"


# ------------------------------------------------------------- wait_start

def _stamp_start_request(data_dir: Path) -> None:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "start_requested_at.txt").write_text(str(time.time()))


def test_wait_start_no_evidence_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(wait_start, "DATA_DIR", tmp_path)
    _stamp_start_request(tmp_path)
    assert wait_start.wait_for_boot(timeout=1) is None


def test_wait_start_fresh_startup_log_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(wait_start, "DATA_DIR", tmp_path)
    _stamp_start_request(tmp_path)
    now = datetime.now(JST).isoformat(timespec="seconds")
    # W-check B1 update: wait_start now requires the evidence line's
    # version to equal THIS package's VERSION (maniac A #4 — an
    # inherited old-version START line used to count as proof).
    # The fixture therefore uses the real current version; the
    # stale-version rejection is pinned in test_wcheck_b1.py.
    current = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    (tmp_path / "logs" / "startup.log").write_text(
        f"[{now}] START version={current} python=3.12 pid=1 db=x gate=open\n",
        encoding="utf-8")
    assert f"START version={current}" in wait_start.wait_for_boot(timeout=3)


def test_wait_start_stale_startup_log_does_not_count(tmp_path, monkeypatch):
    monkeypatch.setattr(wait_start, "DATA_DIR", tmp_path)
    _stamp_start_request(tmp_path)
    old = (datetime.now(JST) - timedelta(hours=5)).isoformat(
        timespec="seconds")
    (tmp_path / "logs" / "startup.log").write_text(
        f"[{old}] START version=1.6.0 python=3.12 pid=1 db=x gate=open\n",
        encoding="utf-8")
    assert wait_start.wait_for_boot(timeout=1) is None


def test_wait_start_fresh_heartbeat_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(wait_start, "DATA_DIR", tmp_path)
    _stamp_start_request(tmp_path)
    con = sqlite3.connect(tmp_path / "grt.db")
    con.execute("CREATE TABLE heartbeat(id INTEGER PRIMARY KEY, "
                "alive_at TEXT, last_job_at TEXT, boot_at TEXT)")
    now = datetime.now(JST).isoformat(timespec="seconds")
    con.execute("INSERT INTO heartbeat VALUES(1, ?, ?, ?)", (now, now, now))
    con.commit()
    con.close()
    assert "heartbeat" in wait_start.wait_for_boot(timeout=3)
