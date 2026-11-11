import tempfile
from pathlib import Path


def test_python_and_pytest_temporary_files_stay_in_checkout_drive(tmp_path):
    expected = Path(__file__).resolve().parents[2] / "work" / "pytest-tmp"
    assert Path(tempfile.gettempdir()).resolve() == expected.resolve()
    assert tmp_path.resolve().is_relative_to(expected.resolve())


def test_setup_installs_use_same_drive_scratch_without_pip_cache():
    root = Path(__file__).resolve().parents[1]
    setup = (root / "setup.bat").read_text(encoding="utf-8")
    assert 'set "TEMP=%GRT_SETUP_TEMP%"' in setup
    assert 'set "TMP=%GRT_SETUP_TEMP%"' in setup
    assert "pip install --no-cache-dir" in setup
    assert "call :restore_setup_temp" in setup
