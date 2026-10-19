"""Run the bounded, read-only historical query-gap analysis.

The module deliberately exposes only :func:`analyze` and a small CLI.  The
SQL is part of this package and is never selected by a caller.  Database
values are passed as SQLite parameters; the database itself is opened with
``mode=ro`` and a progress handler enforces the execution deadline.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "query_gap.sql"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_OUTPUT_ROWS = 500  # Evidence rows, excluding the always-returned summary.
REQUIRED_LOAD_DATASETS = (
    "search_requests",
    "recall_events",
    "historical_candidate_replay",
)
REQUIRED_TABLES = (
    "queries",
    "search_requests",
    "recall_events",
    "historical_candidate_replay",
    "historical_eligibility",
    "load_manifest",
)
TASK_FIELDS = (
    "query_id",
    "raw_query",
    "city_id",
    "region_id",
    "scene_id",
    "history_start",
    "history_end_exclusive",
)
_REGION_RE = re.compile(r"^(?P<city>[A-Za-z0-9_-]+)-(?P<region>[AB])$")
_READ_ONLY_SQL_START = re.compile(r"^\s*(?:--[^\n]*\n\s*)*WITH\b", re.IGNORECASE)
_MUTATING_SQL = re.compile(
    r"\b(?:ATTACH|ALTER|CREATE|DELETE|DETACH|DROP|INSERT|REINDEX|REPLACE|TRUNCATE|UPDATE|VACUUM)\b",
    re.IGNORECASE,
)


class QueryHistoryError(ValueError):
    """Raised when the task or isolated business database violates the input contract."""


def _read_sql() -> tuple[str, str]:
    """Load the one packaged query and reject a non-read-only replacement."""

    try:
        sql_bytes = SQL_PATH.read_bytes()
    except OSError as exc:
        raise QueryHistoryError(f"controlled SQL is unavailable: {SQL_PATH}") from exc
    sql = sql_bytes.decode("utf-8")
    if not _READ_ONLY_SQL_START.match(sql):
        raise QueryHistoryError("controlled SQL must start with a WITH query")
    if ";" in sql:
        raise QueryHistoryError("controlled SQL must contain one statement without a semicolon")
    if _MUTATING_SQL.search(sql):
        raise QueryHistoryError("controlled SQL contains a mutating statement")
    return sql, hashlib.sha256(sql_bytes).hexdigest()


def _parse_plus_eight(value: str, field: str) -> _dt.datetime:
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise QueryHistoryError(f"{field} must be ISO8601 with +08:00") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != _dt.timedelta(hours=8):
        raise QueryHistoryError(f"{field} must include the +08:00 offset")
    return parsed


def _validate_task(task: Mapping[str, Any]) -> tuple[_dt.datetime, _dt.datetime]:
    if not isinstance(task, Mapping):
        raise QueryHistoryError("task must be an object")
    missing = [field for field in TASK_FIELDS if field not in task]
    if missing:
        raise QueryHistoryError(f"task is missing required fields: {', '.join(missing)}")
    non_strings = [field for field in TASK_FIELDS if not isinstance(task[field], str)]
    if non_strings:
        raise QueryHistoryError(f"task fields must be strings: {', '.join(non_strings)}")
    empty = [field for field in TASK_FIELDS if not task[field]]
    if empty:
        raise QueryHistoryError(f"task fields must not be empty: {', '.join(empty)}")

    region_match = _REGION_RE.fullmatch(task["region_id"])
    if not region_match or region_match.group("city") != task["city_id"]:
        raise QueryHistoryError("region_id must be city_id-A or city_id-B")

    start = _parse_plus_eight(task["history_start"], "history_start")
    end = _parse_plus_eight(task["history_end_exclusive"], "history_end_exclusive")
    if start >= end:
        raise QueryHistoryError("history_start must be earlier than history_end_exclusive")
    return start, end


def _window_dates(start: _dt.datetime, end: _dt.datetime) -> list[str]:
    """Return every local +08:00 date touched by the half-open window."""

    shanghai = _dt.timezone(_dt.timedelta(hours=8))
    start_local = start.astimezone(shanghai)
    end_local = end.astimezone(shanghai)
    current = start_local.date()
    last = end_local.date()
    dates: list[str] = []
    while current <= last:
        day_start = _dt.datetime.combine(current, _dt.time.min, tzinfo=shanghai)
        day_end = day_start + _dt.timedelta(days=1)
        if day_start < end_local and day_end > start_local:
            dates.append(current.isoformat())
        current += _dt.timedelta(days=1)
    return dates


def _safe_database_uri(database: str | Path) -> str:
    if isinstance(database, Path):
        database_text = str(database)
    elif isinstance(database, str):
        database_text = database
    else:
        raise QueryHistoryError("database must be a filesystem path")
    if not database_text or database_text.startswith("file:") or database_text == ":memory:":
        raise QueryHistoryError("database must be a local SQLite file path, not a URI")
    if "\x00" in database_text:
        raise QueryHistoryError("database path contains a NUL byte")
    path = Path(database_text).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise QueryHistoryError(f"database does not exist: {database_text}") from exc
    if not resolved.is_file():
        raise QueryHistoryError(f"database is not a regular file: {database_text}")
    # quote() prevents a path character from becoming a URI query parameter.
    return f"file:{quote(str(resolved), safe='/')}?mode=ro"


def _check_tables(conn: sqlite3.Connection) -> list[str]:
    placeholders = ",".join("?" for _ in REQUIRED_TABLES)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (" + placeholders + ")",
        REQUIRED_TABLES,
    ).fetchall()
    present = {row[0] for row in rows}
    return [table for table in REQUIRED_TABLES if table not in present]


def _check_query_identity(conn: sqlite3.Connection, task: Mapping[str, str]) -> None:
    row = conn.execute(
        "SELECT query_id, raw_query, scene_id FROM queries WHERE query_id = ?",
        (task["query_id"],),
    ).fetchone()
    if row is None:
        raise QueryHistoryError(f"query_id not found in queries: {task['query_id']}")
    if row["raw_query"] != task["raw_query"]:
        raise QueryHistoryError("query_id/raw_query mismatch")
    if row["scene_id"] != task["scene_id"]:
        raise QueryHistoryError("query_id/scene_id mismatch")


def _check_load_manifest(
    conn: sqlite3.Connection,
    dates: list[str],
    city_id: str,
) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    statement = (
        "SELECT dataset, biz_date, city_id, status, loaded_at "
        "FROM load_manifest WHERE dataset = ? AND biz_date = ? AND city_id = ? "
        "ORDER BY rowid"
    )
    for dataset in REQUIRED_LOAD_DATASETS:
        for biz_date in dates:
            rows = conn.execute(statement, (dataset, biz_date, city_id)).fetchall()
            if len(rows) != 1:
                gaps.append(
                    {
                        "dataset": dataset,
                        "biz_date": biz_date,
                        "city_id": city_id,
                        "reason": "missing_partition" if not rows else "duplicate_partition",
                        "observed_rows": str(len(rows)),
                    }
                )
                continue
            row = rows[0]
            if row["status"] != "complete":
                gaps.append(
                    {
                        "dataset": dataset,
                        "biz_date": biz_date,
                        "city_id": city_id,
                        "reason": "partition_not_complete",
                        "status": str(row["status"]),
                    }
                )
    return gaps


def _empty_result(
    task: Mapping[str, str],
    sql_sha256: str,
    load_gaps: list[dict[str, str]],
    *,
    missing_tables: list[str] | None = None,
) -> dict[str, Any]:
    gaps = list(load_gaps)
    for table in missing_tables or []:
        gaps.append({"dataset": table, "reason": "missing_table"})
    return {
        "request_count": 0,
        "eligible_count": 0,
        "keyword_miss_count": 0,
        "supply_fail_count": 0,
        "plan_fail_count": 0,
        "unknown_count": 0,
        "verified_gap_count": 0,
        "complete": False,
        "load_gaps": gaps,
        "parameters": dict(task),
        "source": "sqlite:read-only",
        "source_details": {
            "database_mode": "ro",
            "required_load_datasets": list(REQUIRED_LOAD_DATASETS),
            "history_verification": "independent_replay_plus_unique_historical_eligibility",
        },
        "sql_sha256": sql_sha256,
        "evidence_rows": [],
        "candidate_gap_evidence_ref": "#/evidence_rows",
        "candidate_gap_evidence_count": 0,
        "candidate_gap_evidence_total": 0,
        "candidate_gap_evidence_verified_count": 0,
        "evidence_rows_returned": 0,
        "evidence_rows_total": 0,
        "output_truncated": False,
        "verification_blocked": bool(gaps),
    }


def _progress_handler(deadline: float):
    def check_deadline() -> int:
        return 1 if time.monotonic() >= deadline else 0

    return check_deadline


def _decorate_candidate_gap_evidence(
    rows: list[dict[str, Any]],
    *,
    complete: bool,
) -> list[dict[str, Any]]:
    """Make the historical-only nature of each candidate record explicit."""

    decorated: list[dict[str, Any]] = []
    for row in rows:
        status = row.get("verification_status")
        historically_qualified = complete and status == "verified"
        fields = ['request_id','event_time','term_id','ad_plan_id','offer_id','sku_id',
                  'replay_index_version','replay_row_count','snapshot_id','snapshot_count',
                  'verification_status','verification_reason']
        decorated.append({**{key: row[key] for key in fields},
                          'historically_qualified': historically_qualified})
    return decorated


def analyze(database: str | Path, task: Mapping[str, Any]) -> dict[str, Any]:
    """Return the request-level historical funnel for one fixed task.

    The function has no SQL or SQL-path argument by design.  ``database`` is
    the only caller-supplied path; it is opened through a local ``mode=ro``
    URI.  The returned ``verified_gap_count`` counts distinct keyword-miss
    requests, not replay rows or plans, and is suppressed when required
    partitions are incomplete.
    """

    start, end = _validate_task(task)
    sql, sql_sha256 = _read_sql()
    parameters = {field: task[field] for field in TASK_FIELDS}
    uri = _safe_database_uri(database)
    load_gaps: list[dict[str, str]] = []
    conn: sqlite3.Connection | None = None
    deadline = time.monotonic() + float(DEFAULT_TIMEOUT_SECONDS)
    if DEFAULT_TIMEOUT_SECONDS <= 0:
        raise TimeoutError("historical SQL exceeded the execution deadline")

    try:
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.0)
        except sqlite3.Error as exc:
            raise QueryHistoryError(f"unable to open database read-only: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        missing_tables = _check_tables(conn)
        if missing_tables:
            return _empty_result(task, sql_sha256, [], missing_tables=missing_tables)

        _check_query_identity(conn, task)
        load_gaps = _check_load_manifest(conn, _window_dates(start, end), task["city_id"])

        conn.set_progress_handler(_progress_handler(deadline), 1000)
        bound_parameters = dict(parameters)
        bound_parameters["output_limit"] = int(MAX_OUTPUT_ROWS) + 1
        try:
            result_rows = conn.execute(sql, bound_parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
                raise TimeoutError("historical SQL exceeded the execution deadline") from exc
            raise QueryHistoryError(f"controlled historical SQL failed: {exc}") from exc
        finally:
            conn.set_progress_handler(None, 0)

        rows = [dict(row) for row in result_rows]
        summary = next((row for row in rows if row.get("row_kind") == "summary"), None)
        if summary is None:
            raise QueryHistoryError("controlled SQL did not return its summary row")

        complete = not load_gaps
        evidence_rows = [row for row in rows if row.get("row_kind") == "evidence"]
        if not complete:
            # Partial source data may be shown for review, but never presented
            # as a verified commercial opportunity.
            evidence_rows = [
                {
                    **row,
                    "verification_status": "unknown",
                    "verification_reason": "required_load_partition_incomplete",
                }
                for row in evidence_rows
            ]
        evidence_rows = _decorate_candidate_gap_evidence(evidence_rows, complete=complete)
        candidate_gap_evidence_verified_count = sum(
            1 for row in evidence_rows if row.get("historically_qualified")
        )

        result = {
            "request_count": int(summary["request_count"] or 0),
            "eligible_count": int(summary["eligible_count"] or 0),
            "keyword_miss_count": int(summary["keyword_miss_count"] or 0),
            "supply_fail_count": int(summary["supply_fail_count"] or 0),
            "plan_fail_count": int(summary["plan_fail_count"] or 0),
            "unknown_count": int(summary["unknown_count"] or 0),
            "verified_gap_count": int(summary["verified_gap_count"] or 0) if complete else 0,
            "complete": complete,
            "load_gaps": load_gaps,
            "parameters": parameters,
            "source": "sqlite:read-only",
            "source_details": {
                "database_mode": "ro",
                "required_load_datasets": list(REQUIRED_LOAD_DATASETS),
                "history_verification": "independent_replay_plus_unique_historical_eligibility",
                "request_outcome_order": ["eligible", "keyword_miss", "supply_fail", "plan_fail", "unknown"],
            },
            "sql_sha256": sql_sha256,
            "evidence_rows": evidence_rows,
            "candidate_gap_evidence_ref": "#/evidence_rows",
            "evidence_contract": {
                "evidence_type": "potential_supply_opportunity",
                "evidence_scope": "independent_historical_replay_only",
                "attribute_version_comparison": "not_available",
                "semantic_review_status": "not_run",
                "safe_review_gate": "hold_pending_semantic_review",
                "note": "Historical qualification does not prove semantic equivalence. Row verification_status is the historical qualification state; no duplicate status aliases.",
            },
            "candidate_gap_evidence_count": len(evidence_rows),
            "candidate_gap_evidence_total": int(summary["evidence_rows_count"] or 0),
            "candidate_gap_evidence_verified_count": candidate_gap_evidence_verified_count,
            "evidence_rows_returned": len(evidence_rows),
            "evidence_rows_total": int(summary["evidence_rows_count"] or 0),
            "output_truncated": int(summary["evidence_rows_count"] or 0) > len(evidence_rows),
            "evidence_row_limit": int(MAX_OUTPUT_ROWS),
            "evidence_order": "request_id, term_id, ad_plan_id, offer_id; first rows, not a random sample",
            "verification_blocked": not complete,
            "history_unknown_request_count": int(summary["history_unknown_request_count"] or 0),
        }
        return result
    finally:
        if conn is not None:
            conn.close()


def _load_json(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryHistoryError(f"invalid JSON input: {path}") from exc


def _write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed read-only medical query history analysis")
    parser.add_argument("--database", required=True, help="path to an existing SQLite database")
    parser.add_argument("--input", required=True, help="path to the task JSON")
    parser.add_argument("--output", required=True, help="path for the analysis JSON")
    args = parser.parse_args(argv)
    try:
        task = _load_json(args.input)
        result = analyze(args.database, task)
    except (QueryHistoryError, TimeoutError, sqlite3.Error) as exc:
        error = {"error": type(exc).__name__, "message": str(exc)}
        try:
            _write_json(args.output, error)
        except OSError:
            pass
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2
    _write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
