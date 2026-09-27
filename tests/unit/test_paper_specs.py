"""Three paper experiment datasets declared with BuildSpec alone (#611, #613, #636).

These two issues blocked exactly this — can we express it. Declarations actually
pass through loader and validator; we lock it here. Writing "now possible" in docs
differs from verifying declaration remains valid.

Actual collection and HF product comparison is handled by #636 — requires 1,500 public API calls and
API key, so not unit test work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kpubdata_builder.spec import (
    compute_spec_digest,
    expand_param_grid,
    parse_spec,
    serialize_spec_bytes,
)
from kpubdata_builder.spec.validator import validate_spec

_SPECS = Path(__file__).parents[2] / "specs"

_PAPER_SPECS = [
    "seoul-apartment-trades.yaml",
    "seoul-apartment-rent.yaml",
    "seoul-bike-rent-month.yaml",
]


def _load(name: str):  # noqa: ANN202 - BuildSpec
    return parse_spec(yaml.safe_load((_SPECS / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", _PAPER_SPECS)
def test_the_spec_parses_and_validates(name: str) -> None:
    validate_spec(_load(name))


@pytest.mark.parametrize("name", _PAPER_SPECS)
def test_the_spec_round_trips_through_the_snapshot(name: str) -> None:
    # recipe must go through snapshot and return intact so R1 has something to compare.
    spec = _load(name)

    assert parse_spec(yaml.safe_load(serialize_spec_bytes(spec).decode("utf-8"))) == spec


@pytest.mark.parametrize(
    ("name", "expected"),
    [("seoul-apartment-trades.yaml", 1500), ("seoul-apartment-rent.yaml", 1500)],
)
def test_the_real_estate_grids_expand_to_the_script_s_call_count(name: str, expected: int) -> None:
    """25 wards × 60 months; 1,500-line generated list reduced to 2-line spec is the point."""
    source = _load(name).sources[0]

    assert len(expand_param_grid(dict(source.params), dict(source.param_grid))) == expected


def test_the_bike_spec_needs_no_grid() -> None:
    # Bike sharing comes via deployment file, not public API — one snapshot, not repeated calls.
    # Generation diffs are absorbed by coalesce/zfill/null_tokens.
    source = _load("seoul-bike-rent-month.yaml").sources[0]

    assert source.kind == "file"
    assert not source.param_grid
    assert set(source.schema.coalesce) == {"ym_raw", "distance_m", "duration_min"}


def test_each_spec_has_its_own_recipe_identity() -> None:
    digests = {
        name: compute_spec_digest(serialize_spec_bytes(_load(name))) for name in _PAPER_SPECS
    }

    assert len(set(digests.values())) == len(_PAPER_SPECS)
