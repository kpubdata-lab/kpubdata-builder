"""Where runs are kept apart per user, the API key reads run metadata and no one's data
(#1072, ADR 0012 decision of 2026-10-01).

The ``service`` principal — the deployment's ``X-API-Key`` — had full access to every
user's runs wherever ownership was enforced. It is like an administrator now: the
administration routes show it every run's metadata, and another user's run data, files
and rows are not there for it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, FileResponse, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV

from .test_service_jobs import VALID_SPEC_YAML, _service

_ALICE = Principal(kind="oidc", identifier="alice", owner_id="oidc:alice")
#: The principal an ``X-API-Key`` request gets (``auth._verify_api_key``).
_API_KEY = Principal(kind="service", owner_id=compute_owner_id("service", "default"), is_admin=True)

#: Every way a run's data leaves: status, files, manifest, spec, events, stage rows.
_RUN_DATA = (
    "/builds/{run}",
    "/builds/{run}/manifest",
    "/builds/{run}/spec",
    "/builds/{run}/events",
    "/builds/{run}/quality",
    "/builds/{run}/stages",
    "/artifacts/{run}",
    "/artifacts/{run}/manifest.json",
)


def _as(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal)


def _get(service: BuilderService, path: str) -> ServiceResponse | FileResponse:
    return dispatch(service, "GET", path, None)


def _status(response: ServiceResponse | FileResponse) -> int:
    return response.status_code if isinstance(response, ServiceResponse) else 200


@pytest.fixture()
def alices_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    """A service holding one run, built by Alice while ownership is enforced."""
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    service = _service(tmp_path, threading.Event())
    _as(monkeypatch, _ALICE)
    built = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "alice-1"})
    assert isinstance(built, ServiceResponse) and built.status_code < 400, built
    return service


def test_the_owner_reads_her_run(alices_run: BuilderService) -> None:
    """The paths below are real ones: the owner gets every one of them."""
    for template in _RUN_DATA:
        path = template.format(run="alice-1")
        assert _status(_get(alices_run, path)) == 200, path


def test_the_api_key_reaches_none_of_another_users_run_data(
    alices_run: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _API_KEY)

    for template in _RUN_DATA:
        path = template.format(run="alice-1")
        response = _get(alices_run, path)
        # The answer a run that does not exist gets (#796): its existence is not revealed.
        assert _status(response) == 404, path
        assert _status(response) == _status(_get(alices_run, template.format(run="no-such-run")))


def test_the_api_key_cannot_build_over_another_users_run(
    alices_run: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = (alices_run._output_root / "alice-1" / "manifest.json").read_bytes()
    _as(monkeypatch, _API_KEY)

    rebuilt = dispatch(alices_run, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "alice-1"})

    assert isinstance(rebuilt, ServiceResponse) and rebuilt.status_code == 403
    assert (alices_run._output_root / "alice-1" / "manifest.json").read_bytes() == manifest


def test_the_api_key_still_reads_every_runs_metadata(
    alices_run: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What an administrator sees: that the run exists, whose it is and how it ended."""
    _as(monkeypatch, _API_KEY)

    response = _get(alices_run, "/admin/runs")

    assert isinstance(response, ServiceResponse) and response.status_code == 200, response
    runs = response.body["runs"]
    assert isinstance(runs, list)
    assert [run["run_id"] for run in runs if isinstance(run, dict)] == ["alice-1"]


def test_the_api_key_owns_the_runs_it_makes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: it lost other people's runs, not its own."""
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    service = _service(tmp_path, threading.Event())
    _as(monkeypatch, _API_KEY)

    built = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "key-1"})

    assert isinstance(built, ServiceResponse) and built.status_code < 400
    for template in _RUN_DATA:
        assert _status(_get(service, template.format(run="key-1"))) == 200, template


def test_in_a_single_user_deployment_the_api_key_reads_everything_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: where ownership is not enforced nothing is asked of any principal."""
    monkeypatch.delenv(_OWNERSHIP_ENV, raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    service = _service(tmp_path, threading.Event())
    _as(monkeypatch, _ALICE)
    built = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "alice-1"})
    assert isinstance(built, ServiceResponse) and built.status_code < 400

    _as(monkeypatch, _API_KEY)

    for template in _RUN_DATA:
        assert _status(_get(service, template.format(run="alice-1"))) == 200, template
