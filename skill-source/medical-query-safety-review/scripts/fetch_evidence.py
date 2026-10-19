"""Read-only HTTP evidence adapter for the local medical-query mock service.

The adapter deliberately has a small surface area: ``collect_catalog`` reads the
term index and independent terminology reference, while ``collect_candidates``
keeps each term/advertising-plan association and enriches it with the five
separate fact sources exposed by ``mock/api_server.py``.

There is no fallback to files or to a real service.  A configured endpoint must
be a loopback HTTP(S) endpoint in ``mode == "mock"``.  All network work is GET
only, redirects are disabled, and credentials never appear in returned data.
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
    ProxyHandler,
)


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_TOTAL_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_PAGE_SIZE = 100
DEFAULT_MAX_BATCH_IDS = 50
DEFAULT_TOKEN_ENV = "MEDICAL_DEMO_TOKEN"

_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class ConfigurationError(ValueError):
    """Raised before any request when the local-only configuration is invalid."""


class _ResponseSchemaError(ValueError):
    """Raised for a malformed response envelope or malformed health payload."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Disable urllib's normal redirect behavior, including external redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


@dataclass(frozen=True)
class _ClientConfig:
    mode: str
    base_url: str
    token: str | None
    timeout_seconds: float
    max_retries: int
    total_timeout_seconds: float
    max_pages: int
    max_page_size: int
    max_batch_ids: int
    config_dir: Path | None
    lexicon_path: Path | None


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be positive") from exc
    if result <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return result


def _bounded_integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} is invalid") from exc
    if result < low or result > high:
        raise ConfigurationError(f"{name} is invalid")
    return result


