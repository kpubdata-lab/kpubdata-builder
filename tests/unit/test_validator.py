"""BuildSpec 최소 검증 규칙과 오류 수집 동작을 검증한다."""

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
    """유효한 BuildSpec에 대해서는 validate_spec가 예외 없이 통과해야 한다."""
    # sources와 exports가 모두 있는 기본 명세를 허용하는지 확인한다.
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
    # 잘못된 입력 조합마다 기대한 problems 목록이 수집되는지 검증한다.
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
    # 빈 provider/dataset은 검증을 통과해 fetch 단계에서 뒤늦게 실패하므로 거부 (#191).
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
    # NaN 비율은 양수/합계 검사를 조용히 통과하므로 유한성 검사로 막는다 (#192).
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
    # publish=true 인데 license 가 없으면 재배포 가능성을 명시할 수 없다 (#443).
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
    # publish=true + license 선언은 통과한다 (양성 회귀, #443).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        publish=True,
        license="CC-BY-4.0",
    )
    validate_spec(spec)  # 예외 없음


def test_validate_spec_skips_license_check_when_not_publishing() -> None:
    # publish=false 면 license 가 없어도 통과한다 (하위 호환, #443).
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        publish=False,
    )
    validate_spec(spec)  # 예외 없음


def test_validate_spec_warns_time_column_random_split() -> None:
    """ratio 모드에서 key가 시간 컬럼이면 누수 경고 (#444)."""
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
    """key 모드에서 시간 컬럼은 정상 (temporal split, #444 양성)."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="desc",
        sources=_SRC,
        exports=_EXP,
        splits=SplitSpec(mode="key", key="date"),
    )
    validate_spec(spec)  # 예외 없음


def test_validate_spec_no_warning_for_ratio_without_time_key() -> None:
    """ratio 모드에서 key가 없거나 시간이 아니면 경고 없음 (#444 양성)."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="desc",
        sources=_SRC,
        exports=_EXP,
        splits=SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, key="region"),
    )
    validate_spec(spec)  # 예외 없음


def _spec_with_schema(schema: SchemaContract) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.trades",
        title="Trades",
        description="Seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade", schema=schema),),
        exports=(ExportTarget(kind="markdown", output_path="README.md"),),
    )


def test_validate_spec_accepts_formatted_numeric_cast() -> None:
    # #611 — int_comma는 dtype이 아니라 named cast다. validator가 _NAMED_DTYPES만
    # 알고 있으면 선언이 validate 단계에서 막혀 Silver 빌드까지 가지도 못한다.
    spec = _spec_with_schema(SchemaContract(casts={"deal_amount": "int_comma"}))

    validate_spec(spec)


def test_validate_spec_rejects_unknown_derived_kind() -> None:
    # #611 — 알 수 없는 kind가 런타임(정규화)에서야 실패하는 것을 막는다.
    spec = _spec_with_schema(
        SchemaContract(
            derived=(DerivedColumn(name="deal_date", kind="concat_date", columns=("a", "b")),)
        )
    )

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "concat_date" in str(exc.value)


