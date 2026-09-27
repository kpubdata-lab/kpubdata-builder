"""빌더 명세 검증 루틴 (Medallion 재구성: 기존 validator.py에서 이동).

이 모듈은 BuildSpec이 최소 실행 요건을 충족하는지 검사한다. exporter
레지스트리에 의존하므로 spec 패키지 __init__에서 re-export 하지 않고
``kpubdata_builder.spec.validator`` 경로로 직접 가져온다 (순환 import 회피).

주요 함수:
    - validate_spec: dataset_id, sources, exports 같은 필수 조건 검증

BL3 (#417): problems를 {code, path, message, hint} 객체 배열로 구조화.
기존 문자열 problems 소비자를 위해 message는 항상 포함.
"""

from __future__ import annotations

import codecs
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..errors import ValidationError
from ..exporters import EXPORTER_REGISTRY
from ..tabular.polars_helpers import _FORMATTED_CASTS, _NAMED_DTYPES, TEXT_CASTS
from .models import (
    DERIVED_KINDS,
    ON_ABSENT_POLICIES,
    READ_AS_TYPES,
    SOURCE_FILE_FORMATS,
    SOURCE_KINDS,
    SOURCE_URL_FORMATS,
    SOURCE_URL_METHODS,
    UPLOAD_ID_PATTERN,
    BuildSpec,
    ColumnNullTokens,
    DerivedColumn,
    SourceRef,
)


@dataclass(frozen=True)
class ValidationProblem:
    """구조화된 검증 문제 (#417).

    code: 기계가 읽을 수 있는 원인 코드.
    path: 필드 경로 (예: sources[0].params.base_date).
    message: 사람이 읽을 수 있는 설명 (항상 포함 — 기존 호환).
    hint: 수정 제안 (선택).
    """

    code: str
    path: str
    message: str
    hint: str | None = None

    def __str__(self) -> str:
        return self.message


def _p(code: str, path: str, message: str, hint: str | None = None) -> ValidationProblem:
    return ValidationProblem(code=code, path=path, message=message, hint=hint)


def validate_spec(spec: BuildSpec) -> None:
    """BuildSpec의 최소 실행 가능성을 검증한다.

    예외:
        ValidationError: 하나 이상의 검증 규칙을 만족하지 못한 경우.
    """
    problems: list[ValidationProblem] = []
    if not spec.dataset_id.strip():
        problems.append(_p("empty_field", "dataset_id", "dataset_id must be a non-empty string"))
    if not spec.title.strip():
        problems.append(_p("empty_field", "title", "title must be a non-empty string"))
    if not spec.description.strip():
        problems.append(_p("empty_field", "description", "description must be a non-empty string"))
    if not spec.sources:
        problems.append(_p("missing_sources", "sources", "at least one source is required"))
    for i, source in enumerate(spec.sources):
        # provider/dataset은 kind="public_api"(기본값)에서만 의미가 있다 — 다른
        # kind의 필수/금지 field는 _source_kind_problems가 검증한다 (#498).
        if source.kind == "public_api":
            if not source.provider.strip():
                problems.append(
                    _p(
                        "empty_field",
                        f"sources[{i}].provider",
                        f"sources[{i}].provider must be a non-empty string",
                    )
                )
            if not source.dataset.strip():
                problems.append(
                    _p(
                        "empty_field",
                        f"sources[{i}].dataset",
                        f"sources[{i}].dataset must be a non-empty string",
                    )
                )
        if source.alias and not source.alias.strip():
            problems.append(
                _p(
                    "blank_alias",
                    f"sources[{i}].alias",
                    f"sources[{i}].alias must not be blank when provided",
                )
            )
    # exports may be empty (#703). A build with none ends at a committed table,
    # which is a complete job — publishing is an explicit follow-up, not the only
    # way to finish.
    for i, export in enumerate(spec.exports):
        if not export.output_path.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"exports[{i}].output_path",
                    f"exports[{i}].output_path must be a non-empty string",
                )
            )
        if export.kind not in EXPORTER_REGISTRY:
            supported = sorted(EXPORTER_REGISTRY)
            msg = f"exports[{i}].kind {export.kind!r} is not supported"
            problems.append(
                _p(
                    "unsupported_export_kind",
                    f"exports[{i}].kind",
                    msg,
                    hint=f"Use one of: {', '.join(supported)}",
                )
            )
    for key in spec.metadata:
        if not key.strip():
            problems.append(
                _p("empty_metadata_key", "metadata", "metadata keys must be non-empty strings")
            )
            break
    problems.extend(_split_problems(spec))
    problems.extend(_schema_problems(spec))
    problems.extend(_source_kind_problems(spec))
    problems.extend(_source_key_problems(spec))
    problems.extend(_param_grid_problems(spec))
    problems.extend(_pii_problems(spec))
    problems.extend(_license_problems(spec))
    problems.extend(_quality_problems(spec))
    problems.extend(_composition_problems(spec))
    if problems:
        raise ValidationError([str(p) for p in problems], structured=problems)


