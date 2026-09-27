"""BuildSpec YAML 로딩·파싱 (Medallion 재구성: 기존 spec.py에서 분리).

이 모듈은 YAML 텍스트를 읽어 메모리 매핑으로 만든 뒤, models.py의 불변
데이터 클래스로 구조화한다. 타입/필수 키 검증 실패는 SpecLoadError로 변환한다.

주요 함수:
    - load_spec: YAML 파일 경로를 받아 BuildSpec으로 변환
    - parse_spec: 이미 로드된 매핑을 BuildSpec으로 파싱
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import yaml

from ..errors import SpecLoadError
from .models import (
    SOURCE_KINDS,
    BuildSpec,
    ColumnNullTokens,
    CompareColumnsRule,
    CompositionSpec,
    DerivedColumn,
    ExportTarget,
    JoinSpec,
    JsonValue,
    PiiPolicy,
    QualityPolicy,
    RangeRule,
    SchemaContract,
    SourceRef,
    SplitSpec,
)

# JoinSpec.type 허용 어휘 (#506). 초기 범위는 equi-join inner/left로 제한한다.
_JOIN_TYPES = ("inner", "left")

# JoinSpec.on_duplicate_key 허용 어휘 (#506). quality의 severity 관례를 재사용한다.
_JOIN_DUPLICATE_KEY_SEVERITIES = ("warn", "fail")

# quality.*_severity 값의 허용 어휘 (#486). 기존 threshold 위반은 "warn"이 기본이며,
# 명시적으로 "fail"을 선언해야 Gold 진입 전 소스가 실패한다.
_QUALITY_SEVERITIES = ("warn", "fail")

# quality.compare_columns[].operator 허용 어휘 (#486). 자유형 expression/eval은 금지하고
# 이 집합만 허용한다 — invalid operator는 파싱 단계에서 즉시 거부된다.
_COMPARE_COLUMNS_OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte")


def parse_spec(data: dict[str, object]) -> BuildSpec:
    """메모리 상의 매핑 데이터를 BuildSpec으로 파싱한다.

    매개변수:
        data: YAML 로더가 반환한 최상위 매핑.

    반환값:
        BuildSpec: 검증 가능한 빌드 명세 객체.

    예외:
        SpecLoadError: 필드 타입이 맞지 않거나 필수 키가 없을 때.
    """
    try:
        dataset_id = _require_string(data, "dataset_id")
        title = _require_string(data, "title")
        description = _require_string(data, "description")
        # transforms 필드는 제거됨 (#438). VAL-1 의 sources[].schema.casts 가 대체.
        # 조용히 무시하지 않고 명시적 에러로 사용자에게 알린다.
        if "transforms" in data:
            raise ValueError("'transforms' is removed; use sources[].schema.casts instead (#438)")
        if "normalization_mode" in data:
            raise ValueError("'normalization_mode' is removed; use sources[].schema instead (#438)")
        metadata = _parse_json_mapping(data.get("metadata", {}), field_name="metadata")
        publish = _parse_bool(data.get("publish", False), field_name="publish")
        sources = _parse_sources(_require_present(data, "sources"))
        # exports is optional (#703). A warehouse build that only materialises a
        # table is a complete job, and requiring an export target made the common
        # case pay for the rare one — a local analysis had to declare where to
        # publish before it could finish.
        exports = _parse_exports(data.get("exports", []))
        splits = _parse_splits(data.get("splits"))
        pii = _parse_pii(data.get("pii"))
        license_obj = data.get("license")
        if license_obj is not None and not isinstance(license_obj, str):
            raise TypeError("license must be a string")
        attribution_obj = data.get("attribution")
        if attribution_obj is not None and not isinstance(attribution_obj, str):
            raise TypeError("attribution must be a string")
        quality = _parse_quality(data.get("quality"))
        composition = _parse_composition(data.get("composition"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecLoadError(f"Failed to parse build spec: {exc}") from exc

    return BuildSpec(
        dataset_id=dataset_id,
        title=title,
        description=description,
        sources=sources,
        exports=exports,
        metadata=metadata,
        publish=publish,
        splits=splits,
        pii=pii,
        license=license_obj,
        attribution=attribution_obj,
        quality=quality,
        composition=composition,
    )


def load_spec(path: Path) -> BuildSpec:
    """YAML 파일을 읽어 BuildSpec으로 변환한다.

    매개변수:
        path: BuildSpec YAML 경로.

    반환값:
        BuildSpec: 로드된 명세 객체.

    예외:
        SpecLoadError: 파일 읽기, YAML 파싱, 최상위 구조 검증 실패 시.
    """
    try:
        raw_data = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"Failed to load build spec from {path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise SpecLoadError(
            f"Failed to parse build spec from {path}: top-level YAML must be a mapping"
        )

    return parse_spec(cast(dict[str, object], raw_data))


def _require_string(data: dict[str, object], key: str, *, prefix: str = "") -> str:
    """필수 문자열 필드를 추출한다."""
    label = f"{prefix}.{key}" if prefix else key
    if key not in data:
        raise KeyError(f"{label} is required")
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _require_present(data: dict[str, object], key: str) -> object:
    if key not in data:
        raise KeyError(f"{key} is required")
    return data[key]


def _parse_bool(value: object, *, field_name: str) -> bool:
    """불리언 필드 타입을 검증한다."""
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _parse_string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    """문자열 목록 필드를 불변 튜플로 변환한다."""
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")

    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{field_name} entries must be strings")
    return tuple(cast(str, item) for item in items)


def _parse_string_dict(value: object, *, field_name: str) -> dict[str, JsonValue]:
    """문자열 키/값 매핑을 검증하고 새 dict로 복사한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, JsonValue] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"{field_name} entries must be string pairs")
        parsed[key] = item
    return parsed


