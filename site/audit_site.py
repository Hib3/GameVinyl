import csv, html, json, math, re, sqlite3, unicodedata
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit
from catalog_search import norm, valid_catalog

COVERAGE_AREAS = (
    'metadata', 'official_new_release', 'used_listing', 'price',
    'availability', 'image', 'purchase_url',
)

def safe_https(x):
    try:
        u=urlsplit(x or '')
        return u.scheme=='https' and bool(u.hostname) and not (u.username or u.password)
    except ValueError:
        return False

def collector_candidates(path, records, now):
    path=Path(path)
    if not path.is_file():
        return {},'missing'
    try:
        config=json.loads((Path(__file__).resolve().parents[1]/'collector'/'sources.json').read_text(encoding='utf-8'))
        sources={s['id']:s for s in config['sources']}
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            db.row_factory=sqlite3.Row
            db.execute('pragma query_only=on')
            rows=db.execute("""select o.url,o.source,o.observed,o.expires,o.facts,j.last_error
              from observations o join (select url,max(id) id from observations group by url) x on x.id=o.id
              join jobs j on j.url=o.url where json_extract(o.facts,'$.page_type')='product'""").fetchall()
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        return {},'error'
    titles={rid:[norm(r.get(k)) for k in ('title','originalTitle','englishTitle') if len(norm(r.get(k)))>=8]
            for rid,r in records.items()}
    catalogs={rid:norm(r.get('catalog')) for rid,r in records.items() if valid_catalog(r.get('catalog'))}
    out=defaultdict(dict)
    usable={'AUCTION_OPEN','IN_STOCK_STRUCTURED','PURCHASE_BUTTON_OBSERVED','PREORDER_STRUCTURED'}
    for row in rows:
        source=sources.get(row['source'])
        try:
            facts=json.loads(row['facts'])
            host=urlsplit(row['url']).hostname
            expiry=float(row['expires'])
        except (ValueError,TypeError):
            continue
        if (not source or not safe_https(row['url']) or host not in {h.lower() for h in source.get('hosts',[])}
                or row['last_error'] or expiry<now or facts.get('availability') not in usable):
            continue
        catalog=norm(facts.get('catalog'))
        title=norm(facts.get('title'))
        for rid,record in records.items():
            reason=None
            if row['url'] in {s.get('url') for s in record.get('sources',[]) if isinstance(s,dict)}:
                reason='出典URL一致（盤一致は未確認）'
            elif rid in facts.get('watch_ids',[]):
                reason='重要盤ウォッチ検索'
            elif catalog and catalog==catalogs.get(rid):
                reason='型番一致候補'
            elif title and any(key==title or key in title for key in titles[rid]):
                reason='タイトル一致候補'
            if reason:
                observed=datetime.fromtimestamp(float(row['observed']),timezone.utc).astimezone().isoformat(timespec='minutes')
                item={'url':row['url'],'title':str(facts.get('title') or '')[:160],
                      'source':source['name'],'observed':observed,'reason':reason}
                out[rid][row['url']]=item
    return {rid:sorted(items.values(),key=lambda x:x['observed'],reverse=True)[:2]
            for rid,items in out.items()},'healthy'

def pending_candidates(bot):
    if not bot.execute("select 1 from sqlite_master where type='table' and name='candidate_editions'").fetchone():
        return []
    if bot.execute("select 1 from sqlite_master where type='table' and name='sources'").fetchone():
        sources={r[0]:(r[1],urlsplit(r[2]).hostname if safe_https(r[2]) else None)
                 for r in bot.execute('select name,kind,url from sources')}
    else:
        sources={}
    out=[]
    for title,url,found_via,seen in bot.execute(
            "select title,source_url,found_via,coalesce(first_seen,found_at) "
            "from candidate_editions where status='pending' "
            "order by coalesce(first_seen,found_at) desc,id"):
        prefix,sep,name=(found_via or '').partition(':')
        source=sources.get(name if sep else found_via,('',None))
        host=source[1]
        link=None
        if safe_https(url) and host and urlsplit(url).hostname==host:
            parts=urlsplit(url)
            link=urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))
        text=' '.join(str(title or '').split())[:240]
        if text:
            out.append({'title':text,'source':name if sep else (found_via or '出典不明'),
                        'kind':source[0] or prefix,'seen':str(seen or '')[:40],
                        'url':link})
    return out