def _split_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """splits 정의의 검증 문제를 모은다."""
    split = spec.splits
    if split is None:
        return []
    problems: list[ValidationProblem] = []
    if split.mode == "ratio":
        if not split.ratios:
            problems.append(
                _p(
                    "missing_ratios",
                    "splits.ratios",
                    "splits.ratios must define at least one split",
                )
            )
        if any(not name.strip() for name in split.ratios):
            problems.append(
                _p(
                    "empty_ratio_name",
                    "splits.ratios",
                    "splits.ratios names must be non-empty strings",
                )
            )
        non_finite = [name for name, f in split.ratios.items() if not math.isfinite(f)]
        if non_finite:
            problems.append(
                _p("non_finite_ratio", "splits.ratios", f"non-finite values: {sorted(non_finite)}")
            )
        elif any(fraction <= 0 for fraction in split.ratios.values()):
            problems.append(
                _p("non_positive_ratio", "splits.ratios", "splits.ratios values must be positive")
            )
        else:
            total = sum(split.ratios.values())
            if split.ratios and abs(total - 1.0) > 1e-6:
                problems.append(
                    _p(
                        "ratio_sum_mismatch",
                        "splits.ratios",
                        f"splits.ratios must sum to 1.0 (got {total})",
                    )
                )
    elif split.mode == "key":
        if not split.key.strip():
            problems.append(
                _p(
                    "empty_split_key",
                    "splits.key",
                    "splits.key must be a non-empty column name for key mode",
                )
            )
    else:
        problems.append(
            _p(
                "unsupported_split_mode",
                "splits.mode",
                f"splits.mode {split.mode!r} is not supported; use 'ratio' or 'key'",
                hint="Use 'ratio' or 'key'",
            )
        )
    # 시계열 데이터 누수 검사 (#444). ratio 모드에서 key가 시간 컬럼이면
    # 랜덤 셔플이 미래 정보를 train 으로 색는다.
    _TIME_INDICATORS = (
        "date",
        "time",
        "dt",
        "year",
        "month",
        "day",
        "hour",
        "timestamp",
        "at",
        "ts",
    )
    if split.mode == "ratio" and split.key:
        key_lower = split.key.lower()
        if any(ind in key_lower for ind in _TIME_INDICATORS):
            problems.append(
                _p(
                    "time_column_random_split",
                    "splits",
                    f"splits.key {split.key!r} looks like a time column but mode is 'ratio' "
                    "— random split causes time-series data leakage (#444)",
                    hint="Use mode='key' for temporal splitting",
                )
            )
    return problems


