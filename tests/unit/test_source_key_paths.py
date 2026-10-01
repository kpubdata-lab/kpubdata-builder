"""Source output keys never reach paths outside the run directory (#916).

Two keys never name the same directory inside the run either (#930).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from kpubdata_builder.errors import ValidationError
from kpubdata_builder.pipeline import orchestrator
from kpubdata_builder.pipeline.cancellation import BuildCancelled
from kpubdata_builder.pipeline.orchestrator import run_build
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import (
    BuildSpec,
    CompositionSpec,
    ExportTarget,
    JoinSpec,
    JsonValue,
    SourceRef,
)
from kpubdata_builder.spec.validator import _source_key_problems, validate_spec
from kpubdata_builder.stages._path_safety import (
    LEGACY_CHECKPOINT_SUFFIX,
    contained_child,
    path_collision_key,
)

_REPRO_SPEC = """\
dataset_id: demo
title: t
description: d
sources:
  - provider: datago
    dataset: slow
  - provider: "../."
    dataset: "/victim-run"
exports:
  - kind: jsonl
    output_path: out.jsonl
"""


class _Page:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items
        self.total_count = len(items)


class _Dataset:
    def list(self, **_params: object) -> _Page:  # pragma: no cover - list_all is used
        raise AssertionError("list_all is the paginated path")

    def list_all(self, **_params: object) -> Iterator[_Page]:
        yield _Page([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


def _plant_victims(output_root: Path) -> tuple[Path, Path]:
    """A sibling run's data and a file the legacy checkpoint unlink would hit."""
    victim = output_root / "victim-run"
    victim.mkdir(parents=True)
    (victim / "marker.txt").write_text("keep me", encoding="utf-8")
    legacy = output_root / "victim-run.jsonl"
    legacy.write_text("keep me too", encoding="utf-8")
    return victim / "marker.txt", legacy


def _unsafe_spec(*, with_normal_source: bool = True) -> BuildSpec:
    sources = [SourceRef(provider="../.", dataset="/victim-run")]
    if with_normal_source:
        sources.insert(0, SourceRef(provider="datago", dataset="slow"))
    return BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
    )


