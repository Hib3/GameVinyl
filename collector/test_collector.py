import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import Store, Worker, Robots, BeautifulSoup, canonical, public_target, extract, fetch_browser
from server import query_api


class ExtractionTests(unittest.TestCase):
    def html(self, offers):
        return '<h1>Game Vinyl</h1><script type="application/ld+json">'+json.dumps({'@type':'Product','name':'Game Vinyl','sku':'SHOP42','offers':offers})+'</script>'

    def test_single_offer(self):
        f,_,_=extract(self.html({'@type':'Offer','price':'25.50','priceCurrency':'GBP','availability':'https://schema.org/InStock'}),'https://example.com/p','product')
        self.assertEqual(f['price'],'25.50')
        self.assertEqual(f['availability'],'IN_STOCK_STRUCTURED')
        self.assertIsNone(f['catalog'])
        self.assertFalse(f['identity_verified'])

    def test_aggregate_is_not_price(self):
        f,_,_=extract(self.html({'@type':'AggregateOffer','lowPrice':'1','priceCurrency':'JPY'}),'https://example.com/p','product')
        self.assertIsNone(f['price'])

    def test_multiple_offers(self):
        f,_,_=extract(self.html([{'@type':'Offer','price':1},{'@type':'Offer','price':2}]),'https://example.com/p','product')
        self.assertIsNone(f['price'])

    def test_variant_prices_keep_range(self):
        f,_,_=extract(self.html([{'@type':'Offer','price':20,'priceCurrency':'USD','name':'Black vinyl'},{'@type':'Offer','price':30,'priceCurrency':'USD','name':'Color vinyl'}]),'https://example.com/p','product')
        self.assertEqual((f['price'],f['price_max']),('20','30'))
        self.assertEqual(len(f['variants']),2)
        self.assertEqual(f['price_kind'],'variant_range_not_identical_edition')

    def test_product_group_excludes_minidisc(self):
        group={'@type':'ProductGroup','@id':'/products/game#product','name':'Game','hasVariant':[
            {'@type':'Product','name':'Game Vinyl','offers':{'@type':'Offer','price':'52','priceCurrency':'CAD'}},
            {'@type':'Product','name':'Game Mini Disc','offers':{'@type':'Offer','price':'45','priceCurrency':'CAD'}}]}
        html='<script type="application/ld+json">'+json.dumps(group)+'</script>'
        f,_,_=extract(html,'https://example.com/products/game','product')
        self.assertEqual(f['price'],'52')
        self.assertIn('non_vinyl_variants_excluded',f['review_flags'])

    def test_shopify_embedded_product(self):
        product={'title':'Game LP','handle':'game','variants':[{'id':42,'price':2999,'available':True}]}
        html='<meta property="og:price:currency" content="USD"><script type="application/json" id="ProductJson-main">'+json.dumps(product)+'</script>'
        self.assertEqual(extract(html,'https://example.com/products/game','product')[0]['price'],'29.99')
        self.assertIsNone(extract(html,'https://example.com/products/other','product')[0]['price'])

    def test_related_product_stock_not_main_stock(self):
        product={'@type':'Product','name':'Game','description':'Vinyl soundtrack', 'offers':{'@type':'Offer','price':'20','priceCurrency':'EUR','availability':'https://schema.org/InStock'}}
        html='<script type="application/ld+json">'+json.dumps(product)+'</script><aside>Other product sold out</aside>'
        self.assertEqual(extract(html,'https://example.com/p','product')[0]['availability'],'IN_STOCK_STRUCTURED')

    def test_free_text_buyback_not_price(self):
        f,_,_=extract('<h1>Xanadu</h1>定価700円 買取価格800円 送料240円','https://example.com/p','product')
        self.assertIsNone(f['price'])

    def test_conflicting_stock(self):
        html=self.html({'@type':'Offer','price':'25','priceCurrency':'GBP','availability':'https://schema.org/InStock'})+'Sold out'
        self.assertEqual(extract(html,'https://example.com/p','product')[0]['availability'],'CONFLICT')

    def test_error_page(self):
        with self.assertRaises(PermissionError):
            extract('<title>Just a moment</title>','https://example.com','product')

    def test_feed_links(self):
        html='<rss><channel><item><title>New vinyl</title><link>https://example.com/new-vinyl/</link></item></channel></rss>'
        facts,_,links=extract(html,'https://example.com/feed/','discovery')
        self.assertEqual(facts['page_type'],'feed')
        self.assertIn(('https://example.com/new-vinyl/','feed item'),links)

    def test_feed_extraction_does_not_require_optional_xml_builder(self):
        html='<rss><channel><item><title>New vinyl</title><link>https://example.com/new-vinyl/</link></item></channel></rss>'
        def parse(markup, features):
            if features == 'xml':
                raise AssertionError('optional XML builder was requested')
            return BeautifulSoup(markup, features)
        with patch('core.BeautifulSoup', side_effect=parse):
            facts,_,links=extract(html,'https://example.com/feed/','discovery')
        self.assertEqual(facts['page_type'],'feed')
        self.assertIn(('https://example.com/new-vinyl/','feed item'),links)

    def test_negative_and_nan(self):
        for value in ('NaN','Infinity','-5','bad'):
            f,_,_=extract(self.html({'@type':'Offer','price':value,'priceCurrency':'JPY'}),'https://example.com','product')
            self.assertIsNone(f['price'])

    def test_expired_structured_offer(self):
        f,_,_=extract(self.html({'@type':'Offer','price':'2000','priceCurrency':'JPY','availability':'https://schema.org/InStock','priceValidUntil':'2020-01-01'}),'https://example.com/p','product')
        self.assertIsNone(f['price'])
        self.assertEqual(f['availability'],'UNKNOWN')

    def test_tax_display_overrides_expired_json(self):
        html=self.html({'@type':'Offer','price':'2000','priceCurrency':'JPY','availability':'https://schema.org/InStock','priceValidUntil':'2020-01-01'})+'<span class="price">2,200</span><button class="buy">カートに入れる</button>'
        f,_,_=extract(html,'https://example.com/p','product',{'price':'.price','currency':'JPY','buy_button':'.buy'})
        self.assertEqual(f['price'],'2200')
        self.assertEqual(f['structured_price'],'2000')
        self.assertEqual(f['availability'],'PURCHASE_BUTTON_OBSERVED')
        self.assertIn('structured_vs_displayed_price_difference',f['review_flags'])

    def test_multiple_selector_not_used(self):
        f,_,_=extract('<b class="p">50</b><b class="p">20</b>','https://example.com/p','product',{'price':'.p','currency':'JPY'})
        self.assertIsNone(f['price'])

    def test_empty_logo_heading_fallback(self):
        f,_,_=extract('<title>New record</title><h1><img src="logo"></h1>','https://example.com/p','discovery')
        self.assertEqual(f['title'],'New record')


