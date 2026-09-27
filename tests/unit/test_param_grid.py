"""``param_grid`` declaration and expansion (#613).

One source can express only one call combination, so of three paper experiment datasets,
none could be collected with BuildSpec alone. Writing 1,500 sources isn't
workaround — each source becomes separate Silver/Gold output, won't merge into one
dataset.
"""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import (
    BuildSpec,
    ExportTarget,
    SourceRef,
    compute_spec_digest,
    expand_param_grid,
    parse_spec,
    serialize_spec_bytes,
)
from kpubdata_builder.spec.validator import validate_spec

_EXPORTS = (ExportTarget(kind="jsonl", output_path="out/data.jsonl"),)


def _spec(source: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="Sample",
        sources=(source,),
        exports=_EXPORTS,
    )


class TestExpansionOrderIsAContract:
    """If expansion order changes, concatenated Bronze bytes change.

    artifact_id changes with it, and R1's claim "same snapshot·contract·builder rebuilds to
    same result" breaks. So order is contract, not implementation detail.
    """

    GRID = {"DEAL_YMD": ("202001", "202002"), "LAWD_CD": ("11110", "11140")}

    def test_keys_are_sorted_and_the_last_varies_fastest(self) -> None:
        assert expand_param_grid({}, self.GRID) == (
            {"DEAL_YMD": "202001", "LAWD_CD": "11110"},
            {"DEAL_YMD": "202001", "LAWD_CD": "11140"},
            {"DEAL_YMD": "202002", "LAWD_CD": "11110"},
            {"DEAL_YMD": "202002", "LAWD_CD": "11140"},
        )

    def test_declaration_key_order_does_not_change_the_result(self) -> None:
        # canonical_spec_mapping() sorts keys when writing snapshot, so declaration order
        # would make same digest spec call in different order.
        reversed_declaration = {"LAWD_CD": ("11110", "11140"), "DEAL_YMD": ("202001", "202002")}

        assert expand_param_grid({}, reversed_declaration) == expand_param_grid({}, self.GRID)

    def test_values_keep_their_declared_order(self) -> None:
        # Values are not sorted — declaration order matters (e.g., month lists have intent).
        assert [
            c["DEAL_YMD"] for c in expand_param_grid({}, {"DEAL_YMD": ("202012", "202001")})
        ] == [
            "202012",
            "202001",
        ]

    def test_shared_params_are_merged_into_every_combination(self) -> None:
        combos = expand_param_grid({"numOfRows": 100}, {"LAWD_CD": ("11110", "11140")})

        assert all(c["numOfRows"] == 100 for c in combos)
        assert len(combos) == 2

    def test_an_empty_grid_yields_the_shared_params_once(self) -> None:
        assert expand_param_grid({"numOfRows": 100}, {}) == ({"numOfRows": 100},)

    def test_the_combination_count_is_the_cartesian_product(self) -> None:
        grid = {"a": tuple(str(i) for i in range(25)), "b": tuple(str(i) for i in range(60))}

        assert len(expand_param_grid({}, grid)) == 1500


class TestParamGridParsing:
    def test_a_grid_round_trips_through_the_loader(self) -> None:
        spec = parse_spec(
            {
                "dataset_id": "dataset.sample",
                "title": "Sample",
                "description": "Sample",
                "sources": [
                    {
                        "provider": "datago",
                        "dataset": "apt_trade",
                        "params": {"numOfRows": 100},
                        "param_grid": {"LAWD_CD": ["11110", "11140"]},
                    }
                ],
                "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
            }
        )

        assert spec.sources[0].param_grid == {"LAWD_CD": ("11110", "11140")}
        assert spec.sources[0].params == {"numOfRows": 100}

    def test_a_scalar_axis_is_rejected_at_parse_time(self) -> None:
        # Single-value axis differs from shared params — latter expressed via params.
        # parse_spec wraps structural errors in SpecLoadError.
        with pytest.raises(SpecLoadError, match="must be a list"):
            parse_spec(
                {
                    "dataset_id": "dataset.sample",
                    "title": "Sample",
                    "description": "Sample",
                    "sources": [
                        {
                            "provider": "datago",
                            "dataset": "apt_trade",
                            "param_grid": {"LAWD_CD": "11110"},
                        }
                    ],
                    "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
                }
            )


class TestParamGridValidation:
    def test_an_empty_axis_is_rejected(self) -> None:
        # Empty axis makes product zero — no calls ever happen, empty Bronze recorded as success.
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade", param_grid={"LAWD_CD": ()}))

        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)

        assert "empty_param_grid_axis" in {p.code for p in (exc.value.structured_problems or [])}

    def test_declaring_the_same_key_in_params_and_grid_is_rejected(self) -> None:
        spec = _spec(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                params={"LAWD_CD": "11110"},
                param_grid={"LAWD_CD": ("11140",)},
            )
        )

        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)

        assert "param_grid_shadows_params" in {
            p.code for p in (exc.value.structured_problems or [])
        }

    def test_a_nested_value_is_rejected(self) -> None:
        # Request params are scalar; nested values cannot go in URL.
        spec = _spec(
            SourceRef(provider="datago", dataset="apt_trade", param_grid={"x": ({"a": 1},)})
        )

        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)

        assert "invalid_param_grid_value" in {p.code for p in (exc.value.structured_problems or [])}

    def test_a_valid_grid_passes(self) -> None:
        validate_spec(
            _spec(
                SourceRef(
                    provider="datago",
                    dataset="apt_trade",
                    params={"numOfRows": 100},
                    param_grid={"LAWD_CD": ("11110", "11140")},
                )
            )
        )