@pytest.fixture
def skip_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a spec that reaches the pipeline without validate_spec."""
    monkeypatch.setattr(orchestrator, "validate_spec", lambda _spec: None)


# ------------------------------------------------------------------ issue repro


def test_issue_repro_is_rejected_by_validate_and_build(tmp_path: Path) -> None:
    output_root = tmp_path / "runs"
    marker, legacy = _plant_victims(output_root)
    service = BuilderService(output_root=output_root, client_factory=lambda **_: _Client())

    validated = service.validate(_REPRO_SPEC)
    built = service.build(_REPRO_SPEC, run_id="r1")

    assert validated.status_code == 400
    structured = validated.body["structured_problems"]
    assert isinstance(structured, list)
    codes = {(p["code"], p["path"]) for p in structured if isinstance(p, dict)}
    assert ("unsafe_source_key", "sources[1].provider") in codes
    assert ("unsafe_source_key", "sources[1].dataset") in codes
    assert built.status_code == 400
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert legacy.read_text(encoding="utf-8") == "keep me too"
    assert not (output_root / "r1").exists()


def test_unvalidated_unsafe_key_fails_without_deleting_outside_the_run(
    tmp_path: Path, skip_validation: None
) -> None:
    marker, legacy = _plant_victims(tmp_path)
    result = run_build(_unsafe_spec(), client=_Client(), output_root=tmp_path, run_id="r1")

    outcomes = {o.source_key: o.status for o in result.outcomes}
    assert outcomes == {"datago.slow": "ok", "../../victim-run": "failed"}
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert legacy.read_text(encoding="utf-8") == "keep me too"
    # The safe source still cleans up after itself.
    assert not (tmp_path / "r1" / "_bronze_staging").exists()
    assert not (tmp_path / "r1" / "_checkpoints" / "datago.slow").exists()


class _CancelNow:
    def cancel_requested(self) -> bool:
        return True

    def commit(self) -> bool:
        return False


def test_cancelled_unsafe_source_keeps_outside_directories(
    tmp_path: Path, skip_validation: None
) -> None:
    marker, legacy = _plant_victims(tmp_path)

    with contextlib.suppress(BuildCancelled):
        run_build(
            _unsafe_spec(with_normal_source=False),
            client=_Client(),
            output_root=tmp_path,
            run_id="r1",
            cancellation=_CancelNow(),
        )

    assert marker.read_text(encoding="utf-8") == "keep me"
    assert legacy.read_text(encoding="utf-8") == "keep me too"


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("provider", "dataset", "alias", "path"),
    [
        ("../.", "/victim-run", None, "sources[0].provider"),
        ("datago", "../victim", None, "sources[0].dataset"),
        ("datago", "/abs", None, "sources[0].dataset"),
        ("datago", "a/b", None, "sources[0].dataset"),
        ("data\\go", "x", None, "sources[0].provider"),
        (".", "x", None, "sources[0].provider"),
        # The alias decides the directory, but provider/dataset are still checked.
        ("..", "x", "safe", "sources[0].provider"),
    ],
)
def test_unsafe_provider_or_dataset_is_rejected(
    provider: str, dataset: str, alias: str | None, path: str
) -> None:
    spec = BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=(SourceRef(provider=provider, dataset=dataset, alias=alias),),
        exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
    )

    with pytest.raises(ValidationError) as caught:
        validate_spec(spec)

    assert ("unsafe_source_key", path) in {
        (p.code, p.path) for p in caught.value.structured_problems
    }


@pytest.mark.parametrize(
    ("provider", "dataset"),
    [("datago", "air_quality"), ("bok", "base_rate"), ("kosis", "DT_1B04005N.v2"), ("a", "b-c")],
)
def test_real_provider_dataset_keys_still_validate(provider: str, dataset: str) -> None:
    spec = BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=(SourceRef(provider=provider, dataset=dataset),),
        exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
    )

    validate_spec(spec)


def test_every_kpubdata_catalogue_id_is_a_safe_key() -> None:
    from kpubdata import Client

    refs = list(Client().datasets.list())
    assert refs
    sources = tuple(
        SourceRef(provider=ref.provider, dataset=ref.id.split(".", 1)[1]) for ref in refs
    )
    spec = BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=sources,
        exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
    )

    assert _source_key_problems(spec) == []
    # No two catalogue ids share a directory on a case-insensitive filesystem, and
    # none is another id plus the legacy checkpoint suffix (#930).
    keys = [f"{source.provider}.{source.dataset}" for source in sources]
    folded = [path_collision_key(key) for key in keys]
    assert len(set(folded)) == len(folded)
    legacy = {path_collision_key(f"{key}{LEGACY_CHECKPOINT_SUFFIX}") for key in keys}
    assert not legacy & set(folded)


# ------------------------------------------------------------------ path collisions (#930)


def _two_sources(first: SourceRef, second: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=(first, second),
        exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
    )


def _problem_codes(spec: BuildSpec) -> set[tuple[str, str]]:
    with pytest.raises(ValidationError) as caught:
        validate_spec(spec)
    return {(p.code, p.path) for p in caught.value.structured_problems}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Case only: one directory on macOS APFS (default) and Windows.
        (
            SourceRef(provider="datago", dataset="a", alias="Trades"),
            SourceRef(provider="datago", dataset="b", alias="trades"),
        ),
        # Case only, without aliases.
        (
            SourceRef(provider="datago", dataset="Sales"),
            SourceRef(provider="datago", dataset="sales"),
        ),
        # An alias against another source's provider.dataset key.
        (
            SourceRef(provider="datago", dataset="sales"),
            SourceRef(provider="bok", dataset="x", alias="DATAGO.sales"),
        ),
        # Trailing dot: Windows drops it, so ``trades.`` is ``trades``.
        (
            SourceRef(provider="datago", dataset="a", alias="trades"),
            SourceRef(provider="datago", dataset="b", alias="trades."),
        ),
    ],
    ids=["alias-case", "provider-dataset-case", "alias-vs-provider-dataset", "trailing-dot"],
)
def test_keys_naming_one_directory_are_rejected(first: SourceRef, second: SourceRef) -> None:
    assert ("source_key_path_collision", "sources[1]") in _problem_codes(
        _two_sources(first, second)
    )


@pytest.mark.parametrize("order", ["owner-first", "suffixed-first"])
def test_a_key_equal_to_another_plus_the_legacy_suffix_is_rejected(order: str) -> None:
    owner = SourceRef(provider="datago", dataset="a", alias="foo")
    suffixed = SourceRef(provider="datago", dataset="b", alias="FOO.jsonl")
    pair = (owner, suffixed) if order == "owner-first" else (suffixed, owner)

    codes = _problem_codes(_two_sources(*pair))

    assert ("source_key_path_collision", "sources[1]") in codes


def test_exact_duplicate_keeps_its_own_code() -> None:
    source = SourceRef(provider="datago", dataset="sales")

    codes = _problem_codes(_two_sources(source, source))

    assert ("duplicate_source_key", "sources[1]") in codes
    assert all(code != "source_key_path_collision" for code, _ in codes)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            SourceRef(provider="datago", dataset="sales"),
            SourceRef(provider="datago", dataset="sale"),
        ),
        (
            SourceRef(provider="datago", dataset="a", alias="trades"),
            SourceRef(provider="datago", dataset="b", alias="trades_2"),
        ),
        (
            SourceRef(provider="datago", dataset="a", alias="foo"),
            SourceRef(provider="datago", dataset="b", alias="foo.json"),
        ),
        (
            SourceRef(provider="datago", dataset="a", alias="foo"),
            SourceRef(provider="datago", dataset="b", alias="foo.jsonl.v2"),
        ),
        (
            SourceRef(provider="kosis", dataset="DT_1"),
            SourceRef(provider="kosis", dataset="DT_1.v2"),
        ),
    ],
)
def test_distinct_keys_still_validate(first: SourceRef, second: SourceRef) -> None:
    validate_spec(_two_sources(first, second))


def _composed(name: str) -> BuildSpec:
    return BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=(
            SourceRef(provider="datago", dataset="sales", alias="sales"),
            SourceRef(provider="datago", dataset="region", alias="region"),
        ),
        exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
        composition=CompositionSpec(
            name=name,
            join=JoinSpec(left="sales", right="region", left_key="region_id", right_key="id"),
        ),
    )


@pytest.mark.parametrize("name", ["../victim-run", "a/b", ".", "..", "has space", " combined"])
def test_unsafe_composition_name_is_rejected_at_validate_time(name: str) -> None:
    assert ("unsafe_source_key", "composition.name") in _problem_codes(_composed(name))


@pytest.mark.parametrize("name", ["Sales", "REGION", "sales."])
def test_composition_name_colliding_as_a_path_is_rejected(name: str) -> None:
    assert ("composition_name_collision", "composition.name") in _problem_codes(_composed(name))


def test_safe_distinct_composition_name_still_validates() -> None:
    validate_spec(_composed("sales_by_region"))


@pytest.mark.parametrize(
    ("key", "folded"),
    [
        ("Trades", "trades"),
        ("trades.", "trades"),
        ("trades. .", "trades"),
        ("datago.Air_Quality", "datago.air_quality"),
        ("Stra\u00dfe", "strasse"),
        ("e\u0301", "\u00e9"),
    ],
)
def test_path_collision_key_folds_what_filesystems_fold(key: str, folded: str) -> None:
    assert path_collision_key(key) == folded


# ------------------------------------------------------------------ legacy checkpoint (#930)


def test_legacy_checkpoint_cleanup_leaves_a_directory_of_that_name(tmp_path: Path) -> None:
    """A spec that bypassed validation still cannot remove another source's checkpoint."""
    run_dir = tmp_path / "runs" / "r1"
    other = run_dir / orchestrator._CHECKPOINT_DIRNAME / "datago.slow.jsonl"
    other.mkdir(parents=True)
    (other / "part-0.jsonl").write_text("{}\n", encoding="utf-8")

    result = run_build(
        BuildSpec(
            dataset_id="demo",
            title="t",
            description="d",
            sources=(SourceRef(provider="datago", dataset="slow"),),
            exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
        ),
        client=_Client(),
        output_root=tmp_path / "runs",
        run_id="r1",
    )

    assert [o.status for o in result.outcomes] == ["ok"]
    assert (other / "part-0.jsonl").read_text(encoding="utf-8") == "{}\n"


