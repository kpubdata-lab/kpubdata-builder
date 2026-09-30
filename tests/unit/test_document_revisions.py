"""Edited documents are kept as immutable revisions with concurrency checks (#820)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.request_credentials import request_scope
from kpubdata_builder.service.revisions import RevisionConflict, RevisionStore
from kpubdata_builder.spec import JsonValue

_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")
_YAML = "dataset_id: a\ntitle: A\n"


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: object())


def _call(
    service: BuilderService,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, JsonValue] | None = None,
    principal: Principal = _ALICE,
    query: str = "",
) -> ServiceResponse:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)
    response = dispatch(service, method, path, body, query)
    assert isinstance(response, ServiceResponse)
    return response


def _save(
    service: BuilderService,
    monkeypatch: pytest.MonkeyPatch,
    expected: int,
    text: str = _YAML,
    **extra: JsonValue,
) -> ServiceResponse:
    return _call(
        service,
        monkeypatch,
        "PUT",
        "/revisions/spec/my-spec",
        {"content": {"yaml": text}, "expected_revision": expected, **extra},
    )


def test_each_save_is_a_new_revision_with_a_server_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)

    first = _save(service, monkeypatch, 0)
    second = _save(service, monkeypatch, 1, text=_YAML + "description: d\n")

    assert (first.body["revision"], second.body["revision"]) == (1, 2)
    assert second.body["author"] == _ALICE.owner_id
    old = _call(service, monkeypatch, "GET", "/revisions/spec/my-spec", query="revision=1")
    assert cast(dict[str, JsonValue], old.body["content"])["yaml"] == _YAML


def test_a_concurrent_save_is_refused_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: two editors starting from revision 1 — the second is a conflict."""
    service = _service(tmp_path)
    _save(service, monkeypatch, 0)

    winner = _save(service, monkeypatch, 1, text=_YAML + "# alice\n")
    loser = _save(service, monkeypatch, 1, text=_YAML + "# bob\n")

    assert winner.status_code == 200
    assert (loser.status_code, loser.body["code"], loser.body["current_revision"]) == (
        409,
        "revision_conflict",
        2,
    )
    latest = _call(service, monkeypatch, "GET", "/revisions/spec/my-spec")
    assert cast(dict[str, JsonValue], latest.body["content"])["yaml"].endswith("# alice\n")  # type: ignore[union-attr]


def test_racing_threads_make_exactly_one_revision(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "r.sqlite3")
    outcomes: list[str] = []

    def save(n: int) -> None:
        try:
            store.save("ws", "spec", "doc", {"yaml": str(n)}, expected_revision=0, author="a")
            outcomes.append("saved")
        except RevisionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=save, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("saved") == 1
    assert [r.revision for r in store.history("ws", "spec", "doc")] == [1]


def test_a_retried_save_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)

    first = _save(service, monkeypatch, 0, idempotency_key="k1")
    retry = _save(service, monkeypatch, 0, idempotency_key="k1")

    assert first.body == retry.body
    history = _call(service, monkeypatch, "GET", "/revisions/spec/my-spec/history")
    assert len(cast(list[JsonValue], history.body["revisions"])) == 1


def test_revert_makes_a_new_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    _save(service, monkeypatch, 0)
    _save(service, monkeypatch, 1, text="changed: true\n")

    reverted = _call(
        service,
        monkeypatch,
        "POST",
        "/revisions/spec/my-spec/revert",
        {"to_revision": 1, "expected_revision": 2},
    )

    assert (reverted.body["revision"], reverted.body["reverted_from"]) == (3, 1)
    assert cast(dict[str, JsonValue], reverted.body["content"])["yaml"] == _YAML
    history = _call(service, monkeypatch, "GET", "/revisions/spec/my-spec/history")
    assert [r["revision"] for r in cast(list[dict[str, JsonValue]], history.body["revisions"])] == [
        1,
        2,
        3,
    ]
    assert [a["action"] for a in cast(list[dict[str, JsonValue]], history.body["audit"])] == [
        "save",
        "save",
        "revert",
    ]


@pytest.mark.parametrize(
    "content",
    [
        {"yaml": "sources:\n  - params:\n      serviceKey: abc123secret\n"},
        {"note": "fetched https://api.example/x?serviceKey=abc123secret&pageNo=1"},
        {"auth": {"api_key": "abc123secret"}},
    ],
    ids=["yaml-field", "url-parameter", "nested-field"],
)
def test_content_with_a_credential_is_never_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: dict[str, JsonValue]
) -> None:
    """Negative: refused, the value not echoed, and nothing written anywhere."""
    service = _service(tmp_path)
    kind = "spec" if "yaml" in content else "annotation"
    body: dict[str, JsonValue] = {"content": content, "expected_revision": 0}

    response = _call(service, monkeypatch, "PUT", f"/revisions/{kind}/doc", body)

    assert (response.status_code, response.body["code"]) == (400, "credential_in_content")
    assert "abc123secret" not in str(response.body)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"abc123secret" not in path.read_bytes()


def test_the_requests_own_key_is_refused_too(tmp_path: Path) -> None:
    store = RevisionStore(tmp_path / "r.sqlite3")

    with request_scope({"datago": "live-key-820"}), pytest.raises(ValueError):
        store.save(
            "ws", "annotation", "d", {"text": "use live-key-820"}, expected_revision=0, author="a"
        )


def test_another_owners_document_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = _service(tmp_path)
    _save(service, monkeypatch, 0)

    response = _call(service, monkeypatch, "GET", "/revisions/spec/my-spec", principal=_BOB)

    assert (response.status_code, response.body["code"]) == (404, "revision_not_found")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/revisions/unknown/doc", {"content": {}, "expected_revision": 0}),
        ("PUT", "/revisions/spec/doc", {"content": {"yaml": 1}, "expected_revision": 0}),
        ("PUT", "/revisions/spec/doc", {"content": {"yaml": "x"}}),
        ("PUT", "/revisions/annotation/doc", {"content": {}, "expected_revision": -1}),
        ("POST", "/revisions/spec/doc/revert", {"to_revision": 1}),
    ],
)
def test_bad_requests_are_400(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, JsonValue],
) -> None:
    response = _call(_service(tmp_path), monkeypatch, method, path, body)

    assert response.status_code == 400
