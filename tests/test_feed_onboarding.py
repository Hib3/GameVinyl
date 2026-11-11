import json
from datetime import timedelta
from grt import clock, outbox
from grt.fetchers.webpages import FeedFetcher

def source(db):
    sid=db.execute("insert into sources(name,kind,status,url) values('feed','RSS','approved','https://example.org/rss')").lastrowid
    db.commit()
    return sid

def item(title, age_hours=None):
    return {'title':title,'url':'https://example.org/'+title,
            'published':clock.to_iso(clock.now()-timedelta(hours=age_hours)) if age_hours is not None else ''}

def test_first_feed_preserves_backfill_and_only_notifies_recent(db):
    f=FeedFetcher(db,source(db)); cutoff=f.initial_scan_state()
    batch=[item('old',48),item('fresh',1),item('unknown'),item('future',-1)]
    result=f.register_candidates(batch,found_via='feed:feed',initial_cutoff=cutoff)
    assert result=={'created':4,'failed':0}
    f.finish_initial_scan()
    assert f.initial_scan_state() is None
    assert db.execute("select count(*) from candidate_editions where status='pending'").fetchone()[0]==4
    assert db.execute('select count(*) from feed_backfill_items').fetchone()[0]==3
    assert db.execute('select count(*) from notification_outbox').fetchone()[0]==1
    assert outbox.enqueue_pending_candidate_reviews(db)==0
    assert db.execute('select count(*) from notification_outbox').fetchone()[0]==1
    payloads=[json.loads(r[0]) for r in db.execute('select payload_json from review_queue')]
    assert sum(p.get('notify') is False for p in payloads)==3
    f.register_candidates([item('new',1)],found_via='feed:feed')
    assert db.execute('select count(*) from notification_outbox').fetchone()[0]==2

def test_initial_retry_retains_cutoff_and_no_duplicate_notification(db,monkeypatch):
    f=FeedFetcher(db,source(db)); cutoff=f.initial_scan_state()
    original=f._register_one
    fail={'once':True}
    def flaky(it, via, *, notify=True):
        if it['title']=='late' and fail['once']:
            fail['once']=False
            raise ValueError('simulated')
        return original(it,via,notify=notify)
    monkeypatch.setattr(f,'_register_one',flaky)
    batch=[item('old',48),item('fresh',1),item('late',1)]
    assert f.register_candidates(batch,found_via='feed:feed',initial_cutoff=cutoff)['failed']==1
    assert f.initial_scan_state()==cutoff
    assert f.register_candidates(batch,found_via='feed:feed',initial_cutoff=cutoff)['failed']==0
    f.finish_initial_scan()
    assert db.execute('select count(*) from candidate_editions').fetchone()[0]==3
    assert db.execute('select count(*) from feed_backfill_items').fetchone()[0]==1
    assert db.execute('select count(*) from notification_outbox').fetchone()[0]==2

def test_historical_duplicate_does_not_reopen_rejected_candidate(db):
    f=FeedFetcher(db,source(db))
    old=item('prior',48)
    f.register_candidates([old],found_via='other')
    db.execute("update candidate_editions set status='rejected'")
    db.execute('delete from review_queue')
    db.execute('delete from notification_outbox')
    db.commit()
    cutoff=f.initial_scan_state()
    old['url']='https://other.example/other'
    assert f.register_candidates([old],found_via='feed:feed',initial_cutoff=cutoff)['created']==0
    assert db.execute('select status from candidate_editions').fetchone()[0]=='rejected'
    assert db.execute('select count(*) from notification_outbox').fetchone()[0]==0
    assert db.execute('select count(*) from feed_backfill_items').fetchone()[0]==1

def test_legacy_source_with_success_is_not_rebaselined(db):
    sid=source(db); now=clock.now_iso(db)
    db.execute("insert into fetch_runs(source_id,started_at,finished_at,result) values(?,?,?,'OK')",(sid,now,now))
    db.commit()
    assert FeedFetcher(db,sid).initial_scan_state() is None

def test_long_interrupted_initial_scan_does_not_notify_stale_items(db):
    from conftest import advance, set_clock_to
    set_clock_to(12)
    f=FeedFetcher(db,source(db))
    cutoff=f.initial_scan_state()
    formerly_recent={'title':'stale after interruption',
                     'url':'https://example.org/stale',
                     'published':clock.to_iso(clock.now(db)+timedelta(hours=1))}
    advance(48*3600)
    recent=item('recent-after-retry',1)
    result=f.register_candidates([formerly_recent,recent],found_via='feed:feed',
                                 initial_cutoff=cutoff)
    assert result=={'created':2,'failed':0}
    assert db.execute('select count(*) from feed_backfill_items').fetchone()[0]==1
    assert db.execute('select count(*) from notification_outbox').fetchone()[0]==1
    assert outbox.enqueue_pending_candidate_reviews(db)==0


def test_feed_item_repoll_refreshes_observation_without_losing_publisher_dates(db,monkeypatch):
    from conftest import advance
    f=FeedFetcher(db,source(db))
    item_data={'id':'tag:example.org,2026:release-1','title':'Stable release',
               'url':'https://example.org/release-1',
               'published':'Mon, 09 Nov 2026 12:00:00 +0000',
               'updated':'Tue, 10 Nov 2026 08:00:00 +0000'}
    f.register_candidates([item_data],found_via='feed:feed')
    first=db.execute('select first_seen_at,last_seen_at,publisher_published_at,publisher_updated_at'
                     ' from feed_item_observations').fetchone()
    advance(60)
    item_data['updated']='Tue, 10 Nov 2026 08:01:00 +0000'
    assert f.register_candidates([item_data],found_via='feed:feed')['created']==0
    second=db.execute('select first_seen_at,last_seen_at,publisher_published_at,publisher_updated_at'
                      ' from feed_item_observations').fetchone()
    assert second[0]==first[0]
    assert second[1]!=first[1]
    assert second[2]==first[2]
    assert second[3]!=first[3]
    assert db.execute('select count(*) from feed_item_observations').fetchone()[0]==1
