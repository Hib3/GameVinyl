import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_host_cooldown_uses_latest_actual_status():
    source=Path(__file__).resolve().parents[1]/'site'/'refresh_site_data.py'
    spec=importlib.util.spec_from_file_location('refresh_site_policy_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    c=sqlite3.connect(':memory:')
    c.executescript('''create table freshness_sources(source_id text,url text);
        create table freshness_runs(id integer primary key,source_id text,checked_at text,http_status integer);''')
    c.executemany('insert into freshness_sources values(?,?)',[
        ('old','https://shop.example/old'),('limited','https://limited.example/new'),
        ('ok','https://shop.example/current'),('expired','https://old.example/p'),
        ('failed','https://error.example/p')])
    now=datetime(2026,11,10,12,tzinfo=timezone.utc)
    c.executemany('insert into freshness_runs values(?,?,?,?)',[
        (1,'old',(now-timedelta(hours=2)).isoformat(),429),
        (2,'limited',(now-timedelta(hours=1)).isoformat(),429),
        (3,'ok',(now-timedelta(minutes=30)).isoformat(),200),
        (4,'expired',(now-timedelta(hours=7)).isoformat(),503),
        (5,'failed',(now-timedelta(minutes=5)).isoformat(),500)])
    assert mod.blocked_hosts_from_history(c,now)=={'https://limited.example'}
