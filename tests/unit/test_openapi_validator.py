"""Unit tests for ``tests/unit/_openapi.py`` pure Python validator (#209, ADR-0005).

Prove validator itself catches drift (missing required fields, type changes).
Conformance gate's value depends on validator "passing valid responses, failing regressed
ones", so we explicitly lock pass/fail boundaries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ._openapi import resolve_ref, response_schema, validate

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))


# --- type / required / enum / minimum boundaries -----------------------------------------------


def test_accepts_object_with_all_required_fields() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status", "api_version"],
        "properties": {"status": {"type": "string"}, "api_version": {"type": "string"}},
    }
    assert validate({"status": "valid", "api_version": "1.0.0"}, schema, {}) == []


def test_rejects_missing_required_field() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status", "api_version"],
        "properties": {"status": {"type": "string"}, "api_version": {"type": "string"}},
    }
    errors = validate({"status": "valid"}, schema, {})
    assert any("api_version" in e for e in errors)


def test_rejects_wrong_value_type() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "integer"}}}
    errors = validate({"n": "not-an-int"}, schema, {})
    assert errors and any("integer" in e for e in errors)


def test_integer_does_not_accept_bool() -> None:
    # bool is subtype of int but JSON integer is not.
    schema: dict[str, Any] = {"type": "integer"}
    assert validate(True, schema, {})  # True/False rejected as integer


def test_nullable_union_accepts_string_and_null() -> None:
    schema: dict[str, Any] = {"type": ["string", "null"]}
    assert validate(None, schema, {}) == []
    assert validate("ok", schema, {}) == []
    assert validate(7, schema, {})  # integer rejected


def test_enum_rejects_out_of_range_value() -> None:
    schema: dict[str, Any] = {"type": "string", "enum": ["ok", "failed"]}
    assert validate("ok", schema, {}) == []
    assert validate("pending", schema, {})  # out of enum


def test_minimum_rejects_below_bound() -> None:
    schema: dict[str, Any] = {"type": "integer", "minimum": 1}
    assert validate(1, schema, {}) == []
    assert validate(0, schema, {})


def test_not_rejects_object_matching_required_property_schema() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "not": {"required": ["removed"]},
    }
    assert validate({}, schema, {}) == []
    assert validate({"removed": True}, schema, {})


def test_allof_applies_each_forbidden_property_schema() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "allOf": [
            {"not": {"required": ["first"]}},
            {"not": {"required": ["second"]}},
        ],
    }
    assert validate({}, schema, {}) == []
    assert validate({"first": True}, schema, {})
    assert validate({"second": True}, schema, {})


def test_min_items_rejects_short_array() -> None:
    schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "items": {"type": "integer"},
    }
    assert validate([1], schema, {}) == []
    assert validate([], schema, {})


# --- structural keywords: $ref / oneOf / additionalProperties / items -----------


def test_ref_resolves_local_schema() -> None:
    contract: dict[str, Any] = {
        "components": {"schemas": {"Foo": {"type": "object", "required": ["x"]}}}
    }
    assert validate({"x": 1}, {"$ref": "#/components/schemas/Foo"}, contract) == []
    assert validate({}, {"$ref": "#/components/schemas/Foo"}, contract)  # x missing


def test_ref_raises_on_unresolved_target() -> None:
    with pytest.raises(ValueError):
        resolve_ref({"components": {"schemas": {}}}, "#/components/schemas/Missing")


def test_oneof_accepts_when_at_least_one_branch_matches() -> None:
    contract: dict[str, Any] = {
        "components": {
            "schemas": {
                "Error": {"type": "object", "required": ["error"]},
                "ValidationError": {"type": "object", "required": ["status"]},
            }
        }
    }
    schema: dict[str, Any] = {
        "oneOf": [
            {"$ref": "#/components/schemas/Error"},
            {"$ref": "#/components/schemas/ValidationError"},
        ]
    }
    # Error shape and ValidationError shape each fit one branch.
    assert validate({"error": "boom"}, schema, contract) == []
    assert validate({"status": "invalid"}, schema, contract) == []
    # If neither, fail.
    assert validate({"unexpected": 1}, schema, contract)


def test_additional_properties_schema_validates_extras() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": {"type": "string"},
    }
    assert validate({"known": "a", "extra": "b"}, schema, {}) == []
    # Additional properties must be string if not, reject.
    assert validate({"known": "a", "extra": 5}, schema, {})


def test_additional_properties_false_rejects_extras() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": False,
    }
    assert validate({"known": "a"}, schema, {}) == []
    assert validate({"known": "a", "extra": "b"}, schema, {})


def test_additional_properties_omitted_allows_extras() -> None:
    # Additive change with new optional fields in response passes (forward compatible).
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}},
    }
    assert validate({"status": "ok", "future_field": 123}, schema, {}) == []


def test_array_items_are_each_validated() -> None:
    schema: dict[str, Any] = {"type": "array", "items": {"type": "integer"}}
    assert validate([1, 2, 3], schema, {}) == []
    errors = validate([1, "x", 3], schema, {})
    assert any("$[1]" in e for e in errors)


# --- response_schema lookup / path normalization --------------------------------


def test_response_schema_returns_declared_schema() -> None:
    contract = _contract()
    schema = response_schema(contract, "/version", "GET", 200)
    assert schema is not None
    assert validate({"service": "kpubdata-builder", "api_version": "1.0.0"}, schema, contract) == []


def test_response_schema_none_for_undeclared_status() -> None:
    contract = _contract()
    assert response_schema(contract, "/version", "GET", 500) is None


def test_response_schema_normalizes_path_template() -> None:
    # Query /artifacts/{run_id} template with concrete run_id.
    contract = _contract()
    assert response_schema(contract, "/artifacts/any-run-id", "GET", 200) is not None
    assert response_schema(contract, "/artifacts/any-run-id", "GET", 404) is not None


# --- gate value proof: catch regression with actual contract schema -----------


def test_gate_catches_required_field_drift() -> None:
    """gate catches regression where app.py omits run_id from BuildSuccessResponse."""
    contract = _contract()
    schema = response_schema(contract, "/build", "POST", 200)
    assert schema is not None
    good = {
        "status": "ok",
        "run_id": "r1",
        "outcomes": [],
        "manifest": "/p/manifest.json",
        "api_version": "1.0.0",
    }
    assert validate(good, schema, contract) == []
    drifted = dict(good)
    del drifted["run_id"]
    assert validate(drifted, schema, contract)


def test_gate_catches_type_drift() -> None:
    """gate also catches regression where api_version changes from string to integer."""
    contract = _contract()
    schema = response_schema(contract, "/version", "GET", 200)
    assert schema is not None
    assert validate({"service": "kpubdata-builder", "api_version": 1}, schema, contract)
