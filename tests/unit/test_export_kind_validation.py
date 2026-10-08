"""Spec validation knows every export kind ``get_exporter`` can serve (#1192).

``AGENTS.md`` and ADR 0004 say to register an exporter by factory. Validation looked in
the legacy instance registry alone, so an exporter registered that way was built on
request and refused in a spec. The built-in ones are in both registries, which is why
nothing showed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import kpubdata_builder.exporters.registry as registry
from kpubdata_builder import ArtifactDataset
from kpubdata_builder.errors import ValidationError
from kpubdata_builder.exporters import (
    BaseExporter,
    ExportResult,
    get_exporter,
    load_entry_point_exporters,
    register_exporter_factory,
    register_exporter_instance,
    registered_exporter_kinds,
)
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef
from kpubdata_builder.spec.validator import validate_spec

_BUILT_IN = {"csv", "huggingface", "jsonl", "kaggle", "markdown", "parquet"}


class _Exporter(BaseExporter):
    kind = "by-factory"

    @property
    def name(self) -> str:
        return self.kind

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = output_dir / target.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("x", encoding="utf-8")
        return ExportResult(output_path=destination, file_size=1, format=self.name)


class _InstanceExporter(_Exporter):
    kind = "by-instance"


class _EntryPointExporter(_Exporter):
    kind = "by-entry-point"


@pytest.fixture(autouse=True)
def _own_registries() -> Iterator[None]:
    """What a test registers ends with the test.

    The registries stay the same objects: code that imported one of them holds the
    object, and would not see a copy put in its place.
    """
    factories = dict(registry._EXPORTER_FACTORIES)
    instances = dict(registry.EXPORTER_REGISTRY)
    try:
        yield
    finally:
        registry._EXPORTER_FACTORIES.clear()
        registry._EXPORTER_FACTORIES.update(factories)
        registry.EXPORTER_REGISTRY.clear()
        registry.EXPORTER_REGISTRY.update(instances)


def _spec(kind: str) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind=kind, output_path="out/data.bin"),),
    )


def _problems(kind: str) -> list[str]:
    try:
        validate_spec(_spec(kind))
    except ValidationError as error:
        return list(error.problems)
    return []


def test_an_exporter_registered_by_factory_is_a_supported_kind() -> None:
    register_exporter_factory("by-factory", _Exporter)

    assert registry.EXPORTER_REGISTRY.get("by-factory") is None
    assert get_exporter("by-factory").name == "by-factory"
    assert _problems("by-factory") == []


def test_an_exporter_registered_as_an_instance_still_is() -> None:
    register_exporter_instance(_InstanceExporter())

    assert _problems("by-instance") == []


def test_an_exporter_class_found_by_entry_point_is_a_supported_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry point that names a class is registered by factory too."""

    class _EntryPoint:
        name = "by-entry-point"

        def load(self) -> object:
            return _EntryPointExporter

    monkeypatch.setattr(registry, "entry_points", lambda *, group: [_EntryPoint()])

    assert load_entry_point_exporters() == ["by-entry-point"]
    assert _problems("by-entry-point") == []


def test_a_kind_nobody_registered_is_refused_and_told_what_there_is() -> None:
    register_exporter_factory("by-factory", _Exporter)

    with pytest.raises(ValidationError) as refused:
        validate_spec(_spec("xml"))

    (problem,) = refused.value.structured_problems or ()
    assert problem.code == "unsupported_export_kind"
    assert problem.path == "exports[0].kind"
    # The factory-only kind is among the ones the hint offers.
    offered = (problem.hint or "").removeprefix("Use one of: ").split(", ")
    # At least: an installed plugin, or another test's registration, may add to them.
    assert set(offered) >= _BUILT_IN | {"by-factory"}
    assert "xml" not in offered


def test_validation_and_get_exporter_agree_on_every_kind() -> None:
    register_exporter_factory("by-factory", _Exporter)
    register_exporter_instance(_InstanceExporter())

    kinds = registered_exporter_kinds()

    assert kinds >= _BUILT_IN | {"by-factory", "by-instance"}
    for kind in kinds:
        assert get_exporter(kind).name == kind
        assert _problems(kind) == []
    with pytest.raises(KeyError):
        get_exporter("xml")
    assert _problems("xml") != []