class SecurityTests(unittest.TestCase):
    def test_credentials_scheme_port(self):
        for url in ('file:///x','http://user:pass@example.com/','http://example.com:8000/'):
            with self.assertRaises(ValueError):
                canonical(url)

    def test_disallowed_host(self):
        with self.assertRaises(ValueError):
            public_target('https://evil.example/', ['example.com'])

    def test_private_dns(self):
        with patch('core.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):
                public_target('https://example.com/', ['example.com'])

    def test_robots_fail_closed(self):
        with patch('core.fetch_http',return_value=(403,'','https://example.com/robots.txt')):
            with self.assertRaises(PermissionError):
                Robots().check('https://example.com/a',['example.com'])

    def test_robots_disallow(self):
        with patch('core.fetch_http',return_value=(200,'User-agent: *\nDisallow: /private','')):
            with self.assertRaises(PermissionError):
                Robots().check('https://example.com/private',['example.com'])

    def test_runtime_dependencies_do_not_pull_ai_sdks(self):
        forbidden={'crawl4ai','unclecode-litellm','openai','tiktoken','tokenizers','huggingface-hub'}
        for path in (Path(__file__).parents[1]/'requirements.txt', Path(__file__).parent/'requirements.txt', Path(__file__).parent/'requirements-lock.txt'):
            if not path.exists():
                continue
            names={line.split('=',1)[0].strip().lower().replace('_','-') for line in path.read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#')}
            self.assertFalse(forbidden & names, f'{path}: {sorted(forbidden & names)}')
        self.assertIn('playwright==1.65.0', (Path(__file__).parents[1]/'requirements.txt').read_text(encoding='utf-8'))


class BrowserFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_playwright_fetch_keeps_host_method_and_websocket_guards(self):
        captured={}
        class Response: status=200
        class Page:
            url='https://example.com/product'
            async def goto(self,url,**kwargs): captured['goto']=(url,kwargs); return Response()
            async def wait_for_timeout(self,ms): captured['delay']=ms
            async def content(self): return '<h1>Product</h1>'
        class Context:
            async def route_web_socket(self,pattern,handler): captured['websocket_handler']=handler
            async def route(self,pattern,handler): captured['route_handler']=handler
            async def new_page(self): return Page()
        class Browser:
            async def new_context(self,**kwargs):
                captured['context']=kwargs
                return Context()
            async def close(self): captured['closed']=True
        class Chromium:
            async def launch(self,**kwargs): captured['launch']=kwargs; return Browser()
        class Manager:
            async def __aenter__(self): return type('Playwright',(),{'chromium':Chromium()})()
            async def __aexit__(self,*args): return False
        class Route:
            def __init__(self,url,method='GET'):
                self.request=type('Request',(),{'url':url,'method':method})()
                self.aborted=False; self.continued=False
            async def abort(self): self.aborted=True
            async def continue_(self): self.continued=True
        class WebSocket:
            closed=False
            async def close(self): self.closed=True
        with patch('core.public_target',return_value=(None,'198.51.100.8')), patch('playwright.async_api.async_playwright',return_value=Manager()):
            result=await fetch_browser('https://example.com/product',['example.com'])
        self.assertEqual(result,(200,'<h1>Product</h1>','https://example.com/product'))
        self.assertEqual(captured['delay'],1000)
        self.assertEqual(captured['context'],{'service_workers':'block'})
        self.assertIn('MAP example.com 198.51.100.8',captured['launch']['args'][0])
        self.assertTrue(captured['closed'])
        allow=Route('https://example.com/image.jpg')
        await captured['route_handler'](allow)
        self.assertTrue(allow.continued)
        for route in (Route('https://evil.example/image.jpg'),Route('https://example.com/submit','POST')):
            await captured['route_handler'](route)
            self.assertTrue(route.aborted)
        ws=WebSocket()
        await captured['websocket_handler'](ws)
        self.assertTrue(ws.closed)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.sqlite3')
        self.config={'sources':[{'id':'test','name':'Test','note':'test','enabled':True,'method':'http','kind':'product','hosts':['example.com'],'seeds':['https://example.com/p'],'discover':True,'include_path':'^/p'}], 'interval_seconds':21600,'freshness_seconds':86400,'min_request_gap_seconds':0,'max_jobs_per_hour':30,'max_urls_per_source':10}
        with self.store.db() as db:
            db.execute('INSERT INTO source_state VALUES(?,?)',('test',0))
            db.execute('INSERT INTO jobs(url,source,due) VALUES(?,?,?)',('https://example.com/p','test',0))
        self.facts=extract('<h1>Game vinyl</h1>','https://example.com/p','product')[0]

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_exclusive_and_resume(self):
        job=self.store.claim(self.config)
        self.assertIsNotNone(job)
        self.assertIsNone(self.store.claim(self.config))
        with self.store.db() as db:
            db.execute('UPDATE jobs SET lease=0')
        restarted=Store(self.store.path)
        self.assertIsNotNone(restarted.claim(self.config))

    def test_duplicate_snapshot_no_duplicate_event(self):
        job=self.store.claim(self.config)
        for _ in range(2):
            self.store.save(job,self.facts,'evidence','html',self.config)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)

    def test_price_drop_same_currency_only(self):
        job=self.store.claim(self.config)
        for price,currency in [('100','JPY'),('50','USD'),('40','USD')]:
            facts=self.facts|{'price':price,'currency':currency}
            self.store.save(job,facts,'evidence',price+currency,self.config)
        with self.store.db() as db:
            kinds=[r[0] for r in db.execute('SELECT kind FROM events')]
        self.assertEqual(kinds,['new_observation','facts_changed','price_drop_unverified'])

    def test_stale_and_failed_api(self):
        job=self.store.claim(self.config)
        self.store.save(job,self.facts,'evidence','html',self.config)
        self.store.failed(job,PermissionError('robots disallow'),self.config)
        out=query_api(self.store,self.config,'/api/candidates',{})
        self.assertEqual(out['items'][0]['freshness'],'LAST_FETCH_FAILED')
        with self.store.db() as db:
            db.execute('UPDATE observations SET expires=0')
        out=query_api(self.store,self.config,'/api/candidates',{})
        self.assertEqual(out['items'][0]['freshness'],'EXPIRED')
        self.assertFalse(out['items'][0]['purchase_recommended'])

    def test_http_429_pauses_only_that_source_until_retry(self):
        other_source=self.config['sources'][0]|{'id':'other','hosts':['other.example'],'seeds':['https://other.example/p']}
        self.config['sources'].append(other_source)
        with self.store.db() as db:
            db.execute("INSERT INTO jobs(url,source,due) VALUES('https://example.com/p2','test',0)")
        with patch('core.time.time', return_value=1000):
            job=self.store.claim(self.config)
            self.store.failed(job,PermissionError('HTTP 429'),self.config)
            self.assertIsNone(self.store.claim(self.config))
            with self.store.db() as db:
                self.assertEqual(db.execute('SELECT next_allowed FROM source_state WHERE id=?',('test',)).fetchone()[0],22600)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='blocked' AND last_error='HTTP 429'").fetchone()[0],1)
                db.execute('INSERT INTO source_state VALUES(?,?)',('other',0))
                db.execute('INSERT INTO jobs(url,source,due) VALUES(?,?,0)',('https://other.example/p','other'))
            self.assertEqual(self.store.claim(self.config)['source'],'other')

    def test_repeated_same_failure_updates_event_instead_of_adding_rows(self):
        with self.store.db() as db:
            plan=[row['detail'] for row in db.execute("EXPLAIN QUERY PLAN SELECT id,detail FROM events WHERE url=? AND kind='fetch_failed' ORDER BY id DESC LIMIT 1",('https://example.com/p',))]
        self.assertTrue(any('fetch_failed_url' in step for step in plan))
        job=self.store.claim(self.config)
        self.store.failed(job,ValueError('Yahoo redirected away from requested page'),self.config)
        with self.store.db() as db:
            first_created=db.execute("SELECT created FROM events WHERE kind='fetch_failed'").fetchone()[0]
        self.store.failed(job,ValueError('Yahoo redirected away from requested page'),self.config)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='fetch_failed'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT created FROM events WHERE kind='fetch_failed'").fetchone()[0],first_created)
        event=query_api(self.store,self.config,'/api/events',{})['items'][0]
        self.assertEqual(event['detail']['repeat_count'],2)
        self.assertIn('last_seen',event['detail'])

    def test_yahoo_catalog_redirect_block_has_daily_cooldown_but_priority_search_does_not(self):
        error = ValueError('Yahoo redirect blocked: host_relation=same route=search challenge_marker=0 query_match=0')
        cases = [('catalog', 0, 86400), ('catalog', 1, 21600), ('watch', 1, 21600)]
        with patch('core.time.time', return_value=1000):
            for index, (mode, priority, expected_delay) in enumerate(cases, 1):
                url = f'https://auctions.yahoo.co.jp/search/search?p=SAFE_TEST_{index}'
                with self.store.db() as db:
                    db.execute("INSERT INTO jobs(url,source,due,status) VALUES(?, 'yahoo_watch', 0, 'running')", (url,))
                    db.execute('INSERT INTO market_search_targets(search_url,edition_id,query,mode,priority) VALUES(?,?,?,?,?)',
                               (url, f'AR-{index:06d}', f'SAFE_TEST_{index}', mode, priority))
                self.store.failed({'url': url, 'source': 'yahoo_watch', 'attempts': 1}, error, self.config)
                with self.store.db() as db:
                    row = db.execute('SELECT status,due,last_error FROM jobs WHERE url=?', (url,)).fetchone()
                self.assertEqual(row['status'], 'blocked')
                self.assertEqual(row['due'], 1000 + expected_delay)
                self.assertNotIn(f'SAFE_TEST_{index}', row['last_error'])

    def test_discovery_scope_dedup(self):
        source=self.config['sources'][0]
        self.store.discover(source,[('https://example.com/p2','x'),('https://example.com/p2','x'),('http://127.0.0.1/p3','x'),('https://evil.com/p','x')],self.config)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],2)

    def test_worker_failure_not_sold_out(self):
        worker=Worker(self.store,self.config)
        with patch.object(worker.robots,'check',return_value=0),patch('core.fetch_http',return_value=(404,'','https://example.com/p')):
            worker.once()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'error')

    def test_feed_not_candidate(self):
        job=self.store.claim(self.config)
        self.store.save(job,self.facts|{'page_type':'feed'},'feed','feed',self.config)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],0)

    def test_backup_restore(self):
        import shutil
        job=self.store.claim(self.config)
        self.store.save(job,self.facts,'evidence','html',self.config)
        self.store.backup_daily()
        backup=next((Path(self.temp.name)/'backups').glob('daily-*.sqlite3'))
        restored=Path(self.temp.name)/'restored.sqlite3'
        shutil.copyfile(backup,restored)
        with Store(restored).db() as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('select count(*) from observations').fetchone()[0],1)

    def test_low_disk_pauses_collection_and_resumes_at_twenty_percent(self):
        worker=Worker(self.store,self.config)
        with patch('core.shutil.disk_usage',return_value=SimpleNamespace(total=1000,free=199)):
            self.assertFalse(worker.once())
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE url='https://example.com/p'").fetchone()[0],'pending')
            self.assertTrue(json.loads(db.execute("SELECT value FROM meta WHERE key='storage_protection'").fetchone()[0])['paused'])
        with patch('core.shutil.disk_usage',return_value=SimpleNamespace(total=1000,free=200)), patch.object(self.store,'claim',return_value=None):
            self.assertFalse(worker.once())
        with self.store.db() as db:
            state=json.loads(db.execute("SELECT value FROM meta WHERE key='storage_protection'").fetchone()[0])
        self.assertFalse(state['paused'])
        self.assertEqual(state['minimum_free_percent'],20)

    def test_manual_backup_keeps_only_latest_valid_copy(self):
        with patch('core.datetime') as clock:
            clock.now.side_effect=[datetime(2026,11,1,tzinfo=timezone.utc),datetime(2026,11,2,tzinfo=timezone.utc)]
            self.store.backup_manual()
            latest=self.store.backup_manual()
        backups=list((Path(self.temp.name)/'backups').glob('collector-*.sqlite3'))
        self.assertEqual(backups,[latest])
        db=sqlite3.connect(latest)
        try:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
        finally:
            db.close()

    def test_daily_backup_retains_only_latest_verified_day(self):
        with patch('core.datetime') as clock:
            clock.now.side_effect=[datetime(2026,11,1,tzinfo=timezone.utc),datetime(2026,11,2,tzinfo=timezone.utc),datetime(2026,11,3,tzinfo=timezone.utc)]
            for _ in range(3):
                self.store.backup_daily()
        backups=sorted(p.name for p in (Path(self.temp.name)/'backups').glob('daily-*.sqlite3'))
        self.assertEqual(backups,['daily-20261103.sqlite3'])
        db=sqlite3.connect(Path(self.temp.name)/'backups'/backups[0])
        try:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
        finally:
            db.close()
        with self.store.db() as db:
            status=json.loads(db.execute("SELECT value FROM meta WHERE key='daily_backup_status'").fetchone()[0])
        self.assertEqual(status['status'],'created')
        self.assertEqual(status['file'],'daily-20261103.sqlite3')

    def test_daily_backup_failure_keeps_previous_and_cleans_temporary(self):
        folder=Path(self.temp.name)/'backups'
        with patch('core.datetime') as clock:
            clock.now.side_effect=[datetime(2026,11,1,tzinfo=timezone.utc),datetime(2026,11,2,tzinfo=timezone.utc)]
            self.store.backup_daily()
            previous=folder/'daily-20261101.sqlite3'
            previous_bytes=previous.read_bytes()
            (folder/'daily-20261101.sqlite3-wal').write_bytes(b'old wal')
            (folder/'daily-20261101.sqlite3-shm').write_bytes(b'old shm')
            with patch.object(self.store,'_write_backup',side_effect=sqlite3.DatabaseError('interrupted')):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.store.backup_daily()
        self.assertEqual(previous.read_bytes(),previous_bytes)
        self.assertTrue((folder/'daily-20261101.sqlite3-wal').exists())
        self.assertTrue((folder/'daily-20261101.sqlite3-shm').exists())
        self.assertFalse((folder/'daily-20261102.sqlite3').exists())
        self.assertEqual(list(folder.glob('*.tmp')),[])

    def test_daily_backup_rotation_removes_old_database_sidecars_after_success(self):
        folder=Path(self.temp.name)/'backups'
        with patch('core.datetime') as clock:
            clock.now.side_effect=[datetime(2026,11,1,tzinfo=timezone.utc),datetime(2026,11,2,tzinfo=timezone.utc)]
            self.store.backup_daily()
            (folder/'daily-20261101.sqlite3-wal').write_bytes(b'old wal')
            (folder/'daily-20261101.sqlite3-shm').write_bytes(b'old shm')
            self.store.backup_daily()
        self.assertFalse((folder/'daily-20261101.sqlite3').exists())
        self.assertFalse((folder/'daily-20261101.sqlite3-wal').exists())
        self.assertFalse((folder/'daily-20261101.sqlite3-shm').exists())

    def test_daily_backup_does_not_duplicate_same_day(self):
        with patch('core.datetime') as clock, patch.object(self.store,'_write_backup',wraps=self.store._write_backup) as write:
            clock.now.side_effect=[datetime(2026,11,1,tzinfo=timezone.utc),datetime(2026,11,1,tzinfo=timezone.utc)]
            self.assertTrue(self.store.backup_daily())
            self.assertTrue(self.store.backup_daily())
        write.assert_called_once()
        self.assertEqual(len(list((Path(self.temp.name)/'backups').glob('daily-*.sqlite3'))),1)
        with self.store.db() as db:
            status=json.loads(db.execute("SELECT value FROM meta WHERE key='daily_backup_status'").fetchone()[0])
        self.assertEqual(status['status'],'created')

    def test_daily_backup_respects_projected_twenty_percent_reserve(self):
        with self.store.db() as db:
            size=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
        with patch('core.shutil.disk_usage',return_value=SimpleNamespace(total=1000,free=200+size-1)):
            self.assertFalse(self.store.backup_daily())
        self.assertEqual(list((Path(self.temp.name)/'backups').glob('daily-*.sqlite3')),[])
        with self.store.db() as db:
            state=json.loads(db.execute("SELECT value FROM meta WHERE key='daily_backup_status'").fetchone()[0])
        self.assertEqual(state['status'],'skipped_low_disk')
        self.assertEqual(state['required_bytes'],size)
        self.assertEqual(state['free_bytes'],200+size-1)
        self.assertEqual(state['minimum_free_bytes'],200)

    def test_disabled_source_not_claimed(self):
        self.config['sources'][0]['enabled']=False
        self.assertIsNone(self.store.claim(self.config))

    def test_domain_interval(self):
        with self.store.db() as db:
            db.execute('UPDATE source_state SET next_allowed=?',(time.time()+100,))
        self.assertIsNone(self.store.claim(self.config))

    def test_compact_recommendation_api(self):
        job=self.store.claim(self.config)
        facts=self.facts|{'related_links':[{'url':'https://example.com/'+str(n)} for n in range(20)]}
        self.store.save(job,facts,'evidence','html',self.config)
        result=query_api(self.store,self.config,'/api/recommendation-candidates',{})
        self.assertEqual(len(result['items'][0]['related_links']),3)
        self.assertNotIn('related_links',result['items'][0]['facts'])

    def test_record_pagination_metadata_and_search(self):
        with self.store.db() as db:
            for n in range(65):
                r={'id':str(n).zfill(3),'title':'Record '+str(n),'catalog':'CAT'+str(n),'label':'Test Label','releaseYear':1990,'sources':[]}
                db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',(r['id'],r['title'],r['catalog'],r['title'],r['catalog'],json.dumps(r)))
        first=query_api(self.store,self.config,'/api/records',{})
        second=query_api(self.store,self.config,'/api/records',{'offset':['30']})
        last=query_api(self.store,self.config,'/api/records',{'offset':['60']})
        self.assertEqual(first['total'],65)
        self.assertEqual(second['items'][0]['id'],'030')
        self.assertEqual(last['items'][0]['releaseYear'],1990)
        self.assertFalse(last['has_more'])
        self.assertEqual(query_api(self.store,self.config,'/api/records',{'q':['Test Label']})['total'],65)

    def test_articles_not_price_denominator(self):
        job=self.store.claim(self.config)
        self.store.save(job,self.facts|{'page_type':'discovery'},'article','html',self.config)
        self.assertEqual(query_api(self.store,self.config,'/api/candidates',{})['total'],0)
        self.assertEqual(query_api(self.store,self.config,'/api/articles',{})['total'],1)
        self.assertEqual(query_api(self.store,self.config,'/api/status',{})['coverage']['observed_products'],0)

    def test_news_expands_registered_products_only(self):
        self.config['sources'][0]['accept_article_links']=True
        self.store.discover({'kind':'discovery','discover':False},[('https://example.com/p2','vinyl'),('https://evil.com/p3','vinyl')],self.config)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],2)

    def test_unvisited_before_refresh(self):
        with self.store.db() as db:
            db.execute('UPDATE jobs SET last_checked=?,due=0',(time.time()-7200,))
            db.execute('INSERT INTO jobs(url,source,due) VALUES(?,?,?)',('https://example.com/new','test',1))
        self.assertEqual(self.store.claim(self.config)['url'],'https://example.com/new')


if __name__ == '__main__':
    unittest.main()
