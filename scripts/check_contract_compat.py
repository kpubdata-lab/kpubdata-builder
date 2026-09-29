#!/usr/bin/env python3
"""Refuse a contract change that breaks clients without saying so (#693).

The service contract tests ask whether `contract/builder-api.yaml` and the code agree.
They cannot tell a deliberate break from an additive change, because both sides are
edited together in the same pull request. This compares the contract with the one on
the base branch and asks two things:

1. **Was the version raised?** Any change outside ``info`` must raise ``info.version``.
   A client pins a version; two different contracts under one number cannot be pinned.
   Prose and samples (``description``, ``summary``, ``example(s)``, ``x-*``) are exempt:
   they do not change what goes over the wire.
2. **Is anything removed or retyped?** Those break a client that relied on them:

   - an operation (path + method), or a response status code of one
   - a response media type
   - a schema property, or a whole component schema
   - a ``type`` (or ``$ref`` target) of a schema or parameter
   - an ``enum`` value
   - a parameter that became required, or a new required parameter

   A break passes only with a **major** version raise. That raise is the explicit
   approval: it cannot happen by accident, it is visible in review, and it tells every
   consumer the same thing the check found.

Additive changes — new operations, codes, optional properties, enum values — need only
a minor or patch raise.

Known limits, stated rather than hidden: ``allOf``/``oneOf``/``anyOf`` branches are not
compared member by member, and a property newly added to ``required`` is not flagged,
because it breaks a request but not a response and the schema does not say which it is.

Usage:
    python scripts/check_contract_compat.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = Path("contract/builder-api.yaml")
_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Document = Mapping[str, Any]


def _version(document: Document) -> tuple[int, int, int]:
    raw = str(document.get("info", {}).get("version", ""))
    match = _VERSION.match(raw)
    if match is None:
        raise ValueError(f"info.version {raw!r} is not MAJOR.MINOR.PATCH")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _types(schema: Mapping[str, Any]) -> frozenset[str] | None:
    """The schema's type as a set, folding `nullable` and `[x, "null"]` together."""
    raw = schema.get("type")
    if raw is None:
        return None
    types = set(raw) if isinstance(raw, list) else {raw}
    if schema.get("nullable") is True:
        types.add("null")
    return frozenset(str(t) for t in types)


def _compare_schema(base: Any, head: Any, where: str, breaks: list[str]) -> None:
    if not isinstance(base, Mapping) or not isinstance(head, Mapping):
        return
    if "$ref" in base or "$ref" in head:
        if base.get("$ref") != head.get("$ref"):
            breaks.append(f"{where}: $ref {base.get('$ref')} -> {head.get('$ref')}")
        return
    base_types, head_types = _types(base), _types(head)
    if base_types is not None and head_types is not None and not base_types <= head_types:
        breaks.append(f"{where}: type {sorted(base_types)} -> {sorted(head_types)}")
    if "enum" in base and "enum" in head:
        removed = [value for value in base["enum"] if value not in head["enum"]]
        if removed:
            breaks.append(f"{where}: enum values removed {removed}")
    base_props = base.get("properties") or {}
    head_props = head.get("properties") or {}
    for name in sorted(base_props):
        if name not in head_props:
            breaks.append(f"{where}.{name}: property removed")
        else:
            _compare_schema(base_props[name], head_props[name], f"{where}.{name}", breaks)
    if "items" in base and "items" in head:
        _compare_schema(base["items"], head["items"], f"{where}[]", breaks)


def _operations(document: Document) -> dict[tuple[str, str], Mapping[str, Any]]:
    found: dict[tuple[str, str], Mapping[str, Any]] = {}
    for path, item in (document.get("paths") or {}).items():
        for method in _METHODS:
            if isinstance(item, Mapping) and method in item:
                found[(path, method)] = item[method]
    return found


def _parameters(operation: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (str(p.get("in")), str(p.get("name"))): p
        for p in operation.get("parameters") or []
        if isinstance(p, Mapping) and "name" in p
    }


