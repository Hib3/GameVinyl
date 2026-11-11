"""Phase 3 acceptance tests (prompt v2 section 7, Phase 3) plus
Phase 2 operational leftovers: variant sweep (#9), store watch,
suffix-dictionary discipline (#4), seed separation (#1), prefix
sweep budget/states (#2/#3), cross-check same-table rule (#5),
bot commands (#11 of Phase 2)."""
from grt import seedops, storewatch, sweep, variant_sweep
from grt.bot import Bot


# ---- Phase 3 #1: seeds are separate, priceless, gated promotion
# Review fix (Maniac A, D5): promotion gates are VERIFIED against
# records — a resolved 'approved' seed_promotion queue row and a
# composite key computed from the found record — never caller
# booleans. The old boolean-assertion API was removed for that
# reason.
def test_seed_separation_and_promotion_gates(db):
    sid = seedops.import_seed(db, title="Doujin CD X", circle="CircleA",
                              catalog_norm="DMCD-001", event_name="M3")
    assert sid is not None
    assert seedops.seed_count(db) == 1
    assert seedops.active_candidate_count(db) == 0
    row = db.execute(
        "SELECT price_confidence FROM candidate_editions WHERE id=?",
        (sid,)).fetchone()
    assert row[0] is None  # seeds carry no price evidence
    found = {"title": "Doujin CD X", "circle": "CircleA",
             "catalog_norm": "DMCD-001"}
    # No review item exists at all -> refused.
    out = seedops.promote_seed(db, sid, found=found, review_queue_id=999)
    assert out["promoted"] is False
    qid = seedops.request_seed_promotion(db, sid)
    # Review item exists but is unresolved -> refused.
    out = seedops.promote_seed(db, sid, found=found, review_queue_id=qid)
    assert out["promoted"] is False
    db.execute(
        "UPDATE review_queue SET resolved_at='2026-11-08T00:00:00+09:00',"
        " resolution='approved' WHERE id=?", (qid,))
    db.commit()
    # Approved, but the found record's composite key differs.
    out = seedops.promote_seed(
        db, sid, found={"title": "Other", "circle": "CircleB",
                        "catalog_norm": "DMCD-999"},
        review_queue_id=qid)
    assert out["promoted"] is False
    # Approved + composite key matches -> promoted.
    out = seedops.promote_seed(db, sid, found=found, review_queue_id=qid)
    assert out["promoted"] is True
    assert seedops.seed_count(db) == 0


