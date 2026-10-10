"""Where the deployment keeps Builder's state (#1097).

``/data`` holds the SQLite stores, and SQLite on a network filesystem has unreliable
locks. The repository shipped an Azure Container Apps template that put ``/data`` on
Azure Files while the guide forbade exactly that. These hold what replaced it: the
production compose mounts a plain named volume, and no template in the repository
puts ``/data`` on a network share. ``scripts/data_volume_smoke.py`` runs the restart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_NETWORK_SHARES = ("AzureFile", "azureFile", "type: nfs", "driver: nfs", "cifs", "efs")


def _compose() -> dict[str, dict[str, object]]:
    loaded = yaml.safe_load((_ROOT / "docker-compose.prod.app.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_data_is_a_named_volume_with_the_default_local_driver() -> None:
    compose = _compose()
    builder = compose["services"]["builder"]
    assert isinstance(builder, dict)

    assert builder["volumes"] == ["builder-data:/data"]
    # No driver and no driver_opts: Docker's own `local` driver, on the host's disk.
    assert compose["volumes"]["builder-data"] is None


def test_no_tracked_deployment_file_puts_data_on_a_network_share() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "infra", "ops", "*.yml", "*.yaml", "*.bicep", "*.tf"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    deployment = [
        path
        for path in tracked
        if path.startswith(("infra/", "ops/", "docker-compose")) and not path.endswith(".md")
    ]

    assert deployment
    assert not [path for path in deployment if path.endswith((".bicep", ".tf"))]
    offending = [
        (path, marker)
        for path in deployment
        for marker in _NETWORK_SHARES
        if marker in (_ROOT / path).read_text(encoding="utf-8")
    ]
    assert offending == []
