"""Import Muse price findings into a small, separate local history database.

The source ZIP is read in place and is never extracted. The default CLI mode
validates only; --write is required to append the validated snapshot.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import zipfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RECORD_ID = re.compile(r"AR-\d{6}\Z")
STATUS = {"CURRENT_CONFIRMED", "PAST_ONLY", "NO_ROUTE", "NO_SALES_DATA", "BLOCKED"}
MEMBER_LIMIT = 16 * 1024 * 1024
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _member(archive: zipfile.ZipFile, suffix: str) -> tuple[str, bytes]:
    matches = [info for info in archive.infolist() if info.filename.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"expected one ZIP member ending in {suffix}, found {len(matches)}")
    if matches[0].file_size > MEMBER_LIMIT:
        raise ValueError(f"ZIP member is too large: {suffix}")
    return matches[0].filename, archive.read(matches[0])


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON number: {value}")


def _safe_source_urls(notes: str) -> list[str]:
    urls = set()
    for raw in URL_RE.findall(notes):
        candidate = raw.rstrip(".,;:!?)]}")
        try:
            parsed = urlsplit(candidate)
            if (parsed.scheme.lower() != "https" or not parsed.hostname
                    or parsed.username or parsed.password
                    or parsed.port not in (None, 443)
                    or parsed.query or parsed.fragment):
                continue
        except ValueError:
            continue
        urls.add(candidate)
    return sorted(urls)


def prepare_import(archive_path: Path, catalog_ids: set[str]) -> tuple[dict, list[dict]]:
    """Validate every CSV row against the source ledger and canonical IDs."""
    with zipfile.ZipFile(archive_path) as archive:
        csv_name, csv_bytes = _member(archive, "records_with_price_evidence.csv")
        _, ledger_bytes = _member(archive, "results/ledger.json")
        date_match = re.search(r"FINAL_SURVEY[_-](\d{4}-\d{2}-\d{2})", csv_name)
        if not date_match:
            raise ValueError("survey date missing from final survey path")
        survey_date = date_match.group(1)
        datetime.strptime(survey_date, "%Y-%m-%d")
    ledger = json.loads(ledger_bytes, parse_constant=_reject_constant)
    evidence = ledger.get("evidence") if isinstance(ledger, dict) else None
    if not isinstance(evidence, list):
        raise ValueError("ledger evidence must be a list")
    ledger_by_id = {}
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("ledger evidence row must be an object")
        record_id = item.get("record_id")
        if not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id):
            raise ValueError("ledger contains an invalid record ID")
        if record_id in ledger_by_id:
            raise ValueError("ledger contains duplicate record IDs")
        ledger_by_id[record_id] = item

    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    required = {"record_id", "status", "lowest_current_price_usd", "sales_stats", "sources"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("price evidence CSV has an unexpected schema")
    ids = [row.get("record_id", "") for row in rows]
    if any(not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id) for record_id in ids):
        raise ValueError("price evidence CSV contains an invalid record ID")
    if len(ids) != len(set(ids)):
        raise ValueError("price evidence CSV contains duplicate record IDs")
    if not set(ids) <= catalog_ids:
        raise ValueError("price evidence CSV contains IDs outside the canonical catalog")

    dataset_id = hashlib.sha256(csv_bytes + b"\0" + ledger_bytes).hexdigest()
    entries = []
    for row in rows:
        record_id = row["record_id"]
        item = ledger_by_id.get(record_id)
        status = row["status"]
        if item is None or item.get("status") != status or status not in STATUS:
            raise ValueError("CSV status does not match the source ledger")
        checked_at = item.get("checked_at")
        if not isinstance(checked_at, str):
            raise ValueError("ledger checked_at is missing")
        try:
            datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("ledger checked_at is not ISO formatted") from exc
        price_text = row.get("lowest_current_price_usd", "").strip()
        try:
            price = float(Decimal(price_text)) if price_text else None
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise ValueError("CSV contains an invalid USD price") from exc
        if price is not None and (not math.isfinite(price) or price <= 0):
            raise ValueError("CSV contains a non-positive USD price")
        stats_text = row.get("sales_stats", "").strip()
        stats = None
        if stats_text:
            stats = json.loads(stats_text, parse_constant=_reject_constant)
            if not isinstance(stats, dict):
                raise ValueError("sales_stats must be a JSON object")
        source_summary = row.get("sources", "").strip()
        if len(source_summary) > 200:
            raise ValueError("source summary is too long")
        entries.append({
            "dataset_id": dataset_id,
            "edition_id": record_id,
            "survey_date": survey_date,
            "checked_at": checked_at,
            "status": status,
            "price_usd": price,
            "source_summary": source_summary,
            "sales_stats_json": json.dumps(stats, ensure_ascii=False,
                                             separators=(",", ":"), allow_nan=False) if stats is not None else None,
            "source_urls_json": json.dumps(_safe_source_urls(str(item.get("notes") or "")),
                                            separators=(",", ":")),
        })
    metadata = {
        "dataset_id": dataset_id,
        "survey_date": survey_date,
        "row_count": len(entries),
        "priced_rows": sum(row["price_usd"] is not None for row in entries),
        "source_url_rows": sum(bool(json.loads(row["source_urls_json"])) for row in entries),
        "status_counts": {key: sum(row["status"] == key for row in entries)
                          for key in sorted(STATUS)},
    }
    return metadata, entries


def _validate_target(path: Path) -> None:
    if not path.exists():
        return
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as db:
            tables = {row[0] for row in db.execute(
                "select name from sqlite_master where type='table'")}
    except sqlite3.Error as exc:
        raise ValueError("output exists but is not a readable SQLite database") from exc
    unexpected = tables - {"muse_price_imports", "muse_price_history"}
    if unexpected:
        raise ValueError("refusing to write into a database with unrelated tables")


def write_import(path: Path, metadata: dict, entries: list[dict]) -> None:
    path = Path(path)
    _validate_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute("pragma foreign_keys=on")
        db.execute("pragma journal_mode=DELETE")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""
            CREATE TABLE IF NOT EXISTS muse_price_imports(
              dataset_id TEXT PRIMARY KEY, survey_date TEXT NOT NULL,
              row_count INTEGER NOT NULL, priced_rows INTEGER NOT NULL,
              imported_at TEXT NOT NULL
            )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS muse_price_history(
              dataset_id TEXT NOT NULL REFERENCES muse_price_imports(dataset_id),
              edition_id TEXT NOT NULL, survey_date TEXT NOT NULL,
              checked_at TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN
                ('CURRENT_CONFIRMED','PAST_ONLY','NO_ROUTE','NO_SALES_DATA','BLOCKED')),
              price_usd REAL CHECK(price_usd IS NULL OR price_usd > 0),
              source_summary TEXT NOT NULL,
              sales_stats_json TEXT,
              source_urls_json TEXT NOT NULL,
              PRIMARY KEY(dataset_id,edition_id)
            )""")
        imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("""INSERT OR IGNORE INTO muse_price_imports
              VALUES(?,?,?,?,?)""", (metadata["dataset_id"], metadata["survey_date"],
                                        metadata["row_count"], metadata["priced_rows"], imported_at))
        db.executemany("""INSERT OR IGNORE INTO muse_price_history
              VALUES(:dataset_id,:edition_id,:survey_date,:checked_at,:status,
                     :price_usd,:source_summary,:sales_stats_json,:source_urls_json)""", entries)
        if db.execute("pragma quick_check").fetchone() != ("ok",):
            raise sqlite3.DatabaseError("price history quick_check failed")
        if db.execute("pragma foreign_key_check").fetchall():
            raise sqlite3.DatabaseError("price history foreign_key_check failed")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--write", action="store_true", help="append after validation (default: dry run)")
    args = parser.parse_args(argv)
    try:
        seed = json.loads((ROOT / "seed" / "records.json").read_text(encoding="utf-8"))
        catalog_ids = {row.get("id") for row in seed if isinstance(row, dict)}
        metadata, entries = prepare_import(args.archive, catalog_ids)
        if args.write:
            write_import(args.output, metadata, entries)
            metadata["written"] = True
        else:
            metadata["written"] = False
        print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
            sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Muse price import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
