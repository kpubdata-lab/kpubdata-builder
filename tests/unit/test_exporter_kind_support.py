"""Spec validation accepts every kind a registry can serve (#1199).

AGENTS.md's registration steps register a factory; the validator used to read
only the legacy instance registry, so a kind registered exactly as documented
was rejected as unsupported. These checks hold that union in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kpubdata_builder.exporters import (
    EXPORTER_REGISTRY,
    BaseExporter,
    register_exporter_factory,
    supported_exporter_kinds,
)
from kpubdata_builder.spec import validator as spec_validator

if TYPE_CHECKING:
    from kpubdata_builder import ArtifactDataset
    from kpubdata_builder.exporters import ExportResult
    from kpubdata_builder.spec import ExportTarget

# Registered once, the documented way: a factory and nothing else. There is no
# unregister API and clearing both registries would wipe the built-ins for
# later tests, so a distinctly named kind is left behind on purpose.
_KIND = "documented-factory-only"


class _DocumentedExporter(BaseExporter):
    @property
    def name(self) -> str:
        return _KIND

    def export(
        self,
        artifact: ArtifactDataset,
        target: ExportTarget,
        output_dir: Path,
    ) -> ExportResult:
        raise AssertionError("the support test never exports")


register_exporter_factory(_KIND, _DocumentedExporter, override=True)


def test_a_factory_only_kind_counts_as_supported() -> None:
    assert _KIND in supported_exporter_kinds()
    assert _KIND not in EXPORTER_REGISTRY


def test_the_validator_asks_the_union_not_the_legacy_registry() -> None:
    source = Path(spec_validator.__file__).read_text(encoding="utf-8")
    assert "EXPORTER_REGISTRY" not in source
    assert "supported_exporter_kinds" in source
