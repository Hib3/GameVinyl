#!/usr/bin/env python3
"""Automatic-start registration for the Game Record Tool (Windows).

Why this module exists: through v1.6 the Task Scheduler registration
lived inside setup.bat, where the schtasks output was discarded
(>nul 2>nul) and the batch-level quoting of the /TR argument was
fragile. On the user's PC the registration failed in every version
while setup still presented itself as essentially finished, so the
bot simply never started at logon and nobody could see WHY.

This module replaces that step with three mechanisms, tried in
order, each VERIFIED before it is accepted:

  1. schtasks /Create (the classic route), driven from Python so the
     argument list is exact — no batch quoting in between.
  2. PowerShell Register-ScheduledTask (register_tasks.ps1), which
     uses the modern CIM API and reports real error messages.
  3. Startup-folder shortcuts (make_startup_shortcuts.ps1): needs no
     Task Scheduler and no administrator rights at all. The main
     process starts at logon via pythonw; the watchdog runs as a
     15-minute in-process loop (watchdog_loop.py).

Every command's output is kept: it is printed AND appended to
data/logs/setup_tasks.log, so a failure is never silent again.
A mechanism counts as success only after verification:
  - task route: `schtasks /Query /XML` shows all four tasks present,
    enabled, and pointing at THIS folder's venv python + scripts;
  - startup route: all four .lnk files exist in the Startup folder.
The winning mechanism is recorded in data/logs/autostart_mode.txt
("task-scheduler" or "startup-folder"); diagnose.py reads it too.

Usage:
  python setup_tasks.py            register + verify (used by setup.bat)
  python setup_tasks.py --start    start the main process NOW through
                                   the recorded mechanism
  python setup_tasks.py --remove   remove tasks AND shortcuts

Stdlib only. Safe to run repeatedly.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _default_data_dir() -> Path:
    # Releases and the active app share the project root's single data folder.
    for parent in (ROOT, *ROOT.parents):
        if (parent / "backups").is_dir() and (parent / "releases").is_dir():
            return parent / "data"
    return ROOT.parent / "data"


DATA_DIR = Path(os.environ.get("GRT_DATA_DIR") or _default_data_dir())
LOG_FILE = DATA_DIR / "logs" / "setup_tasks.log"
MODE_FILE = DATA_DIR / "logs" / "autostart_mode.txt"
START_REQUEST_FILE = DATA_DIR / "logs" / "start_requested_at.txt"

MAIN_TASK = "GameRecordTool"
WATCHDOG_TASK = "GameRecordToolWatchdog"
COLLECTOR_TASK = "GameRecordToolCollector"
SITE_TASK = "GameRecordToolSiteFreshness"
TASK_SCRIPTS = ((MAIN_TASK, "run.py"), (WATCHDOG_TASK, "watchdog.py"),
                (COLLECTOR_TASK, "collector/server.py"),
                (SITE_TASK, "site/update_site.py"))
SHORTCUT_SCRIPTS = ((MAIN_TASK, "run.py"), (WATCHDOG_TASK, "watchdog_loop.py"),
                    (COLLECTOR_TASK, "collector/server.py"),
                    (SITE_TASK, "site/freshness_loop.py"))


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{_stamp()}] {msg}"
    _LOG_LINES.append(line)
    try:
        print(msg, flush=True)
    except Exception:
        pass


def flush_log() -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("\n".join(_LOG_LINES) + "\n")
    except Exception:
        pass
    _LOG_LINES.clear()


def run_cmd(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a command, capturing combined output. Never raises."""
    log("$ " + " ".join(f'"{a}"' if " " in a else a for a in argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        for ln in out.splitlines():
            log("  | " + ln)
        log(f"  -> exit {proc.returncode}")
        return proc.returncode, out
    except Exception as exc:
        log(f"  -> EXCEPTION {exc!r}")
        return -1, repr(exc)


# ---------------------------------------------------------------- schtasks

def build_tr(script_name: str) -> str:
    """Build a pythonw action that pins the shared data path."""
    return (f'"{VENV_PYW}" "{ROOT / "task_launcher.py"}" '
            f'--data-dir "{DATA_DIR}" --script "{script_name}"')


def task_arguments(script_name: str) -> str:
    return (f'"{ROOT / "task_launcher.py"}" '
            f'--data-dir "{DATA_DIR}" --script "{script_name}"')


def build_argv(script_name: str) -> list[str]:
    return [str(VENV_PYW), str(ROOT / "task_launcher.py"),
            "--data-dir", str(DATA_DIR), "--script", script_name]


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def task_xml(name: str, script: str) -> str:
    """A complete Task Scheduler XML definition for one task.

    W-check B1 (wholesaler #1): the plain schtasks command line
    cannot set Settings, so Route 1 tasks silently inherited the
    schema defaults — ExecutionTimeLimit PT72H (the resident
    process is KILLED after 72 hours) and the battery restrictions
    (no start on battery / stop when unplugged). The PowerShell
    route set these correctly all along; the two routes did not
    mean the same thing. Registering from XML gives Route 1 the
    same Settings as register_tasks.ps1, and verify_tasks() now
    checks them, so a route that yields the defaults is rejected
    and the next route is tried instead of trusted.
    """
    if name in (MAIN_TASK, COLLECTOR_TASK):
        trigger = ("<LogonTrigger><Enabled>true</Enabled>"
                   "</LogonTrigger>")
    elif name == SITE_TASK:
        first_run = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        trigger = (f"<TimeTrigger><StartBoundary>{first_run}"
                   "</StartBoundary><Enabled>true</Enabled>"
                   "<Repetition><Interval>PT1H</Interval>"
                   "<StopAtDurationEnd>false</StopAtDurationEnd>"
                   "</Repetition></TimeTrigger>")
    else:
        trigger = ("<TimeTrigger><StartBoundary>2020-01-01T00:00:00"
                   "</StartBoundary><Enabled>true</Enabled>"
                   "<Repetition><Interval>PT15M</Interval>"
                   "<StopAtDurationEnd>false</StopAtDurationEnd>"
                   "</Repetition></TimeTrigger>")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<RegistrationInfo><Author>GameRecordTool</Author>"
        "</RegistrationInfo>"
        f"<Triggers>{trigger}</Triggers>"
        '<Principals><Principal id="Author">'
        "<LogonType>InteractiveToken</LogonType>"
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>"
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false"
        "</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<AllowHardTerminate>true</AllowHardTerminate>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<RunOnlyIfNetworkAvailable>false"
        "</RunOnlyIfNetworkAvailable>"
        "<Enabled>true</Enabled>"
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"
        "<Priority>7</Priority>"
        "</Settings>"
        '<Actions Context="Author"><Exec>'
        f"<Command>{_xml_escape(str(VENV_PYW))}</Command>"
         f"<Arguments>{_xml_escape(task_arguments(script))}"
        "</Arguments>"
        f"<WorkingDirectory>{_xml_escape(str(ROOT))}"
        "</WorkingDirectory>"
        "</Exec></Actions></Task>")


def try_schtasks() -> bool:
    log("== Route 1: schtasks /Create /XML ==")
    ok = True
    for name, script in TASK_SCRIPTS:
        xml_path = DATA_DIR / "logs" / f"task_{name}.xml"
        try:
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text(task_xml(name, script),
                                encoding="utf-16")
        except Exception as exc:
            log(f"route1: cannot write task XML for {name}: {exc!r}")
            return False
        rc, _ = run_cmd(["schtasks", "/Create", "/TN", name,
                         "/XML", str(xml_path), "/F"])
        if rc != 0:
            # Fall back to the classic command line for this task.
            # Its Settings will be the schema defaults, which
            # verify_tasks() now rejects — the run then moves on to
            # the PowerShell route instead of trusting a task that
            # stops after 72 hours.
            log(f"route1: /XML create failed for {name}; trying the "
                "classic /TR form (verification will judge it).")
            args = ["schtasks", "/Create", "/TN", name,
                    "/TR", build_tr(script), "/F"]
            if name == MAIN_TASK:
                args += ["/SC", "ONLOGON"]
            elif name == SITE_TASK:
                args += ["/SC", "HOURLY", "/MO", "1"]
            else:
                args += ["/SC", "MINUTE", "/MO", "15"]
            rc, _ = run_cmd(args)
            if rc != 0:
                ok = False
    return ok


# ------------------------------------------------------- task verification

def _settings_flag(root, ns: str, tag: str) -> str | None:
    el = root.find(f".//{ns}Settings/{ns}{tag}")
    if el is None or el.text is None:
        return None
    return el.text.strip().lower()


def parse_task_xml(xml_text: str) -> dict:
    """Task facts from `schtasks /Query /XML` output.

    W-check B1: verification used to read only Settings/Enabled and
    the action target. It now also reads the trigger-level Enabled
    flags (maniac B D-2: a task whose LogonTrigger is disabled
    never fires yet used to pass as [OK]) and the Settings that
    decide whether a resident process survives (wholesaler #1:
    ExecutionTimeLimit defaults to PT72H; battery flags default to
    restricting). A missing ExecutionTimeLimit element means the
    schema default PT72H applies, so it is reported as 'PT72H'.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {"parse_error": True}
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[: root.tag.index("}") + 1]
    enabled = True
    el = root.find(f".//{ns}Settings/{ns}Enabled")
    if el is not None and (el.text or "").strip().lower() == "false":
        enabled = False
    triggers_enabled = True
    for trig in root.findall(f".//{ns}Triggers/*"):
        en = trig.find(f"{ns}Enabled")
        if en is not None and (en.text or "").strip().lower() == "false":
            triggers_enabled = False
    limit_el = root.find(f".//{ns}Settings/{ns}ExecutionTimeLimit")
    limit = ((limit_el.text or "").strip().upper()
             if limit_el is not None else "PT72H")
    command = arguments = ""
    exec_el = root.find(f".//{ns}Actions/{ns}Exec")
    if exec_el is not None:
        cmd_el = exec_el.find(f"{ns}Command")
        arg_el = exec_el.find(f"{ns}Arguments")
        command = (cmd_el.text or "") if cmd_el is not None else ""
        arguments = (arg_el.text or "") if arg_el is not None else ""
    intervals = [item.text.strip().upper() for item in root.findall(
        f".//{ns}Triggers/*/{ns}Repetition/{ns}Interval") if item.text]
    return {"enabled": enabled and triggers_enabled,
            "settings_enabled": enabled,
            "triggers_enabled": triggers_enabled,
            "command": command, "arguments": arguments,
            "execution_time_limit": limit,
            "disallow_on_batteries":
                _settings_flag(root, ns, "DisallowStartIfOnBatteries"),
            "stop_on_batteries":
                _settings_flag(root, ns, "StopIfGoingOnBatteries"),
            "start_when_available":
                _settings_flag(root, ns, "StartWhenAvailable"),
            "trigger_intervals": intervals}


_ZERO_LIMITS = {"PT0S", "PT0M", "PT0H", "P0D", "P0DT0H0M0S", ""}


def settings_problems(info: dict) -> list[str]:
    """Why this task's Settings are wrong for a resident process."""
    problems = []
    if info.get("execution_time_limit", "PT72H") not in _ZERO_LIMITS:
        problems.append(
            f"ExecutionTimeLimit={info.get('execution_time_limit')} "
            "(the process would be stopped when the limit hits; "
            "a resident process needs PT0S = unlimited)")
    if info.get("disallow_on_batteries") == "true":
        problems.append("DisallowStartIfOnBatteries=true "
                        "(no start while on battery)")
    if info.get("stop_on_batteries") == "true":
        problems.append("StopIfGoingOnBatteries=true "
                        "(stopped when the PC is unplugged)")
    return problems


def _norm(p: str) -> str:
    return p.strip().strip('"').replace("/", "\\").lower()


def query_task(name: str) -> dict | None:
    """Verified info about one task, or None when it cannot be read."""
    try:
        proc = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"],
                              capture_output=True, text=True,
                              errors="replace", timeout=20)
    except Exception as exc:
        log(f"query {name}: EXCEPTION {exc!r}")
        return None
    if proc.returncode != 0:
        log(f"query {name}: not registered (exit {proc.returncode})")
        return None
    info = parse_task_xml(proc.stdout or "")
    if info.get("parse_error"):
        log(f"query {name}: XML parse failed")
        return None
    return info


