#!/usr/bin/env python3
"""Fake Windows commands for integration-testing setup_tasks.py.

Copied (or symlinked) into a temp bin dir under the names `schtasks`
and `powershell`, this script emulates just enough of both tools for
setup_tasks' three registration routes to run end-to-end on Linux:

- schtasks /Create stores a task in a state dir (env FAKEWIN_STATE).
  Both creation forms are modelled: /XML (the file is really read
  and its Settings kept) and the classic /TR form (which inherits
  the Task Scheduler schema defaults — ExecutionTimeLimit PT72H and
  the battery restrictions — exactly as real Windows does).
  /Query /XML renders Task Scheduler style XML including Settings;
  /Run and /Delete behave as expected.
- powershell -File register_tasks.ps1 creates both tasks in the
  same state, with the Settings register_tasks.ps1 actually sets
  (unlimited execution, battery allowed). The script file itself is
  inspected before success is simulated: it must exist, mention
  both task names and Register-ScheduledTask. (W-check B1, maniac
  B D-1: the old shim never looked at the script at all, so a
  garbage .ps1 "registered successfully" — a tautology. The shim
  still cannot EXECUTE PowerShell; that limit is stated, and the
  real-parser check lives in the winsim suite.)
- powershell -File make_startup_shortcuts.ps1 -Mode create/remove
  makes or removes .lnk files under $APPDATA/.../Startup. The .lnk
  content records the target script path, like a real shortcut, so
  diagnose's link-target check has something real to read.

Failure injection: FAKEWIN_SCHTASKS_FAIL=1 makes schtasks /Create
fail; FAKEWIN_PS_FAIL=1 makes both PowerShell scripts fail;
FAKEWIN_SCHTASKS_LIE=1 makes /Create report success while storing a
task whose target points at a DIFFERENT folder (verification must
catch it).
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

STATE = Path(os.environ.get("FAKEWIN_STATE", "/tmp/fakewin-state"))
TASKS_FILE = STATE / "tasks.json"

# What real Windows applies when a task is created without Settings:
# the schema default ExecutionTimeLimit is PT72H and starting on
# battery is disallowed / stopped on unplug (Microsoft docs).
CLASSIC_DEFAULT_SETTINGS = {
    "execution_time_limit": "PT72H",
    "disallow_on_batteries": "true",
    "stop_on_batteries": "true",
}
# What register_tasks.ps1 sets explicitly.
PS_SETTINGS = {
    "execution_time_limit": "PT0S",
    "disallow_on_batteries": "false",
    "stop_on_batteries": "false",
}


def _load() -> dict:
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(tasks: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks), encoding="utf-8")


def _split_tr(tr: str) -> tuple[str, str]:
    parts, cur, in_q = [], "", False
    for ch in tr:
        if ch == '"':
            in_q = not in_q
            continue
        if ch == " " and not in_q:
            if cur:
                parts.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        parts.append(cur)
    cmd = parts[0] if parts else ""
    arg = f'"{parts[1]}"' if len(parts) > 1 else ""
    return cmd, arg


def _parse_task_xml_file(path: str) -> dict | None:
    """Extract command/arguments/settings from a /Create /XML file."""
    try:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe",
                                                   b"\xfe\xff") \
            else raw.decode("utf-8", errors="replace")
        root = ET.fromstring(text)
    except Exception:
        return None
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[: root.tag.index("}") + 1]

    def _text(parent_path: str, tag: str) -> str:
        el = root.find(f"{parent_path}/{ns}{tag}")
        return (el.text or "").strip() if el is not None else ""

    settings = {
        "execution_time_limit":
            _text(f".//{ns}Settings", "ExecutionTimeLimit") or "PT72H",
        "disallow_on_batteries":
            (_text(f".//{ns}Settings", "DisallowStartIfOnBatteries")
             or "true").lower(),
        "stop_on_batteries":
            (_text(f".//{ns}Settings", "StopIfGoingOnBatteries")
             or "true").lower(),
        "trigger_interval": next(iter([
            el.text.strip().upper() for el in root.findall(
                f".//{ns}Triggers/*/{ns}Repetition/{ns}Interval") if el.text]), None),
    }
    return {"command": _text(f".//{ns}Actions/{ns}Exec", "Command"),
            "arguments": _text(f".//{ns}Actions/{ns}Exec", "Arguments"),
            "settings": settings}


def _store_task(tasks: dict, name: str, command: str, arguments: str,
                settings: dict) -> None:
    if os.environ.get("FAKEWIN_SCHTASKS_LIE") == "1":
        script = ("watchdog.py" if "Watchdog" in name else
                  "collector/server.py" if "Collector" in name else "run.py")
        arguments = f'"C:\\some\\old\\folder\\{script}"'
    tasks[name] = {"command": command, "arguments": arguments,
                   "settings": settings}


def schtasks(argv: list[str]) -> int:
    tasks = _load()
    low = [a.lower() for a in argv]
    op = low[0] if low else ""
    name = ""
    if "/tn" in low:
        name = argv[low.index("/tn") + 1]
    if op == "/create":
        if os.environ.get("FAKEWIN_SCHTASKS_FAIL") == "1":
            print("ERROR: Access is denied.")
            return 1
        if "/xml" in low:
            parsed = _parse_task_xml_file(argv[low.index("/xml") + 1])
            if parsed is None:
                print("ERROR: The task XML is invalid.")
                return 1
            _store_task(tasks, name, parsed["command"],
                        parsed["arguments"], parsed["settings"])
        else:
            tr = argv[low.index("/tr") + 1] if "/tr" in low else ""
            cmd, arg = _split_tr(tr)
            _store_task(tasks, name, cmd, arg,
                        dict(CLASSIC_DEFAULT_SETTINGS))
        _save(tasks)
        print(f'SUCCESS: The scheduled task "{name}" has successfully '
              "been created.")
        return 0
    if op == "/query":
        if name not in tasks:
            print("ERROR: The system cannot find the file specified.")
            return 1
        if "/xml" in low:
            task = tasks[name]
            st = task.get("settings", CLASSIC_DEFAULT_SETTINGS)
            print('<?xml version="1.0" encoding="UTF-16"?>')
            print('<Task xmlns="http://schemas.microsoft.com/windows/'
                  '2004/02/mit/task">')
            interval = st.get("trigger_interval")
            if interval:
                print("  <Triggers><TimeTrigger><Enabled>true</Enabled>"
                      f"<Repetition><Interval>{interval}</Interval></Repetition>"
                      "</TimeTrigger></Triggers>")
            else:
                print("  <Triggers><LogonTrigger><Enabled>true</Enabled>"
                      "</LogonTrigger></Triggers>")
            print("  <Settings><Enabled>true</Enabled>"
                  f"<ExecutionTimeLimit>{st['execution_time_limit']}"
                  "</ExecutionTimeLimit>"
                  "<DisallowStartIfOnBatteries>"
                  f"{st['disallow_on_batteries']}"
                  "</DisallowStartIfOnBatteries>"
                  "<StopIfGoingOnBatteries>"
                  f"{st['stop_on_batteries']}</StopIfGoingOnBatteries>"
                  "<StartWhenAvailable>true</StartWhenAvailable>"
                  "</Settings>")
            print(f"  <Actions><Exec><Command>{task['command']}"
                  "</Command>"
                  f"<Arguments>{task['arguments']}</Arguments>"
                  "</Exec></Actions>")
            print("</Task>")
        return 0
    if op == "/run":
        return 0 if name in tasks else 1
    if op == "/delete":
        tasks.pop(name, None)
        _save(tasks)
        return 0
    return 0


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA", "/tmp/fakewin-appdata")
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup")


def _script_path(argv: list[str]) -> Path | None:
    if "-File" in argv:
        return Path(argv[argv.index("-File") + 1])
    return None


def powershell(argv: list[str]) -> int:
    script_path = _script_path(argv)
    script = script_path.name if script_path else ""
    if os.environ.get("FAKEWIN_PS_FAIL") == "1":
        print("PS_ERROR: simulated PowerShell failure")
        return 1
    if (script == "register_tasks.ps1"
            and os.environ.get("FAKEWIN_PS_REGISTER_FAIL") == "1"):
        print("PS_ERROR: simulated Register-ScheduledTask failure")
        return 1
    root = ""
    if "-Root" in argv:
        root = argv[argv.index("-Root") + 1]
    data_dir = ""
    if "-DataDir" in argv:
        data_dir = argv[argv.index("-DataDir") + 1]
    mode = "create"
    if "-Mode" in argv:
        mode = argv[argv.index("-Mode") + 1]
    if script == "register_tasks.ps1":
        # W-check B1 (maniac B D-1): do not simulate success for a
        # script that could not possibly register anything. The
        # real script must exist and visibly contain the two task
        # registrations. (Syntax-level truth comes from the winsim
        # suite's real PowerShell parser check.)
        try:
            content = script_path.read_text(encoding="utf-8",
                                            errors="replace")
        except Exception:
            print("PS_ERROR: script file unreadable")
            return 1
        if ("Register-ScheduledTask" not in content
                or "GameRecordTool" not in content
                or "GameRecordToolWatchdog" not in content
                or "GameRecordToolCollector" not in content
                or "GameRecordToolSiteFreshness" not in content):
            print("PS_ERROR: script does not contain the expected "
                  "registrations (simulated parse/execution failure)")
            return 1
        tasks = _load()
        py = root + "\\.venv\\Scripts\\pythonw.exe"
        for task_name, script_name in (
                ("GameRecordTool", "run.py"),
                ("GameRecordToolWatchdog", "watchdog.py"),
                ("GameRecordToolCollector", "collector/server.py"),
                ("GameRecordToolSiteFreshness", "site/update_site.py")):
            interval = {"GameRecordToolWatchdog": "PT15M",
                        "GameRecordToolSiteFreshness": "PT1H"}.get(task_name)
            tasks[task_name] = {
                "command": py,
                "arguments": (f'"{root}\\task_launcher.py" --data-dir '
                              f'"{data_dir}" --script "{script_name}"'),
                "settings": dict(PS_SETTINGS, trigger_interval=interval)}
        _save(tasks)
        print("MAIN_TASK_REGISTERED")
        print("WATCHDOG_TASK_REGISTERED")
        print("COLLECTOR_TASK_REGISTERED")
        print("SITE_FRESHNESS_TASK_REGISTERED")
        return 0
    if script == "make_startup_shortcuts.ps1":
        folder = _startup_dir()
        if mode == "create":
            folder.mkdir(parents=True, exist_ok=True)
            # A real .lnk carries its target; write it readably so
            # diagnose's target verification has real input.
            (folder / "GameRecordTool.lnk").write_text(
                f'lnk-target:"{root}\\task_launcher.py" --data-dir "{data_dir}" '
                '--script "run.py"', encoding="utf-16")
            (folder / "GameRecordToolWatchdog.lnk").write_text(
                f'lnk-target:"{root}\\task_launcher.py" --data-dir "{data_dir}" '
                '--script "watchdog_loop.py"',
                encoding="utf-16")
            (folder / "GameRecordToolCollector.lnk").write_text(
                f'lnk-target:"{root}\\task_launcher.py" --data-dir "{data_dir}" '
                '--script "collector/server.py"', encoding="utf-16")
            (folder / "GameRecordToolSiteFreshness.lnk").write_text(
                f'lnk-target:"{root}\\task_launcher.py" --data-dir "{data_dir}" '
                '--script "site/freshness_loop.py"', encoding="utf-16")
            print("MAIN_SHORTCUT_CREATED")
            print("WATCHDOG_SHORTCUT_CREATED")
        else:
            for n in ("GameRecordTool", "GameRecordToolWatchdog",
                      "GameRecordToolCollector", "GameRecordToolSiteFreshness"):
                p = folder / f"{n}.lnk"
                if p.exists():
                    p.unlink()
            print("SHORTCUTS_REMOVED")
        return 0
    print("PS_ERROR: unknown script")
    return 1


if __name__ == "__main__":
    prog = Path(sys.argv[0]).name
    if prog.startswith("schtasks"):
        raise SystemExit(schtasks(sys.argv[1:]))
    raise SystemExit(powershell(sys.argv[1:]))