def _schema_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """sources[].schema 계약 자체의 유효성을 검증한다 (#437).

    dtypes/casts 의 문자열이 ``_NAMED_DTYPES`` 키로 해석 가능한지 확인한다.
    로더(loader._parse_schema)는 구조만 검사하고, 여기서 의미를 검사한다 —
    알 수 없는 dtype 문자열이 런타임(정규화/검증)에서야 실패하는 것을 막는다.
    """
    problems: list[ValidationProblem] = []
    supported = sorted(_NAMED_DTYPES)
    supported_casts = sorted(set(_NAMED_DTYPES) | set(_FORMATTED_CASTS) | set(TEXT_CASTS))
    for i, source in enumerate(spec.sources):
        if source.schema is None:
            continue
        for col, dtype in source.schema.dtypes.items():
            if dtype.lower() not in _NAMED_DTYPES:
                problems.append(
                    _p(
                        "unknown_dtype",
                        f"sources[{i}].schema.dtypes.{col}",
                        f"unknown dtype {dtype!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(supported)}",
                    )
                )
        for col, cast in source.schema.casts.items():
            if cast.lower() not in supported_casts:
                problems.append(
                    _p(
                        "unknown_cast_dtype",
                        f"sources[{i}].schema.casts.{col}",
                        f"unknown cast dtype {cast!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(supported_casts)}",
                    )
                )
        for col, dtype in source.schema.read_as.items():
            if dtype not in READ_AS_TYPES:
                problems.append(
                    _p(
                        "unknown_read_as_type",
                        f"sources[{i}].schema.read_as.{col}",
                        f"unknown read_as type {dtype!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(READ_AS_TYPES)}",
                    )
                )
        problems.extend(
            _column_null_token_problems(source.schema.column_null_tokens, prefix=f"sources[{i}]")
        )
        problems.extend(_coalesce_problems(source.schema.coalesce, prefix=f"sources[{i}]"))
        problems.extend(_zfill_problems(source.schema.zfill, prefix=f"sources[{i}]"))
        problems.extend(_rename_problems(source.schema.rename, prefix=f"sources[{i}]"))
        problems.extend(
            _derived_problems(
                source.schema.derived,
                prefix=f"sources[{i}]",
                # derived 앞에 도는 선언들이 이름 지은 컬럼만 모은다. dtypes 는
                # 컬럼을 만들지 않고 기대 타입을 선언할 뿐이므로 뺀다 — 파생 컬럼의
                # dtype 을 선언하는 것은 정상적인 사용이다.
                reserved_names=set(source.schema.rename.values())
                | set(source.schema.casts)
                | set(source.schema.zfill)
                | set(source.schema.coalesce),
            )
        )
    return problems


def _column_null_token_problems(
    column_null_tokens: dict[str, ColumnNullTokens], *, prefix: str
) -> list[ValidationProblem]:
    """schema.column_null_tokens 선언 자체의 유효성을 검증한다 (#623).

    토큰이 비면 그 컬럼에 아무 일도 일어나지 않는다. 선언을 써 두고 동작하지 않는
    상태가 가장 나쁘다 — 결측이 값으로 남은 채 품질 지표가 그것을 세지 않는다.
    """
    problems: list[ValidationProblem] = []
    for column, rule in column_null_tokens.items():
        field = f"{prefix}.schema.column_null_tokens.{column}"
        if not rule.tokens:
            problems.append(
                _p(
                    "empty_column_null_tokens",
                    field,
                    f"column_null_tokens for column {column!r} declares no tokens",
                    hint="List the source spellings that mean 'missing' in this column",
                )
            )
        if rule.on_absent not in ON_ABSENT_POLICIES:
            problems.append(
                _p(
                    "unknown_on_absent_policy",
                    f"{field}.on_absent",
                    f"unknown on_absent policy {rule.on_absent!r} for column {column!r}",
                    hint=f"Use one of: {', '.join(ON_ABSENT_POLICIES)}",
                )
            )
    return problems


