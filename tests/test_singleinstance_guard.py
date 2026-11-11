"""Regression tests: single-instance guard at the run.py entry point.

Field context (debugger 13/20, watchdog/main interaction): nothing in
the system used to stop two resident run.py processes from running at
once. The logon task, a watchdog-spawned restart and a manual start
could all be alive together, each polling the same Telegram bot token
(concurrent getUpdates -> HTTP 409 contention, split delivery). The
watchdog-side restart dedup lives in test_qc_watchdog_restart_dedup;
these tests pin the entry-point half: a second run.py must refuse to
start while the first is alive, and the lock must free up once the
first process is gone (OS file lock — no stale pidfile).

All cross-process: exclusion only means anything between interpreters.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

HOLDER = (
    "import sys, time\n"
    "sys.path.insert(0, {src!r})\n"
    "from pathlib import Path\n"
    "from grt import singleinstance\n"
    "h = singleinstance.acquire(Path({lock!r}))\n"
    "print('ACQUIRED' if h is not None else 'REFUSED', flush=True)\n"
    "if h is None:\n"
    "    raise SystemExit(3)\n"
    "time.sleep(60)\n"
)


def _spawn_holder(lock: Path) -> subprocess.Popen:
    code = HOLDER.format(src=str(SRC), lock=str(lock))
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _try_acquire(lock: Path) -> int:
    code = HOLDER.format(src=str(SRC), lock=str(lock)).replace(
        "time.sleep(60)", "raise SystemExit(0)")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30)
    return proc.returncode


def test_lock_excludes_second_process_and_frees_on_death(tmp_path):
    lock = tmp_path / "main.lock"
    holder = _spawn_holder(lock)
    try:
        line = holder.stdout.readline().strip()
        assert line == "ACQUIRED"
        assert _try_acquire(lock) == 3  # refused while holder lives
    finally:
        holder.terminate()
        holder.wait(timeout=15)
    # The OS released the lock with the dead process: no cleanup, and
    # the next start is not blocked by a leftover lock file.
    assert _try_acquire(lock) == 0


def test_run_py_second_instance_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AAFakeTokenForGuardTest000000\n"
        "DISCOGS_TOKEN=fakeguardtoken00000000000000000000\n",
        encoding="utf-8")
    env = {"GRT_DATA_DIR": str(data), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "PYTHONPATH": str(SRC)}
    if os.name == "nt":
        # Winsock needs SystemRoot even for importing asyncio.
        env["SystemRoot"] = os.environ["SystemRoot"]
    first = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=env)
    try:
        # Give the first instance time to take the lock and settle.
        deadline = time.time() + 20
        while time.time() < deadline and first.poll() is None:
            if (data / "main.lock").exists():
                break
            time.sleep(0.25)
        assert first.poll() is None, "first instance died at startup"

        second = subprocess.run(
            [sys.executable, str(ROOT / "run.py")],
            capture_output=True, text=True, timeout=30, env=env)
        assert second.returncode == 0
        assert "already" in (second.stderr + second.stdout)
        log = data / "logs" / "duplicate_instance.log"
        assert log.exists() and "second instance refused" in \
            log.read_text(encoding="utf-8")
        assert first.poll() is None, "first instance must be unaffected"
    finally:
        first.terminate()
        try:
            first.wait(timeout=15)
        except subprocess.TimeoutExpired:
            first.kill()
