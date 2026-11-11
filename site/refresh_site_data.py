import argparse, json, re, sqlite3, time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import httpx
import feedparser
from bs4 import BeautifulSoup

AGENT='GameRecordTool/2.0 (personal catalog freshness)'
SHOP='https://data-discs.com'
RATE_LIMIT_STATUSES=(403,429,503)
RATE_LIMIT_COOLDOWN=timedelta(hours=6)
TERMS=SHOP+'/pages/terms-conditions'
SOURCES=(
 ('blipblop','https://blipblop.net/feed/',None,None,None,None),
 ('data-discs-streets-ajax',SHOP+'/products/streets-of-rage','DATA001',TERMS,57436513108342,'DATA001C'),
 ('data-discs-shenmue-ajax',SHOP+'/products/shenmue','DATA002',TERMS,43731220106,'DATA002RP'),
 ('data-discs-shinobi3-ajax',SHOP+'/products/shinobi3','DATA003',TERMS,6831161089,'DATA003C'),
 ('data-discs-superhangon-red',SHOP+'/products/superhangon','DATA004',TERMS,21218451193941,'DATA004C'),
 ('data-discs-superhangon-splatter',SHOP+'/products/superhangon','DATA004',TERMS,6753804097,'DATA004S'),
 ('data-discs-superhangon-black',SHOP+'/products/superhangon','DATA004',TERMS,6844454657,'DATA004B'),
 ('data-discs-streetsofrage2-clear',SHOP+'/products/streetsofrage2','DATA005',TERMS,44868879562,'DATA005C-RE2'),
 ('data-discs-streetsofrage2-black',SHOP+'/products/streetsofrage2','DATA005',TERMS,45126063946,'DATA005B-RE'),
)


def robots(client, origin):
    parser=RobotFileParser()
    parser.parse(read(client, origin+'/robots.txt').decode('utf-8').splitlines())
    return parser

def read(client,url):
    with client.stream('GET',url) as resp:
        resp.raise_for_status()
        chunks=bytearray()
        for chunk in resp.iter_bytes():
            if len(chunks)+len(chunk)>2_000_000:
                raise ValueError('body_limit')
            chunks.extend(chunk)
        return bytes(chunks)

def allowed(rules,url):
    if not rules.can_fetch(AGENT,url):
        raise PermissionError('robots_denied')

def parse_product(product, catalog, currency, variant_id, sku):
    if not product['title'].strip().upper().startswith(catalog+':'):
        raise ValueError('catalog_mismatch')
    variants=[v for v in product['variants'] if v.get('id') == variant_id and v.get('sku') == sku]
    if len(variants)!=1:
        raise ValueError('variant_review')
    v=variants[0]
    if type(v.get('id')) is not int or type(v.get('available')) is not bool:
        raise ValueError('invalid_variant')
    if currency!='GBP' or type(v.get('price')) is not int or v['price']<=0:
        raise ValueError('price_or_currency_unverified')
    return (1,v['price']/100,currency,'in_stock' if v['available'] else 'out_of_stock',
            str(v['id']),v.get('sku'))

def observe(client,rules,currency,url,catalog,variant_id,sku,products):
    if catalog:
        endpoint=url+'.js'
        allowed(rules,endpoint)
        if endpoint not in products:
            products[endpoint]=json.loads(read(client,endpoint))
        return parse_product(products[endpoint],catalog,currency,variant_id,sku)
    allowed(rules,url)
    feed=feedparser.parse(read(client,url))
    if feed.bozo or not feed.entries:
        raise ValueError('invalid_or_empty_feed')
    return len(feed.entries),None,None,None,None,None

def discover_links(c,client,rules):
    index=SHOP+'/collections/records'
    allowed(rules,index)
    soup=BeautifulSoup(read(client,index),'html.parser')
    allowed_catalogs={r[0] for r in c.execute("select distinct catalog from site_catalog where lower(label)='data discs'")}
    found=defaultdict(set)
    for a in soup.select('a[href]'):
        href=urlsplit(a['href'])
        path=re.fullmatch(r'/(?:collections/records/)?products/([a-z0-9-]+)',href.path)
        title=re.match(r'^(DATA[0-9]{3}):', ' '.join(a.stripped_strings))
        if path and title and not href.netloc and not href.scheme and title[1] in allowed_catalogs:
            found[title[1]].add(SHOP+'/products/'+path[1])
    unique=[(cat,next(iter(urls))) for cat,urls in sorted(found.items()) if len(urls)==1]
    if not unique:
        raise ValueError('merchant_directory_empty')
    return index,unique


def blocked_hosts_from_history(c, now):
    latest={}
    for url,checked_at,status in c.execute("""select s.url,r.checked_at,r.http_status
        from freshness_runs r join freshness_sources s using(source_id)
        where r.http_status is not null order by r.id desc"""):
        parts=urlsplit(url)
        origin=f'{parts.scheme}://{parts.netloc}'.lower()
        if origin and origin not in latest:
            latest[origin]=(checked_at,status)
    blocked=set()
    for origin,(checked_at,status) in latest.items():
        if status not in RATE_LIMIT_STATUSES:
            continue
        try:
            checked=datetime.fromisoformat(checked_at)
            if checked.tzinfo is None:
                checked=checked.astimezone()
        except (TypeError,ValueError):
            continue
        if checked <= now < checked+RATE_LIMIT_COOLDOWN:
            blocked.add(origin)
    return blocked


