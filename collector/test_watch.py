import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core import Store, Worker, extract
from server import query_api


class WatchTest(unittest.TestCase):
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
                db.execute("UPDATE jobs SET due=0 WHERE url LIKE '%/search/search?%'")
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