def test_a_legacy_checkpoint_file_is_still_removed(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    legacy = run_dir / orchestrator._CHECKPOINT_DIRNAME / f"datago.slow{LEGACY_CHECKPOINT_SUFFIX}"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}\n", encoding="utf-8")

    run_build(
        BuildSpec(
            dataset_id="demo",
            title="t",
            description="d",
            sources=(SourceRef(provider="datago", dataset="slow"),),
            exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
        ),
        client=_Client(),
        output_root=tmp_path / "runs",
        run_id="r1",
    )

    assert not legacy.exists()


# ------------------------------------------------------------------ path layer


@pytest.mark.parametrize("name", ["..", ".", "../x", "../../victim-run", "/abs", "a/b", ""])
def test_contained_child_refuses_unsafe_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        contained_child(tmp_path, name, field_name="key", label="entry")


def test_contained_child_refuses_a_symlink_leading_outside(tmp_path: Path) -> None:
    parent = tmp_path / "run" / "_bronze_staging"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent / "datago.x").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        contained_child(parent, "datago.x", field_name="key", label="entry")


def test_contained_child_refuses_the_parent_itself(tmp_path: Path) -> None:
    (tmp_path / "self").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="not the directory itself"):
        contained_child(tmp_path, "self", field_name="key", label="entry")