def verify_tasks() -> bool:
    """All resident tasks exist, are enabled, and point at THIS folder."""
    all_ok = True
    for name, script in TASK_SCRIPTS:
        info = query_task(name)
        if info is None:
            all_ok = False
            continue
        cmd_ok = _norm(info["command"]) == _norm(str(VENV_PYW))
        arg_ok = (_norm(str(ROOT / "task_launcher.py")) in _norm(info["arguments"])
                  and _norm(str(DATA_DIR)) in _norm(info["arguments"])
                  and f'--script "{script}"' in info["arguments"])
        problems = settings_problems(info)
        if name == SITE_TASK and "PT1H" not in info.get("trigger_intervals", []):
            problems.append("site freshness trigger must repeat every PT1H")
        if info["enabled"] and cmd_ok and arg_ok and not problems:
            log(f"verify {name}: OK (enabled, target = this folder, "
                "settings fit a resident process)")
        else:
            log(f"verify {name}: NG enabled={info['enabled']} "
                f"command={info['command']!r} arguments={info['arguments']!r} "
                f"settings_problems={problems}")
            all_ok = False
    return all_ok


# -------------------------------------------------------------- powershell

def _run_ps_script(script_name: str, extra_args: list[str]) -> int:
    script = ROOT / script_name
    if not script.exists():
        log(f"powershell route: {script_name} is missing from the package")
        return -1
    rc, _ = run_cmd(["powershell", "-NoProfile", "-ExecutionPolicy",
                     "Bypass", "-File", str(script), *extra_args],
                    timeout=120)
    return rc