def breaking_changes(base: Document, head: Document) -> list[str]:
    """Every change from ``base`` to ``head`` that can break an existing client."""
    breaks: list[str] = []
    base_ops, head_ops = _operations(base), _operations(head)
    for key in sorted(base_ops):
        path, method = key
        where = f"{method.upper()} {path}"
        if key not in head_ops:
            breaks.append(f"{where}: operation removed")
            continue
        old, new = base_ops[key], head_ops[key]
        old_responses = old.get("responses") or {}
        new_responses = new.get("responses") or {}
        for code in sorted(old_responses, key=str):
            if code not in new_responses:
                breaks.append(f"{where}: response {code} removed")
                continue
            old_content = (old_responses[code] or {}).get("content") or {}
            new_content = (new_responses[code] or {}).get("content") or {}
            for media in sorted(old_content):
                if media not in new_content:
                    breaks.append(f"{where} {code}: media type {media} removed")
                else:
                    _compare_schema(
                        old_content[media].get("schema"),
                        new_content[media].get("schema"),
                        f"{where} {code}",
                        breaks,
                    )
        old_body = ((old.get("requestBody") or {}).get("content")) or {}
        new_body = ((new.get("requestBody") or {}).get("content")) or {}
        for media in sorted(set(old_body) & set(new_body)):
            _compare_schema(
                old_body[media].get("schema"),
                new_body[media].get("schema"),
                f"{where} request",
                breaks,
            )
        old_params, new_params = _parameters(old), _parameters(new)
        for pkey, param in sorted(new_params.items()):
            was = old_params.get(pkey)
            if param.get("required") and (was is None or not was.get("required")):
                breaks.append(f"{where}: parameter {pkey[1]} ({pkey[0]}) is now required")
            if was is not None:
                _compare_schema(
                    was.get("schema"),
                    param.get("schema"),
                    f"{where} parameter {pkey[1]}",
                    breaks,
                )

    base_schemas = (base.get("components") or {}).get("schemas") or {}
    head_schemas = (head.get("components") or {}).get("schemas") or {}
    for name in sorted(base_schemas):
        where = f"#/components/schemas/{name}"
        if name not in head_schemas:
            breaks.append(f"{where}: schema removed")
        else:
            _compare_schema(base_schemas[name], head_schemas[name], where, breaks)
    return breaks


# Prose and samples do not change what a client may send or will receive, so editing
# them alone needs no version raise. Everything else does.
_NON_NORMATIVE = frozenset({"description", "summary", "example", "examples", "externalDocs"})


def _normative(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _normative(item)
            for key, item in value.items()
            if key not in _NON_NORMATIVE and not str(key).startswith("x-")
        }
    if isinstance(value, list):
        return [_normative(item) for item in value]
    return value


def check(base: Document, head: Document) -> list[str]:
    """Problems that should fail the build; empty when the change is acceptable."""
    strip = [lambda d: {k: v for k, v in d.items() if k != "info"}, _normative]
    old, new = base, head
    for step in strip:
        old, new = step(old), step(new)
    if old == new:
        return []
    base_version, head_version = _version(base), _version(head)
    shown = ".".join(map(str, head_version))
    problems: list[str] = []
    if head_version <= base_version:
        problems.append(
            f"the contract changed but info.version stayed {shown} "
            f"(base {'.'.join(map(str, base_version))}); raise it and keep "
            "API_CONTRACT_VERSION in step"
        )
    breaks = breaking_changes(base, head)
    if breaks and head_version[0] <= base_version[0]:
        problems.append(
            "the change breaks existing clients, which needs a MAJOR version raise "
            f"({base_version[0]}.x -> {base_version[0] + 1}.0.0) — that raise is the "
            "explicit approval. Breaking changes:\n  - " + "\n  - ".join(breaks)
        )
    return problems


def _load_at(ref: str) -> Document | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{CONTRACT.as_posix()}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return cast(Document, yaml.safe_load(result.stdout))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="Git ref to compare against.")
    args = parser.parse_args(argv)

    base = _load_at(args.base)
    if base is None:
        print(f"{CONTRACT} does not exist at {args.base}; nothing to compare")
        return 0
    head = cast(Document, yaml.safe_load((REPO_ROOT / CONTRACT).read_text(encoding="utf-8")))
    problems = check(base, head)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    print(
        f"contract compatible with {args.base}: "
        f"{base['info']['version']} -> {head['info']['version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
