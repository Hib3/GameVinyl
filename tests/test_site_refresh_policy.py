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


def test_blocked_directory_preserves_cooldown_reason(tmp_path, monkeypatch):
    source=Path(__file__).resolve().parents[1]/'site'/'refresh_site_data.py'
    spec=importlib.util.spec_from_file_location('refresh_site_policy_blocked_test',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    db=tmp_path/'site.sqlite3'
    c=sqlite3.connect(db)
    c.executescript('''
        create view site_catalog as select 'DATA001' as catalog,'Data Discs' as label;
        create table freshness_sources(source_id text primary key,kind text,url text,
          catalog_group text,terms_url text,use_scope text);
        create table freshness_runs(id integer primary key,source_id text,checked_at text,
          result text,http_status integer,extracted integer,price real,currency text,availability text);
        insert into freshness_sources values('data-discs-streets-ajax','product_api',
          'https://data-discs.com/products/streets-of-rage','DATA001','https://data-discs.com/pages/terms','private_ajax_catalog_group');
    ''')
    c.execute("insert into freshness_runs(source_id,checked_at,result,http_status) values(?,?,?,429)",
              ('data-discs-streets-ajax',
               (datetime.now().astimezone()-timedelta(hours=1)).isoformat(timespec='seconds'),
               'http_429'))
    c.commit();c.close()

    class NoNetwork:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args, **kwargs): raise AssertionError('cooldown must prevent requests')
        def get(self, *args, **kwargs): raise AssertionError('cooldown must prevent requests')

    monkeypatch.setattr(mod.httpx,'Client',NoNetwork)
    assert mod.run(db,1,True) is False
    c=sqlite3.connect(db)
    result=c.execute("select result from freshness_runs where source_id='data-discs-directory' order by id desc limit 1").fetchone()[0]
    c.close()
    assert result=='host_temporarily_blocked'
