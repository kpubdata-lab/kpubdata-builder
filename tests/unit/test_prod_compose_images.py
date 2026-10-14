"""Production compose images are pinned to an approved, unmovable image (#1337).

``docker-compose.prod.app.yml`` used to default Builder to ``:latest``, so a redeploy
pulled whatever ``main`` had built last instead of the release that was approved.
These hold the committed file to ``scripts/check_prod_images.py``'s rule, show the
rule refusing what it must refuse, and run ``docker compose config`` — when Docker is
there — to show compose itself refuses to start without ``BUILDER_IMAGE``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.prod.app.yml"
_DIGEST = "@sha256:" + "a" * 64


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load_script("check_prod_images")


def test_every_production_image_is_pinned_or_required() -> None:
    assert check.static_problems(_COMPOSE.read_text(encoding="utf-8")) == []
    assert check.main(["--static"]) == 0


def test_builder_has_no_mutable_fallback_and_caddy_is_pinned_by_digest() -> None:
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]

    assert str(services["builder"]["image"]).startswith("${BUILDER_IMAGE:?")
    assert ":-" not in str(services["builder"]["image"])
    assert check.unpinned(str(services["caddy"]["image"]), "caddy") is None
    assert "latest" not in _COMPOSE.read_text(encoding="utf-8").split("services:", 1)[1]


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/org/app:latest",
        "${IMAGE:-ghcr.io/org/app:latest}",
        "caddy:2.11.6-alpine",
        "${IMAGE:-}",
        "${IMAGE}",
        "caddy@sha256:short",
    ],
)
def test_a_movable_or_missing_reference_is_refused(image: str) -> None:
    text = yaml.safe_dump({"services": {"proxy": {"image": image}}})

    assert check.static_problems(text) != []


@pytest.mark.parametrize(
    "image",
    [
        "caddy:2.11.6-alpine" + _DIGEST,
        "caddy" + _DIGEST,
        "${IMAGE:-caddy:2" + _DIGEST + "}",
        "${IMAGE:?name a release}",
    ],
)
def test_a_pinned_or_required_reference_passes(image: str) -> None:
    text = yaml.safe_dump({"services": {"proxy": {"image": image}}})

    assert check.static_problems(text) == []


def test_builder_must_be_a_release_not_any_digest() -> None:
    repo = "ghcr.io/kpubdata-lab/kpubdata-builder"

    assert check.unpinned(f"{repo}:v0.5.0{_DIGEST}", "builder") is None
    assert check.unpinned(f"{repo}:v1.0.0-rc.1{_DIGEST}", "builder") is None
    assert check.unpinned(f"{repo}:main{_DIGEST}", "builder") is not None
    assert check.unpinned(f"{repo}{_DIGEST}", "builder") is not None
    assert check.unpinned(f"{repo}:v0.5.0", "builder") is not None


def _compose_config(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE), "config", "--images"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=_ROOT,
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_compose_refuses_to_start_without_builder_image() -> None:
    env = {key: value for key, value in os.environ.items() if key != "BUILDER_IMAGE"}
    probe = subprocess.run(
        ["docker", "compose", "version"], check=False, capture_output=True, text=True
    )
    if probe.returncode != 0:
        pytest.skip("needs docker compose")

    refused = _compose_config(env)
    assert refused.returncode != 0
    assert "BUILDER_IMAGE" in refused.stderr
    assert "docs/deploy.md" in refused.stderr

    # Local development opts into a mutable name explicitly, and compose accepts it.
    allowed = _compose_config({**env, "BUILDER_IMAGE": "kpubdata-builder:dev"})
    assert allowed.returncode == 0, allowed.stderr
    assert "kpubdata-builder:dev" in allowed.stdout.split()