def _coalesce_problems(
    coalesce: dict[str, tuple[str, ...]], *, prefix: str
) -> list[ValidationProblem]:
    """schema.coalesce 선언 자체의 유효성을 검증한다 (#620).

    후보가 비면 normalize 가 런타임에 "후보가 하나도 없다"로 실패한다. 선언 시점에
    막는 편이 어디를 고쳐야 하는지 말해 준다.

    겹치는 그룹도 여기서 막는다. 각 규칙은 수렴한 후보 컬럼을 지우므로, 한 규칙의
    target 이 다른 규칙의 후보이면 결과가 매핑 순회 순서에 달린다. 그런데
    ``canonical_spec_mapping()`` 은 키를 정렬해 스냅샷을 쓰기 때문에, 같은 digest 의
    선언이 원래 빌드와 다르게 동작할 수 있다 — recipe 재현 가능성이 거기서 깨진다.
    """
    problems: list[ValidationProblem] = []
    targets = set(coalesce)
    owners: dict[str, str] = {}
    for target, candidates in coalesce.items():
        field = f"{prefix}.schema.coalesce.{target}"
        for candidate in candidates:
            if candidate in targets and candidate != target:
                problems.append(
                    _p(
                        "overlapping_coalesce_groups",
                        field,
                        f"coalesce target {candidate!r} is also a candidate of {target!r}",
                        hint=(
                            "Overlapping coalesce groups make the result depend on "
                            "declaration order; declare independent alias groups"
                        ),
                    )
                )
            owner = owners.setdefault(candidate, target)
            if owner != target:
                problems.append(
                    _p(
                        "overlapping_coalesce_groups",
                        field,
                        f"coalesce candidate {candidate!r} is claimed by both {owner!r} "
                        f"and {target!r}",
                        hint=(
                            "Overlapping coalesce groups make the result depend on "
                            "declaration order; declare independent alias groups"
                        ),
                    )
                )
    for target, candidates in coalesce.items():
        field = f"{prefix}.schema.coalesce.{target}"
        if not candidates:
            problems.append(
                _p(
                    "empty_coalesce_candidates",
                    field,
                    f"coalesce target {target!r} declares no candidate columns",
                    hint="List the source column names this canonical column is built from",
                )
            )
        if len(set(candidates)) != len(candidates):
            problems.append(
                _p(
                    "duplicate_coalesce_candidates",
                    field,
                    f"coalesce target {target!r} repeats a candidate column",
                )
            )
    return problems


def _zfill_problems(zfill: dict[str, int], *, prefix: str) -> list[ValidationProblem]:
    """schema.zfill 의 폭이 말이 되는지 검증한다 (#620)."""
    problems: list[ValidationProblem] = []
    for column, width in zfill.items():
        if width < 1:
            problems.append(
                _p(
                    "invalid_zfill_width",
                    f"{prefix}.schema.zfill.{column}",
                    f"zfill width for column {column!r} must be >= 1, got {width}",
                )
            )
    return problems


#: kind별로 요구하는 입력 컬럼 개수 (#611). None이면 1개 이상이면 된다.
_DERIVED_ARITY: dict[str, int | None] = {"date_parts": 3, "join_key": None}


def _rename_problems(rename: dict[str, str], *, prefix: str) -> list[ValidationProblem]:
    """schema.rename 의 대상 이름이 서로 겹치지 않는지 검증한다 (#611 후속).

    두 원 필드가 같은 canonical 이름으로 모이면 normalize_table 에서 Polars
    DuplicateError 로 터진다 — 선언 시점에 spec 용어로 막는다. (대상 이름이 rename
    되지 않는 *기존* 컬럼과 겹치는 경우는 원천을 봐야 알 수 있어 런타임 가드가 맡는다.)
    """
    problems: list[ValidationProblem] = []
    seen: dict[str, str] = {}
    for source_name, target in rename.items():
        if target in seen:
            problems.append(
                _p(
                    "duplicate_rename_target",
                    f"{prefix}.schema.rename.{source_name}",
                    f"rename target {target!r} is also the target of {seen[target]!r}",
                )
            )
        else:
            seen[target] = source_name
    return problems


