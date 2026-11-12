import json
import sqlite3

import pytest

from grt import clock, collector_bridge, outbox
from grt.bot import Bot
from grt.pricedrop import price_evidence_ok

URL = "https://auctions.yahoo.co.jp/jp/auction/c1250831588"
EDITION = "AR-000001"


@pytest.fixture
def feed(db, tmp_path, monkeypatch):
    folder = tmp_path / "collector"
    folder.mkdir()
    monkeypatch.setenv("GRT_DATA_DIR", str(tmp_path))
    path = folder / "collector.sqlite3"
    with sqlite3.connect(path) as source:
        source.executescript("""
            CREATE TABLE events(id INTEGER PRIMARY KEY,url TEXT,created REAL,kind TEXT,detail TEXT);
            CREATE INDEX watch_event_url ON events(url) WHERE kind='watch_new';
            CREATE TABLE jobs(url TEXT PRIMARY KEY,source TEXT,status TEXT,last_error TEXT);
            CREATE TABLE observations(id INTEGER PRIMARY KEY,url TEXT,observed REAL,expires REAL,facts TEXT);
            CREATE INDEX obs_url ON observations(url,id);
        """)
    db.execute("INSERT INTO editions(id,title,watch_level) VALUES(?, 'Unrelated catalogue name', 1)", (EDITION,))
    db.execute("INSERT INTO settings(key,value) VALUES ('allowed_chat_id','555'), ('deadline_override_quiet_selected','1')")
    db.commit()

    def seed(*, availability="AUCTION_OPEN", expired=False, ids=(EDITION,),
             malformed=False):
        now = clock.now(db).timestamp()
        facts = dict(page_type="product", title="Example\nthird-party title",
                     watch_ids=list(ids), availability=availability,
                     auction_end=now+3600, price="123", currency="JPY")
        with sqlite3.connect(path) as source:
            source.execute("INSERT OR IGNORE INTO jobs VALUES(?, 'yahoo_watch','ok',NULL)", (URL,))
            source.execute("INSERT INTO events(url,created,kind,detail) VALUES(?,?,'watch_new',?)",
                           (URL,now-10,"{" if malformed else json.dumps(facts)))
            source.execute("INSERT INTO observations(url,observed,expires,facts) VALUES(?,?,?,?)",
                           (URL,now-20,now-1 if expired else now+3600,json.dumps(facts)))
        return path
    return seed


@pytest.mark.parametrize("approve",[True,False])
def test_active_idempotent_and_human_callback(db, feed, monkeypatch, approve):
    feed()
    assert collector_bridge.poll(db)==1
    assert collector_bridge.poll(db)==0
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]==1
    listing=db.execute("SELECT id,edition_id,price FROM listings").fetchone()
    assert listing[1] is None and listing[2] is None
    assert not price_evidence_ok(db,listing[0],EDITION)
    qid,=db.execute("SELECT id FROM review_queue WHERE kind='match_review'").fetchone()
    event=db.execute("SELECT event_type,body,reply_markup_json FROM notification_outbox").fetchall()
    assert len(event)==1 and event[0][0]=="match_review"
    assert "Example third-party title" in event[0][1] and URL in event[0][1]
    assert json.loads(event[0][2])["inline_keyboard"][0][0]["callback_data"]==f"qok:{qid}"
    assert db.execute("SELECT COUNT(*) FROM candidate_editions").fetchone()[0]==0
    messages=[]
    class Transport:
        def send(self,*args,**kwargs):
            messages.append(kwargs["reply_markup"])
    db.execute("UPDATE notification_outbox SET not_before=NULL,quiet_exempt=1")
    db.commit()
    assert outbox.process_outbox(db,Transport(),chat_id="555")["sent"]==1
    assert outbox.process_outbox(db,Transport(),chat_id="555")["sent"]==0
    assert len(messages)==1
    bot=Bot(db,lambda *a,**kw:{"ok":True})
    monkeypatch.setattr(bot,"_send",lambda *a,**kw:None)
    action=bot.handle_update({"update_id":900001,"callback_query":{"id":"test","message":{"chat":{"id":555}},
                              "data":f"{'qok' if approve else 'qno'}:{qid}"}})
    assert action==("match_review_approved" if approve else "match_review_rejected")
    assert db.execute("SELECT edition_id FROM listings").fetchone()[0]==(EDITION if approve else None)
    assert not price_evidence_ok(db,listing[0],EDITION)


@pytest.mark.parametrize("kwargs",[{"availability":"AUCTION_ENDED"}, {"expired":True}, {"ids":("AR-999999",)}])
def test_ineligible_no_review_or_notification(db,feed,kwargs):
    feed(**kwargs)
    assert collector_bridge.poll(db)==0
    for table in ("listings","candidate_editions","review_queue","notification_outbox"):
        assert db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]==0


def test_write_error_keeps_cursor_and_rolls_back(db,feed):
    feed()
    db.execute("CREATE TRIGGER reject_review BEFORE INSERT ON notification_outbox "
               "BEGIN SELECT RAISE(ABORT,'test write failure'); END")
    db.commit()
    assert collector_bridge.poll(db)==0
    assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]==0
    assert db.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]==0
    assert db.execute("SELECT value FROM settings WHERE key=?",(collector_bridge.CURSOR,)).fetchone() is None
    db.execute("DROP TRIGGER reject_review")
    db.commit()
    assert collector_bridge.poll(db)==1


def test_malformed_event_keeps_cursor(db,feed,caplog):
    path=feed(malformed=True)
    assert collector_bridge.poll(db)==0
    assert URL not in caplog.text
    with sqlite3.connect(path) as source:
        source.execute("UPDATE events SET detail=(SELECT facts FROM observations)")
    assert collector_bridge.poll(db)==1


def test_collector_unavailable_leaves_event_retryable(db,feed,monkeypatch):
    feed()
    def unavailable():
        raise sqlite3.OperationalError("test source unavailable")
    with monkeypatch.context() as m:
        m.setattr(collector_bridge,"_open_collector",unavailable)
        assert collector_bridge.poll(db)==0
        assert db.execute("SELECT value FROM settings WHERE key=?",(collector_bridge.CURSOR,)).fetchone() is None
    assert collector_bridge.poll(db)==1


def test_ended_after_enqueue_is_not_delivered(db,feed):
    path=feed()
    assert collector_bridge.poll(db)==1
    with sqlite3.connect(path) as source:
        source.execute("UPDATE jobs SET status='ended'")
    class NoSend:
        def send(self,*args,**kwargs):
            pytest.fail("ended listing was sent")
    db.execute("UPDATE notification_outbox SET not_before=NULL,quiet_exempt=1")
    db.commit()
    assert outbox.process_outbox(db,NoSend(),chat_id='555')['sent']==0
    assert db.execute("SELECT state FROM notification_outbox").fetchone()[0]=="cancelled"
