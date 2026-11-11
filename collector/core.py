"""Local, conservative record discovery. No LLM calls and no master writes."""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, quote
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, FeatureNotFound

ROOT = Path(__file__).resolve().parent.parent
MIN_FREE_SPACE_RATIO = 0.10
ARCHIVE = ROOT / 'outputs/game-record-audit-2026-08-25/records-dashboard'
DATA = Path(os.environ.get('GRT_DATA_DIR') or ROOT / 'data') / 'collector'
AGENT = 'VGMVinylCollector/1.0'
MAX_BYTES = 4 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat()


def norm(value):
    import unicodedata
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('unsupported URL')
    if p.port not in (None, 80, 443):
        raise ValueError('nonstandard port')
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', p.query, ''))


def public_target(url, hosts):
    url = canonical(url)
    p = urlsplit(url)
    if p.hostname not in hosts:
        raise ValueError('host not registered')
    addresses = {x[4][0] for x in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('non-public address rejected')
    return p, sorted(addresses)[0]


def fetch_http(url, hosts):
    """Pin the validated IP; validate every redirect; no environment proxies."""
    for _ in range(5):
        p, ip = public_target(url, hosts)
        port = p.port or (443 if p.scheme == 'https' else 80)
        cls = http.client.HTTPSConnection if p.scheme == 'https' else http.client.HTTPConnection
        con = cls(p.hostname, port, timeout=25)
        con._create_connection = lambda address, timeout, source_address=None, ip=ip, port=port: socket.create_connection((ip, port), timeout, source_address)
        try:
            con.request('GET', p.path + ('?' + p.query if p.query else ''), headers={'User-Agent': AGENT, 'Accept': 'text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.1', 'Accept-Encoding': 'identity'})
            response = con.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('redirect without location')
                url = canonical(urljoin(url, location))
                continue
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('response too large')
            if response.getheader('Content-Encoding', 'identity') not in ('', 'identity'):
                raise ValueError('unexpected compression')
            content_type = response.getheader('Content-Type', '')
            charset = re.search(r'charset=["\']?([\w-]+)', content_type)
            encoding = charset.group(1) if charset else 'utf-8'
            return response.status, raw.decode(encoding, errors='replace'), url
        finally:
            con.close()
    raise ValueError('too many redirects')


class Robots:
    def __init__(self):
        self.cache = {}

    def check(self, url, hosts):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        cached = self.cache.get(origin)
        if not cached or cached[0] < time.time() - 3600:
            status, body, _ = fetch_http(origin + '/robots.txt', hosts)
            if status == 404:
                body = 'User-agent: *\nAllow: /'
            elif status != 200:
                raise PermissionError(f'robots unavailable HTTP {status}')
            parser = RobotFileParser()
            parser.parse(body.splitlines())
            self.cache[origin] = (time.time(), parser)
        parser = self.cache[origin][1]
        if not parser.can_fetch(AGENT, url):
            raise PermissionError('robots disallow')
        return max(parser.crawl_delay(AGENT) or 0, parser.crawl_delay('*') or 0)


async def fetch_browser(url, hosts):
    from playwright.async_api import async_playwright
    # Only explicitly registered public hosts, fresh ephemeral browser profile.
    pinned = {}
    for host in hosts:
        _, ip = public_target('https://' + host, hosts)
        pinned[host] = ip
    rules = ', '.join(f'MAP {host} {ip}' for host, ip in pinned.items())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=[f'--host-resolver-rules={rules}', '--disable-quic', '--disable-background-networking'])
        try:
            context = await browser.new_context(service_workers='block')
            async def reject_socket(ws):
                await ws.close()
            await context.route_web_socket('**/*', reject_socket)
            async def route_handler(route):
                target = route.request.url
                try:
                    p = urlsplit(canonical(target))
                    if p.hostname not in pinned or route.request.method not in ('GET', 'HEAD'):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    await route.abort()
            await context.route('**/*', route_handler)
            page = await context.new_page()
            response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            if response is None:
                raise RuntimeError('browser navigation returned no response')
            await page.wait_for_timeout(1000)
            final = page.url
            public_target(final, hosts)
            html = await page.content()
            if len(html.encode('utf-8')) > MAX_BYTES:
                raise ValueError('rendered response too large')
            return response.status, html, final
        finally:
            await browser.close()


