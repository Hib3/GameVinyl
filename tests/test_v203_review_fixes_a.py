"""Regression tests for the multi-perspective review of Phase 2/3
(2026-11-08, Maniac A — catalog archaeology). Each test names the
review defect it pins down."""
from grt import seedops, sweep


def _ledger(db, prefix, centre, half, state):
    for n in range(max(0, centre - half), centre + half + 1):
        db.execute(
            "INSERT INTO catalog_prefix_ledger(prefix, number, state)"
            " VALUES(?, ?, ?)", (prefix, n, state))
    db.commit()


# D1: centres come from real edition numbers, not the constant 1.
def test_sweep_centres_derived_from_editions(db):
    db.execute("INSERT INTO priority_catalog_prefixes VALUES('ZZ')")
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm) VALUES"
        " ('AR-Z1', 'T', 'ZZ105')")
    db.commit()
    seen = []

    def querier(catalog):
        seen.append(catalog)
        return "none"

    sweep.run_weekly_sweep(db, querier=querier, budget=50)
    assert "ZZ105" in seen and "ZZ100" in seen and "ZZ110" in seen
    assert "ZZ1" not in seen and "ZZ0" not in seen


# D2: a fully BLOCKED band is not complete and must not widen.
def test_blocked_band_does_not_widen(db):
    _ledger(db, "XX", 100, 5, "blocked")
    seen = []

    def querier(catalog):
        seen.append(catalog)
        return "none"

    sweep.run_weekly_sweep(db, querier=querier, budget=100,
                           centres=[("XX", 100)])
    nums = {int(c[2:]) for c in seen}
    assert nums and max(nums) <= 105 and min(nums) >= 95


# D2b: a resolved band WITH a hit widens to ±20.
def test_hit_band_widens(db):
    _ledger(db, "XX", 100, 5, "searched_none")
    db.execute(
        "UPDATE catalog_prefix_ledger SET state='hit' WHERE"
        " prefix='XX' AND number=100")
    db.commit()
    seen = []

    def querier(catalog):
        seen.append(catalog)
        return "none"

    sweep.run_weekly_sweep(db, querier=querier, budget=100,
                           centres=[("XX", 100)])
    nums = {int(c[2:]) for c in seen}
    assert 85 in nums and 115 in nums


# D3: unsearched count reflects reality (was hard-wired to 0).
def test_unsearched_band_count_is_real(db):
    db.execute("INSERT INTO priority_catalog_prefixes VALUES('ZZ')")
    db.execute(
        "INSERT INTO editions(id, title, catalog_norm) VALUES"
        " ('AR-Z1', 'T', 'ZZ105')")
    db.commit()
    assert sweep.unsearched_band_count(db) == 11
    sweep.run_weekly_sweep(db, querier=lambda c: "none", budget=50)
    assert sweep.unsearched_band_count(db) == 0


# D4: approved suffixes are actually probed within the budget.
def test_suffix_variants_are_probed(db):
    db.execute(
        "INSERT INTO editions(id, title) VALUES('AR-1', 'T')")
    db.execute(
        "INSERT INTO catalog_suffix_rules(suffix, base_edition_id,"
        " evidence_url, approved, policy_version) VALUES('A',"
        " 'AR-1', 'https://example.jp/ev', 1, 1)")
    db.commit()
    seen = []

    def querier(catalog):
        seen.append(catalog)
        return "hit" if catalog == "XX100A" else "none"

    out = sweep.run_weekly_sweep(db, querier=querier, budget=100,
                                 centres=[("XX", 100)])
    assert "XX100A" in seen
    q = db.execute(
        "SELECT payload_json FROM review_queue WHERE"
        " kind='sweep_hit'").fetchall()
    assert any("XX100A" in r[0] for r in q)
    assert out["queries"] > 11  # suffix probes count in the budget


# D6: a raising querier degrades to blocked, never aborts the run.
def test_sweep_survives_querier_exception(db):
    calls = {"n": 0}

    def querier(catalog):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("network exploded")
        return "none"

    out = sweep.run_weekly_sweep(db, querier=querier, budget=50,
                                 centres=[("XX", 100)])
    assert out["queries"] == 11
    states = dict(db.execute(
        "SELECT number, state FROM catalog_prefix_ledger WHERE"
        " prefix='XX'").fetchall())
    assert "blocked" in states.values()


# D8: cross-check never raises a seed's confidence.
def test_crosscheck_refuses_seeds(db):
    sid = seedops.import_seed(db, title="Seed CD", circle="C1",
                              event_name="M3")
    out = seedops.crosscheck_candidate(
        db, sid, [{"raw_title": "x"}],
        matcher_evaluate=lambda i, e: {"score": 999,
                                       "existence_confidence": "高"},
        edition={"id": "AR-1"})
    assert out["raised"] is False