def test_validate_spec_rejects_date_parts_without_three_columns() -> None:
    # date_parts는 (year, month, day) 정확히 3개를 요구한다. 2개로 선언하면
    # normalize_table에서 unpack ValueError로 터진다 — 선언 시점에 막는다.
    spec = _spec_with_schema(
        SchemaContract(
            derived=(DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m")),)
        )
    )

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_accepts_year_month_cast() -> None:
    # #620 — year_month도 dtype이 아니라 named cast다. validator가 모르면 선언이
    # validate 단계에서 막혀 Silver 빌드까지 가지도 못한다.
    spec = _spec_with_schema(SchemaContract(casts={"ym": "year_month"}))

    validate_spec(spec)


def test_validate_spec_rejects_coalesce_without_candidates() -> None:
    # #620 — 후보가 비면 normalize가 런타임에 실패한다. 선언 시점에 막는 편이
    # 어디를 고쳐야 하는지 말해 준다.
    spec = _spec_with_schema(SchemaContract(coalesce={"move_meter": ()}))

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "move_meter" in str(exc.value)


def test_validate_spec_rejects_repeated_coalesce_candidate() -> None:
    spec = _spec_with_schema(SchemaContract(coalesce={"move_meter": ("a", "a")}))

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_rejects_chained_coalesce_groups() -> None:
    # #620 — 각 규칙은 수렴한 후보를 지우므로 {"a": ["x"], "b": ["a"]}의 결과는
    # 선언 순서에 달린다. canonical_spec_mapping()은 키를 정렬해 스냅샷을 쓰기
    # 때문에 같은 digest의 선언이 원래 빌드와 다르게 동작할 수 있다.
    spec = _spec_with_schema(SchemaContract(coalesce={"a": ("x",), "b": ("a",)}))

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "is also a candidate of" in str(exc.value)


def test_validate_spec_rejects_coalesce_candidate_claimed_twice() -> None:
    spec = _spec_with_schema(SchemaContract(coalesce={"a": ("x", "y"), "b": ("y",)}))

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_allows_a_coalesce_target_among_its_own_candidates() -> None:
    # 세대가 섞인 스냅샷에서 canonical 이름이 후보 중 하나인 것은 정상이다.
    validate_spec(_spec_with_schema(SchemaContract(coalesce={"a": ("a", "legacy_a")})))


@pytest.mark.parametrize("width", [0, -1])
def test_validate_spec_rejects_non_positive_zfill_width(width: int) -> None:
    spec = _spec_with_schema(SchemaContract(zfill={"station_no": width}))

    with pytest.raises(ValidationError):
        validate_spec(spec)


def test_validate_spec_rejects_empty_column_null_tokens() -> None:
    # #623 — 선언을 써 두고 동작하지 않는 상태가 가장 나쁘다.
    spec = _spec_with_schema(
        SchemaContract(column_null_tokens={"gender": ColumnNullTokens(tokens=())})
    )

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "gender" in str(exc.value)


def test_validate_spec_rejects_unknown_on_absent_policy() -> None:
    # #623 — 알 수 없는 정책이 런타임에서야 "error가 아니니 ignore"로 읽히면 안 된다.
    spec = _spec_with_schema(
        SchemaContract(
            column_null_tokens={"gender": ColumnNullTokens(tokens=("",), on_absent="skip")}
        )
    )

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "skip" in str(exc.value)


def test_validate_spec_rejects_duplicate_rename_targets() -> None:
    # 두 원 필드가 같은 canonical 이름으로 모이면 normalize_table이 Polars
    # DuplicateError로 터진다 — 선언 시점에 spec 용어로 막는다.
    spec = _spec_with_schema(SchemaContract(rename={"sggCd": "code", "lawdCd": "code"}))

    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)

    assert "duplicate_rename_target" in {p.code for p in (exc.value.structured_problems or [])}


@pytest.mark.parametrize(
    "schema",
    [
        # rename 대상과 충돌
        SchemaContract(
            rename={"dealYmd": "deal_date"},
            derived=(DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m", "d")),),
        ),
        # casts 키와 충돌 (casts 는 derived 앞에 돈다)
        SchemaContract(
            casts={"deal_date": "str"},
            derived=(DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m", "d")),),
        ),
        # zfill 키와 충돌
        SchemaContract(
            zfill={"key": 5},
            derived=(DerivedColumn(name="key", kind="join_key", columns=("a",)),),
        ),
        # coalesce 대상과 충돌
        SchemaContract(
            coalesce={"key": ("legacy_key",)},
            derived=(DerivedColumn(name="key", kind="join_key", columns=("a",)),),
        ),
        # 파생 규칙끼리 충돌
        SchemaContract(
            derived=(
                DerivedColumn(name="key", kind="join_key", columns=("a",)),
                DerivedColumn(name="key", kind="join_key", columns=("b",)),
            ),
        ),
    ],
)
def test_validate_spec_rejects_derived_name_collision(schema: SchemaContract) -> None:
    # 파생 컬럼이 같은 schema가 이미 약속한 이름을 쓰면 with_columns가 기존 컬럼을
    # 소리 없이 덮어쓴다 — 선언끼리의 충돌은 선언 시점에 잡는다.
    with pytest.raises(ValidationError) as exc:
        validate_spec(_spec_with_schema(schema))

    assert "derived_name_collision" in {p.code for p in (exc.value.structured_problems or [])}


def test_validate_spec_allows_declaring_a_derived_columns_expected_dtype() -> None:
    # dtypes 는 컬럼을 만들지 않고 기대 타입을 선언할 뿐이다 — 파생 컬럼의 dtype 을
    # 선언하는 것은 충돌이 아니라 정상적인 사용이다.
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
    """같은 dataset을 params만 달리 선언하면 산출물 디렉터리가 겹친다 (#630).

    두 outcome 모두 "ok"로 끝나기 때문에, 막지 않으면 run은 성공으로 보고되고
    데이터는 절반이 조용히 사라진다.
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
    """alias를 주면 합법이다 — 파라미터 그리드(#613)가 이 모양으로 쓴다."""
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
    """persist의 validate_path_segment도 잡지만 그때는 이미 fetch를 마친 뒤다 (#630)."""
    spec = _spec_with_sources(SourceRef(provider="datago", dataset="apt_trade", alias=".."))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("sources[0].alias" in p for p in exc_info.value.problems)
