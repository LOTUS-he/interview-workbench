"""Deterministic hard-constraint checks for the medical query review handoff.

The query and item handoffs remain the semantic source of truth.  This module
does not decide relevance from strings in a query, a term, or a product title.
It only combines explicit model decisions with deterministic scope, attribute,
evidence, freshness, version, and current-eligibility checks.  A missing or
contradictory fact is deliberately represented as ``unknown``.

``eligibility-rules.yaml`` is a JSON-compatible YAML subset and is read with
``json.load`` by the caller.  The loader below is provided for the standalone
CLI and for small local integrations.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional


PASS = "pass"
REJECT = "reject"
UNKNOWN = "unknown"
STATUSES = {PASS, REJECT, UNKNOWN}

ATTRIBUTES = (
    "brand_name",
    "generic_name",
    "dosage_form",
    "pack_count",
    "pack_unit",
    "audience",
)

RULE_IDS = {
    "scope": "SCOPE-001",
    "admit": "ADMIT-001",
    "rewrite": "REWRITE-001",
    "brand": "BRAND-001",
    "form": "FORM-001",
    "pack": "PACK-001",
    "audience": "AUDIENCE-001",
    "evidence": "EVIDENCE-001",
    "eligibility": "ELIG-001",
    "time": "TIME-001",
    "decision": "DECISION-001",
    "value": "VALUE-001",
}

ATTR_RULES = {
    "brand_name": RULE_IDS["brand"],
    "generic_name": RULE_IDS["rewrite"],
    "dosage_form": RULE_IDS["form"],
    "pack_count": RULE_IDS["pack"],
    "pack_unit": RULE_IDS["pack"],
    "audience": RULE_IDS["audience"],
}

_EVIDENCE_PREFIXES = ("evidence/", "steps/", "references/")
_PACK_UNITS = {
    "片": "片",
    "粒": "粒",
    "袋": "袋",
    "贴": "贴",
    "支": "支",
    "只": "只",
    "盒": "盒",
    "瓶": "瓶",
    "包": "包",
    "毫升": "毫升",
    "ml": "毫升",
    "mL": "毫升",
}
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def load_policy(path: str | Path) -> dict[str, Any]:
    """Load the JSON-compatible YAML policy without a third-party parser."""

    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("policy must be a JSON object")
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def _nonempty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _norm_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", "", value).casefold()
    return value or None


def _valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normal_attr(value: Any, attr: str) -> Any:
    if value is None:
        return None
    if attr == "pack_count":
        return value if _valid_int(value) and value >= 0 else None
    if isinstance(value, str):
        return _norm_text(value)
    return value


def _equal_attr(left: Any, right: Any, attr: str) -> bool:
    if attr == "pack_count":
        return _valid_int(left) and _valid_int(right) and left == right
    return _normal_attr(left, attr) == _normal_attr(right, attr)


def _comparable_attr(value: Any, attr: str) -> bool:
    if attr == "pack_count":
        return _valid_int(value) and value >= 0
    if attr == "brand_id":
        return isinstance(value, str) and bool(value.strip())
    return isinstance(value, str) and bool(value.strip())


def _rule(policy: Mapping[str, Any], key: str) -> str:
    """Return a declared rule ID while tolerating a minimal test policy."""

    expected = RULE_IDS[key]
    declared = policy.get("rules")
    if isinstance(declared, list):
        for row in declared:
            if isinstance(row, Mapping) and row.get("id") == expected:
                return expected
    elif isinstance(declared, Mapping) and expected in declared:
        return expected
    return expected


def _rules(policy: Mapping[str, Any], *keys: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for key in keys:
        value = key if key in RULE_IDS else None
        rule_id = _rule(policy, value) if value else key
        if rule_id and rule_id not in seen:
            seen.add(rule_id)
            result.append(rule_id)
    return result


def _clean_refs(value: Any) -> list[str]:
    """Keep only safe run-relative evidence references."""

    result: list[str] = []
    values = value if isinstance(value, list) else [value]
    for ref in values:
        if not isinstance(ref, str):
            continue
        ref = ref.strip()
        if not ref or ref.startswith("/") or ".." in ref.split("#", 1)[0].split("/"):
            continue
        if not ref.startswith(_EVIDENCE_PREFIXES):
            continue
        if ref not in result:
            result.append(ref)
    return result


def _refs(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        for ref in _clean_refs(value):
            if ref not in result:
                result.append(ref)
    return result


def _reason(code: str, message: str) -> str:
    return f"{code}: {message}"


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _extend_unique(values: list[str], additions: Iterable[str]) -> None:
    for value in additions:
        _append_unique(values, value)


def _merge_status(*statuses: Optional[str]) -> str:
    normalized = [status for status in statuses if status in STATUSES]
    if any(status == REJECT for status in normalized):
        return REJECT
    if any(status == UNKNOWN for status in normalized) or len(normalized) != len(statuses):
        return UNKNOWN
    return PASS


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _time_status(
    value: Any,
    business_time: Optional[datetime],
    max_age_seconds: Any,
) -> tuple[str, Optional[str]]:
    observed = _parse_time(value)
    if observed is None or business_time is None or not _valid_int(max_age_seconds):
        return UNKNOWN, "missing or invalid time evidence"
    age = (business_time - observed).total_seconds()
    if age < 0:
        return UNKNOWN, "fact timestamp is newer than the business clock"
    if age > max_age_seconds:
        return UNKNOWN, f"fact age {int(age)}s exceeds {max_age_seconds}s"
    return PASS, None


def _business_time(evidence: Mapping[str, Any]) -> Optional[datetime]:
    meta = _as_mapping(evidence.get("meta"))
    # business_time is the contract field. as_of is accepted only as a
    # backwards-compatible response alias; wall-clock time is never used.
    return _parse_time(meta.get("business_time") or meta.get("as_of"))


def _extract_history(
    evidence: Mapping[str, Any], policy: Mapping[str, Any], history: Any
) -> Optional[Mapping[str, Any]]:
    if isinstance(history, Mapping):
        return history
    for container in (policy, evidence):
        for key in ("history", "runtime_history", "_runtime"):
            value = container.get(key) if isinstance(container, Mapping) else None
            if key == "_runtime" and isinstance(value, Mapping):
                value = value.get("history")
            if isinstance(value, Mapping):
                return value
    return None


def _profile(query_decision: Any) -> Mapping[str, Any]:
    query = _as_mapping(query_decision)
    profile = _as_mapping(query.get("profile"))
    if profile:
        return profile
    return query


def _query_attributes(profile: Mapping[str, Any]) -> dict[str, Any]:
    attributes = _as_mapping(profile.get("attributes"))
    result: dict[str, Any] = {}
    for attr in ATTRIBUTES:
        value = attributes.get(attr)
        if value is None and attr in profile:
            value = profile.get(attr)
        result[attr] = value
    # brand_id is useful when the model has a dictionary-backed brand ID. It
    # is intentionally not inferred from a brand string.
    if "brand_id" in attributes:
        result["brand_id"] = attributes.get("brand_id")
    elif "brand_id" in profile:
        result["brand_id"] = profile.get("brand_id")
    return result


def _attribute_provenance(profile: Mapping[str, Any], attrs: Mapping[str, Any]) -> list[str]:
    evidence = _as_mapping(profile.get("attribute_evidence"))
    missing: list[str] = []
    for attr, value in attrs.items():
        if attr == "brand_id" or value is None:
            continue
        row = _as_mapping(evidence.get(attr))
        if not _nonempty(row.get("raw_span")) or not _nonempty(row.get("source_ref")):
            missing.append(attr)
    return missing


def _object_attrs(obj: Any) -> dict[str, Any]:
    source = _as_mapping(obj)
    result = {attr: source.get(attr) for attr in ATTRIBUTES}
    if "brand_id" in source:
        result["brand_id"] = source.get("brand_id")
    return result


def _term_attributes(rewrite: Mapping[str, Any], term: Mapping[str, Any]) -> dict[str, Any]:
    result = _object_attrs(term)
    rewrite_attrs = _as_mapping(rewrite.get("attributes") or rewrite.get("term_attributes"))
    term_attrs = _as_mapping(term.get("attributes") or term.get("term_attributes"))
    for attr in list(ATTRIBUTES) + ["brand_id"]:
        if rewrite_attrs.get(attr) is not None:
            result[attr] = rewrite_attrs.get(attr)
        elif term_attrs.get(attr) is not None:
            result[attr] = term_attrs.get(attr)
    return result


def _compare_attributes(
    query_attrs: Mapping[str, Any],
    candidate_attrs: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    subject: str,
) -> tuple[str, list[str], list[str]]:
    """Compare only structured attributes supplied by the model/evidence.

    ``subject`` is either ``term`` or ``product``. Product title text is not
    consulted here; title conflict detection is a separate conservative check.
    """

    statuses: list[str] = []
    reasons: list[str] = []
    rule_refs: list[str] = []

    q_brand_id = query_attrs.get("brand_id")
    c_brand_id = candidate_attrs.get("brand_id")
    q_brand = query_attrs.get("brand_name")
    c_brand = candidate_attrs.get("brand_name")
    if q_brand_id is not None:
        rule_id = _rule(policy, "brand")
        rule_refs.append(rule_id)
        if not _comparable_attr(q_brand_id, "brand_id") or not _comparable_attr(c_brand_id, "brand_id"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, f"{subject} brand_id is missing or invalid"))
        elif not _equal_attr(q_brand_id, c_brand_id, "brand_name"):
            statuses.append(REJECT)
            reasons.append(_reason(rule_id, f"{subject} brand_id conflicts with explicit query brand"))
        else:
            statuses.append(PASS)
    elif q_brand is not None:
        rule_id = _rule(policy, "brand")
        rule_refs.append(rule_id)
        if not _comparable_attr(q_brand, "brand_name") or not _comparable_attr(c_brand, "brand_name"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, f"{subject} brand_name is missing or invalid"))
        elif not _equal_attr(q_brand, c_brand, "brand_name"):
            statuses.append(REJECT)
            reasons.append(_reason(rule_id, f"{subject} brand_name conflicts with explicit query brand"))
        else:
            statuses.append(PASS)

    for attr, label, key in (
        ("generic_name", "generic_name", "rewrite"),
        ("dosage_form", "dosage_form", "form"),
    ):
        q_value = query_attrs.get(attr)
        if q_value is None:
            continue
        rule_id = _rule(policy, key)
        rule_refs.append(rule_id)
        c_value = candidate_attrs.get(attr)
        if not _comparable_attr(q_value, attr) or not _comparable_attr(c_value, attr):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, f"{subject} {label} is missing or invalid"))
        elif not _equal_attr(q_value, c_value, attr):
            statuses.append(REJECT)
            reasons.append(_reason(rule_id, f"{subject} {label} conflicts with explicit query attribute"))
        else:
            statuses.append(PASS)

    q_count = query_attrs.get("pack_count")
    q_unit = query_attrs.get("pack_unit")
    pack_explicit = q_count is not None or q_unit is not None
    if pack_explicit:
        rule_id = _rule(policy, "pack")
        rule_refs.append(rule_id)
        c_count = candidate_attrs.get("pack_count")
        c_unit = candidate_attrs.get("pack_unit")
        if q_count is not None and not _comparable_attr(q_count, "pack_count"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, "query pack_count is missing or invalid"))
        elif q_unit is not None and not _comparable_attr(q_unit, "pack_unit"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, "query pack_unit is missing or invalid"))
        elif q_count is not None and q_unit is None:
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, "pack_count has no comparable pack_unit"))
        elif q_unit is not None and not _comparable_attr(c_unit, "pack_unit"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, f"{subject} pack_unit is missing or invalid"))
        elif q_count is not None and not _comparable_attr(c_count, "pack_count"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, f"{subject} pack_count is missing or invalid"))
        else:
            if q_count is not None:
                if not _valid_int(c_count) or c_count != q_count:
                    statuses.append(REJECT)
                    reasons.append(_reason(rule_id, f"{subject} pack_count conflicts (query={q_count}, candidate={c_count})"))
                else:
                    statuses.append(PASS)
            if q_unit is not None:
                if c_unit is None:
                    statuses.append(UNKNOWN)
                    reasons.append(_reason(rule_id, f"{subject} pack_unit is missing"))
                elif not _equal_attr(q_unit, c_unit, "pack_unit"):
                    statuses.append(REJECT)
                    reasons.append(_reason(rule_id, f"{subject} pack_unit conflicts (query={q_unit}, candidate={c_unit})"))
                else:
                    statuses.append(PASS)

    q_audience = query_attrs.get("audience")
    c_audience = candidate_attrs.get("audience")
    audience_rule = _rule(policy, "audience")
    if q_audience is not None:
        rule_refs.append(audience_rule)
        if not _comparable_attr(q_audience, "audience") or not _comparable_attr(c_audience, "audience"):
            statuses.append(UNKNOWN)
            reasons.append(_reason(audience_rule, f"{subject} audience is missing or invalid"))
        elif not _equal_attr(q_audience, c_audience, "audience"):
            statuses.append(REJECT)
            reasons.append(_reason(audience_rule, f"{subject} audience crosses the explicit query audience"))
        else:
            statuses.append(PASS)
    elif c_audience is not None and _norm_text(c_audience) not in {"通用", "general", "universal"}:
        # An unspecified query must not be silently narrowed to a sensitive
        # audience. The model may later resolve it, but the hard checker does
        # not infer that resolution from the product text.
        rule_refs.append(audience_rule)
        statuses.append(UNKNOWN)
        reasons.append(_reason(audience_rule, f"{subject} adds a specific audience not expressed by query"))

    return _merge_status(*statuses) if statuses else PASS, reasons, rule_refs


def _chinese_number(value: str) -> Optional[int]:
    if value.isdigit():
        return int(value)
    if not value or any(char not in _CHINESE_DIGITS for char in value):
        return None
    # The teaching data uses simple counts (六十, 一百, 两盒). This parser is
    # deliberately narrow and is used only to detect an explicit title
    # conflict, never to establish semantic relevance.
    if len(value) == 1:
        return _CHINESE_DIGITS[value]
    if "百" in value or "千" in value or "万" in value:
        units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
        total = 0
        section = 0
        number = 0
        for char in value:
            if char in _CHINESE_DIGITS:
                number = _CHINESE_DIGITS[char]
            elif char in units:
                unit = units[char]
                section += (number or 1) * unit
                number = 0
                if unit == 10000:
                    total += section
                    section = 0
        return total + section + number
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if all(char in _CHINESE_DIGITS for char in value):
        return int("".join(str(_CHINESE_DIGITS[char]) for char in value))
    return None


def _title_attributes(product: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only explicit, structured title claims for conflict checks."""

    result: dict[str, Any] = {}
    for key in ("title_attributes", "parsed_title_attributes", "title_parsed"):
        row = _as_mapping(product.get(key))
        for attr in list(ATTRIBUTES) + ["brand_id"]:
            if row.get(attr) is not None:
                result[attr] = row.get(attr)

    if product.get("title_pack_count") is not None:
        result["pack_count"] = product.get("title_pack_count")
    if product.get("title_pack_unit") is not None:
        result["pack_unit"] = product.get("title_pack_unit")

    title = product.get("title")
    if isinstance(title, str):
        # A numeric+unit token is an explicit package claim. It is not a
        # keyword relevance rule and cannot add any positive attribute.
        token_pattern = r"([0-9]+|[零〇一二两三四五六七八九十百千万]+)\s*(毫升|ml|mL|片|粒|袋|贴|支|只|盒|瓶|包)"
        tokens = []
        for match in re.finditer(token_pattern, title):
            count = _chinese_number(match.group(1))
            unit = _PACK_UNITS.get(match.group(2), match.group(2))
            if count is not None:
                tokens.append((count, unit))
        unique_tokens = list(dict.fromkeys(tokens))
        if len(unique_tokens) == 1:
            result.setdefault("pack_count", unique_tokens[0][0])
            result.setdefault("pack_unit", unique_tokens[0][1])
        elif len(unique_tokens) > 1:
            # A title with competing explicit package claims is itself
            # contradictory. Keep a sentinel for the caller.
            result["__multiple_pack_claims__"] = unique_tokens
    return result


