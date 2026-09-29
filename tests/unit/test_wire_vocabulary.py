"""Builder owns the wire vocabulary; kpubdata's values are translated, never passed (#831).

Independence Rule 7: Studio reads Builder's ``AccessStatus``, not kpubdata's probe
statuses. The values may match, but a value kpubdata adds must not reach the wire
until Builder maps it.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.service import vocabulary
from kpubdata_builder.service.spec_api import _catalog_dataset_body

_CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "builder-api.yaml"


def _schemas() -> dict[str, Any]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return cast(dict[str, Any], contract["components"]["schemas"])


class _FutureRepresentation(str, Enum):
    HOLOGRAM = "hologram"


class TestTheContractEnumIsBuildersVocabulary:
    def test_access(self) -> None:
        enum = _schemas()["DatasetStatusAxes"]["properties"]["access"]["enum"]

        assert tuple(enum) == vocabulary.ACCESS_STATUSES

    def test_representation(self) -> None:
        enum = _schemas()["CatalogDataset"]["properties"]["representation"]["enum"]

        assert tuple(enum) == vocabulary.REPRESENTATIONS

    def test_operations(self) -> None:
        enum = _schemas()["CatalogDataset"]["properties"]["operations"]["items"]["enum"]

        assert tuple(enum) == vocabulary.OPERATIONS

    def test_pagination(self) -> None:
        enum = _schemas()["CatalogQuerySupport"]["properties"]["pagination"]["enum"]

        assert tuple(enum) == vocabulary.PAGINATION_MODES


class TestAnUnknownKpubdataValueBecomesABuilderValue:
    """Negative: a value kpubdata adds later must not leak onto the wire."""

    def test_access_status(self) -> None:
        assert vocabulary.access_status("quantum_blocked") == "unknown"
        assert vocabulary.access_status(None) == "unknown"
        assert vocabulary.access_status(42) == "unknown"

    def test_representation(self) -> None:
        assert vocabulary.representation(_FutureRepresentation.HOLOGRAM) == "other"
        assert vocabulary.representation("hologram") == "other"

    def test_operations(self) -> None:
        assert vocabulary.operations(["teleport", "list", "get"]) == ["get", "list"]

    def test_pagination(self) -> None:
        assert vocabulary.pagination_mode("spiral") is None

    def test_the_catalog_body_carries_only_contract_values(self) -> None:
        dataset = SimpleNamespace(
            dataset_key="future.dataset",
            name="Future",
            description=None,
            tags=[],
            source_url=None,
            representation=_FutureRepresentation.HOLOGRAM,
            operations=[SimpleNamespace(value="teleport"), SimpleNamespace(value="list")],
            query_support=SimpleNamespace(
                pagination=SimpleNamespace(value="spiral"),
                filterable_fields=[],
                sortable_fields=[],
                time_range=False,
                max_page_size=None,
            ),
            raw_metadata={},
        )

        body = _catalog_dataset_body(cast(Any, dataset), requires_service_key=False)

        assert body["representation"] == "other"
        assert body["operations"] == ["list"]
        assert body["query_support"] is None


class TestTheValuesCurrentlyMirrorKpubdata:
    """Every value the installed kpubdata has is mapped to the same word.

    A failure here means kpubdata gained a value: the wire is still safe (it becomes
    the fallback), but Builder should decide what to call it.
    """

    def test_representation(self) -> None:
        from kpubdata import Representation

        for member in Representation:
            assert vocabulary.representation(member) == member.value

    def test_operation(self) -> None:
        from kpubdata import Operation

        assert vocabulary.operations(list(Operation)) == sorted(m.value for m in Operation)

    def test_pagination(self) -> None:
        from kpubdata import PaginationMode

        for member in PaginationMode:
            assert vocabulary.pagination_mode(member) == member.value

    def test_probe_status(self) -> None:
        # Test-only: the probe vocabulary is not public kpubdata API, which is why
        # Builder keeps its own copy in the first place.
        probe = pytest.importorskip("kpubdata._probe")

        for status in probe.PROBE_STATUSES:
            assert vocabulary.access_status(status) == status