def json_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from json_nodes(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from json_nodes(item)


def watch_matches(config, title, catalog=None):
    key = norm(title) + ' ' + norm(catalog)
    return [w['record_id'] for w in config.get('watches', [])
            if any(all(norm(t) and norm(t) in key for t in group) for group in w['terms'])]


def auction_links(html, config, *, filter_watches=True):
    soup = BeautifulSoup(html, 'html.parser')
    script = soup.select_one('#__NEXT_DATA__')
    props = json.loads(script.string)['props'] if script and script.string else {}
    state = props.get('initialState') or props.get('pageProps', {}).get('initialState', {})
    rows = state.get('search', {}).get('items', {}).get('listing', {}).get('items')
    if not isinstance(rows, list):
        raise ValueError('Yahoo search listing data unavailable')
    return [('https://auctions.yahoo.co.jp/jp/auction/'+r['auctionId'], r['title'])
            for r in rows if isinstance(r,dict) and not r.get('isFleamarketItem') and not r.get('isShoppingItem')
            and re.fullmatch(r'[a-z0-9]+',str(r.get('auctionId','')))
            and (not filter_watches or watch_matches(config,r.get('title','')))]


def extract(html, url, kind, selectors=None):
    is_feed = '<rss' in html[:1000] or '<feed' in html[:1000]
    if is_feed:
        try:
            soup = BeautifulSoup(html, 'xml')
        except FeatureNotFound:
            soup = BeautifulSoup(html, 'html.parser')
    else:
        soup = BeautifulSoup(html, 'html.parser')
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes.extend(json_nodes(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError, RecursionError):
            pass
    products = [n for n in nodes if 'Product' in (n.get('@type') if isinstance(n.get('@type'), list) else [n.get('@type')])]
    def same_product(target):
        return urlsplit(urljoin(url, str(target or ''))).path.rstrip('/') == urlsplit(url).path.rstrip('/')
    groups = [n for n in nodes if n.get('@type') == 'ProductGroup' and same_product(n.get('url') or n.get('@id'))]
    if len(groups) == 1 and isinstance(groups[0].get('hasVariant'), list):
        group = groups[0]
        grouped_offers = []
        for variant in group['hasVariant']:
            offer = variant.get('offers')
            if isinstance(offer, dict):
                grouped_offers.append(offer | {'name': variant.get('name') or variant.get('sku')})
        if grouped_offers:
            products = [group | {'@type':'Product','offers':grouped_offers}]
    elif len(products) > 1:
        matching = [p for p in products if (p.get('url') or p.get('@id')) and same_product(p.get('url') or p.get('@id'))]
        if len(matching) == 1:
            products = matching
    # Older Shopify themes expose product JSON plus an explicit currency meta tag.
    if not products and kind == 'product':
        currency_tag = soup.select_one('meta[property="og:price:currency"],meta[property="product:price:currency"]')
        currency = currency_tag.get('content','').upper() if currency_tag else ''
        for script in soup.select('script[type="application/json"][id^="ProductJson"],script[type="text/template"]'):
            try:
                product = json.loads(script.string or script.get_text())
                if isinstance(product,dict) and isinstance(product.get('product'),dict):
                    product=product['product']
                if not isinstance(product,dict) or not same_product('/products/'+str(product.get('handle',''))) or not re.fullmatch('[A-Z]{3}',currency):
                    continue
                offers=[]
                for v in product.get('variants',[]):
                    offers.append({'@type':'Offer','name':v.get('name') or v.get('title'),
                                   'price':str(Decimal(str(v['price']))/100),'priceCurrency':currency,
                                   'availability':'https://schema.org/'+('InStock' if v.get('available') else 'OutOfStock'),
                                   'url':url.split('?')[0]+'?variant='+str(v['id'])})
                products=[{'@type':'Product','name':product.get('title'),'description':product.get('description'),
                           'brand':product.get('vendor'),'offers':offers}]
                break
            except (ValueError,TypeError,KeyError,InvalidOperation):
                continue
    title = next((tag.get_text(' ',strip=True) for tag in [soup.select_one('.entry-title'),soup.h1,soup.title] if tag and tag.get_text(' ',strip=True)),url)[:500]
    links = [(urljoin(url, a.get('href', '')), a.get_text(' ', strip=True)[:300]) for a in soup.select('a[href]')]
    # RSS/Atom discovery avoids interpreting link elements as HTML anchors.
    if '<rss' in html[:1000] or '<feed' in html[:1000]:
        try:
            root = ET.fromstring(html)
            for entry in list(root.findall('.//item')) + list(root.findall('.//{http://www.w3.org/2005/Atom}entry')):
                for link in entry:
                    if link.tag.split('}')[-1] == 'link':
                        target = link.get('href') or link.text
                        if target:
                            links.append((urljoin(url, target.strip()), 'feed item'))
        except ET.ParseError:
            pass
    selected = {}
    for name in ('price','buy_button','model'):
        selector = (selectors or {}).get(name)
        if selector:
            elements = soup.select(selector)
            if len(elements) == 1:
                element = elements[0]
                selected[name] = {'selector':selector,'text':element.get_text(' ',strip=True)[:500], 'disabled':element.has_attr('disabled') or element.get('aria-disabled')=='true'}
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
        tag.decompose()
    text = ' '.join(soup.get_text(' ', strip=True).split())
    facts = {'title': title, 'catalog': None, 'price': None, 'currency': None,
             'availability': 'UNKNOWN', 'shipping': None, 'extraction': 'text_only',
             'page_type': 'feed' if is_feed else kind,
             'extractor_version': 3,
             'identity_verified': False, 'review_flags': []}
    related = []
    for target,label in links:
        try:
            p = urlsplit(canonical(target))
            if p.hostname != urlsplit(url).hostname and p.hostname not in ('localhost',) and not re.search(r'facebook|twitter|instagram|pinterest|wordpress',p.hostname):
                related.append({'url':canonical(target),'label':label})
        except ValueError:
            pass
    facts['related_links'] = list({x['url']:x for x in related}.values())[:20]
    if len(products) == 1 and kind == 'product':
        product = products[0]
        facts['title'] = str(product.get('name') or title)[:500]
        # SKU is a store identifier, never assumed to be a catalog number.
        facts['store_sku'] = str(product.get('sku') or '')[:150] or None
        description = str(product.get('description') or '')
        facts['description'] = (BeautifulSoup(description, 'html.parser').get_text(' ', strip=True) if '<' in description else description)[:2400]
        facts['brand'] = product.get('brand', {}).get('name') if isinstance(product.get('brand'), dict) else product.get('brand')
        facts['extraction'] = 'single_product_jsonld'
        offers = product.get('offers', [])
        offers = offers if isinstance(offers, list) else [offers]
        original_offer_count = len(offers)
        offers = [o for o in offers if not isinstance(o,dict) or not re.search(r'\b(?:cassette|mini\s?disc|compact disc|CD|flexi|t-shirt|slipmat)\b',str(o.get('name','')),re.I)]
        if len(offers) != original_offer_count:
            facts['review_flags'].append('non_vinyl_variants_excluded')
        if re.search(r'\bpre[- ]?order\b',facts.get('description',''),re.I):
            facts['review_flags'].append('preorder_description')
        if len(offers) == 1 and isinstance(offers[0], dict) and offers[0].get('@type') == 'Offer':
            offer = offers[0]
            if offer.get('priceValidUntil'):
                try:
                    end = datetime.fromisoformat(str(offer['priceValidUntil']).replace('Z','+00:00'))
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=timezone.utc)
                    if end.date() < datetime.now(timezone.utc).date():
                        facts['review_flags'].append('structured_offer_expired')
                except ValueError:
                    facts['review_flags'].append('invalid_offer_expiry')
            try:
                price = Decimal(str(offer.get('price')))
                currency = str(offer.get('priceCurrency', '')).upper()
                if price.is_finite() and price >= 0 and re.fullmatch('[A-Z]{3}', currency):
                    facts['price'], facts['currency'] = str(price), currency
            except InvalidOperation:
                pass
            state = str(offer.get('availability', '')).rsplit('/', 1)[-1]
            facts['availability'] = {'InStock': 'IN_STOCK_STRUCTURED', 'OutOfStock': 'SOLD_OUT_STRUCTURED', 'SoldOut': 'SOLD_OUT_STRUCTURED', 'PreOrder': 'PREORDER_STRUCTURED'}.get(state, 'UNKNOWN')
            stock_text = facts.get('description') or text
            if facts['availability'] == 'IN_STOCK_STRUCTURED' and re.search(r'sold out|out of stock|品切れ|売り切れ', stock_text, re.I):
                facts['availability'] = 'CONFLICT'
                facts['review_flags'].append('stock_text_conflict')
            elif facts['availability'] == 'IN_STOCK_STRUCTURED' and 'preorder_description' in facts['review_flags']:
                facts['availability'] = 'PREORDER_STRUCTURED'
        elif len(offers) > 1 and all(isinstance(o, dict) and o.get('@type') == 'Offer' for o in offers):
            variants = []
            for offer in offers:
                try:
                    price = Decimal(str(offer.get('price')))
                    currency = str(offer.get('priceCurrency', '')).upper()
                    if not price.is_finite() or price < 0 or not re.fullmatch('[A-Z]{3}', currency):
                        continue
                    expiry = str(offer.get('priceValidUntil') or '')
                    if expiry and expiry[:10] < datetime.now(timezone.utc).date().isoformat():
                        continue
                    variants.append({'title': str(offer.get('name') or offer.get('sku') or '版名不明')[:200],
                                     'price': str(price), 'currency': currency,
                                     'availability': str(offer.get('availability', '')).rsplit('/', 1)[-1] or 'UNKNOWN',
                                     'url': urljoin(url, str(offer.get('url') or url))})
                except InvalidOperation:
                    continue
            facts['variants'] = variants
            if len(variants) == len(offers) and len({v['currency'] for v in variants}) == 1:
                prices = [Decimal(v['price']) for v in variants]
                facts['price'], facts['price_max'] = str(min(prices)), str(max(prices))
                facts['currency'] = variants[0]['currency']
                facts['price_kind'] = 'variant_range_not_identical_edition'
                facts['review_flags'].append('select_variant_before_comparing')
            else:
                facts['review_flags'].append('incomplete_variant_prices')
        else:
            facts['review_flags'].append('multiple_or_aggregate_offers_not_priced')
    elif len(products) > 1:
        facts['review_flags'].append('multiple_products_not_priced')
    if 'structured_offer_expired' in facts['review_flags']:
        facts['structured_price'] = facts['price']
        facts['price'] = None
        facts['availability'] = 'UNKNOWN'
    if selected and kind == 'product':
        facts['selected_evidence'] = selected
        if 'model' in selected:
            facts['title'] = selected['model']['text'].removeprefix('型番：').strip()
            pattern = (selectors or {}).get('catalog_pattern')
            found = re.findall(pattern, facts['title']) if pattern else []
            if len(found) == 1:
                facts['catalog'] = found[0]
        if 'price' in selected:
            price_text = selected['price']['text']
            match = re.fullmatch(r'\s*[£$¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)\s*(?:円)?\s*',price_text)
            if match:
                observed_price = str(Decimal(match.group(1).replace(',','')))
                structured = facts.get('structured_price',facts['price'])
                if structured is not None and Decimal(structured) != Decimal(observed_price):
                    facts['review_flags'].append('structured_vs_displayed_price_difference')
                facts['structured_price'] = structured
                facts['price'] = observed_price
                facts['currency'] = selectors['currency']
                facts['price_kind'] = selectors.get('price_kind','item_displayed')
                facts['extraction'] = 'site_specific_selector'
        if 'buy_button' in selected:
            button = selected['buy_button']
            if re.search('sold out|out of stock|売り切れ|品切れ',button['text'],re.I):
                facts['availability'] = 'SOLD_OUT_DISPLAYED'
            elif button['disabled']:
                facts['availability'] = 'UNKNOWN'
            else:
                facts['availability'] = 'PURCHASE_BUTTON_OBSERVED'
    if re.search(r'captcha|verify you are human|access denied|just a moment', title, re.I):
        raise PermissionError('challenge page')
    if re.search(r'trouble connecting|接続エラー', text, re.I):
        facts['availability'] = 'UNKNOWN'
        facts['review_flags'].append('connection_error_in_page')
    if kind == 'product' and re.search(r'\b(?:CD|cassette|mini\s?disc|flexi|t-shirt|slipmat|art book|plush|PDF)\b',facts['title'],re.I) and not re.search(r'\b(?:vinyl|\d*x?LP|12.inch|7.inch)\b',facts['title'],re.I):
        facts['page_type']='excluded_media'
        facts['review_flags'].append('not_vinyl_product')
    if urlsplit(url).hostname == 'auctions.yahoo.co.jp' and kind == 'product':
        # Auction observations are not fixed-price offers.
        offer = products[0].get('offers') if len(products) == 1 else None
        end = None
        if isinstance(offer, dict) and offer.get('priceValidUntil'):
            try:
                dt = datetime.fromisoformat(offer['priceValidUntil'])
                if dt.tzinfo is not None:
                    end = dt.timestamp()
            except (TypeError,ValueError):
                pass
        facts['auction_end'] = end
        facts['auction_price'] = facts['price'] or facts.get('structured_price')
        facts['price_kind'] = 'auction_current_bid'
        if 'このオークションは終了しています' in text or (end is not None and end <= time.time()):
            facts.update(availability='AUCTION_ENDED',price=None)
        elif end is not None and facts['price'] is not None:
            facts['availability'] = 'AUCTION_OPEN'
        if re.search(r'CD.{0,40}(?:ありません|欠品)', facts.get('description',''), re.I | re.S):
            facts['review_flags'].append('incomplete_set')
    # No free-text regex price guess: avoids buyback/list-price/shipping confusion.
    return facts, text[:24000], links


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,title TEXT,catalog TEXT,norm_title TEXT,norm_catalog TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS jobs(url TEXT PRIMARY KEY,source TEXT,due REAL,status TEXT DEFAULT 'pending',lease REAL DEFAULT 0,attempts INTEGER DEFAULT 0,last_error TEXT,last_checked REAL);
            CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,url TEXT,source TEXT,observed REAL,expires REAL,hash TEXT,facts_hash TEXT,facts TEXT,excerpt TEXT,raw BLOB);
            CREATE INDEX IF NOT EXISTS obs_url ON observations(url,id);
            CREATE TABLE IF NOT EXISTS candidates(url TEXT PRIMARY KEY,title TEXT,first_seen REAL,last_seen REAL,status TEXT DEFAULT 'pending_verification');
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,url TEXT,created REAL,kind TEXT,detail TEXT);
            CREATE INDEX IF NOT EXISTS watch_event_url ON events(url) WHERE kind='watch_new';
            CREATE TABLE IF NOT EXISTS source_state(id TEXT PRIMARY KEY,next_allowed REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS history_imports(name TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS market_search_targets(search_url TEXT NOT NULL,edition_id TEXT NOT NULL,query TEXT NOT NULL,mode TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'queued',last_checked REAL,last_error TEXT,PRIMARY KEY(search_url,edition_id));
            CREATE INDEX IF NOT EXISTS market_search_target_url ON market_search_targets(search_url);
            CREATE TABLE IF NOT EXISTS market_search_coverage(edition_id TEXT PRIMARY KEY,status TEXT NOT NULL,checked_at REAL,last_error TEXT);
            CREATE TABLE IF NOT EXISTS market_search_matches(search_url TEXT NOT NULL,listing_url TEXT NOT NULL,edition_id TEXT NOT NULL,checked_at REAL NOT NULL,PRIMARY KEY(search_url,listing_url,edition_id));
            CREATE INDEX IF NOT EXISTS market_search_match_listing ON market_search_matches(listing_url,checked_at);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def backup_daily(self):
        day = datetime.now(timezone.utc).strftime('%Y%m%d')
        with self.db() as db:
            last = db.execute("SELECT value FROM meta WHERE key='daily_backup'").fetchone()
            if last and json.loads(last['value']) == day:
                return
        folder = self.path.parent/'backups'
        folder.mkdir(exist_ok=True)
        target = folder/f'daily-{day}.sqlite3'
        with self.db() as db:
            backup = sqlite3.connect(target)
            try:
                db.backup(backup)
            finally:
                backup.close()
        self.meta('daily_backup',day)
        # Keep two daily restore points; manual backups stay outside this rotation.
        backups = sorted(p for p in folder.glob('daily-*.sqlite3') if re.fullmatch(r'daily-\d{8}\.sqlite3',p.name))
        for old in backups[:-2]:
            if old.resolve().parent == folder.resolve():
                old.unlink()

    def backup_manual(self):
        folder = self.path.parent/'backups'
        folder.mkdir(exist_ok=True)
        target = folder/('collector-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
        temporary = target.with_suffix('.tmp')
        backup = sqlite3.connect(temporary)
        try:
            with self.db() as db:
                db.backup(backup)
            if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('manual backup failed quick_check')
        except Exception:
            backup.close()
            temporary.unlink(missing_ok=True)
            raise
        backup.close()
        temporary.replace(target)
        backups = sorted(p for p in folder.glob('collector-*.sqlite3') if re.fullmatch(r'collector-\d{8}T\d{12}Z\.sqlite3', p.name))
        for old in backups:
            if old != target and old.resolve().parent == folder.resolve():
                old.unlink()
        return target

    def initialize(self, config, bot_db_path=None):
        seed_records = json.loads((ROOT / 'seed' / 'records.json').read_text(encoding='utf-8'))
        watched_ids = set()
        if bot_db_path and Path(bot_db_path).is_file():
            try:
                uri=Path(bot_db_path).resolve().as_uri()+'?mode=ro'
                with closing(sqlite3.connect(uri,uri=True,timeout=5)) as bot:
                    watched_ids={row[0] for row in bot.execute('SELECT id FROM editions WHERE watch_level>0')}
            except sqlite3.Error:
                watched_ids=set()
        now=time.time()
        with self.db() as db:
            if not db.execute('SELECT 1 FROM records LIMIT 1').fetchone():
                db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?)', [(r['id'],r.get('title'),r.get('catalog'),norm(r.get('title')),norm(r.get('catalog')),json.dumps(r,ensure_ascii=False)) for r in seed_records])
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('import_hash', json.dumps(hashlib.sha256((ROOT / 'seed' / 'records.json').read_bytes()).hexdigest())))
            for source in config['sources']:
                db.execute('INSERT OR IGNORE INTO source_state(id) VALUES(?)', (source['id'],))
                for url in source['seeds']:
                    db.execute('INSERT OR IGNORE INTO jobs(url,source,due) VALUES(?,?,?)', (canonical(url),source['id'],time.time()))
                if source['id']=='yahoo_watch':
                    manual_ids=set()
                    for watch in config.get('watches',[]):
                        record_id=watch.get('record_id')
                        if isinstance(record_id,str) and re.fullmatch(r'AR-\d{6}',record_id):
                            manual_ids.add(record_id)
                        for term in [watch['search'],*watch.get('search_aliases',[])]:
                            url='https://auctions.yahoo.co.jp/search/search?p='+quote(term)
                            db.execute('INSERT OR IGNORE INTO jobs(url,source,due) VALUES(?,?,?)',(url,source['id'],time.time()))
                            if isinstance(record_id,str) and re.fullmatch(r'AR-\d{6}',record_id):
                                db.execute("INSERT INTO market_search_targets(search_url,edition_id,query,mode,priority) VALUES(?,?,?,'watch',1) ON CONFLICT(search_url,edition_id) DO UPDATE SET query=excluded.query,mode=excluded.mode,priority=excluded.priority",(url,record_id,term))
                    normal_index=0
                    watched_index=0
                    normal_gap=max(int(source.get('min_request_gap_seconds',config['min_request_gap_seconds'])),1)
                    watched_gap=max(int(3600/max(config.get('catalog_priority_slots_per_hour',12),1)),normal_gap)
                    for record in seed_records:
                        record_id=record.get('id')
                        if not isinstance(record_id,str) or not re.fullmatch(r'AR-\d{6}',record_id):
                            continue
                        title=' '.join(str(record.get('title') or '').split())
                        catalog=' '.join(str(record.get('catalog') or '').split())
                        if not title and not catalog:
                            db.execute("INSERT INTO market_search_coverage VALUES(?,'no_route',NULL,'title/catalog missing') ON CONFLICT(edition_id) DO UPDATE SET status='no_route',last_error='title/catalog missing'",(record_id,))
                            continue
                        query=' '.join(x for x in (title,catalog) if x)[:220]
                        db.execute("INSERT INTO market_search_coverage VALUES(?,'queued',NULL,NULL) ON CONFLICT(edition_id) DO UPDATE SET status=CASE WHEN market_search_coverage.status='no_route' THEN 'queued' ELSE market_search_coverage.status END,last_error=NULL",(record_id,))
                        if record_id not in manual_ids:
                            url='https://auctions.yahoo.co.jp/search/search?p='+quote(query)
                            is_watched=record_id in watched_ids
                            offset=watched_index*watched_gap if is_watched else normal_index*normal_gap
                            if is_watched: watched_index+=1
                            else: normal_index+=1
                            db.execute('INSERT OR IGNORE INTO jobs(url,source,due) VALUES(?,?,?)',(url,source['id'],now+offset))
                            db.execute("INSERT INTO market_search_targets(search_url,edition_id,query,mode,priority) VALUES(?,?,?,'catalog',?) ON CONFLICT(search_url,edition_id) DO UPDATE SET query=excluded.query,mode=excluded.mode,priority=excluded.priority",(url,record_id,query,int(is_watched)))
                        db.execute("INSERT INTO market_search_coverage VALUES(?,'queued',NULL,NULL) ON CONFLICT(edition_id) DO UPDATE SET status=CASE WHEN market_search_coverage.status='no_route' THEN 'queued' ELSE market_search_coverage.status END,last_error=NULL",(record_id,))
                for path in config.get('catalog_pages',{}).get(source['id'],[]):
                    db.execute('INSERT OR IGNORE INTO jobs(url,source,due) VALUES(?,?,?)', ('https://'+source['hosts'][0]+path,source['id'],time.time()))
            for path in ARCHIVE.glob('discovery-*.json') if ARCHIVE.is_dir() else ():
                db.execute('INSERT OR IGNORE INTO history_imports VALUES(?,?)', (path.name,path.read_text(encoding='utf-8')))

    def claim(self, config):
        enabled = [s['id'] for s in config['sources'] if s.get('enabled')]
        if not enabled:
            return None
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE jobs SET status='pending' WHERE status='running' AND lease<?", (now,))
            count = db.execute('SELECT COUNT(*) FROM jobs WHERE last_checked>?', (now-3600,)).fetchone()[0]
            if count >= config['max_jobs_per_hour']:
                return None
            watched_searches = db.execute("""SELECT COUNT(DISTINCT j.url) FROM jobs j
              JOIN market_search_targets t ON t.search_url=j.url
              WHERE j.last_checked>? AND t.mode='catalog' AND t.priority=1""",(now-3600,)).fetchone()[0]
            exclude_watched_searches = watched_searches >= config.get('catalog_priority_slots_per_hour',12)
            priority = [s['id'] for s in config['sources'] if s.get('priority')] or ['']
            reserved_capacity = count >= config['max_jobs_per_hour'] - config.get('priority_slots_per_hour',0)
            if reserved_capacity:
                enabled = [s for s in enabled if s in priority]
                if not enabled:
                    return None
            query = "SELECT jobs.* FROM jobs JOIN source_state ON source_state.id=jobs.source WHERE status NOT IN ('running','ended','rotated') AND due<=? AND next_allowed<=? AND source IN (" + ','.join('?' for _ in enabled) + ')'
            params=[now,now,*enabled]
            if reserved_capacity and 'yahoo_watch' in enabled:
                yahoo=next(s for s in config['sources'] if s['id']=='yahoo_watch')
                seeds=yahoo.get('seeds',[])
                query += " AND (jobs.source!='yahoo_watch' OR jobs.url IN ("+','.join('?' for _ in seeds)+") OR EXISTS (SELECT 1 FROM market_search_targets t WHERE t.search_url=jobs.url AND t.priority=1) OR EXISTS (SELECT 1 FROM market_search_matches m JOIN market_search_targets t ON t.search_url=m.search_url AND t.edition_id=m.edition_id WHERE m.listing_url=jobs.url AND t.priority=1))"
                params.extend(seeds)
            if exclude_watched_searches:
                query += " AND NOT EXISTS (SELECT 1 FROM market_search_targets t WHERE t.search_url=jobs.url AND t.mode='catalog' AND t.priority=1)"
            query += ' ORDER BY (source IN (' + ','.join('?' for _ in priority) + ')) DESC,COALESCE((SELECT MAX(t.priority) FROM market_search_targets t WHERE t.search_url=jobs.url),0) DESC,(last_checked IS NULL) DESC,due LIMIT 1'
            params.extend(priority)
            row = db.execute(query, params).fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running',lease=?,attempts=attempts+1,last_checked=? WHERE url=?", (now+180,now,row['url']))
                db.execute('UPDATE source_state SET next_allowed=? WHERE id=?', (now+config['min_request_gap_seconds'],row['source']))
                return dict(row)

    def save(self, job, facts, excerpt, html, config):
        now = time.time()
        with self.db() as db:
            candidate_ids=[row[0] for row in db.execute(
                'SELECT DISTINCT edition_id FROM market_search_matches WHERE listing_url=? AND checked_at>=?',
                (job['url'],now-config['freshness_seconds']))]
        if candidate_ids:
            facts['candidate_ids']=sorted(set(facts.get('candidate_ids',[]))|set(candidate_ids))
        payload = json.dumps(facts,sort_keys=True,ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        raw = html.encode('utf-8')
        with self.db() as db:
            prior = db.execute('SELECT id,hash,facts_hash,facts FROM observations WHERE url=? ORDER BY id DESC LIMIT 1', (job['url'],)).fetchone()
            content_hash = hashlib.sha256(raw).hexdigest()
            if prior and prior['facts_hash'] == digest:
                db.execute('UPDATE observations SET observed=?,expires=? WHERE id=?',(now,now+config['freshness_seconds'],prior['id']))
            else:
                db.execute('INSERT INTO observations(url,source,observed,expires,hash,facts_hash,facts,excerpt,raw) VALUES(?,?,?,?,?,?,?,?,?)', (job['url'],job['source'],now,now+config['freshness_seconds'],content_hash,digest,payload,excerpt[:6000],b''))
            if not prior or prior['facts_hash'] != digest:
                kind = 'new_observation' if not prior else 'facts_changed'
                if prior:
                    previous = json.loads(prior['facts'])
                    if facts['currency'] and facts['currency'] == previous.get('currency') and facts['price'] is not None and previous.get('price') is not None and Decimal(facts['price']) < Decimal(previous['price']):
                        kind = 'price_drop_unverified'
                db.execute('INSERT INTO events(url,created,kind,detail) VALUES(?,?,?,?)', (job['url'],now,kind,payload))
            if facts.get('watch_ids') and facts.get('availability') in ('AUCTION_OPEN','IN_STOCK_STRUCTURED','PURCHASE_BUTTON_OBSERVED','PREORDER_STRUCTURED') and not db.execute("SELECT 1 FROM events WHERE url=? AND kind='watch_new'",(job['url'],)).fetchone():
                db.execute('INSERT INTO events(url,created,kind,detail) VALUES(?,?,?,?)', (job['url'],now,'watch_new',payload))
            if facts.get('page_type') not in ('feed','market_search'):
                db.execute('INSERT INTO candidates(url,title,first_seen,last_seen) VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,last_seen=excluded.last_seen', (job['url'],facts['title'],now,now))
            source = next((s for s in config['sources'] if s['id']==job['source']), {})
            state = 'ended' if facts['availability']=='AUCTION_ENDED' else 'ok'
            db.execute("UPDATE jobs SET status=?,due=?,lease=0,attempts=0,last_error=NULL WHERE url=?", (state,now+source.get('interval_seconds',config['interval_seconds']),job['url']))
            # Bounded evidence retention per URL; original archive is never touched.
            db.execute('DELETE FROM observations WHERE url=? AND id NOT IN (SELECT id FROM observations WHERE url=? ORDER BY id DESC LIMIT 30)', (job['url'],job['url']))

    def failed(self, job, error, config):
        blocked = isinstance(error, (PermissionError, ValueError))
        delay = config['interval_seconds'] if blocked else min(3600 * 2 ** min(job['attempts'], 4), 86400)
        now = time.time()
        error_text = str(error)[:500]
        with self.db() as db:
            db.execute("UPDATE jobs SET status=?,due=?,lease=0,last_error=? WHERE url=?", ('blocked' if blocked else 'error', now+delay,error_text,job['url']))
            result='blocked' if blocked else 'error'
            db.execute('UPDATE market_search_targets SET status=?,last_error=? WHERE search_url=?',(result,error_text[:300],job['url']))
            db.execute('UPDATE market_search_coverage SET status=?,last_error=? WHERE edition_id IN (SELECT edition_id FROM market_search_targets WHERE search_url=?)',(result,error_text[:300],job['url']))
            previous = db.execute("SELECT id,detail FROM events WHERE url=? AND kind='fetch_failed' ORDER BY id DESC LIMIT 1",(job['url'],)).fetchone()
            detail={'error':error_text,'repeat_count':1,'last_seen':now}
            if previous:
                try:
                    old=json.loads(previous['detail'])
                except (TypeError,ValueError):
                    old={}
                if isinstance(old,dict) and old.get('error')==error_text:
                    try:
                        count=max(int(old.get('repeat_count',1)),1)
                    except (TypeError,ValueError):
                        count=1
                    detail.update(repeat_count=count+1)
                    db.execute('UPDATE events SET detail=? WHERE id=?',(json.dumps(detail),previous['id']))
                    return
            db.execute('INSERT INTO events(url,created,kind,detail) VALUES(?,?,?,?)', (job['url'],now,'fetch_failed',json.dumps(detail)))

    def save_market_search(self, search_url, links):
        now=time.time()
        with self.db() as db:
            edition_ids=[row[0] for row in db.execute('SELECT edition_id FROM market_search_targets WHERE search_url=?',(search_url,))]
            db.execute('DELETE FROM market_search_matches WHERE search_url=?',(search_url,))
            db.executemany('INSERT OR IGNORE INTO market_search_matches VALUES(?,?,?,?)',
                           ((search_url,url,edition_id,now) for url,_ in links for edition_id in edition_ids))
            db.execute("UPDATE market_search_targets SET status='searched',last_checked=?,last_error=NULL WHERE search_url=?",(now,search_url))
            db.execute("UPDATE market_search_coverage SET status='searched',checked_at=?,last_error=NULL WHERE edition_id IN (SELECT edition_id FROM market_search_targets WHERE search_url=?)",(now,search_url))

    def discover(self, source, links, config):
        # News -> registered merchant products. Never follow arbitrary external hosts.
        if source.get('kind') == 'discovery':
            for merchant in config['sources']:
                if merchant.get('enabled') and merchant.get('accept_article_links'):
                    self.discover(merchant, links, config)
        if not source.get('discover'):
            return
        with self.db() as db:
            yahoo=source['id']=='yahoo_watch'
            max_urls=source.get('max_urls',config['max_urls_per_source'])
            if yahoo:
                count=db.execute("SELECT COUNT(*) FROM jobs j WHERE j.source=? AND j.status NOT IN ('ended','rotated') AND NOT EXISTS (SELECT 1 FROM market_search_targets t WHERE t.search_url=j.url)",(source['id'],)).fetchone()[0]
                budget=100
            else:
                count = db.execute("SELECT COUNT(*) FROM jobs WHERE source=? AND status!='ended'", (source['id'],)).fetchone()[0]
                budget = min(100, max(0, max_urls-count))
            catalog_paths = config.get('catalog_pages',{}).get(source['id'],[])
            for url, _ in sorted(links,key=lambda x:0 if '?page=' in x[0] else 1):
                if budget <= 0:
                    break
                try:
                    url = canonical(url)
                    p = urlsplit(url)
                    if p.hostname not in source['hosts']:
                        continue
                    path=p.path
                    if source.get('accept_article_links'):
                        path=re.sub(r'^/collections/[^/]+/products/', '/products/', path)
                    if path in catalog_paths:
                        params=parse_qs(p.query)
                        if p.query and (set(params)!={'page'} or len(params['page'])!=1 or not params['page'][0].isdigit() or not 1<=int(params['page'][0])<=50):
                            continue
                    elif p.query or not re.search(source['include_path'],path):
                        continue
                    if source.get('accept_article_links'):
                        url=urlunsplit((p.scheme,source['hosts'][0],path,p.query,''))
                    existing=db.execute('SELECT status FROM jobs WHERE url=?',(url,)).fetchone()
                    if existing and existing['status']!='rotated':
                        continue
                    if yahoo and count>=max_urls:
                        victim=db.execute("SELECT j.url FROM jobs j WHERE j.source=? AND j.status NOT IN ('ended','running','rotated') AND NOT EXISTS (SELECT 1 FROM market_search_targets t WHERE t.search_url=j.url) ORDER BY COALESCE(j.last_checked,0),j.due LIMIT 1",(source['id'],)).fetchone()
                        if not victim:
                            break
                        db.execute("UPDATE jobs SET status='rotated',lease=0 WHERE url=?",(victim['url'],))
                        count-=1
                    due=time.time()+config['min_request_gap_seconds']
                    db.execute("INSERT INTO jobs(url,source,due) VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET status='pending',due=excluded.due,lease=0,attempts=0,last_error=NULL WHERE jobs.status='rotated'",(url,source['id'],due))
                    count+=1
                    budget-=1
                except ValueError:
                    continue


class Worker:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.robots = Robots()
        self.storage_paused = None

    def once(self):
        self.store.meta('heartbeat',time.time())
        usage = shutil.disk_usage(self.store.path.parent)
        paused = usage.free < usage.total * MIN_FREE_SPACE_RATIO
        if paused != self.storage_paused:
            self.storage_paused = paused
            try:
                self.store.meta('storage_protection', {
                    'paused': paused,
                    'free_bytes': usage.free,
                    'total_bytes': usage.total,
                    'minimum_free_percent': int(MIN_FREE_SPACE_RATIO * 100),
                })
            except sqlite3.Error:
                pass
        if paused:
            return False
        job = self.store.claim(self.config)
        if not job:
            return False
        source = next(s for s in self.config['sources'] if s['id'] == job['source'])
        try:
            delay = self.robots.check(job['url'],source['hosts'])
            with self.store.db() as db:
                db.execute('UPDATE source_state SET next_allowed=? WHERE id=?', (time.time()+max(delay,source.get('min_request_gap_seconds',self.config['min_request_gap_seconds'])),source['id']))
            if source['method'] == 'playwright':
                status,html,final = asyncio.run(fetch_browser(job['url'],source['hosts']))
            else:
                status,html,final = fetch_http(job['url'],source['hosts'])
            if status in (401,403,429):
                raise PermissionError(f'HTTP {status}')
            if status != 200:
                raise RuntimeError(f'HTTP {status}; not treated as sold out')
            if source['id']=='yahoo_watch' and urlsplit(final).path != urlsplit(job['url']).path:
                raise ValueError('Yahoo redirected away from requested page')
            if source['id']=='yahoo_watch' and urlsplit(job['url']).path=='/search/search':
                with self.store.db() as db:
                    modes={row[0] for row in db.execute('SELECT mode FROM market_search_targets WHERE search_url=?',(job['url'],))}
                links=auction_links(html,self.config,filter_watches='catalog' not in modes)
                self.store.save_market_search(job['url'],links)
                self.store.discover(source,links,self.config)
                self.store.save(job,{'title':'Yahoo!オークション 重要盤検索','page_type':'market_search','price':None,'currency':None,'availability':'UNKNOWN','matching_urls':[url for url,_ in links]},'',html,self.config)
                self.store.meta('last_success',time.time())
                return True
            kind='catalog' if urlsplit(final).path in self.config.get('catalog_pages',{}).get(source['id'],[]) else source['kind']
            facts,text,links = extract(html,final,kind,source.get('selectors'))
            facts['final_url'] = final
            if kind=='product' and facts.get('page_type')=='product':
                ids=watch_matches(self.config,facts['title'],facts.get('catalog'))
                if ids:
                    facts['watch_ids']=ids
            self.store.save(job,facts,text,html,self.config)
            self.store.discover(source,links,self.config)
            self.store.meta('last_success',time.time())
        except Exception as error:
            self.store.failed(job,error,self.config)
        return True
