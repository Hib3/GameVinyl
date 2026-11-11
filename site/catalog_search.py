import json, re, sqlite3, time, unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from pathlib import Path
import httpx

API = 'https://musicbrainz.org/ws/2/release/'
VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text(encoding='utf-8').strip()
def user_agent(contact):
    if not isinstance(contact, str) or not contact or len(contact) > 255:
        return None
    if any(ch.isspace() or ch in '()' for ch in contact):
        return None
    try:
        url = urlsplit(contact)
        is_url = url.scheme == 'https' and bool(url.hostname) and not (url.username or url.password)
    except ValueError:
        is_url = False
    is_email = re.fullmatch(r'[^@/]+@[^@/]+\.[^@/]+', contact) is not None
    if not (is_url or is_email):
        return None
    return f'GameRecordTool/{VERSION} ({contact})'
MISSING = {'', 'unknown', '不明', 'なし', 'none', 'n/a', '-'}
LIMIT = 50
HOST_COOLDOWN = timedelta(hours=6)
COOLDOWN_RESULTS = {'http_401','http_403','http_429','http_503'}

def norm(s):
    return ' '.join(unicodedata.normalize('NFKC', str(s or '')).casefold().split())

def valid_catalog(s):
    x = norm(s)
    return x not in MISSING and not x.startswith(('n/a','型番なし','カタログなし','不明'))

def quote(s):
    return '"' + s.replace('\\','\\\\').replace('"','\\"') + '"'

def request_key(title, catalog, label):
    if valid_catalog(catalog):
        return 'catno', norm(catalog), norm(label)
    return 'release', norm(title), norm(label)

def candidates(payload, field, value, label):
    matches = []
    for r in payload['releases']:
        if not any('vinyl' in norm(m.get('format')) for m in r.get('media', [])):
            continue
        if field == 'release' and norm(r.get('title')) != value:
            continue
        for li in r.get('label-info', []):
            if label and norm((li.get('label') or {}).get('name')) != label:
                continue
            if field == 'catno' and norm(li.get('catalog-number')) != value:
                continue
            if not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',r.get('id','')):
                continue
            matches.append({k:r.get(k) for k in ('id','title','date','barcode')})
            break
    return matches

def run(db, limit=80, contact=None):
    ua = user_agent(contact)
    if ua is None:
        print('MusicBrainz skipped: contact not configured or invalid', flush=True)
        return {'status':'skipped','attempted':0,'failed':0,'selected':0}
    start=time.monotonic()
    now=datetime.now(timezone.utc)
    cutoff=(now-timedelta(days=30)).isoformat()
    retry=(now-timedelta(hours=6)).isoformat()
    c=sqlite3.connect(db)
    with c:
        c.executescript("""
        create table if not exists catalog_lookup_queue(
          edition_id text primary key references editions(id), field text not null,
          search_value text not null, label text not null
        );
        create table if not exists catalog_lookup(
          field text not null, search_value text not null, label text not null,
          checked_at text not null, result text not null, match_json text not null,
          primary key(field,search_value,label)
        );
        """)
        c.executemany('insert or replace into catalog_lookup_queue values(?,?,?,?)',
          ((rid,*request_key(t,cat,l)) for rid,t,cat,l in c.execute('select id,title,catalog,label from site_catalog').fetchall()))
        c.commit()
        jobs=c.execute("""
          select q.field,q.search_value,q.label
          from catalog_lookup_queue q left join catalog_lookup a using(field,search_value,label)
          group by q.field,q.search_value,q.label
          having a.checked_at is null
             or (a.result in ('candidates','no_exact_candidate','partial') and a.checked_at<?)
             or (a.result not in ('candidates','no_exact_candidate','partial') and a.checked_at<?)
          order by coalesce(julianday(a.checked_at) + case when a.result in ('candidates','no_exact_candidate','partial') then 30.0 else 0.25 end, julianday(?)), min(q.edition_id) limit ?
        """,(cutoff,retry,now.isoformat(),limit)).fetchall()
        latest=c.execute('select checked_at,result from catalog_lookup order by checked_at desc limit 1').fetchone()
        if latest and latest[1] in COOLDOWN_RESULTS:
            try:
                failed_at=datetime.fromisoformat(latest[0])
                if failed_at.tzinfo is None:
                    failed_at=failed_at.replace(tzinfo=timezone.utc)
            except (TypeError,ValueError):
                failed_at=None
            if failed_at is not None and now-failed_at < HOST_COOLDOWN:
                return {'status':'cooldown','attempted':0,'failed':0,
                        'selected':len(jobs),'reason':latest[1]}
        attempted=0; failures=0
        with httpx.Client(timeout=7, headers={'User-Agent':ua,'Accept':'application/json'},trust_env=False,follow_redirects=False) as client:
            for i,(field,value,label) in enumerate(jobs):
                if time.monotonic()-start>125:
                    break
                if i:
                    time.sleep(2)
                query=field+':'+quote(value)
                if label:
                    query+=' AND label:'+quote(label)
                result='invalid_response'; found=[]; stop=False
                try:
                    r=client.get(API,params={'query':query,'fmt':'json','limit':LIMIT})
                    r.raise_for_status()
                    data=r.json()
                    found=candidates(data,field,value,label)
                    result='partial' if data['count']>LIMIT else 'candidates' if found else 'no_exact_candidate'
                except httpx.HTTPStatusError as e:
                    result=f'http_{e.response.status_code}'
                    stop=e.response.status_code in (401,403,429,503)
                except httpx.RequestError:
                    result='network_error'
                    stop=True
                except (KeyError,TypeError,ValueError,AttributeError):
                    pass
                attempted+=1
                if result not in ('candidates','no_exact_candidate','partial'):
                    failures+=1
                c.execute('insert or replace into catalog_lookup values(?,?,?,?,?,?)',
                          (field,value,label,datetime.now(timezone.utc).isoformat(),result,json.dumps(found,ensure_ascii=False)))
                c.commit()
                if stop:
                    break
    c.close()
    return {'status':'error' if failures else 'ok','attempted':attempted,
            'failed':failures,'selected':len(jobs)}

