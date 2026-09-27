"""Plugin exporter API (#13, #325, #326): verify in-code registration and entry point discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

import kpubdata_builder.exporters.registry as registry
from kpubdata_builder import ArtifactDataset
from kpubdata_builder.exporters import (
    BaseExporter,
    ExportResult,
    clear_exporter_registry,
    get_exporter,
    load_entry_point_exporters,
    register_exporter,
    register_exporter_factory,
)
from kpubdata_builder.spec import ExportTarget


class _FakeExporter(BaseExporter):
    @property
    def name(self) -> str:
        return "fake"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = output_dir / target.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text("fake", encoding="utf-8")
        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )


class _FakeExporter2(_FakeExporter):
    @property
    def name(self) -> str:
        return "fake2"


class _FakeEntryPoint:
    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self._value = value

    def load(self) -> object:
        return self._value


def test_builtin_exporters_are_registered() -> None:
    # Verify built-in exporters are registered in the factory registry (#325).
    import kpubdata_builder.exporters.registry as reg_module

    builtin_kinds = {"jsonl", "markdown", "csv", "parquet", "huggingface", "kaggle"}
    assert set(reg_module._EXPORTER_FACTORIES) >= builtin_kinds


def test_register_factory_and_get_exporter() -> None:
    # Factory-style registration (ADR 0004 recommendation).
    register_exporter_factory("fake", _FakeExporter)

    exporter = get_exporter("fake")
    assert isinstance(exporter, _FakeExporter)
    # Verify a new instance is returned each time.
    another = get_exporter("fake")
    assert exporter is not another


def test_register_factory_duplicate_without_override_raises() -> None:
    # Explicit exception on duplicate registration (#325).
    clear_exporter_registry()  # Cleanup before start.
    register_exporter_factory("fake", _FakeExporter)

    with pytest.raises(ValueError, match="already registered"):
        register_exporter_factory("fake", _FakeExporter)


def test_register_factory_duplicate_with_override_replaces() -> None:
    # Override with override=True.
    clear_exporter_registry()  # Cleanup before start.
    register_exporter_factory("fake", _FakeExporter)

    class AnotherFakeExporter(BaseExporter):
        @property
        def name(self) -> str:
            return "fake"

        def export(
            self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
        ) -> ExportResult:
            dest = output_dir / target.output_path
            return ExportResult(output_path=dest, file_size=0, format="fake")

    register_exporter_factory("fake", AnotherFakeExporter, override=True)

    exporter = get_exporter("fake")
    assert isinstance(exporter, AnotherFakeExporter)
    assert not isinstance(exporter, _FakeExporter)


def test_register_and_get_exporter() -> None:
    # Instance-style registration (legacy compatibility).
    clear_exporter_registry()  # Cleanup before start.
    register_exporter(_FakeExporter())

    assert isinstance(get_exporter("fake"), _FakeExporter)
    # Instance-style returns the same instance.
    another = get_exporter("fake")
    assert another is get_exporter("fake")


def test_register_duplicate_without_override_raises() -> None:
    clear_exporter_registry()  # Cleanup before start.
    register_exporter(_FakeExporter())

    with pytest.raises(ValueError, match="already registered"):
        register_exporter(_FakeExporter())


def test_register_duplicate_with_override_replaces() -> None:
    clear_exporter_registry()  # Cleanup before start.
    first = _FakeExporter()
    second = _FakeExporter()
    register_exporter(first)

    register_exporter(second, override=True)

    assert get_exporter("fake") is second


def test_clear_exporter_registry() -> None:
    # Verify clear_exporter_registry clears all (#325).
    register_exporter_factory("fake3", _FakeExporter)

    clear_exporter_registry()

    with pytest.raises(KeyError, match="unknown exporter kind"):
        get_exporter("fake3")


def test_clear_and_rebuild_builtin_exporters() -> None:
    # Verify built-in exporters can be cleared and re-registered.
    from kpubdata_builder.exporters import CsvExporter, JsonlExporter, MarkdownExporter

    clear_exporter_registry()

    # Re-register built-in exporters.
    register_exporter_factory("csv", CsvExporter)
    register_exporter_factory("jsonl", JsonlExporter)
    register_exporter_factory("markdown", MarkdownExporter)

    # Must be queryable.
    assert isinstance(get_exporter("csv"), CsvExporter)
    assert isinstance(get_exporter("jsonl"), JsonlExporter)
    assert isinstance(get_exporter("markdown"), MarkdownExporter)


def test_get_exporter_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown exporter kind"):
        get_exporter("does-not-exist")


def test_load_entry_point_exporters_discovers_class_and_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Class registered as factory, instance as legacy style (#325).
    eps = [
        _FakeEntryPoint("by-class", _FakeExporter),
        _FakeEntryPoint("by-instance", _FakeExporter2()),
    ]

    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        assert group == registry.EXPORTER_ENTRY_POINT_GROUP
        return eps

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)

    loaded = load_entry_point_exporters(override=True)

    # Class is registered with entry point name.
    assert "by-class" in loaded
    assert "fake2" in loaded
    assert isinstance(get_exporter("by-class"), _FakeExporter)
    # Factory style returns new instance each time.
    assert get_exporter("by-class") is not get_exporter("by-class")
    assert isinstance(get_exporter("fake2"), _FakeExporter2)


def test_load_entry_point_exporters_rejects_non_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        del group
        return [_FakeEntryPoint("bad", object())]

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)

    with pytest.raises(TypeError, match="did not resolve to a BaseExporter"):
        load_entry_point_exporters()