def _title_conflict(
    product: Any, policy: Mapping[str, Any]
) -> tuple[str, list[str], list[str]]:
    product_map = _as_mapping(product)
    if not product_map:
        return UNKNOWN, [_reason(_rule(policy, "evidence"), "product evidence is missing")], [_rule(policy, "evidence")]
    title_attrs = _title_attributes(product_map)
    if "__multiple_pack_claims__" in title_attrs:
        return UNKNOWN, [
            _reason(_rule(policy, "pack"), "title contains multiple explicit package claims")
        ], [_rule(policy, "pack")]

    statuses: list[str] = []
    reasons: list[str] = []
    rules: list[str] = []
    for attr in ATTRIBUTES:
        title_value = title_attrs.get(attr)
        structured_value = product_map.get(attr)
        if title_value is None or structured_value is None:
            continue
        if not _equal_attr(title_value, structured_value, attr):
            rule_key = "pack" if attr in {"pack_count", "pack_unit"} else ATTR_RULES.get(attr, "evidence")
            rule_id = _rule(policy, rule_key.replace("-001", "") if rule_key in RULE_IDS else "evidence")
            # ATTR_RULES values are IDs; map back to a policy key for custom
            # policies without making the output depend on a string search.
            rule_id = next((RULE_IDS[k] for k, v in ATTR_RULES.items() if v == rule_key), rule_id)
            statuses.append(UNKNOWN)
            reasons.append(_reason(rule_id, f"title and structured {attr} conflict; no source is preferred"))
            rules.append(rule_id)
    return _merge_status(*statuses) if statuses else PASS, reasons, rules


