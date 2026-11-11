"""Round 10 (shipped-artifact reproduction) regression tests.

Automates the two properties verified by hand from the shipped ZIP:
(1) a fresh database created from schema.sql natively carries the
    columns that older installs only gain lazily (fetch_runs.
    fetcher_kind, notification_outbox.reply_markup_json);
(2) enqueue_candidate_review refuses to queue a review message for a
    candidate id that does not exist (found during the v1.4 -> v1.5
    upgrade rehearsal, where a phantom review row could be queued).
"""
from grt import db as dbmod
from grt import outbox


def _cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def test_fresh_schema_has_lazy_columns_natively(tmp_path):
    db = dbmod.connect(tmp_path / "fresh.db")
    dbmod.init_schema(db)
    assert "fetcher_kind" in _cols(db, "fetch_runs")
    assert "reply_markup_json" in _cols(db, "notification_outbox")


def test_enqueue_candidate_review_rejects_phantom_candidate(tmp_path):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    assert outbox.enqueue_candidate_review(db, 999999) is None
    n = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
    assert n == 0


def test_enqueue_candidate_review_still_works_for_real_candidate(tmp_path):
    db = dbmod.connect(tmp_path / "t.db")
    dbmod.init_schema(db)
    db.execute(
        "INSERT INTO candidate_editions(title, status)"
        " VALUES('Real', 'pending')")
    db.commit()
    cid = db.execute("SELECT MAX(id) FROM candidate_editions").fetchone()[0]
    oid = outbox.enqueue_candidate_review(db, cid)
    assert oid is not None
    # idempotent: a second call queues nothing new (enqueue returns
    # None when the idempotency key already exists)
    assert outbox.enqueue_candidate_review(db, cid) is None
    n = db.execute(
        "SELECT COUNT(*) FROM notification_outbox"
        " WHERE idempotency_key=?",
        ("candidate_review|" + str(cid),)).fetchone()[0]
    assert n == 1
