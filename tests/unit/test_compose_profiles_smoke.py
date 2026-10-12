"""The production compose's profiles and the smoke that starts each of them (#692).

These do not start a container; ``scripts/compose_profiles_smoke.py`` does, in the
Docker workflow. They hold what keeps that smoke honest: it starts every profile the
compose file declares, the Studio profile does not ask for an image nobody publishes,
and the published-image check reports an image the registry does not have.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_script("compose_profiles_smoke")


def _services() -> dict[str, dict[str, object]]:
    compose = yaml.safe_load((_ROOT / "docker-compose.prod.app.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert isinstance(services, dict)
    return services


def test_the_smoke_starts_every_profile_the_compose_declares() -> None:
    declared: set[str] = set()
    for service in _services().values():
        profiles = service.get("profiles", [])
        assert isinstance(profiles, list)
        declared |= {str(profile) for profile in profiles}

    assert declared | {"default"} == set(smoke.PROFILES)


def test_studio_is_built_from_a_pinned_commit_not_pulled() -> None:
    studio = _services()["studio"]

    # An `image:` would be pulled first, and Studio publishes none (#675's case).
    assert "image" not in studio
    assert studio["profiles"] == ["studio"]
    build = studio["build"]
    assert isinstance(build, dict)
    context = re.fullmatch(
        r"https://github\.com/kpubdata-lab/kpubdata-studio\.git#\$\{STUDIO_REF:-(\w+)\}",
        str(build["context"]),
    )
    assert context, build["context"]
    # A full commit, not a branch: the same value brings up the same screens.
    assert re.fullmatch(r"[0-9a-f]{40}", context.group(1))


#: Studio commits, oldest first. ``STUDIO_REF``'s default must be one of these at or
#: after ``MINIMUM_STUDIO_REF``. Bumping the default means appending the new commit here.
KNOWN_STUDIO_REFS = (
    "023f0c3c84a2d794bf68aa027ef0e4b56bfd1ade",  # kpubdata-studio#926: before #933
    "07c70390ac1ecb6548ba3f11958ec7f01746c959",  # kpubdata-studio#933: assistant off by default
    "3d60c5927eaac4fcf2fb20829ce82fcd0f03083f",  # kpubdata-studio#936
    "978216b44a7e9fd805490758f7d176c002d00582",  # kpubdata-studio#938
)
#: The merge commit of kpubdata-studio#933. An older Studio lets Ask KPubData send
#: questions and data to any https address.
MINIMUM_STUDIO_REF = "07c70390ac1ecb6548ba3f11958ec7f01746c959"


def _studio_ref() -> str:
    build = _services()["studio"]["build"]
    assert isinstance(build, dict)
    found = re.search(r"\$\{STUDIO_REF:-(\w+)\}", str(build["context"]))
    assert found
    return found.group(1)


def test_studio_ref_is_not_older_than_the_assistant_off_by_default_commit() -> None:
    ref = _studio_ref()

    assert ref in KNOWN_STUDIO_REFS, "add the new Studio commit to KNOWN_STUDIO_REFS, oldest first"
    assert KNOWN_STUDIO_REFS.index(ref) >= KNOWN_STUDIO_REFS.index(MINIMUM_STUDIO_REF)


def test_studio_passes_the_assistant_and_account_settings_and_keeps_the_assistant_off() -> None:
    environment = _services()["studio"]["environment"]
    assert isinstance(environment, dict)

    assert environment["ASSISTANT_ENABLED"] == "${STUDIO_ASSISTANT_ENABLED:-false}"
    assert environment["ASSISTANT_ALLOWED_ORIGINS"] == "${STUDIO_ASSISTANT_ALLOWED_ORIGINS:-}"
    assert environment["ACCOUNT_URL"] == "${STUDIO_ACCOUNT_URL:-}"


def test_the_smoke_tells_an_assistant_that_is_off_from_one_that_is_on() -> None:
    off_config = '"assistantEnabled": "false"'
    off = {"Content-Security-Policy": "default-src 'self'; connect-src 'self' http://localhost:*"}
    bare = {"Content-Security-Policy": "default-src 'self'; connect-src 'self' https: http://x"}

    assert smoke.assistant_off(off_config, off)
    assert not smoke.assistant_off(off_config, bare)
    assert not smoke.assistant_off('"assistantEnabled": "true"', off)
    assert not smoke.assistant_off("window.__KPUBDATA_CONFIG__ = {};", off)
    assert not smoke.assistant_off(off_config, {})


def test_studio_listens_on_the_loopback_by_default() -> None:
    assert _services()["studio"]["ports"] == ["${STUDIO_BIND:-127.0.0.1:8080}:8080"]


def test_the_published_check_reads_both_deployment_compose_files() -> None:
    images = smoke.published_images()

    assert "caddy:2.8-alpine" in images
    # The CUBRID stack's Builder image: the tag #675 found unpublished.
    assert "ghcr.io/kpubdata-lab/kpubdata-builder:cubrid" in images


def test_an_image_the_registry_does_not_have_is_reported() -> None:
    assert smoke.missing_images(["a:1", "b:2"], inspect=["false"]) == ["a:1", "b:2"]
    assert smoke.missing_images(["a:1"], inspect=["true"]) == []


def _answer(status: int, body: object) -> object:
    return smoke.Answer(status, {}, json.dumps(body))


@pytest.mark.parametrize(
    ("status", "body", "ok"),
    [
        (200, {"status": "ready", "checks": {"disk": "ok", "workers": "ok"}}, True),
        (503, {"status": "not_ready", "checks": {"disk": "low", "workers": "ok"}}, True),
        (503, {"status": "not_ready", "checks": {"disk": "ok", "state_store": "failed"}}, False),
        (503, {"status": "not_ready", "checks": {"disk": "low", "workers": "draining"}}, False),
        (401, {"error": "unauthorized"}, False),
    ],
)
def test_readiness_passes_a_ready_service_and_only_the_runners_disk(
    status: int, body: object, ok: bool
) -> None:
    assert smoke.readiness_ok(_answer(status, body)) is ok