def _handoff_errors(
    query_decision: Any,
    item_decision: Any,
    *,
    has_candidates: bool = False,
) -> list[str]:
    errors: list[str] = []
    query = _as_mapping(query_decision)
    items = _as_mapping(item_decision)
    if not query:
        errors.append("query-decision handoff is missing")
    if not items:
        errors.append("item-decision handoff is missing")
    if query and query.get("phase") not in (None, "query"):
        errors.append("query-decision phase is not query")
    if items and items.get("phase") not in (None, "items"):
        errors.append("item-decision phase is not items")
    query_run = query.get("run_id")
    item_run = items.get("run_id")
    if query_run and item_run and query_run != item_run:
        errors.append("query and item handoffs have different run_id")
    for label, handoff in (("query", query), ("items", items)):
        if not handoff:
            continue
        producer = _as_mapping(handoff.get("producer"))
        if label == "items" and producer.get("type") == "not_executed":
            # The scheduler may close the items phase early when catalog
            # collection returned no candidate. This is an execution marker,
            # not a semantic model result and is never allowed with real
            # candidates or non-empty reviews.
            if producer.get("model") is not None:
                errors.append("items not_executed handoff model must be null")
            if _as_list(handoff.get("reviews")):
                errors.append("items not_executed handoff must have empty reviews")
            if has_candidates:
                errors.append("items not_executed handoff is invalid when candidates exist")
            continue
        if producer.get("type") not in (None, "agent"):
            errors.append(f"{label} handoff producer type is not agent")
        if producer.get("model") is not None and not isinstance(producer.get("model"), str):
            errors.append(f"{label} handoff producer model is invalid")
    return errors


def _model_rewrite_status(rewrite: Mapping[str, Any]) -> str:
    decision = rewrite.get("decision")
    if decision not in STATUSES:
        decision = rewrite.get("relevance")
    return decision if decision in STATUSES else UNKNOWN


def _model_item_status(review: Mapping[str, Any]) -> str:
    term_item = review.get("term_item")
    query_item = review.get("query_item")
    if term_item in STATUSES or query_item in STATUSES:
        if term_item not in STATUSES or query_item not in STATUSES:
            return UNKNOWN
        if REJECT in {term_item, query_item}:
            return REJECT
        if term_item == PASS and query_item == PASS:
            return PASS
        return UNKNOWN
    direct = review.get("relevance")
    return direct if direct in STATUSES else UNKNOWN