def try_powershell() -> bool:
    log("== Route 2: PowerShell Register-ScheduledTask ==")
    return _run_ps_script("register_tasks.ps1",
                          ["-Root", str(ROOT), "-DataDir", str(DATA_DIR)]) == 0


# -------------------------------------------------------- startup fallback

def startup_folder() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup")


def shortcut_paths() -> list[Path]:
    folder = startup_folder()
    return [folder / f"{name}.lnk" for name, _ in SHORTCUT_SCRIPTS]


def try_startup_fallback() -> bool:
    log("== Route 3: Startup-folder shortcuts ==")
    rc = _run_ps_script("make_startup_shortcuts.ps1",
                        ["-Root", str(ROOT), "-DataDir", str(DATA_DIR),
                         "-Mode", "create"])
    if rc != 0:
        return False
    ok = True
    for path in shortcut_paths():
        exists = path.exists()
        log(f"verify shortcut {path}: {'OK' if exists else 'NG (missing)'}")
        ok = ok and exists
    return ok


def remove_startup_shortcuts() -> None:
    _run_ps_script("make_startup_shortcuts.ps1",
                   ["-Root", str(ROOT), "-DataDir", str(DATA_DIR),
                    "-Mode", "remove"])
    for path in shortcut_paths():
        try:
            if path.exists():
                path.unlink()
                log(f"removed shortcut {path}")
        except Exception as exc:
            log(f"remove shortcut {path}: {exc!r}")


