"""The build execution service stands on its own (#596, #637).

Constructed from its narrow dependencies — no ``BuilderService`` — so its error
mapping can be tested without assembling the whole service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.events import BuildEventStore
from kpubdata_builder.service.build_runs_api import BuildRunsApiService
from kpubdata_builder.service.build_slots import BuildSlots
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.service.providers import ProviderCredentialConflictError
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import parse_spec
from kpubdata_builder.store import make_build_index
from kpubdata_builder.store.artifacts import make_artifact_store

_SPEC = parse_spec(
    {
        "dataset_id": "d.x",
        "title": "T",
        "description": "D",
        "sources": [{"provider": "datago", "dataset": "air_quality"}],
    }
)


def _service(tmp_path: Path, open_client: object) -> BuildRunsApiService:
    executor = AsyncBuildExecutor(max_workers=1, max_queue_size=1)
    return BuildRunsApiService(
        output_root=tmp_path,
        api_version="9.9.9",
        load_validated=lambda _yaml: _SPEC,
        open_client=open_client,  # type: ignore[arg-type]
        close_client=lambda _client: None,
        upload_repository_for=lambda _spec: None,
        event_store=lambda: BuildEventStore(tmp_path),
        table_catalog=lambda: None,
        warehouse_configured=False,
        build_index=make_build_index(tmp_path),
        store=make_artifact_store(tmp_path),
        async_builds=executor,
        build_slots=BuildSlots(1),
    )


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ProviderCredentialConflictError("two keys for datago"), 400),
        (ValueError("bad credential request"), 400),
        (RuntimeError("factory broke"), 502),
    ],
)
def test_a_client_that_cannot_be_opened_maps_to_a_status(
    tmp_path: Path, error: Exception, status: int
) -> None:
    def open_client(*_args: object) -> object:
        raise error

    response = _service(tmp_path, open_client).build("ignored")

    assert response.status_code == status
    if status == 502:
        # The factory's own message never reaches the caller.
        assert response.body == {
            "error": "provider client unavailable",
            "code": "provider_client_unavailable",
        }


def test_a_spec_that_does_not_validate_is_returned_as_is(tmp_path: Path) -> None:
    service = _service(tmp_path, lambda *_: pytest.fail("no client for an invalid spec"))
    refusal = ServiceResponse(400, {"error": "invalid"})
    service._load_validated = lambda _yaml: refusal  # type: ignore[method-assign]

    assert service.build("ignored") is refusal


def test_status_of_an_unsafe_or_unknown_run(tmp_path: Path) -> None:
    service = _service(tmp_path, lambda *_: pytest.fail("not used"))

    assert service.build_status("../escape").status_code == 400
    assert service.build_status("never-ran").status_code == 404
    assert service.cancel_build("never-ran").status_code == 404