def _term_records(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = evidence.get("terms")
    if values is None:
        values = _as_mapping(evidence.get("catalog")).get("terms")
    return [item for item in _as_list(values) if isinstance(item, Mapping)]


def _candidate_records(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = evidence.get("candidates")
    if values is None:
        values = _as_mapping(evidence.get("recall")).get("candidates")
    records = [item for item in _as_list(values) if isinstance(item, Mapping)]
    # Accept a term-centric evidence response as an integration convenience;
    # it still requires explicit candidate objects and never parses term text.
    if not records:
        for term in _term_records(evidence):
            records.extend(item for item in _as_list(term.get("candidates")) if isinstance(item, Mapping))
    return records


def _index_by_id(records: Iterable[Mapping[str, Any]], key: str) -> dict[Any, Mapping[str, Any]]:
    result: dict[Any, Mapping[str, Any]] = {}
    for record in records:
        value = record.get(key)
        if value is not None and value not in result:
            result[value] = record
    return result


def _term_ref(term: Mapping[str, Any], index: int) -> list[str]:
    return _refs(
        term.get("evidence_ref"),
        term.get("evidence_refs"),
        f"evidence/catalog.json#/terms/{index}",
    )


def _candidate_ref(candidate: Mapping[str, Any], index: int) -> list[str]:
    return _refs(
        candidate.get("evidence_ref"),
        candidate.get("evidence_refs"),
        f"evidence/candidates.json#/candidates/{index}",
    )


def _candidate_id(candidate: Mapping[str, Any]) -> Any:
    return candidate.get("candidate_id") or candidate.get("ad_plan_id") or _as_mapping(candidate.get("plan")).get("ad_plan_id")


def _candidate_term_id(candidate: Mapping[str, Any]) -> Any:
    return candidate.get("term_id") or _as_mapping(candidate.get("term")).get("term_id")


def _records_for(value: Any, key: str, expected: Any = None) -> list[Mapping[str, Any]]:
    records = [row for row in _as_list(value) if isinstance(row, Mapping)]
    if expected is None:
        return records
    return [row for row in records if row.get(key) in (None, expected)]


def _first_matching(value: Any, key: str, expected: Any = None) -> Optional[Mapping[str, Any]]:
    rows = _records_for(value, key, expected)
    return rows[0] if rows else None


def _merged_qualification(product: Mapping[str, Any], qualification: Any, sku_id: Any) -> Mapping[str, Any]:
    row = _first_matching(qualification, "sku_id", sku_id)
    result: dict[str, Any] = {}
    if row:
        result.update(row)
    for key in (
        "qualification_status",
        "prescription_type",
        "category",
        "product_version",
        "as_of",
    ):
        if result.get(key) is None and product.get(key) is not None:
            result[key] = product.get(key)
    return result


def _check_version_consistency(
    *,
    term: Optional[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    product: Mapping[str, Any],
    qualification: Mapping[str, Any],
    offer: Mapping[str, Any],
    plan: Mapping[str, Any],
    evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[str, list[str], list[str]]:
    reasons: list[str] = []
    rules = [_rule(policy, "evidence")]
    statuses: list[str] = []
    evidence_meta = _as_mapping(evidence.get("meta"))
    expected_dataset = _as_mapping(policy.get("evidence")).get("dataset_version")
    actual_dataset = evidence_meta.get("dataset_version") or candidate.get("dataset_version")
    if actual_dataset is None:
        statuses.append(UNKNOWN)
        reasons.append(_reason(rules[0], "dataset_version is missing"))
    elif actual_dataset != expected_dataset:
        statuses.append(UNKNOWN)
        reasons.append(_reason(rules[0], f"dataset_version conflicts (expected={expected_dataset}, actual={actual_dataset})"))
    else:
        statuses.append(PASS)

    if term is None:
        statuses.append(UNKNOWN)
        reasons.append(_reason(rules[0], "term evidence is missing"))
    else:
        index_version = term.get("index_version")
        recall = _as_mapping(candidate.get("recall"))
        linked_version = (
            candidate.get("index_version")
            or candidate.get("term_index_version")
            or recall.get("index_version")
        )
        if index_version is None or linked_version is None:
            statuses.append(UNKNOWN)
            reasons.append(_reason(rules[0], "term index_version or candidate link version is missing"))
        elif index_version != linked_version:
            statuses.append(UNKNOWN)
            reasons.append(_reason(rules[0], "term and candidate index_version conflict"))
        else:
            statuses.append(PASS)

    product_version = product.get("product_version") or product.get("version")
    qualification_version = qualification.get("product_version") or qualification.get("version")
    if product_version is None or qualification_version is None:
        statuses.append(UNKNOWN)
        reasons.append(_reason(rules[0], "product_version is missing from product or qualification"))
    elif product_version != qualification_version:
        statuses.append(UNKNOWN)
        reasons.append(_reason(rules[0], "product and qualification product_version conflict"))
    else:
        statuses.append(PASS)

    if offer.get("version") is None or plan.get("version") is None:
        statuses.append(UNKNOWN)
        reasons.append(_reason(rules[0], "offer or plan version is missing"))
    else:
        statuses.append(PASS)
    return _merge_status(*statuses), reasons, rules


def _single_plan_eligibility(
    *,
    task: Mapping[str, Any],
    evidence: Mapping[str, Any],
    product: Mapping[str, Any],
    qualification: Mapping[str, Any],
    offer: Mapping[str, Any],
    plan: Mapping[str, Any],
    store: Mapping[str, Any],
    candidate: Mapping[str, Any],
    term: Optional[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> tuple[str, list[str], list[str]]:
    reasons: list[str] = []
    rules: list[str] = []
    statuses: list[str] = []
    elig_rule = _rule(policy, "eligibility")
    evidence_rule = _rule(policy, "evidence")
    time_rule = _rule(policy, "time")
    rules.extend([elig_rule, evidence_rule, time_rule])

    if not product or not qualification or not offer or not plan:
        statuses.append(UNKNOWN)
        reasons.append(_reason(evidence_rule, "required product, qualification, offer, or plan evidence is missing"))
    else:
        qualification_status = qualification.get("qualification_status")
        if qualification_status == "valid":
            statuses.append(PASS)
        elif qualification_status in {"expired", "invalid", "rejected"}:
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, f"qualification_status={qualification_status}"))
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "qualification_status is missing or unknown"))

        prescription_type = qualification.get("prescription_type")
        if prescription_type == "RX":
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, "prescription drug expansion is out of scope"))
        elif prescription_type in {"OTC", "NOT_APPLICABLE"}:
            statuses.append(PASS)
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "prescription_type is missing or unknown"))

        merchant_qualification = offer.get("merchant_qualification")
        if merchant_qualification is None and store:
            merchant_qualification = store.get("merchant_qualification")
        if merchant_qualification == "valid":
            statuses.append(PASS)
        elif merchant_qualification in {"expired", "invalid", "rejected"}:
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, f"merchant_qualification={merchant_qualification}"))
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "merchant_qualification is missing or unknown"))

        on_shelf = offer.get("on_shelf")
        if on_shelf is True:
            statuses.append(PASS)
        elif on_shelf is False:
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, "offer is off shelf"))
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "on_shelf is missing or not boolean"))

        stock = offer.get("stock")
        if isinstance(stock, (int, float)) and not isinstance(stock, bool):
            if stock > 0:
                statuses.append(PASS)
            else:
                statuses.append(REJECT)
                reasons.append(_reason(elig_rule, "stock is not positive"))
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "stock is missing or not numeric"))

        price = offer.get("price_fen")
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            if price > 0:
                statuses.append(PASS)
            else:
                statuses.append(REJECT)
                reasons.append(_reason(elig_rule, "price_fen is not positive"))
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "price_fen is missing or not numeric"))

        city_id = task.get("city_id")
        if offer.get("city_id") != city_id:
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, "offer city_id does not match task city"))
        else:
            region_ids = offer.get("region_ids")
            if not isinstance(region_ids, list):
                statuses.append(UNKNOWN)
                reasons.append(_reason(elig_rule, "offer region_ids is missing"))
            elif task.get("region_id") in region_ids:
                statuses.append(PASS)
            else:
                statuses.append(REJECT)
                reasons.append(_reason(elig_rule, "offer does not reach the target region"))

        if store:
            if store.get("store_id") not in {None, offer.get("store_id"), candidate.get("store_id")}:
                statuses.append(UNKNOWN)
                reasons.append(_reason(evidence_rule, "store_id conflicts across offer and store evidence"))
            if store.get("city_id") not in {None, city_id}:
                statuses.append(UNKNOWN)
                reasons.append(_reason(evidence_rule, "store city_id conflicts with task city"))
            store_regions = store.get("region_ids")
            if store_regions is not None and task.get("region_id") not in store_regions:
                statuses.append(REJECT)
                reasons.append(_reason(elig_rule, "store does not reach the target region"))

        if plan.get("offer_id") not in {None, offer.get("offer_id"), candidate.get("offer_id")}:
            statuses.append(UNKNOWN)
            reasons.append(_reason(evidence_rule, "plan offer_id conflicts with offer evidence"))
        if plan.get("status") == "active":
            statuses.append(PASS)
        elif plan.get("status") is None:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "plan status is missing"))
        else:
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, f"plan status={plan.get('status')}"))

        budget = plan.get("remaining_budget_fen")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            if budget > 0:
                statuses.append(PASS)
            else:
                statuses.append(REJECT)
                reasons.append(_reason(elig_rule, "remaining_budget_fen is not positive"))
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "remaining_budget_fen is missing or not numeric"))

        if plan.get("material_status") == "approved":
            statuses.append(PASS)
        elif plan.get("material_status") is None:
            statuses.append(UNKNOWN)
            reasons.append(_reason(elig_rule, "material_status is missing"))
        else:
            statuses.append(REJECT)
            reasons.append(_reason(elig_rule, f"material_status={plan.get('material_status')}"))

    business_time = _business_time(evidence)
    time_config = _as_mapping(policy.get("time"))
    time_values = (
        (product.get("updated_at") or product.get("as_of"), time_config.get("catalog_max_age_seconds"), "product"),
        (qualification.get("as_of") or qualification.get("updated_at"), time_config.get("qualification_max_age_seconds"), "qualification"),
        (offer.get("as_of") or offer.get("updated_at"), time_config.get("offer_max_age_seconds"), "offer"),
        (plan.get("as_of") or plan.get("updated_at"), time_config.get("plan_max_age_seconds"), "plan"),
    )
    observed_times: dict[str, datetime] = {}
    for value, max_age, label in time_values:
        status, detail = _time_status(value, business_time, max_age)
        statuses.append(status)
        if status != PASS:
            reasons.append(_reason(time_rule, f"{label} evidence is not fresh: {detail}"))
        parsed = _parse_time(value)
        if parsed is not None:
            observed_times[label] = parsed

    offer_time = observed_times.get("offer")
    plan_time = observed_times.get("plan")
    skew_limit = time_config.get("offer_plan_max_skew_seconds")
    if offer_time is None or plan_time is None or not _valid_int(skew_limit):
        statuses.append(UNKNOWN)
        reasons.append(_reason(time_rule, "offer/plan timestamps are required for cross-interface skew check"))
    else:
        skew = abs((offer_time - plan_time).total_seconds())
        if skew <= skew_limit:
            statuses.append(PASS)
        else:
            statuses.append(UNKNOWN)
            reasons.append(_reason(time_rule, f"offer/plan snapshot time skew {int(skew)}s exceeds {skew_limit}s"))

    version_status, version_reasons, version_rules = _check_version_consistency(
        term=term,
        candidate=candidate,
        product=product,
        qualification=qualification,
        offer=offer,
        plan=plan,
        evidence=evidence,
        policy=policy,
    )
    statuses.append(version_status)
    reasons.extend(version_reasons)
    _extend_unique(rules, version_rules)
    return _merge_status(*statuses), reasons, rules


