"""The contract gate has to actually refuse (#693).

The service contract tests ask whether the document and the code agree. They pass a
deliberate break as long as both sides are edited together. This gate compares the
contract with the base branch's. Mostly negative tests: a gate nobody has watched fail
is a gate nobody knows works.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_contract_compat.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_contract_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()

_BASE: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1.4.0", "description": "v1.4.0 adds things."},
    "paths": {
        "/runs/{run_id}": {
            "get": {
                "parameters": [
                    {
                        "name": "run_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Run"}}
                        },
                    },
                    "404": {"description": "missing"},
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Run": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["ok", "failed"]},
                    "rows": {"type": "array", "items": {"type": "integer"}},
                },
            }
        }
    },
}


def _head(version: str = "1.5.0") -> dict[str, Any]:
    head = copy.deepcopy(_BASE)
    head["info"]["version"] = version
    return head


def _schema(head: dict[str, Any]) -> dict[str, Any]:
    return head["components"]["schemas"]["Run"]


def _get(head: dict[str, Any]) -> dict[str, Any]:
    return head["paths"]["/runs/{run_id}"]["get"]


# ------------------------------------------------------------------ what breaks


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda h: h["paths"].pop("/runs/{run_id}"), "operation removed"),
        (lambda h: _get(h)["responses"].pop("404"), "response 404 removed"),
        (
            lambda h: _get(h)["responses"]["200"]["content"].pop("application/json"),
            "media type application/json removed",
        ),
        (lambda h: _schema(h)["properties"].pop("status"), "Run.status: property removed"),
        (
            lambda h: _schema(h)["properties"]["run_id"].update(type="integer"),
            "Run.run_id: type",
        ),
        (
            lambda h: _schema(h)["properties"]["status"].update(enum=["ok"]),
            "enum values removed ['failed']",
        ),
        (lambda h: _schema(h)["properties"]["rows"]["items"].update(type="string"), "Run.rows[]"),
        (lambda h: h["components"]["schemas"].pop("Run"), "schema removed"),
        (lambda h: _get(h)["parameters"][1].update(required=True), "limit (query) is now required"),
        (
            lambda h: _get(h)["parameters"].append(
                {"name": "owner", "in": "query", "required": True, "schema": {"type": "string"}}
            ),
            "owner (query) is now required",
        ),
        (
            lambda h: _get(h)["responses"]["200"]["content"]["application/json"].update(
                schema={"$ref": "#/components/schemas/Other"}
            ),
            "$ref #/components/schemas/Run -> #/components/schemas/Other",
        ),
    ],
)
def test_a_break_under_a_minor_raise_fails(mutate: Any, expected: str) -> None:
    """Negative: each kind of break is caught, and a minor raise does not excuse it."""
    head = _head("1.5.0")
    mutate(head)

    problems = gate.check(_BASE, head)

    assert len(problems) == 1
    assert "MAJOR version raise" in problems[0]
    assert expected in problems[0]


def test_a_major_raise_is_the_explicit_approval() -> None:
    """An intentional break passes — but only when the version says so."""
    head = _head("2.0.0")
    _schema(head)["properties"].pop("status")

    assert gate.check(_BASE, head) == []


# --------------------------------------------------------------- what does not


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h["paths"].update(
            {"/new": {"get": {"responses": {"200": {"description": "x"}}}}}
        ),
        lambda h: _get(h)["responses"].update({"409": {"description": "conflict"}}),
        lambda h: _schema(h)["properties"].update({"note": {"type": "string"}}),
        lambda h: _schema(h)["properties"]["status"].update(enum=["ok", "failed", "cancelled"]),
        lambda h: _schema(h)["properties"]["run_id"].update(type=["string", "null"]),
        lambda h: _get(h)["parameters"].append({"name": "q", "in": "query", "schema": {}}),
    ],
    ids=[
        "new operation",
        "new status",
        "new optional property",
        "new enum value",
        "type widened",
        "new optional parameter",
    ],
)
def test_an_additive_change_needs_only_a_minor_raise(mutate: Any) -> None:
    head = _head("1.5.0")
    mutate(head)

    assert gate.check(_BASE, head) == []


def test_any_normative_change_must_raise_the_version() -> None:
    """Negative: two different contracts under one number cannot be pinned."""
    head = _head("1.4.0")
    _schema(head)["properties"].update({"note": {"type": "string"}})

    (problem,) = gate.check(_BASE, head)

    assert "info.version stayed 1.4.0" in problem


def test_prose_and_samples_need_no_raise() -> None:
    head = _head("1.4.0")
    head["info"]["description"] = "reworded"
    _get(head)["summary"] = "Read one run"
    _schema(head)["properties"]["run_id"]["example"] = "run-1"

    assert gate.check(_BASE, head) == []


def test_a_lowered_version_is_refused() -> None:
    head = _head("1.3.9")
    _schema(head)["properties"].update({"note": {"type": "string"}})

    assert "info.version stayed 1.3.9" in gate.check(_BASE, head)[0]


def test_the_real_contract_is_compatible_with_itself() -> None:
    contract = yaml.safe_load((_ROOT / "contract" / "builder-api.yaml").read_text("utf-8"))

    assert gate.breaking_changes(contract, contract) == []
    assert gate.check(contract, copy.deepcopy(contract)) == []


def test_the_cli_refuses_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _head("1.5.0")
    _schema(head)["properties"].pop("status")
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "builder-api.yaml").write_text(yaml.safe_dump(head), "utf-8")
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_load_at", lambda _ref: _BASE)

    assert gate.main(["--base", "origin/main"]) == 1
    assert "Run.status: property removed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda props: props.pop("description"), "Run.description: property removed"),
        (lambda props: props["description"].update(type="integer"), "Run.description: type"),
        (lambda props: props.pop("summary"), "Run.summary: property removed"),
    ],
    ids=["removed", "retyped", "summary-removed"],
)
def test_a_property_named_like_prose_is_still_a_property(mutate: Any, expected: str) -> None:
    """#791: `description` is a keyword as a schema's text, but a name as a property.

    BuildSpec has a `description` property. Stripping every `description` key made its
    removal or retyping invisible, so the change passed without a version raise.
    """
    base = copy.deepcopy(_BASE)
    _schema(base)["properties"].update(
        {"description": {"type": "string"}, "summary": {"type": "string"}}
    )
    head = copy.deepcopy(base)
    head["info"]["version"] = "1.4.0"
    mutate(_schema(head)["properties"])

    problems = gate.check(base, head)

    assert any("info.version stayed 1.4.0" in p for p in problems)
    assert any(expected in p for p in problems)


def test_prose_on_a_property_is_still_prose() -> None:
    """The fix must not turn a property's own description text into a normative change."""
    base = copy.deepcopy(_BASE)
    _schema(base)["properties"]["run_id"]["description"] = "old text"
    head = copy.deepcopy(base)
    _schema(head)["properties"]["run_id"]["description"] = "new text"

    assert gate.check(base, head) == []
