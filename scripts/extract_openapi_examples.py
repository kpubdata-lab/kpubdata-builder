"""Extract named media-type examples from OpenAPI that Studio consumes as JSON."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONTRACT = Path(__file__).parents[1] / "contract" / "builder-api.yaml"
HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})


def resolve_local_ref(document: dict[str, Any], value: Any) -> Any:
    """If entire mapping is local ``$ref``, recursively return actual value."""
    seen: set[str] = set()
    while isinstance(value, dict) and set(value) == {"$ref"}:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError(f"unsupported example reference: {ref!r}")
        if ref in seen:
            raise ValueError(f"cyclic local reference: {ref}")
        seen.add(ref)
        node: Any = document
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"unresolved local reference: {ref}")
            node = node[part]
        value = node
    return value


def _media_examples(
    document: dict[str, Any], path: str, method: str, location: str, content: Any
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not isinstance(content, dict):
        return records
    for media_type, media in content.items():
        if not isinstance(media, dict):
            continue
        examples = media.get("examples", {})
        if not isinstance(examples, dict):
            continue
        for name, raw_example in examples.items():
            example = resolve_local_ref(document, raw_example)
            if not isinstance(example, dict) or "value" not in example:
                raise ValueError(f"{method} {path} {location} example {name!r} has no value")
            records.append(
                {
                    "path": path,
                    "method": method.upper(),
                    "location": location,
                    "media_type": media_type,
                    "name": name,
                    "summary": example.get("summary"),
                    "value": example["value"],
                }
            )
    return records


def extract_examples(document: dict[str, Any]) -> dict[str, Any]:
    """Extract all request/response named examples preserving path order."""
    records: list[dict[str, Any]] = []
    paths = document.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI paths must be a mapping")
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            request_body = resolve_local_ref(document, operation.get("requestBody"))
            if isinstance(request_body, dict):
                records.extend(
                    _media_examples(document, path, method, "request", request_body.get("content"))
                )
            responses = operation.get("responses", {})
            if not isinstance(responses, dict):
                continue
            for status, raw_response in responses.items():
                response = resolve_local_ref(document, raw_response)
                if isinstance(response, dict):
                    records.extend(
                        _media_examples(
                            document,
                            path,
                            method,
                            f"response:{status}",
                            response.get("content"),
                        )
                    )
    return {"contract_version": str(document["info"]["version"]), "examples": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()
    document = yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("OpenAPI document must be a mapping")
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(extract_examples(document), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
