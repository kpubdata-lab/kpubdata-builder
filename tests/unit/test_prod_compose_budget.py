"""The production compose configures a warehouse and a memory limit its own settings fit (#993).

``docker-compose.prod.app.yml`` set no warehouse root, so every ``/warehouse/*`` route
answered ``warehouse_not_configured``, and capped the container at 512M — less than the
default limit of one DuckDB connection. This computes the budget ``docs/deploy.md`` §9
describes from the file's own defaults and fails when the container limit is below it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.prod.app.yml"
_ENV_EXAMPLE = _ROOT / ".env.app.example"

#: Sources a build fetches at once (``_MAX_PARALLEL_SOURCES``), each with a connection.
_SOURCES_PER_BUILD = 4
#: Connections outside builds: one preview, one composition.
_EXTRA_CONNECTIONS = 2
#: The server process, HTTP threads and headroom (docs/deploy.md §9).
_BASE_MB = 400

_DEFAULT = re.compile(r"^\$\{[A-Z0-9_]+:-(?P<default>[^}]*)\}$")
_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "KIB": 2**10, "MIB": 2**20, "GIB": 2**30}
_COMPOSE_UNITS = {"K": 2**10, "M": 2**20, "G": 2**30}


def _default(value: object) -> str:
    """What compose substitutes for ``${VAR:-default}`` when VAR is unset."""
    text = str(value)
    match = _DEFAULT.match(text)
    return match.group("default") if match else text


def _duckdb_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", value)
    assert match, value
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def _compose_bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KMG])", value.upper())
    assert match, value
    return int(match.group(1)) * _COMPOSE_UNITS[match.group(2)]


def required_bytes(environment: dict[str, Any]) -> int:
    """The memory the service's own settings can ask for at once."""
    workers = int(_default(environment["KPUBDATA_BUILDER_MAX_WORKERS"]))
    connections = workers * _SOURCES_PER_BUILD + _EXTRA_CONNECTIONS
    duckdb = connections * _duckdb_bytes(_default(environment["KPUBDATA_DUCKDB_MEMORY_LIMIT"]))
    query = int(_default(environment["KPUBDATA_QUERY_MEMORY_BUDGET_MB"])) * 2**20
    return duckdb + query + _BASE_MB * 2**20


@pytest.fixture(scope="module")
def builder() -> dict[str, Any]:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    service: dict[str, Any] = compose["services"]["builder"]
    return service


def test_the_warehouse_root_is_on_the_data_volume(builder: dict[str, Any]) -> None:
    root = _default(builder["environment"]["KPUBDATA_BUILDER_WAREHOUSE"])

    assert root.startswith("/data/")
    assert "builder-data:/data" in builder["volumes"]


def test_the_container_limit_covers_the_configured_budget(builder: dict[str, Any]) -> None:
    limit = _compose_bytes(_default(builder["deploy"]["resources"]["limits"]["memory"]))

    assert limit >= required_bytes(builder["environment"])


def test_a_query_reserves_no_more_than_the_query_budget(builder: dict[str, Any]) -> None:
    """A query that reserves more than the budget can never be admitted."""
    environment = builder["environment"]

    assert int(_default(environment["KPUBDATA_QUERY_MAX_MEMORY_MB"])) <= int(
        _default(environment["KPUBDATA_QUERY_MEMORY_BUDGET_MB"])
    )


def test_the_old_512m_limit_fails_the_check(builder: dict[str, Any]) -> None:
    """The gate itself: the limit this replaces does not cover the budget."""
    assert _compose_bytes("512M") < required_bytes(builder["environment"])


def test_the_default_duckdb_limit_alone_overruns_a_small_container() -> None:
    """With no DuckDB limit set a connection may take 1GB: ten of them need over 10 GB."""
    environment = {
        "KPUBDATA_BUILDER_MAX_WORKERS": "2",
        "KPUBDATA_DUCKDB_MEMORY_LIMIT": "1GB",
        "KPUBDATA_QUERY_MEMORY_BUDGET_MB": "768",
    }

    assert required_bytes(environment) > _compose_bytes("3G")


def test_the_env_example_names_every_budget_variable(builder: dict[str, Any]) -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    overridable = [
        name
        for name, value in builder["environment"].items()
        if name.startswith(("KPUBDATA_DUCKDB_", "KPUBDATA_QUERY_")) and _DEFAULT.match(str(value))
    ]

    assert overridable
    assert [name for name in [*overridable, "BUILDER_MEMORY_LIMIT"] if name not in text] == []
