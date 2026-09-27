"""BuildSpec       ."""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.spec import (
    BuildSpec,
    ColumnNullTokens,
    DerivedColumn,
    ExportTarget,
    SchemaContract,
    SourceRef,
    SplitSpec,
)
from kpubdata_builder.spec.validator import validate_spec


def test_validate_spec_accepts_valid_spec() -> None:
    """BuildSpec  validate_spec    ."""
    # sources exports      .
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="markdown", output_path="README.md"),),
    )

    validate_spec(spec)


_SRC = (SourceRef(provider="datago", dataset="air_quality"),)
_EXP = (ExportTarget(kind="markdown", output_path="README.md"),)


@pytest.mark.parametrize(
    ("dataset_id", "title", "description", "sources", "exports", "expected_problems"),
    [
        (
            "   ",
            "Sample Dataset",
            "Sample description",
            _SRC,
            _EXP,
            ["dataset_id must be a non-empty string"],
        ),
        (
            "dataset.sample",
            "  ",
            "Sample description",
            _SRC,
            _EXP,
            ["title must be a non-empty string"],
        ),
        (
            "dataset.sample",
            "Sample Dataset",
            "  ",
            _SRC,
            _EXP,
            ["description must be a non-empty string"],
        ),
        (
            "dataset.sample",
            "Sample Dataset",
            "Sample description",
            (),
            _EXP,
            ["at least one source is required"],
        ),
    ],
)
def test_validate_spec_rejects_invalid_spec(
    dataset_id: str,
    title: str,
    description: str,
    sources: tuple[SourceRef, ...],
    exports: tuple[ExportTarget, ...],
    expected_problems: list[str],
) -> None:
    #     problems   .
    spec = BuildSpec(
        dataset_id=dataset_id,
        title=title,
        description=description,
        sources=sources,
        exports=exports,
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert exc_info.value.problems == expected_problems


def test_validate_spec_accepts_a_spec_without_exports() -> None:
    """A spec with no export target is valid (#703).

    Where this used to be a validation problem, it is now the materialise-only end
    state: the build finishes at a committed table and publishing is an explicit
    follow-up. Requiring an export target made a local analysis declare where to
    publish before it could finish.
    """
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=(),
    )

    validate_spec(spec)  # does not raise