def _validate_json_value(
    value: object, *, field_name: str, _ancestors: frozenset[int] = frozenset()
) -> JsonValue:
    """값이 JSON 프리미티브/컨테이너인지 재귀적으로 검증한다.

    YAML anchor/alias로 만들어진 순환 구조(예: ``a: &x {self: *x}``)를 만나면
    무한 재귀로 ``RecursionError`` crash가 발생할 수 있다. 현재 재귀 경로상의
    컨테이너 ``id()``를 추적해 순환을 감지하면 ``ValueError``로 명확히 실패한다
    (load_spec이 이를 SpecLoadError로 감싼다) (#169).
    """
    if isinstance(value, float) and not math.isfinite(value):
        # NaN/Infinity는 json.dumps가 비표준 토큰(NaN/Infinity)으로 직렬화하므로,
        # 표준 JSON 계약을 깨지 않도록 비유한 float를 전역적으로 거부한다 (#201).
        raise ValueError(f"{field_name} must be a finite number, got {value!r}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        marker = id(value)
        if marker in _ancestors:
            raise ValueError(f"{field_name} contains a circular reference")
        child_ancestors = _ancestors | {marker}
        if isinstance(value, list):
            return [
                _validate_json_value(
                    item, field_name=f"{field_name}[{i}]", _ancestors=child_ancestors
                )
                for i, item in enumerate(value)
            ]
        result: dict[str, JsonValue] = {}
        for k, v in cast(dict[object, object], value).items():
            if not isinstance(k, str):
                raise TypeError(f"{field_name} keys must be strings, got {type(k).__name__}")
            result[k] = _validate_json_value(
                v, field_name=f"{field_name}.{k}", _ancestors=child_ancestors
            )
        return result
    raise TypeError(
        f"{field_name} contains non-JSON value of type {type(value).__name__}: {value!r}"
    )


def _parse_json_mapping(value: object, *, field_name: str) -> dict[str, JsonValue]:
    """JSON 호환 값만 담는 매핑 필드를 검증한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, JsonValue] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = _validate_json_value(item, field_name=f"{field_name}.{key}")
    return parsed


def _parse_param_grid(value: object, *, field_name: str) -> dict[str, tuple[JsonValue, ...]]:
    """``param_grid`` 를 키별 값 튜플로 파싱한다 (#613).

    각 값은 **리스트여야 한다.** 스칼라를 허용하면 "값 하나짜리 축" 과 "공통
    파라미터" 가 구문상 구분되지 않고, 그 둘은 의미가 다르다 — 후자는 ``params``
    가 표현한다. 비었는지/문자열인지 같은 의미 검증은 validator 가 맡는다.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, tuple[JsonValue, ...]] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        if not isinstance(item, list):
            raise TypeError(
                f"{field_name}.{key} must be a list of values; "
                "a single shared value belongs in params"
            )
        values = cast(list[object], item)
        parsed[key] = tuple(
            _validate_json_value(entry, field_name=f"{field_name}.{key}[{i}]")
            for i, entry in enumerate(values)
        )
    return parsed


# kind별로만 유효한 field들 (#498). 서로 다른 kind의 field가 섞인 source는 명백한
# 계약 오류이므로 loader가 즉시 거부한다 — "kind=file인데 provider도 있음" 같은
# 모호한 spec을 조용히 부분 해석하지 않는다.
_PUBLIC_API_ONLY_FIELDS: tuple[str, ...] = ("upload_id", "format", "encoding", "endpoint", "method")
_FILE_ONLY_FIELDS: tuple[str, ...] = (
    "provider",
    "dataset",
    "params",
    "param_grid",
    "endpoint",
    "method",
)
_URL_ONLY_FIELDS: tuple[str, ...] = (
    "provider",
    "dataset",
    "params",
    "param_grid",
    "upload_id",
    "encoding",
)


def _reject_foreign_fields(
    mapping: dict[str, object], forbidden: tuple[str, ...], *, prefix: str, kind: str
) -> None:
    present = sorted(name for name in forbidden if name in mapping)
    if present:
        raise TypeError(f"{prefix}: fields {present} are not valid for kind={kind!r} (#498)")


def _parse_sources(value: object) -> tuple[SourceRef, ...]:
    """sources 배열을 SourceRef 튜플로 변환한다 (#498).

    ``kind`` 로 public_api(기본, 하위 호환)/file/url을 구분해 서로 다른 field
    조합을 파싱한다. ``kind`` 를 생략한 기존 source는 항상 ``public_api`` 로
    해석된다 — 기존 Public API BuildSpec은 재작성 없이 그대로 동작한다.
    """
    if not isinstance(value, list):
        raise TypeError("sources must be a list")
    if not value:
        raise ValueError("sources must not be empty")

    items = cast(list[object], value)
    parsed_sources: list[SourceRef] = []
    for index, item in enumerate(items):
        prefix = f"sources[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        # normalization_mode 필드는 제거됨 (#438). sources[].schema 가 대체.
        # 조용히 무시하지 않고 명시적 에러로 알린다.
        if "normalization_mode" in mapping:
            raise TypeError(
                f"sources[{index}].normalization_mode is removed; "
                "use sources[].schema instead (#438)"
            )
        kind_obj = mapping.get("kind", "public_api")
        if not isinstance(kind_obj, str):
            raise TypeError(f"{prefix}.kind must be a string")
        if kind_obj not in SOURCE_KINDS:
            raise ValueError(
                f"{prefix}.kind {kind_obj!r} is not supported; use one of {SOURCE_KINDS} (#498)"
            )
        alias_obj = mapping.get("alias", "")
        if not isinstance(alias_obj, str):
            raise TypeError(f"sources[{index}].alias must be a string")
        schema_obj = mapping.get("schema")
        schema = _parse_schema(schema_obj, prefix=prefix) if schema_obj is not None else None

        if kind_obj == "file":
            parsed_sources.append(
                _parse_file_source(mapping, index=index, alias=alias_obj, schema=schema)
            )
        elif kind_obj == "url":
            parsed_sources.append(
                _parse_url_source(mapping, index=index, alias=alias_obj, schema=schema)
            )
        else:
            parsed_sources.append(
                _parse_public_api_source(mapping, index=index, alias=alias_obj, schema=schema)
            )
    return tuple(parsed_sources)


def _parse_public_api_source(
    mapping: dict[str, object], *, index: int, alias: str, schema: SchemaContract | None
) -> SourceRef:
    prefix = f"sources[{index}]"
    _reject_foreign_fields(mapping, _PUBLIC_API_ONLY_FIELDS, prefix=prefix, kind="public_api")
    provider = _require_string(mapping, "provider", prefix=prefix)
    dataset = _require_string(mapping, "dataset", prefix=prefix)
    params = _parse_json_mapping(mapping.get("params", {}), field_name=f"{prefix}.params")
    param_grid = _parse_param_grid(mapping.get("param_grid", {}), field_name=f"{prefix}.param_grid")
    return SourceRef(
        provider=provider,
        dataset=dataset,
        params=params,
        param_grid=param_grid,
        alias=alias,
        schema=schema,
        kind="public_api",
    )


def _parse_file_source(
    mapping: dict[str, object], *, index: int, alias: str, schema: SchemaContract | None
) -> SourceRef:
    """kind='file' source를 파싱한다 (#498). 값 vocabulary(허용 format/encoding
    존재 여부, upload_id 형태)의 의미 검증은 validator.py가 담당한다."""
    prefix = f"sources[{index}]"
    _reject_foreign_fields(mapping, _FILE_ONLY_FIELDS, prefix=prefix, kind="file")
    upload_id = _require_string(mapping, "upload_id", prefix=prefix)
    format_value = _require_string(mapping, "format", prefix=prefix)
    encoding_obj = mapping.get("encoding", "utf-8")
    if not isinstance(encoding_obj, str) or not encoding_obj.strip():
        raise TypeError(f"{prefix}.encoding must be a non-empty string")
    return SourceRef(
        alias=alias,
        schema=schema,
        kind="file",
        upload_id=upload_id,
        format=format_value,
        encoding=encoding_obj,
    )


def _parse_url_source(
    mapping: dict[str, object], *, index: int, alias: str, schema: SchemaContract | None
) -> SourceRef:
    """kind='url' source를 파싱한다 (#498). scheme/userinfo/method vocabulary 같은
    SSRF 관련 의미 검증은 validator.py가 담당한다 — 여기서는 구조만 본다."""
    prefix = f"sources[{index}]"
    _reject_foreign_fields(mapping, _URL_ONLY_FIELDS, prefix=prefix, kind="url")
    endpoint = _require_string(mapping, "endpoint", prefix=prefix)
    method_obj = mapping.get("method", "GET")
    if not isinstance(method_obj, str) or not method_obj.strip():
        raise TypeError(f"{prefix}.method must be a non-empty string")
    format_obj = mapping.get("format", "")
    if not isinstance(format_obj, str):
        raise TypeError(f"{prefix}.format must be a string")
    return SourceRef(
        alias=alias,
        schema=schema,
        kind="url",
        endpoint=endpoint,
        method=method_obj,
        format=format_obj,
    )


def _parse_schema(value: object, *, prefix: str) -> SchemaContract:
    """sources[].schema 매핑을 SchemaContract로 변환한다 (#437).

    required/dtypes/casts 세 필드를 파싱한다. dtype/cast 값은 문자열이어야 하고,
    실제 polars dtype 으로 해석 가능한지는 validator.py 가 검증한다 (로더는 구조만).
    """
    mapping = _ensure_mapping(value, field_name=f"{prefix}.schema")
    required = _parse_string_list(
        mapping.get("required", []), field_name=f"{prefix}.schema.required"
    )
    dtypes = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("dtypes", {}), field_name=f"{prefix}.schema.dtypes"),
    )
    casts = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("casts", {}), field_name=f"{prefix}.schema.casts"),
    )
    rename = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("rename", {}), field_name=f"{prefix}.schema.rename"),
    )
    derived = _parse_derived(mapping.get("derived", []), prefix=f"{prefix}.schema.derived")
    read_as = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("read_as", {}), field_name=f"{prefix}.schema.read_as"),
    )
    null_tokens = _parse_string_list(
        mapping.get("null_tokens", []), field_name=f"{prefix}.schema.null_tokens"
    )
    column_null_tokens = _parse_column_null_tokens(
        mapping.get("column_null_tokens", {}), prefix=f"{prefix}.schema.column_null_tokens"
    )
    coalesce = _parse_coalesce(mapping.get("coalesce", {}), prefix=f"{prefix}.schema.coalesce")
    zfill = _parse_zfill(mapping.get("zfill", {}), prefix=f"{prefix}.schema.zfill")
    return SchemaContract(
        required=required,
        dtypes=dtypes,
        casts=casts,
        rename=rename,
        derived=derived,
        read_as=read_as,
        null_tokens=null_tokens,
        column_null_tokens=column_null_tokens,
        coalesce=coalesce,
        zfill=zfill,
    )


