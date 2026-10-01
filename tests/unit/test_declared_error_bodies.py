"""The error bodies the contract declares are the ones Builder sends (#947, #951).

`revision_conflict` (409, with `current_revision`) and `signup_pending` /
`signup_rejected` (403) were once only named in prose, so a client read them from a
sentence. They are now schemas — `RevisionConflictError` and `SignupNotApprovedError`
behind the shared `SignupNotApproved` response. These tests run the real routes and
check each body against what the contract declares, both ways:

- the body validates against the declared schema (a renamed or retyped field, or a code
  the schema does not list, fails);
- the body has exactly the keys of the contract's named example, so the example a client
  drift test reads (`contract/fixtures/responses.json`, `error_fixtures`) is the shape
  Builder sends;
- every sign-up code the ledger can answer is in the schema's enum and in the prose a
  reader starts from (`Error.code`, `bearerAuth`), and the enum lists no code the ledger
  never sends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.user_ledger import LedgerEntry, admission_refusal
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_ROOT = Path(__file__).parents[2]
_CONTRACT: dict[str, Any] = yaml.safe_load(
    (_ROOT / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)
_ALICE = Principal("oidc", "alice", "oidc:alice", admitted=True)
_YAML = "dataset_id: a\ntitle: A\n"
_ISSUER = "https://idp.example"


def _newcomer() -> Principal:
    return Principal(
        kind="oidc",
        identifier="newcomer",
        owner_id=compute_owner_id("oidc", _ISSUER, "newcomer"),
        admitted=False,
        display_name="newcomer@example.com",
    )


def _admin() -> Principal:
    return Principal(
        kind="oidc",
        identifier="admin",
        owner_id=compute_owner_id("oidc", _ISSUER, "admin"),
        is_admin=True,
        admitted=True,
    )


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: object())


def _call(
    service: BuilderService,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, JsonValue] | None = None,
    principal: Principal = _ALICE,
) -> ServiceResponse:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)
    response = dispatch(service, method, path, body, "")
    assert isinstance(response, ServiceResponse)
    return response


def _body(response: ServiceResponse) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], response.body)


def _example(response: dict[str, Any], name: str) -> dict[str, Any]:
    return cast(dict[str, Any], response["content"]["application/json"]["examples"][name]["value"])


def _operation_response(path: str, method: str, status: int) -> dict[str, Any]:
    return cast(dict[str, Any], _CONTRACT["paths"][path][method]["responses"][str(status)])


_SIGNUP = cast(dict[str, Any], _CONTRACT["components"]["responses"]["SignupNotApproved"])
_SIGNUP_SCHEMA = cast(dict[str, Any], _SIGNUP["content"]["application/json"]["schema"])


# ----------------------------------------------------------------- revision_conflict


def _conflicts(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> dict[str, ServiceResponse]:
    """A stale save and a stale revert of a document at revision 2."""
    for expected, text in ((0, _YAML), (1, _YAML + "# 2\n")):
        saved = _call(
            service,
            monkeypatch,
            "PUT",
            "/revisions/spec/my-spec",
            {"content": {"yaml": text}, "expected_revision": expected},
        )
        assert saved.status_code == 200
    save = _call(
        service,
        monkeypatch,
        "PUT",
        "/revisions/spec/my-spec",
        {"content": {"yaml": _YAML + "# stale\n"}, "expected_revision": 1},
    )
    revert = _call(
        service,
        monkeypatch,
        "POST",
        "/revisions/spec/my-spec/revert",
        {"to_revision": 1, "expected_revision": 1},
    )
    return {"put": save, "post": revert}


_CONFLICT_ROUTES = (
    ("put", "/revisions/{kind}/{doc_id}", "/revisions/spec/my-spec"),
    ("post", "/revisions/{kind}/{doc_id}/revert", "/revisions/spec/my-spec/revert"),
)


@pytest.mark.parametrize(("method", "template", "concrete"), _CONFLICT_ROUTES)
def test_a_revision_conflict_is_the_declared_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    template: str,
    concrete: str,
) -> None:
    response = _conflicts(_service(tmp_path), monkeypatch)[method]
    schema = response_schema(_CONTRACT, concrete, method, 409)

    assert response.status_code == 409
    assert schema == {"$ref": "#/components/schemas/RevisionConflictError"}
    assert validate(response.body, schema, _CONTRACT) == []
    assert _body(response)["current_revision"] == 2
    example = _example(_operation_response(template, method, 409), "RevisionConflict")
    assert set(_body(response)) == set(example)
    assert _body(response)["code"] == example["code"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda b: b.pop("current_revision"), "missing required property 'current_revision'"),
        (lambda b: b.update(current_revision="2"), "$.current_revision: expected type"),
        (lambda b: b.update(code="conflict"), "'conflict' not in enum ['revision_conflict']"),
    ],
    ids=["field-renamed-away", "field-retyped", "code-renamed"],
)
def test_a_diverging_revision_conflict_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Any, expected: str
) -> None:
    """Negative: the check above fails when the implementation and contract part ways."""
    body = dict(_body(_conflicts(_service(tmp_path), monkeypatch)["put"]))
    mutate(body)

    errors = validate(body, {"$ref": "#/components/schemas/RevisionConflictError"}, _CONTRACT)

    assert any(expected in error for error in errors), errors


# ----------------------------------------------------------------- signup ledger


def _refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, ServiceResponse]:
    """A pending user's and then, once rejected, the same user's 403, from real routes."""
    service = _service(tmp_path)
    newcomer = _newcomer()
    pending = _call(service, monkeypatch, "GET", "/builds", principal=newcomer)
    decided = _call(
        service,
        monkeypatch,
        "POST",
        f"/admin/users/{newcomer.owner_id}/reject",
        principal=_admin(),
    )
    assert decided.status_code == 200
    # An operation that declares its own 403 answers the shared one first.
    rejected = _call(
        service,
        monkeypatch,
        "PUT",
        "/revisions/spec/my-spec",
        {"content": {"yaml": _YAML}, "expected_revision": 0},
        principal=newcomer,
    )
    return {"SignupPending": pending, "SignupRejected": rejected}


@pytest.mark.parametrize("example", ["SignupPending", "SignupRejected"])
def test_a_signup_refusal_is_the_declared_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, example: str
) -> None:
    response = _refusals(tmp_path, monkeypatch)[example]
    declared = _example(_SIGNUP, example)

    assert response.status_code == _SIGNUP["x-status"] == 403
    assert validate(response.body, _SIGNUP_SCHEMA, _CONTRACT) == []
    assert _body(response) == declared


def _ledger_codes() -> set[str]:
    """Every code `admission_refusal` can answer, over every status a user can have."""
    codes: set[str] = set()
    for status in ("pending", "approved", "rejected"):
        entry = LedgerEntry("u", None, status, "t", "t", None, None)
        refusal = admission_refusal(entry, _newcomer())
        if refusal is not None:
            codes.add(cast(str, refusal["code"]))
    return codes


def test_the_contract_lists_exactly_the_ledgers_codes() -> None:
    schema = _CONTRACT["components"]["schemas"]["SignupNotApprovedError"]
    (extension,) = [part for part in schema["allOf"] if "$ref" not in part]
    declared = set(extension["properties"]["code"]["enum"])
    error_code = _CONTRACT["components"]["schemas"]["Error"]["properties"]["code"]["description"]
    auth = _CONTRACT["components"]["securitySchemes"]["bearerAuth"]["description"]

    assert _ledger_codes() == declared == {"signup_pending", "signup_rejected"}
    for code in declared:
        assert f"`{code}`" in error_code
        assert f"`{code}`" in auth
    assert "SignupNotApproved" in auth


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda b: b.update(code="signup_waiting"), "'signup_waiting' not in enum"),
        (lambda b: b.pop("code"), "missing required property 'code'"),
        (lambda b: b.pop("error"), "missing required property 'error'"),
    ],
    ids=["code-renamed", "code-dropped", "message-dropped"],
)
def test_a_diverging_signup_refusal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Any, expected: str
) -> None:
    """Negative: the check above fails when the implementation and contract part ways."""
    body = dict(_body(_refusals(tmp_path, monkeypatch)["SignupPending"]))
    mutate(body)

    errors = validate(body, _SIGNUP_SCHEMA, _CONTRACT)

    assert any(expected in error for error in errors), errors
