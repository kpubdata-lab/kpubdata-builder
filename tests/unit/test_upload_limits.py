"""Per-user upload limits in a multi-user deployment (#1045, kpubdata#812).

Nothing bounded what one account could upload, and nothing ever deleted an upload but
its owner. In a multi-user deployment each owner now has a file count, a total size
and a retention period; a single-user deployment has none of them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.upload_limits import (
    MAX_FILES_ENV,
    MAX_TOTAL_BYTES_ENV,
    RETENTION_DAYS_ENV,
    UploadLimits,
    resolve_upload_limits,
)
from kpubdata_builder.uploads import SQLiteUploadRepository

from .test_service import _FakeClient

_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")
_CSV = b"a,b\n1,2\n"


@pytest.fixture()
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    for name in (MAX_FILES_ENV, MAX_TOTAL_BYTES_ENV, RETENTION_DAYS_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1"}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)


def _as(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)


def _upload(service: BuilderService, content: bytes = _CSV) -> ServiceResponse:
    response = dispatch(service, "POST", "/uploads", None, query="format=csv", raw_body=content)
    assert isinstance(response, ServiceResponse)
    return response


def _delete(service: BuilderService, upload_id: str) -> ServiceResponse:
    response = dispatch(service, "DELETE", f"/uploads/{upload_id}", None)
    assert isinstance(response, ServiceResponse)
    return response


def _age(service: BuilderService, upload_id: str, *, days: int) -> None:
    """Make the upload look ``days`` old, as it would be once that time had passed."""
    created = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    path = service._output_root / ".service" / "uploads.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE uploads SET created_at = ? WHERE upload_id = ?", (created, upload_id)
        )


# --- the limits in force ---


def test_the_defaults_are_the_decided_ones(multi_user: None) -> None:
    assert resolve_upload_limits() == UploadLimits(
        max_files=50, max_total_bytes=1024**3, retention_days=30
    )


def test_each_limit_is_overridable_and_zero_turns_it_off(
    multi_user: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_FILES_ENV, "3")
    monkeypatch.setenv(MAX_TOTAL_BYTES_ENV, "0")
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")

    assert resolve_upload_limits() == UploadLimits(
        max_files=3, max_total_bytes=None, retention_days=7
    )


@pytest.mark.parametrize("value", ["many", "-5", " "])
def test_a_value_that_is_not_a_count_falls_back_to_the_default(
    multi_user: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MAX_FILES_ENV, value)

    limits = resolve_upload_limits()

    assert limits is not None
    assert limits.max_files == 50


def test_a_single_user_deployment_has_no_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.setenv(MAX_FILES_ENV, "1")

    assert resolve_upload_limits() is None


# --- file count ---


def test_the_file_past_the_count_is_refused_and_deleting_one_makes_room(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_FILES_ENV, "2")
    _as(monkeypatch, _ALICE)
    first = _upload(service)
    assert first.status_code == 200
    assert _upload(service).status_code == 200

    refused = _upload(service)

    assert refused.status_code == 409
    assert refused.body == {
        "error": (
            "upload limit reached: delete an upload you no longer need, then send this one again"
        ),
        "code": "upload_quota_exceeded",
        "limit": "max_files",
        "limit_value": 2,
        "used": 2,
    }
    assert _delete(service, str(first.body["upload_id"])).status_code == 200
    assert _upload(service).status_code == 200


def test_one_users_uploads_do_not_count_against_another(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_FILES_ENV, "1")
    _as(monkeypatch, _ALICE)
    assert _upload(service).status_code == 200
    assert _upload(service).status_code == 409

    _as(monkeypatch, _BOB)

    assert _upload(service).status_code == 200


# --- total size ---


def test_the_upload_that_crosses_the_total_is_refused(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_TOTAL_BYTES_ENV, str(2 * len(_CSV) + 3))
    _as(monkeypatch, _ALICE)
    assert _upload(service).status_code == 200
    assert _upload(service).status_code == 200

    refused = _upload(service)

    assert refused.status_code == 409
    assert (refused.body["code"], refused.body["limit"]) == (
        "upload_quota_exceeded",
        "max_total_bytes",
    )
    assert refused.body["used"] == 2 * len(_CSV)
    # A smaller one that still fits under the total goes through.
    assert _upload(service, b"a\n1").status_code == 200


def test_an_upload_that_exactly_reaches_the_total_fits(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_TOTAL_BYTES_ENV, str(2 * len(_CSV)))
    _as(monkeypatch, _ALICE)

    assert [_upload(service).status_code for _ in range(3)] == [200, 200, 409]


# --- retention ---


def test_an_upload_past_retention_is_gone_and_no_longer_counts(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_FILES_ENV, "1")
    _as(monkeypatch, _ALICE)
    old = str(_upload(service).body["upload_id"])
    assert _upload(service).status_code == 409
    _age(service, old, days=31)

    # The owner's next upload drops what is past retention before counting.
    assert _upload(service).status_code == 200

    gone = dispatch(service, "GET", f"/uploads/{old}", None)
    assert isinstance(gone, ServiceResponse)
    assert gone.status_code == 404


def test_an_upload_inside_the_retention_period_is_kept(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)
    kept = str(_upload(service).body["upload_id"])
    _age(service, kept, days=29)

    assert service.purge_expired_uploads() == 0
    found = dispatch(service, "GET", f"/uploads/{kept}", None)
    assert isinstance(found, ServiceResponse)
    assert found.status_code == 200


def test_startup_deletes_every_owners_expired_uploads_and_their_files(
    multi_user: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A spill threshold of one byte, so every payload is a file on disk and not a row only.
    database = tmp_path / ".service" / "uploads.sqlite3"
    database.parent.mkdir(parents=True)
    repository = SQLiteUploadRepository(database, spill_threshold_bytes=1)
    client = _FakeClient({"datago.air_quality": [{"id": "1"}]})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_kwargs: client,
        upload_repository=repository,
    )
    _as(monkeypatch, _ALICE)
    alice_old = str(_upload(service).body["upload_id"])
    alice_new = str(_upload(service).body["upload_id"])
    _as(monkeypatch, _BOB)
    bob_old = str(_upload(service).body["upload_id"])
    for upload_id in (alice_old, bob_old):
        _age(service, upload_id, days=40)
    blobs = Path(f"{database}.blobs")
    assert len([path for path in blobs.rglob("*") if path.is_file()]) == 3

    assert service.purge_expired_uploads() == 2

    with sqlite3.connect(database) as connection:
        remaining = [str(row[0]) for row in connection.execute("SELECT upload_id FROM uploads")]
    assert remaining == [alice_new]
    # The payload files of the two that went are gone with their rows.
    assert len([path for path in blobs.rglob("*") if path.is_file()]) == 1


def test_a_workspace_without_uploads_gets_no_store_from_the_startup_purge(
    multi_user: None, service: BuilderService, tmp_path: Path
) -> None:
    assert service.purge_expired_uploads() == 0
    assert not (tmp_path / ".service" / "uploads.sqlite3").exists()


def test_retention_off_keeps_everything(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RETENTION_DAYS_ENV, "0")
    _as(monkeypatch, _ALICE)
    old = str(_upload(service).body["upload_id"])
    _age(service, old, days=400)

    assert service.purge_expired_uploads() == 0
    assert _upload(service).status_code == 200
    found = dispatch(service, "GET", f"/uploads/{old}", None)
    assert isinstance(found, ServiceResponse)
    assert found.status_code == 200


# --- single user ---


def test_a_single_user_deployment_is_unchanged(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.setenv(MAX_FILES_ENV, "1")
    monkeypatch.setenv(RETENTION_DAYS_ENV, "1")
    first = str(_upload(service).body["upload_id"])
    _age(service, first, days=10)

    assert _upload(service).status_code == 200
    assert service.purge_expired_uploads() == 0
    found = dispatch(service, "GET", f"/uploads/{first}", None)
    assert isinstance(found, ServiceResponse)
    assert found.status_code == 200


# --- expires_at: the owner can see the end coming (#1047) ---


def _get_upload(service: BuilderService, upload_id: str) -> ServiceResponse:
    response = dispatch(service, "GET", f"/uploads/{upload_id}", None)
    assert isinstance(response, ServiceResponse)
    return response


def test_an_upload_says_when_it_expires(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    created = _upload(service)

    body = created.body
    expires = datetime.fromisoformat(str(body["expires_at"]))
    assert expires - datetime.fromisoformat(str(body["created_at"])) == timedelta(days=30)
    # Reading it back says the same.
    assert _get_upload(service, str(body["upload_id"])).body["expires_at"] == body["expires_at"]


def test_the_date_follows_the_retention_period_in_force(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RETENTION_DAYS_ENV, "7")
    _as(monkeypatch, _ALICE)

    body = _upload(service).body

    expires = datetime.fromisoformat(str(body["expires_at"]))
    assert expires - datetime.fromisoformat(str(body["created_at"])) == timedelta(days=7)


def test_the_date_is_the_moment_the_purge_takes_the_upload(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the field promises and what the purge does are the same boundary."""
    _as(monkeypatch, _ALICE)
    upload_id = str(_upload(service).body["upload_id"])

    _age(service, upload_id, days=29)
    still_there = _get_upload(service, upload_id)
    assert still_there.status_code == 200
    assert datetime.fromisoformat(str(still_there.body["expires_at"])) > datetime.now(timezone.utc)
    assert service.purge_expired_uploads() == 0

    _age(service, upload_id, days=31)
    # Past the date the upload is not there for a read either (#1067): the read is what
    # removes it now, so the purge finds nothing left to take.
    assert _get_upload(service, upload_id).status_code == 404
    assert service.purge_expired_uploads() == 0


def test_nothing_expires_when_retention_is_off(
    multi_user: None, service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RETENTION_DAYS_ENV, "0")
    _as(monkeypatch, _ALICE)

    body = _upload(service).body

    assert "expires_at" in body
    assert body["expires_at"] is None


def test_nothing_expires_in_a_single_user_deployment(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)

    body = _upload(service).body

    assert body["expires_at"] is None
    assert _get_upload(service, str(body["upload_id"])).body["expires_at"] is None