def _parse_column_null_tokens(value: object, *, prefix: str) -> dict[str, ColumnNullTokens]:
    """schema.column_null_tokens 를 파싱한다 (#623).

    두 표기를 받는다. 목록만 쓰면 ``on_absent`` 는 기본값 ``"error"`` 다.

        column_null_tokens:
          foo: ["", "NA"]
          bar:
            tokens: [""]
            on_absent: ignore

    ``on_absent`` 어휘의 검증은 validator.py 가 한다 — 로더는 구조만 본다.
    """
    mapping = _ensure_mapping(value, field_name=prefix)
    parsed: dict[str, ColumnNullTokens] = {}
    for column, declaration in mapping.items():
        field = f"{prefix}.{column}"
        if isinstance(declaration, dict):
            unknown = set(declaration) - {"tokens", "on_absent"}
            if unknown:
                raise TypeError(f"{field} has unknown keys: {sorted(unknown)}")
            tokens = _parse_string_list(declaration.get("tokens", []), field_name=f"{field}.tokens")
            on_absent = declaration.get("on_absent", "error")
            if not isinstance(on_absent, str):
                raise TypeError(f"{field}.on_absent must be a string")
        else:
            tokens = _parse_string_list(declaration, field_name=field)
            on_absent = "error"
        parsed[column] = ColumnNullTokens(tokens=tokens, on_absent=on_absent)
    return parsed


