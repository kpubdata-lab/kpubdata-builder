"""kpubdata SourceClient Protocol conformance tests (#226).

Builder's Bronze stage requires a kpubdata-compatible client with the following structural contract:

    client.dataset(source_key).list(**params).items -> Iterable[dict]

This test (1) fixes that arbitrary objects can structurally satisfy the Protocol, and
(2) if the real kpubdata package is installed, verifies that kpubdata.Client satisfies
the same structural contract (method/attribute existence level). No network calls.
"""

from __future__ import annotations

import typing
from collections.abc import Iterable

import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import SourceClient


class _ConformingResult:
    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return [{"id": "1"}]


class _ConformingDataset:
    def list(self, **params: JsonValue) -> _ConformingResult:
        return _ConformingResult()


class _ConformingClient:
    def dataset(self, source_key: str) -> _ConformingDataset:
        return _ConformingDataset()


class TestProtocolIsSatisfiable:
    def test_in_memory_client_satisfies_protocol(self) -> None:
        # SourceClient may not be runtime_checkable, so independent of mypy (static),
        # we verify structural conformance at runtime through call path.
        client: SourceClient = _ConformingClient()
        dataset = client.dataset("datago.air_quality")
        result = dataset.list(region="seoul")
        items = list(result.items)
        assert items == [{"id": "1"}]

    def test_protocol_call_chain_shapes(self) -> None:
        client = _ConformingClient()
        # Exact path builder calls: dataset(key).list(**params).items.
        assert hasattr(client, "dataset")
        dataset = client.dataset("k")
        assert hasattr(dataset, "list")
        result = dataset.list()
        assert hasattr(result, "items")
        for record in result.items:
            assert isinstance(record, dict)


class TestRealKpubdataClientConformance:
    def test_real_client_structurally_conforms(self) -> None:
        # Run only when real kpubdata package is importable. No network calls —
        # only verify structural conformance at class/method signature level.
        kpubdata = pytest.importorskip("kpubdata")

        client_cls = getattr(kpubdata, "Client", None)
        assert client_cls is not None, "kpubdata.Client must exist"

        # Client.dataset(source_key) must exist — runtime structural contract.
        assert hasattr(client_cls, "dataset"), "kpubdata.Client must expose .dataset()"
        assert callable(client_cls.dataset)

        # dataset(...) return type must have .list(**params).
        # Use get_type_hints to safely resolve forward refs, and skip deep type inspection
        # if no annotation (runtime structural contract already verified above).
        try:
            dataset_hints = typing.get_type_hints(client_cls.dataset)
        except Exception:
            dataset_hints = {}
        dataset_cls = dataset_hints.get("return")
        if dataset_cls is None:
            # No annotation or cannot parse — skip deep return-type inspection.
            return
        assert hasattr(dataset_cls, "list"), "Dataset must expose .list()"
        assert callable(dataset_cls.list)

        # list(...) return type must have .items (attribute builder consumes).
        try:
            list_hints = typing.get_type_hints(dataset_cls.list)
        except Exception:
            list_hints = {}
        list_return = list_hints.get("return")
        if list_return is None:
            # No annotation or cannot parse — skip deep return-type inspection.
            return
        assert hasattr(list_return, "items"), "list() result must expose .items"