def remove_tasks() -> None:
    for name, _ in TASK_SCRIPTS:
        run_cmd(["schtasks", "/Delete", "/TN", name, "/F"])


# ------------------------------------------------------------------- modes

def record_mode(mode: str) -> None:
    try:
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text(mode + "\n", encoding="utf-8")
    except Exception as exc:
        log(f"record_mode: {exc!r}")
    log(f"AUTOSTART_MODE={mode}")


def read_mode() -> str | None:
    try:
        text = MODE_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def register() -> int:
    log("=== setup_tasks: registration start ===")
    log(f"install folder: {ROOT}")
    if not VENV_PYW.exists():
        log(f"NG: venv python not found at {VENV_PYW} "
            "(setup step [2/7] must succeed first)")
        flush_log()
        return 1

    if try_schtasks() and verify_tasks():
        remove_startup_shortcuts()
        record_mode("task-scheduler")
        log("RESULT: task-scheduler registration verified.")
        flush_log()
        return 0

    log("schtasks route did not verify; trying PowerShell route.")
    if try_powershell() and verify_tasks():
        remove_startup_shortcuts()
        record_mode("task-scheduler")
        log("RESULT: task-scheduler registration verified (PowerShell).")
        flush_log()
        return 0

    log("Task Scheduler routes did not verify; "
        "falling back to Startup-folder shortcuts.")
    if try_startup_fallback():
        record_mode("startup-folder")
        log("RESULT: Startup-folder autostart verified.")
        flush_log()
        return 0

    log("RESULT: ALL autostart routes failed. See the log lines above "
        "for the exact errors.")
    flush_log()
    return 1