def _derived_problems(
    derived: tuple[DerivedColumn, ...],
    *,
    prefix: str,
    reserved_names: set[str] | None = None,
) -> list[ValidationProblem]:
    """schema.derived 규칙의 kind 어휘, 컬럼 개수, 이름 충돌을 검증한다 (#611).

    normalize_table 은 date_parts 를 (year, month, day) 로 unpack 하므로, 개수가
    맞지 않으면 런타임에 ValueError 로 터진다. 선언 시점에 막는다.

    ``reserved_names`` 는 derived 앞 단계가 이미 이름 지은 컬럼명(rename 대상, casts/
    zfill/coalesce 키)이다. 파생 컬럼이 그 이름을 쓰면 with_columns 가 기존 컬럼을 소리 없이
    덮어쓴다 — 선언끼리 겹치는 것은 여기서, 원천 컬럼과 겹치는 것은 런타임 가드가 막는다.
    """
    problems: list[ValidationProblem] = []
    reserved = set(reserved_names or ())
    seen_names: set[str] = set()
    for index, rule in enumerate(derived):
        field = f"{prefix}.schema.derived[{index}]"
        if rule.name in reserved or rule.name in seen_names:
            problems.append(
                _p(
                    "derived_name_collision",
                    f"{field}.name",
                    f"derived column {rule.name!r} collides with a column already declared "
                    "in this schema (rename target, casts/zfill/coalesce key, or another "
                    "derived rule)",
                )
            )
        seen_names.add(rule.name)
        if rule.kind not in DERIVED_KINDS:
            problems.append(
                _p(
                    "unknown_derived_kind",
                    f"{field}.kind",
                    f"unknown derived kind {rule.kind!r} for column {rule.name!r}",
                    hint=f"Use one of: {', '.join(DERIVED_KINDS)}",
                )
            )
            continue
        arity = _DERIVED_ARITY[rule.kind]
        if arity is not None and len(rule.columns) != arity:
            problems.append(
                _p(
                    "derived_column_arity",
                    f"{field}.columns",
                    f"{rule.kind} requires exactly {arity} columns, got {len(rule.columns)}",
                )
            )
        elif arity is None and not rule.columns:
            problems.append(
                _p(
                    "derived_column_arity",
                    f"{field}.columns",
                    f"{rule.kind} requires at least one column",
                )
            )
    return problems


def _source_kind_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """kind vocabulary와 kind='file'/'url' source의 값·SSRF 관련 형태를 검증한다 (#498).

    ``kind`` 자체가 ``SOURCE_KINDS``(public_api/file/url) 밖이면 여기서
    거부한다 — loader(YAML 경로)는 이미 거부하지만, ``SourceRef(kind="ftp",
    ...)``처럼 loader를 거치지 않고 BuildSpec을 직접 구성하면 이 검사가 유일한
    방어선이다(fail-closed, #538 review). canonical source kind 계약은
    public_api | file | url 세 가지뿐이며, 알 수 없는 kind를 암묵적으로
    public_api처럼 처리하지 않는다.

    나머지는 loader가 검사하지 않는 의미 규칙(허용 format/encoding/method
    vocabulary, URL scheme/userinfo, upload_id 형태)만 다룬다. 실제 네트워크
    SSRF 방어(DNS resolve·redirect 재검증)는 fetch 시점(ingestion.url_fetch)의
    책임이다 — 여기서는 명백히 안전하지 않은 선언(scheme=http, userinfo 포함
    등)을 build 실행 전에 빠르게 거부한다.
    """
    problems: list[ValidationProblem] = []
    for i, source in enumerate(spec.sources):
        prefix = f"sources[{i}]"
        if source.kind not in SOURCE_KINDS:
            problems.append(
                _p(
                    "unsupported_source_kind",
                    f"{prefix}.kind",
                    f"{prefix}.kind {source.kind!r} is not supported; "
                    f"use one of {SOURCE_KINDS} (#498)",
                )
            )
        elif source.kind == "file":
            problems.extend(_file_source_problems(source, prefix))
        elif source.kind == "url":
            problems.extend(_url_source_problems(source, prefix))
    return problems


def _file_source_problems(source: SourceRef, prefix: str) -> list[ValidationProblem]:
    problems: list[ValidationProblem] = []
    if not source.upload_id.strip():
        problems.append(
            _p(
                "empty_field",
                f"{prefix}.upload_id",
                f"{prefix}.upload_id must be a non-empty string",
            )
        )
    elif not UPLOAD_ID_PATTERN.match(source.upload_id):
        problems.append(
            _p(
                "invalid_upload_id",
                f"{prefix}.upload_id",
                f"{prefix}.upload_id {source.upload_id!r} is not a valid upload identifier",
                hint="upload_id must come from POST /uploads",
            )
        )
    if source.format not in SOURCE_FILE_FORMATS:
        problems.append(
            _p(
                "unsupported_source_format",
                f"{prefix}.format",
                f"{prefix}.format {source.format!r} is not supported for kind='file'",
                hint=f"Use one of: {', '.join(SOURCE_FILE_FORMATS)}",
            )
        )
    try:
        codecs.lookup(source.encoding)
    except LookupError:
        problems.append(
            _p(
                "unknown_encoding",
                f"{prefix}.encoding",
                f"{prefix}.encoding {source.encoding!r} is not a known encoding",
            )
        )
    return problems


