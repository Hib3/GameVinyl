import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grt import clock
from grt import db as dbmod
from grt import migrate

_state = {"offset": 0}


def _set_offset(value: float) -> None:
    _state["offset"] = int(value)
    clock.set_local_offset(int(value))


@pytest.fixture()
def db(tmp_path):
    conn = dbmod.connect(tmp_path / "test.db")
    dbmod.init_schema(conn)
    migrate.seed_dictionaries(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def reset_clock():
    _set_offset(0)
    yield
    _set_offset(0)


def set_clock_to(hour: int, minute: int = 0, add_days: int = 0):
    """Move the test clock so clock.now() reads (current date + add_days)
    at hour:minute JST."""
    current = clock.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target += timedelta(days=add_days)
    _set_offset(_state["offset"] + (target - current).total_seconds())


def advance(seconds: float):
    _set_offset(_state["offset"] + seconds)
