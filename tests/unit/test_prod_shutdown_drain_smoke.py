"""The stop timeout the deployment ships, and the smoke that stops a container (#1118).

``serve`` waits for running builds for the grace period, then asks the rest to stop and
waits a little more. Docker kills the container at its own stop timeout whatever the
server is doing, so the compose file's ``stop_grace_period`` has to be the longer of the
two. These hold that, and that the ``Docker`` workflow runs
``scripts/shutdown_drain_smoke.py`` against the image it built. They do not start a
container; the smoke does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import yaml

from kpubdata_builder.service import http as http_module

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = "scripts/shutdown_drain_smoke.py"


def _smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shutdown_drain_smoke", _ROOT / _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["shutdown_drain_smoke"] = module
    spec.loader.exec_module(module)
    return module


def _workflow() -> dict[object, dict[str, dict[str, list[str]]]]:
    loaded = yaml.safe_load((_ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_docker_waits_longer_than_the_server_takes_to_stop() -> None:
    compose = yaml.safe_load((_ROOT / "docker-compose.prod.app.yml").read_text(encoding="utf-8"))
    builder = compose["services"]["builder"]
    budget = http_module._DEFAULT_SHUTDOWN_GRACE_SECONDS + http_module._SHUTDOWN_CANCEL_SECONDS

    # The smoke reads the same value, and fails a stop that took longer than it.
    assert _smoke().stop_timeout_seconds() == float(builder["stop_grace_period"].removesuffix("s"))
    assert _smoke().stop_timeout_seconds() > budget
    # The compose file does not set the grace period, so the default is what runs.
    assert http_module.SHUTDOWN_GRACE_ENV not in builder["environment"]


def test_the_smoke_changes_only_what_it_says_it_changes() -> None:
    override = yaml.safe_load(_smoke()._OVERRIDE)

    assert set(override["services"]) == {"builder"}
    assert set(override["services"]["builder"]) == {"environment"}
    assert set(override["services"]["builder"]["environment"]) == {
        "ENFORCE_OWNERSHIP",
        http_module.SHUTDOWN_GRACE_ENV,
    }


def test_the_image_build_runs_the_smoke() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["build"]["steps"]
    assert isinstance(steps, list)
    runs = [step.get("run", "") for step in steps]
    names = [step.get("name") for step in steps]

    assert f"python3 {_SCRIPT} --image kpubdata-builder:ci" in runs
    assert names.index("Shutdown drain smoke") > names.index("Build serve image")


def test_a_change_to_the_smoke_runs_the_workflow() -> None:
    # PyYAML reads the key `on` as the boolean True.
    triggers = _workflow()[True]

    assert _SCRIPT in triggers["pull_request"]["paths"]
    assert _SCRIPT in triggers["push"]["paths"]
