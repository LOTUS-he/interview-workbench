"""Validate Skill resource references and generate a reverse dependency index.

The resource files use JSON-compatible YAML, so the standard-library json
module is intentionally sufficient.  The main runner may import
``load_rule_ids`` to reject model handoff references that are not declared in
references/resource-map.yaml.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parents[1]
RULE_ID_RE = re.compile(r"\b[A-Z][A-Z0-9_]*-[0-9]{3}\b")
STEP_IDS = {"01", "02", "03", "04", "05", "06", "07", "08"}
ARTIFACT_IDS = {"00", "10", "20", "30", "40", "50"}
STABLE_RULE_IDS = {
    "SCOPE-001",
    "ADMIT-001",
    "REWRITE-001",
    "BRAND-001",
    "FORM-001",
    "PACK-001",
    "AUDIENCE-001",
    "EVIDENCE-001",
    "ELIG-001",
    "TIME-001",
    "DECISION-001",
    "VALUE-001",
}


class ResourceMapError(ValueError):
    """The map itself is malformed, rather than merely incomplete."""


def _root(root: str | Path | None = None) -> Path:
    candidate = Path(root) if root is not None else SKILL_ROOT
    return candidate.resolve()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourceMapError(f"Invalid JSON-compatible YAML/JSON: {path}: {exc}") from exc


def _relative_path(root: Path, value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ResourceMapError(f"{label} must be a non-empty relative path")
    raw = Path(value)
    if raw.is_absolute():
        raise ResourceMapError(f"{label} must be relative: {value}")
    resolved = (root / raw).resolve()
    if resolved != root and root not in resolved.parents and PROJECT_ROOT not in resolved.parents and resolved != PROJECT_ROOT:
        raise ResourceMapError(f"{label} escapes project root: {value}")
    return resolved


def load_resource_map(root: str | Path | None = None) -> dict[str, Any]:
    """Load the JSON-compatible resource map from the Skill root."""

    skill_root = _root(root)
    path = skill_root / "references" / "resource-map.yaml"
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ResourceMapError("resource-map.yaml must contain an object")
    if value.get("schema_version") != "1.0":
        raise ResourceMapError("resource-map.yaml must declare schema_version 1.0")
    if value.get("format") != "json-compatible-yaml":
        raise ResourceMapError("resource-map.yaml must declare JSON-compatible YAML")
    return value


def load_rule_ids(root: str | Path | None = None) -> set[str]:
    """Return all rule IDs registered in resource-map.yaml.

    This is the shared allow-list for model ``rule_refs``.  It deliberately
    reads the map rather than scanning arbitrary prose, so a source document
    cannot silently introduce a rule that the runner does not know.  A caller
    should treat a malformed map as a validation error, not as an empty set.
    """

    mapping = load_resource_map(root)
    rows = mapping.get("rules")
    if not isinstance(rows, list):
        raise ResourceMapError("resource-map.yaml rules must be a list")
    ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("rule_id"), str):
            raise ResourceMapError(f"rules[{index}].rule_id must be a string")
        rule_id = row["rule_id"]
        if not RULE_ID_RE.fullmatch(rule_id):
            raise ResourceMapError(f"Invalid rule ID: {rule_id}")
        if rule_id in ids:
            raise ResourceMapError(f"Duplicate rule ID: {rule_id}")
        ids.add(rule_id)
    return ids


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResourceMapError(f"{label} must be a list")
    return value


def _check_path(
    root: Path,
    value: str,
    *,
    label: str,
    missing: list[dict[str, str]],
    required: bool = False,
) -> Path:
    path = _relative_path(root, value, label=label)
    if not path.is_file():
        missing.append({"path": value, "label": label, "required": str(required).lower()})
    return path


def _check_string_list(value: Any, label: str) -> list[str]:
    rows = _as_list(value, label)
    if any(not isinstance(item, str) or not item.strip() for item in rows):
        raise ResourceMapError(f"{label} must contain only non-empty strings")
    return rows


def _source_rule_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    return set(RULE_ID_RE.findall(text))


def _resource_lookup(mapping: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = mapping.get("authoritative_files")
    if not isinstance(rows, list):
        raise ResourceMapError("authoritative_files must be a list")
    result: dict[str, dict[str, Any]] = {}
    paths: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ResourceMapError(f"authoritative_files[{index}] must be an object")
        resource_id = row.get("resource_id")
        path = row.get("path")
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ResourceMapError(f"authoritative_files[{index}].resource_id is required")
        if resource_id in result:
            raise ResourceMapError(f"Duplicate resource_id: {resource_id}")
        if not isinstance(path, str) or not path.strip():
            raise ResourceMapError(f"authoritative_files[{index}].path is required")
        if path in paths:
            raise ResourceMapError(f"Duplicate resource path: {path}")
        paths.add(path)
        result[resource_id] = row
    return result


def _validate_map(root: Path, mapping: dict[str, Any], strict: bool) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    errors: list[str] = []
    missing: list[dict[str, str]] = []
    resources = _resource_lookup(mapping)

    for resource_id, row in resources.items():
        try:
            path = _check_path(
                root,
                row["path"],
                label=f"authoritative_files[{resource_id}]",
                missing=missing,
                required=bool(row.get("required", False)),
            )
            row["exists"] = path.is_file()
        except ResourceMapError as exc:
            errors.append(str(exc))

    steps = _as_list(mapping.get("steps"), "steps")
    step_by_id: dict[str, dict[str, Any]] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{index}] must be an object")
            continue
        step_id = step.get("step_id")
        if step_id not in STEP_IDS:
            errors.append(f"steps[{index}] has invalid step_id: {step_id}")
            continue
        if step_id in step_by_id:
            errors.append(f"Duplicate step_id: {step_id}")
            continue
        step_by_id[step_id] = step
        for field in ("reads", "execute", "tests", "outputs"):
            try:
                _as_list(step.get(field), f"steps[{step_id}].{field}")
            except ResourceMapError as exc:
                errors.append(str(exc))
        for output_id in step.get("outputs", []):
            if output_id not in ARTIFACT_IDS:
                errors.append(f"steps[{step_id}].outputs has invalid public step_id: {output_id}")
        for resource_ref in step.get("reads", []):
            if isinstance(resource_ref, str) and resource_ref not in resources:
                errors.append(f"steps[{step_id}].reads references unknown resource_id: {resource_ref}")
        for field in ("execute", "tests"):
            for item in step.get(field, []):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"steps[{step_id}].{field} contains an invalid path")
                    continue
                if item in resources:
                    continue
                try:
                    _check_path(root, item, label=f"steps[{step_id}].{field}", missing=missing)
                except ResourceMapError as exc:
                    errors.append(str(exc))

    if set(step_by_id) != STEP_IDS:
        errors.append(f"steps must contain exactly 01-08; got {sorted(step_by_id)}")

    rules = _as_list(mapping.get("rules"), "rules")
    rule_by_id: dict[str, dict[str, Any]] = {}
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rules[{index}] must be an object")
            continue
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
            errors.append(f"rules[{index}] has invalid rule_id: {rule_id}")
            continue
        if rule_id in rule_by_id:
            errors.append(f"Duplicate rule_id: {rule_id}")
            continue
        rule_by_id[rule_id] = rule
        source = rule.get("source")
        if not isinstance(source, str):
            errors.append(f"rules[{rule_id}].source is required")
        else:
            try:
                source_path = _check_path(root, source, label=f"rules[{rule_id}].source", missing=missing, required=True)
                if source_path.is_file() and rule_id not in _source_rule_ids(source_path):
                    errors.append(f"rules[{rule_id}] is not declared in source: {source}")
            except ResourceMapError as exc:
                errors.append(str(exc))
        for field in ("steps", "scripts", "tests"):
            try:
                values = _check_string_list(rule.get(field), f"rules[{rule_id}].{field}")
            except ResourceMapError as exc:
                errors.append(str(exc))
                continue
            if field == "steps":
                errors.extend(f"rules[{rule_id}].steps has invalid step_id: {value}" for value in values if value not in STEP_IDS)
            else:
                for value in values:
                    if value in resources:
                        continue
                    try:
                        _check_path(root, value, label=f"rules[{rule_id}].{field}", missing=missing)
                    except ResourceMapError as exc:
                        errors.append(str(exc))

    missing_stable = sorted(STABLE_RULE_IDS - set(rule_by_id))
    if missing_stable:
        errors.append("resource-map.yaml is missing stable machine rule IDs: " + ", ".join(missing_stable))

    if strict:
        errors.extend(f"Missing required resource: {item['path']} ({item['label']})" for item in missing)

    reverse_by_rule: dict[str, dict[str, Any]] = {}
    for rule_id in sorted(rule_by_id):
        rule = rule_by_id[rule_id]
        reverse_by_rule[rule_id] = {
            "source": rule.get("source"),
            "steps": list(rule.get("steps", [])),
            "scripts": list(rule.get("scripts", [])),
            "tests": list(rule.get("tests", [])),
        }

    reverse_by_step: dict[str, dict[str, Any]] = {}
    for step_id in sorted(step_by_id):
        step = step_by_id[step_id]
        reverse_by_step[step_id] = {
            "name": step.get("name"),
            "reads": list(step.get("reads", [])),
            "execute": list(step.get("execute", [])),
            "tests": list(step.get("tests", [])),
            "outputs": list(step.get("outputs", [])),
            "rules": sorted(rule_id for rule_id, row in rule_by_id.items() if step_id in row.get("steps", [])),
        }

    reverse_by_resource: dict[str, dict[str, Any]] = {}
    for resource_id, row in resources.items():
        path = row.get("path")
        reverse_by_resource[resource_id] = {
            "path": path,
            "kind": row.get("kind"),
            "exists": bool(row.get("exists", False)),
            "required": bool(row.get("required", False)),
            "steps": sorted(step_id for step_id, step in step_by_id.items() if resource_id in step.get("reads", []) or resource_id in step.get("execute", []) or resource_id in step.get("tests", [])),
            "rules": sorted(rule_id for rule_id, row in rule_by_id.items() if row.get("source") == path or path in row.get("scripts", []) or path in row.get("tests", [])),
        }

    backlinks = {
        "schema_version": "1.0",
        "generated_by": "scripts/validate_resources.py",
        "skill": mapping.get("skill"),
        "strict": strict,
        "stable_rule_ids": sorted(STABLE_RULE_IDS),
        "rule_ids": sorted(rule_by_id),
        "missing": missing,
        "by_step": reverse_by_step,
        "by_rule": reverse_by_rule,
        "by_resource": reverse_by_resource,
    }
    return errors, missing, backlinks


def validate_resources(root: str | Path | None = None, *, strict: bool = False) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    """Validate the resource map and return ``(errors, missing, backlinks)``."""

    skill_root = _root(root)
    mapping = load_resource_map(skill_root)
    return _validate_map(skill_root, mapping, strict)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Skill resources and write backlinks.json")
    parser.add_argument("--output", required=True, help="Path for the generated backlinks JSON")
    parser.add_argument("--strict", action="store_true", help="Fail if a referenced resource is not yet present")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        errors, missing, backlinks = validate_resources(strict=args.strict)
        output = Path(args.output).resolve()
        backlinks["output"] = str(output)
        _write_json(output, backlinks)
        result = {
            "valid": not errors,
            "strict": args.strict,
            "errors": errors,
            "missing_count": len(missing),
            "output": str(output),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 1 if errors else 0
    except (OSError, ResourceMapError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
