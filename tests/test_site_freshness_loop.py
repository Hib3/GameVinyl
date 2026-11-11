import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('freshness_loop', ROOT / 'site' / 'freshness_loop.py')
freshness_loop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freshness_loop)


def test_fallback_runs_hourly_update_and_logs_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness_loop, 'ROOT', tmp_path)
    monkeypatch.setattr(freshness_loop, 'DATA', tmp_path / 'data')
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout='staging database required', stderr='')

    monkeypatch.setattr(freshness_loop.subprocess, 'run', run)
    assert freshness_loop.run_once() == 1
    assert Path(calls[0][0][1]).name == 'update_site.py'
    assert calls[0][1]['timeout'] == 600
    assert 'exit=1' in (tmp_path / 'data' / 'site' / 'freshness_loop.log').read_text(encoding='utf-8')
