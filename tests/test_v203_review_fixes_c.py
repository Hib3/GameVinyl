"""Regression tests for the multi-perspective review of Phase 2/3
(2026-11-08, collector persona — variant completeness, parent
veto, /price & /coverage honesty)."""
from grt import matcher, variant_sweep
from grt.bot import Bot


# C1a: one confirmed variant with a single channel stays unknown.
def test_single_variant_one_channel_stays_unknown(db):
    db.execute(
        "INSERT INTO editions(id, title, rarity) VALUES"
        " ('AR-1','T','R5')")
    db.execute(
        "INSERT INTO edition_variants(edition_id, status) VALUES"
        " ('AR-1', 'confirmed')")
    db.commit()
    assert variant_sweep.evaluate_completeness(db, "AR-1") == "unknown"


# C1b: two confirmed variants -> multiple; grants never leave domain.
def test_multiple_and_domain(db):
    db.execute(
        "INSERT INTO editions(id, title, rarity) VALUES"
        " ('AR-1','T','R5')")
    for _ in range(2):
        db.execute(
            "INSERT INTO edition_variants(edition_id, status) VALUES"
            " ('AR-1', 'confirmed')")
    db.commit()
    assert variant_sweep.evaluate_completeness(db, "AR-1") == "multiple"
    variant_sweep.run_sweep(db)
    val = db.execute(
        "SELECT variant_completeness FROM editions WHERE id='AR-1'"
    ).fetchone()[0]
    assert val in ("unknown", "single_confirmed", "multiple")


# C1c: a legit single_confirmed is not destroyed by the sweep.
def test_sweep_preserves_single_confirmed(db):
    db.execute(
        "INSERT INTO editions(id, title, rarity, discogs_id,"
        " variant_completeness) VALUES('AR-1','T','R5', 99,"
        " 'single_confirmed')")
    db.execute(
        "INSERT INTO edition_variants(edition_id, status, evidence_url)"
        " VALUES('AR-1', 'confirmed', 'https://example.jp/e')")
    db.commit()
    variant_sweep.run_sweep(db)
    val = db.execute(
        "SELECT variant_completeness FROM editions WHERE id='AR-1'"
    ).fetchone()[0]
    assert val == "single_confirmed"


# C6: unconfirmed_count counts unconfirmed variant ROWS.
def test_unconfirmed_count_units(db):
    db.execute(
        "INSERT INTO editions(id, title, variant_completeness)"
        " VALUES('AR-1','T','single_confirmed')")
    db.execute(
        "INSERT INTO editions(id, title, variant_completeness)"
        " VALUES('AR-2','T','multiple')")
    db.commit()
    assert variant_sweep.unconfirmed_count(db) == 0
    db.execute(
        "INSERT INTO edition_variants(edition_id, status) VALUES"
        " ('AR-1', 'unverified')")
    db.commit()
    assert variant_sweep.unconfirmed_count(db) == 1


# C4: the parent veto fires for hyphenated / raw-key barcodes too.
def test_parent_veto_normalizes_barcode(db):
    db.execute(
        "INSERT INTO parent_products(parent_title, parent_barcode)"
        " VALUES('Game Box', '4900000000002')")
    db.execute(
        "INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.commit()
    for listing in ({"barcode": "4900-0000-0000-2"},
                    {"barcode_raw": "4900000000002"}):
        res = matcher.match_listing(db, listing)
        assert res and res[0]["vetoes"] == ["parent_product"]


class _Transport:
    def __call__(self, *a, **kw):
        return {"ok": True}


def _bot(db, monkeypatch):
    db.execute(
        "INSERT INTO settings(key, value) VALUES"
        " ('allowed_chat_id', '555'),"
        " ('deadline_override_quiet_selected', '1')")
    db.commit()
    bot = Bot(db, _Transport())
    sent = []
    monkeypatch.setattr(bot, "_send",
                        lambda chat, text, reply_markup=None:
                        sent.append(text))
    return bot, sent


# C5: /coverage price evidence requires an actual price.
def test_coverage_counts_only_real_price_evidence(db, monkeypatch):
    for i in range(4):
        db.execute(
            f"INSERT INTO editions(id, title) VALUES('AR-{i}','T')")
    db.execute(
        "INSERT INTO price_snapshots(edition_id, source, taken_at,"
        " num_for_sale, lowest_price) VALUES('AR-0', 'discogs',"
        " '2026-11-08', 0, NULL)")
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    bot.handle_update({"update_id": 1, "message": {
        "chat": {"id": 555}, "text": "/coverage"}})
    joined = "\n".join(sent)
    assert "価格証拠あり: 0件" in joined


# C7: /price buckets are per-variant.
def test_price_buckets_split_by_variant(db, monkeypatch):
    db.execute("INSERT INTO editions(id, title) VALUES('AR-1','T')")
    db.execute(
        "INSERT INTO edition_variants(id, edition_id, status) VALUES"
        " (1, 'AR-1', 'confirmed'), (2, 'AR-1', 'confirmed')")
    for vid, price in ((1, 3000), (2, 9000)):
        db.execute(
            "INSERT INTO listings(edition_id, variant_id, source, url,"
            " price, total_jpy, cond_rank, status) VALUES('AR-1', ?,"
            " 's', ?, ?, ?, 3, 'active')",
            (vid, f"https://x/{vid}", price, price))
    db.commit()
    bot, sent = _bot(db, monkeypatch)
    bot.handle_update({"update_id": 2, "message": {
        "chat": {"id": 555}, "text": "/price AR-1"}})
    joined = "\n".join(sent)
    assert "バリアント1" in joined and "バリアント2" in joined
    # W-check B3 display update: amounts carry a thousands
    # separator (3,000 / 9,000), as everywhere else in /price.
    assert "3,000" in joined and "9,000" in joined
