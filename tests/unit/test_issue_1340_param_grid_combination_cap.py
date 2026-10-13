"""A ``param_grid`` that multiplies out to too many calls is refused at validation (#1340).

Validation looked at each axis — empty, shadowing ``params``, not a scalar — and never
at their product, and the fetch expands the whole grid before its first request. A few
long axes in a short spec could therefore stand for millions of provider calls. The
product is now counted from the axis lengths and held to a limit before anything runs.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef, expand_param_grid
from kpubdata_builder.spec.param_grid import (
    DEFAULT_PARAM_GRID_MAX_COMBINATIONS,
    PARAM_GRID_MAX_COMBINATIONS_ENV,
    count_param_grid_combinations,
    param_grid_max_combinations,
)
from kpubdata_builder.spec.validator import validate_spec

_CODE = "param_grid_too_large"


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Dataset:
    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._calls = calls

    def list(self, **params: object) -> _Result:
        self._calls.append(dict(params))
        return _Result([{"n": len(self._calls)}])


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(self.calls)


def _source(
    grid: dict[str, tuple[JsonValue, ...]],
    *,
    dataset: str = "apt_trade",
    params: dict[str, JsonValue] | None = None,
) -> SourceRef:
    return SourceRef(provider="datago", dataset=dataset, params=params or {}, param_grid=grid)


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="grid.cap",
        title="Grid",
        description="d",
        sources=sources,
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def _axis(length: int) -> tuple[JsonValue, ...]:
    return tuple(str(i) for i in range(length))


def _problems(spec: BuildSpec) -> list[tuple[str, str]]:
    """The ``(code, path)`` of each problem validation reports; none when it passes."""
    try:
        validate_spec(spec)
    except ValidationError as error:
        return [(p.code, p.path) for p in (error.structured_problems or [])]
    return []


@pytest.fixture(autouse=True)
def _no_limit_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PARAM_GRID_MAX_COMBINATIONS_ENV, raising=False)


# ------------------------------------------------------------------ the boundary


def test_a_grid_exactly_at_the_default_limit_is_accepted() -> None:
    grid = {"a": _axis(100), "b": _axis(DEFAULT_PARAM_GRID_MAX_COMBINATIONS // 100)}
    assert count_param_grid_combinations(grid) == DEFAULT_PARAM_GRID_MAX_COMBINATIONS

    assert _problems(_spec(_source(grid))) == []


def test_a_grid_one_over_the_default_limit_is_rejected() -> None:
    grid = {"a": _axis(DEFAULT_PARAM_GRID_MAX_COMBINATIONS + 1)}

    assert _problems(_spec(_source(grid))) == [(_CODE, "sources[0].param_grid")]


@pytest.mark.parametrize(
    ("lengths", "rejected"),
    [((2, 3), False), ((6,), False), ((7,), True), ((2, 2, 2), True), ((1, 1, 6), False)],
)
def test_the_configured_limit_is_the_boundary(
    monkeypatch: pytest.MonkeyPatch, lengths: tuple[int, ...], rejected: bool
) -> None:
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "6")
    grid = {f"k{i}": _axis(length) for i, length in enumerate(lengths)}

    assert (_problems(_spec(_source(grid))) == [(_CODE, "sources[0].param_grid")]) is rejected


def test_the_message_says_the_count_and_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "6")

    with pytest.raises(ValidationError) as raised:
        validate_spec(_spec(_source({"a": _axis(4), "b": _axis(5)})))

    (problem,) = raised.value.structured_problems or []
    assert problem.message == "param_grid expands to 20 combinations; at most 6 are allowed"
    assert PARAM_GRID_MAX_COMBINATIONS_ENV in (problem.hint or "")


def test_each_source_is_held_to_the_limit_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "6")
    spec = _spec(
        _source({"a": _axis(6)}, dataset="small"),
        _source({"a": _axis(7)}, dataset="large"),
        _source({"a": _axis(5)}, dataset="other"),
    )

    assert _problems(spec) == [(_CODE, "sources[1].param_grid")]


# ------------------------------------------------- a product too large to expand


def test_a_product_too_large_to_expand_is_rejected_without_expanding() -> None:
    """Thirty axes of ten values are 10^30 combinations: expanding them never returns.

    That this test finishes at all is the evidence nothing was expanded; the time bound
    only keeps a regression from hanging the suite quietly.
    """
    grid = {f"k{i:02d}": _axis(10) for i in range(30)}
    assert count_param_grid_combinations(grid) == 10**30

    started = time.monotonic()
    with pytest.raises(ValidationError) as raised:
        validate_spec(_spec(_source(grid)))
    elapsed = time.monotonic() - started

    (problem,) = raised.value.structured_problems or []
    assert (problem.code, problem.path) == (_CODE, "sources[0].param_grid")
    assert problem.message == (
        "param_grid expands to about 10^30 combinations; "
        f"at most {DEFAULT_PARAM_GRID_MAX_COMBINATIONS} are allowed"
    )
    assert elapsed < 5.0


def test_a_rejected_build_makes_no_provider_request_and_writes_nothing(tmp_path: Path) -> None:
    client = _Client()
    grid = {f"k{i:02d}": _axis(10) for i in range(30)}

    with pytest.raises(ValidationError):
        run_build(_spec(_source(grid)), client=client, output_root=tmp_path, run_id="refused")

    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------- empty axes


def test_an_empty_axis_is_reported_as_empty_and_not_as_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product is zero, which no limit refuses; the empty axis is the problem."""
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "6")
    grid = {"a": (), "b": _axis(1_000)}
    assert count_param_grid_combinations(grid) == 0

    assert _problems(_spec(_source(grid))) == [("empty_param_grid_axis", "sources[0].param_grid.a")]