def approved_yahoo_listings(bot, now):
    """Read only Yahoo listings whose latest identity decision was human approval."""
    tables={r[0] for r in bot.execute(
        "select name from sqlite_master where type='table'")}
    if not {'listings','match_decisions'} <= tables:
        return defaultdict(list)
    cols={r[1] for r in bot.execute('pragma table_info(listings)')}
    image_expr='l.image_url' if 'image_url' in cols else 'NULL'
    out=defaultdict(list)
    rows=bot.execute(f"""
      select l.edition_id,l.url,{image_expr},l.raw_title,l.last_seen
      from listings l join match_decisions d
        on d.listing_id=l.id and d.edition_id=l.edition_id
      where l.source='Yahoo Shopping used' and l.edition_id is not null
        and d.id=(select max(d2.id) from match_decisions d2
                  where d2.listing_id=l.id and d2.edition_id=l.edition_id)
        and d.matcher_version='human-review-1'
        and d.price_confidence='高'
      order by l.last_seen desc, l.id desc
    """).fetchall()
    for edition_id,url,image,title,seen in rows:
        host=(urlsplit(url).hostname or '').lower() if safe_https(url) else ''
        if not (host=='shopping.yahoo.co.jp' or
                host.endswith('.shopping.yahoo.co.jp')):
            continue
        checked=parse_date(seen)
        if checked is None or checked>now:
            continue
        image_host=(urlsplit(image).hostname or '').lower() if safe_https(image) else ''
        image_ok=(image_host=='yimg.jp' or image_host.endswith('.yimg.jp'))
        items=out[edition_id]
        if len(items)<3:
            items.append({'url':url,'image':image if image_ok else None,
                          'title':' '.join(str(title or '').split())[:200],
                          'lastSeen':str(seen)[:40]})
    return out

def price_ok(x):
    return type(x) in (int,float) and math.isfinite(x) and x>0

def parse_date(value):
    try:
        d=datetime.fromisoformat(value)
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
    except (TypeError,ValueError):
        return None

def atomic_text(path, text):
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(text,encoding='utf-8')
    tmp.replace(path)

def compact_json(x):
    return json.dumps(x,ensure_ascii=False,separators=(',',':'),allow_nan=False).replace('<','\\u003c')