def _eligibility(
    *,
    task: Mapping[str, Any],
    evidence: Mapping[str, Any],
    candidate: Mapping[str, Any],
    term: Optional[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> tuple[str, list[str], list[str], dict[str, Any]]:
    candidate_id = _candidate_id(candidate)
    product = _as_mapping(candidate.get("product"))
    offer = _as_mapping(candidate.get("offer"))
    store = _as_mapping(candidate.get("store"))
    sku_id = candidate.get("sku_id") or offer.get("sku_id") or product.get("sku_id")
    qualification = _merged_qualification(product, candidate.get("qualification"), sku_id)
    plans = _records_for(candidate.get("plan"), "offer_id", candidate.get("offer_id") or offer.get("offer_id"))
    if not plans:
        plans = _records_for(candidate.get("plan"), "ad_plan_id", candidate_id)
    errors = candidate.get("errors")
    reasons: list[str] = []
    rules: list[str] = []
    if errors:
        reasons.append(_reason(_rule(policy, "evidence"), "candidate has object-level evidence errors"))
        rules.append(_rule(policy, "evidence"))

    if not plans:
        status, single_reasons, single_rules = _single_plan_eligibility(
            task=task,
            evidence=evidence,
            product=product,
            qualification=qualification,
            offer=offer,
            plan={},
            store=store,
            candidate=candidate,
            term=term,
            policy=policy,
        )
        return UNKNOWN if not errors and status == REJECT else status, reasons + single_reasons, list(dict.fromkeys(rules + single_rules)), {
            "product": product,
            "qualification": qualification,
            "offer": offer,
            "store": store,
            "plan": {},
            "sku_id": sku_id,
            "candidate_id": candidate_id,
        }

    evaluations = []
    for plan in plans:
        evaluations.append(
            _single_plan_eligibility(
                task=task,
                evidence=evidence,
                product=product,
                qualification=qualification,
                offer=offer,
                plan=plan,
                store=store,
                candidate=candidate,
                term=term,
                policy=policy,
            )
        )
    # Multiple plans belong to one offer. One fully eligible active plan is
    # enough to confirm the offer; rejected or unknown sibling plans do not
    # erase that fact.
    if any(item[0] == PASS for item in evaluations):
        status = PASS
        selected_plan = next(item for item, evaluation in zip(plans, evaluations) if evaluation[0] == PASS)
        selected = evaluations[plans.index(selected_plan)]
        reasons.extend(selected[1])
        rules.extend(selected[2])
        plan = selected_plan
    elif any(item[0] == UNKNOWN for item in evaluations):
        status = UNKNOWN
        for evaluation in evaluations:
            reasons.extend(evaluation[1])
            rules.extend(evaluation[2])
        plan = plans[0]
    else:
        status = REJECT
        for evaluation in evaluations:
            reasons.extend(evaluation[1])
            rules.extend(evaluation[2])
        plan = plans[0]
    if errors:
        status = UNKNOWN if status == PASS else status
    return status, list(dict.fromkeys(reasons)), list(dict.fromkeys(rules)), {
        "product": product,
        "qualification": qualification,
        "offer": offer,
        "store": store,
        "plan": plan,
        "sku_id": sku_id,
        "candidate_id": candidate_id,
    }


def _scope_check(task: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[bool, list[str], list[str]]:
    scope = _as_mapping(policy.get("scope"))
    rule_id = _rule(policy, "scope")
    reasons: list[str] = []
    fields = ("query_id", "raw_query", "city_id", "region_id", "scene_id", "history_start", "history_end_exclusive")
    for field in fields:
        if not isinstance(task.get(field), str) or not task.get(field, "").strip():
            reasons.append(_reason(rule_id, f"task field {field} is missing"))
    if task.get("scene_id") not in set(_as_list(scope.get("scene_id"))):
        reasons.append(_reason(rule_id, "scene_id is outside policy scope"))
    city_ids = set(_as_list(scope.get("city_ids")))
    if task.get("city_id") not in city_ids:
        reasons.append(_reason(rule_id, "city_id is outside policy scope"))
    suffixes = set(_as_list(scope.get("region_suffixes")))
    expected_region = task.get("region_id")
    if not isinstance(expected_region, str) or "-" not in expected_region:
        reasons.append(_reason(rule_id, "region_id does not identify a city region"))
    else:
        region_city, suffix = expected_region.rsplit("-", 1)
        if region_city != task.get("city_id") or suffix not in suffixes:
            reasons.append(_reason(rule_id, "region_id does not belong to city_id and policy regions"))
    start = _parse_time(task.get("history_start"))
    end = _parse_time(task.get("history_end_exclusive"))
    if start is None or end is None:
        reasons.append(_reason(rule_id, "history window is not ISO8601 with timezone"))
    elif end <= start:
        reasons.append(_reason(rule_id, "history window is not increasing"))
    return not reasons, reasons, [rule_id]


def _rewrite_result_base(term_id: Any) -> dict[str, Any]:
    return {
        "term_id": term_id,
        "relevance": UNKNOWN,
        "eligibility": UNKNOWN,
        "decision": UNKNOWN,
        "model_decision": UNKNOWN,
        "reasons": [],
        "rule_refs": [],
        "evidence_refs": [],
    }


def _evaluate_rewrites(
    query_decision: Mapping[str, Any],
    evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    profile = _profile(query_decision)
    query_attrs = _query_attributes(profile)
    term_records = _term_records(evidence)
    term_by_id = _index_by_id(term_records, "term_id")
    rewrites = _as_mapping(query_decision).get("rewrites")
    results: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for rewrite in _as_list(rewrites):
        if not isinstance(rewrite, Mapping):
            continue
        term_id = rewrite.get("term_id")
        if term_id in seen:
            result = _rewrite_result_base(term_id)
            result["model_decision"] = UNKNOWN
            result["reasons"] = [_reason(_rule(policy, "rewrite"), "duplicate term_id in query handoff")]
            result["rule_refs"] = [_rule(policy, "rewrite"), _rule(policy, "evidence")]
            results.append(result)
            continue
        seen.add(term_id)
        result = _rewrite_result_base(term_id)
        model_status = _model_rewrite_status(rewrite)
        result["model_decision"] = model_status
        result["evidence_refs"] = _refs(rewrite.get("evidence_refs"), rewrite.get("evidence_ref"))
        result["rule_refs"] = _refs(rewrite.get("rule_refs"))
        if model_status == REJECT:
            result["relevance"] = REJECT
            result["eligibility"] = REJECT
            result["decision"] = REJECT
            result["reasons"].append(_reason(_rule(policy, "rewrite"), "query model rejected this rewrite"))
            _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "admit")])
            results.append(result)
            continue
        if model_status == UNKNOWN:
            result["reasons"].append(_reason(_rule(policy, "rewrite"), "query model did not provide a pass decision"))
            _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "evidence")])
            results.append(result)
            continue

        term = term_by_id.get(term_id)
        if term is None:
            result["reasons"].append(_reason(_rule(policy, "evidence"), "rewrite term is not present in catalog evidence"))
            _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "evidence")])
            results.append(result)
            continue
        result["term"] = term.get("term")
        term_index = term_records.index(term)
        _extend_unique(result["evidence_refs"], _term_ref(term, term_index))
        if term.get("enabled") is not True:
            result["relevance"] = REJECT
            result["eligibility"] = REJECT
            result["decision"] = REJECT
            result["reasons"].append(_reason(_rule(policy, "rewrite"), "term is not enabled in the index"))
            _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "evidence")])
            results.append(result)
            continue
        if term.get("index_version") is None:
            result["reasons"].append(_reason(_rule(policy, "evidence"), "enabled term has no index_version"))
            _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "evidence")])
            results.append(result)
            continue

        attr_status, attr_reasons, attr_rules = _compare_attributes(
            query_attrs,
            _term_attributes(rewrite, term),
            policy=policy,
            subject="term",
        )
        result["reasons"].extend(attr_reasons)
        _extend_unique(result["rule_refs"], attr_rules)
        if attr_status == REJECT:
            result["relevance"] = REJECT
            result["eligibility"] = REJECT
            result["decision"] = REJECT
        elif attr_status == UNKNOWN:
            result["reasons"].append(_reason(_rule(policy, "rewrite"), "term attributes are insufficient for hard-constraint verification"))
            _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "evidence")])
        else:
            result["relevance"] = PASS
            result["eligibility"] = PASS
            result["decision"] = PASS
        results.append(result)
    return results


