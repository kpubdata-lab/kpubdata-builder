"""Public failure messages shared by build and preview (#225, #954).

Build and preview turn a per-source exception into caller-visible text through one
helper (``pipeline.failures.public_failure_message``). Allow-listed exceptions keep
their message; anything else — engine errors, path-bearing OS errors, bugs — is
replaced with a generic message and logged server-side only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import duckdb
import pytest

import kpubdata_builder.pipeline.orchestrator as orchestrator_module
import kpubdata_builder.pipeline.preview as preview_module
from kpubdata_builder.errors import (
    DatasetValidationError,
    ExportError,
    ManifestError,
    TabularError,
    ValidationError,
)
from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.pipeline import preview_build, run_build
from kpubdata_builder.pipeline.failures import (
    PUBLIC_MESSAGE_ERRORS,
    generic_failure_message,
    public_failure_message,
)
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.stages.gold.pii import PiiDeclarationError
from kpubdata_builder.stages.gold.select import GoldSelectionError
from kpubdata_builder.tabular.duckdb_runtime import RESOURCE_LIMIT_MESSAGE, ResourceLimitError

_LOGGER = "kpubdata_builder.pipeline.failures"
_RAW_TEXT = "failed: /srv/kpubdata/spill/run-42/duckdb_temp_block-7.block"


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        return _FakeDataset(self._data[source_key])


def _client() -> _FakeClient:
    return _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})


def _spec() -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.conform",
        title="Conform Sample",
        description="failure message fixture",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


_SPEC_YAML = (
    "dataset_id: dataset.conform\n"
    "title: Conform Sample\n"
    "description: failure message fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: data.jsonl\n"
)

_ALLOWED: list[BaseException] = [
    ValidationError(["column 'x' is not declared"]),
    DatasetValidationError(["row count below minimum"]),
    IngestionError("file source requires an upload store to be configured"),
    GoldSelectionError("selection names unknown column 'x'"),
    PiiDeclarationError("pii column 'x' is not in Silver"),
    ResourceLimitError(RESOURCE_LIMIT_MESSAGE),
]

_HIDDEN: list[BaseException] = [
    RuntimeError(_RAW_TEXT),
    OSError(2, "No such file or directory", "/srv/kpubdata/runs/run-42/gold"),
    ExportError(f"cannot write {_RAW_TEXT}"),
    ManifestError(f"cannot serialize {_RAW_TEXT}"),
    TabularError(f"lossy cast in {_RAW_TEXT}"),
    KeyError(_RAW_TEXT),
]


def test_allow_list_matches_documented_public_errors() -> None:
    assert set(PUBLIC_MESSAGE_ERRORS) == {
        ValidationError,
        DatasetValidationError,
        IngestionError,
        GoldSelectionError,
        PiiDeclarationError,
        ResourceLimitError,
    }


def test_duckdb_out_of_memory_is_stated_as_the_resource_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #701: the engine's text (sizes, spill path) is logged; the caller gets Builder's.
    exc = duckdb.OutOfMemoryException(f"Out of Memory Error: could not spill ({_RAW_TEXT})")
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        assert public_failure_message(exc, "air") == RESOURCE_LIMIT_MESSAGE
    assert any("/srv/kpubdata" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", _ALLOWED, ids=lambda e: type(e).__name__)
def test_allow_listed_exception_keeps_its_message(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        assert public_failure_message(exc, "air") == str(exc)
    assert not caplog.records


@pytest.mark.parametrize("exc", _HIDDEN, ids=lambda e: type(e).__name__)
def test_other_exception_is_replaced_and_logged(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        message = public_failure_message(exc, "air")
    assert message == generic_failure_message("air") == "pipeline failed for source 'air'"
    assert "/srv/kpubdata" not in message
    assert any(r.exc_info is not None and r.exc_info[1] is exc for r in caplog.records)
    assert any("/srv/kpubdata" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", _HIDDEN, ids=lambda e: type(e).__name__)
def test_preview_replaces_raw_exception_text(
    exc: BaseException, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(preview_module, "build_silver_dataset", _fail)

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        result = preview_build(_spec(), client=_client(), limit=2)

    preview = result.previews[0]
    assert preview.status == "failed"
    assert preview.error == "pipeline failed for source 'datago.air_quality'"
    assert any("/srv/kpubdata" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", _ALLOWED, ids=lambda e: type(e).__name__)
def test_preview_keeps_allow_listed_message(
    exc: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(preview_module, "build_silver_dataset", _fail)

    result = preview_build(_spec(), client=_client(), limit=2)

    assert result.previews[0].error == str(exc)


def test_preview_and_build_agree_on_the_same_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One rule, two paths: the same raw failure yields the same caller-visible text.
    def _fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(_RAW_TEXT)

    monkeypatch.setattr(preview_module, "build_silver_dataset", _fail)
    monkeypatch.setattr(orchestrator_module, "build_silver_dataset", _fail)

    preview = preview_build(_spec(), client=_client(), limit=2).previews[0]
    build = run_build(_spec(), client=_client(), output_root=tmp_path, run_id="run1")

    assert preview.error == build.outcomes[0].error == generic_failure_message("datago.air_quality")


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(_RAW_TEXT),
        duckdb.OutOfMemoryException(f"Out of Memory Error: spill to {_RAW_TEXT}"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_preview_http_response_never_carries_raw_text(
    exc: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(preview_module, "build_silver_dataset", _fail)
    service = BuilderService(output_root=tmp_path, client_factory=_client)

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        resp = dispatch(service, "POST", "/preview", {"spec": _SPEC_YAML, "limit": 2})

    assert isinstance(resp, ServiceResponse)
    assert resp.status_code == 200
    body = json.dumps(resp.body)
    assert "/srv/kpubdata" not in body
    assert "Out of Memory" not in body
    assert "duckdb_temp_block" not in body
    expected = (
        RESOURCE_LIMIT_MESSAGE
        if isinstance(exc, duckdb.OutOfMemoryException)
        else "pipeline failed for source 'datago.air_quality'"
    )
    previews = cast(list[dict[str, JsonValue]], resp.body["previews"])
    assert previews[0]["error"] == expected
    # The detail survives server-side only.
    assert any("/srv/kpubdata" in r.getMessage() for r in caplog.records)
