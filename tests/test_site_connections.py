"""Site audit connection lifetime and ambiguous candidate handling."""
import csv
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import re
import sqlite3
from pathlib import Path
import time
import pytest

@pytest.fixture
def site_fixture(tmp_path, monkeypatch):
    site_dir=Path(__file__).resolve().parents[1]/'site'
    monkeypatch.syspath_prepend(str(site_dir))
    spec=importlib.util.spec_from_file_location('site_audit_test',site_dir/'audit_site.py')
    audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
    site=tmp_path/'site.db';bot=tmp_path/'bot.db'
    c=sqlite3.connect(bot)
    with c:
        c.executescript("""
        create table price_snapshots(edition_id,taken_at,lowest_price,currency,num_for_sale);
        create table editions(id,watch_level);
        """)
    c.close()
    c=sqlite3.connect(site)
    with c:
        c.executescript("""
        create table editions(id text primary key,identity_status,tool_json,muse_json,html_json,game_enrichment_json text default '{}');
        create table observations(matched_edition_id,checked_at,price_usd,source_url);
        create table site_thumbnail_eligible(edition_id,image_url);
        create table offer_candidates(matched_edition_id,listing_url);
        create table official_product_links(catalog_group,official_product_url);
        create table merchant_discovery(catalog_group,official_product_url,last_seen_at);
        create table catalog_lookup_queue(edition_id,field,search_value,label);
        create table catalog_lookup(field,search_value,label,result,checked_at,match_json);
        """)
        c.executemany("insert into editions(id,identity_status,tool_json,muse_json,html_json) values(?, 'shared_catalog_identity', '{}','{}','{}')",
                      ((f'AR-{i:06d}',) for i in range(1,3778)))
    c.close()
    template=tmp_path/'template.html'
    template.write_text('<p class="audit-as-of" id="audit-as-of">監査データ更新：'
                        '@@AUDIT_AS_OF@@ JST</p> @@AUDIT_OVERVIEW@@ '
                        '@@AUDITED_RECORDS@@ @@DISCOVERED_CANDIDATES@@',encoding='utf-8')
    return audit,site,bot,template

def test_audit_closes_database_connections(site_fixture,tmp_path,monkeypatch):
    audit,site,bot,template=site_fixture
    opened=[]
    class Tracked(sqlite3.Connection):
        def close(self):
            opened.append('closed')
            return super().close()
    real_connect=sqlite3.connect
    def connect(*args,**kwargs):
        kwargs['factory']=Tracked
        return real_connect(*args,**kwargs)
    monkeypatch.setattr(audit.sqlite3,'connect',connect)
    audit.run(site,bot,template,tmp_path/'index.html',tmp_path/'audit.csv')
    assert len(opened)==2

def test_collector_candidates_exposes_market_search_coverage(site_fixture,tmp_path):
    audit,_,_,_=site_fixture
    collector=tmp_path/'collector.sqlite3'
    c=sqlite3.connect(collector)
    with c:
        c.executescript("""
        create table observations(id integer primary key,url,source,observed,expires,facts);
        create table jobs(url,last_error);
        create table market_search_coverage(edition_id,status,checked_at,last_error);
        """)
        c.execute("insert into market_search_coverage values('AR-000001','searched',1790000000,null)")
    c.close()
    _,coverage,state=audit.collector_candidates(collector,{'AR-000001':{'title':'Game Vinyl'}},1800000000)
    assert state=='healthy'
    assert coverage['AR-000001']['status']=='searched'
    assert coverage['AR-000001']['error'] is None