def _review_map(item_decision: Mapping[str, Any]) -> tuple[dict[tuple[Any, Any], Mapping[str, Any]], set[tuple[Any, Any]]]:
    mapping: dict[tuple[Any, Any], Mapping[str, Any]] = {}
    duplicates: set[tuple[Any, Any]] = set()
    for review in _as_list(item_decision.get("reviews")):
        if not isinstance(review, Mapping):
            continue
        key = (review.get("term_id"), review.get("candidate_id"))
        if key in mapping:
            duplicates.add(key)
        else:
            mapping[key] = review
    return mapping, duplicates


def _item_base(candidate_id: Any, term_id: Any) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "term_id": term_id,
        "relevance": UNKNOWN,
        "eligibility": UNKNOWN,
        "reasons": [],
        "rule_refs": [],
        "evidence_refs": [],
    }


def _evaluate_items(
    *,
    task: Mapping[str, Any],
    query_attrs: Mapping[str, Any],
    item_decision: Mapping[str, Any],
    evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
    rewrite_map: Mapping[Any, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates = _candidate_records(evidence)
    reviews, duplicate_reviews = _review_map(item_decision)
    terms = _term_records(evidence)
    term_by_id = _index_by_id(terms, "term_id")
    outputs: list[dict[str, Any]] = []
    seen_candidates: set[tuple[Any, Any]] = set()
    for index, candidate in enumerate(candidates):
        candidate_id = _candidate_id(candidate)
        term_id = _candidate_term_id(candidate)
        result = _item_base(candidate_id, term_id)
        result["evidence_refs"] = _candidate_ref(candidate, index)
        result["sku_id"] = candidate.get("sku_id")
        result["offer_id"] = candidate.get("offer_id") or _as_mapping(candidate.get("offer")).get("offer_id")
        result["store_id"] = candidate.get("store_id") or _as_mapping(candidate.get("offer")).get("store_id")
        result["ad_plan_id"] = candidate.get("ad_plan_id") or candidate_id
        key = (term_id, candidate_id)
        if key in seen_candidates:
            result["reasons"].append(_reason(_rule(policy, "evidence"), "duplicate term_id/candidate_id evidence"))
            result["rule_refs"] = [_rule(policy, "evidence")]
            outputs.append(result)
            continue
        seen_candidates.add(key)
        if key in duplicate_reviews:
            result["reasons"].append(_reason(_rule(policy, "evidence"), "duplicate term_id/candidate_id item handoff"))
            result["rule_refs"] = [_rule(policy, "evidence")]
            outputs.append(result)
            continue

        review = reviews.get(key)
        if review is None:
            result["reasons"].append(_reason(_rule(policy, "evidence"), "item semantic review is missing; no keyword fallback is allowed"))
            result["rule_refs"] = [_rule(policy, "evidence"), _rule(policy, "rewrite")]
            outputs.append(result)
            continue
        result["evidence_refs"] = _refs(result["evidence_refs"], review.get("evidence_refs"), review.get("evidence_ref"))
        result["rule_refs"] = _refs(review.get("rule_refs"))
        model_status = _model_item_status(review)
        result["model_relevance"] = model_status
        _extend_unique(result["rule_refs"], [_rule(policy, "rewrite"), _rule(policy, "evidence")])

        rewrite = rewrite_map.get(term_id)
        term = term_by_id.get(term_id)
        if rewrite is None or rewrite.get("decision") != PASS:
            result["reasons"].append(_reason(_rule(policy, "rewrite"), "candidate term is not an accepted rewrite"))
            hard_term_status = REJECT if rewrite and rewrite.get("decision") == REJECT else UNKNOWN
        else:
            hard_term_status = PASS
        if term is None:
            hard_term_status = UNKNOWN
            result["reasons"].append(_reason(_rule(policy, "evidence"), "candidate term is absent from catalog evidence"))

        candidate_product = _as_mapping(candidate.get("product"))
        attr_status, attr_reasons, attr_rules = _compare_attributes(
            query_attrs,
            _object_attrs(candidate_product),
            policy=policy,
            subject="product",
        )
        result["reasons"].extend(attr_reasons)
        _extend_unique(result["rule_refs"], attr_rules)
        title_status, title_reasons, title_rules = _title_conflict(candidate_product, policy)
        result["reasons"].extend(title_reasons)
        _extend_unique(result["rule_refs"], title_rules)
        # When the title makes an explicit package claim that conflicts with
        # structured pack fields, PACK-001 requires manual review. Do not let
        # the structured value win merely because it also conflicts with the
        # query. Other independent hard conflicts (for example a brand
        # mismatch) still remain rejecting conflicts.
        title_pack_conflict = title_status == UNKNOWN and any(
            "title and structured pack_count conflict" in reason
            or "title and structured pack_unit conflict" in reason
            for reason in title_reasons
        )
        independent_attr_reject = attr_status == REJECT and any(
            not reason.startswith(_rule(policy, "pack") + ":")
            for reason in attr_reasons
        )
        if title_pack_conflict and attr_status == REJECT and not independent_attr_reject:
            hard_relevance = UNKNOWN
        else:
            hard_relevance = _merge_status(hard_term_status, attr_status, title_status)
        if hard_relevance == REJECT:
            result["relevance"] = REJECT
        elif hard_relevance == UNKNOWN:
            result["relevance"] = UNKNOWN
        elif model_status == REJECT:
            result["relevance"] = REJECT
            result["reasons"].append(_reason(_rule(policy, "rewrite"), "item model rejected term-item or query-item relevance"))
        elif model_status == UNKNOWN:
            result["relevance"] = UNKNOWN
            result["reasons"].append(_reason(_rule(policy, "evidence"), "item model relevance is unknown or incomplete"))
        else:
            result["relevance"] = PASS

        eligibility_status, eligibility_reasons, eligibility_rules, facts = _eligibility(
            task=task,
            evidence=evidence,
            candidate=candidate,
            term=term,
            policy=policy,
        )
        result["eligibility"] = eligibility_status
        result["reasons"].extend(eligibility_reasons)
        _extend_unique(result["rule_refs"], eligibility_rules)
        result["offer_id"] = result.get("offer_id") or facts["offer"].get("offer_id")
        result["store_id"] = result.get("store_id") or facts["offer"].get("store_id")
        result["sku_id"] = result.get("sku_id") or facts["sku_id"]
        result["reasons"] = list(dict.fromkeys(result["reasons"]))
        outputs.append(result)
    return outputs


def _history_summary(
    history: Optional[Mapping[str, Any]], accepted_items: list[Mapping[str, Any]], policy: Mapping[str, Any]
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "present": isinstance(history, Mapping),
        "complete": False,
        "data_complete": False,
        "gap_count": None,
        "semantic_linked": False,
        "state": "missing",
    }
    if not isinstance(history, Mapping):
        return summary
    summary["complete"] = history.get("complete") is True
    summary["data_complete"] = summary["complete"]
    if history.get("load_gaps"):
        summary["data_complete"] = False
    unknown_count = history.get("unknown_count")
    if isinstance(unknown_count, int) and unknown_count > 0:
        summary["data_complete"] = False
    gap_count = history.get("verified_gap_count")
    if _valid_int(gap_count) and gap_count >= 0:
        summary["gap_count"] = gap_count
    else:
        summary["data_complete"] = False

    accepted_term_ids = set()
    accepted_identifiers: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted_items:
        term_id = item.get("term_id")
        accepted_term_ids.add(term_id)
        accepted_identifiers[term_id].append(
            {
                "candidate_id": item.get("candidate_id"),
                "offer_id": item.get("offer_id"),
                "sku_id": item.get("sku_id"),
            }
        )

    def linked(row: Mapping[str, Any]) -> bool:
        # A history row is usable only when SQL has explicitly established
        # historical qualification. A raw replay row or an arbitrary row
        # linked by the same store is not a verified opportunity.
        historically_qualified = row.get("historically_qualified")
        verification_status = row.get("verification_status")
        if historically_qualified is not None and historically_qualified is not True:
            return False
        if verification_status is not None:
            normalized_status = _norm_text(verification_status)
            if normalized_status not in {"verified", "confirmed", "qualified", "pass", "passed", "complete", "valid"}:
                return False
        if historically_qualified is not True and verification_status is None:
            return False

        term_id = row.get("term_id") or row.get("matched_term_id")
        candidate_id = row.get("candidate_id") or row.get("ad_plan_id")
        if term_id not in accepted_term_ids:
            return False
        row_identifiers = {
            "candidate_id": candidate_id,
            "offer_id": row.get("offer_id"),
            "sku_id": row.get("sku_id"),
        }
        # At least one exact identity is required. All identities supplied by
        # SQL must agree with the same accepted item, preventing a term+sku
        # from being combined with another candidate's offer.
        for accepted in accepted_identifiers.get(term_id, []):
            comparable = [
                key
                for key, value in row_identifiers.items()
                if value is not None and accepted.get(key) is not None
            ]
            if not comparable:
                continue
            if all(row_identifiers[key] == accepted.get(key) for key in comparable):
                return True
        return False

    rows = _as_list(history.get("evidence_rows"))
    links = _as_list(history.get("verified_links")) + _as_list(history.get("semantic_links"))
    for row in rows + links:
        if isinstance(row, Mapping) and linked(row):
            summary["semantic_linked"] = True
            break
    if not summary["complete"] or not summary["data_complete"]:
        summary["state"] = "incomplete"
    elif summary["gap_count"] == 0:
        summary["state"] = "no_verified_gap"
    elif summary["semantic_linked"]:
        summary["state"] = "verified_linked"
    else:
        # verified_gap_count is deliberately never treated as a semantic
        # opportunity on its own.
        summary["state"] = "potential_only"
    return summary


def _window_is_complete_7_days(
    task: Mapping[str, Any], history: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[bool, str]:
    """Validate the exact natural-day window used by value segmentation."""

    value_config = _as_mapping(policy.get("value"))
    expected_days = value_config.get("history_window_days")
    if not _valid_int(expected_days) or expected_days <= 0:
        return False, "policy value.history_window_days is missing or invalid"

    def window(source: Mapping[str, Any]) -> tuple[Any, Any]:
        parameters = _as_mapping(source.get("parameters"))
        start = parameters.get("history_start") or parameters.get("start") or source.get("history_start") or source.get("start")
        end = parameters.get("history_end_exclusive") or parameters.get("end_exclusive") or source.get("history_end_exclusive") or source.get("end_exclusive")
        return start, end

    task_start_raw, task_end_raw = window(task)
    history_start_raw, history_end_raw = window(history)
    if history_start_raw is not None or history_end_raw is not None:
        if history_start_raw != task_start_raw or history_end_raw != task_end_raw:
            return False, "history.parameters window does not match task window"
    start = _parse_time(task_start_raw)
    end = _parse_time(task_end_raw)
    if start is None or end is None:
        return False, "task/parameters history window is missing or invalid"
    if start.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8):
        return False, "value window must use +08:00"
    if any((start.hour, start.minute, start.second, start.microsecond, end.hour, end.minute, end.second, end.microsecond)):
        return False, "value window must contain complete natural days"
    if end - start != timedelta(days=expected_days):
        return False, f"value window must be exactly {expected_days} natural days"
    return True, ""


def _value_tier(
    task: Mapping[str, Any],
    history: Optional[Mapping[str, Any]],
    items: list[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> tuple[str, int, int, list[str]]:
    value_rule = _rule(policy, "value")
    if not isinstance(history, Mapping) or history.get("complete") is not True:
        return UNKNOWN, 0, 0, [_reason(value_rule, "history is not complete")]
    if history.get("load_gaps") or (isinstance(history.get("unknown_count"), int) and history.get("unknown_count") > 0):
        return UNKNOWN, 0, 0, [_reason(value_rule, "history data is incomplete or contains unknown requests")]
    request_count = history.get("request_count")
    if not _valid_int(request_count) or request_count < 0:
        return UNKNOWN, 0, 0, [_reason(value_rule, "valid request_count is missing")]
    window_ok, window_reason = _window_is_complete_7_days(task, history, policy)
    if not window_ok:
        return UNKNOWN, 0, 0, [_reason(value_rule, window_reason)]

    relevant_items = [item for item in items if item.get("relevance") == PASS]
    if not relevant_items:
        return UNKNOWN, 0, 0, [_reason(value_rule, "no semantically relevant item is confirmed")]
    if any(item.get("eligibility") == UNKNOWN for item in relevant_items):
        return UNKNOWN, 0, 0, [_reason(value_rule, "necessary current eligibility remains unknown")]
    store_ids = {
        item.get("store_id")
        for item in relevant_items
        if item.get("eligibility") == PASS and item.get("store_id") is not None
    }
    store_count = len(store_ids)
    value_config = _as_mapping(policy.get("value"))
    v1 = _as_mapping(value_config.get("v1"))
    v2 = _as_mapping(value_config.get("v2"))
    required_values = (
        v1.get("min_valid_requests"),
        v1.get("min_distinct_stores"),
        v2.get("min_valid_requests"),
        v2.get("min_distinct_stores"),
        value_config.get("v3"),
    )
    if (
        not _valid_int(required_values[0])
        or not _valid_int(required_values[1])
        or not _valid_int(required_values[2])
        or not _valid_int(required_values[3])
        or not required_values[4]
    ):
        return UNKNOWN, 0, 0, [_reason(value_rule, "policy value-tier thresholds are missing or invalid")]
    if request_count >= required_values[0] and store_count >= required_values[1]:
        return "V1", request_count, store_count, []
    if request_count >= required_values[2] and store_count >= required_values[3]:
        return "V2", request_count, store_count, []
    return "V3", request_count, store_count, []


def _queue_entry(
    *,
    kind: str,
    reasons: Iterable[str],
    rule_refs: Iterable[str],
    evidence_refs: Iterable[str],
    term_id: Any = None,
    candidate_id: Any = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": kind,
        "reasons": list(dict.fromkeys(reasons)),
        "rule_refs": list(dict.fromkeys(rule_refs)),
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
    }
    if term_id is not None:
        entry["term_id"] = term_id
    if candidate_id is not None:
        entry["candidate_id"] = candidate_id
    return entry


def check(
    task: Mapping[str, Any],
    query_decision: Mapping[str, Any],
    item_decision: Mapping[str, Any],
    evidence: Mapping[str, Any],
    policy: Mapping[str, Any] | str | Path,
    history: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Combine the two model handoffs with deterministic hard constraints.

    The sixth ``history`` argument is optional for compatibility with callers
    that put it into the policy runtime context. The function always returns
    a recommendation string and stable ``items``/``rewrites`` records.
    """

    policy_obj = load_policy(policy) if isinstance(policy, (str, Path)) else _as_mapping(policy)
    task_obj = _as_mapping(task)
    query_obj = _as_mapping(query_decision)
    item_obj = _as_mapping(item_decision)
    evidence_obj = _as_mapping(evidence)
    history_obj = _extract_history(evidence_obj, policy_obj, history)

    result: dict[str, Any] = {
        "rewrites": [],
        "items": [],
        "recommendation": "hold",
        "risk": _profile(query_obj).get("risk", query_obj.get("risk", UNKNOWN)),
        "value_tier": UNKNOWN,
        "review_queue": [],
    }
    if result["risk"] not in {"L0", "L1", "L2"}:
        result["risk"] = UNKNOWN

    scope_ok, scope_reasons, scope_rules = _scope_check(task_obj, policy_obj)
    profile = _profile(query_obj)
    query_attrs = _query_attributes(profile)
    admission_stopped = not scope_ok or result["risk"] in {"L0", "L2"}
    provenance_missing = _attribute_provenance(profile, query_attrs)
    if provenance_missing:
        result["review_queue"].append(
            _queue_entry(
                kind="query_attribute_provenance",
                reasons=[_reason(_rule(policy_obj, "evidence"), "attribute provenance missing: " + ",".join(provenance_missing))],
                rule_refs=[_rule(policy_obj, "evidence"), _rule(policy_obj, "rewrite")],
                evidence_refs=_refs(profile.get("evidence_refs")),
            )
        )
    if scope_reasons:
        result["review_queue"].append(
            _queue_entry(
                kind="scope",
                reasons=scope_reasons,
                rule_refs=scope_rules,
                evidence_refs=[],
            )
        )

    candidate_records = _candidate_records(evidence_obj)
    handoff_errors = _handoff_errors(query_obj, item_obj, has_candidates=bool(candidate_records))
    if handoff_errors:
        result["review_queue"].append(
            _queue_entry(
                kind="model_handoff",
                reasons=[_reason(_rule(policy_obj, "evidence"), error) for error in handoff_errors],
                rule_refs=[_rule(policy_obj, "evidence"), _rule(policy_obj, "admit")],
                evidence_refs=_refs(query_obj.get("evidence_refs"), item_obj.get("evidence_refs")),
            )
        )

    rewrite_results = _evaluate_rewrites(query_obj, evidence_obj, policy_obj)
    # A model pass is not enough: the term must have an enabled catalog record
    # and at least one collected candidate before it can support submission.
    candidate_terms = {_candidate_term_id(candidate) for candidate in candidate_records}
    for rewrite in rewrite_results:
        if rewrite.get("decision") == PASS and rewrite.get("term_id") not in candidate_terms:
            rewrite["model_decision"] = PASS
            rewrite["decision"] = UNKNOWN
            rewrite["relevance"] = UNKNOWN
            rewrite["eligibility"] = UNKNOWN
            rewrite["reasons"].append(_reason(_rule(policy_obj, "rewrite"), "enabled rewrite has no collected candidate"))
            _extend_unique(rewrite["rule_refs"], [_rule(policy_obj, "rewrite"), _rule(policy_obj, "evidence")])
    for rewrite in rewrite_results:
        rewrite["reasons"] = list(dict.fromkeys(rewrite.get("reasons", [])))
        rewrite["rule_refs"] = list(dict.fromkeys(rewrite.get("rule_refs", [])))
        rewrite["evidence_refs"] = list(dict.fromkeys(rewrite.get("evidence_refs", [])))
    result["rewrites"] = rewrite_results

    rewrite_map = {row.get("term_id"): row for row in rewrite_results}
    result["items"] = _evaluate_items(
        task=task_obj,
        query_attrs=query_attrs,
        item_decision=item_obj,
        evidence=evidence_obj,
        policy=policy_obj,
        rewrite_map=rewrite_map,
    )

    accepted_current = [
        item
        for item in result["items"]
        if item.get("relevance") == PASS and item.get("eligibility") == PASS
    ]
    if admission_stopped:
        # Scope failures and L0/L2 admission decisions stop the downstream
        # history/value stage. Keep explicit output fields without turning an
        # inapplicable stage into a request for unrelated evidence.
        history_state = {"state": "not_evaluated", "gap_count": None}
        value_tier = UNKNOWN
        request_count = None
        store_count = None
        value_reasons = []
    else:
        history_state = _history_summary(history_obj, accepted_current, policy_obj)
        value_tier, request_count, store_count, value_reasons = _value_tier(
            task_obj, history_obj, result["items"], policy_obj
        )
    result["value_tier"] = value_tier
    result["history_status"] = history_state["state"]
    result["history_verified_gap_count"] = history_state["gap_count"]
    result["history_gap_is_potential_only"] = history_state["gap_count"] is not None and history_state["gap_count"] > 0
    confirmed_offer_ids = {
        item.get("offer_id")
        for item in accepted_current
        if item.get("offer_id") is not None
    }
    result["confirmed_current_offer_count"] = len(confirmed_offer_ids)
    result["confirmed_current_candidate_count"] = len(accepted_current)
    result["distinct_store_count"] = store_count
    result["valid_request_count"] = request_count

    for item in result["items"]:
        if item.get("relevance") == UNKNOWN or item.get("eligibility") == UNKNOWN:
            result["review_queue"].append(
                _queue_entry(
                    kind="item_review",
                    term_id=item.get("term_id"),
                    candidate_id=item.get("candidate_id"),
                    reasons=item.get("reasons", []),
                    rule_refs=item.get("rule_refs", []),
                    evidence_refs=item.get("evidence_refs", []),
                )
            )
    for rewrite in result["rewrites"]:
        if rewrite.get("decision") == UNKNOWN:
            result["review_queue"].append(
                _queue_entry(
                    kind="rewrite_review",
                    term_id=rewrite.get("term_id"),
                    reasons=rewrite.get("reasons", []),
                    rule_refs=rewrite.get("rule_refs", []),
                    evidence_refs=rewrite.get("evidence_refs", []),
                )
            )
    if not admission_stopped and value_reasons:
        result["review_queue"].append(
            _queue_entry(
                kind="value_tier",
                reasons=value_reasons,
                rule_refs=[_rule(policy_obj, "value")],
                evidence_refs=_refs(history_obj.get("evidence_refs") if history_obj else None),
            )
        )
    if not admission_stopped and history_state["state"] in {"missing", "incomplete", "potential_only"}:
        history_reason = {
            "missing": "history evidence is missing",
            "incomplete": "history is incomplete or contains unknown/load gaps",
            "potential_only": "verified_gap_count is a potential opportunity without semantic term-item linkage",
        }[history_state["state"]]
        result["review_queue"].append(
            _queue_entry(
                kind="history_review",
                reasons=[_reason(_rule(policy_obj, "decision"), history_reason)],
                rule_refs=[_rule(policy_obj, "decision"), _rule(policy_obj, "evidence")],
                evidence_refs=_refs(history_obj.get("evidence_refs") if history_obj else None),
            )
        )

    # Scope and explicit L0 always stop expansion. L2 is a separate hold path.
    if not scope_ok or result["risk"] == "L0":
        result["recommendation"] = "reject"
    elif result["risk"] == "L2":
        result["recommendation"] = "hold"
    else:
        ready_rewrites = [row for row in result["rewrites"] if row.get("decision") == PASS]
        unknown_rewrites = [row for row in result["rewrites"] if row.get("decision") == UNKNOWN]
        rejected_rewrites = [row for row in result["rewrites"] if row.get("decision") == REJECT]
        decision_config = _as_mapping(policy_obj.get("decision"))
        minimum_offers = decision_config.get("minimum_confirmed_current_offers")
        offer_gate = _valid_int(minimum_offers) and result["confirmed_current_offer_count"] >= minimum_offers
        if offer_gate and history_state["state"] == "verified_linked" and not handoff_errors and not provenance_missing:
            result["recommendation"] = "submit_for_human_review"
        elif not ready_rewrites and not unknown_rewrites and (rejected_rewrites or not result["rewrites"]):
            result["recommendation"] = "no_rewrite"
        elif ready_rewrites and not candidate_records and not unknown_rewrites:
            result["recommendation"] = "no_rewrite"
        else:
            result["recommendation"] = "hold"

    # A submit result is only legal under all three independent gates, even if
    # a future caller adds a more permissive policy branch above.
    if result["recommendation"] == "submit_for_human_review":
        decision_config = _as_mapping(policy_obj.get("decision"))
        minimum_offers = decision_config.get("minimum_confirmed_current_offers")
        if (
            not _valid_int(minimum_offers)
            or result["confirmed_current_offer_count"] < minimum_offers
            or history_state["state"] != "verified_linked"
        ):
            result["recommendation"] = "hold"
        if not any(row.get("decision") == PASS for row in result["rewrites"]):
            result["recommendation"] = "hold"
    result["review_queue"] = list(dict.fromkeys(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in result["review_queue"]
    ))
    result["review_queue"] = [json.loads(entry) for entry in result["review_queue"]]
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic medical query hard-constraint checks")
    parser.add_argument("--task", required=True)
    parser.add_argument("--query-decision", required=True)
    parser.add_argument("--item-decision", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--history")
    args = parser.parse_args()

    def read_json(path: str) -> Any:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    result = check(
        read_json(args.task),
        read_json(args.query_decision),
        read_json(args.item_decision),
        read_json(args.evidence),
        args.policy,
        history=read_json(args.history) if args.history else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
