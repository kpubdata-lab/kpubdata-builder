"""An owner can list their uploads, and one past retention is not there (#1067).

A user at an upload limit was told to delete an upload and had no way to learn an id. And
the contract says an upload is deleted at ``expires_at``, while it stayed readable — and
buildable — until the service restarted or its owner uploaded again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.upload_limits import RETENTION_DAYS_ENV

from ._openapi import response_schema, validate
from .test_upload_limits import _ALICE, _BOB, _CSV, _age, _as, _upload, multi_user, service

__all__ = ["multi_user", "service"]

_CONTRACT = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)
_SPEC = """\
dataset_id: up.table
title: Uploaded
description: d
sources:
  - kind: file
    upload_id: {upload_id}
    format: csv
    alias: rows
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def _get(service: BuilderService, path: str) -> ServiceResponse:
    response = dispatch(service, "GET", path, None)
    assert isinstance(response, ServiceResponse)
    return response


def _listed(service: BuilderService) -> list[str]:
    response = _get(service, "/uploads")
    assert response.status_code == 200, response.body
    uploads = response.body["uploads"]
    assert isinstance(uploads, list)
    return [str(item["upload_id"]) for item in uploads if isinstance(item, dict)]


def _id(response: ServiceResponse) -> str:
    assert response.status_code == 200, response.body
    return str(response.body["upload_id"])


def test_the_list_holds_the_requesters_uploads_and_nobody_elses(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    _as(monkeypatch, _ALICE)
    first = _id(_upload(service))
    second = _id(_upload(service, b"a,b\n3,4\n"))
    _as(monkeypatch, _BOB)
    bobs = _id(_upload(service))

    assert _listed(service) == [bobs]
    _as(monkeypatch, _ALICE)
    assert sorted(_listed(service)) == sorted([first, second])
    assert bobs not in _listed(service)


def test_the_list_is_metadata_as_the_contract_declares_it(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    _as(monkeypatch, _ALICE)
    upload_id = _id(_upload(service))

    response = _get(service, "/uploads")

    schema = response_schema(_CONTRACT, "/uploads", "get", 200)
    assert schema is not None
    assert validate(response.body, schema, _CONTRACT) == []
    assert response.body["uploads"] == [_get(service, f"/uploads/{upload_id}").body]
    assert _CSV.decode() not in str(response.body)


def test_an_empty_list_is_an_empty_list(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    _as(monkeypatch, _ALICE)

    assert _get(service, "/uploads").body == {"uploads": []}


def test_an_upload_past_retention_is_not_found_and_not_listed(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """Without a restart and without another upload — the two things that purged before."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")
    _as(monkeypatch, _ALICE)
    old = _id(_upload(service))
    kept = _id(_upload(service, b"a,b\n3,4\n"))
    _age(service, old, days=8)

    assert _get(service, f"/uploads/{old}").status_code == 404
    assert _get(service, f"/uploads/{kept}").status_code == 200
    assert _listed(service) == [kept]


def test_a_build_does_not_read_an_upload_past_retention(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")
    _as(monkeypatch, _ALICE)
    upload_id = _id(_upload(service))
    spec = _SPEC.format(upload_id=upload_id)
    fresh = dispatch(service, "POST", "/preview", {"spec": spec})
    assert isinstance(fresh, ServiceResponse) and fresh.status_code == 200, fresh.body

    _age(service, upload_id, days=8)
    preview = dispatch(service, "POST", "/preview", {"spec": spec})
    build = dispatch(service, "POST", "/build", {"spec": spec, "run_id": "expired-1"})

    assert isinstance(preview, ServiceResponse) and isinstance(build, ServiceResponse)
    # A preview reports a source that failed inside a 200; a build fails as a whole.
    assert "upload not found" in str(preview.body)
    assert "'status': 'failed'" in str(preview.body)
    assert build.status_code >= 400
    assert "upload not found" in str(build.body)


def test_reading_another_owners_expired_upload_does_not_delete_mine(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """Negative: a read drops what of the *reader's* is past retention, nothing else."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")
    _as(monkeypatch, _ALICE)
    alices = _id(_upload(service))
    _as(monkeypatch, _BOB)
    bobs = _id(_upload(service))
    _age(service, bobs, days=8)

    _as(monkeypatch, _ALICE)
    assert _get(service, f"/uploads/{bobs}").status_code == 404
    assert _listed(service) == [alices]


def test_a_single_user_deployment_keeps_and_lists_everything(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: no retention there, so age changes nothing."""
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")
    upload_id = _id(_upload(service))
    _age(service, upload_id, days=400)

    assert _get(service, f"/uploads/{upload_id}").status_code == 200
    assert _listed(service) == [upload_id]
    assert _get(service, f"/uploads/{upload_id}").body["expires_at"] is None