def _url_source_problems(source: SourceRef, prefix: str) -> list[ValidationProblem]:
    problems: list[ValidationProblem] = []
    if source.method not in SOURCE_URL_METHODS:
        problems.append(
            _p(
                "unsupported_source_method",
                f"{prefix}.method",
                f"{prefix}.method {source.method!r} is not supported (P0 supports GET only, #498)",
                hint=f"Use one of: {', '.join(SOURCE_URL_METHODS)}",
            )
        )
    if source.format and source.format not in SOURCE_URL_FORMATS:
        problems.append(
            _p(
                "unsupported_source_format",
                f"{prefix}.format",
                f"{prefix}.format {source.format!r} is not supported for kind='url'",
                hint=f"Use one of: {', '.join(SOURCE_URL_FORMATS)}",
            )
        )
    problems.extend(_endpoint_problems(source.endpoint, f"{prefix}.endpoint"))
    return problems


def _endpoint_problems(endpoint: str, path: str) -> list[ValidationProblem]:
    if not endpoint.strip():
        return [_p("empty_field", path, f"{path} must be a non-empty string")]
    parsed = urlsplit(endpoint)
    problems: list[ValidationProblem] = []
    if parsed.scheme != "https":
        problems.append(
            _p(
                "unsafe_url_scheme",
                path,
                f"{path} must use https (got {parsed.scheme or 'none'!r}) — SSRF policy, #498",
            )
        )
    if parsed.username or parsed.password:
        problems.append(
            _p(
                "url_userinfo_forbidden",
                path,
                f"{path} must not contain userinfo (SSRF policy, #498)",
            )
        )
    if not parsed.hostname:
        problems.append(_p("missing_url_host", path, f"{path} must include a host"))
    return problems


def _source_key_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """출력 키가 겹치거나 경로로 안전하지 않은 source 선언을 잡는다 (#630).

    산출물 디렉터리(``bronze/<key>/``, ``silver/<key>/``, ``gold/<key>/``)와
    ``row_counts``/``schema_summaries`` 의 키가 모두 이 값이다. 같은
    ``(provider, dataset)`` 을 params 만 달리해 alias 없이 두 번 선언하면 두
    source 가 같은 키를 갖는데, source 들은 스레드 풀에서 동시에 돌고 persist 는
    디렉터리를 통째로 교체하므로 한쪽 산출물이 사라진다. dict 키도 하나만
    살아남는다. 그런데 두 outcome 모두 "ok" 라서 **run 은 성공으로 보고되고
    데이터는 절반이 조용히 없어진다.**

    alias 는 경로 세그먼트가 된다. persist 의 ``validate_path_segment`` 가
    결국 잡기는 하지만 그때는 이미 fetch 를 마친 뒤다. 선언 단계에서 멈춘다.
    """
    # spec -> stages 최상위 import 는 순환이다(stages.bronze.resolve 가 spec 을
    # 읽는다). 키 계산을 여기서 복제하면 두 정의가 갈리므로(#629 가 정확히 그
    # 사고였다) 정의는 하나로 두고 import 만 미룬다.
    from ..stages._path_safety import validate_path_segment
    from ..stages.bronze.resolve import source_identity

    problems: list[ValidationProblem] = []
    first_index: dict[str, int] = {}
    for i, source in enumerate(spec.sources):
        if source.alias:
            try:
                validate_path_segment(source.alias, field_name=f"sources[{i}].alias")
            except ValueError as error:
                problems.append(
                    _p(
                        "unsafe_alias",
                        f"sources[{i}].alias",
                        str(error),
                        hint="alias becomes a directory name under the run workspace",
                    )
                )
                continue
            key = source.alias
        else:
            try:
                provider, dataset = source_identity(source)
            except (AttributeError, TypeError):
                # kind 별 필수 field 가 비었을 때다. 그쪽 문제는
                # _source_kind_problems 가 이미 보고하므로 여기서 겹쳐 말하지 않는다.
                continue
            key = f"{provider}.{dataset}"

        if key in first_index:
            problems.append(
                _p(
                    "duplicate_source_key",
                    f"sources[{i}]",
                    f"sources[{i}] resolves to the same output key {key!r} as "
                    f"sources[{first_index[key]}]; outputs would overwrite each other",
                    hint="give each source a distinct alias",
                )
            )
        else:
            first_index[key] = i
    return problems


