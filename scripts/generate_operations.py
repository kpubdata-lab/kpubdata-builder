#!/usr/bin/env python3
"""Write the table of operations the service routes, from the contract (#1109).

`contract/builder-api.yaml` says which method and path is which `operationId`, and on
which operations a request may carry a provider key or a publish credential. The service
kept its own copies: a test held a hand-written `(path, method) -> operationId` table to
the contract, and `route_reads_provider_keys` / `route_reads_publish_credentials` each
spelled the paths out again, with another test holding them to the contract. A route
changed in one place and not the others was caught only if someone had remembered to
extend the right table.

The contract is not shipped in the wheel, so the service cannot read it when it runs.
This writes what the service needs of it into `src/kpubdata_builder/service/
_contract_operations.py`, a module nobody edits:

    method, path          as the contract declares them
    operation_id          the contract's `operationId`
    provider_key          the operation declares the `X-Provider-Key` header
    publish_credential    the operation declares the `X-Publish-Credential` header
    authenticated         False where the contract says `security: []`

The unit tests regenerate it and fail when the committed file differs, so it cannot
drift from the contract — the same arrangement as `generate_response_fixtures.py`.

Usage:
    python scripts/generate_operations.py          # rewrite the module
    python scripts/generate_operations.py --check  # exit 1 when it is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract" / "builder-api.yaml"
OUTPUT = ROOT / "src" / "kpubdata_builder" / "service" / "_contract_operations.py"

_METHODS = ("get", "post", "put", "patch", "delete")
#: The shared header parameters whose presence on an operation the service acts on.
_PROVIDER_KEY_REF = "#/components/parameters/ProviderKey"
_PUBLISH_CREDENTIAL_REF = "#/components/parameters/PublishCredential"


def _parameter_refs(path_item: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    """The `$ref`s of the parameters an operation has, its path's included."""
    declared = [*(path_item.get("parameters") or []), *(operation.get("parameters") or [])]
    return {item["$ref"] for item in declared if isinstance(item, dict) and "$ref" in item}


def operations(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every operation the contract declares, in the contract's own order.

    Raises:
        ValueError: An operation has no `operationId`, or two share one.
    """
    found: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for path, path_item in document["paths"].items():
        for method in _METHODS:
            operation = path_item.get(method)
            # `x-planned: true` marks an operation the contract describes ahead of the
            # service; it is not one the service routes.
            if operation is None or operation.get("x-planned") is True:
                continue
            where = f"{method.upper()} {path}"
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"{where} has no operationId")
            if operation_id in seen:
                raise ValueError(
                    f"{where} and {seen[operation_id]} share operationId {operation_id}"
                )
            seen[operation_id] = where
            refs = _parameter_refs(path_item, operation)
            found.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation_id,
                    "provider_key": _PROVIDER_KEY_REF in refs,
                    "publish_credential": _PUBLISH_CREDENTIAL_REF in refs,
                    "authenticated": operation.get("security", None) != [],
                }
            )
    return found


def render(document: dict[str, Any]) -> str:
    version = document["info"]["version"]
    lines = [
        '"""The operations of the service contract, generated from it (#1109).',
        "",
        "Do not edit. `scripts/generate_operations.py` writes this module from",
        "`contract/builder-api.yaml`, and the unit tests fail when it is stale. Read it",
        "through `kpubdata_builder.service.operations`.",
        '"""',
        "",
        "# One operation per line, whatever its length: the table is read as a table.",
        "# ruff: noqa: E501",
        "# fmt: off",
        "",
        "from __future__ import annotations",
        "",
        "#: The contract version this table was generated from.",
        f'CONTRACT_VERSION = "{version}"',
        "",
        "#: (method, path, operation_id, provider_key, publish_credential, authenticated)",
        "OPERATIONS: tuple[tuple[str, str, str, bool, bool, bool], ...] = (",
    ]
    for item in operations(document):
        lines.append(
            f'    ("{item["method"]}", "{item["path"]}", "{item["operation_id"]}", '
            f"{item['provider_key']}, {item['publish_credential']}, {item['authenticated']}),"
        )
    lines += [")", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the module is stale")
    args = parser.parse_args(argv)
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    text = render(document)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(
                f"{OUTPUT.relative_to(ROOT)} is stale: run scripts/generate_operations.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} matches the contract")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
