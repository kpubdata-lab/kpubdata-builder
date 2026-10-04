"""An async build reads the uploads of whoever submitted it (#998).

``POST /builds`` took a spec with a ``kind: file`` source, answered 202, and the job
failed in the worker: the submitting owner reached the manifest and credential
resolution but not the file resolver. These pin what the async path does with a file
source for the uploader, for another owner, and for a submission with no owner.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService

_TERMINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture()
def service(tmp_path: Path) -> BuilderService:
    (tmp_path / "out").mkdir()
    return BuilderService(output_root=tmp_path / "out", client_factory=lambda **_kw: None)


def _spec(service: BuilderService, owner: str) -> str:
    upload = service._upload_repository.put(
        owner,
        content=b'{"a": 1}\n{"a": 2}\n',
        format="jsonl",
        encoding="utf-8",
        original_filename="rows.jsonl",
    )
    return f"""\
dataset_id: async.file
title: Async file
description: d
sources:
  - kind: file
    upload_id: {upload.upload_id}
    format: jsonl
    alias: rows
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def _finished(service: BuilderService, run_id: str) -> dict[str, object]:
    for _ in range(400):
        body = service.build_status(run_id).body
        if body.get("status") in _TERMINAL:
            return body
        time.sleep(0.02)
    raise AssertionError(f"{run_id} did not finish")


def test_the_submitter_can_build_their_upload_asynchronously(service: BuilderService) -> None:
    spec = _spec(service, "owner-1")

    assert service.submit_build(spec, run_id="mine", owner_id="owner-1").status_code == 202
    body = _finished(service, "mine")

    assert body["status"] == "succeeded", body
    manifest = json.loads((service._output_root / "mine" / "manifest.json").read_text("utf-8"))
    assert (manifest["status"], manifest["owner_id"]) == ("ok", "owner-1")
    rows = (service._output_root / "mine").rglob("data.jsonl")
    assert [json.loads(line) for line in next(rows).read_text("utf-8").splitlines()] == [
        {"a": 1},
        {"a": 2},
    ]


def test_it_matches_the_synchronous_build(service: BuilderService) -> None:
    spec = _spec(service, "owner-1")

    sync = service.build(spec, run_id="sync", owner_id="owner-1")
    service.submit_build(spec, run_id="async", owner_id="owner-1")

    assert sync.status_code == 200
    assert _finished(service, "async")["status"] == "succeeded"


def test_another_owners_upload_is_not_readable(service: BuilderService) -> None:
    """Uploads are isolated per owner on the async path as on the synchronous one."""
    spec = _spec(service, "owner-1")

    service.submit_build(spec, run_id="theirs", owner_id="owner-2")
    body = _finished(service, "theirs")

    assert body["status"] == "failed"
    assert service.build(spec, run_id="theirs-sync", owner_id="owner-2").status_code != 200
    assert not list((service._output_root / "theirs").rglob("data.jsonl"))


def test_a_submission_with_no_owner_fails_with_the_reason(service: BuilderService) -> None:
    spec = _spec(service, "owner-1")

    assert service.submit_build(spec, run_id="nobody").status_code == 202
    body = _finished(service, "nobody")

    assert body["status"] == "failed"
    assert body["error"] == "file source requires an authenticated, stable principal owner"