def _param_grid_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """``param_grid`` 선언 자체의 유효성을 검증한다 (#613).

    전개는 데카르트 곱이라 선언 하나가 호출 수를 곱한다. 잘못된 선언이 런타임까지
    가면 이미 수백 번 호출한 뒤에 실패하므로, 선언 시점에 막는다.
    """
    problems: list[ValidationProblem] = []
    for i, source in enumerate(spec.sources):
        if not source.param_grid:
            continue
        prefix = f"sources[{i}].param_grid"
        if source.kind != "public_api":
            problems.append(
                _p(
                    "param_grid_not_supported",
                    prefix,
                    f"param_grid is only valid for kind='public_api', not {source.kind!r}",
                )
            )
            continue
        for key, values in source.param_grid.items():
            field = f"{prefix}.{key}"
            if not values:
                # 빈 축 하나가 데카르트 곱 전체를 0 으로 만든다 — 호출이 한 번도
                # 일어나지 않고 빈 Bronze 가 성공으로 기록된다.
                problems.append(
                    _p(
                        "empty_param_grid_axis",
                        field,
                        f"param_grid axis {key!r} has no values; the expansion would be empty",
                        hint="Remove the axis, or list the values it should iterate over",
                    )
                )
            if key in source.params:
                # 같은 키를 양쪽에 두면 어느 쪽이 이기는지 선언만 봐서는 알 수 없다.
                problems.append(
                    _p(
                        "param_grid_shadows_params",
                        field,
                        f"{key!r} is declared in both params and param_grid",
                        hint="params carries values shared by every combination; "
                        "keep the axis in only one place",
                    )
                )
            for index, value in enumerate(values):
                if isinstance(value, (dict, list)):
                    # 요청 파라미터는 스칼라다. 중첩 값은 URL 로 나갈 수 없다.
                    problems.append(
                        _p(
                            "invalid_param_grid_value",
                            f"{field}[{index}]",
                            f"param_grid values must be scalars, got {type(value).__name__}",
                        )
                    )
    return problems


def _pii_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """PII 정책의 명백한 위반을 사전에 검증한다 (#441).

    데이터 스캔 자체는 orchestrator 가 silver 이후에 수행한다. 여기서는 정책
    선언 단계의 위반(publish=true + allow 등)만 잡는다.
    """
    problems: list[ValidationProblem] = []
    if spec.pii is None:
        return problems
    # 공개 배포(publish=true) 스펙에서 allow 는 PII 미검출 통과 위험이 있어 금지.
    if spec.publish and spec.pii.mode == "allow":
        problems.append(
            _p(
                "pii_allow_with_publish",
                "pii.mode",
                "pii.mode='allow' is forbidden when publish=true (공개 배포 PII 노출 위험, #441)",
            )
        )
    return problems


