"""Scheduling of site catalog retries, exercised without network."""
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx

spec = importlib.util.spec_from_file_location(
    "site_catalog_search", Path(__file__).resolve().parents[1] / "site" / "catalog_search.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PACKAGE_VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text(encoding='utf-8').strip()

def test_retry_due_and_503_stops(tmp_path, monkeypatch):
    path = tmp_path / "site.sqlite"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as c:
        c.executescript("""
          create table editions(id text primary key);
          create table site_catalog(id text,title text,catalog text,label text);
          create table catalog_lookup(field text,search_value text,label text,
            checked_at text,result text,match_json text,
            primary key(field,search_value,label));
        """)
        for key in ("old", "recent", "new"):
            c.execute("insert into editions values (?)", (key,))
            c.execute("insert into site_catalog values (?,?,?,?)", (key,key,key,""))
        for key, hours in (("old",8),("recent",7)):
            c.execute("insert into catalog_lookup values (?,?,?,?,?,?)",
                      ("catno",key,"",(now-timedelta(hours=hours)).isoformat(),"http_503","[]"))
    seen = []
    class Client:
        def __init__(self,*args,**kwargs):
            assert kwargs["headers"]["User-Agent"] == f"GameRecordTool/{PACKAGE_VERSION} (https://example.org/contact)"
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,url,params):
            seen.append(params["query"])
            return httpx.Response(503,request=httpx.Request("GET",url))
    monkeypatch.setattr(mod.httpx,"Client",Client)
    monkeypatch.setattr(mod.time,"sleep",lambda _:None)
    result=mod.run(path,3,contact='https://example.org/contact')
    assert result == {'status':'error','attempted':1,'failed':1,'selected':3}
    assert seen == ['catno:"old"']
    with sqlite3.connect(path) as c:
        rows = dict(c.execute("select search_value,result from catalog_lookup"))
    assert rows == {"old":"http_503","recent":"http_503"}

def test_recent_api_503_cools_all_queries(tmp_path,monkeypatch):
    path=tmp_path/'site.sqlite'
    now=datetime.now(timezone.utc)
    with sqlite3.connect(path) as c:
        c.executescript('''create table editions(id text primary key);
          create table site_catalog(id text,title text,catalog text,label text);
          create table catalog_lookup(field text,search_value text,label text,
            checked_at text,result text,match_json text,
            primary key(field,search_value,label));''')
        c.execute('insert into editions values (?)',('new',))
        c.execute('insert into site_catalog values (?,?,?,?)',('new','new title','NEW001','label'))
        c.execute('insert into catalog_lookup values (?,?,?,?,?,?)',
          ('catno','old title','label',(now-timedelta(hours=1)).isoformat(),'http_503','[]'))
    def forbidden(*args,**kwargs): raise AssertionError('recent host failure must suppress network')
    monkeypatch.setattr(mod.httpx,'Client',forbidden)
    result=mod.run(path,3,contact='https://example.org/contact')
    assert result=={'status':'cooldown','attempted':0,'failed':0,'selected':1,'reason':'http_503'}

def test_catalog_search_requires_valid_contact_before_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network must not start")
    monkeypatch.setattr(mod.httpx, "Client", forbidden)
    for contact in (None, "", "not-a-contact", "a@example.org\nBcc:evil"):
        assert mod.run(tmp_path / "unused.db", contact=contact)["status"] == "skipped"
    assert not (tmp_path / "unused.db").exists()
    assert mod.user_agent("https://example.org/contact") is not None
    assert mod.user_agent("operator@example.org") is not None

def test_site_refresh_continues_when_contact_missing(tmp_path, monkeypatch):
    import importlib.util
    import sys
    site_code = Path(__file__).resolve().parents[1] / "site"
    monkeypatch.syspath_prepend(str(site_code))
    spec = importlib.util.spec_from_file_location("test_site_update", site_code / "update_site.py")
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
    fake_root = tmp_path / "game-record-tool"
    fake_code = fake_root / "site"
    fake_code.mkdir(parents=True)
    monkeypatch.setattr(update, "__file__", str(fake_code / "update_site.py"))
    calls = []
    monkeypatch.setattr(update.refresh_site_data, "run", lambda *args: calls.append("refresh") or True)
    monkeypatch.setattr(update.audit_site, "run", lambda *args, **kwargs: calls.append(("audit", kwargs["catalog_result"]["status"])))
    def forbidden(*args, **kwargs):
        raise AssertionError("network must not start")
    monkeypatch.setattr(update.catalog_search.httpx, "Client", forbidden)
    monkeypatch.setattr(sys, "argv", ["update_site.py", "--search-limit", "1"])
    assert update.main() == 0
    assert calls == ["refresh", ("audit", "skipped")]


def test_site_reports_catalog_error_after_audit(tmp_path, monkeypatch):
    import importlib.util
    import sys
    site_code = Path(__file__).resolve().parents[1] / "site"
    monkeypatch.syspath_prepend(str(site_code))
    spec = importlib.util.spec_from_file_location("test_site_update_error", site_code / "update_site.py")
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
    root=tmp_path/"game-record-tool"; code=root/"site"; code.mkdir(parents=True)
    monkeypatch.setattr(update,"__file__",str(code/"update_site.py"))
    calls=[]
    monkeypatch.setattr(update.refresh_site_data,"run",lambda *args: calls.append("refresh") or True)
    result={'status':'error','attempted':2,'failed':1,'selected':5}
    monkeypatch.setattr(update.catalog_search,"run",lambda *args,**kwargs: result)
    monkeypatch.setattr(update.audit_site,"run",lambda *args,**kwargs: calls.append(kwargs["catalog_result"]))
    monkeypatch.setattr(sys,"argv",["update_site.py","--search-limit","1"])
    assert update.main()==1
    assert calls==["refresh",result]

def test_site_treats_partial_source_failure_as_completed(tmp_path,monkeypatch):
    import importlib.util
    import sys
    site_code=Path(__file__).resolve().parents[1]/'site'
    monkeypatch.syspath_prepend(str(site_code))
    spec=importlib.util.spec_from_file_location('test_site_update_cooldown',site_code/'update_site.py')
    update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)
    root=tmp_path/'game-record-tool';code=root/'site';code.mkdir(parents=True)
    monkeypatch.setattr(update,'__file__',str(code/'update_site.py'))
    monkeypatch.setattr(update.refresh_site_data,'run',lambda *args:False)
    result={'status':'cooldown','attempted':0,'failed':0,'selected':3,'reason':'http_503'}
    monkeypatch.setattr(update.catalog_search,'run',lambda *args,**kwargs:result)
    monkeypatch.setattr(update.audit_site,'run',lambda *args,**kwargs:None)
    monkeypatch.setattr(sys,'argv',['update_site.py','--search-limit','3'])
    assert update.main()==0


