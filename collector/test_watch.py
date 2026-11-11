import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core
from core import Store, Worker, extract
from server import query_api


class WatchTest(unittest.TestCase):
    def test_initialize_preserves_block_reason_across_restart(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)/'package'
            (root/'seed').mkdir(parents=True)
            (root/'seed'/'records.json').write_text(json.dumps([{'id':'AR-000001','title':'Game Vinyl','catalog':'GAME-1'}]),encoding='utf-8')
            store=Store(Path(folder)/'collector.sqlite3')
            config={'sources':[{'id':'yahoo_watch','seeds':[],'min_request_gap_seconds':0}], 'watches':[], 'interval_seconds':600,'min_request_gap_seconds':0,'catalog_priority_slots_per_hour':12}
            with patch.object(core,'ROOT',root):
                store.initialize(config)
                with store.db() as db:
                    db.execute("UPDATE market_search_coverage SET status='blocked',last_error='redirect diagnostics' WHERE edition_id='AR-000001'")
                store.initialize(config)
            with store.db() as db:
                row=db.execute("SELECT status,last_error FROM market_search_coverage WHERE edition_id='AR-000001'").fetchone()
        self.assertEqual(tuple(row),('blocked','redirect diagnostics'))

    def test_status_reports_live_disk_reserve(self):
        with TemporaryDirectory() as folder:
            store=Store(Path(folder)/'collector.sqlite3')
            with patch('server.shutil.disk_usage',return_value=SimpleNamespace(total=1000,free=99)):
                status=query_api(store,{'sources':[],'max_jobs_per_hour':10},'/api/status',{})
        self.assertTrue(status['meta']['storage_protection']['paused'])
        self.assertEqual(status['meta']['storage_protection']['free_bytes'],99)

    def test_yahoo_redirect_block_records_route_class_without_search_query(self):
        config=json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        source=next(s for s in config['sources'] if s['id']=='yahoo_watch')
        config['sources']=[source]
        config['min_request_gap_seconds']=0
        source['min_request_gap_seconds']=0
        search='https://auctions.yahoo.co.jp/search/search?p=PRIVATE_QUERY_MARKER'
        with TemporaryDirectory() as folder:
            store=Store(Path(folder)/'collector.sqlite3')
            with store.db() as db:
                db.execute('INSERT INTO source_state(id,next_allowed) VALUES(?,0)',('yahoo_watch',))
                db.execute('INSERT INTO jobs(url,source,due) VALUES(?,?,0)',(search,'yahoo_watch'))
            worker=Worker(store,config)
            with patch.object(worker.robots,'check',return_value=0), patch('core.fetch_http',return_value=(200,'<title>アクセスが集中しています</title>','https://auctions.yahoo.co.jp/')):
                self.assertTrue(worker.once())
            with store.db() as db:
                row=db.execute('SELECT status,last_error FROM jobs WHERE url=?',(search,)).fetchone()
            self.assertEqual(row['status'],'blocked')
            self.assertIn('route=root challenge_marker=1',row['last_error'])
            self.assertNotIn('PRIVATE_QUERY_MARKER',row['last_error'])

    def test_all_seed_editions_are_queued_or_have_a_route_reason(self):
        config = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        config['sources'] = [s for s in config['sources'] if s['id'] == 'yahoo_watch']
        with TemporaryDirectory() as folder:
            store=Store(Path(folder)/'collector.sqlite3')
            store.initialize(config)
            with store.db() as db:
                total=db.execute('select count(*) from records').fetchone()[0]
                covered=db.execute('select count(*) from market_search_coverage').fetchone()[0]
                missing=db.execute('select count(*) from records r left join market_search_coverage c on c.edition_id=r.id where c.edition_id is null').fetchone()[0]
                self.assertEqual((total,covered,missing),(3777,3777,0))
                self.assertEqual(db.execute("select count(*) from market_search_coverage where status='no_route' and coalesce(last_error,'')=''").fetchone()[0],0)
            status=query_api(store,config,'/api/status',{})
            self.assertEqual(status['market_search_coverage'].get('queued'),3777)

    def test_catalog_searches_are_resumable_and_candidates_do_not_alert(self):
        config = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        config['sources'] = [s for s in config['sources'] if s['id'] == 'yahoo_watch']
        config['watches'] = []
        config['min_request_gap_seconds'] = 0
        config['sources'][0]['min_request_gap_seconds'] = 0
        records = [
            {'id':'AR-000001','title':'Vintage Game Soundtrack','catalog':'VG-001'},
            {'id':'AR-000002','title':'Another Game Soundtrack','catalog':'VG-002'},
        ]
        with TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'seed').mkdir()
            (root/'seed'/'records.json').write_text(json.dumps(records),encoding='utf-8')
            bot=root/'bot.sqlite3'
            bot_db=sqlite3.connect(bot)
            with bot_db as db:
                db.execute('create table editions(id text,watch_level integer)')
                db.execute("insert into editions values('AR-000002',1)")
            bot_db.close()
            with patch.object(core,'ROOT',root):
                store=Store(root/'collector.sqlite3')
                store.initialize(config,bot)
                store.initialize(config,bot)
                with store.db() as db:
                    self.assertEqual(db.execute('select count(*) from market_search_coverage').fetchone()[0],2)
                    self.assertEqual(db.execute("select count(*) from market_search_targets where mode='catalog'").fetchone()[0],2)
                    self.assertEqual(db.execute("select priority from market_search_targets where edition_id='AR-000002'").fetchone()[0],1)
                    search=db.execute("select search_url from market_search_targets where edition_id='AR-000001'").fetchone()[0]
                listing='https://auctions.yahoo.co.jp/jp/auction/abc123'
                store.save_market_search(search,[(listing,'Vintage Game Soundtrack')])
                with store.db() as db:
                    db.execute("insert into jobs(url,source,due) values(?,'yahoo_watch',0)",(listing,))
                store.save({'url':listing,'source':'yahoo_watch','attempts':1},
                    {'title':'Vintage Game Soundtrack','page_type':'product','price':'1000','currency':'JPY','availability':'AUCTION_OPEN'},
                    '', '<h1>fixture</h1>', config)
                with store.db() as db:
                    facts=json.loads(db.execute('select facts from observations where url=?',(listing,)).fetchone()[0])
                    self.assertEqual(facts['candidate_ids'],['AR-000001'])
                    self.assertFalse(db.execute("select 1 from events where kind='watch_new'").fetchone())
                joined=query_api(store,config,'/api/records',{'q':['Vintage Game Soundtrack']})
                self.assertEqual(joined['items'][0]['possible_offers'][0]['match_reason'],'market_search_candidate_needs_review')

    def test_reserved_yahoo_capacity_only_processes_priority_queries(self):
        config = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        config['sources'] = [s for s in config['sources'] if s['id'] == 'yahoo_watch']
        config['sources'][0]['seeds']=[]
        config['watches']=[]
        config['max_jobs_per_hour']=2
        config['priority_slots_per_hour']=1
        config['min_request_gap_seconds']=0
        records=[{'id':'AR-000001','title':'Priority Game Record','catalog':'PG-1'},
                 {'id':'AR-000002','title':'Normal Game Record','catalog':'NG-2'}]
        with TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'seed').mkdir()
            (root/'seed'/'records.json').write_text(json.dumps(records),encoding='utf-8')
            with patch.object(core,'ROOT',root):
                store=Store(root/'collector.sqlite3')
                store.initialize(config)
                with store.db() as db:
                    priority=db.execute("select search_url from market_search_targets where edition_id='AR-000001'").fetchone()[0]
                    normal=db.execute("select search_url from market_search_targets where edition_id='AR-000002'").fetchone()[0]
                    db.execute('update market_search_targets set priority=1 where edition_id=?',('AR-000001',))
                    db.execute("update jobs set last_checked=?,status='ok' where url=?",(time.time(),priority))
                    db.execute('update jobs set due=0 where url in (?,?)',(priority,normal))
                self.assertEqual(store.claim(config)['url'],priority)

    def test_yahoo_detail_cap_rotates_without_requeueing_ended_listings(self):
        config = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        config['sources']=[s for s in config['sources'] if s['id']=='yahoo_watch']
        source=config['sources'][0]
        source['seeds']=[]
        source['max_urls']=1
        with TemporaryDirectory() as folder:
            store=Store(Path(folder)/'collector.sqlite3')
            store.initialize(config)
            old='https://auctions.yahoo.co.jp/jp/auction/old001'
            new='https://auctions.yahoo.co.jp/jp/auction/new001'
            with store.db() as db:
                db.execute("insert into jobs(url,source,due) values(?,?,0)",(old,source['id']))
            store.discover(source,[(new,'new')],config)
            with store.db() as db:
                self.assertEqual(db.execute('select status from jobs where url=?',(old,)).fetchone()[0],'rotated')
                self.assertEqual(db.execute('select status from jobs where url=?',(new,)).fetchone()[0],'pending')
            store.discover(source,[(old,'old')],config)
            with store.db() as db:
                self.assertEqual(db.execute('select status from jobs where url=?',(old,)).fetchone()[0],'pending')
                db.execute("update jobs set status='ended' where url=?",(old,))
            store.discover(source,[(old,'old')],config)
            with store.db() as db:
                self.assertEqual(db.execute('select status from jobs where url=?',(old,)).fetchone()[0],'ended')

    def test_pdf_is_not_vinyl(self):
        self.assertEqual(extract('<h1>Book (PDF + Media Gallery)</h1>', 'https://example.com/pdf', 'product')[0]['page_type'],'excluded_media')

    def test_search_discovers_only_watch_and_ended_offer_never_alerts(self):
        config = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
        config['sources'] = [s for s in config['sources'] if s['id'] == 'yahoo_watch']
        config['min_request_gap_seconds'] = 0
        config['sources'][0]['min_request_gap_seconds'] = 0
        config['watches'][0].pop('search_aliases',None)
        with TemporaryDirectory() as folder:
            store = Store(Path(folder)/'test.sqlite3')
            store.initialize(config)
            worker = Worker(store, config)
            found = 'https://auctions.yahoo.co.jp/jp/auction/test123'
            with store.db() as db:
                db.execute("UPDATE jobs SET due=9999999999 WHERE url LIKE '%/search/search?%'")
                watch_url=db.execute("select search_url from market_search_targets where mode='watch' limit 1").fetchone()[0]
                db.execute('UPDATE jobs SET due=0 WHERE url=?',(watch_url,))
            title = 'ポケモン ピカチュウレコード 3周年記念 レコード・CD1枚'
            rows = [{'auctionId':'test123','title':title}, {'auctionId':'other999','title':'ピカチュウ 人形'}]
            state = {'props':{'initialState':{'search':{'items':{'listing':{'items':rows}}}}}}
            search = '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(state)+'</script>'
            def result(url, hosts):
                if '/search/search?' in url:
                    return 200, search, url
                ended = url != found
                offer = {'@type':'Offer','price':'61000','priceCurrency':'JPY','priceValidUntil':('2020' if ended else '2099')+'-11-07T22:44:51+09:00'}
                html = '<h1>'+title+'</h1>' + ('このオークションは終了しています' if ended else '入札する')
                html += '<script type="application/ld+json">'+json.dumps({'@type':'Product','name':title,'offers':offer})+'</script>'
                return 200, html, url
            with patch.object(worker.robots,'check',return_value=0),patch('core.fetch_http',side_effect=result):
                for _ in range(4):
                    worker.once()
            feed = query_api(store, config, '/api/watch', {})
            self.assertEqual([a['url'] for a in feed['alerts']], [found])
            offers = {o['url']:o['facts'] for o in feed['offers']}
            self.assertEqual(offers[found]['availability'],'AUCTION_OPEN')
            ended = next(f for u,f in offers.items() if u.endswith('c1250831588'))
            self.assertEqual(ended['availability'],'AUCTION_ENDED')
            self.assertEqual(ended['auction_price'],'61000')
            self.assertIsNone(ended['price'])
            with store.db() as db:
                self.assertFalse(db.execute("SELECT 1 FROM jobs WHERE url LIKE '%other999%'").fetchone())
                self.assertFalse(db.execute("SELECT 1 FROM observations WHERE length(raw)>8").fetchone())
            self.assertFalse((Path(folder)/'backups').exists())
