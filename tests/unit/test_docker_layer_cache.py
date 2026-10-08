"""The image's dependency layer survives a source change, and the three builds
keep separate gha cache scopes (#1181).

The Dockerfile comment claimed the manifest was copied first to cache the
dependency layer, but ``src/`` was copied before ``uv sync``, so every source
change re-ran the whole install. The workflows' three builds also shared one
default scope and overwrote each other's cache.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _dockerfile() -> str:
    return (_ROOT / "Dockerfile").read_text(encoding="utf-8")


def _docker_workflow() -> str:
    return (_ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")


def test_the_dependency_layer_precedes_the_source() -> None:
    text = _dockerfile()
    manifest = text.index("COPY pyproject.toml uv.lock")
    deps = text.index("uv sync --no-sources --no-install-project")
    source = text.index("COPY src/ ./src/")
    project = text.index("COPY src/ ./src/", source + 1) if False else text.rindex("uv sync --no-sources")
    assert manifest < deps < source < project


def test_both_syncs_mount_the_uv_cache() -> None:
    assert _dockerfile().count("--mount=type=cache,target=/root/.cache/uv") == 2


def test_the_three_builds_use_separate_gha_scopes() -> None:
    text = _docker_workflow()
    for scope in ("scope=ci", "scope=publish", "scope=cubrid"):
        assert scope in text, scope
    assert "cache-from: type=gha\n" not in text
