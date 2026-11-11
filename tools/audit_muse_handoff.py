"""Read-only deterministic audit of a Muse handoff ZIP against the site DB.

The command never extracts the ZIP, opens .env, contacts a source, or writes
to SQLite. Use --issues-csv to create a local review queue explicitly.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote


def _json_member(archive: zipfile.ZipFile, suffix: str):
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"expected one ZIP member ending in {suffix}, found {len(matches)}")
    return json.loads(archive.read(matches[0]))


def _nonempty(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _decode_object(value, where: str) -> dict:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {where}")
    return value


def _read_site_db(path: Path) -> dict[str, dict]:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(editions)")}
        required = {"id", "muse_json", "game_enrichment_json"}
        if not required <= columns:
            raise ValueError("site DB editions table is missing required audit columns")
        return {
            str(record_id): {
                "muse": _decode_object(muse, f"editions.{record_id}.muse_json"),
                "enrichment": _decode_object(enrichment, f"editions.{record_id}.game_enrichment_json"),
            }
            for record_id, muse, enrichment in conn.execute(
                "SELECT id, muse_json, game_enrichment_json FROM editions")
        }
    finally:
        conn.close()


def audit_handoff(archive_path: Path, site_db_path: Path) -> tuple[dict, list[dict]]:
    """Compare source layers and claims; never promotes or alters any value."""
    with zipfile.ZipFile(archive_path) as archive:
        records = _json_member(archive, "webdata/records.json")
        enrichment_doc = _json_member(archive, "webdata/game_enrichment.json")
        ledger = _json_member(archive, "results/ledger.json")
        deals = _json_member(archive, "webdata/deals_current.json")
    if not isinstance(records, list) or not isinstance(enrichment_doc, dict):
        raise ValueError("unexpected Muse records or enrichment schema")
    muse_enrichment = enrichment_doc.get("by_record_id")
    if not isinstance(muse_enrichment, dict) or not isinstance(ledger, dict):
        raise ValueError("unexpected Muse enrichment or ledger schema")

    record_counts = Counter(str(row.get("id") or "") for row in records)
    duplicate_ids = sorted(key for key, count in record_counts.items() if not key or count > 1)
    muse_by_id = {str(row["id"]): row for row in records if row.get("id")}
    site = _read_site_db(site_db_path)
    ledger_rows = ledger.get("evidence", [])
    if not isinstance(ledger_rows, list):
        raise ValueError("unexpected results/ledger.json evidence schema")
    ledger_by_id = {str(row["record_id"]): row for row in ledger_rows if row.get("record_id")}
    expansion = ledger.get("expansion_evidence", [])
    if not isinstance(expansion, list):
        raise ValueError("unexpected expansion evidence schema")

    issues: list[dict] = []

    def issue(record_id: str, kind: str, field: str = "", record=None, evidence=None):
        record, evidence = record or {}, evidence or {}
        issues.append({
            "record_id": record_id,
            "candidate_id": evidence.get("candidate_id"),
            "issue": kind,
            "field": field,
            "record_status": record.get("status"),
            "ledger_status": evidence.get("status"),
            "expansion_outcome": evidence.get("outcome"),
            "checked_at": evidence.get("checked_at"),
            "route": evidence.get("route"),
            "source_url": evidence.get("source_url"),
            "explanation": evidence.get("what_established") or evidence.get("notes"),
        })

    for record_id in duplicate_ids:
        issue(record_id, "archive_duplicate_or_empty_id")

    field_conflicts: Counter = Counter()
    field_presence_gaps: Counter = Counter()
    muse_payload_missing = 0
    enrichment_mismatches = 0
    status_drift = 0
    status_missing = 0
    status_missing_with_price = 0
    ledger_missing = 0
    for record_id, record in muse_by_id.items():
        db_record = site.get(record_id)
        evidence = ledger_by_id.get(record_id, {})
        if db_record is None:
            issue(record_id, "site_db_record_missing", record=record, evidence=evidence)
            continue
        db_muse = db_record["muse"]
        if record_id not in ledger_by_id:
            ledger_missing += 1
            issue(record_id, "ledger_evidence_missing", record=record)
        if not db_muse:
            muse_payload_missing += 1
            issue(record_id, "site_db_muse_payload_missing", record=record, evidence=evidence)
        for field, source_value in record.items():
            if field == "id":
                continue
            target_value = db_muse.get(field)
            if _nonempty(source_value) and _nonempty(target_value) and source_value != target_value:
                field_conflicts[field] += 1
                issue(record_id, "muse_field_conflict", field, record, evidence)
            elif _nonempty(source_value) != _nonempty(target_value):
                field_presence_gaps[field] += 1
                issue(record_id, "muse_field_presence_gap", field, record, evidence)
        expected_enrichment = muse_enrichment.get(record_id, {})
        if expected_enrichment != db_record["enrichment"]:
            enrichment_mismatches += 1
            issue(record_id, "game_enrichment_mismatch", record=record, evidence=evidence)
        if "status" not in record or record.get("status") is None:
            status_missing += 1
            if _nonempty(record.get("price")):
                status_missing_with_price += 1
            issue(record_id, "muse_status_missing", record=record, evidence=evidence)
        elif evidence and record.get("status") != evidence.get("status"):
            status_drift += 1
            issue(record_id, "muse_ledger_status_drift", record=record, evidence=evidence)

    for record_id in sorted(set(site) - set(muse_by_id)):
        issue(record_id, "site_db_record_not_in_muse_archive")

    for row in expansion:
        record_id = str(row.get("record_id") or "")
        record = muse_by_id.get(record_id, {})
        if not _nonempty(row.get("source_url")):
            issue(record_id, "expansion_source_url_missing", record=record, evidence=row)
        if not _nonempty(row.get("what_established")):
            issue(record_id, "expansion_what_established_missing", record=record, evidence=row)

    expansion_without_what_established = sum(not _nonempty(row.get("what_established")) for row in expansion)
    expansion_without_notes = sum(not _nonempty(row.get("notes")) for row in expansion)
    expansion_without_explanation = sum(
        not _nonempty(row.get("what_established")) and not _nonempty(row.get("notes"))
        for row in expansion)
    expansion_without_url = sum(not _nonempty(row.get("source_url")) for row in expansion)
    evidence_without_url = sum(not _nonempty(row.get("source_url")) for row in ledger_rows)
    null_status_expansions: dict[str, set[str]] = defaultdict(set)
    for row in expansion:
        record_id = str(row.get("record_id") or "")
        record = muse_by_id.get(record_id)
        if record_id and record and record.get("status") is None:
            null_status_expansions[str(row.get("outcome") or "(missing)")].add(record_id)
    deals_items = deals.get("deals", []) if isinstance(deals, dict) else []
    if not isinstance(deals_items, list):
        deals_items = []
    summary = {
        "archive_record_count": len(records),
        "archive_unique_id_count": len(muse_by_id),
        "duplicate_or_empty_ids": len(duplicate_ids),
        "site_db_record_count": len(site),
        "site_records_missing_from_db": sum(record_id not in site for record_id in muse_by_id),
        "site_records_not_in_archive": sum(record_id not in muse_by_id for record_id in site),
        "site_muse_payload_missing": muse_payload_missing,
        "muse_field_conflicts_by_field": dict(sorted(field_conflicts.items())),
        "muse_field_presence_gaps_by_field": dict(sorted(field_presence_gaps.items())),
        "game_enrichment_source_count": len(muse_enrichment),
        "game_enrichment_db_mismatches": enrichment_mismatches,
        "muse_status_counts": dict(sorted(Counter(row.get("status") or "(missing)" for row in records).items())),
        "ledger_status_counts": dict(sorted(Counter(row.get("status") or "(missing)" for row in ledger_rows).items())),
        "muse_ledger_status_drift": status_drift,
        "ledger_evidence_missing": ledger_missing,
        "muse_status_missing": status_missing,
        "muse_status_missing_with_price": status_missing_with_price,
        "muse_status_missing_with_expansion": len(set().union(*null_status_expansions.values())) if null_status_expansions else 0,
        "muse_status_missing_expansion_outcomes": dict(sorted((k, len(v)) for k, v in null_status_expansions.items())),
        "ledger_rows_without_item_source_url": evidence_without_url,
        "expansion_rows": len(expansion),
        "expansion_outcome_counts": dict(sorted(Counter(row.get("outcome") or "(missing)" for row in expansion).items())),
        "expansion_rows_without_source_url": expansion_without_url,
        "expansion_rows_without_what_established": expansion_without_what_established,
        "expansion_rows_without_notes": expansion_without_notes,
        "expansion_rows_without_any_explanation": expansion_without_explanation,
        "deals_candidate_count": len(deals_items),
        "deals_checked_date": deals.get("checked_date") if isinstance(deals, dict) else None,
        "deals_unique_record_ids": len({str(row.get("record_id")) for row in deals_items if row.get("record_id")}),
        "deals_attribution_counts": dict(sorted(Counter(row.get("attribution") or "(missing)" for row in deals_items).items())),
        "assessment": "review_required" if issues or evidence_without_url or expansion_without_explanation else "consistent",
    }
    return summary, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--site-db", required=True, type=Path)
    parser.add_argument("--issues-csv", type=Path, help="optional local review queue output")
    args = parser.parse_args(argv)
    if not args.archive.is_file() or not args.site_db.is_file():
        parser.error("archive and site DB must be existing files")
    if args.issues_csv and args.issues_csv.resolve() in {args.archive.resolve(), args.site_db.resolve()}:
        parser.error("issues CSV must not overwrite either input")
    try:
        summary, issues = audit_handoff(args.archive, args.site_db)
        if args.issues_csv:
            args.issues_csv.parent.mkdir(parents=True, exist_ok=True)
            with args.issues_csv.open("w", newline="", encoding="utf-8-sig") as output:
                writer = csv.DictWriter(output, fieldnames=[
                    "record_id", "candidate_id", "issue", "field", "record_status",
                    "ledger_status", "expansion_outcome",
                    "checked_at", "route", "source_url", "explanation"])
                writer.writeheader()
                writer.writerows(issues)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile, sqlite3.Error) as exc:
        print(f"audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
