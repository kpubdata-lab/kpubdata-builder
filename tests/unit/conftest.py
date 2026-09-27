"""Test configuration fixture (#326).

This module defines fixtures used commonly across all tests.
Windows cross-platform detection helper (#553) is also here.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from kpubdata_builder.exporters import EXPORTER_REGISTRY


def symlinks_supported() -> bool:
    """Check if symlink creation is possible in current permissions/filesystem (#553).

    On Windows, symlink_to raises OSError without admin privileges or Developer Mode
    — tests where symlink behavior itself is the subject skip based on this value.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(target, target_is_directory=True)
            return True
    except OSError:
        return False


def spawn_timeout_multiplier() -> float:
    """Timeout scale for process spawn-based tests (#553).

    Windows spawn loads child interpreter modules (including polars) from scratch,
    so it can be several seconds slower than Linux — timeout for tests where timing
    itself is not the subject is generously padded by platform scale.
    """
    return 3.0 if sys.platform == "win32" else 1.0


requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(), reason="symlink creation is not permitted on this platform (#553)"
)


@pytest.fixture(autouse=True)
def _isolate_exporter_registry() -> Iterator[None]:
    """Isolate exporter registry before and after each test (#326).

    To prevent exporter registration leaks between tests, snapshot both factory
    and instance registries, then restore to original state on test completion.
    This preserves built-in exporter factory registration (#325) across tests.
    """
    import kpubdata_builder.exporters.registry as reg_module

    factory_snapshot = dict(reg_module._EXPORTER_FACTORIES)
    instance_snapshot = dict(EXPORTER_REGISTRY)
    try:
        yield
    finally:
        reg_module._EXPORTER_FACTORIES.clear()
        reg_module._EXPORTER_FACTORIES.update(factory_snapshot)
        EXPORTER_REGISTRY.clear()
        EXPORTER_REGISTRY.update(instance_snapshot)
