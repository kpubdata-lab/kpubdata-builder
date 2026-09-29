#!/usr/bin/env python3
"""Write the response fixtures a client checks its parser against (#814).

Adding an optional field to a response is additive under the contract (ADR 0013), but a
client whose parser rejects unknown keys breaks on it anyway: Studio's
`silverColumnInfoSchema.strict()` did exactly that when 1.30.0 added `logical_type`
(#735). The contract's `additionalProperties: false` describes what this Builder sends at
this version; it is not a rule for a client to reject what a later minor adds.

So every named 2xx response example in `contract/builder-api.yaml` becomes three bodies:

    current               the example as this contract version sends it
    with_additive_fields  the same body with an unknown optional field added to every
                          object the contract declares properties for — what a later
                          minor may send. A client must accept it.
    required_type_broken  the same body with one required top-level field retyped.
                          A client must still reject it.

The output is `contract/fixtures/responses.json`. It is generated, not written by hand:
the unit tests regenerate it and fail when the committed file differs, so it cannot
drift from the contract.

Usage:
    python scripts/generate_response_fixtures.py          # rewrite the file
    python scripts/generate_response_fixtures.py --check  # exit 1 when it is stale
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract" / "builder-api.yaml"
OUTPUT = ROOT / "contract" / "fixtures" / "responses.json"
FIXTURE_FORMAT = 1
PROBE_FIELD = "future_optional_field"
PROBE_VALUE = "added by a later minor contract version"

RULES = (
    "A response may gain optional fields in any minor version; a client ignores fields "
    "it does not know. A required field keeps its name and type until the next major "
    "version; a client rejects a body whose required field is missing or mistyped."
)


def _load_extractor() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_extract_openapi_examples", Path(__file__).resolve().parent / "extract_openapi_examples.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve(document: dict[str, Any], schema: Any) -> dict[str, Any]:
    seen: set[str] = set()
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref in seen:
            break
        seen.add(ref)
        node: Any = document
        for part in ref[2:].split("/"):
            node = node[part]
        schema = node
    return schema if isinstance(schema, dict) else {}


def _types(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        return [raw]
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _fits(document: dict[str, Any], schema: dict[str, Any], value: Any) -> bool:
    """A shallow check, enough to pick the oneOf/anyOf branch a value was written for."""
    schema = _resolve(document, schema)
    types = _types(schema)
    if isinstance(value, dict):
        if types and "object" not in types:
            return False
        if not set(schema.get("required", [])) <= set(value):
            return False
        properties = schema.get("properties", {})
        closed = schema.get("additionalProperties") is False
        return not closed or set(value) <= set(properties)
    if isinstance(value, list):
        return not types or "array" in types
    return True


def _branch(document: dict[str, Any], schema: dict[str, Any], value: Any) -> dict[str, Any]:
    """The schema that governs ``value``: resolved, with a matching oneOf/anyOf branch."""
    schema = _resolve(document, schema)
    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                if _fits(document, branch, value):
                    return _branch(document, branch, value)
            return {}
    merged = dict(schema)
    for part in schema.get("allOf", []) or []:
        resolved = _branch(document, part, value)
        merged.setdefault("properties", {})
        merged["properties"] = {**resolved.get("properties", {}), **merged["properties"]}
    return merged


def add_probes(
    document: dict[str, Any], schema: dict[str, Any], value: Any, path: str, added: list[str]
) -> Any:
    """Add ``PROBE_FIELD`` to every object whose schema names its properties.

    Maps with free keys (a row, `null_counts`) are left alone: a new key there is a new
    column or a new count, not a new field.
    """
    governing = _branch(document, schema, value)
    if isinstance(value, dict):
        properties = governing.get("properties")
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(properties, dict) and key in properties:
                out[key] = add_probes(document, properties[key], item, f"{path}.{key}", added)
            elif isinstance(governing.get("additionalProperties"), dict):
                sub = governing["additionalProperties"]
                out[key] = add_probes(document, sub, item, f"{path}.{key}", added)
            else:
                out[key] = copy.deepcopy(item)
        if isinstance(properties, dict) and properties and PROBE_FIELD not in properties:
            out[PROBE_FIELD] = PROBE_VALUE
            added.append(path)
        return out
    if isinstance(value, list):
        items = governing.get("items", {})
        return [
            add_probes(document, items, item, f"{path}[{index}]", added)
            for index, item in enumerate(value)
        ]
    return copy.deepcopy(value)


def _retyped(value: Any) -> Any:
    if isinstance(value, bool):
        return "true"
    if isinstance(value, (int, float)):
        return "not a number"
    if isinstance(value, str):
        return 12345
    if isinstance(value, list):
        return {"not": "an array"}
    if isinstance(value, dict):
        return ["not", "an object"]
    return None


def break_required(
    document: dict[str, Any], schema: dict[str, Any], value: Any
) -> tuple[Any, str] | None:
    """Retype the first non-null required top-level field; None when there is none."""
    governing = _branch(document, schema, value)
    if not isinstance(value, dict):
        return None
    for key in governing.get("required", []):
        if key in value and value[key] is not None:
            broken = copy.deepcopy(value)
            broken[key] = _retyped(value[key])
            return broken, f"$.{key}"
    return None


def _response_schema(document: dict[str, Any], path: str, method: str, status: str) -> Any:
    operation = document["paths"][path][method.lower()]
    response = _resolve(document, operation["responses"][status])
    return operation.get("operationId"), response["content"]["application/json"]["schema"]


def build_fixtures(document: dict[str, Any]) -> dict[str, Any]:
    extractor = _load_extractor()
    fixtures: list[dict[str, Any]] = []
    for example in extractor.extract_examples(document)["examples"]:
        location = example["location"]
        if not location.startswith("response:2") or example["media_type"] != "application/json":
            continue
        status = location.split(":", 1)[1]
        operation_id, schema = _response_schema(
            document, example["path"], example["method"], status
        )
        current = example["value"]
        added: list[str] = []
        additive = add_probes(document, schema, current, "$", added)
        broken = break_required(document, schema, current)
        entry: dict[str, Any] = {
            "operation_id": operation_id,
            "method": example["method"],
            "path": example["path"],
            "status": int(status),
            "example": example["name"],
            "current": current,
            "with_additive_fields": additive,
            "additive_paths": added,
        }
        if broken is not None:
            entry["required_type_broken"] = broken[0]
            entry["broken_path"] = broken[1]
        fixtures.append(entry)
    return {
        "fixture_format": FIXTURE_FORMAT,
        "contract_version": str(document["info"]["version"]),
        "probe_field": PROBE_FIELD,
        "rules": RULES,
        "fixtures": fixtures,
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(build_fixtures(document), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    args = parser.parse_args(argv)
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    text = render(document)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(
                f"{OUTPUT.relative_to(ROOT)} is stale: run scripts/generate_response_fixtures.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} matches the contract")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
