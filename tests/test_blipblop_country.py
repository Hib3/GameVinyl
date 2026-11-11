"""Unknown origin is not evidence of a Japanese source."""
from conftest import set_clock_to
from grt import candidates, digest, scoring
from grt.fetchers.blipblop import BlipBlopFetcher

def test_rss_origin_does_not_add_japanese_source_points(db):
    set_clock_to(12)
    source_id = db.execute(
        "insert into sources(name,kind,status,url) values "
        "('blipblop','rss','approved','https://blipblop.net/feed/')"
    ).lastrowid
    db.commit()
    f = BlipBlopFetcher(db, source_id)
    f.register_candidates([{'title':'Example Game Vinyl LP',
                            'url':'https://example.org/release',
                            'summary':'','published':''}])
    row = db.execute("select * from candidate_editions").fetchone()
    sources = candidates.independent_source_count(db, row['id'])
    cand = {'title':row['raw_title'] or row['title'],
            'catalog_norm':row['catalog_norm'],
            'existence_confidence':row['existence_confidence'],
            'distribution_type':row['distribution_type'],
            'independent_sources':sources}
    expected = scoring.review_priority(db,cand)
    queue = db.execute("select priority from review_queue where kind='candidate_review'").fetchone()
    assert queue[0] == expected
    assert candidates._priority_for(db,row,sources) == expected
    stage = db.execute("select event_stage from announcements where candidate_id=?", (row['id'],)).fetchone()
    cand['event_stage'] = stage[0] if stage else None
    scored = scoring.discovery_score(db,cand)
    assert digest._candidate_band(db,row) == (scored['band'],scored['score'])
    assert scoring.review_priority(db,dict(cand,country='JP')) > expected