def start_now() -> int:
    """Start the main process NOW through the recorded mechanism."""
    log("=== setup_tasks: start now ===")
    try:
        START_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        START_REQUEST_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception as exc:
        # W-check B1 (maniac A #4): the stamp is the ONLY thing that
        # makes wait_start's evidence "fresh". If it cannot be
        # written, wait_start would fall back to judging against an
        # inherited stamp and could accept an OLD boot record as
        # proof. Fail the start step honestly instead.
        log(f"start-request stamp failed: {exc!r}")
        log("NG: cannot stamp the start request; aborting the start "
            "step so a stale boot record is never accepted as proof.")
        flush_log()
        return 1
    mode = read_mode()
    log(f"recorded autostart mode: {mode}")
    if mode == "task-scheduler":
        main_rc, _ = run_cmd(["schtasks", "/Run", "/TN", MAIN_TASK])
        collector_rc, _ = run_cmd(["schtasks", "/Run", "/TN", COLLECTOR_TASK])
        # Site freshness is hourly; starting it here would duplicate the
        # next scheduled run when setup finishes near the hour boundary.
        site_rc = 0
        if main_rc == 0 and collector_rc == 0:
            flush_log()
            return 0
        log("schtasks /Run failed for a resident task; launching missing processes directly.")
    # Direct detached launch (startup-folder mode, or task-run fallback).
    python = VENV_PYW
    if not python.exists():
        log(f"NG: no python at {python}")
        flush_log()
        return 1
    try:
        flags = 0
        if os.name == "nt":
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        if mode != "task-scheduler" or main_rc != 0:
            subprocess.Popen(build_argv("run.py"), cwd=str(ROOT),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags,
                             close_fds=True)
            log(f"launched: {python} task_launcher.py run.py (detached)")
        if mode != "task-scheduler" or collector_rc != 0:
            subprocess.Popen(build_argv("collector/server.py"),
                             cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            log(f"launched: {python} task_launcher.py collector/server.py (detached)")
        if mode != "task-scheduler" or site_rc != 0:
            subprocess.Popen(build_argv("site/freshness_loop.py"),
                             cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            log(f"launched: {python} task_launcher.py site/freshness_loop.py (detached)")
        if mode == "startup-folder":
            subprocess.Popen(build_argv("watchdog_loop.py"),
                             cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            log(f"launched: {python} task_launcher.py watchdog_loop.py (detached)")
        flush_log()
        return 0
    except Exception as exc:
        log(f"launch failed: {exc!r}")
        flush_log()
        return 1


def remove_all() -> int:
    log("=== setup_tasks: remove all autostart ===")
    remove_tasks()
    remove_startup_shortcuts()
    try:
        if MODE_FILE.exists():
            MODE_FILE.unlink()
    except Exception:
        pass
    log("removed.")
    flush_log()
    return 0


def _read_env_text(path: Path) -> str:
    """Decode a user-edited .env exactly like grt.config does
    (W-check B1, adversarial QA D-1/D-2): UTF-16 BOM -> utf-16,
    UTF-8 BOM -> utf-8-sig, strict UTF-8, cp932 fallback, never a
    raw UnicodeDecodeError and never a BOM glued to the first key.
    Duplicated here because this module must stay stdlib-only."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    head = raw[:200]
    if head.count(0) >= 8:
        odd_nuls = sum(1 for i in range(1, len(head), 2)
                       if head[i] == 0)
        even_nuls = sum(1 for i in range(0, len(head), 2)
                        if head[i] == 0)
        if odd_nuls > even_nuls:
            return raw.decode("utf-16-le", errors="replace")
        if even_nuls > odd_nuls:
            return raw.decode("utf-16-be", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _env_value(lines: list[str], key: str) -> str | None:
    for line in lines:
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith(key + "="):
            return stripped.split("=", 1)[1].strip()
    return None


def tokens_ready() -> int:
    """Exit 0 when data/.env holds a plausible Telegram bot token.

    Used by setup.bat step [7/7]: on a first install the tokens are
    usually not pasted yet, so the boot check must be skipped with a
    clear message instead of timing out and crying failure. The
    token VALUE is never printed or logged — only its shape is read.

    W-check B1 (seller #2): the Discogs token's state is reported
    too. Setup used to declare completion while DISCOGS_TOKEN was
    still the template placeholder, leaving the user to discover
    the dead Discogs lane a day later. The exit code still turns on
    the Telegram token (the bot cannot run without it); a missing
    Discogs token is a loud warning, not silence.
    """
    env_path = DATA_DIR / ".env"
    try:
        lines = _read_env_text(env_path).splitlines()
    except Exception as exc:
        log(f"tokens: could not read {env_path}: {exc!r}")
        flush_log()
        return 1
    telegram = _env_value(lines, "TELEGRAM_BOT_TOKEN")
    discogs = _env_value(lines, "DISCOGS_TOKEN")
    if discogs and len(discogs) >= 20 and "ここに" not in discogs:
        log("tokens: DISCOGS_TOKEN looks set (shape only).")
    else:
        log("tokens: WARNING - DISCOGS_TOKEN is not set yet. The bot "
            "will start, but Discogs matching stays OFF until it is "
            "pasted into data/.env (SETUP_GUIDE section 5).")
    if telegram is None:
        log("tokens: TELEGRAM_BOT_TOKEN line not found in data/.env.")
        flush_log()
        return 1
    if len(telegram) >= 30 and ":" in telegram:
        log("tokens: TELEGRAM_BOT_TOKEN looks set (shape only).")
        flush_log()
        return 0
    log("tokens: TELEGRAM_BOT_TOKEN is not set yet.")
    flush_log()
    return 1


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    if "--start" in argv:
        return start_now()
    if "--remove" in argv:
        return remove_all()
    if "--check-tokens" in argv:
        return tokens_ready()
    return register()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
