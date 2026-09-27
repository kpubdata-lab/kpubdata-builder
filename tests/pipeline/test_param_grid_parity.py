"""Verify BuildSpec ``param_grid`` against deploy script path (#613).

The purpose of this feature is to replace the 1,500-item combination list
created by `scripts/generate_fetch_params.py` with just two lines in the spec.
To achieve this, we must verify that we call the **same combination set**.

Order differs, and that is intentional — the test below fixes the reasoning.
"""

from __future__ import annotations

from typing import Any

from kpubdata_builder.spec import expand_param_grid

_DISTRICTS = ["11110", "11140", "11170"]
_MONTHS = ["202001", "202002"]


def _script_order(districts: list[str], months: list[str]) -> list[dict[str, str]]:
    """`scripts/generate_fetch_params.py:generate_params` nesting order.

    Outer loop is district, inner loop is year-month.
    """
    return [{"LAWD_CD": code, "DEAL_YMD": ym} for code in districts for ym in months]


def test_the_same_combinations_are_produced() -> None:
    """Combination **set** is identical — this is the actual claim of "reduce
    1,500 lines to two"."""
    from_spec = expand_param_grid({}, {"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)})

    assert {tuple(sorted(c.items())) for c in from_spec} == {
        tuple(sorted(c.items())) for c in _script_order(_DISTRICTS, _MONTHS)
    }


def test_the_combination_count_matches() -> None:
    assert len(
        expand_param_grid({}, {"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)})
    ) == len(_script_order(_DISTRICTS, _MONTHS))


def test_the_order_deliberately_differs_from_the_script() -> None:
    """Order deliberately differs from the script — and that is correct.

    Script runs in declaration order (district outer, year-month inner).
    Spec path sorts keys **by name**, so `DEAL_YMD` becomes outer.

    Why we cannot follow declaration order: `canonical_spec_mapping()` sorts
    mapping keys when writing snapshots. Relying on declaration order means a
    rebuild reading the snapshot will call combinations in **different order**,
    causing the same recipe's digest to produce different Bronze bytes. That
    breaks reproducibility.

    Consequently, Bronze rebuilt via BuildSpec path has identical **record set**
    but different bytes vs. existing artifacts. In #636 (HF dataset rebuild),
    this difference surfaces first when byte-comparing against existing output —
    it is not a data difference.
    """
    from_spec = expand_param_grid({}, {"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)})

    assert list(from_spec) != _script_order(_DISTRICTS, _MONTHS)
    # Only difference: outer/inner swapped.
    assert list(from_spec) == [
        {"DEAL_YMD": ym, "LAWD_CD": code} for ym in _MONTHS for code in _DISTRICTS
    ]


class _FakeDataset:
    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self._seen = seen

    def list(self, **params: Any) -> Any:
        self._seen.append(dict(params))
        key = f"{params['LAWD_CD']}-{params['DEAL_YMD']}"
        return type("R", (), {"items": [{"id": key}]})()


class _FakeClient:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def dataset(self, source_key: str) -> Any:
        return _FakeDataset(self.seen)


def test_a_build_fetches_exactly_the_script_s_combination_set() -> None:
    """Run engine and verify that called combinations match script's set."""
    from kpubdata_builder.spec import SourceRef
    from kpubdata_builder.stages.bronze.resolve import build_bronze_artifact_for_source

    client = _FakeClient()
    source = SourceRef(
        provider="datago",
        dataset="apt_trade",
        param_grid={"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)},
    )

    artifact = build_bronze_artifact_for_source(source, client=client)  # type: ignore[arg-type]

    assert {tuple(sorted(c.items())) for c in client.seen} == {
        tuple(sorted(c.items())) for c in _script_order(_DISTRICTS, _MONTHS)
    }
    assert len(artifact.raw_records) == len(_script_order(_DISTRICTS, _MONTHS))