def test_validate_spec_rejects_unsupported_export_kind() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=(ExportTarget(kind="xml", output_path="out/data.xml"),),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("xml" in p and "not supported" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_empty_output_path() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=(ExportTarget(kind="jsonl", output_path="   "),),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("output_path" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_empty_metadata_key() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        metadata={"": "value"},
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("metadata keys" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_empty_source_provider_and_dataset() -> None:
    #  provider/dataset   fetch     (#191).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="  ", dataset=""),),
        exports=_EXP,
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    problems = exc_info.value.problems
    assert any("sources[0].provider" in p for p in problems)
    assert any("sources[0].dataset" in p for p in problems)


def test_validate_spec_rejects_blank_source_alias() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality", alias="   "),),
        exports=_EXP,
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("sources[0].alias" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_nan_split_ratio() -> None:
    # NaN  /       (#192).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        splits=SplitSpec(mode="ratio", ratios={"train": float("nan"), "test": 0.5}),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("finite" in p for p in exc_info.value.problems)


def test_validate_spec_requires_license_when_publish() -> None:
    # publish=true  license        (#443).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        publish=True,
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "missing_license_for_publish" in codes


def test_validate_spec_accepts_publish_with_license() -> None:
    # publish=true + license   ( , #443).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        publish=True,
        license="CC-BY-4.0",
    )
    validate_spec(spec)  #


def test_validate_spec_skips_license_check_when_not_publishing() -> None:
    # publish=false  license    ( , #443).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        publish=False,
    )
    validate_spec(spec)  #


def test_validate_spec_warns_time_column_random_split() -> None:
    """ratio  key     (#444)."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="desc",
        sources=_SRC,
        exports=_EXP,
        splits=SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, key="date"),
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "time_column_random_split" in codes


def test_validate_spec_no_warning_for_key_mode_with_time_column() -> None:
    """key     (temporal split, #444 )."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="desc",
        sources=_SRC,
        exports=_EXP,
        splits=SplitSpec(mode="key", key="date"),
    )
    validate_spec(spec)  #


def test_validate_spec_no_warning_for_ratio_without_time_key() -> None:
    """ratio  key      (#444 )."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="desc",
        sources=_SRC,
        exports=_EXP,
        splits=SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, key="region"),
    )
    validate_spec(spec)  #


def _spec_with_schema(schema: SchemaContract) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.trades",
        title="Trades",
        description="Seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade", schema=schema),),
        exports=(ExportTarget(kind="markdown", output_path="README.md"),),
    )


def test_validate_spec_accepts_formatted_numeric_cast() -> None:
    # #611 — int_comma dtype  named cast. validator _NAMED_DTYPES
    #    validate   Silver   .
    spec = _spec_with_schema(SchemaContract(casts={"deal_amount": "int_comma"}))

    validate_spec(spec)


def test_validate_spec_rejects_unknown_derived_kind() -> None:
    # #611 —    kind ()   .
    spec = _spec_with_schema(
        SchemaContract(
            derived=(DerivedColumn(name="deal_date", kind="concat_date", columns=("a", "b")),)
        )
    )

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "concat_date" in str(exc.value)


def test_validate_spec_rejects_date_parts_without_three_columns() -> None:
    # date_parts (year, month, day)  3 . 2
    # normalize_table unpack ValueError  —   .
    spec = _spec_with_schema(
        SchemaContract(
            derived=(DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m")),)
        )
    )

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_accepts_year_month_cast() -> None:
    # #620 — year_month dtype  named cast. validator
    # validate   Silver   .
    spec = _spec_with_schema(SchemaContract(casts={"ym": "year_month"}))

    validate_spec(spec)


def test_validate_spec_rejects_coalesce_without_candidates() -> None:
    # #620 —   normalize  .
    #     .
    spec = _spec_with_schema(SchemaContract(coalesce={"move_meter": ()}))

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "move_meter" in str(exc.value)


def test_validate_spec_rejects_repeated_coalesce_candidate() -> None:
    spec = _spec_with_schema(SchemaContract(coalesce={"move_meter": ("a", "a")}))

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_rejects_chained_coalesce_groups() -> None:
    # #620 —      {"a": ["x"], "b": ["a"]}
    #   . canonical_spec_mapping()
    #   digest       .
    spec = _spec_with_schema(SchemaContract(coalesce={"a": ("x",), "b": ("a",)}))

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "is also a candidate of" in str(exc.value)


def test_validate_spec_rejects_coalesce_candidate_claimed_twice() -> None:
    spec = _spec_with_schema(SchemaContract(coalesce={"a": ("x", "y"), "b": ("y",)}))

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_allows_a_coalesce_target_among_its_own_candidates() -> None:
    #    canonical      .
    validate_spec(_spec_with_schema(SchemaContract(coalesce={"a": ("a", "legacy_a")})))


@pytest.mark.parametrize("width", [0, -1])
def test_validate_spec_rejects_non_positive_zfill_width(width: int) -> None:
    spec = _spec_with_schema(SchemaContract(zfill={"station_no": width}))

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_rejects_empty_column_null_tokens() -> None:
    # #623 —        .
    spec = _spec_with_schema(
        SchemaContract(column_null_tokens={"gender": ColumnNullTokens(tokens=())})
    )

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "gender" in str(exc.value)


def test_validate_spec_rejects_unknown_on_absent_policy() -> None:
    # #623 —      "error  ignore"   .
    spec = _spec_with_schema(
        SchemaContract(
            column_null_tokens={"gender": ColumnNullTokens(tokens=("",), on_absent="skip")}
        )
    )

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "skip" in str(exc.value)


def test_validate_spec_rejects_duplicate_rename_targets() -> None:
    #     canonical   normalize_table Polars
    # DuplicateError  —   spec  .
    spec = _spec_with_schema(SchemaContract(rename={"sggCd": "code", "lawdCd": "code"}))

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "duplicate_rename_target" in {p.code for p in (exc.value.structured_problems or [])}


@pytest.mark.parametrize(
    "schema",
    [
        # rename
        SchemaContract(
            rename={"dealYmd": "deal_date"},
            derived=(DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m", "d")),),
        ),
        # casts   (casts  derived  )
        SchemaContract(
            casts={"deal_date": "str"},
            derived=(DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m", "d")),),
        ),
        # zfill
        SchemaContract(
            zfill={"key": 5},
            derived=(DerivedColumn(name="key", kind="join_key", columns=("a",)),),
        ),
        # coalesce
        SchemaContract(
            coalesce={"key": ("legacy_key",)},
            derived=(DerivedColumn(name="key", kind="join_key", columns=("a",)),),
        ),
        #
        SchemaContract(
            derived=(
                DerivedColumn(name="key", kind="join_key", columns=("a",)),
                DerivedColumn(name="key", kind="join_key", columns=("b",)),
            ),
        ),
    ],
)
def test_validate_spec_rejects_derived_name_collision(schema: SchemaContract) -> None:
    #    schema     with_columns
    #    —     .
    with pytest.raises(ValidationError) as exc:
        validate_spec(_spec_with_schema(schema))

    assert "derived_name_collision" in {p.code for p in (exc.value.structured_problems or [])}


def test_validate_spec_allows_declaring_a_derived_columns_expected_dtype() -> None:
    # dtypes         —   dtype
    #      .
    validate_spec(
        _spec_with_schema(
            SchemaContract(
                dtypes={"deal_date": "date"},
                derived=(
                    DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m", "d")),
                ),
            )
        )
    )


def _spec_with_sources(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=tuple(sources),
        exports=_EXP,
    )


def test_two_sources_resolving_to_the_same_output_key_are_rejected() -> None:
    """dataset params      (#630).

    outcome  "ok"  ,   run
      .
    """
    spec = _spec_with_sources(
        SourceRef(provider="datago", dataset="apt_trade", params={"LAWD_CD": "11110"}),
        SourceRef(provider="datago", dataset="apt_trade", params={"LAWD_CD": "11140"}),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    problems = exc_info.value.problems
    assert any("datago.apt_trade" in p for p in problems)
    assert any("sources[1]" in p for p in problems)


def test_distinct_aliases_make_the_same_dataset_declarable_twice() -> None:
    """alias   —  (#613)   ."""
    validate_spec(
        _spec_with_sources(
            SourceRef(
                provider="datago", dataset="apt_trade", params={"LAWD_CD": "11110"}, alias="jongno"
            ),
            SourceRef(
                provider="datago", dataset="apt_trade", params={"LAWD_CD": "11140"}, alias="mapo"
            ),
        )
    )


def test_two_sources_sharing_an_alias_are_rejected() -> None:
    spec = _spec_with_sources(
        SourceRef(provider="datago", dataset="apt_trade", alias="trades"),
        SourceRef(provider="datago", dataset="apt_rent", alias="trades"),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("'trades'" in p for p in exc_info.value.problems)


def test_an_alias_that_escapes_the_workspace_is_rejected_before_fetching() -> None:
    """persist validate_path_segment    fetch   (#630)."""
    spec = _spec_with_sources(SourceRef(provider="datago", dataset="apt_trade", alias=".."))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("sources[0].alias" in p for p in exc_info.value.problems)