def test_contained_child_accepts_a_dotted_key(tmp_path: Path) -> None:
    assert contained_child(tmp_path, "datago.air_quality", field_name="k", label="e") == (
        tmp_path / "datago.air_quality"
    )


@pytest.mark.parametrize(
    ("dirname", "suffix"),
    [
        (orchestrator._STAGING_DIRNAME, ""),
        (orchestrator._CHECKPOINT_DIRNAME, ""),
        (orchestrator._CHECKPOINT_DIRNAME, ".jsonl"),
    ],
)
def test_source_work_path_refuses_escaping_keys(tmp_path: Path, dirname: str, suffix: str) -> None:
    run_dir = tmp_path / "r1"

    with pytest.raises(ValueError):
        orchestrator._source_work_path(run_dir, dirname, "../../victim-run", suffix=suffix)
    assert orchestrator._source_work_path(run_dir, dirname, "datago.slow", suffix=suffix) == (
        run_dir / dirname / f"datago.slow{suffix}"
    )


def test_source_work_path_refuses_a_work_root_symlinked_outside(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / orchestrator._CHECKPOINT_DIRNAME).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        orchestrator._source_work_path(run_dir, orchestrator._CHECKPOINT_DIRNAME, "datago.x")


def test_staging_cleanup_skips_an_unsafe_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marker, _ = _plant_victims(tmp_path)
    run_dir = tmp_path / "r1"

    with caplog.at_level(logging.WARNING, logger=orchestrator.__name__):
        orchestrator._remove_source_staging(run_dir, "../../victim-run")

    assert marker.exists()
    assert "staging cleanup skipped" in caplog.text


def test_staging_cleanup_removes_a_safe_key(tmp_path: Path) -> None:
    staging = tmp_path / "r1" / orchestrator._STAGING_DIRNAME / "datago.slow"
    staging.mkdir(parents=True)
    (staging / "records.jsonl").write_text("{}\n", encoding="utf-8")

    orchestrator._remove_source_staging(tmp_path / "r1", "datago.slow")

    assert not staging.exists()


def test_symlinked_staging_entry_is_not_followed_by_a_build(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("keep me", encoding="utf-8")
    staging_root = tmp_path / "runs" / "r1" / orchestrator._STAGING_DIRNAME
    staging_root.mkdir(parents=True)
    (staging_root / "datago.slow").symlink_to(outside, target_is_directory=True)

    result = run_build(
        BuildSpec(
            dataset_id="demo",
            title="t",
            description="d",
            sources=(SourceRef(provider="datago", dataset="slow"),),
            exports=(ExportTarget(kind="jsonl", output_path="out.jsonl"),),
        ),
        client=_Client(),
        output_root=tmp_path / "runs",
        run_id="r1",
    )

    assert [o.status for o in result.outcomes] == ["failed"]
    assert (outside / "marker.txt").read_text(encoding="utf-8") == "keep me"