# ---- Phase 3 #2/#3: sweep budget, states, review-queue routing
def test_prefix_sweep_budget_and_states(db):
    db.execute("INSERT INTO priority_catalog_prefixes VALUES('TEST-')")
    db.commit()

    def querier(catalog):
        if catalog == "TEST-3":
            return "hit"
        if catalog == "TEST-4":
            return "blocked"
        return "none"

    out = sweep.run_weekly_sweep(db, querier=querier, budget=5,
                                 centres=[("TEST-", 3)])
    assert out["queries"] == 5  # budget respected
    states = dict(db.execute(
        "SELECT number, state FROM catalog_prefix_ledger WHERE"
        " prefix='TEST-'").fetchall())
    assert states.get(3) == "hit"
    assert states.get(4) == "blocked"  # never collapsed to none
    q = db.execute(
        "SELECT COUNT(*) FROM review_queue WHERE kind='sweep_hit'"
    ).fetchone()[0]
    assert q == 1  # hits go to review, not instant notification
    outbox = db.execute(
        "SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
    assert outbox == 0


def test_prefix_sweep_band_expansion_rule(db):
    for n in range(0, 11):
        db.execute(
            "INSERT INTO catalog_prefix_ledger(prefix, number, state)"
            " VALUES('B-', ?, 'searched_none')", (n,))
    db.commit()
    assert sweep._band_complete(db, "B-", 5, 5) is True
    assert sweep._band_complete(db, "B-", 5, 20) is False


# ---- Phase 3 #4: suffix dictionary discipline
def test_suffix_dictionary_requires_evidence_and_approval(db):
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('-P','AR-1',"
        " NULL, 1, 1)")
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('-R','AR-1',"
        " 'https://example.jp/ev', 0, 1)")
    db.commit()
    # -P lacks an evidence URL; -R is unapproved: neither may apply.
    assert sweep.approved_suffixes(db) == []


# ---- Phase 3 #5: cross-check raises only via the shared score table
def test_crosscheck_uses_shared_thresholds(db):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-9','T')")
    cur = db.execute(
        "INSERT INTO candidate_editions(title, status) VALUES('T',"
        " 'candidate_review')")
    db.commit()
    cid = int(cur.lastrowid)

    def fake_eval_low(item, edition):
        return {"score": 30, "existence_confidence": "low"}

    def fake_eval_high(item, edition):
        return {"score": 95, "existence_confidence": "high"}

    # The shared table's high label is the Japanese high marker; the
    # fakes must use the real vocabulary to exercise the real gate.
    def fake_eval_high_jp(item, edition):
        return {"score": 95, "existence_confidence": "\u9ad8"}

    out = seedops.crosscheck_candidate(
        db, cid, [{"raw_title": "x"}], matcher_evaluate=fake_eval_low,
        edition={"id": "AR-9"})
    assert out["raised"] is False
    out = seedops.crosscheck_candidate(
        db, cid, [{"raw_title": "x"}],
        matcher_evaluate=fake_eval_high_jp, edition={"id": "AR-9"})
    assert out["raised"] is True


# ---- Phase 2 #9: variant sweep priority + grant/revoke records
# Review fix (collector D1): the value domain is the spec's
# {unknown, single_confirmed, multiple}; the old 'complete' value
# was outside it and broke the matcher's P2 gate. single_confirmed
# needs ONE confirmed variant + >=2 independent channels.
def test_variant_sweep_priority_and_records(db):
    db.execute(
        "INSERT INTO editions(id, title, country, rarity, discogs_id)"
        " VALUES('AR-OV','Overseas', 'Germany', 'R5', 4242)")
    db.execute(
        "INSERT INTO edition_variants(edition_id, status, evidence_url)"
        " VALUES('AR-OV', 'confirmed', 'https://example.jp/ev')")
    db.commit()
    prio = variant_sweep.priority_editions(db)
    assert prio and prio[0] == "AR-OV"
    out = variant_sweep.run_sweep(db)
    assert out["granted"] == 1
    row = db.execute(
        "SELECT variant_completeness FROM editions WHERE id='AR-OV'"
    ).fetchone()
    assert row[0] == "single_confirmed"
    ev = db.execute(
        "SELECT COUNT(*) FROM system_events WHERE"
        " kind='variant_completeness_granted'").fetchone()[0]
    assert ev == 1


# ---- store watch: baseline then change detection
class _Resp:
    def __init__(self, text):
        self.status_code = 200
        self.text = text


class _Client:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return _Resp(self.pages[url])


def test_storewatch_change_detection(db):
    cur = db.execute(
        "INSERT INTO sources(name, url, kind, status) VALUES('Label',"
        " 'https://label.jp/shop', 'STORE', 'approved')")
    db.commit()
    sid = int(cur.lastrowid)
    client = _Client({"https://label.jp/shop": "page v1"})
    out = storewatch.check_source(db, sid, client=client)
    assert out["result"] == "BASELINE"
    out = storewatch.check_source(db, sid, client=client)
    assert out["changed"] is False
    client.pages["https://label.jp/shop"] = "page v2 with new item"
    out = storewatch.check_source(db, sid, client=client)
    assert out["changed"] is True


# ---- Phase 2 #11: the four commands answer
class _Transport:
    def __init__(self):
        self.sent = []

    def __call__(self, *a, **kw):
        self.sent.append((a, kw))
        return {"ok": True}


def _make_bot(db, monkeypatch):
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('allowed_chat_id', '555')")
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('deadline_override_quiet_selected', '1')")
    db.commit()
    bot = Bot(db, _Transport())
    sent = []
    monkeypatch.setattr(bot, "_send",
                        lambda chat, text, reply_markup=None:
                        sent.append(text))
    return bot, sent


def test_bot_phase2_commands(db, monkeypatch):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.commit()
    bot, sent = _make_bot(db, monkeypatch)
    for i, cmd in enumerate(("/price AR-1", "/queue", "/deadlines",
                             "/coverage")):
        bot.handle_update({"update_id": 1000 + i,
                           "message": {"chat": {"id": 555},
                                       "text": cmd}})
    joined = "\n".join(sent)
    assert "\u4fa1\u683c" in joined or "AR-1" in joined
    assert len(sent) >= 4


def test_bot_watch_rarity_range(db, monkeypatch):
    for i, r in enumerate(("R3", "R4", "R5")):
        db.execute(
            "INSERT INTO editions(id, title, rarity) VALUES"
            f" ('AR-{i}', 'T{i}', '{r}')")
    db.commit()
    bot, sent = _make_bot(db, monkeypatch)
    bot.handle_update({"update_id": 424242,
                       "message": {"chat": {"id": 555},
                                   "text": "/watch R4-R5"}})
    levels = dict(db.execute(
        "SELECT rarity, watch_level FROM editions").fetchall())
    assert levels == {"R3": 0, "R4": 2, "R5": 2}
