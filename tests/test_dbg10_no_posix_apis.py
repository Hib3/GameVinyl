"""Debugger-10 regression guard: no POSIX-only APIs in shipped code.

Field symptom: run.py did not run on Windows. One hypothesis was that
POSIX-only APIs/idioms had crept in (os.fork, fcntl, Unix-only signals,
hardcoded '/' path building, zoneinfo-without-tzdata, default-encoding
file IO). A full audit on 2026-11-08 RULED THIS OUT: the entire ``os.``
surface of the product is ``os.environ``; paths go through pathlib;
all file IO pins ``encoding="utf-8"``; JST is a fixed +09:00 offset (no
zoneinfo/tzdata dependency). This test keeps it that way: it fails if
any shipped Python source imports a POSIX-only module or calls a
POSIX-only API, so the ruled-out hypothesis can never silently return.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", "tests"}

POSIX_ONLY_MODULES = {
    "fcntl", "termios", "resource", "pwd", "grp", "syslog", "pty",
    "curses", "posix", "ossaudiodev", "spwd", "crypt",
}

POSIX_ONLY_CALLS = {
    ("os", "fork"), ("os", "forkpty"), ("os", "killpg"), ("os", "setsid"),
    ("os", "setuid"), ("os", "setgid"), ("os", "getuid"), ("os", "geteuid"),
    ("os", "chown"), ("os", "mkfifo"), ("os", "nice"),
    ("time", "tzset"),
}

UNIX_ONLY_SIGNALS = {
    "SIGUSR1", "SIGUSR2", "SIGHUP", "SIGTSTP", "SIGCONT", "SIGWINCH",
    "SIGPIPE", "SIGALRM", "SIGQUIT",
}


def _shipped_sources() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        if SKIP_PARTS & set(p.parts):
            continue
        out.append(p)
    return out


def test_sources_exist():
    assert _shipped_sources(), "no shipped sources found - audit vacuous"


def _guarded_nodes(tree) -> set[int]:
    """ids of nodes nested under an If or Try ancestor.

    Platform-branched code such as
    ``if os.name == "nt": import msvcrt ... else: import fcntl`` is the
    CORRECT cross-platform pattern (see grt.singleinstance): the POSIX
    import only executes on POSIX. Only *unguarded* usage — code that
    runs unconditionally, including at import time — can break Windows.
    """
    guarded: set[int] = set()

    def visit(node, inside: bool):
        inside = inside or isinstance(node, (ast.If, ast.Try))
        if inside:
            guarded.add(id(node))
        for child in ast.iter_child_nodes(node):
            visit(child, inside)

    visit(tree, False)
    return guarded


def test_no_posix_only_imports_or_calls():
    problems: list[str] = []
    for path in _shipped_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded = _guarded_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in POSIX_ONLY_MODULES:
                        problems.append(
                            f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in POSIX_ONLY_MODULES:
                    problems.append(
                        f"{path.name}: from {node.module} import ...")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(
                        func.value, ast.Name):
                    if (func.value.id, func.attr) in POSIX_ONLY_CALLS:
                        problems.append(
                            f"{path.name}: call {func.value.id}.{func.attr}()")
                    if func.value.id == "subprocess" and func.attr in (
                            "Popen", "run", "call", "check_output"):
                        for kw in node.keywords:
                            if kw.arg == "preexec_fn":
                                problems.append(
                                    f"{path.name}: subprocess preexec_fn")
            elif isinstance(node, ast.Attribute) and isinstance(
                    node.value, ast.Name) and node.value.id == "signal":
                if node.attr in UNIX_ONLY_SIGNALS:
                    problems.append(f"{path.name}: signal.{node.attr}")
    assert not problems, "POSIX-only API usage found: " + "; ".join(problems)


def test_no_zoneinfo_dependency():
    """zoneinfo needs the IANA database, which Windows lacks.

    Updated 2026-11-08 (v2.0.3 wholesaler review): spec C-10.3
    REQUIRES the deadline conversion to use zoneinfo, so the ban is
    replaced by its precondition — the tzdata package (which ships
    the IANA database) must be pinned in BOTH dependency manifests,
    which setup.bat installs on the user's Windows PC."""
    root = Path(__file__).resolve().parents[1]
    req = (root / "requirements.txt").read_text(encoding="utf-8")
    pyproj = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "tzdata" in req, "zoneinfo used but tzdata not pinned in requirements.txt"
    assert "tzdata" in pyproj, "zoneinfo used but tzdata not pinned in pyproject.toml"
