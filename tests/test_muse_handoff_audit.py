import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

from tools.audit_muse_handoff import audit_handoff, main


def _fixture_inputs(tmp_path: Path):
    archive_path = tmp_path / "muse.zip"
    prefix = "handoff/"
    records = [
        {"id": "AR-1", "title": "Known", "status": None, "price": 12},
        {"id": "AR-2", "title": "Source title", "status": "PAST_ONLY"},
    ]
    enrichment = {"by_record_id": {
        "AR-1": {"game": "Game A"},
        "AR-2": {"game": "Game B"},
    }}
    ledger = {
        "evidence": [
            {"record_id": "AR-1", "status": "CURRENT_CONFIRMED",
             "checked_at": "2026-10-27", "route": "discogs", "notes": "old claim"},
            {"record_id": "AR-2", "status": "CURRENT_CONFIRMED",
             "checked_at": "2026-10-28", "route": "discogs",
             "source_url": "https://example.test/release/2"},
        ],
        "expansion_evidence": [
            {"record_id": "AR-1", "source_url": None, "what_established": None},
        ],
    }
    deals = {"checked_date": "2026-11-02", "deals": [{"record_id": "AR-1", "attribution": "OK"}]}
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, value in (("webdata/records.json", records),
                            ("webdata/game_enrichment.json", enrichment),
                            ("results/ledger.json", ledger),
                            ("webdata/deals_current.json", deals)):
            archive.writestr(prefix + name, json.dumps(value))

    db_path = tmp_path / "site.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE editions (id TEXT PRIMARY KEY, muse_json TEXT, game_enrichment_json TEXT)")
    rows = [
        ("AR-1", {"id": "AR-1", "title": "Known", "status": None, "price": 12}, {"game": "Game A"}),
        ("AR-2", {"id": "AR-2", "title": "Different title", "status": "PAST_ONLY"}, {}),
        ("AR-3", {"id": "AR-3"}, None),
    ]
    conn.executemany("INSERT INTO editions VALUES (?, ?, ?)",
                     [(key, json.dumps(muse), json.dumps(game)) for key, muse, game in rows])
    conn.commit()
    conn.close()
    return archive_path, db_path


def test_read_only_audit_reports_lineage_and_status_drift(tmp_path):
    archive, db = _fixture_inputs(tmp_path)
    before = hashlib.sha256(db.read_bytes()).digest()

    summary, issues = audit_handoff(archive, db)

    assert summary["archive_record_count"] == 2
    assert summary["site_db_record_count"] == 3
    assert summary["site_records_not_in_archive"] == 1
    assert summary["muse_field_conflicts_by_field"] == {"title": 1}
    assert summary["game_enrichment_db_mismatches"] == 1
    assert summary["muse_ledger_status_drift"] == 1
    assert summary["muse_status_missing"] == 1
    assert summary["muse_status_missing_with_price"] == 1
    assert summary["muse_status_missing_with_expansion"] == 1
    assert summary["ledger_rows_without_item_source_url"] == 1
    assert summary["expansion_rows_without_source_url"] == 1
    assert summary["expansion_rows_without_what_established"] == 1
    assert summary["expansion_rows_without_notes"] == 1
    assert summary["expansion_rows_without_any_explanation"] == 1
    assert summary["deals_candidate_count"] == 1
    assert summary["deals_unique_record_ids"] == 1
    assert summary["deals_attribution_counts"] == {"OK": 1}
    assert summary["assessment"] == "review_required"
    assert any(row["issue"] == "muse_field_conflict" and row["field"] == "title"
               for row in issues)
    assert any(row["issue"] == "muse_status_missing" and row["record_id"] == "AR-1"
               for row in issues)
    assert any(row["issue"] == "expansion_source_url_missing" and row["record_id"] == "AR-1"
               for row in issues)
    assert any(row["issue"] == "expansion_what_established_missing"
               and row["candidate_id"] is None for row in issues)
    assert hashlib.sha256(db.read_bytes()).digest() == before


def test_ambiguous_archive_members_fail_closed(tmp_path):
    archive, db = _fixture_inputs(tmp_path)
    with zipfile.ZipFile(archive, "a") as z:
        # A second archive member with the same suffix is ambiguous and rejected.
        z.writestr("another/webdata/records.json", "[]")
    try:
        audit_handoff(archive, db)
    except ValueError as exc:
        assert "expected one ZIP member" in str(exc)
    else:
        raise AssertionError("ambiguous archive member should fail closed")


def test_duplicate_record_ids_are_counted_for_review(tmp_path):
    archive, db = _fixture_inputs(tmp_path)
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    records_member = next(name for name in members if name.endswith("webdata/records.json"))
    records = json.loads(members[records_member])
    records.append(dict(records[0]))
    members[records_member] = json.dumps(records).encode()
    with zipfile.ZipFile(archive, "w") as updated:
        for name, content in members.items():
            updated.writestr(name, content)

    summary, issues = audit_handoff(archive, db)

    assert summary["duplicate_or_empty_ids"] == 1
    assert any(row["issue"] == "archive_duplicate_or_empty_id" for row in issues)


def test_cli_writes_only_requested_review_csv(tmp_path, capsys):
    archive, db = _fixture_inputs(tmp_path)
    output = tmp_path / "review" / "issues.csv"

    assert main(["--archive", str(archive), "--site-db", str(db),
                 "--issues-csv", str(output)]) == 0

    assert '"assessment": "review_required"' in capsys.readouterr().out
    csv_text = output.read_text(encoding="utf-8-sig")
    assert "muse_field_conflict" in csv_text
    assert "source_url" in csv_text
    assert ",12," not in csv_text