def _resolve_local_path(value: Any, config_dir: Path | None) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigurationError("lexicon path is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute() and config_dir is not None:
        path = config_dir / path
    return path.resolve()


def _find_lexicon_path(config: Mapping[str, Any], config_dir: Path | None) -> Path | None:
    for key in (
        "lexicon_path",
        "reference_lexicon_path",
        "dictionary_path",
        "terminology_catalog",
    ):
        if config.get(key) is not None:
            return _resolve_local_path(config[key], config_dir)

    for key in ("data_dir", "mock_data_dir"):
        if config.get(key) is not None:
            directory = _resolve_local_path(config[key], config_dir)
            if directory is not None:
                return (directory / "标准词与别名.json").resolve()

    # The installed Skill owns references/terminology-catalog.json.  During
    # teacher-engine tests that file may not yet be populated, so the bundled
    # mock reference dictionary is the second, still independent, source.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates = (
            parent / "references" / "terminology-catalog.json",
            parent / "mock" / "数据" / "标准词与别名.json",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def _normalize_config(config: Mapping[str, Any], config_dir: Path | None = None) -> _ClientConfig:
    if not isinstance(config, Mapping):
        raise ConfigurationError("config must be an object")
    if config_dir is None and config.get("_config_dir") is not None:
        config_dir = _resolve_local_path(config.get("_config_dir"), None)

    mode = config.get("mode")
    if mode != "mock":
        raise ConfigurationError("only mock mode is supported")

    raw_base_url = config.get("base_url")
    if not isinstance(raw_base_url, str) or not raw_base_url.strip():
        raise ConfigurationError("base_url is required")
    parsed = urlsplit(raw_base_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ConfigurationError("base_url must use HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("base_url must not contain query or fragment")
    try:
        # Accessing port catches malformed port values before a request.
        parsed.port
    except ValueError as exc:
        raise ConfigurationError("base_url port is invalid") from exc
    if not _is_loopback_host(parsed.hostname):
        raise ConfigurationError("base_url must point to loopback")

    normalized_base = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )
    token_env = config.get("token_env", DEFAULT_TOKEN_ENV)
    if token_env is not None and not isinstance(token_env, str):
        raise ConfigurationError("token_env is invalid")
    token = os.environ.get(token_env) if token_env else None
    if token is None and config.get("demo_token") is not None:
        if not isinstance(config.get("demo_token"), str):
            raise ConfigurationError("demo_token is invalid")
        token = config.get("demo_token")

    timeout_seconds = _positive_number(
        config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds"
    )
    total_timeout_seconds = _positive_number(
        config.get("total_timeout_seconds", DEFAULT_TOTAL_TIMEOUT_SECONDS),
        "total_timeout_seconds",
    )
    max_retries = _bounded_integer(
        config.get("max_retries", DEFAULT_MAX_RETRIES), "max_retries", 0, 20
    )
    max_pages = _bounded_integer(
        config.get("max_pages", DEFAULT_MAX_PAGES), "max_pages", 1, 10000
    )
    max_page_size = _bounded_integer(
        config.get("max_page_size", DEFAULT_MAX_PAGE_SIZE),
        "max_page_size",
        1,
        100,
    )
    max_batch_ids = _bounded_integer(
        config.get("max_batch_ids", DEFAULT_MAX_BATCH_IDS),
        "max_batch_ids",
        1,
        50,
    )

    explicit_lexicon = _find_lexicon_path(config, config_dir)
    return _ClientConfig(
        mode=mode,
        base_url=normalized_base,
        token=token,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        total_timeout_seconds=total_timeout_seconds,
        max_pages=max_pages,
        max_page_size=max_page_size,
        max_batch_ids=max_batch_ids,
        config_dir=config_dir,
        lexicon_path=explicit_lexicon,
    )


def _iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _safe_code(value: Any, fallback: str = "UPSTREAM_ERROR") -> str:
    if isinstance(value, str) and _CODE_RE.fullmatch(value):
        return value
    return fallback


def _redact(value: Any, secret: str | None) -> Any:
    """Copy data while ensuring a configured bearer token cannot be serialized."""

    if not secret:
        return copy.deepcopy(value)
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value


def _dedupe_errors(errors: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for error in errors:
        normalized = dict(error)
        marker = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            output.append(normalized)
    return output


def _normalize_ids(value: Sequence[str] | str, name: str) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        raise ValueError(f"{name} must be a sequence or comma-separated string")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} contains an invalid ID")
        item = item.strip()
        if item not in seen:
            seen.add(item)
            result.append(item)
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _read_limited(stream: Any) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise _ResponseSchemaError("response too large")
    return body


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ResponseSchemaError("response is not valid JSON") from exc


def _extract_http_error_code(raw: bytes, fallback: str) -> str:
    try:
        payload = _decode_json(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        return _safe_code(error.get("code"), fallback) if isinstance(error, dict) else fallback
    except _ResponseSchemaError:
        return fallback


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        return isinstance(reason, (TimeoutError, socket.timeout)) or (
            isinstance(reason, OSError) and "timed out" in str(reason).lower()
        )
    return isinstance(exc, OSError) and "timed out" in str(exc).lower()


def _retry_delay(headers: Mapping[str, Any], attempt: int) -> float:
    raw = headers.get("Retry-After") if headers else None
    try:
        if raw is not None:
            value = float(raw)
            if value >= 0:
                return min(value, 5.0)
    except (TypeError, ValueError):
        pass
    return min(0.25 * (2 ** max(0, attempt - 1)), 1.0)


class _HTTPClient:
    def __init__(self, config: _ClientConfig):
        self.config = config
        self.deadline = time.monotonic() + config.total_timeout_seconds
        self.calls: list[dict[str, Any]] = []
        # Do not inherit HTTP(S)_PROXY.  The endpoint and every connection are
        # local-only by validation and this opener keeps them local in practice.
        self.opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    def _url(self, path: str, params: Mapping[str, Any]) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "?" in path:
            raise ConfigurationError("request path is invalid")
        base = urlsplit(self.config.base_url)
        path_prefix = base.path.rstrip("/")
        full_path = f"{path_prefix}{path}" if path_prefix else path
        # urlencode is intentionally used for every value, including comma-
        # separated IDs and Chinese query text.
        query = urlencode(list(params.items()), doseq=False)
        return urlunsplit((base.scheme, base.netloc, full_path, query, ""))

    def _record_call(
        self,
        path: str,
        params: Mapping[str, Any],
        attempts: int,
        status: int | None,
        error: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "method": "GET",
            "path": path,
            "params": dict(params),
            "attempts": attempts,
        }
        if status is not None:
            item["status"] = status
        if error is not None:
            item["error"] = _safe_code(error)
        self.calls.append(_redact(item, self.config.token))

    def _record_response_meta(self, payload: Mapping[str, Any]) -> None:
        if not self.calls or not isinstance(payload, Mapping):
            return
        meta = payload.get("meta")
        if not isinstance(meta, Mapping):
            return
        kept = {
            key: meta[key]
            for key in (
                "source",
                "dataset_version",
                "scenario",
                "as_of",
                "served_at",
                "next_cursor",
                "total",
            )
            if key in meta
        }
        self.calls[-1]["response_meta"] = _redact(kept, self.config.token)

    def request_json(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        authenticate: bool = True,
    ) -> dict[str, Any]:
        url = self._url(path, params)
        attempts = 0
        last_status: int | None = None
        last_error: str | None = None

        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                last_error = "DEADLINE_EXCEEDED"
                break
            attempts += 1
            headers = {"Accept": "application/json"}
            if authenticate and self.config.token is not None:
                headers["Authorization"] = f"Bearer {self.config.token}"
            request = Request(url, headers=headers, method="GET")
            request_timeout = min(self.config.timeout_seconds, max(0.001, remaining))
            raw: bytes | None = None
            response_headers: Mapping[str, Any] = {}
            try:
                with self.opener.open(request, timeout=request_timeout) as response:
                    last_status = int(response.getcode())
                    response_headers = response.headers
                    raw = _read_limited(response)
            except HTTPError as exc:
                last_status = int(exc.code)
                response_headers = exc.headers or {}
                try:
                    raw = _read_limited(exc)
                except (_ResponseSchemaError, OSError):
                    raw = b""
            except (TimeoutError, socket.timeout, URLError, OSError) as exc:
                if _is_timeout_exception(exc):
                    last_error = "TIMEOUT"
                    if attempts <= self.config.max_retries:
                        delay = min(_retry_delay({}, attempts), max(0.0, self.deadline - time.monotonic()))
                        if delay > 0:
                            time.sleep(delay)
                        continue
                    break
                last_error = "CONNECTION_ERROR"
                break
            except _ResponseSchemaError:
                last_error = "RESPONSE_TOO_LARGE"
                break

            status = last_status
            if status == 200:
                try:
                    payload = _decode_json(raw or b"")
                except _ResponseSchemaError:
                    last_error = "INVALID_JSON"
                    break
                if not isinstance(payload, dict):
                    last_error = "SCHEMA_INVALID"
                    break
                self._record_call(path, params, attempts, status)
                self._record_response_meta(payload)
                return {"ok": True, "status": status, "payload": payload}

            fallback = f"HTTP_{status}" if status is not None else "HTTP_ERROR"
            if status is not None and 300 <= status < 400:
                last_error = "REDIRECT_BLOCKED"
                break
            last_error = _extract_http_error_code(raw or b"", fallback)
            if status == 429 and attempts <= self.config.max_retries:
                delay = min(
                    _retry_delay(response_headers, attempts),
                    max(0.0, self.deadline - time.monotonic()),
                )
                if delay > 0:
                    time.sleep(delay)
                    continue
            break

        self._record_call(path, params, attempts, last_status, last_error)
        return {
            "ok": False,
            "status": last_status,
            "error": _safe_code(last_error or "HTTP_ERROR"),
            "retryable": last_error in {"TIMEOUT", "RATE_LIMITED"},
        }


def _require_string(obj: Mapping[str, Any], key: str) -> None:
    if not isinstance(obj.get(key), str) or not obj[key]:
        raise _ResponseSchemaError(f"{key} must be a nonempty string")


def _require_bool(obj: Mapping[str, Any], key: str) -> None:
    if not isinstance(obj.get(key), bool):
        raise _ResponseSchemaError(f"{key} must be boolean")


def _require_integer(obj: Mapping[str, Any], key: str) -> None:
    if isinstance(obj.get(key), bool) or not isinstance(obj.get(key), int):
        raise _ResponseSchemaError(f"{key} must be integer")


def _validate_envelope(payload: Any, kind: str) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise _ResponseSchemaError("response envelope must be an object")
    _require_string(payload, "request_id")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise _ResponseSchemaError("meta must be an object")
    for key in ("source", "dataset_version", "scenario", "as_of", "served_at"):
        _require_string(meta, key)
    if not _iso_timestamp(meta["as_of"]) or not _iso_timestamp(meta["served_at"]):
        raise _ResponseSchemaError("meta timestamp is invalid")
    if meta.get("next_cursor") is not None and not isinstance(meta.get("next_cursor"), str):
        raise _ResponseSchemaError("meta.next_cursor is invalid")
    total = meta.get("total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise _ResponseSchemaError("meta.total is invalid")
    errors = payload.get("errors")
    if not isinstance(errors, list):
        raise _ResponseSchemaError("errors must be a list")
    normalized_errors: list[dict[str, Any]] = []
    for item in errors:
        if not isinstance(item, dict):
            raise _ResponseSchemaError("item error must be an object")
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise _ResponseSchemaError("item error id is invalid")
        if not isinstance(item.get("code"), str) or not item["code"]:
            raise _ResponseSchemaError("item error code is invalid")
        retryable = item.get("retryable", False)
        if not isinstance(retryable, bool):
            raise _ResponseSchemaError("item error retryable is invalid")
        normalized_errors.append(
            {
                "id": item["id"],
                "code": _safe_code(item["code"]),
                "retryable": retryable,
            }
        )
    top_error = payload.get("error")
    if top_error is not None:
        if not isinstance(top_error, dict) or not isinstance(top_error.get("code"), str):
            raise _ResponseSchemaError("error is invalid")
        retryable = top_error.get("retryable", False)
        if not isinstance(retryable, bool):
            raise _ResponseSchemaError("error.retryable is invalid")
        raise _ResponseSchemaError(_safe_code(top_error["code"], "REQUEST_ERROR"))
    data = payload.get("data")
    if kind == "health":
        if not isinstance(data, dict):
            raise _ResponseSchemaError("health data must be an object")
    elif not isinstance(data, list):
        raise _ResponseSchemaError("business data must be a list")
    return meta, data, normalized_errors


_ROW_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "terms": (
        ("term_id", "string"),
        ("term", "string"),
        ("enabled", "bool"),
        ("index_version", "string"),
    ),
    "recall": (
        ("term_id", "string"),
        ("ad_plan_id", "string"),
        ("index_version", "string"),
        ("updated_at", "timestamp"),
        ("candidate_id", "string"),
        ("offer_id", "string"),
        ("sku_id", "string"),
        ("store_id", "string"),
        ("city_id", "string"),
    ),
    "products": (
        ("sku_id", "string"),
        ("spu_id", "string"),
        ("brand_id", "string"),
        ("brand_name", "string"),
        ("generic_name", "string"),
        ("dosage_form", "string"),
        ("pack_count", "integer"),
        ("pack_unit", "string"),
        ("strength", "nullable_string"),
        ("audience", "nullable_string"),
        ("category", "string"),
        ("prescription_type", "nullable_string"),
        ("title", "string"),
        ("updated_at", "timestamp"),
        ("product_version", "string"),
        ("qualification_status", "string"),
        ("source", "string"),
    ),
    "offers": (
        ("offer_id", "string"),
        ("sku_id", "string"),
        ("store_id", "string"),
        ("city_id", "string"),
        ("region_ids", "string_list"),
        ("on_shelf", "bool"),
        ("stock", "integer"),
        ("price_fen", "integer"),
        ("merchant_qualification", "string"),
        ("as_of", "timestamp"),
        ("version", "string"),
    ),
    "plans": (
        ("ad_plan_id", "string"),
        ("offer_id", "string"),
        ("status", "string"),
        ("remaining_budget_fen", "integer"),
        ("bid_fen", "integer"),
        ("material_status", "string"),
        ("as_of", "timestamp"),
        ("version", "string"),
    ),
    "stores": (
        ("store_id", "string"),
        ("store_name", "string"),
        ("city_id", "string"),
        ("region_ids", "string_list"),
        ("merchant_qualification", "string"),
    ),
    "qualifications": (
        ("sku_id", "string"),
        ("category", "string"),
        ("prescription_type", "nullable_string"),
        ("qualification_status", "string"),
        ("product_version", "string"),
        ("as_of", "timestamp"),
    ),
}


def _validate_row(row: Any, kind: str) -> None:
    if not isinstance(row, dict):
        raise _ResponseSchemaError("row must be an object")
    for key, field_type in _ROW_FIELDS[kind]:
        if field_type == "string":
            _require_string(row, key)
        elif field_type == "nullable_string":
            if row.get(key) is not None:
                _require_string(row, key)
        elif field_type == "bool":
            _require_bool(row, key)
        elif field_type == "integer":
            _require_integer(row, key)
        elif field_type == "timestamp":
            _require_string(row, key)
            if not _iso_timestamp(row[key]):
                raise _ResponseSchemaError(f"{key} must be ISO timestamp")
        elif field_type == "string_list":
            values = row.get(key)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
                raise _ResponseSchemaError(f"{key} must be string list")


_RELATION_FIELDS = {
    "terms": None,
    "recall": "term_id",
    "products": "sku_id",
    "offers": "sku_id",
    "plans": "offer_id",
    "stores": "store_id",
    "qualifications": "sku_id",
}


def _validated_rows(
    data: list[Any],
    kind: str,
    expected_values: set[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    relation_field = _RELATION_FIELDS[kind]
    for row in data:
        ident = row.get(relation_field) if isinstance(row, dict) and relation_field else None
        if expected_values is not None and ident not in expected_values:
            errors.append(
                {
                    "id": ident if isinstance(ident, str) and ident else "<unknown>",
                    "code": "UNEXPECTED_ID",
                    "retryable": False,
                }
            )
            continue
        try:
            _validate_row(row, kind)
        except _ResponseSchemaError:
            errors.append(
                {
                    "id": ident if isinstance(ident, str) and ident else "<unknown>",
                    "code": "SCHEMA_INVALID",
                    "retryable": False,
                }
            )
            continue
        rows.append(copy.deepcopy(row))
    return rows, errors


def _page_params(base: Mapping[str, Any], limit: int, cursor: str | None) -> dict[str, Any]:
    params = dict(base)
    params["limit"] = limit
    if cursor is not None:
        params["cursor"] = cursor
    return params


def _fetch_pages(
    client: _HTTPClient,
    path: str,
    base_params: Mapping[str, Any],
    *,
    kind: str,
    expected_values: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    rows: list[dict[str, Any]] = []
    item_errors: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for page_number in range(client.config.max_pages):
        params = _page_params(base_params, client.config.max_page_size, cursor)
        response = client.request_json(path, params)
        if not response.get("ok"):
            return rows, item_errors, response
        try:
            meta, data, page_errors = _validate_envelope(response.get("payload"), kind)
        except _ResponseSchemaError as exc:
            code = _safe_code(str(exc), "SCHEMA_INVALID")
            return rows, item_errors, {
                "ok": False,
                "status": response.get("status"),
                "error": code,
                "retryable": False,
            }
        page_rows, schema_errors = _validated_rows(data, kind, expected_values)
        rows.extend(page_rows)
        item_errors.extend(page_errors)
        item_errors.extend(schema_errors)
        next_cursor = meta.get("next_cursor")
        if next_cursor is None:
            return rows, _dedupe_errors(item_errors), None
        if next_cursor in seen_cursors or next_cursor == cursor:
            return rows, _dedupe_errors(item_errors), {
                "ok": False,
                "status": response.get("status"),
                "error": "PAGINATION_LOOP",
                "retryable": False,
            }
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if page_number + 1 >= client.config.max_pages:
            return rows, _dedupe_errors(item_errors), {
                "ok": False,
                "status": response.get("status"),
                "error": "MAX_PAGES_EXCEEDED",
                "retryable": False,
            }
    return rows, _dedupe_errors(item_errors), {
        "ok": False,
        "status": None,
        "error": "MAX_PAGES_EXCEEDED",
        "retryable": False,
    }


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _fetch_batches(
    client: _HTTPClient,
    path: str,
    ids: Sequence[str],
    *,
    id_param: str,
    kind: str,
    relation_field: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    final_errors: list[dict[str, Any]] = []
    for chunk in _chunks(ids, client.config.max_batch_ids):
        pending = list(chunk)
        item_attempt = 0
        while pending:
            item_attempt += 1
            page_rows, page_errors, request_error = _fetch_pages(
                client,
                path,
                {id_param: ",".join(pending)},
                kind=kind,
                expected_values=set(pending),
            )
            rows.extend(page_rows)
            if request_error is not None:
                code = _safe_code(request_error.get("error"), "REQUEST_ERROR")
                retryable = bool(request_error.get("retryable", False))
                final_errors.extend(
                    {"id": ident, "code": code, "retryable": retryable} for ident in pending
                )
                break

            retry_ids: list[str] = []
            for error in page_errors:
                ident = error.get("id")
                if ident in pending and error.get("retryable") and ident not in retry_ids:
                    retry_ids.append(ident)
                if not error.get("retryable"):
                    final_errors.append(dict(error))
            if not retry_ids:
                break
            if item_attempt > client.config.max_retries:
                final_errors.extend(
                    error for error in page_errors if error.get("id") in retry_ids
                )
                break
            if client.deadline - time.monotonic() <= 0:
                final_errors.extend(
                    {
                        "id": ident,
                        "code": "DEADLINE_EXCEEDED",
                        "retryable": False,
                    }
                    for ident in retry_ids
                )
                break
            pending = retry_ids

    # A retry can return the same successful object.  De-duplicate only exact
    # object repeats; distinct plans/offers remain distinct facts.
    unique_rows: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    for row in rows:
        marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if marker not in seen_rows:
            seen_rows.add(marker)
            unique_rows.append(row)
    return unique_rows, _dedupe_errors(final_errors)


def _health(client: _HTTPClient) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    response = client.request_json("/health", {}, authenticate=False)
    if not response.get("ok"):
        return None, {
            "source": "health",
            "code": _safe_code(response.get("error"), "HEALTH_FAILED"),
            "retryable": bool(response.get("retryable", False)),
        }
    try:
        meta, data, errors = _validate_envelope(response.get("payload"), "health")
        if errors:
            return None, {"source": "health", "code": "SCHEMA_INVALID", "retryable": False}
        if data.get("status") != "ok" or data.get("mode") != "read_only":
            return None, {"source": "health", "code": "HEALTH_NOT_READY", "retryable": False}
        if not _iso_timestamp(data.get("business_time")):
            return None, {"source": "health", "code": "SCHEMA_INVALID", "retryable": False}
        return {
            "source": meta["source"],
            "dataset_version": meta["dataset_version"],
            "scenario": meta["scenario"],
            "business_time": data["business_time"],
            "as_of": meta["as_of"],
            "served_at": meta["served_at"],
            "mode": data["mode"],
            "status": data["status"],
        }, None
    except _ResponseSchemaError:
        return None, {"source": "health", "code": "SCHEMA_INVALID", "retryable": False}


def _result_meta(health: Mapping[str, Any] | None) -> dict[str, Any]:
    if health is None:
        return {
            "source": None,
            "dataset_version": None,
            "scenario": None,
            "business_time": None,
            "health_checked": False,
        }
    return {
        "source": health.get("source"),
        "dataset_version": health.get("dataset_version"),
        "scenario": health.get("scenario"),
        "business_time": health.get("business_time"),
        "as_of": health.get("as_of"),
        "health_checked": True,
    }


def _load_lexicon(config: _ClientConfig) -> tuple[Any, dict[str, Any] | None]:
    path = config.lexicon_path
    if path is None or not path.is_file():
        return None, {"source": "lexicon", "code": "LEXICON_UNAVAILABLE", "retryable": False}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, {"source": "lexicon", "code": "LEXICON_INVALID", "retryable": False}
    if not isinstance(value, dict) or not isinstance(value.get("aliases"), list):
        return None, {"source": "lexicon", "code": "LEXICON_INVALID", "retryable": False}
    return copy.deepcopy(value), None


def _append_request_error(
    errors: list[dict[str, Any]],
    source: str,
    response: Mapping[str, Any],
    *,
    identifier: str | None = None,
    **context: Any,
) -> None:
    item: dict[str, Any] = {
        "source": source,
        "code": _safe_code(response.get("error"), "REQUEST_ERROR"),
        "retryable": bool(response.get("retryable", False)),
    }
    if identifier is not None:
        item["id"] = identifier
    item.update(context)
    errors.append(item)


def _append_item_errors(
    errors: list[dict[str, Any]],
    source: str,
    item_errors: Iterable[Mapping[str, Any]],
    **context: Any,
) -> None:
    for item in item_errors:
        normalized: dict[str, Any] = {
            "source": source,
            "id": item.get("id"),
            "code": _safe_code(item.get("code"), "ITEM_ERROR"),
            "retryable": bool(item.get("retryable", False)),
        }
        normalized.update(context)
        errors.append(normalized)


def _candidate_error(
    source: str, identifier: str | None, code: str, retryable: bool = False
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source": source,
        "code": _safe_code(code, "ITEM_ERROR"),
        "retryable": bool(retryable),
    }
    if identifier is not None:
        item["id"] = identifier
    return item


def collect_catalog(config: Mapping[str, Any]) -> dict[str, Any]:
    """Fetch the complete term catalog and an independent terminology dictionary."""

    client_config = _normalize_config(config)
    client = _HTTPClient(client_config)
    result: dict[str, Any] = {
        "terms": [],
        "lexicon": None,
        "meta": _result_meta(None),
        "errors": [],
        "calls": client.calls,
    }
    lexicon, lexicon_error = _load_lexicon(client_config)
    result["lexicon"] = lexicon
    if lexicon_error:
        result["errors"].append(lexicon_error)

    health, health_error = _health(client)
    result["meta"] = _result_meta(health)
    if health_error:
        result["errors"].append(health_error)
        return _redact(result, client_config.token)

    rows, item_errors, request_error = _fetch_pages(
        client,
        "/v1/terms",
        {"q": ""},
        kind="terms",
    )
    result["terms"] = rows
    _append_item_errors(result["errors"], "terms", item_errors)
    if request_error is not None:
        _append_request_error(result["errors"], "terms", request_error)
    result["errors"] = _dedupe_errors(result["errors"])
    return _redact(result, client_config.token)


def collect_candidates(
    config: Mapping[str, Any], term_ids: Sequence[str] | str, city_id: str
) -> dict[str, Any]:
    """Fetch term candidates while preserving each term/advertising-plan row."""

    requested_terms = _normalize_ids(term_ids, "term_ids")
    if not isinstance(city_id, str) or not city_id.strip():
        raise ValueError("city_id must be a nonempty string")
    city_id = city_id.strip()

    client_config = _normalize_config(config)
    client = _HTTPClient(client_config)
    result: dict[str, Any] = {
        "terms": [],
        "candidates": [],
        "meta": _result_meta(None),
        "errors": [],
        "calls": client.calls,
    }
    health, health_error = _health(client)
    result["meta"] = _result_meta(health)
    if health_error:
        result["errors"].append(health_error)
        return _redact(result, client_config.token)

    lexicon, lexicon_error = _load_lexicon(client_config)
    # The candidate contract only requires terms/candidates/meta/errors/calls;
    # retaining the independent lexicon is useful evidence and mirrors catalog.
    result["lexicon"] = lexicon
    if lexicon_error:
        result["errors"].append(lexicon_error)

    catalog_rows, catalog_item_errors, catalog_request_error = _fetch_pages(
        client,
        "/v1/terms",
        {"q": ""},
        kind="terms",
    )
    catalog_by_id = {row["term_id"]: row for row in catalog_rows}
    result["terms"] = [catalog_by_id[term_id] for term_id in requested_terms if term_id in catalog_by_id]
    _append_item_errors(result["errors"], "terms", catalog_item_errors)
    if catalog_request_error is not None:
        _append_request_error(result["errors"], "terms", catalog_request_error)

    candidates: list[dict[str, Any]] = []
    seen_candidate_keys: set[tuple[str, str]] = set()
    for term_id in requested_terms:
        recall_rows, recall_item_errors, recall_request_error = _fetch_pages(
            client,
            "/v1/recall",
            {"term_id": term_id, "city_id": city_id},
            kind="recall",
            expected_values={term_id},
        )
        _append_item_errors(result["errors"], "recall", recall_item_errors, term_id=term_id)
        if recall_request_error is not None:
            _append_request_error(
                result["errors"],
                "recall",
                recall_request_error,
                identifier=term_id,
                term_id=term_id,
                city_id=city_id,
            )
            continue
        for row in recall_rows:
            key = (row["term_id"], row["candidate_id"])
            if key in seen_candidate_keys:
                result["errors"].append(
                    {
                        "source": "recall",
                        "code": "DUPLICATE_CANDIDATE",
                        "retryable": False,
                        "term_id": term_id,
                        "id": row["candidate_id"],
                    }
                )
                continue
            seen_candidate_keys.add(key)
            candidates.append(
                {
                    "candidate_id": row["candidate_id"],
                    "term_id": row["term_id"],
                    "sku_id": row["sku_id"],
                    "offer_id": row["offer_id"],
                    "store_id": row["store_id"],
                    "recall": copy.deepcopy(row),
                    "product": None,
                    "offer": None,
                    "plan": None,
                    "store": None,
                    "qualification": None,
                    "errors": [],
                }
            )

    endpoint_specs = (
        ("product", "/v1/products", "sku_ids", "sku_id", "sku_id", "products"),
        ("offer", "/v1/offers", "sku_ids", "sku_id", "offer_id", "offers"),
        ("plan", "/v1/ad-plans", "offer_ids", "offer_id", "ad_plan_id", "plans"),
        ("store", "/v1/stores", "store_ids", "store_id", "store_id", "stores"),
        ("qualification", "/v1/qualifications", "sku_ids", "sku_id", "sku_id", "qualifications"),
    )
    for label, path, id_param, request_field, object_key, kind in endpoint_specs:
        if not candidates:
            break
        ids = list(dict.fromkeys(candidate[request_field] for candidate in candidates))
        extra: dict[str, Any] = {"city_id": city_id} if label == "offer" else {}
        # _fetch_batches currently accepts the endpoint's fixed request shape;
        # offers need city_id in every request, so add it through the helper
        # below rather than changing the API's parameter contract.
        if extra:
            rows: list[dict[str, Any]] = []
            endpoint_errors: list[dict[str, Any]] = []
            for chunk in _chunks(ids, client.config.max_batch_ids):
                pending = list(chunk)
                item_attempt = 0
                while pending:
                    item_attempt += 1
                    page_rows, page_errors, request_error = _fetch_pages(
                        client,
                        path,
                        {id_param: ",".join(pending), **extra},
                        kind=kind,
                        expected_values=set(pending),
                    )
                    rows.extend(page_rows)
                    if request_error is not None:
                        endpoint_errors.extend(
                            {"id": ident, "code": _safe_code(request_error.get("error"), "REQUEST_ERROR"),
                             "retryable": bool(request_error.get("retryable", False))}
                            for ident in pending
                        )
                        break
                    retry_ids = []
                    for error in page_errors:
                        ident = error.get("id")
                        if error.get("retryable") and ident in pending and ident not in retry_ids:
                            retry_ids.append(ident)
                        if not error.get("retryable"):
                            endpoint_errors.append(dict(error))
                    if not retry_ids:
                        break
                    if item_attempt > client.config.max_retries:
                        endpoint_errors.extend(error for error in page_errors if error.get("id") in retry_ids)
                        break
                    if client.deadline - time.monotonic() <= 0:
                        endpoint_errors.extend(
                            {"id": ident, "code": "DEADLINE_EXCEEDED", "retryable": False}
                            for ident in retry_ids
                        )
                        break
                    pending = retry_ids
            endpoint_errors = _dedupe_errors(endpoint_errors)
        else:
            rows, endpoint_errors = _fetch_batches(
                client,
                path,
                ids,
                id_param=id_param,
                kind=kind,
                relation_field=request_field,
            )

        _append_item_errors(result["errors"], label, endpoint_errors)
        object_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row.get(object_key)
            if isinstance(key, str) and key not in object_map:
                object_map[key] = row

        for candidate in candidates:
            # The recall row exposes candidate_id (the advertising plan
            # dimension); the enriched plan fact exposes ad_plan_id.  Do not
            # collapse or rename that distinction while joining facts.
            expected_key = candidate["candidate_id"] if label == "plan" else candidate[object_key]
            value = object_map.get(expected_key)
            candidate[label] = copy.deepcopy(value) if value is not None else None
            if value is None:
                relevant = [
                    error
                    for error in endpoint_errors
                    if error.get("id") == candidate[request_field]
                ]
                if not relevant and label in {"offer", "plan"}:
                    relevant = [
                        error
                        for error in endpoint_errors
                        if error.get("id") == candidate[request_field]
                    ]
                if not relevant:
                    missing = _candidate_error(label, candidate[request_field], "MISSING_FACT")
                    candidate["errors"].append(missing)
                    result["errors"].append(
                        {
                            "source": label,
                            "id": candidate[request_field],
                            "code": "MISSING_FACT",
                            "retryable": False,
                        }
                    )

        for error in endpoint_errors:
            identifier = error.get("id")
            affected = [
                candidate
                for candidate in candidates
                if identifier is None or candidate[request_field] == identifier
            ]
            for candidate in affected:
                candidate["errors"].append(
                    _candidate_error(
                        label,
                        identifier if isinstance(identifier, str) else candidate[request_field],
                        _safe_code(error.get("code"), "ITEM_ERROR"),
                        bool(error.get("retryable", False)),
                    )
                )

    result["candidates"] = candidates
    result["errors"] = _dedupe_errors(result["errors"])
    return _redact(result, client_config.token)


def _load_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    config_path = Path(path).resolve()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("config file is unreadable") from exc
    if not isinstance(config, dict):
        raise ConfigurationError("config file must contain an object")
    return config


def _write_output(path: str | os.PathLike[str], value: Any) -> None:
    output_path = Path(path)
    output_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch local read-only medical evidence")
    parser.add_argument("--config", required=True, help="JSON-compatible YAML config path")
    parser.add_argument("--term-ids", help="comma-separated term IDs; omit for catalog")
    parser.add_argument("--city-id", help="city ID required when --term-ids is present")
    parser.add_argument("--output", required=True, help="JSON output path")
    args = parser.parse_args(argv)

    if args.term_ids and not args.city_id:
        parser.error("--city-id is required with --term-ids")
    try:
        config = _load_config_file(args.config)
        config["_config_dir"] = str(Path(args.config).resolve().parent)
        # _config_dir is an internal path hint only; it is never serialized.
        if args.term_ids:
            value = collect_candidates(config, args.term_ids, args.city_id)
        else:
            value = collect_catalog(config)
        _write_output(args.output, value)
        return 0
    except (ConfigurationError, ValueError, OSError):
        # Do not echo exception text: a malformed config or an HTTP error must
        # not accidentally print a bearer token or a URL containing secrets.
        print("fetch_evidence: invalid configuration or output path", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