def _parse_coalesce(value: object, *, prefix: str) -> dict[str, tuple[str, ...]]:
    """``{이름: (문자열, ...)}`` 형태의 선언을 파싱한다 (#620, #623).

    schema.coalesce 와 schema.column_null_tokens 가 같은 모양이라 함께 쓴다.

    구조만 검사한다 — 후보가 비었는지 같은 의미 검증은 validator.py 가 한다.
    """
    mapping = _ensure_mapping(value, field_name=prefix)
    parsed: dict[str, tuple[str, ...]] = {}
    for target, candidates in mapping.items():
        parsed[target] = _parse_string_list(candidates, field_name=f"{prefix}.{target}")
    return parsed


def _parse_zfill(value: object, *, prefix: str) -> dict[str, int]:
    """schema.zfill 을 ``{컬럼: 폭}`` 으로 변환한다 (#620)."""
    mapping = _ensure_mapping(value, field_name=prefix)
    parsed: dict[str, int] = {}
    for column, width in mapping.items():
        if not isinstance(width, int) or isinstance(width, bool):
            raise TypeError(f"{prefix}.{column} must be an integer width")
        parsed[column] = width
    return parsed


def _parse_derived(value: object, *, prefix: str) -> tuple[DerivedColumn, ...]:
    """schema.derived 배열을 DerivedColumn 튜플로 변환한다 (#611).

    구조만 검사한다 — kind 어휘와 컬럼 개수의 의미 검증은 validator.py 가 한다.
    """
    if not isinstance(value, list):
        raise TypeError(f"{prefix} must be a list")
    rules: list[DerivedColumn] = []
    for index, item in enumerate(cast(list[object], value)):
        mapping = _ensure_mapping(item, field_name=f"{prefix}[{index}]")
        name = mapping.get("name")
        kind = mapping.get("kind")
        if not isinstance(name, str) or not name:
            raise TypeError(f"{prefix}[{index}].name must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise TypeError(f"{prefix}[{index}].kind must be a non-empty string")
        columns = _parse_string_list(
            mapping.get("columns", []), field_name=f"{prefix}[{index}].columns"
        )
        rules.append(DerivedColumn(name=name, kind=kind, columns=columns))
    return tuple(rules)


def _parse_exports(value: object) -> tuple[ExportTarget, ...]:
    """Convert the exports array into a tuple of ExportTarget.

    An empty list is valid (#703): the build then ends at a materialised table
    instead of at an exported artifact.
    """
    if not isinstance(value, list):
        raise TypeError("exports must be a list")

    items = cast(list[object], value)
    parsed_exports: list[ExportTarget] = []
    for index, item in enumerate(items):
        prefix = f"exports[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        kind = _require_string(mapping, "kind", prefix=prefix)
        output_path = _require_string(mapping, "output_path", prefix=prefix)
        options = _parse_json_mapping(
            mapping.get("options", {}), field_name=f"exports[{index}].options"
        )
        parsed_exports.append(ExportTarget(kind=kind, output_path=output_path, options=options))
    return tuple(parsed_exports)


def _parse_splits(value: object) -> SplitSpec | None:
    """splits 매핑을 SplitSpec으로 변환한다(없으면 None)."""
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="splits")
    mode = _require_string(mapping, "mode", prefix="splits")

    seed_obj = mapping.get("seed", 0)
    if not isinstance(seed_obj, int) or isinstance(seed_obj, bool):
        raise TypeError("splits.seed must be an integer")

    ratios: dict[str, float] = {}
    ratios_obj = mapping.get("ratios", {})
    if not isinstance(ratios_obj, dict):
        raise TypeError("splits.ratios must be a mapping")
    for name, fraction in cast(dict[object, object], ratios_obj).items():
        if not isinstance(name, str):
            raise TypeError("splits.ratios keys must be strings")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise TypeError("splits.ratios values must be numbers")
        ratios[name] = float(fraction)

    key_obj = mapping.get("key", "")
    if not isinstance(key_obj, str):
        raise TypeError("splits.key must be a string")

    return SplitSpec(mode=mode, ratios=ratios, key=key_obj, seed=seed_obj)