def _quality_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """quality 정책의 의미적 일관성을 검증한다 (#486).

    구조/타입/severity 어휘/operator 어휘는 loader가 파싱 단계에서 이미 거부한다.
    여기서는 loader가 검사하지 않는 관계형 제약(범위 비어있음, min>max, 존재하지
    않는 컬럼을 겨냥한 severity override)만 다룬다.
    """
    problems: list[ValidationProblem] = []
    quality = spec.quality
    if quality is None:
        return problems
    for i, rule in enumerate(quality.range):
        path = f"quality.range[{i}]"
        if rule.min is None and rule.max is None:
            problems.append(
                _p(
                    "empty_range_rule",
                    path,
                    f"{path} must declare at least one of min/max",
                )
            )
        elif rule.min is not None and rule.max is not None and rule.min > rule.max:
            problems.append(
                _p(
                    "inverted_range_rule",
                    path,
                    f"{path}.min ({rule.min}) must be <= max ({rule.max})",
                )
            )
    unknown_severity_columns = sorted(
        set(quality.max_null_ratio_severity) - set(quality.max_null_ratio)
    )
    if unknown_severity_columns:
        problems.append(
            _p(
                "unknown_severity_column",
                "quality.max_null_ratio_severity",
                "quality.max_null_ratio_severity references columns not declared in "
                f"quality.max_null_ratio: {unknown_severity_columns}",
            )
        )
    return problems


def _license_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """publish=true 인 빌드의 라이선스 누락을 사전에 검증한다 (#443).

    kpubdata 가 라이선스 메타데이터를 제공하지 않으므로, 공개 배포 시 사용자
    명시 선언(``license`` 필드)이 재배포 가능성의 유일한 출처이다.
    """
    problems: list[ValidationProblem] = []
    if spec.publish and not spec.license:
        problems.append(
            _p(
                "missing_license_for_publish",
                "license",
                "license is required when publish=true (재배포 가능성 명시, #443)",
            )
        )
    return problems


def _composition_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """composition의 alias 참조/구조적 일관성을 검증한다 (#506).

    composition이 None이면 아무 것도 검사하지 않는다 — composition 없는 기존
    multi-source BuildSpec은 이 검증의 영향을 받지 않는다. join key 존재 여부와
    dtype 호환성은 Silver 스키마가 나와야 알 수 있으므로(파싱/구조 검증 시점엔
    미지) 여기서 다루지 않는다 — orchestrator의 빌드 파이프라인 검증 게이트가
    런타임에 담당한다.
    """
    problems: list[ValidationProblem] = []
    composition = spec.composition
    if composition is None:
        return problems

    if not composition.name.strip():
        problems.append(
            _p("empty_field", "composition.name", "composition.name must be a non-empty string")
        )
    else:
        existing_output_keys = {
            source.alias if source.alias else f"{source.provider}.{source.dataset}"
            for source in spec.sources
        }
        if composition.name in existing_output_keys:
            problems.append(
                _p(
                    "composition_name_collision",
                    "composition.name",
                    f"composition.name {composition.name!r} collides with an existing source "
                    "output key (alias or provider.dataset)",
                )
            )

    join = composition.join
    aliases = [source.alias for source in spec.sources if source.alias]
    duplicate_aliases = sorted({a for a in aliases if aliases.count(a) > 1})
    if duplicate_aliases:
        problems.append(
            _p(
                "duplicate_source_alias",
                "sources",
                "sources[].alias must be unique when composition is used; "
                f"duplicates: {duplicate_aliases}",
            )
        )

    if join.left and join.left == join.right:
        problems.append(
            _p(
                "self_join",
                "composition.join",
                "composition.join.left and right must reference different sources, "
                f"both are {join.left!r}",
            )
        )

    for side_name, alias in (("left", join.left), ("right", join.right)):
        if not alias.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"composition.join.{side_name}",
                    f"composition.join.{side_name} must be a non-empty string",
                )
            )
            continue
        if not any(source.alias == alias for source in spec.sources):
            problems.append(
                _p(
                    "unknown_composition_source",
                    f"composition.join.{side_name}",
                    f"composition.join.{side_name} {alias!r} does not match any sources[].alias "
                    "(a source referenced by composition must declare a non-empty alias)",
                )
            )

    if not join.left_key.strip():
        problems.append(
            _p(
                "empty_field",
                "composition.join.left_key",
                "composition.join.left_key must be a non-empty string",
            )
        )
    if not join.right_key.strip():
        problems.append(
            _p(
                "empty_field",
                "composition.join.right_key",
                "composition.join.right_key must be a non-empty string",
            )
        )

    return problems


__all__ = ["ValidationProblem", "validate_spec"]