def run(db, rounds, merchants_only=False):
    all_ok=True
    if not db.is_file():
        raise ValueError('staging database required')
    with sqlite3.connect(db) as c:
        if not c.execute("select 1 from sqlite_master where type='view' and name='site_catalog'").fetchone():
            raise ValueError('staging database required')
        c.execute('pragma foreign_keys=on')
        c.executescript("""
        create table if not exists merchant_discovery(
          catalog_group text primary key, official_product_url text not null,
          directory_url text not null, last_seen_at text not null
        );
        create view if not exists freshness_last_success as
        select s.source_id, r.checked_at, r.price, r.currency, r.availability
        from freshness_sources s join freshness_runs r using (source_id)
        where r.id=(select max(x.id) from freshness_runs x where x.source_id=s.source_id and x.result='ok');
        create table if not exists ajax_variant_checks (
            run_id integer primary key references freshness_runs(id), variant_id text not null, sku text
        );
        create view if not exists official_product_links as
        select f.source_id, f.catalog_group, f.url as official_product_url,
               count(e.id) as editions_sharing_catalog,
               'catalog_group_only' as identity_status, 'reuse_unconfirmed' as image_rights
        from freshness_sources f left join site_catalog e on e.catalog=f.catalog_group and e.label='Data Discs'
        where f.use_scope='private_ajax_catalog_group'
        group by f.source_id;
        """)
        c.executemany('insert or ignore into freshness_sources values(?,?,?,?,?,?)',[
          (sid,'product_api' if cat else 'rss',url,cat,terms,'private_ajax_catalog_group' if cat else 'discovery_feed')
          for sid,url,cat,terms,_,_ in SOURCES
        ] + [('data-discs-directory','product_directory',SHOP+'/collections/records',None,TERMS,'private_directory')])
        c.commit()
        for number in range(rounds):
            if number:
                time.sleep(45)
            with httpx.Client(headers={'User-Agent':AGENT},timeout=15,follow_redirects=False,trust_env=False) as client:
                rules_by_host={}
                currency=None
                blocked_hosts=blocked_hosts_from_history(c,datetime.now().astimezone())
                products={}
                for sid,url,cat,_,variant_id,expected_sku in SOURCES:
                    if merchants_only and not cat:
                        continue
                    origin='https://'+urlsplit(url).netloc
                    extracted=price=cur=availability=vid=sku=status=None
                    try:
                        if origin in blocked_hosts:
                            raise PermissionError('host_temporarily_blocked')
                        if origin not in rules_by_host:
                            rules_by_host[origin]=robots(client,origin)
                        rules=rules_by_host[origin]
                        if cat and currency is None:
                            allowed(rules,SHOP+'/cart.js')
                            currency=json.loads(read(client,SHOP+'/cart.js'))['currency']
                        extracted,price,cur,availability,vid,sku=observe(client,rules,currency,url,cat,variant_id,expected_sku,products)
                        result,status='ok',200
                    except httpx.HTTPStatusError as exc:
                        status=exc.response.status_code
                        result=f'http_{status}'
                        if status in RATE_LIMIT_STATUSES:
                            blocked_hosts.add(origin)
                    except httpx.RequestError:
                        result='network_error'
                    except PermissionError as exc:
                        result=str(exc)
                    except (ValueError,KeyError,TypeError,AttributeError):
                        result='parse_or_validation_error'
                    all_ok &= result == 'ok'
                    now=datetime.now().astimezone().isoformat(timespec='seconds')
                    rid=c.execute('insert into freshness_runs(source_id,checked_at,result,http_status,extracted,price,currency,availability) values(?,?,?,?,?,?,?,?)',
                                  (sid,now,result,status,extracted,price,cur,availability)).lastrowid
                    if vid:
                        c.execute('insert into ajax_variant_checks values(?,?,?)',(rid,vid,sku))
                    c.commit()
                    print(json.dumps([sid,now,result,status,extracted,price,cur,availability,vid,sku]),flush=True)
                result,status,count='not_checked',None,None
                try:
                    if SHOP in blocked_hosts:
                        raise PermissionError('host_temporarily_blocked')
                    rules=rules_by_host[SHOP]
                    index,links=discover_links(c,client,rules)
                    count=len(links)
                    now=datetime.now().astimezone().isoformat(timespec='seconds')
                    c.executemany('insert or replace into merchant_discovery values(?,?,?,?)',
                                  ((cat,url,index,now) for cat,url in links))
                    result,status='ok',200
                except httpx.HTTPStatusError as exc:
                    status=exc.response.status_code
                    result=f'http_{status}'
                except httpx.RequestError:
                    result='network_error'
                except PermissionError as exc:
                    result=str(exc)
                except (ValueError,KeyError,TypeError):
                    result='directory_unverified'
                now=datetime.now().astimezone().isoformat(timespec='seconds')
                c.execute('insert into freshness_runs(source_id,checked_at,result,http_status,extracted) values(?,?,?,?,?)',
                          ('data-discs-directory',now,result,status,count))
                c.commit()
                all_ok &= result=='ok'
                print(json.dumps(['data-discs-directory',now,result,status,count]),flush=True)

    return all_ok

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('db',type=Path)
    ap.add_argument('--rounds',type=int,default=1,choices=(1,2,3))
    ap.add_argument('--merchants-only',action='store_true')
    args=ap.parse_args()
    raise SystemExit(0 if run(args.db,args.rounds,args.merchants_only) else 1)