def _ensure_mapping(value: object, *, field_name: str) -> dict[str, object]:
    """문자열 키를 가진 매핑인지 확인하고 복사본을 반환한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, object] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = item
    return parsed


def _parse_pii(value: object) -> PiiPolicy | None:
    """pii 매핑을 PiiPolicy로 변환한다 (없으면 None, #441).

    mode 는 block(기본)/warn/allow 중 하나. allow_columns 는 오탐 해제용 컬럼 목록.
    """
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="pii")
    mode_obj = mapping.get("mode", "block")
    if not isinstance(mode_obj, str):
        raise TypeError("pii.mode must be a string")
    if mode_obj not in ("block", "warn", "allow"):
        raise ValueError(f"pii.mode must be one of block/warn/allow, got {mode_obj!r}")
    allow_columns = _parse_string_list(
        mapping.get("allow_columns", []), field_name="pii.allow_columns"
    )
    return PiiPolicy(mode=mode_obj, allow_columns=allow_columns)


def _parse_severity(value: object, *, field_name: str) -> str:
    """quality severity 값("warn"/"fail")을 검증한다 (#486)."""
    if not isinstance(value, str) or value not in _QUALITY_SEVERITIES:
        raise ValueError(f"{field_name} must be one of {_QUALITY_SEVERITIES}, got {value!r}")
    return value


def _parse_severity_map(value: object, *, field_name: str) -> dict[str, str]:
    """컬럼별 severity override 매핑을 검증한다 (#486)."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for k, v in cast(dict[object, object], value).items():
        if not isinstance(k, str):
            raise TypeError(f"{field_name} keys must be strings")
        result[k] = _parse_severity(v, field_name=f"{field_name}.{k}")
    return result


def _parse_optional_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    return float(value)


def _parse_range_rules(value: object) -> tuple[RangeRule, ...]:
    """quality.range 배열을 RangeRule 튜플로 변환한다 (#486)."""
    if not isinstance(value, list):
        raise TypeError("quality.range must be a list")
    rules: list[RangeRule] = []
    for index, item in enumerate(cast(list[object], value)):
        prefix = f"quality.range[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        column = _require_string(mapping, "column", prefix=prefix)
        min_value = _parse_optional_number(mapping.get("min"), field_name=f"{prefix}.min")
        max_value = _parse_optional_number(mapping.get("max"), field_name=f"{prefix}.max")
        severity = _parse_severity(mapping.get("severity", "warn"), field_name=f"{prefix}.severity")
        rules.append(RangeRule(column=column, min=min_value, max=max_value, severity=severity))
    return tuple(rules)


def _parse_compare_columns_rules(value: object) -> tuple[CompareColumnsRule, ...]:
    """quality.compare_columns 배열을 CompareColumnsRule 튜플로 변환한다 (#486).

    자유형 expression/eval은 금지하고, operator는 ``_COMPARE_COLUMNS_OPERATORS``만
    허용한다 — invalid operator는 여기서 즉시 거부된다.
    """
    if not isinstance(value, list):
        raise TypeError("quality.compare_columns must be a list")
    rules: list[CompareColumnsRule] = []
    for index, item in enumerate(cast(list[object], value)):
        prefix = f"quality.compare_columns[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        left = _require_string(mapping, "left", prefix=prefix)
        right = _require_string(mapping, "right", prefix=prefix)
        operator = _require_string(mapping, "operator", prefix=prefix)
        if operator not in _COMPARE_COLUMNS_OPERATORS:
            raise ValueError(
                f"{prefix}.operator must be one of {_COMPARE_COLUMNS_OPERATORS}, got {operator!r}"
            )
        severity = _parse_severity(mapping.get("severity", "warn"), field_name=f"{prefix}.severity")
        rules.append(
            CompareColumnsRule(left=left, operator=operator, right=right, severity=severity)
        )
    return tuple(rules)


def _parse_quality(value: object) -> QualityPolicy | None:
    """quality 매핑을 QualityPolicy로 변환한다 (없으면 None, #446/#486).

    max_duplicate_rate/min_rows 는 스칼라, max_null_ratio 는 {컬럼명: 비율} 매핑
    — 기존 #446 syntax 그대로다. ``*_severity`` 필드(#486)는 선택이며 생략 시
    "warn"이 기본이다. ``range``/``compare_columns``는 #486에서 추가된 typed
    확장 규칙이다.
    """
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="quality")
    max_dup = mapping.get("max_duplicate_rate")
    if max_dup is not None and (not isinstance(max_dup, (int, float)) or isinstance(max_dup, bool)):
        raise TypeError("quality.max_duplicate_rate must be a number")
    max_dup_severity = _parse_severity(
        mapping.get("max_duplicate_rate_severity", "warn"),
        field_name="quality.max_duplicate_rate_severity",
    )
    min_rows = mapping.get("min_rows")
    if min_rows is not None and (not isinstance(min_rows, int) or isinstance(min_rows, bool)):
        raise TypeError("quality.min_rows must be an integer")
    min_rows_severity = _parse_severity(
        mapping.get("min_rows_severity", "warn"), field_name="quality.min_rows_severity"
    )
    null_ratio_raw = mapping.get("max_null_ratio", {})
    if not isinstance(null_ratio_raw, dict):
        raise TypeError("quality.max_null_ratio must be a mapping")
    max_null_ratio: dict[str, float] = {}
    for k, v in cast(dict[object, object], null_ratio_raw).items():
        if not isinstance(k, str) or not isinstance(v, (int, float)) or isinstance(v, bool):
            raise TypeError("quality.max_null_ratio entries must be string→number pairs")
        max_null_ratio[k] = float(v)
    max_null_ratio_severity = _parse_severity_map(
        mapping.get("max_null_ratio_severity", {}), field_name="quality.max_null_ratio_severity"
    )
    range_rules = _parse_range_rules(mapping.get("range", []))
    compare_columns_rules = _parse_compare_columns_rules(mapping.get("compare_columns", []))
    return QualityPolicy(
        max_duplicate_rate=float(max_dup) if max_dup is not None else None,
        max_duplicate_rate_severity=max_dup_severity,
        max_null_ratio=max_null_ratio,
        max_null_ratio_severity=max_null_ratio_severity,
        min_rows=min_rows,
        min_rows_severity=min_rows_severity,
        range=range_rules,
        compare_columns=compare_columns_rules,
    )


def _parse_join(value: object) -> JoinSpec:
    """composition.join 매핑을 JoinSpec으로 변환한다 (#506).

    left/right/left_key/right_key는 필수 문자열이다. type/on_duplicate_key는
    고정 어휘 필드라 pii.mode와 동일하게 여기서 즉시 검증한다 — left/right가
    실제 sources[].alias와 대응하는지, join key가 실제로 존재/호환되는지는
    구조가 아니라 값 검증이라 validator/orchestrator가 각각 담당한다.
    """
    mapping = _ensure_mapping(value, field_name="composition.join")
    left = _require_string(mapping, "left", prefix="composition.join")
    right = _require_string(mapping, "right", prefix="composition.join")
    left_key = _require_string(mapping, "left_key", prefix="composition.join")
    right_key = _require_string(mapping, "right_key", prefix="composition.join")

    join_type = mapping.get("type", "inner")
    if not isinstance(join_type, str) or join_type not in _JOIN_TYPES:
        raise ValueError(f"composition.join.type must be one of {_JOIN_TYPES}, got {join_type!r}")

    on_duplicate_key = mapping.get("on_duplicate_key", "warn")
    if (
        not isinstance(on_duplicate_key, str)
        or on_duplicate_key not in _JOIN_DUPLICATE_KEY_SEVERITIES
    ):
        raise ValueError(
            "composition.join.on_duplicate_key must be one of "
            f"{_JOIN_DUPLICATE_KEY_SEVERITIES}, got {on_duplicate_key!r}"
        )

    return JoinSpec(
        left=left,
        right=right,
        left_key=left_key,
        right_key=right_key,
        type=join_type,
        on_duplicate_key=on_duplicate_key,
    )


def _parse_composition(value: object) -> CompositionSpec | None:
    """composition 매핑을 CompositionSpec으로 변환한다 (없으면 None, #506)."""
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="composition")
    name = _require_string(mapping, "name", prefix="composition")
    join = _parse_join(_require_present(mapping, "join"))
    return CompositionSpec(name=name, join=join)


__all__ = [
    "load_spec",
    "parse_spec",
]
