import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_launcher", ROOT / "task_launcher.py")
task_launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_launcher)


def test_launcher_sets_shared_data_dir_and_target(tmp_path, monkeypatch):
    install = tmp_path / "release" / "game-record-tool"
    install.mkdir(parents=True)
    data = tmp_path / "shared-data"
    calls = []
    monkeypatch.setattr(task_launcher, "ROOT", install)
    monkeypatch.setattr(task_launcher.runpy, "run_path",
                        lambda path, run_name: calls.append((path, run_name)))
    monkeypatch.chdir(tmp_path)

    task_launcher.main(["--data-dir", str(data), "--script", "run.py"])

    assert task_launcher.os.environ["GRT_DATA_DIR"] == str(data.resolve())
    assert task_launcher.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "runtime" / "playwright")
    assert calls == [(str(install / "run.py"), "__main__")]


def test_launcher_rejects_non_absolute_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(task_launcher, "ROOT", tmp_path)
    try:
        task_launcher.main(["--data-dir", "relative", "--script", "run.py"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("relative data directory was accepted")
