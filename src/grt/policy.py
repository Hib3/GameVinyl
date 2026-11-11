"""Policy storage, versioning and shadow evaluation (agreement-24).

Every policy-affecting change (score points / thresholds, quiet policy,
demotion stages, price-drop rate, route caps) goes through a version bump
with a recorded diff. Shadow evaluation re-judges the last 30 days of
match_decisions deterministically under a proposed policy before the user
approves it. Changes that would break the discovery floor guarantee are
rejected by validation.
"""
from __future__ import annotations

import json
from datetime import timedelta

from . import clock
from .config import get_setting, set_setting
from .migrate import current_policy_version

POLICY_KEY = "policy_json"

DEFAULT_POLICY = {
    # Discovery score (agreement-15)
    "discovery": {
        "jp_source": 30, "multi_source": 25, "noroute_band": 25,
        "stage_boost": 25, "rare_form": 20, "known_band_or_label": 15,
        "mid_confidence": 10, "repost_only": -20,
        "immediate_threshold": 60, "digest_top_threshold": 30,
        "floor": 60,
        "floor_conditions": ["known_prefix", "known_label", "nonretail_form",
                             "stage", "wantlist"],
    },
    # Review queue priority (agreement-05)
    "review_priority": {
        "jp_source": 30, "multi_source": 25, "noroute_prefix": 25,
        "mid_confidence": 10, "repost_only": -20,
    },
    # Quiet hours (agreement-18)
    "quiet": {"start": "22:00", "end": "06:30"},
    # Price-drop notification rule (agreement-08)
    "price_drop_rate": 0.10,
    # Matcher score table (agreement-02)
    "matcher_weights": {
        "barcode": 100, "catalog_exact": 100, "catalog_numeric": 40,
        "title_exact": 30, "title_similar": 15, "label": 10, "year": 10,
        "media": 10,
    },
    # Auto-demotion stages in minutes (agreement-16)
    "demotion_stages_min": [15, 60, 360, 1440],
}


def get_policy(db) -> dict:
    raw = get_setting(db, POLICY_KEY)
    policy = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy
    if raw:
        # A corrupt or wrongly-shaped stored policy must never raise
        # here: get_policy sits under every notification enqueue, and a
        # JSONDecodeError would silently kill them all (debugger-14
        # finding, 2026-11-08). Fall back to the defaults instead.
        try:
            stored = json.loads(raw)
        except (ValueError, TypeError):
            stored = None
        if isinstance(stored, dict):
            for k, v in stored.items():
                if isinstance(v, dict) and isinstance(policy.get(k), dict):
                    policy[k].update(v)
                elif isinstance(policy.get(k), dict):
                    # Section is a dict by design; a non-dict stored
                    # value for it is corruption, not an override.
                    continue
                else:
                    policy[k] = v
    return policy


def validate_policy(policy: dict) -> None:
    """Reject changes that break the floor guarantee (agreement-15/24)."""
    d = policy.get("discovery", {})
    floor = d.get("floor")
    imm = d.get("immediate_threshold")
    conds = d.get("floor_conditions") or []
    if floor is None or imm is None or floor > imm:
        raise ValueError("floor must be <= immediate_threshold")
    if not conds:
        raise ValueError("floor_conditions must not be empty")
    known = {"known_prefix", "known_label", "nonretail_form", "stage", "wantlist"}
    if not set(conds) <= known:
        raise ValueError(f"unknown floor conditions: {set(conds) - known}")


def shadow_evaluate(db, new_policy: dict) -> dict:
    """Deterministic recompute over the last 30 days of match_decisions:
    how many decisions would change band under the proposed weights."""
    weights = new_policy.get("matcher_weights", {})
    since = (clock.now(db) - timedelta(days=30)).isoformat()
    rows = db.execute(
        "SELECT * FROM match_decisions WHERE decided_at>=?", (since,)).fetchall()
    out = {"decisions": 0, "would_be_high_price": 0, "would_be_review": 0,
           "band_changes": 0}
    for r in rows:
        out["decisions"] += 1
        breakdown = json.loads(r["score_breakdown_json"] or "{}")
        new_score = sum(weights.get(k, 0) for k in breakdown)
        veto = r["veto"]
        if new_score >= 100 and not veto:
            out["would_be_high_price"] += 1
            band = "高"
        elif new_score >= 60 or veto:
            out["would_be_review"] += 1
            band = "中"
        else:
            band = "低"
        if band != r["existence_confidence"]:
            out["band_changes"] += 1
    return out


def apply_change(db, diff: dict, changed_via: str, note: str = "") -> int:
    """Apply a policy diff after approval: bump version, record diff."""
    policy = get_policy(db)
    for section, changes in diff.items():
        if isinstance(changes, dict) and isinstance(policy.get(section), dict):
            policy[section].update(changes)
        else:
            policy[section] = changes
    validate_policy(policy)
    set_setting(db, POLICY_KEY, json.dumps(policy, ensure_ascii=False))
    new_version = current_policy_version(db) + 1
    db.execute(
        "INSERT INTO policy_versions(version_no, changed_at, changed_via,"
        " diff_json, note) VALUES(?, ?, ?, ?, ?)",
        (new_version, clock.now_iso(db), changed_via,
         json.dumps(diff, ensure_ascii=False), note))
    db.commit()
    return new_version
