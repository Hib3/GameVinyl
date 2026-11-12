import csv
import importlib.util
import io
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from tools.import_muse_price_history import prepare_import, write_import

_site_spec = importlib.util.spec_from_file_location(
    "site_audit_site", Path(__file__).resolve().parents[1] / "site" / "audit_site.py")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "site"))
_site_module = importlib.util.module_from_spec(_site_spec)
_site_spec.loader.exec_module(_site_module)
muse_price_history = _site_module.muse_price_history


def _archive(path, *, mismatch=False, duplicate=False):
    rows = [
        {"record_id": "AR-000001", "status": "CURRENT_CONFIRMED",
         "lowest_current_price_usd": "12.50",
         "sales_stats": json.dumps({"median_usd": 15, "last_sold": "2026-10-26"}),
         "sources": "Discogs"},
        {"record_id": "AR-000002", "status": "PAST_ONLY",
         "lowest_current_price_usd": "", "sales_stats": "", "sources": "archive"},
    ]
    if duplicate:
        rows.append(dict(rows[0]))
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    evidence = [
        {"record_id": "AR-000001", "status": "NO_ROUTE" if mismatch else "CURRENT_CONFIRMED",
         "checked_at": "2026-10-28", "lowest_current_price_usd": "12.50",
         "notes": "source https://www.discogs.com/release/12345-title"},
        {"record_id": "AR-000002", "status": "PAST_ONLY", "checked_at": "2026-10-27",
         "notes": "query https://example.test/item?token=redacted http://insecure.test/item"},
    ]
    ledger = json.dumps({"evidence": evidence})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("handoff/results/FINAL_SURVEY_2026-10-27/records_with_price_evidence.csv",
                         stream.getvalue())
        archive.writestr("handoff/results/ledger.json", ledger)
    return path


def test_prepare_import_is_read_only_and_keeps_only_safe_historical_links(tmp_path):
    archive = _archive(tmp_path / "muse.zip")
    metadata, rows = prepare_import(archive, {"AR-000001", "AR-000002"})

    assert metadata["row_count"] == 2
    assert metadata["priced_rows"] == 1
    assert metadata["source_url_rows"] == 1
    assert rows[0]["source_urls_json"] == '["https://www.discogs.com/release/12345-title"]'
    assert rows[1]["source_urls_json"] == "[]"
    assert not (tmp_path / "history.sqlite3").exists()


def test_write_is_idempotent_and_preserves_per_edition_checked_dates(tmp_path):
    archive = _archive(tmp_path / "muse.zip")
    metadata, rows = prepare_import(archive, {"AR-000001", "AR-000002"})
    target = tmp_path / "history.sqlite3"

    write_import(target, metadata, rows)
    write_import(target, metadata, rows)

    with sqlite3.connect(target) as db:
        assert db.execute("select count(*) from muse_price_history").fetchone() == (2,)
        assert db.execute("select count(*) from muse_price_imports").fetchone() == (1,)
        assert db.execute("select checked_at from muse_price_history where edition_id='AR-000002'").fetchone() == ("2026-10-27",)
        assert db.execute("pragma quick_check").fetchone() == ("ok",)


def test_prepare_import_rejects_ledger_drift_duplicate_and_unknown_ids(tmp_path):
    mismatch = _archive(tmp_path / "mismatch.zip", mismatch=True)
    with pytest.raises(ValueError, match="status does not match"):
        prepare_import(mismatch, {"AR-000001", "AR-000002"})

    duplicate = _archive(tmp_path / "duplicate.zip", duplicate=True)
    with pytest.raises(ValueError, match="duplicate record IDs"):
        prepare_import(duplicate, {"AR-000001", "AR-000002"})

    valid = _archive(tmp_path / "unknown.zip")
    with pytest.raises(ValueError, match="outside the canonical catalog"):
        prepare_import(valid, {"AR-000001"})


def test_write_refuses_to_modify_a_catalog_database(tmp_path):
    archive = _archive(tmp_path / "muse.zip")
    metadata, rows = prepare_import(archive, {"AR-000001", "AR-000002"})
    target = tmp_path / "site.sqlite3"
    with sqlite3.connect(target) as db:
        db.execute("create table editions(id text primary key)")
    with pytest.raises(ValueError, match="unrelated tables"):
        write_import(target, metadata, rows)
    with sqlite3.connect(target) as db:
        assert db.execute("select name from sqlite_master where type='table'").fetchall() == [("editions",)]


def test_history_is_read_only_and_keeps_old_price_separate(tmp_path):
    archive = _archive(tmp_path / "muse.zip")
    metadata, rows = prepare_import(archive, {"AR-000001", "AR-000002"})
    target = tmp_path / "history.sqlite3"
    write_import(target, metadata, rows)

    history = muse_price_history(target)

    assert history["AR-000001"]["priceUsd"] == 12.5
    assert history["AR-000001"]["surveyDate"] == "2026-10-27"
    assert history["AR-000001"]["sourceUrls"] == ["https://www.discogs.com/release/12345-title"]
    assert "AR-000003" not in history
    with sqlite3.connect(target) as db:
        assert db.execute("select count(*) from muse_price_history").fetchone() == (2,)