def run(db, botdb, template, target, csv_path, *, catalog_result=None, collector_db=None):
    now=datetime.now(timezone.utc)
    cutoff=now-timedelta(hours=6)
    with closing(sqlite3.connect(botdb.as_uri()+'?mode=ro',uri=True)) as bot:
        bot.execute('pragma query_only=on')
        snapshots={}
        for r in bot.execute("select edition_id,taken_at,lowest_price,currency,num_for_sale from price_snapshots order by rowid"):
            d=parse_date(r[1])
            if d and d<=now and (r[0] not in snapshots or d>=snapshots[r[0]][0]):
                snapshots[r[0]]=(d,*r[1:])
        monitored_ids={row[0] for row in bot.execute('select id from editions where watch_level>0')}
        monitored=len(monitored_ids)
        discoveries=pending_candidates(bot)
        yahoo_market=approved_yahoo_listings(bot,now)
    with closing(sqlite3.connect(db)) as c:
        c.execute('pragma foreign_keys=on')
        rows=c.execute('select id,identity_status,tool_json,muse_json,html_json from editions order by id').fetchall()
        assert len(rows)==3777 and all(re.fullmatch(r'AR-\d{6}',r[0]) for r in rows)
        parsed=[(rid,state,json.loads(a),json.loads(b),json.loads(h)) for rid,state,a,b,h in rows]
        collector_map,collector_state=collector_candidates(
            collector_db or Path(botdb).parent/'collector'/'collector.sqlite3',
            {rid:(a or {}) for rid,_,a,_,_ in parsed},now.timestamp())
        groups=Counter((norm(a.get('label')),norm(a.get('catalog'))) for _,_,a,_,_ in parsed if a and valid_catalog(a.get('catalog')))
        evidence=defaultdict(list)
        for rid,date,p,url in c.execute("select matched_edition_id,checked_at,price_usd,source_url from observations where matched_edition_id is not null"):
            d=parse_date(date)
            if d and d<=now:
                safe_url=safe_https(url)
                evidence[rid].append((d,date,p,safe_url,url if safe_url else None))
        review_evidence=defaultdict(list)
        exists=c.execute("select 1 from sqlite_master where type='table' and name='field_review_evidence'").fetchone()
        if exists:
            for rid,field,note,url,checked in c.execute(
                    "select edition_id,field_name,note,evidence_url,checked_at "
                    "from field_review_evidence where review_status='needs_review'"):
                if isinstance(note,str) and note.strip():
                    review_evidence[rid].append((str(field or ''),note[:500],url,checked))
        images=dict(c.execute('select edition_id,image_url from site_thumbnail_eligible'))
        image_rights={}
        if c.execute("select 1 from sqlite_master where type='table' and name='image_rights_review'").fetchone():
            image_rights=dict(c.execute('select edition_id,status from image_rights_review'))
        market={rid:url for rid,url in c.execute("select matched_edition_id,listing_url from offer_candidates where matched_edition_id is not null")
                if re.fullmatch(r'https://www\.discogs\.com/sell/release/\d+/?',url or '')}
        merchants={}
        for cat,url in c.execute("select catalog_group,official_product_url from official_product_links union select catalog_group,official_product_url from merchant_discovery where datetime(last_seen_at)>=datetime(?,'-30 days')",(now.isoformat(),)):
            if re.fullmatch(r'https://data-discs\.com/products/[a-z0-9-]+',url or ''):
                merchants[norm(cat)]=url
        lookups={rid:(result,checked,matches) for rid,result,checked,matches in c.execute("""
          select q.edition_id,l.result,l.checked_at,l.match_json from catalog_lookup_queue q
          join catalog_lookup l using(field,search_value,label)
        """)}
        candidate_editions=defaultdict(set)
        for edition_id,(state,_,raw_matches) in lookups.items():
            if state not in ('candidates','partial'):
                continue
            try:
                matches=json.loads(raw_matches)
            except (ValueError,TypeError):
                continue
            if not isinstance(matches,list):
                continue
            for match in matches:
                uid=match.get('id') if isinstance(match,dict) else None
                if isinstance(uid,str) and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',uid):
                    candidate_editions[uid].add(edition_id)
        export=[]; audit=[]; coverage=[]
        for rid,identity,a,b,h in parsed:
            a,b,h=a or {},b or {},h or {}
            r=dict(h)
            r['id']=rid
            for field in ('title','catalog','label','year','format'):
                r[field]=a.get(field,b.get(field,h.get(field)))
            if type(r['year']) is not int:
                r['year']=None
            for field in ('price','median'):
                if not price_ok(r.get(field)):
                    r[field]=None
            # Legacy survey values do not retain complete provenance or a
            # consistent currency. Do not publish Marketplace Data until it
            # can be refreshed and attributed under the current API terms.
            r['price']=r['median']=r['lastSold']=None
            checks=[]
            if identity=='shared_catalog_identity': checks.append('共有カタログIDで結合（独立した盤同定は未確認）')
            else: checks.append('基本情報の照合待ち')
            for field,note,_,_ in review_evidence.get(rid,[]):
                checks.append('出典の確認待ち（'+field+'）: '+note)
            key=(norm(r.get('label')),norm(r.get('catalog')))
            if not valid_catalog(r.get('catalog')): checks.append('型番不明')
            elif groups[key]>1: checks.append('同型番の複数盤あり')
            muse_game=b.get('game') if isinstance(b.get('game'),str) else ''
            muse_game_untranscribed=muse_game=='不明（BlipBlop個別ReleaseのGame(s)欄は表示確認済み、今回未転記）'
            if muse_game_untranscribed: muse_game=''
            html_game=h.get('game') if isinstance(h.get('game'),str) else ''
            muse_game_key=norm(muse_game); html_game_key=norm(html_game)
            alt=muse_game if muse_game_key and muse_game_key!=html_game_key else None
            if muse_game_key and not html_game_key: checks.append('ゲーム情報はMuseのみ収録（HTML欠落・要確認）')
            elif html_game_key and not muse_game_key:
                checks.append('Museゲーム名未転記（HTML記載は未検証）' if muse_game_untranscribed else 'ゲーム情報はHTMLのみ収録（Muse未収録・要確認）')
            elif muse_game_key!=html_game_key: checks.append('Muse/HTMLのゲーム表記不一致（要確認）')
            if b.get('status')=='CURRENT_CONFIRMED' and not price_ok(b.get('price')): checks.append('出品記録に数値価格なし')
            obs=evidence[rid]
            survey=max((x[1] for x in obs),default=None)
            if not any(x[3] for x in obs): checks.append('監査台帳に確認用URLなし')
            price_dates=[x[1] for x in obs if price_ok(x[2]) and x[2]==r['price']]
            if r['price'] is not None and not price_dates: checks.append('調査価格の対応証拠不足')
            price_date=max(price_dates,default=None)
            links=[]
            for _,_,url,_ in review_evidence.get(rid,[]):
                if safe_https(url):
                    parts=urlsplit(url)
                    if not parts.query and not parts.fragment:
                        links.append({'label':'調査出典（版・在庫は未確定）','url':url})
            if rid in market: links.append({'label':'Discogsの盤別出品一覧（在庫未確認）','url':market[rid]})
            for item in yahoo_market.get(rid,[]):
                links.append({'label':'Yahoo!ショッピング中古出品（最終観測 '+item['lastSeen']+'・現在在庫は未保証）','url':item['url']})
            if key[0]=='data discs' and key[1] in merchants:
                links.append({'label':'公式商品ページ（型番共通・盤違いに注意）','url':merchants[key[1]]})
            offers=collector_map.get(rid,[])
            for offer in offers:
                links.append({'label':offer['source']+' 商品ページ候補（'+offer['reason']+'・'+offer['observed']+'。版・在庫・総額は未確認）: '+offer['title'],
                              'url':offer['url']})
            q=' '.join(str(r.get(k) or '') for k in ('title','catalog') if k!='catalog' or valid_catalog(r.get(k)))
            links.append({'label':'Discogsで探す（検索）','url':'https://www.discogs.com/search/?'+urlencode({'q':q,'type':'release'})})
            for label,base,param in (
                ('Yahoo!ショッピングで探す（在庫未確認）','https://shopping.yahoo.co.jp/search','p'),
                ('Yahoo!オークションで探す（在庫未確認）','https://auctions.yahoo.co.jp/search/search','p'),
                ('メルカリで探す（在庫未確認）','https://jp.mercari.com/search','keyword'),
                ('楽天市場で探す（在庫未確認）','https://search.rakuten.co.jp/search/mall/','sitem'),
                ('eBayで探す（在庫未確認）','https://www.ebay.com/sch/i.html','_nkw'),
            ):
                links.append({'label':label,'url':base+'?'+urlencode({param:q})})
            thumb=images.get(rid) if safe_https(images.get(rid)) else None
            if not thumb: checks.append('掲載可能画像なし')
            snap=snapshots.get(rid)
            fresh=bool(snap and snap[0]>=cutoff and type(snap[4]) is int and snap[4]>=0)
            bot_note='未観測' if not snap else snap[1]+' の観測（'+('6時間以内' if fresh else '要再取得')+'）'
            lookup=lookups.get(rid)
            query_state=lookup[0] if lookup else 'pending'
            if lookup and query_state in ('candidates','partial'):
                candidates=json.loads(lookup[2])
                checks.append('外部照合候補'+str(len(candidates))+'件')
                if any(isinstance(candidate,dict) and
                       isinstance(candidate.get('id'),str) and
                       len(candidate_editions.get(candidate.get('id'),()))>1
                       for candidate in candidates):
                    checks.append('同一外部候補が複数盤に共通・版の確認が必要')
                for candidate in candidates[:3]:
                    uid=candidate.get('id','')
                    if re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',uid):
                        links.append({'label':'MusicBrainzの盤候補（未確定）','url':'https://musicbrainz.org/release/'+uid})
            lookup_labels={'pending':'未照会','candidates':'候補あり・版未確定','partial':'検索結果の一部のみ確認','no_exact_candidate':'一致候補なし・資料不足の可能性'}
            lookup_note=lookup_labels.get(query_state,'取得に失敗・再試行予定')
            if not fresh: checks.append('個別盤の在庫は未確認')
            summary=' ／ '.join(checks) or '確認済みの要確認項目なし'
            offer_note=(str(len(offers))+'件の店舗ページ候補（版・在庫・総額は未確認）' if offers else
                        '今回の取得範囲で未検出（全市場網羅ではありません）' if collector_state=='healthy' else
                        '取得DB未配置・候補非表示' if collector_state=='missing' else
                        '取得DB読取失敗・候補非表示')
            coverage_items=[]
            def add_coverage(area,status,reason,checked_at=None,source_id=None):
                coverage.append((rid,area,status,reason,checked_at,source_id))
                coverage_items.append({'area':area,'status':status})
            if query_state=='no_exact_candidate':
                add_coverage('metadata','not_found','MusicBrainzで今回の検索値に一致候補なし（全情報源での不存在を意味しない）',lookup[1],'musicbrainz')
            elif query_state in ('candidates','partial'):
                add_coverage('metadata','candidate_needs_review','外部目録候補あり・盤の一致は未確認',lookup[1],'musicbrainz')
            elif query_state in ('http_403','http_429','http_503'):
                add_coverage('metadata','blocked','MusicBrainz照会がHTTP制限で停止中',lookup[1],'musicbrainz')
            elif lookup and query_state not in ('pending','no_exact_candidate'):
                add_coverage('metadata','error','外部目録照合が成功状態ではない: '+str(query_state)[:80],lookup[1],'musicbrainz')
            else:
                add_coverage('metadata','source_unverified','共有カタログIDは盤単位の一次資料照合を証明しない',lookup[1] if lookup else None,'musicbrainz' if lookup else None)
            official=key[0]=='data discs' and key[1] in merchants
            add_coverage('official_new_release','no_route',
                         '利用条件と対象盤一致を確認した公開公式新譜フィード/APIなし')
            market_candidate=bool(offers) or rid in market or rid in yahoo_market
            used_status=('candidate_needs_review' if market_candidate else
                         'source_unverified' if collector_state=='healthy' else
                         'error' if collector_state=='error' else 'no_route')
            used_reason=('人間承認済みYahoo中古出品あり（最終観測日時表示・現在在庫は未保証）' if rid in yahoo_market else
                         '店舗/マーケット候補があり在庫・版は未確認' if market_candidate else
                         '取得済み情報に盤別候補なし（市場全体での不存在は未確認）' if collector_state=='healthy' else
                         'Collector候補DBを読めず' if collector_state=='error' else
                         'Collector候補DBが未配置')
            add_coverage('used_listing',used_status,used_reason,
                          offers[0]['observed'] if offers else (yahoo_market[rid][0]['lastSeen'] if rid in yahoo_market else None),
                          offers[0]['source'] if offers else ('yahoo_shopping' if rid in yahoo_market else ('discogs_marketplace' if rid in market else None)))
            priced_observations=[item for item in evidence[rid] if price_ok(item[2])]
            latest_price=max(priced_observations,key=lambda item:item[0]) if priced_observations else None
            has_price=bool(snap) or bool(latest_price)
            price_status=('candidate_needs_review' if has_price else
                          'source_unverified' if rid in monitored_ids else 'no_route')
            price_reason=('価格の出典・原通貨・送料条件・盤一致をそろえて確認できていない' if has_price else
                          'Bot監視対象だが現行価格証拠なし' if rid in monitored_ids else 'この盤の現在価格を取得する経路なし')
            add_coverage('price',price_status,price_reason,
                         snap[1] if snap else (latest_price[1] if latest_price else None),
                         'bot_snapshot' if snap else (urlsplit(latest_price[4]).hostname if latest_price and latest_price[4] else None))
            if snap:
                availability_status='candidate_needs_review' if fresh else 'stale'
                availability_reason='6時間以内のBot在庫観測だが出典/版の完全照合待ち' if fresh else 'Bot在庫観測は6時間より古い'
                availability_at,availability_source=snap[1],'bot_snapshot'
            elif offers:
                availability_status='candidate_needs_review'
                availability_reason='候補ページに販売状態の記載あり・盤一致未確認'
                availability_at,availability_source=offers[0]['observed'],offers[0]['source']
            elif rid in yahoo_market:
                availability_status='candidate_needs_review'
                availability_reason='Yahoo APIで中古・在庫ありの返却履歴あり（現在在庫は未保証）'
                availability_at,availability_source=yahoo_market[rid][0]['lastSeen'],'yahoo_shopping'
            elif rid in monitored_ids:
                availability_status='source_unverified'; availability_reason='Bot監視対象だが在庫観測なし'
                availability_at=availability_source=None
            else:
                availability_status='no_route'; availability_reason='この盤の現在在庫を取得する経路なし'
                availability_at=availability_source=None
            add_coverage('availability',availability_status,availability_reason,availability_at,availability_source)
            image_status='source_unverified' if thumb else ('candidate_needs_review' if image_rights.get(rid)=='unconfirmed' else 'no_route')
            image_reason=('画像URLはあるが監査出力に権利根拠/確認日がない' if thumb else
                          '画像候補の利用権が未確認' if image_status=='candidate_needs_review' else '利用権確認済み画像なし')
            add_coverage('image',image_status,image_reason,source_id='image_rights_review' if image_status=='candidate_needs_review' else None)
            purchase_candidate=official or market_candidate
            add_coverage('purchase_url','candidate_needs_review' if purchase_candidate else 'no_route',
                         '購入ページ候補あり・最終観測日時表示、現在の購入可否は未保証' if rid in yahoo_market else ('購入ページ候補あり・対象盤/購入可否の人手確認待ち' if purchase_candidate else '盤一致を確認した購入URLなし'),
                         offers[0]['observed'] if offers else (yahoo_market[rid][0]['lastSeen'] if rid in yahoo_market else None),
                         offers[0]['source'] if offers else ('yahoo_shopping' if rid in yahoo_market else ('data_discs' if official else ('discogs_marketplace' if rid in market else None))))
            assert len(coverage_items)==len(COVERAGE_AREAS)
            r['fieldAudit']={'summary':summary,'priceDate':None,
              'priceStatus':'価格表示停止中（出典・通貨・利用条件を再確認中）',
              'surveyDate':survey,'lookupNote':lookup_note,'botPrice':bot_note,'gameAlternative':alt,
              'collectorOffers':offer_note,'collectorStatus':collector_state,'links':links,'image':thumb,
              'coverage':coverage_items,'marketListings':yahoo_market.get(rid,[])}
            export.append(r)
            audit.append((rid,now.isoformat(),identity,query_state,lookup[1] if lookup else None,survey,price_date,int(fresh),len(links)-1,int(bool(thumb)),summary))
        c.executescript("""
          create table if not exists edition_audit_latest(
            edition_id text primary key references editions(id), audited_at text not null, identity_status text not null,
            lookup_status text not null, lookup_at text, survey_date text, price_evidence_date text, recent_bot_observation integer not null,
            reference_links integer not null, eligible_image integer not null, review_notes text not null
          );
          create table if not exists edition_coverage_latest(
            edition_id text not null references editions(id), area text not null,
            status text not null, reason text not null, checked_at text, source_id text,
            audited_at text not null,
            primary key(edition_id,area),
            check(area in ('metadata','official_new_release','used_listing','price','availability','image','purchase_url')),
            check(status in ('verified_current','verified_historical','candidate_needs_review','no_route','not_found','blocked','stale','source_unverified','not_applicable','error'))
          );
        """)
        with c:
            c.execute('delete from edition_audit_latest')
            c.executemany('insert into edition_audit_latest values(?,?,?,?,?,?,?,?,?,?,?)',audit)
            c.execute('delete from edition_coverage_latest')
            c.executemany('insert into edition_coverage_latest values(?,?,?,?,?,?,?)',
                          (row+(now.isoformat(),) for row in coverage))
        assert len(coverage)==len(rows)*len(COVERAGE_AREAS)
        assert c.execute('select count(*) from edition_coverage_latest').fetchone()[0]==len(coverage)
        assert c.execute('pragma integrity_check').fetchone()==('ok',)
        assert not c.execute('pragma foreign_key_check').fetchall()
    search_count=sum(a[3]!='pending' for a in audit)
    overview=f'監査対象 {len(audit):,}盤・外部検索着手 {search_count:,}盤・6時間以内のBot在庫観測 {sum(a[7] for a in audit):,}盤・掲載可能画像 {sum(a[9] for a in audit):,}盤。Bot監視指定 {monitored:,}盤。共有カタログIDによる結合は、盤の独立照合を意味しません。価格は出典・通貨・利用条件の再確認まで非表示です。'
    overview+=f" Collector照合状態 {collector_state}・取得範囲内の店舗ページ候補 {sum(bool(v) for v in collector_map.values()):,}盤。"
    overview+=f' Bot未確認候補 {len(discoveries):,}件。'
    if catalog_result is not None:
        if catalog_result['status'] == 'skipped':
            overview += ' MusicBrainz: 連絡先未設定または不正のため今回停止。'
        elif catalog_result['status'] == 'cooldown':
            overview += f" MusicBrainz: 直近の{catalog_result['reason']}後の6時間休止中・今回未照会 {catalog_result['selected']}件。"
        else:
            ok = catalog_result['attempted'] - catalog_result['failed']
            pending = catalog_result['selected'] - catalog_result['attempted']
            overview += f" MusicBrainz今回: 成功 {ok}・失敗 {catalog_result['failed']}・選択後未実行 {pending}。"
    source=template.read_text(encoding='utf-8')
    assert source.count('@@AUDIT_AS_OF@@')==source.count('@@AUDITED_RECORDS@@')==source.count('@@AUDIT_OVERVIEW@@')==source.count('@@DISCOVERED_CANDIDATES@@')==1
    audit_as_of=now.astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M')
    atomic_text(target,source.replace('@@AUDIT_AS_OF@@',audit_as_of).replace('@@AUDIT_OVERVIEW@@',html.escape(overview)).replace('@@AUDITED_RECORDS@@',compact_json(export)).replace('@@DISCOVERED_CANDIDATES@@',compact_json({'count':len(discoveries),'items':discoveries})))
    tmp=csv_path.with_name(csv_path.name+'.tmp')
    with tmp.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f)
        w.writerow(('盤ID','監査日時','基本情報照合','外部検索結果','外部検索日時','調査日','価格証拠日','最近の在庫観測','参照リンク数','掲載可能画像','監査事項'))
        w.writerows(audit)
    tmp.replace(csv_path)
    coverage_path=csv_path.with_name('edition_coverage.csv')
    tmp=coverage_path.with_name(coverage_path.name+'.tmp')
    with tmp.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f)
        w.writerow(('盤ID','領域','状態','理由','確認日時','情報源ID','監査日時'))
        w.writerows(row+(now.isoformat(),) for row in coverage)
    tmp.replace(coverage_path)
    print(overview,flush=True)

