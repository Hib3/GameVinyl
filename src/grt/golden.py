"""Golden-set regression gate (agreements 20/24, acceptance #2).

Re-judges all golden cases with the current matcher and diffs against the
expected two-layer confidence and disposition. A price-'high' false
positive (matcher says price 高 where the expectation is not 高) is a
release blocker and reported separately.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config
from .matcher import evaluate


def golden_path() -> Path:
    return config.PROJECT_ROOT / "tests" / "golden_cases.json"


def load_cases(path: Path | None = None) -> list[dict]:
    data = json.loads((path or golden_path()).read_text(encoding="utf-8"))
    return data["cases"] if isinstance(data, dict) else data


def run_golden(path: Path | None = None) -> dict:
    cases = load_cases(path)
    diffs: list[dict] = []
    price_false_positives: list[str] = []
    for case in cases:
        listing = dict(case["listing"])
        exp = case["expected"]
        want = {"existence": exp["existence"], "price": exp["price"],
                "disposition": exp["disposition"]}
        if "editions" in case:
            # Tie case: several editions judged against one listing; a top
            # tie withholds judgement (all mid / review).
            results = [evaluate(listing, dict(e), case.get("variant"))
                       for e in case["editions"]]
            top = max(r["score"] for r in results)
            tied = [r for r in results if r["score"] == top]
            result = dict(tied[0])
            if len(tied) > 1 and top >= 60:
                result["disposition"] = "review"
                if result["existence_confidence"] == "高":
                    result["existence_confidence"] = "中"
                if result["price_confidence"] == "高":
                    result["price_confidence"] = "中"
        else:
            result = evaluate(listing, dict(case["edition"]),
                              case.get("variant"))
        got = {"existence": result["existence_confidence"],
               "price": result["price_confidence"],
               "disposition": result["disposition"]}
        if got != want:
            diffs.append({"id": case["id"], "category": case.get("category"),
                          "expected": want, "got": got,
                          "score": result["score"], "vetoes": result["vetoes"],
                          "gate_case": result["gate_case"]})
        if result["price_confidence"] == "高" and exp["price"] != "高":
            price_false_positives.append(case["id"])
    return {"total": len(cases), "diffs": diffs,
            "price_false_positives": price_false_positives,
            "passed": len(cases) - len(diffs)}
