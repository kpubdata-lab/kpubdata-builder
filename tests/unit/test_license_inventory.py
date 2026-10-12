"""The licence inventory gate has to actually refuse (#847).

A gate nobody has watched fail is a gate nobody knows works, so most of these break the
record and expect a problem.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "license_inventory.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_license_inventory", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()


@pytest.fixture
def inventory() -> dict[str, Any]:
    data = gate.load()
    assert data is not None
    return copy.deepcopy(data)


def test_repository_inventory_is_current() -> None:
    assert gate.check(gate.load(), gate.tracked_files()) == []


def test_new_locked_package_without_a_record_fails(inventory: dict[str, Any]) -> None:
    removed = sorted(inventory["packages"])[0]
    del inventory["packages"][removed]
    problems = gate.check(inventory, gate.tracked_files())
    assert any(removed in p and "no recorded licence" in p for p in problems)


def test_version_change_without_a_record_fails(inventory: dict[str, Any]) -> None:
    key = sorted(inventory["packages"])[0]
    inventory["packages"][key]["version"] = "0.0.0"
    assert any("recorded 0.0.0" in p for p in gate.check(inventory, gate.tracked_files()))


def test_package_no_longer_locked_fails(inventory: dict[str, Any]) -> None:
    inventory["packages"]["ghost==1.0"] = {
        "version": "1.0",
        "scope": "dev",
        "license": "MIT",
        "source": "",
        "basis": "installed",
        "status": "recorded",
    }
    assert any("ghost==1.0" in p for p in gate.check(inventory, gate.tracked_files()))


def test_empty_licence_cannot_be_recorded(inventory: dict[str, Any]) -> None:
    key = sorted(inventory["packages"])[0]
    inventory["packages"][key].update(license="", status="recorded")
    assert any("empty licence" in p for p in gate.check(inventory, gate.tracked_files()))


def test_uncertain_metadata_cannot_be_recorded(inventory: dict[str, Any]) -> None:
    key = sorted(inventory["packages"])[0]
    inventory["packages"][key].update(license="MIT", basis="other-version", status="recorded")
    assert any("status does not match" in p for p in gate.check(inventory, gate.tracked_files()))


def test_stale_notice_fails(inventory: dict[str, Any]) -> None:
    key = sorted(inventory["packages"])[0]
    inventory["packages"][key]["license"] = "Changed"
    assert any("NOTICES" in p for p in gate.check(inventory, gate.tracked_files()))


def test_untracked_kind_of_asset_fails(inventory: dict[str, Any]) -> None:
    files = [*gate.tracked_files(), "docs/fonts/new.woff2"]
    assert any("new.woff2" in p for p in gate.check(inventory, files))


def test_new_bundled_data_outside_inventory_fails(inventory: dict[str, Any]) -> None:
    files = [*gate.tracked_files(), "src/kpubdata_builder/new_data.json"]
    assert any("new_data.json" in p for p in gate.check(inventory, files))


def test_asset_entry_matching_nothing_fails(inventory: dict[str, Any]) -> None:
    files = [
        f
        for f in gate.tracked_files()
        if not f.startswith("src/kpubdata_builder/replay_fixtures/datago/")
    ]
    assert any("replay-provider-responses" in p for p in gate.check(inventory, files))


def test_container_inventory_change_fails(inventory: dict[str, Any]) -> None:
    inventory["container_inputs"][0]["version"] = "different"
    assert any("container inputs" in p for p in gate.check(inventory, gate.tracked_files()))


def test_other_version_metadata_needs_review(monkeypatch: pytest.MonkeyPatch) -> None:
    class OtherVersion:
        version = "0.0.0"

    monkeypatch.setattr(gate.metadata, "distribution", lambda name: OtherVersion())
    monkeypatch.setattr(gate, "declared_license", lambda dist: "MIT")
    monkeypatch.setattr(gate, "source_url", lambda dist, name: "https://example.test")
    packages = gate.build_packages({})
    assert all(record["basis"] == "other-version" for record in packages.values())
    assert all(record["status"] == "needs review" for record in packages.values())


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("MIT", "recorded"),
        ("Apache-2.0", "recorded"),
        ("BSD-3-Clause", "recorded"),
        ("", "needs review"),
        ("UNKNOWN", "needs review"),
        ("MPL-2.0", "needs review"),
        ("GPL-3.0-or-later", "needs review"),
        ("Dual License", "needs review"),
    ],
)
def test_status_never_judges_an_unclear_licence(declared: str, expected: str) -> None:
    assert gate.status_of(declared) == expected
