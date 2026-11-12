import shutil
import tempfile
from pathlib import Path

import pytest


_TEST_TEMP = Path(__file__).resolve().parents[2] / "GameRecordTool-pytest-tmp"


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    _TEST_TEMP.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(_TEST_TEMP)
    tempfile.tempdir = str(_TEST_TEMP)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_TEMP, ignore_errors=True)