class TestParamGridIsPartOfTheRecipe:
    def test_changing_the_grid_moves_the_digest(self) -> None:
        # grid determines what data was fetched; if not in digest, changing grid still looks
        # like "same recipe".
        first = _spec(
            SourceRef(provider="datago", dataset="apt_trade", param_grid={"LAWD_CD": ("11110",)})
        )
        second = _spec(
            SourceRef(provider="datago", dataset="apt_trade", param_grid={"LAWD_CD": ("11140",)})
        )

        assert compute_spec_digest(serialize_spec_bytes(first)) != compute_spec_digest(
            serialize_spec_bytes(second)
        )

    def test_no_grid_leaves_the_digest_untouched(self) -> None:
        # Unused feature must not move existing spec recipe identity.
        without = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        empty = _spec(SourceRef(provider="datago", dataset="apt_trade", param_grid={}))

        assert compute_spec_digest(serialize_spec_bytes(without)) == compute_spec_digest(
            serialize_spec_bytes(empty)
        )

    def test_a_grid_survives_the_snapshot_round_trip(self) -> None:
        import yaml

        from kpubdata_builder.spec import serialize_spec

        spec = _spec(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                params={"numOfRows": 100},
                param_grid={"LAWD_CD": ("11110", "11140")},
            )
        )

        assert parse_spec(yaml.safe_load(serialize_spec(spec))) == spec


class TestBronzeCollectionAcrossCombinations:
    """Call per combination and concatenate into single Bronze (#613)."""

    class _Dataset:
        def __init__(self, calls: list[dict[str, object]]) -> None:
            self._calls = calls

        def list(self, **params: object) -> object:
            self._calls.append(dict(params))
            tag = params.get("LAWD_CD", "x")
            return type("R", (), {"items": [{"id": f"{tag}-1"}, {"id": f"{tag}-2"}]})()

    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def dataset(self, source_key: str) -> object:
            return TestBronzeCollectionAcrossCombinations._Dataset(self.calls)

    def _build(self, source: SourceRef) -> tuple[object, list[dict[str, object]]]:
        from kpubdata_builder.stages.bronze.resolve import build_bronze_artifact_for_source

        client = self._Client()
        artifact = build_bronze_artifact_for_source(source, client=client)  # type: ignore[arg-type]
        return artifact, client.calls

    def test_each_combination_is_called_once_in_expansion_order(self) -> None:
        artifact, calls = self._build(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                params={"numOfRows": 100},
                param_grid={"LAWD_CD": ("11110", "11140")},
            )
        )

        assert [c["LAWD_CD"] for c in calls] == ["11110", "11140"]
        assert all(c["numOfRows"] == 100 for c in calls)
        assert [r["id"] for r in artifact.raw_records] == [  # type: ignore[attr-defined]
            "11110-1",
            "11110-2",
            "11140-1",
            "11140-2",
        ]

    def test_records_concatenate_in_combination_order(self) -> None:
        # If order changes, raw_records.jsonl bytes change and artifact_id follows —
        # R1's rebuild determinism rests on this.
        first, _ = self._build(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                param_grid={"LAWD_CD": ("11110", "11140")},
            )
        )
        second, _ = self._build(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                param_grid={"LAWD_CD": ("11110", "11140")},
            )
        )

        assert first.raw_records == second.raw_records  # type: ignore[attr-defined]

    def test_the_expansion_is_recorded_in_provenance(self) -> None:
        # Without recording combination that produced Bronze, reproducibility loses grounding.
        artifact, _ = self._build(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                param_grid={"LAWD_CD": ("11110", "11140")},
            )
        )

        recorded = artifact.fetch_params["param_combinations"]  # type: ignore[attr-defined]
        assert recorded == [{"LAWD_CD": "11110"}, {"LAWD_CD": "11140"}]

    def test_a_source_without_a_grid_keeps_the_old_shape(self) -> None:
        # Unused feature must not change provenance shape.
        artifact, calls = self._build(
            SourceRef(provider="datago", dataset="apt_trade", params={"numOfRows": 100})
        )

        assert calls == [{"numOfRows": 100}]
        assert "param_combinations" not in artifact.fetch_params  # type: ignore[attr-defined]

    def test_an_empty_expansion_is_refused(self) -> None:
        from kpubdata_builder.stages.bronze.build import build_bronze_artifact

        with pytest.raises(ValueError, match="must not be empty"):
            build_bronze_artifact(
                self._Client(),  # type: ignore[arg-type]
                source_key="datago.apt_trade",
                param_combinations=[],
            )
