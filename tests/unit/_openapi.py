"""Pure Python OpenAPI 3.1 JSON Schema subset validator (#209, ADR-0005).

Without external dependencies (`openapi-core`, `jsonschema`), validates response
schema subset of `contract/builder-api.yaml`. Adheres to ADR-0005's "no external
dependencies, stdlib-deterministic" principle while resolving unresolved question #1
(whether to do schema matching in pure Python lightweight vs. introduce validation
library) in the "pure Python lightweight" direction.

Static contract verification (path/status code/operationId matching — #317, #319)
checks whether YAML's *declaration* matches `dispatch`'s *routing list*. This
validator goes one step further: validates whether **actual dispatch response body
conforms to declared schema** (wire-level conformance). If app.py removes required
fields or changes types in response but doesn't update YAML, static checks miss it
but this check catches it — this is the drift reality that ADR-0005 aims to prevent.

Supported keywords (subset used by this contract):
    - ``$ref`` (local references only in form ``#/components/schemas/Name``)
    - ``type`` (single string or union with null: ``[string, "null"]``)
    - ``required``, ``properties``
    - ``additionalProperties`` (``true`` | ``false`` | schema)
    - ``items`` (array)
    - ``enum``
    - ``oneOf`` (valid if at least one branch passes — lenient interpretation for
      conformance gate to catch actual drift; prioritizes avoiding false positives
      even if multiple branches match)
    - ``not``, ``allOf``
    - ``minimum`` (integer/number lower bound), ``minItems`` (array min length)

Unknown keywords are ignored (forward compatibility). Additional optional fields
allowed by default, so *additive* changes (new optional fields in response) pass,
but *structural regressions* (missing required fields, type changes) fail.
"""

from __future__ import annotations

from typing import Any

# OpenAPI/JSON Schema documents and JSON values are both arbitrary nested
# structures. Since this is a test helper module, uses Any instead of
# precise recursive aliases (mypy only checks src/, so no impact).
Schema = dict[str, Any]
Json = Any


def resolve_ref(contract: Schema, ref: str) -> Schema:
    """Resolve local $ref in form ``#/components/schemas/Name`` to actual schema.

    External and remote references are not used in this contract, so reject them.
    """
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref (only local '#/' allowed): {ref}")
    node: Any = contract
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"unresolved $ref: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"$ref target is not a mapping: {ref}")
    return node


def _matches_type(value: Json, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        # bool is subtype of int but not JSON integer, so excluded.
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _type_names(schema: Schema) -> list[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def validate(
    value: Json,
    schema: Schema,
    contract: Schema,
    path: str = "$",
    *,
    allow_additional: bool = False,
) -> list[str]:
    """Validate ``value`` satisfies ``schema`` and return list of violations.

    Empty list means valid. Each violation is human-readable string. ``path``
    (default ``$``) indicates location in JSON document.

    ``allow_additional`` reads the contract the way a client should (#814): an
    undeclared property is ignored even where the schema says
    ``additionalProperties: false``. Required properties and types are still checked.
    """
    # If $ref present, not used with other keywords (OpenAPI spec), so interpret and delegate.
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return validate(
            value, resolve_ref(contract, ref), contract, path, allow_additional=allow_additional
        )

    errors: list[str] = []

    # oneOf: valid if at least one branch passes (lenient interpretation —
    # see module docstring for intent).
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        passing = [
            branch
            for branch in one_of
            if isinstance(branch, dict)
            and not validate(value, branch, contract, path, allow_additional=allow_additional)
        ]
        if not passing:
            errors.append(f"{path}: value matched no oneOf branch")
        return errors

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, dict):
                errors.extend(
                    validate(value, branch, contract, path, allow_additional=allow_additional)
                )

    not_schema = schema.get("not")
    if isinstance(not_schema, dict) and not validate(value, not_schema, contract, path):
        errors.append(f"{path}: value matched forbidden 'not' schema")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']!r}")

    type_names = _type_names(schema)
    if type_names and not any(_matches_type(value, name) for name in type_names):
        errors.append(f"{path}: expected type {type_names}, got {type(value).__name__}")
        # If types differ, no point checking deeper structures.
        return errors

    object_keywords = {"required", "properties", "additionalProperties"}
    if isinstance(value, dict) and (
        "object" in type_names or any(keyword in schema for keyword in object_keywords)
    ):
        errors.extend(_validate_object(value, schema, contract, path, allow_additional))
    elif "array" in type_names and isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: array length {len(value)} < minItems {schema['minItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    validate(
                        item,
                        item_schema,
                        contract,
                        f"{path}[{index}]",
                        allow_additional=allow_additional,
                    )
                )

    if (
        "minimum" in schema
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value < schema["minimum"]
    ):
        errors.append(f"{path}: {value} < minimum {schema['minimum']}")

    return errors


def _validate_object(
    value: dict[str, Json],
    schema: Schema,
    contract: Schema,
    path: str,
    allow_additional: bool = False,
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    for key in schema.get("required", []):
        if key not in value:
            errors.append(f"{path}: missing required property {key!r}")

    for key, sub in value.items():
        child_path = f"{path}.{key}"
        prop_schema = properties.get(key)
        if isinstance(prop_schema, dict):
            errors.extend(
                validate(sub, prop_schema, contract, child_path, allow_additional=allow_additional)
            )
            continue
        # Undeclared property → judged by additionalProperties policy.
        addl = schema.get("additionalProperties", True)
        if addl is False and not allow_additional:
            errors.append(f"{child_path}: extra property not allowed")
        elif isinstance(addl, dict):
            errors.extend(
                validate(sub, addl, contract, child_path, allow_additional=allow_additional)
            )
        # True / omitted → allow additional properties (allow incidental variations).
    return errors


def _normalize_path(contract: Schema, path: str) -> str:
    """Matches concrete path (e.g., ``/artifacts/run-1``) to template (``/artifacts/{run_id}``)."""
    paths = contract.get("paths")
    if not isinstance(paths, dict):
        return path
    if path in paths:
        return path
    path_parts = path.split("/")
    for template in paths:
        tmpl_parts = template.split("/")
        if len(tmpl_parts) != len(path_parts):
            continue
        # Equal length guaranteed by continue above, so strict=True is safe.
        if all(
            tp.startswith("{") or tp == pp for tp, pp in zip(tmpl_parts, path_parts, strict=True)
        ):
            return template
    return path


def response_schema(contract: Schema, path: str, method: str, status_code: int) -> Schema | None:
    """Return response JSON schema for ``(path, method, status_code)``.

    Handles path template normalization and ``content/application/json``
    extraction. Returns ``None`` if status code is not declared in contract.
    """
    paths = contract.get("paths")
    if not isinstance(paths, dict):
        return None
    norm = _normalize_path(contract, path)
    operation = paths.get(norm)
    if not isinstance(operation, dict):
        return None
    responses = operation.get(method.lower())
    if not isinstance(responses, dict):
        return None
    response = responses.get("responses", {}).get(str(status_code))
    if not isinstance(response, dict):
        return None
    content = response.get("content", {})
    app_json = content.get("application/json")
    schema = app_json.get("schema") if isinstance(app_json, dict) else None
    return schema if isinstance(schema, dict) else None
