"""Launch a scheduled service with the shared data directory set."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
SCRIPTS = {
    "run.py",
    "watchdog.py",
    "watchdog_loop.py",
    "collector/server.py",
    "site/update_site.py",
    "site/freshness_loop.py",
}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--script", required=True, choices=sorted(SCRIPTS))
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        parser.error("--data-dir must be an absolute path")
    if os.name == "nt" and args.data_dir.drive.casefold() != ROOT.drive.casefold():
        parser.error("--data-dir must be on the install drive")
    data_dir = args.data_dir.resolve()
    os.environ["GRT_DATA_DIR"] = str(data_dir)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(data_dir.parent / "runtime" / "playwright")
    os.chdir(ROOT)
    target = ROOT / args.script
    import sys
    sys.argv = [str(target)]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
