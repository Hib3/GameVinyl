"""v1.8: end-to-end registration-route tests against fake Windows.

setup_tasks.py's three routes (schtasks -> PowerShell -> Startup
folder) are exercised in a subprocess with stub `schtasks` and
`powershell` commands (tests/fakewin/fakewin.py). This is
the layer that could never be tested before v1.8: the route ORDER,
the verification gate (a task pointing at the wrong folder must NOT
count as success), the fallback, and the all-routes-failed exit.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKEWIN = ROOT / "tests" / "fakewin" / "fakewin.py"
SHIP_FILES = ("setup_tasks.py", "register_tasks.ps1",
              "make_startup_shortcuts.ps1", "watchdog_loop.py",
              "wait_start.py", "collector/server.py",
              "site/freshness_loop.py", "site/update_site.py")


def _stage(tmp_path: Path) -> tuple[Path, dict]:
    install = tmp_path / "install"
    install.mkdir()
    for name in SHIP_FILES:
        target = install / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, target)
    scripts = install / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("stub")
    (scripts / "pythonw.exe").write_text("stub")
    (install / "data").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("schtasks", "powershell"):
        stub = bin_dir / tool
        shutil.copy(FAKEWIN, stub)
    env = dict(os.environ)
    env["FAKEWIN_BIN"] = str(bin_dir)
    env["FAKEWIN_STATE"] = str(tmp_path / "state")
    env["APPDATA"] = str(tmp_path / "appdata")
    env["GRT_DATA_DIR"] = str(install / "data")
    env.pop("FAKEWIN_SCHTASKS_FAIL", None)
    env.pop("FAKEWIN_SCHTASKS_LIE", None)
    env.pop("FAKEWIN_PS_FAIL", None)
    env.pop("FAKEWIN_PS_REGISTER_FAIL", None)
    return install, env


# Route commands through Python explicitly. Windows cannot execute the
# extensionless shebang stubs and would otherwise hit the live scheduler.
_RUN_WITH_FAKEWIN = """
import os
from pathlib import Path
import runpy
import subprocess
import sys

run = subprocess.run

def fake_run(argv, **kwargs):
    tool = Path(os.environ["FAKEWIN_BIN"]) / argv[0]
    if argv[0] not in ("schtasks", "powershell"):
        raise AssertionError(f"Unexpected command: {argv[0]}")
    return run([sys.executable, str(tool), *argv[1:]], **kwargs)

subprocess.run = fake_run
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _register(install: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _RUN_WITH_FAKEWIN,
         str(install / "setup_tasks.py"), *args],
        capture_output=True, text=True, env=env, timeout=120)


def _mode(install: Path) -> str:
    return (install / "data" / "logs" / "autostart_mode.txt").read_text(
        encoding="utf-8").strip()


def test_route1_schtasks_success(tmp_path):
    install, env = _stage(tmp_path)
    proc = _register(install, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _mode(install) == "task-scheduler"


def test_route2_powershell_when_schtasks_fails(tmp_path):
    install, env = _stage(tmp_path)
    env["FAKEWIN_SCHTASKS_FAIL"] = "1"
    proc = _register(install, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _mode(install) == "task-scheduler"
    assert "Route 2" in proc.stdout


def test_route3_startup_folder_when_scheduler_unavailable(tmp_path):
    install, env = _stage(tmp_path)
    env["FAKEWIN_SCHTASKS_FAIL"] = "1"
    env["FAKEWIN_PS_REGISTER_FAIL"] = "1"
    proc = _register(install, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _mode(install) == "startup-folder"
    startup = (tmp_path / "appdata" / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    assert (startup / "GameRecordTool.lnk").exists()
    assert (startup / "GameRecordToolWatchdog.lnk").exists()
    assert (startup / "GameRecordToolCollector.lnk").exists()
    assert (startup / "GameRecordToolSiteFreshness.lnk").exists()


def test_wrong_folder_task_is_not_accepted(tmp_path):
    # schtasks "succeeds" but registers a task pointing at an old
    # folder; verification must reject it and route 2 must repair it.
    install, env = _stage(tmp_path)
    env["FAKEWIN_SCHTASKS_LIE"] = "1"
    proc = _register(install, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _mode(install) == "task-scheduler"
    assert "verify GameRecordTool: NG" in proc.stdout


def test_all_routes_fail_exit1_and_everything_logged(tmp_path):
    install, env = _stage(tmp_path)
    env["FAKEWIN_SCHTASKS_FAIL"] = "1"
    env["FAKEWIN_PS_FAIL"] = "1"
    proc = _register(install, env)
    assert proc.returncode == 1
    log = (install / "data" / "logs" / "setup_tasks.log").read_text(
        encoding="utf-8")
    assert "Route 1" in log and "Route 2" in log and "Route 3" in log
    assert "ALL autostart routes failed" in log
    assert not (install / "data" / "logs" / "autostart_mode.txt").exists()


def test_setup_bat_registers_before_pip_and_self_logs():
    # v1.8: registration must not depend on pip (it is stdlib-only),
    # and setup.bat must keep its own step log for diagnose == 8 ==.
    text = (ROOT / "setup.bat").read_bytes().decode("ascii")
    assert "setup_bat.log" in text
    reg = text.index("[3/7] Registering automatic start")
    pip = text.index("[4/7] Installing packages")
    boot = text.index("[7/7] Starting the bot")
    assert reg < pip < boot


def test_remove_clears_both_routes(tmp_path):
    install, env = _stage(tmp_path)
    env["FAKEWIN_SCHTASKS_FAIL"] = "1"
    env["FAKEWIN_PS_REGISTER_FAIL"] = "1"
    assert _register(install, env).returncode == 0
    proc = _register(install, env, "--remove")
    assert proc.returncode == 0
    startup = (tmp_path / "appdata" / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    assert not (startup / "GameRecordTool.lnk").exists()
    assert not (install / "data" / "logs" / "autostart_mode.txt").exists()