def test_a_source_without_a_grid_is_one_call_and_never_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "1")
    assert count_param_grid_combinations({}) == 1

    assert _problems(_spec(_source({}, params={"LAWD_CD": "11110"}))) == []


# --------------------------------------------------------------- ordinary grids


@pytest.mark.parametrize(
    "grid",
    [
        {},
        {"a": ("x",)},
        {"b": ("1", "2", "3"), "a": ("x", "y")},
        {"a": (), "b": ("1", "2")},
        {"LAWD_CD": _axis(25), "DEAL_YMD": _axis(60)},
    ],
)
def test_the_count_is_the_length_of_the_expansion(grid: dict[str, tuple[JsonValue, ...]]) -> None:
    assert count_param_grid_combinations(grid) == len(expand_param_grid({}, grid) if grid else [1])


def test_the_largest_grid_this_repository_declares_is_accepted() -> None:
    """Seoul apartment trades: 25 districts by 60 months, 1,500 combinations."""
    assert _problems(_spec(_source({"LAWD_CD": _axis(25), "DEAL_YMD": _axis(60)}))) == []


def test_a_permitted_grid_is_fetched_once_per_combination_in_the_declared_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the limit exactly: keys sorted, the last key fastest, values as declared."""
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "6")
    client = _Client()
    grid: dict[str, tuple[JsonValue, ...]] = {
        "LAWD_CD": ("11140", "11110"),
        "DEAL_YMD": ("202003", "202001", "202002"),
    }

    result = run_build(_spec(_source(grid)), client=client, output_root=tmp_path, run_id="at-cap")

    assert result.status == "ok"
    assert client.calls == [
        {"DEAL_YMD": "202003", "LAWD_CD": "11140"},
        {"DEAL_YMD": "202003", "LAWD_CD": "11110"},
        {"DEAL_YMD": "202001", "LAWD_CD": "11140"},
        {"DEAL_YMD": "202001", "LAWD_CD": "11110"},
        {"DEAL_YMD": "202002", "LAWD_CD": "11140"},
        {"DEAL_YMD": "202002", "LAWD_CD": "11110"},
    ]


def test_one_combination_more_and_the_same_build_makes_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "5")
    client = _Client()
    grid: dict[str, tuple[JsonValue, ...]] = {
        "LAWD_CD": ("11140", "11110"),
        "DEAL_YMD": ("202003", "202001", "202002"),
    }

    with pytest.raises(ValidationError):
        run_build(_spec(_source(grid)), client=client, output_root=tmp_path, run_id="over-cap")

    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ the setting


@pytest.mark.parametrize(
    ("written", "limit"),
    [
        (None, DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        ("", DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        ("   ", DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        ("many", DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        ("-1", DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        ("1.5", DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        ("9" * 5_000, DEFAULT_PARAM_GRID_MAX_COMBINATIONS),
        (" 25 ", 25),
        ("1", 1),
        ("0", None),
    ],
)
def test_the_limit_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, written: str | None, limit: int | None
) -> None:
    if written is not None:
        monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, written)

    assert param_grid_max_combinations() == limit


def test_zero_turns_the_limit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PARAM_GRID_MAX_COMBINATIONS_ENV, "0")
    grid = {f"k{i:02d}": _axis(10) for i in range(30)}

    assert _problems(_spec(_source(grid))) == []