def test_same_external_candidate_across_editions_is_flagged(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    shared='00000000-0000-0000-0000-000000000001'
    unique='00000000-0000-0000-0000-000000000002'
    c=sqlite3.connect(site)
    with c:
        c.executemany('insert into catalog_lookup_queue values(?,?,?,?)',[
            ('AR-000001','catno','shared',''),
            ('AR-000002','catno','shared',''),
            ('AR-000003','catno','unique','')])
        c.executemany('insert into catalog_lookup values(?,?,?,?,?,?)',[
            ('catno','shared','','candidates','2026-11-09',json.dumps([{'id':shared}])),
            ('catno','unique','','candidates','2026-11-09',json.dumps([{'id':unique}]))])
    c.close()
    output=tmp_path/'audit.csv'
    audit.run(site,bot,template,tmp_path/'index.html',output)
    with output.open(encoding='utf-8-sig',newline='') as f:
        rows={r['盤ID']:r['監査事項'] for r in csv.DictReader(f)}
    note='同一外部候補が複数盤に共通・版の確認が必要'
    assert note in rows['AR-000001'] and note in rows['AR-000002']
    assert note not in rows['AR-000003']
    html=(tmp_path/'index.html').read_text(encoding='utf-8')
    assert f'https://coverartarchive.org/release/{shared}/front-250' in html
    assert '版一致・利用権未確認' in html

def test_field_evidence_is_visible_as_unconfirmed_and_urls_are_checked(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    c=sqlite3.connect(site)
    with c:
        c.execute("""create table field_review_evidence(
          evidence_id text primary key,edition_id text,field_name text,
          note text,evidence_url text,checked_at text,review_status text)""")
        c.executemany("insert into field_review_evidence values(?,?,?,?,?,?,?)",[
          ('a','AR-000028','販売形態','店舗はイベント限定と記載・未確定',
           'https://example.org/item','2026-11-10','needs_review'),
          ('b','AR-000029','販売形態','不明な出典',
           'https://example.org/item?token=not-for-display','2026-11-10','needs_review'),
          ('c','AR-000030','販売形態','確認済みの記録',
           'https://example.org/accepted','2026-11-10','resolved')])
    c.close()
    target=tmp_path/'index.html';output=tmp_path/'audit.csv'
    audit.run(site,bot,template,target,output)
    with output.open(encoding='utf-8-sig',newline='') as f:
        rows={r['盤ID']:r['監査事項'] for r in csv.DictReader(f)}
    assert '出典の確認待ち（販売形態）: 店舗はイベント限定と記載・未確定' in rows['AR-000028']
    assert '不明な出典' in rows['AR-000029']
    assert '確認済みの記録' not in rows['AR-000030']
    html=target.read_text(encoding='utf-8')
    assert 'https://example.org/item' in html
    assert 'token=not-for-display' not in html
    assert 'https://example.org/accepted' not in html

def test_pending_candidates_are_unconfirmed_and_only_link_to_source_host(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    template.write_text('@@AUDIT_AS_OF@@ @@AUDIT_OVERVIEW@@ @@AUDITED_RECORDS@@ '
                        '<script id="discovery-data" type="application/json">'
                        '@@DISCOVERED_CANDIDATES@@</script>',encoding='utf-8')
    c=sqlite3.connect(bot)
    with c:
        c.executescript("""
        create table sources(name,kind,url);
        create table candidate_editions(id,title,source_url,found_via,first_seen,found_at,status);
        """)
        c.execute('insert into sources values(?,?,?)',(
            'feed','RSS','https://shop.example/collection.atom'))
        c.executemany('insert into candidate_editions values(?,?,?,?,?,?,?)',[
            (1,'Known title','https://shop.example/products/item?track=secret','feed:feed','2026-11-10T01:00:00+09:00',None,'pending'),
            (2,'<img src=x onerror=alert(1)>','https://evil.example/item','feed:feed','2026-11-10T01:01:00+09:00',None,'pending'),
            (3,'Resolved title','https://shop.example/products/old','feed:feed','2026-11-10T01:02:00+09:00',None,'approved')])
    c.close()
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    text=target.read_text(encoding='utf-8')
    match=re.search(r'<script id="discovery-data" type="application/json">(.*?)</script>',text)
    data=json.loads(match.group(1))
    assert data['count']==2
    items={x['title']:x for x in data['items']}
    assert items['Known title']['url']=='https://shop.example/products/item'
    assert items['<img src=x onerror=alert(1)>']['url'] is None
    assert '\\u003cimg src=x onerror=alert(1)' in match.group(1)
    assert '<img src=x onerror=alert(1)>' not in text

def test_catalogue_price_card_displays_evidence_date():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert 'var priceDate = r.fieldAudit.priceDate;' in source
    assert '"確認日不明"' in source
    assert 'class="asof"' in source

def test_catalog_audit_reports_host_cooldown(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv',catalog_result={
        'status':'cooldown','reason':'http_503','attempted':0,'failed':0,'selected':71})
    assert 'MusicBrainz: 直近のhttp_503後の6時間休止中・今回未照会 71件。' in target.read_text(encoding='utf-8')

def test_catalogue_has_accessible_results_heading_contrast_and_description():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert '<meta name="description" content="ゲーム音楽レコード3,777盤' in source
    assert '<main aria-labelledby="edition-results-heading">' in source
    assert '<h2 class="visually-hidden" id="edition-results-heading">収録盤の検索結果</h2>' in source
    assert '.view-tab .tab-count { color: #ffab95; }' in source
    assert '--muted: #5f646a;' in source
    assert '.rarity.r4 { background: #f6ead0; color: #865900; }' in source

def test_catalogue_audit_details_and_search_scope_are_accessible_and_transparent():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert '<details class="audit-disclosure">' in source
    assert '<summary>監査の詳細（取得範囲・確認状態）</summary>' in source
    assert '<p class="audit-overview" id="audit-overview">@@AUDIT_OVERVIEW@@</p>' in source
    assert 'placeholder="タイトル / ゲーム / 型番 / 調査メモなどで検索"' in source
    assert 'aria-label="タイトル、ゲーム、メーカー、型番、レーベル、アーティスト、同定根拠、監査注記、種別根拠で検索"' in source
    for key in ('title', 'game', 'maker', 'catalog', 'label', 'artist', 'auditBasis', 'auditNote', 'mtypeBasis'):
        assert 'key: "' + key + '"' in source
    assert 'function searchHitFields(r)' in source
    assert '検索一致：' in source

def test_unverified_survey_marketplace_prices_are_not_published(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    c=sqlite3.connect(site)
    with c:
        c.execute("update editions set html_json=? where id='AR-000001'", (
            json.dumps({'title':'Example','price':99.5,'median':120.0,
                        'lastSold':'2026-10-01','status':'CURRENT_CONFIRMED'}),))
    c.close()
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    text=target.read_text(encoding='utf-8')
    records_start=text.index('[{')
    records,_=json.JSONDecoder().raw_decode(text,records_start)
    record=next(row for row in records if row['id']=='AR-000001')
    assert record['price'] is None and record['median'] is None and record['lastSold'] is None
    assert record['fieldAudit']['priceDate'] is None
    assert '価格表示停止中' in record['fieldAudit']['priceStatus']
    assert '共有カタログIDで結合（独立した盤同定は未確認）' in record['fieldAudit']['summary']
    assert '共有カタログIDによる結合は、盤の独立照合を意味しません。' in text
    template_source=(Path(__file__).resolve().parents[1]/'site'/'site_template.html').read_text(encoding='utf-8')
    assert '価格は出典・通貨・利用条件の再確認まで非表示です。' in template_source

def test_each_record_gets_manual_marketplace_search_links_labeled_unverified(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    with sqlite3.connect(site) as db:
        db.execute("update editions set html_json=? where id='AR-000001'",
                   (json.dumps({'title':'Example soundtrack','catalog':'VG-123'}),))
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    text=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(text[text.index('[{'):])
    record=next(row for row in records if row['id']=='AR-000001')
    links=record['fieldAudit']['links']
    expected={
        'ブックオフで探す（在庫未確認）':'https://shopping.bookoff.co.jp/search/keyword/',
        'オフモールで探す（在庫未確認）':'https://netmall.hardoff.co.jp/cate/000100150001/?q=',
        'Yahoo!ショッピングで探す（在庫未確認）':'https://shopping.yahoo.co.jp/search?p=',
        'Yahoo!オークションで探す（在庫未確認）':'https://auctions.yahoo.co.jp/search/search?p=',
        'メルカリで探す（在庫未確認）':'https://jp.mercari.com/search?keyword=',
        '楽天市場で探す（在庫未確認）':'https://search.rakuten.co.jp/search/mall/?sitem=',
        '駿河屋で探す（在庫未確認）':'https://www.suruga-ya.jp/search?search_word=',
        'eBayで探す（在庫未確認）':'https://www.ebay.com/sch/i.html?_nkw=',
    }
    for label,prefix in expected.items():
        link=next(item for item in links if item['label']==label)
        assert link['url'].startswith(prefix)
        assert 'Example' in link['url']
    bookoff=next(item for item in links if item['label']=='ブックオフで探す（在庫未確認）')
    assert 'Example%20soundtrack' in bookoff['url']
    assert 'VG-123' in bookoff['url']
    offmall=next(item for item in links if item['label']=='オフモールで探す（在庫未確認）')
    assert 'Example%20soundtrack' in offmall['url']
    assert 'VG-123' in offmall['url']
    empty=next(row for row in records if row['id']=='AR-000002')
    assert not any(item['label'] in expected for item in empty['fieldAudit']['links'])

def test_discogs_observation_display_expires_after_six_hours(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    now=datetime.now(timezone.utc)
    c=sqlite3.connect(bot)
    with c:
        c.executemany("insert into price_snapshots values(?,?,?,?,?)",[
            ('AR-000001',(now-timedelta(hours=5,minutes=59)).isoformat(),12.0,'USD',1),
            ('AR-000002',(now-timedelta(hours=6,minutes=1)).isoformat(),12.0,'USD',1),
        ])
    c.close()
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    text=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(text[text.index('[{'):])
    records={r['id']:r for r in records}
    assert records['AR-000001']['fieldAudit']['botPrice'].endswith('（6時間以内）')
    assert records['AR-000002']['fieldAudit']['botPrice'].endswith('（要再取得）')
    assert '6時間以内のBot在庫観測 1盤' in text
    assert '24時間以内のBot在庫観測' not in text

def test_edition_coverage_has_seven_evidence_bound_rows(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    checked=datetime.now(timezone.utc).isoformat()
    c=sqlite3.connect(site)
    with c:
        c.execute("update editions set tool_json=? where id='AR-000001'",(
            json.dumps({'title':'Example record','catalog':'DATA001','label':'Data Discs'}),))
        c.execute('insert into catalog_lookup_queue values(?,?,?,?)',
                  ('AR-000001','catno','DATA001','Data Discs'))
        c.execute('insert into catalog_lookup values(?,?,?,?,?,?)',
                  ('catno','DATA001','Data Discs','no_exact_candidate',checked,'[]'))
        c.execute('insert into catalog_lookup_queue values(?,?,?,?)',
                  ('AR-000002','catno','DATA002','Data Discs'))
        c.execute('insert into catalog_lookup values(?,?,?,?,?,?)',
                  ('catno','DATA002','Data Discs','http_503',checked,'[]'))
        c.execute('insert into merchant_discovery values(?,?,?)',
                  ('DATA001','https://data-discs.com/products/example',checked))
        c.execute('insert into offer_candidates values(?,?)',
                  ('AR-000001','https://www.discogs.com/sell/release/12345'))
        c.execute('insert into observations values(?,?,?,?)',
                  ('AR-000001',checked,25.0,'https://market.example/listing/1'))
        c.execute('insert into observations values(?,?,?,?)',
                  ('AR-000003',checked,12.0,'https://evidence.example/item/3'))
        c.execute('create table image_rights_review(edition_id,image_url,status,permission_source)')
        c.execute('insert into image_rights_review values(?,?,?,?)',
                  ('AR-000001','https://images.example/cover.jpg','unconfirmed',None))
    c.close()
    c=sqlite3.connect(bot)
    with c:
        c.execute('insert into price_snapshots values(?,?,?,?,?)',
                  ('AR-000001',checked,25.0,'USD',1))
        c.execute('insert into editions values(?,?)',('AR-000004',1))
    c.close()
    target=tmp_path/'index.html'; csv_path=tmp_path/'edition_audit.csv'
    audit.run(site,bot,template,target,csv_path)
    c=sqlite3.connect(site)
    rows=c.execute('select area,status,reason,checked_at,source_id from edition_coverage_latest where edition_id=? order by area',
                   ('AR-000001',)).fetchall()
    assert c.execute('select count(*) from edition_coverage_latest').fetchone()[0]==3777*7
    assert c.execute('select count(distinct edition_id) from edition_coverage_latest').fetchone()[0]==3777
    c.close()
    states={row[0]:row[1] for row in rows}
    assert states=={
        'availability':'candidate_needs_review','image':'candidate_needs_review',
        'metadata':'not_found','official_new_release':'no_route',
        'price':'candidate_needs_review','purchase_url':'candidate_needs_review',
        'used_listing':'candidate_needs_review'}
    assert all(row[2] for row in rows)
    assert next(row for row in rows if row[0]=='metadata')[4]=='musicbrainz'
    assert next(row for row in rows if row[0]=='price')[4]=='bot_snapshot'
    c=sqlite3.connect(site)
    assert c.execute("select status from edition_coverage_latest where edition_id='AR-000002' and area='metadata'").fetchone()[0]=='blocked'
    assert c.execute("select source_id from edition_coverage_latest where edition_id='AR-000003' and area='price'").fetchone()[0]=='evidence.example'
    c.close()
    c=sqlite3.connect(site)
    assert c.execute("select status from edition_coverage_latest where edition_id='AR-000004' and area in ('price','availability') order by area").fetchall()==[('source_unverified',),('source_unverified',)]
    c.close()
    with (tmp_path/'edition_coverage.csv').open(encoding='utf-8-sig',newline='') as f:
        report=list(csv.DictReader(f))
    assert len(report)==3777*7 and len({(r['盤ID'],r['領域']) for r in report})==3777*7
    html=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(html[html.index('[{'):])
    item=next(record for record in records if record['id']=='AR-000001')
    assert len(item['fieldAudit']['coverage'])==7
    template_source=(Path(__file__).resolve().parents[1]/'site'/'site_template.html').read_text(encoding='utf-8')
    assert '候補・要確認' in template_source
    assert '<a href="./edition_coverage.csv">3,777盤の7領域別監査CSV</a>' in template_source

def test_purchase_search_routes_are_not_reported_as_no_route(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    c=sqlite3.connect(site)
    with c:
        c.execute("update editions set tool_json=? where id='AR-000001'",
                  (json.dumps({'title':'Example record'}),))
    c.close()
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    c=sqlite3.connect(site)
    row=c.execute("select status,reason,checked_at,source_id from edition_coverage_latest where edition_id='AR-000001' and area='purchase_url'").fetchone()
    c.close()
    assert row==('source_unverified','市場検索リンクを提示・個別商品ページ/在庫は未確認',None,'market_search_links')
    html=target.read_text(encoding='utf-8')
    assert 'Discogsで探す（検索）' in html
    assert 'Yahoo!オークションで探す（在庫未確認）' in html

def test_official_release_coverage_uses_rss_health_and_keeps_low_priority_route(site_fixture):
    audit,_,bot,_=site_fixture
    checked=datetime.now(timezone.utc)
    c=sqlite3.connect(bot)
    with c:
        c.executescript("""
        create table sources(id integer primary key,name,kind,status,discovered_via,expected_cadence_days);
        create table source_health(source_id,state,last_success_at);
        """)
        c.execute('insert into sources values(?,?,?,?,?,?)',
                  (6,'game-music-records-official','RSS','approved','official_product_rss',0.25))
        c.execute('insert into source_health values(?,?,?)',
                  (6,'HEALTHY',checked.isoformat()))
    fresh=audit.official_feed_coverage(c,checked)
    assert fresh[0]=='source_unverified'
    assert '低優先度' in fresh[1] and '盤別' in fresh[1]
    assert fresh[2]==checked.isoformat() and fresh[3]=='game-music-records-official'
    with c:
        c.execute('update source_health set state=?,last_success_at=? where source_id=6',
                  ('DOWN',(checked-timedelta(days=2)).isoformat()))
    stale=audit.official_feed_coverage(c,checked)
    assert stale[0]=='stale'
    assert '直近成功' in stale[1]
    c.close()

def test_collector_products_become_unverified_store_links_read_only(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    c=sqlite3.connect(site)
    with c:
        c.execute("update editions set tool_json=? where id='AR-000001'",(
            json.dumps({'title':'Example game soundtrack','catalog':'DATA001','label':'Data Discs'}),))
    c.close()
    collector=tmp_path/'collector.sqlite3'
    url='https://data-discs.com/products/example'
    c=sqlite3.connect(collector)
    with c:
        c.executescript('create table jobs(url primary key,source,last_error); create table observations(id primary key,url,source,observed,expires,facts);')
        now=time.time()
        c.execute('insert into jobs values(?,?,null)',(url,'datadiscs'))
        c.execute('insert into observations values(?,?,?,?,?,?)',(
            1,url,'datadiscs',now,now+3600,json.dumps({'page_type':'product','title':'DATA001: Example game soundtrack',
            'catalog':'DATA001','price':15.99,'currency':'GBP','availability':'IN_STOCK_STRUCTURED'})))
    c.close()
    before=collector.read_bytes()
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv',collector_db=collector)
    text=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(text[text.index('[{'):])
    record=next(row for row in records if row['id']=='AR-000001')
    assert record['fieldAudit']['collectorOffers'].startswith('1件')
    link=next(x for x in record['fieldAudit']['links'] if '商品ページ候補' in x['label'])
    assert link['url']==url and '版・在庫・総額は未確認' in link['label']
    assert '15.99' not in text
    assert collector.read_bytes()==before
def test_game_source_absence_is_distinguished_from_conflicting_values(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    c=sqlite3.connect(site)
    with c:
        c.executemany('update editions set muse_json=?,html_json=? where id=?',[
            (json.dumps({'game':'Muse only'}),json.dumps({}),'AR-000001'),
            (json.dumps({}),json.dumps({'game':'HTML only'}),'AR-000002'),
            (json.dumps({'game':'Muse value'}),json.dumps({'game':'HTML value'}),'AR-000003'),
            (json.dumps({'game':'Same value'}),json.dumps({'game':'Same value'}),'AR-000004'),
            (json.dumps({'game':'不明（BlipBlop個別ReleaseのGame(s)欄は表示確認済み、今回未転記）'}),
             json.dumps({'game':'HTML value'}),'AR-000005'),
        ])
    c.close()
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    c=sqlite3.connect(site)
    notes={rid:note for rid,note in c.execute(
        "select edition_id,review_notes from edition_audit_latest where edition_id like 'AR-00000%'" )}
    c.close()
    assert 'Museのみ収録' in notes['AR-000001']
    assert 'HTMLのみ収録' in notes['AR-000002']
    assert '表記不一致' in notes['AR-000003']
    assert '表記不一致' not in notes['AR-000001']
    assert '表記不一致' not in notes['AR-000002']
    assert 'ゲーム情報の差' not in notes['AR-000004']
    text=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(text[text.index('[{'):])
    records={record['id']:record for record in records}
    assert records['AR-000001']['fieldAudit']['gameAlternative']=='Muse only'
    assert records['AR-000002']['fieldAudit']['gameAlternative'] is None
    assert records['AR-000003']['fieldAudit']['gameAlternative']=='Muse value'
    assert records['AR-000004']['fieldAudit']['gameAlternative'] is None
    assert records['AR-000005']['fieldAudit']['gameAlternative'] is None
    assert 'Museゲーム名未転記（HTML記載は未検証）' in notes['AR-000005']
    assert 'ゲーム表記差あり' not in notes['AR-000005']
    assert '不明（BlipBlop個別ReleaseのGame(s)欄は表示確認済み、今回未転記）' not in text

def test_muse_game_enrichment_provenance_reaches_site_details(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    enrichment={'category':'GAME_IDENTIFIED','game_identified':'Example Game',
                'confidence':'高','basis':'Muse source evidence'}
    with sqlite3.connect(site) as db:
        db.execute("update editions set game_enrichment_json=? where id='AR-000001'",
                   (json.dumps(enrichment,ensure_ascii=False),))
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    text=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(text[text.index('[{'):])
    record=next(row for row in records if row['id']=='AR-000001')
    assert record['fieldAudit']['museGameEvidence']=={
        'category':'GAME_IDENTIFIED','game':'Example Game',
        'confidence':'高','basis':'Muse source evidence'}
    template_source=(Path(__file__).resolve().parents[1]/'site'/'site_template.html').read_text(encoding='utf-8')
    assert 'Muse調査のゲーム判定' in template_source

def test_price_comparison_pauses_without_embedded_marketplace_observations():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert 'var DEALS_URL' not in source
    assert 'fetch(DEALS_URL)' not in source
    assert 'data = { checked_date: null, deals: [] };' in source
    assert '.deals-controls[hidden] { display: none; }' in source
    assert 'statusEl.hidden = list.length > 0 || data.deals.length === 0;' in source
    assert '<h2>価格情報</h2>' in source
    assert '価格表示を一時停止しています。出典・通貨・利用条件の確認後に再開します。' in source
    assert 'This application uses Discogs’ API but is not affiliated with, sponsored or endorsed by Discogs.' in source

def test_source_annotations_are_not_described_as_independently_verified():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert 'このサイトで独立に再検証した事実としては扱いません。' in source
    assert '全3,777件の最終品質監査（2026-10-30）' not in source
    assert '添付HTMLの説明文では、ゲーム名が空欄だった863件のうち855件' in source
    assert '盤ごとの一次資料URLや監査ログが注釈に紐付いていないため、当サイトで独立確認済みの結果とは扱いません。' in source
    assert 'ゲーム名が空欄だった863件は、タイトル・型番・レーベルと Discogs' not in source

def test_mobile_masthead_and_tabs_keep_deliberate_line_breaks():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert '<h1><span>ゲーム音楽レコード</span><span>データベース</span></h1>' in source
    assert '.masthead h1 span { display: block; }' in source
    assert '.stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; width: 100%; }' in source
    assert '.stat { min-width: 104px; padding: 6px 9px 5px; }' in source
    assert '.stat:nth-child(n + 3)' not in source
    assert '.view-tab { flex: 0 0 auto; white-space: nowrap;' in source
    assert '@media (max-width: 360px)' in source and '.view-tabs { overflow-x: auto; overflow-y: hidden; padding: 0; }' in source


def test_header_stats_separate_review_candidates_from_current_evidence():
    template=Path(__file__).resolve().parents[1]/"site"/"site_template.html"
    source=template.read_text(encoding="utf-8")
    assert 'coverageCount("used_listing", "candidate_needs_review")' in source
    assert 'coverageCount("price", "verified_current")' in source
    assert '現行価格証跡・確認済み' in source
    assert '中古出品候補・要確認' in source
    assert 'var withEvidence = DATA.filter' not in source


def test_audit_snapshot_time_is_rendered_visibly(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    text=target.read_text(encoding="utf-8")
    assert re.search(
        r'<p class="audit-as-of" id="audit-as-of">監査データ更新：'
        r'\d{4}-\d{2}-\d{2} \d{2}:\d{2} JST</p>', text)
    assert '@@AUDIT_AS_OF@@' not in text


def test_only_human_approved_yahoo_listing_is_exported_with_attribution(site_fixture,tmp_path):
    audit,site,bot,template=site_fixture
    seen=datetime.now(timezone.utc).isoformat()
    c=sqlite3.connect(bot)
    with c:
        c.executescript("""
          create table listings(id integer,edition_id text,source text,url text,
            image_url text,raw_title text,last_seen text,price real,currency text);
          create table match_decisions(id integer,listing_id integer,
            edition_id text,matcher_version text,price_confidence text);
        """)
        c.execute("insert into listings values(1,'AR-000001',?,?,?, ?,?,?,?)",
                  ('Yahoo Shopping used',
                   'https://store.shopping.yahoo.co.jp/shop/record.html',
                   'https://item-shopping.c.yimg.jp/i/g/record',
                   'Vintage record',seen,4200,'JPY'))
        c.execute("insert into match_decisions values(1,1,'AR-000001',"
                  "'human-review-1','高')")
        c.execute("insert into listings values(6,'AR-000001',?,?,?, ?,?,?,?)",
                  ('Yahoo Shopping used',
                   'https://store.shopping.yahoo.co.jp/shop/record-2.html',
                   'https://item-shopping.c.yimg.jp/i/g/record-2',
                   'Second vintage record',seen,4300,'JPY'))
        c.execute("insert into match_decisions values(6,6,'AR-000001',"
                  "'human-review-1','高')")
        c.execute("insert into listings values(2,'AR-000002',?,?,?, ?,?,?,?)",
                  ('Yahoo Shopping used',
                   'https://store.shopping.yahoo.co.jp/shop/unreviewed.html',
                   'https://item-shopping.c.yimg.jp/i/g/unreviewed',
                   'Unreviewed',seen,4000,'JPY'))
        c.execute("insert into match_decisions values(2,2,'AR-000002',"
                  "'matcher-1','高')")
        c.execute("insert into listings values(3,'AR-000003',?,?,?, ?,?,?,?)",
                  ('eBay Browse used','https://www.ebay.com/itm/123',
                   'https://i.ebayimg.com/image.jpg','eBay vintage',seen,18,'USD'))
        c.execute("insert into match_decisions values(3,3,'AR-000003',"
                  "'human-review-1','高')")
        c.execute("insert into listings values(4,'AR-000004',?,?,?, ?,?,?,?)",
                  ('Rakuten Ichiba','https://item.rakuten.co.jp/shop/abc/',
                   'https://thumbnail.image.rakuten.co.jp/a.jpg',
                   'Rakuten item',seen,3000,'JPY'))
        c.execute("insert into match_decisions values(4,4,'AR-000004',"
                  "'human-review-1','高')")
        stale=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        c.execute("insert into listings values(5,'AR-000005',?,?,?, ?,?,?,?)",
                  ('eBay Browse used','https://www.ebay.com/itm/stale',None,
                   'Stale eBay item',stale,20,'USD'))
        c.execute("insert into match_decisions values(5,5,'AR-000005',"
                  "'human-review-1','高')")
    c.close()

    target=tmp_path/'index.html'
    audit.run(site,bot,template,target,tmp_path/'audit.csv')
    html=target.read_text(encoding='utf-8')
    records,_=json.JSONDecoder().raw_decode(html[html.index('[{'):])
    exported={row['id']:row for row in records}
    assert 'https://store.shopping.yahoo.co.jp/shop/record.html' in html
    assert 'https://store.shopping.yahoo.co.jp/shop/record-2.html' in html
    assert 'https://item-shopping.c.yimg.jp/i/g/record' in html
    assert 'unreviewed.html' not in html
    assert 'https://www.ebay.com/itm/123' not in html
    assert 'eBay vintage' not in html
    assert exported['AR-000003']['fieldAudit']['marketListings']==[]
    assert 'https://item.rakuten.co.jp/shop/abc/' not in html
    assert 'Rakuten item' not in html
    assert exported['AR-000004']['fieldAudit']['marketListings']==[]
    assert exported['AR-000005']['fieldAudit']['marketListings']==[]
    assert 'image.jpg' not in html and '/stale' not in html
    assert len(exported['AR-000001']['fieldAudit']['marketListings'])==2
    assert 'marketListings' in html
    production_template=(Path(__file__).resolve().parents[1]/'site'/'site_template.html').read_text(encoding='utf-8')
    assert '<!-- Begin Yahoo! JAPAN Web Services Attribution Snippet -->' in production_template
    assert '<span style="margin:15px 15px 15px 15px"><a href="https://developer.yahoo.co.jp/sitemap/">Webサービス by Yahoo! JAPAN</a></span>' in production_template



