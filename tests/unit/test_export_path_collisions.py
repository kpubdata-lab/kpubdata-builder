"""Export paths never overwrite a Gold artifact or another export (#1320).

Every export writes into the run's Gold directory, beside ``table.parquet``,
``package.json``, ``splits/`` and the dataset card. ``validate_spec`` rejects a path
that names that directory, one of those entries, or an entry another export also
writes, comparing names the way case-insensitive and normalizing filesystems do.
``export_gold_package`` checks the resolved paths again before writing anything, so a
symlink cannot lead an export onto Gold either.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.errors import ExportError, PathTraversalError, ValidationError
from kpubdata_builder.pipeline import orchestrator
from kpubdata_builder.pipeline.export import export_gold_package
from kpubdata_builder.pipeline.orchestrator import run_build
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.stages.gold import ExportPlan, GoldPackage
from tests.support.polars_bridge import handle_from_frame

_NFC = unicodedata.normalize("NFC", "데이터.csv")
_NFD = unicodedata.normalize("NFD", "데이터.csv")


def _spec(*targets: ExportTarget) -> BuildSpec:
    return BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=targets,
        license="CC-BY-4.0",
    )


def _problems(*targets: ExportTarget) -> set[tuple[str, str]]:
    try:
        validate_spec(_spec(*targets))
    except ValidationError as error:
        structured = cast(list[object], error.structured_problems or [])
        return {(getattr(p, "code"), getattr(p, "path")) for p in structured}  # noqa: B009
    return set()


def _jsonl(path: str) -> ExportTarget:
    return ExportTarget(kind="jsonl", output_path=path)


# ------------------------------------------------------------- validate_spec


@pytest.mark.parametrize(
    "path",
    [".", "./", "./.", "/", "../out.jsonl", "a/../out.jsonl", "a\\..\\out.jsonl", "/etc/out"]
    + ["\\\\server\\share\\out.jsonl", "C:\\out.jsonl", "c:out.jsonl", "...", "a/ ./b"],
)
def test_root_absolute_and_traversal_paths_are_rejected(path: str) -> None:
    assert _problems(_jsonl(path)) == {("unsafe_export_path", "exports[0].output_path")}


@pytest.mark.parametrize(
    "path",
    ["table.parquet", "package.json", "splits", "splits/train.parquet", "README.md", "card.json"]
    # Names a case-insensitive (macOS, Windows) or trailing-dot-dropping (Windows)
    # filesystem maps onto a Gold entry.
    + ["TABLE.PARQUET", "Package.Json", "table.parquet.", "package.json ", "SPLITS/x.jsonl"]
    + ["./table.parquet", "readme.md", "splits\\train.parquet"],
)
def test_gold_artifact_names_are_rejected(path: str) -> None:
    assert _problems(_jsonl(path)) == {("reserved_export_path", "exports[0].output_path")}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_jsonl("data.jsonl"), ExportTarget(kind="csv", output_path="data.jsonl")),
        (_jsonl("data.jsonl"), _jsonl("DATA.jsonl")),
        (_jsonl("data.jsonl"), _jsonl("data.jsonl.")),
        (_jsonl("data.jsonl"), _jsonl("./data.jsonl")),
        (_jsonl("out/data.jsonl"), _jsonl("out//data.jsonl")),
        (_jsonl("out/data.jsonl"), _jsonl("OUT\\data.jsonl")),
        (_jsonl(_NFC), _jsonl(_NFD)),
        # Parent and child: one export's directory holds another's file.
        (ExportTarget(kind="huggingface", output_path="hf"), _jsonl("hf/data.jsonl")),
        (_jsonl("hf/data.jsonl"), ExportTarget(kind="huggingface", output_path="HF")),
        (_jsonl("out"), _jsonl("out/data.jsonl")),
        # A Kaggle export writes dataset-metadata.json, and below Gold the card, beside it.
        (ExportTarget(kind="kaggle", output_path="pkg/d.csv"), _jsonl("pkg/dataset-metadata.json")),
        (ExportTarget(kind="kaggle", output_path="pkg/d.csv"), _jsonl("pkg/README.md")),
        (_jsonl("Dataset-Metadata.json"), ExportTarget(kind="kaggle", output_path="d.csv")),
    ],
)
def test_exports_sharing_an_entry_are_rejected(first: ExportTarget, second: ExportTarget) -> None:
    problems = _problems(first, second)
    assert problems and {code for code, _ in problems} == {"export_path_collision"}
    assert problems <= {
        ("export_path_collision", "exports[0].output_path"),
        ("export_path_collision", "exports[1].output_path"),
    }


def test_a_kaggle_export_onto_its_own_metadata_is_rejected() -> None:
    target = ExportTarget(kind="kaggle", output_path="dataset-metadata.json")
    assert _problems(target) == {("export_path_collision", "exports[0].output_path")}


def test_problems_name_the_spec_index_after_a_skipped_export() -> None:
    problems = _problems(_jsonl(" "), _jsonl("a.jsonl"), _jsonl("A.jsonl"))
    assert ("export_path_collision", "exports[2].output_path") in problems


@pytest.mark.parametrize(
    "targets",
    [
        # The parity scenarios' layout: every exporter kind side by side.
        (
            ExportTarget(kind="parquet", output_path="data.parquet"),
            ExportTarget(kind="csv", output_path="data.csv"),
            _jsonl("data.jsonl"),
            ExportTarget(kind="markdown", output_path="data.md"),
            ExportTarget(kind="huggingface", output_path="hf", options={"format": "jsonl"}),
            ExportTarget(kind="kaggle", output_path="kaggle/data.csv"),
        ),
        # Studio's derived layout (artifacts/builds/<id>/data.<ext>, Hugging Face beside).
        (
            _jsonl("artifacts/builds/demo/data.jsonl"),
            ExportTarget(kind="huggingface", output_path="artifacts/builds/demo-2"),
        ),
        # Kaggle exports in one directory merge into one metadata file.
        (
            ExportTarget(kind="kaggle", output_path="a.csv"),
            ExportTarget(kind="kaggle", output_path="b.csv"),
        ),
        (_jsonl("./data.jsonl"), _jsonl("nested/dir/data.jsonl"), _jsonl("table.parquet.d/x")),
        (_jsonl("splits_extra.jsonl"), _jsonl("my-package.json")),
    ],
)
def test_distinct_paths_still_validate(targets: tuple[ExportTarget, ...]) -> None:
    validate_spec(_spec(*targets))


# ------------------------------------------------- export_gold_package (resolved paths)


def _gold(tmp_path: Path, *targets: ExportTarget) -> tuple[GoldPackage, Path]:
    frame = pl.DataFrame({"id": ["1", "2"], "v": [1, 2]})
    table = handle_from_frame(frame, workdir=tmp_path / "work")
    gold = tmp_path / "gold"
    gold.mkdir()
    table.write_parquet(gold / "table.parquet")
    (gold / "package.json").write_text('{"row_count": 2}\n', encoding="utf-8")
    package = GoldPackage(
        dataset_name="d",
        table=table,
        export_plan=ExportPlan(targets=targets),
        source_silver="s",
        metadata={"title": "T", "license": "CC-BY-4.0", "dataset_id": "o/d"},
    )
    return package, gold


def _digest(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


@pytest.mark.parametrize(
    "target",
    [
        ExportTarget(kind="parquet", output_path="alias/table.parquet"),
        ExportTarget(kind="jsonl", output_path="pointer"),
        ExportTarget(kind="huggingface", output_path="alias/splits"),
        ExportTarget(kind="csv", output_path="alias/Package.json"),
    ],
)
def test_a_symlink_cannot_lead_an_export_onto_gold(tmp_path: Path, target: ExportTarget) -> None:
    package, gold = _gold(tmp_path, ExportTarget(kind="csv", output_path="first.csv"), target)
    (gold / "alias").symlink_to(gold, target_is_directory=True)
    (gold / "pointer").symlink_to(gold / "table.parquet")
    before = _digest(gold)

    with pytest.raises(ExportError, match=r"exports\[1\]"):
        export_gold_package(package, output_dir=gold, table_path=gold / "table.parquet")

    # Nothing is written, not even the valid export listed first.
    assert _digest(gold) == before
    assert not (gold / "first.csv").exists()


def test_a_symlink_cannot_lead_two_exports_onto_one_file(tmp_path: Path) -> None:
    package, gold = _gold(tmp_path, _jsonl("data.jsonl"), _jsonl("alias/data.jsonl"))
    (gold / "alias").symlink_to(gold, target_is_directory=True)

    with pytest.raises(ExportError, match="overlaps"):
        export_gold_package(package, output_dir=gold)
    assert not (gold / "data.jsonl").exists()


def test_a_symlink_out_of_gold_is_still_traversal(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    package, gold = _gold(tmp_path, _jsonl("away/data.jsonl"))
    (gold / "away").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        export_gold_package(package, output_dir=gold)
    assert list(outside.iterdir()) == []


def test_the_gold_directory_itself_is_refused(tmp_path: Path) -> None:
    package, gold = _gold(tmp_path, ExportTarget(kind="huggingface", output_path="."))
    before = _digest(gold)

    with pytest.raises(ExportError, match="Gold directory itself"):
        export_gold_package(package, output_dir=gold)
    assert _digest(gold) == before


def test_distinct_resolved_paths_still_export(tmp_path: Path) -> None:
    package, gold = _gold(
        tmp_path, _jsonl("out/data.jsonl"), ExportTarget(kind="huggingface", output_path="hf")
    )
    before = _digest(gold)

    paths = export_gold_package(package, output_dir=gold)

    assert paths == (gold / "out" / "data.jsonl", gold / "hf")
    assert (gold / "hf" / "dataset_infos.json").is_file()
    assert {k: v for k, v in _digest(gold).items() if k in before} == before


# ------------------------------------------------------------ a whole build


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    def list(self, **_params: JsonValue) -> _Result:
        return _Result([{"id": "1", "v": 1}, {"id": "2", "v": 2}])

    def dataset(self, _key: str) -> _Source:
        return self


def _factory(**_kwargs: object) -> SourceClient:
    return cast(SourceClient, _Source())


def _yaml(*paths: str) -> str:
    exports = "".join(f"  - kind: jsonl\n    output_path: {path!r}\n" for path in paths)
    return (
        "dataset_id: demo.table\ntitle: Demo\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: apt_trade\n    alias: m\nexports:\n" + exports
    )


def _tree(root: Path) -> dict[str, str]:
    return {
        os.fspath(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    "paths",
    [
        ("table.parquet",),
        ("package.json",),
        ("splits/train.jsonl",),
        (".",),
        ("TABLE.PARQUET",),
        ("table.parquet.",),
        ("data.jsonl", "Data.JSONL"),
        (_NFC, _NFD),
        ("../m/table.parquet",),
    ],
)
def test_a_rejected_spec_leaves_the_committed_run_untouched(
    tmp_path: Path, paths: tuple[str, ...]
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    service = BuilderService(output_root=root, client_factory=_factory)
    try:
        assert service.build(_yaml("data.jsonl"), run_id="r1").status_code == 200
        gold = root / "r1" / "gold" / "m"
        table_bytes = (gold / "table.parquet").read_bytes()
        before = _tree(root)

        validated = service.validate(_yaml(*paths))
        built = service.build(_yaml(*paths), run_id="r1")
        fresh = service.build(_yaml(*paths), run_id="r2")
    finally:
        service._async_builds._executor.shutdown(wait=True)

    assert validated.status_code == built.status_code == fresh.status_code == 400
    structured = cast(list[dict[str, object]], validated.body["structured_problems"])
    assert {p["path"] for p in structured} >= {f"exports[{len(paths) - 1}].output_path"}
    assert (gold / "table.parquet").read_bytes() == table_bytes
    assert _tree(root) == before
    assert not (root / "r2").exists()


def test_an_unvalidated_spec_fails_at_export_without_touching_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: with validation skipped, the export stage still refuses before writing."""
    monkeypatch.setattr(orchestrator, "validate_spec", lambda _spec: None)
    written: list[bytes] = []
    persist: Callable[..., object] = orchestrator.persist_gold_package

    def remember(*args: object, **kwargs: object) -> object:
        result = persist(*args, **kwargs)
        written.append((getattr(result, "table_path")).read_bytes())  # noqa: B009
        return result

    monkeypatch.setattr(orchestrator, "persist_gold_package", remember)
    spec = BuildSpec(
        dataset_id="demo",
        title="t",
        description="d",
        sources=(SourceRef(provider="datago", dataset="apt_trade", alias="m"),),
        exports=(_jsonl("ok.jsonl"), _jsonl("TABLE.PARQUET")),
    )

    result = run_build(spec, client=_Source(), output_root=tmp_path, run_id="r1")

    gold = tmp_path / "r1" / "gold" / "m"
    assert [o.status for o in result.outcomes] == ["failed"]
    assert written and (gold / "table.parquet").read_bytes() == written[0]
    assert not (gold / "ok.jsonl").exists()
