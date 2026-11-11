import base64
import hashlib
import re
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from core import Store
from server import (CONTENT_SECURITY_POLICY, bot_data, epoch, query_api,
                    static_content_security_policy, static_target,
                    site_content_security_policy)


class BotIntegrationTest(unittest.TestCase):
    def test_site_template_assets_are_allowed_by_dynamic_policy(self):
        template = Path(__file__).resolve().parents[1] / 'site' / 'site_template.html'
        source = template.read_text(encoding='utf-8')
        assets = re.findall(r'<style[^>]*>(.*?)</style>', source, re.S | re.I)
        assets += [body for attrs, body in re.findall(
            r'<script([^>]*)>(.*?)</script>', source, re.S | re.I)
            if not re.search(r'type=["\']application/json["\']', attrs, re.I)]
        hashes = {
            "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
            for body in assets
        }
        self.assertEqual(len(hashes), 3)
        policy = site_content_security_policy(source.encode())
        self.assertTrue(all(digest in policy for digest in hashes))
        self.assertNotIn('unsafe-inline', policy)
        self.assertIn('https://fonts.googleapis.com', policy)
        self.assertIn('https://fonts.gstatic.com', policy)

    def test_generated_site_policy_hashes_executable_inline_assets_only(self):
        source = ('<style>body{\r\ncolor:#111\r\n}</style>'
                  '<script type="application/json">{\r\n"title":"safe"\r\n}</script>'
                  '<script>render()\r\n</script>')
        policy = site_content_security_policy(source.encode())
        style_hash = base64.b64encode(hashlib.sha256(b'body{\ncolor:#111\n}').digest()).decode()
        data_hash = base64.b64encode(hashlib.sha256(b'{\n"title":"safe"\n}').digest()).decode()
        script_hash = base64.b64encode(hashlib.sha256(b'render()\n').digest()).decode()
        self.assertIn("'sha256-" + style_hash + "'", policy)
        self.assertNotIn("'sha256-" + data_hash + "'", policy)
        self.assertIn("'sha256-" + script_hash + "'", policy)
        self.assertNotIn('unsafe-inline', policy)
        with TemporaryDirectory() as folder:
            site = Path(folder) / 'site'
            site.mkdir()
            target = site / 'index.html'
            target.write_bytes(source.encode())
            self.assertEqual(static_content_security_policy(target, site, source.encode()), policy)
            self.assertEqual(static_content_security_policy(Path(folder) / 'collector.html', site,
                              source.encode()), CONTENT_SECURITY_POLICY)

    def test_bot_join_is_read_only_and_hides_stale_price(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'bot.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                    CREATE TABLE editions(id TEXT PRIMARY KEY,title TEXT,catalog TEXT,label TEXT,year INTEGER,format TEXT,country TEXT,game TEXT,discogs_id INTEGER,watch_level INTEGER);
                    CREATE TABLE price_snapshots(edition_id TEXT,source TEXT,taken_at REAL,lowest_price REAL,currency TEXT);
                    INSERT INTO editions VALUES('AR-1','Title','CAT','Label',2000,'LP','JP','Game',123,1);
                    INSERT INTO editions VALUES('AR-2','Old','CAT2','Label',1999,'LP',NULL,'Game',NULL,0);
                    INSERT INTO price_snapshots VALUES('AR-1','discogs','2026-11-09T18:00:00+00:00',12.5,'USD');
                    INSERT INTO price_snapshots VALUES('AR-2','discogs','2026-11-09T17:59:00+00:00',8,'USD');
                ''')
            before = path.read_bytes()
            now = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
            result = bot_data(path, ['AR-1', 'AR-2'], now=now)
            self.assertTrue(result['available'])
            self.assertEqual((result['edition_count'], result['watched_count']), (2, 1))
            self.assertTrue(result['records']['AR-1']['fresh_discogs_price'])
            self.assertTrue(result['records']['AR-1']['discogs_url'].endswith('/123'))
            self.assertFalse(result['records']['AR-2']['fresh_discogs_price'])
            self.assertIsNone(result['records']['AR-2']['lowest_price'])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(epoch('2026-11-10T09:00:00+09:00'), now)
            store = Store(Path(folder) / 'collector.sqlite3')
            store.initialize({'sources': []})
            with store.db() as db:
                db.execute('DELETE FROM records')
                db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',
                           ('AR-1', 'Old title', 'CAT', 'old title', 'cat',
                            '{"id":"AR-1","title":"Old title","sources":[]}'))
            joined = query_api(store, {'sources': []}, '/api/records', {'q': ['Old title']}, path)
            self.assertEqual(joined['items'][0]['bot_master']['title'], 'Title')
            self.assertEqual(joined['items'][0]['title'], 'Old title')

    def test_new_store_seeds_catalog_without_legacy_archive(self):
        with TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'collector.sqlite3')
            store.initialize({'sources': []})
            with store.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 3777)

    def test_unified_server_routes_catalog_and_collector_without_path_escape(self):
        with TemporaryDirectory() as folder:
            site = Path(folder)
            (site / 'index.html').write_text('catalog', encoding='utf-8')
            self.assertEqual(static_target('/', site), (site / 'index.html').resolve())
            self.assertEqual(static_target('/archive/', site), (site / 'index.html').resolve())
            self.assertEqual(static_target('/collector/index.html', site).name, 'index.html')
            self.assertIsNone(static_target('/collector/../../../../Windows/win.ini', site))


    def test_missing_bot_database_is_reported_without_failing(self):
        with TemporaryDirectory() as folder:
            result = bot_data(Path(folder) / 'missing.sqlite3')
            self.assertEqual(result, {'available': False, 'reason': 'Botのデータベースがありません'})
