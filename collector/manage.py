"""Local administrator commands; never exposed through the HTTP API."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import hashlib
import zlib
from urllib.parse import urlsplit
from core import DATA, Store, extract

parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['status','retry','backup','reextract','expand'])
parser.add_argument('--source')
args=parser.parse_args()
store=Store(DATA/'collector.sqlite3')
if args.command=='status':
    with store.db() as db:
        print(json.dumps({'jobs':[dict(r) for r in db.execute('SELECT source,status,COUNT(*) count FROM jobs GROUP BY source,status')], 'observations':db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 'errors':[dict(r) for r in db.execute('SELECT source,last_error FROM jobs WHERE last_error IS NOT NULL LIMIT 5')]},ensure_ascii=True))
elif args.command=='retry':
    if not args.source:
        parser.error('--source required')
    with store.db() as db:
        count=db.execute("UPDATE jobs SET due=? WHERE source=? AND status!='running'",(time.time(),args.source)).rowcount
        print(json.dumps({'rescheduled':count,'source':args.source}))
elif args.command=='expand':
    config=json.loads((Path(__file__).parent/'sources.json').read_text(encoding='utf-8'))
    store.initialize(config)
    source={'kind':'discovery','discover':False}
    with store.db() as db:
        before=db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        observations=db.execute('SELECT o.facts FROM observations o JOIN (SELECT MAX(id) id FROM observations GROUP BY url) x ON o.id=x.id').fetchall()
        records=db.execute('SELECT data FROM records').fetchall()
    for row in observations:
        store.discover(source,[(x['url'],x.get('label','')) for x in json.loads(row['facts']).get('related_links',[])],config)
    for row in records:
        data=json.loads(row['data'])
        store.discover(source,[(x['url'],x.get('label','')) for x in data.get('sources',[]) if x.get('url')],config)
    with store.db() as db:
        print(json.dumps({'new_product_jobs':db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]-before,'sources':[dict(r) for r in db.execute('SELECT source,COUNT(*) count FROM jobs GROUP BY source')]}))
elif args.command=='reextract':
    config=json.loads((Path(__file__).parent/'sources.json').read_text(encoding='utf-8'))
    sources={s['id']:s for s in config['sources']}
    changed=0
    with store.db() as db:
        for row in db.execute('SELECT * FROM observations WHERE length(raw)>8'):
            source=sources.get(row['source'])
            if not source:
                continue
            kind='catalog' if urlsplit(row['url']).path in config.get('catalog_pages',{}).get(source['id'],[]) else source['kind']
            facts,excerpt,_=extract(zlib.decompress(row['raw']).decode('utf-8'),row['url'],kind,source.get('selectors'))
            facts['final_url']=json.loads(row['facts']).get('final_url',row['url'])
            payload=json.dumps(facts,sort_keys=True,ensure_ascii=False)
            digest=hashlib.sha256(payload.encode()).hexdigest()
            if digest!=row['facts_hash']:
                db.execute('UPDATE observations SET facts=?,facts_hash=?,excerpt=? WHERE id=?',(payload,digest,excerpt,row['id']))
                db.execute('INSERT INTO events(url,created,kind,detail) VALUES(?,?,?,?)',(row['url'],time.time(),'extraction_revised',json.dumps({'observation_id':row['id'],'original_observed':row['observed'],'old_facts':json.loads(row['facts']),'reason':'local re-extraction; not a new network observation'},ensure_ascii=False)))
                db.execute('UPDATE candidates SET title=? WHERE url=?',(facts['title'],row['url']))
                changed+=1
    print(json.dumps({'reextracted':changed,'observation_times_preserved':True}))
else:
    target=DATA/'backups'/('collector-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
    target.parent.mkdir(exist_ok=True)
    with store.db() as db:
        backup=sqlite3.connect(target)
        try:
            db.backup(backup)
        finally:
            backup.close()
    print(str(target))
