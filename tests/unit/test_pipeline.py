"""Pipeline orchestrator(#48): Bronze→Silver→Gold 실행·워크스페이스·manifest 검증."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import polars as pl
import pytest
import yaml

import kpubdata_builder.pipeline.orchestrator as orchestrator
from kpubdata_builder.pipeline import BuildContext, BuildResult, run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef, parse_spec


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items: list[dict[str, JsonValue]] = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items: list[dict[str, JsonValue]] = items

    def list(self, **_params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakePaginatedDataset(_FakeDataset):
    def __init__(self, pages: tuple[list[dict[str, JsonValue]], ...]) -> None:
        super().__init__(pages[0])
        self._pages = pages

    def list_all(self, **_params: JsonValue) -> Iterable[_FakeResult]:
        return (_FakeResult(page) for page in self._pages)


class _FakeClient:
    """source_key → 레코드 매핑을 돌려주는 테스트용 클라이언트."""

    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data: dict[str, list[dict[str, JsonValue]]] = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


class _FakePaginatedClient:
    def __init__(self, data: dict[str, tuple[list[dict[str, JsonValue]], ...]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakePaginatedDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakePaginatedDataset(self._data[source_key])


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def test_build_context_create_validates_and_defaults_run_id(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))

    ctx = BuildContext.create(spec, output_root=tmp_path)

    assert ctx.run_id  # 비어 있지 않은 기본 run_id
    assert ctx.output_root == tmp_path
    assert ctx.spec is spec


def test_run_build_executes_full_pipeline_and_writes_workspace(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert isinstance(result, BuildResult)
    assert result.status == "ok"
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.source_key == "datago.apt_trade"
    assert outcome.status == "ok"
    assert outcome.stages_completed == ("bronze", "silver", "gold")

    # run workspace 디렉터리 구조
    run_dir = tmp_path / "run1"
    assert (run_dir / "buildspec.yaml").is_file()
    assert (run_dir / "bronze").is_dir()
    assert (run_dir / "silver").is_dir()
    assert (run_dir / "gold").is_dir()

    # manifest 기록
    assert result.manifest_path.exists()
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["build_id"] == "run1"
    inputs = cast(list[str], manifest["inputs"])
    assert "datago.apt_trade" in inputs
    outputs = cast(list[str], manifest["outputs"])
    assert str(run_dir / "silver" / "datago.apt_trade" / "schema.json") in outputs
    assert str(run_dir / "silver" / "datago.apt_trade" / "stats.json") in outputs
    assert str(run_dir / "silver" / "datago.apt_trade" / "preview.json") in outputs
    assert str(run_dir / "silver" / "datago.apt_trade" / "validation.json") in outputs
    assert str(run_dir / "gold" / "datago.apt_trade" / "package.json") in outputs

    # gold parquet 산출
    gold_parquet = run_dir / "gold" / "datago.apt_trade" / "table.parquet"
    assert gold_parquet.exists()
    assert pl.read_parquet(gold_parquet).to_dicts() == [
        {"id": "1", "amount": 1000},
        {"id": "2", "amount": 2500},
    ]


def test_run_build_finishes_without_any_export_target(tmp_path: Path) -> None:
    """A build with no exports is a complete job, not a misconfiguration (#703).

    The product promise is "collect Korean public data into my own environment and
    analyse it with SQL", and that promise is kept without publishing anything.
    Requiring an export target made the common case pay for the rare one: a local
    analysis had to declare where to publish before the build would finish.
    """
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(),
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run-materialise")

    assert result.status == "ok"
    assert result.outcomes[0].stages_completed == ("bronze", "silver", "gold")

    # The materialised table is there, which is the point of the run.
    gold_parquet = tmp_path / "run-materialise" / "gold" / "datago.apt_trade" / "table.parquet"
    assert gold_parquet.exists()
    assert pl.read_parquet(gold_parquet).to_dicts() == [{"id": "1", "amount": 1000}]

    # And nothing was exported, because nothing was asked for.
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    outputs = cast(list[str], manifest["outputs"])
    assert not [path for path in outputs if path.endswith("data.jsonl")]


def test_run_build_executes_export_targets(tmp_path: Path) -> None:
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(
            ExportTarget(kind="jsonl", output_path="exports/data.jsonl"),
            ExportTarget(kind="markdown", output_path="exports/README.md"),
        ),
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    gold_dir = tmp_path / "run1" / "gold" / "datago.apt_trade"
    jsonl_path = gold_dir / "exports" / "data.jsonl"
    markdown_path = gold_dir / "exports" / "README.md"
    assert jsonl_path.read_text(encoding="utf-8") == '{"amount": 1000, "id": "1"}\n'
    assert "# Apartment Trades" in markdown_path.read_text(encoding="utf-8")
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    outputs = cast(list[str], manifest["outputs"])
    assert str(jsonl_path) in outputs
    assert str(markdown_path) in outputs


def test_run_build_writes_dataset_card_readme(tmp_path: Path) -> None:
    # 성공한 빌드의 gold 디렉터리에 dataset card README.md가 생성되는지 검증한다 (#37).
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    readme = tmp_path / "run1" / "gold" / "datago.apt_trade" / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "# Apartment Trades" in text
    assert "## Schema" in text
    assert "- datago.apt_trade" in text

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    outputs = cast(list[str], manifest["outputs"])
    assert str(readme) in outputs


@pytest.mark.parametrize(
    ("license_value", "metadata", "expected", "unexpected"),
    [
        ("CC-BY-4.0", {}, "CC-BY-4.0", None),
        (None, {"license": "KOGL Type 1"}, "KOGL Type 1", None),
        ("CC0-1.0", {"license": "legacy-license"}, "CC0-1.0", "legacy-license"),
    ],
)
def test_run_build_dataset_card_uses_canonical_license_with_legacy_fallback(
    tmp_path: Path,
    license_value: str | None,
    metadata: dict[str, JsonValue],
    expected: str,
    unexpected: str | None,
) -> None:
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        metadata=metadata,
        license=license_value,
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    readme = tmp_path / "run1" / "gold" / "datago.apt_trade" / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert f"## License\n\n{expected}\n" in text
    if unexpected is not None:
        assert unexpected not in text


def test_run_build_snapshot_round_trip_preserves_legacy_license_fallback(
    tmp_path: Path,
) -> None:
    """serializer는 legacy metadata.license를 top-level로 승격하지 않는다 (#487).

    snapshot에는 metadata가 그대로 보존되므로, snapshot을 다시 parse해서 재실행해도
    ``_dataset_card_license``의 legacy fallback으로 동일한 license가 나와야 한다 —
    canonical spec의 license 표현과 dataset card 렌더링이 재현 가능함을 검증한다.
    """
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        metadata={"license": "KOGL Type 1"},
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    first = run_build(spec, client=client, output_root=tmp_path, run_id="run1")
    assert first.status == "ok"

    snapshot_text = (tmp_path / "run1" / "buildspec.yaml").read_text(encoding="utf-8")
    reparsed = parse_spec(cast(dict[str, object], yaml.safe_load(snapshot_text)))
    assert reparsed.license is None
    assert reparsed.metadata["license"] == "KOGL Type 1"

    second = run_build(reparsed, client=client, output_root=tmp_path, run_id="run2")
    assert second.status == "ok"
    readme = tmp_path / "run2" / "gold" / "datago.apt_trade" / "README.md"
    assert "## License\n\nKOGL Type 1\n" in readme.read_text(encoding="utf-8")


def test_run_build_dataset_card_ignores_non_string_metadata_version(tmp_path: Path) -> None:
    """metadata.version이 null/숫자/list/dict이면 문자열화하지 않고 unversioned로 렌더링한다 (#487).

    metadata가 JsonValue로 넓어지면서 ``str(None) == "None"``이 그대로 카드에 노출되던
    회귀를 막는다.
    """
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        metadata={"version": None},
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    text = (tmp_path / "run1" / "gold" / "datago.apt_trade" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "## Version\n\nunversioned" in text
    assert "None" not in text


def test_run_build_does_not_forward_arbitrary_metadata_to_exporters(tmp_path: Path) -> None:
    """임의 metadata는 exporter에 새지 않는다.

    #629 이전에는 orchestrator가 exporter용 metadata를 두 곳에서 만들었고, 이
    테스트는 그중 두 번째(``_execute_exports``)를 가로채 확인했다. 이제 그 두 번째
    경로는 없다 — ``_gold_package_metadata``가 exporter가 보는 유일한 출처이므로
    계약을 거기서 확인한다.
    """
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        metadata={"nested": {"value": 1}, "tags": ["private", "internal"]},
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    # dataset_id는 #550부터 exporter에 전달되는 공개 필드다(Kaggle metadata id
    # 정합). 임의 metadata(nested/tags)는 여전히 새지 않는다.
    assert orchestrator._gold_package_metadata(spec) == {
        "title": "Apartment Trades",
        "description": "seoul apartment trades",
        "dataset_id": "apt_trade",
    }


def test_gold_package_is_the_only_exporter_metadata_source(tmp_path: Path) -> None:
    """orchestrator가 exporter용 metadata를 따로 조립하지 않는다 (#629).

    출처가 둘이면 갈리고, 뒤가 앞을 덮는다 — schema가 사라진 것이 그 결과였다.
    """
    source = pathlib.Path(orchestrator.__file__).read_text(encoding="utf-8")

    assert "_execute_exports(" not in source.split("def _execute_exports(", 1)[1]


def test_run_build_uses_alias_as_source_key(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    assert result.outcomes[0].source_key == "trades"
    assert (tmp_path / "run1" / "gold" / "trades" / "table.parquet").exists()


def test_run_build_card_uses_alias_as_source_identity(tmp_path: Path) -> None:
    # #225: alias가 설정된 경우 dataset card의 sources 항목도 output_key(alias)를 사용해야
    # manifest의 inputs 필드와 일치해야 한다.
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    readme = tmp_path / "run1" / "gold" / "trades" / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    # card는 alias(output_key)를 source 식별자로 사용해야 한다.
    assert "- trades" in text
    # fetch_key(provider.dataset)는 card에 나타나지 않아야 한다.
    assert "- datago.apt_trade" not in text

    # manifest inputs도 alias를 사용한다 — 두 곳이 일치해야 한다.
    import json
    from typing import cast

    manifest = cast(dict[str, object], json.loads(result.manifest_path.read_text(encoding="utf-8")))
    assert "trades" in cast(list[str], manifest["inputs"])


def test_run_build_redacts_path_from_unexpected_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #225: 예상치 못한 예외(OS 오류 등)의 절대 경로가 클라이언트에 노출되지 않아야 한다.
    # #246: 상세 정보는 warnings.warn이 아닌 logger.error로 기록해야 한다.
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    def _fail_with_path(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("failed: /absolute/path/to/file.json")

    monkeypatch.setattr(orchestrator, "build_gold_package", _fail_with_path)

    import logging

    with caplog.at_level(logging.ERROR, logger="kpubdata_builder.pipeline.orchestrator"):
        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    # 클라이언트에게 돌아가는 error 메시지에는 절대 경로가 없어야 한다.
    assert "/absolute/path" not in (outcome.error or "")
    # 상세 정보는 logger.error로만 기록된다 (#246).
    assert any("/absolute/path" in r.getMessage() for r in caplog.records)


def test_run_build_records_failure_when_source_missing(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="missing"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "bronze" not in outcome.stages_completed

    # 실패해도 manifest는 남는다
    assert result.manifest_path.exists()
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["errors"]


def test_run_build_preserves_partial_artifacts_when_later_stage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}, {"id": "2"}]})

    def _fail_gold(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("gold failed")

    monkeypatch.setattr(orchestrator, "build_gold_package", _fail_gold)

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    assert result.outcomes[0].stages_completed == ("bronze", "silver")
    assert (tmp_path / "run1" / "bronze" / "datago.apt_trade").is_dir()
    assert (tmp_path / "run1" / "silver" / "datago.apt_trade" / "table.parquet").exists()
    assert not (tmp_path / "run1" / "gold" / "datago.apt_trade").exists()

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    outputs = cast(list[str], manifest["outputs"])
    assert str(tmp_path / "run1" / "silver" / "datago.apt_trade" / "preview.json") in outputs
    assert str(tmp_path / "run1" / "gold" / "datago.apt_trade" / "table.parquet") not in outputs


def test_run_build_fails_source_when_silver_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 검증 실패한 Silver 데이터셋은 Gold로 흘러가지 않고 소스가 실패 처리되어야 한다 (#189).
    import dataclasses

    from kpubdata_builder.stages.silver import build_silver_dataset as real_build
    from kpubdata_builder.stages.silver.models import ValidationResult
    from kpubdata_builder.stages.silver.validate import ValidationProblem

    def _invalid_silver(*args: object, **kwargs: object) -> object:
        dataset = real_build(*args, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(
            dataset,
            validation=ValidationResult(
                ok=False,
                problems=(
                    ValidationProblem(
                        code="synthetic_failure",
                        field=None,
                        message="synthetic validation failure",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(orchestrator, "build_silver_dataset", _invalid_silver)

    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    assert "synthetic validation failure" in (outcome.error or "")
    # Gold 단계까지 가지 않는다.
    assert "gold" not in outcome.stages_completed
    assert not (tmp_path / "run1" / "gold" / "datago.apt_trade").exists()


def test_run_build_writes_schema_summaries_to_manifest(tmp_path: Path) -> None:
    # 성공한 빌드의 manifest.json에 소스별 schema summary가 기록되는지 검증한다 (#11).
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    summaries = cast(dict[str, JsonValue], manifest["schema_summaries"])
    apt = cast(dict[str, JsonValue], summaries["datago.apt_trade"])
    assert apt["total_fields"] == 2
    fields = cast(list[dict[str, JsonValue]], apt["fields"])
    assert [(f["name"], f["nullable"]) for f in fields] == [("id", False), ("amount", False)]
    # 타입 문자열은 polars dtype 표현을 그대로 싣는다(정수 컬럼).
    amount_type = cast(str, fields[1]["type"])
    assert "Int" in amount_type


def test_run_build_writes_provenance_to_manifest(tmp_path: Path) -> None:
    # 성공한 빌드의 manifest.json에 소스별 상세 provenance가 기록되는지 검증한다 (#12).
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    provenance = cast(list[dict[str, JsonValue]], manifest["provenance"])
    assert len(provenance) == 1
    entry = provenance[0]
    assert entry["provider"] == "datago"
    assert entry["dataset"] == "apt_trade"
    assert entry["record_count"] == 2
    assert cast(str, entry["data_checksum"]).startswith("sha256:")
    assert cast(str, entry["fetched_at"]).endswith("+00:00")


def test_run_build_manifest_counts_all_paginated_records(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakePaginatedClient(
        {"datago.apt_trade": ([{"id": "1", "amount": 1000}], [{"id": "2", "amount": 2500}])}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    row_counts = cast(dict[str, int], manifest["row_counts"])
    assert row_counts["datago.apt_trade"] == 2
    provenance = cast(list[dict[str, JsonValue]], manifest["provenance"])
    assert provenance[0]["record_count"] == 2


def test_run_build_rejects_unsafe_run_id(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValueError, match="run_id"):
        _ = run_build(spec, client=client, output_root=tmp_path, run_id="../escape")


def test_run_build_executes_sources_concurrently(tmp_path: Path) -> None:
    # 동시에 진행 중인 fetch 수를 직접 관찰해 병렬 실행을 검증한다 (#247).
    # wall-clock 임계값 대신 concurrency counter를 쓰는 이유: CI 러너마다 성능 편차가
    # 커서 시간 기반 assert는 느린 러너에서 flaky해진다(느린 러너에서 실측된 회귀:
    # 순차 실행이 아닌데도 elapsed가 임계값을 넘어 실패).
    import threading
    import time

    lock = threading.Lock()
    concurrent_count = 0
    max_seen = 0
    release = threading.Event()

    class _BlockingClient(_FakeClient):
        def dataset(self, source_key: str) -> _FakeDataset:
            nonlocal concurrent_count, max_seen
            with lock:
                concurrent_count += 1
                max_seen = max(max_seen, concurrent_count)
            release.wait(timeout=5.0)
            with lock:
                concurrent_count -= 1
            return super().dataset(source_key)

    spec = _spec(
        SourceRef(provider="datago", dataset="a"),
        SourceRef(provider="datago", dataset="b"),
        SourceRef(provider="datago", dataset="c"),
    )
    client = _BlockingClient(
        {"datago.a": [{"id": "1"}], "datago.b": [{"id": "1"}], "datago.c": [{"id": "1"}]}
    )

    results: list[BuildResult] = []

    def _run() -> None:
        results.append(run_build(spec, client=client, output_root=tmp_path, run_id="run-parallel"))

    runner_thread = threading.Thread(target=_run)
    runner_thread.start()
    try:
        # 3개 소스 모두 동시에 fetch를 블로킹할 때까지 능동적으로 대기한다.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with lock:
                if concurrent_count >= 3:
                    break
            time.sleep(0.02)

        with lock:
            observed = max_seen
    finally:
        release.set()
        runner_thread.join(timeout=5.0)

    assert observed == 3
    assert results[0].status == "ok"
    assert len(results[0].outcomes) == 3


def test_run_build_preserves_source_order_in_manifest_with_multiple_sources(
    tmp_path: Path,
) -> None:
    # 스레드 풀 완료 순서가 뒤바뀌어도 manifest의 inputs/outcomes는 spec.sources
    # 순서를 유지해 결정적이어야 한다 (#247: executor.map은 제출 순서로 결과를 반환).
    spec = _spec(
        SourceRef(provider="datago", dataset="a"),
        SourceRef(provider="datago", dataset="b"),
        SourceRef(provider="datago", dataset="c"),
    )
    client = _FakeClient(
        {"datago.a": [{"id": "1"}], "datago.b": [{"id": "1"}], "datago.c": [{"id": "1"}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run-order")

    assert result.status == "ok"
    assert [o.source_key for o in result.outcomes] == [
        "datago.a",
        "datago.b",
        "datago.c",
    ]
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["inputs"] == ["datago.a", "datago.b", "datago.c"]


def test_run_build_validates_spec_before_running(tmp_path: Path) -> None:
    # 잘못된 spec(소스 없음)은 단계 진입 전 fail-fast로 거부되어야 한다 (#212).
    from kpubdata_builder.errors import ValidationError

    bad_spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValidationError, match="at least one source"):
        _ = run_build(bad_spec, client=client, output_root=tmp_path, run_id="run1")

    # fail-fast: manifest나 워크스페이스가 생성되지 않는다.
    assert not (tmp_path / "run1").exists()


def test_run_build_keeps_snapshot_when_source_pipeline_fails(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="missing"))

    result = run_build(spec, client=_FakeClient({}), output_root=tmp_path, run_id="failed-run")

    assert result.status == "failed"
    assert (tmp_path / "failed-run" / "buildspec.yaml").is_file()
    assert result.spec_digest.startswith("sha256:")


# --- Canonical source contract(#498): file/url kind가 동일 pipeline을 타는지 검증 ---


def test_run_build_with_file_source_runs_full_pipeline(tmp_path: Path) -> None:
    """kind="file" source도 public_api와 동일한 Bronze→Silver→Gold 산출물을 만든다."""
    from kpubdata_builder.uploads import SQLiteUploadRepository

    upload_repository = SQLiteUploadRepository(tmp_path / "uploads.sqlite3")
    metadata = upload_repository.put(
        "owner-1",
        content=b"id,amount\n1,1000\n2,2500\n",
        format="csv",
        encoding="utf-8",
        original_filename="trades.csv",
    )
    spec = _spec(
        SourceRef(
            kind="file",
            upload_id=metadata.upload_id,
            format="csv",
            encoding="utf-8",
            alias="uploaded_trades",
        )
    )

    result = run_build(
        spec,
        client=_FakeClient({}),
        output_root=tmp_path,
        run_id="file-run",
        owner_id="owner-1",
        upload_repository=upload_repository,
    )

    assert result.status == "ok"
    assert result.outcomes[0].source_key == "uploaded_trades"
    assert result.outcomes[0].stages_completed == ("bronze", "silver", "gold")

    gold_parquet = tmp_path / "file-run" / "gold" / "uploaded_trades" / "table.parquet"
    assert pl.read_parquet(gold_parquet).to_dicts() == [
        {"id": 1, "amount": 1000},
        {"id": 2, "amount": 2500},
    ]

    # provenance/manifest 어디에도 로컬 파일시스템 경로가 남지 않는다(#498).
    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path / "uploads.sqlite3") not in manifest_text
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    provenance = cast(list[dict[str, JsonValue]], manifest["provenance"])
    assert provenance[0]["provider"] == "file"
    assert provenance[0]["dataset"] == metadata.upload_id
    assert cast(dict[str, JsonValue], provenance[0]["params"])["upload_id"] == metadata.upload_id


def test_run_build_with_file_source_fails_closed_without_owner(tmp_path: Path) -> None:
    """owner_id 없이 file source를 실행하면 해당 소스만 명확히 실패한다."""
    from kpubdata_builder.uploads import SQLiteUploadRepository

    upload_repository = SQLiteUploadRepository(tmp_path / "uploads.sqlite3")
    metadata = upload_repository.put(
        "owner-1", content=b"id\n1\n", format="csv", encoding="utf-8", original_filename=None
    )
    spec = _spec(SourceRef(kind="file", upload_id=metadata.upload_id, format="csv"))

    result = run_build(
        spec,
        client=_FakeClient({}),
        output_root=tmp_path,
        run_id="no-owner-run",
        upload_repository=upload_repository,
    )

    assert result.status == "failed"
    assert result.outcomes[0].status == "failed"
    assert "authenticated" in (result.outcomes[0].error or "")


def test_run_build_with_url_source_runs_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kind="url" source도 SSRF-safe fetch를 거쳐 동일 pipeline을 탄다."""
    from kpubdata_builder.ingestion.url_fetch import FetchResult
    from kpubdata_builder.stages.bronze import resolve as resolve_module

    def _fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
        assert url == "https://example.org/data.json"
        return FetchResult(
            content=b'[{"id": 1, "amount": 1000}]',
            content_type="application/json",
            final_url=url,
        )

    monkeypatch.setattr(resolve_module, "safe_fetch_get", _fake_fetch)
    spec = _spec(
        SourceRef(kind="url", endpoint="https://example.org/data.json", alias="external_feed")
    )

    result = run_build(spec, client=_FakeClient({}), output_root=tmp_path, run_id="url-run")

    assert result.status == "ok"
    assert result.outcomes[0].source_key == "external_feed"
    gold_parquet = tmp_path / "url-run" / "gold" / "external_feed" / "table.parquet"
    assert pl.read_parquet(gold_parquet).to_dicts() == [{"id": 1, "amount": 1000}]


class TestExportsRunExactlyOnce:
    """BuildSpec.exports가 소스마다 두 번 실행되지 않는다 (#629).

    ``package.export_plan.targets``가 곧 ``spec.exports``이므로
    ``export_gold_package``가 이미 전부 내보낸다. 그런데 오케스트레이터가 같은
    타깃을 같은 디렉터리에 한 번 더 썼다. 두 번째가 만든 ArtifactDataset에는
    schema가 없어서, 덮어쓴 결과로 **게시되는 산출물에서 schema가 사라졌다.**
    """

    @staticmethod
    def _spec_with_exports(*, license_value: str | None = None) -> BuildSpec:
        return BuildSpec(
            dataset_id="owner/apt-trade",
            title="Apartment Trades",
            description="seoul apartment trades",
            sources=(SourceRef(provider="datago", dataset="apt_trade"),),
            exports=(ExportTarget(kind="jsonl", output_path="exports/data.jsonl"),),
            license=license_value,
        )

    def test_manifest_does_not_list_the_same_export_twice(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})

        result = run_build(
            self._spec_with_exports(), client=client, output_root=tmp_path, run_id="run1"
        )

        manifest = cast(
            dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
        )
        outputs = cast(list[str], manifest["outputs"])
        assert len(outputs) == len(set(outputs)), f"duplicated manifest outputs: {outputs}"

    def test_the_export_file_count_matches_the_files_written(self, tmp_path: Path) -> None:
        # file_count가 실제의 두 배면, 그 수를 읽는 쪽은 존재하지 않는 파일을 센다.
        client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})

        result = run_build(
            self._spec_with_exports(), client=client, output_root=tmp_path, run_id="run1"
        )

        gold_dir = tmp_path / "run1" / "gold" / "datago.apt_trade"
        assert (gold_dir / "exports" / "data.jsonl").is_file()
        manifest = cast(
            dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
        )
        outputs = [p for p in cast(list[str], manifest["outputs"]) if p.endswith("data.jsonl")]
        assert len(outputs) == 1

    def test_a_declared_license_reaches_the_gold_package_metadata(self, tmp_path: Path) -> None:
        # Kaggle exporter는 artifact.metadata["license"]만 본다. 이 키가 없으면
        # spec.license를 무엇으로 선언하든 항상 CC-BY-4.0이 게시됐다.
        assert (
            orchestrator._gold_package_metadata(
                self._spec_with_exports(license_value="CC-BY-NC-4.0")
            )["license"]
            == "CC-BY-NC-4.0"
        )

    def test_an_undeclared_license_leaves_the_key_out(self, tmp_path: Path) -> None:
        # 빈 문자열을 실으면 exporter의 기본값 대신 빈 라이선스가 게시된다 —
        # 선언하지 않은 것과 빈 값으로 선언한 것은 다르다.
        assert "license" not in orchestrator._gold_package_metadata(self._spec_with_exports())

    def test_a_legacy_metadata_license_is_still_carried(self, tmp_path: Path) -> None:
        spec = BuildSpec(
            dataset_id="owner/apt-trade",
            title="Apartment Trades",
            description="seoul apartment trades",
            sources=(SourceRef(provider="datago", dataset="apt_trade"),),
            exports=(ExportTarget(kind="jsonl", output_path="exports/data.jsonl"),),
            metadata={"license": "ODbL-1.0"},
        )

        assert orchestrator._gold_package_metadata(spec)["license"] == "ODbL-1.0"
