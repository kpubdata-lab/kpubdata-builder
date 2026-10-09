"""The image installs its dependencies in a layer a source change does not rebuild.

``COPY src/`` came before the dependency install, so every change to the source
reinstalled every dependency, and a ``chown -R /app`` after the install copied the
whole virtual environment into one more layer. These read the Dockerfile's
instructions in order; the image itself is built and started by docker.yml.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "Dockerfile"


def _instructions() -> list[str]:
    """Each instruction on one line, continuations joined and comments dropped."""
    text = re.sub(r"\\\n", " ", _DOCKERFILE.read_text(encoding="utf-8"))
    return [line.strip() for line in text.splitlines() if line.strip() and line[0] != "#"]


def _index(predicate_text: str) -> int:
    matches = [i for i, line in enumerate(_instructions()) if predicate_text in line]
    assert len(matches) == 1, (predicate_text, matches)
    return matches[0]


def test_dependencies_are_installed_before_the_source_is_copied() -> None:
    manifests = _index("pyproject.toml uv.lock ./")
    dependencies = _index("--no-install-project")
    source = _index("COPY --chown=builder:builder src/ ./src/")
    project = [
        i
        for i, line in enumerate(_instructions())
        if "uv sync" in line and "--no-install-project" not in line
    ]

    assert manifests < dependencies < source
    assert len(project) == 1 and project[0] > source


def test_both_installs_are_locked_and_ignore_the_local_sources() -> None:
    syncs = [line for line in _instructions() if "uv sync" in line]

    assert len(syncs) == 2
    for line in syncs:
        assert "--locked" in line
        assert "--no-sources" in line
        assert "${EXTRAS}" in line
        assert "--mount=type=cache" in line


def test_the_install_runs_as_the_unprivileged_user_and_nothing_is_chowned_after() -> None:
    instructions = _instructions()
    user = _index("useradd --system --uid 10001")
    first_sync = next(i for i, line in enumerate(instructions) if "uv sync" in line)
    switched = [i for i, line in enumerate(instructions) if line == "USER builder"]

    assert user < first_sync
    assert switched and switched[0] < first_sync
    assert instructions[-2:] == ["USER builder", 'ENTRYPOINT ["docker-entrypoint.sh"]']
    assert not any("chown -R" in line for line in instructions)


def test_the_uv_binaries_are_removed() -> None:
    assert any("rm -rf /bin/uv /bin/uvx" in line for line in _instructions())


def test_each_image_build_keeps_its_own_cache_scope() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    )
    writes = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            options = step.get("with", {})
            if "cache-to" in options:
                writes.append(options["cache-to"])
                assert "scope=" in options["cache-from"]

    assert len(writes) == 3
    assert len(set(writes)) == 3
    assert all(re.search(r"\bscope=\w+", w) for w in writes)
