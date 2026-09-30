"""Preview (#3, #497): verify preview_build creates schema+sample+diff and does not write files."""

from __future__ import annotations

import builtins
import random as random_module
from collections.abc import Iterable
from pathlib import Path

import pytest

import kpubdata_builder.pipeline.preview as preview_module
from kpubdata_builder.errors import ValidationError
from kpubdata_builder.pipeline import PreviewResult, preview_build
from kpubdata_builder.spec import (
    BuildSpec,
    ColumnNullTokens,
    ExportTarget,
    JsonValue,
    SourceRef,
)
from kpubdata_builder.spec.models import SchemaContract
from kpubdata_builder.stages.silver.build import build_silver_dataset
from kpubdata_builder.tabular import PreviewSlice, SchemaInfo
from kpubdata_builder.tabular.polars_bridge import handle_from_frame, to_polars


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def test_preview_build_returns_schema_and_sample() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": str(i), "v": i} for i in range(10)]})

    result = preview_build(spec, client=client, limit=3)

    assert isinstance(result, PreviewResult)
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.source_key == "datago.apt_trade"
    assert preview.status == "ok"
    assert isinstance(preview.schema, SchemaInfo)
    assert [c.name for c in preview.schema.columns] == ["id", "v"]
    assert isinstance(preview.preview, PreviewSlice)
    assert preview.preview.total_rows == 10
    assert len(preview.preview.rows) == 3


def test_preview_build_writes_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # Previous test verified that temp directory not passed to preview_build was empty,
    # effectively always passing (#196). We intercept actual filesystem write calls to verify
    # preview_build
    # guarantees no writes.
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    real_open = builtins.open

    def _guard_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"unexpected file write during preview: {file!r} (mode={mode})")
        return real_open(file, mode, *args, **kwargs)

    def _boom(self: Path, *args: object, **kwargs: object) -> object:
        raise AssertionError(f"unexpected filesystem write during preview: {self!r}")

    monkeypatch.setattr(builtins, "open", _guard_open)
    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "write_bytes", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)

    # If a write path is called, it fails with AssertionError.
    result = preview_build(spec, client=client, limit=5)

    assert isinstance(result, PreviewResult)


def test_preview_build_validates_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    # Invalid spec must fail fast instead of partial execution/empty results (#193).
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="unsupported_kind", output_path="data.x"),),
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValidationError):
        preview_build(spec, client=client, limit=5)


def test_preview_build_records_failure_for_missing_source() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="missing"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = preview_build(spec, client=client)

    preview = result.previews[0]
    assert preview.status == "failed"
    assert preview.error is not None


def test_preview_build_fetches_by_provider_dataset_and_reports_alias() -> None:
    """Even with alias, fetch uses provider.dataset key, surface key is alias (#98 review
    same regression).
    """
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = preview_build(spec, client=client, limit=1)

    preview = result.previews[0]
    assert preview.status == "ok"
    assert preview.source_key == "trades"


def test_preview_build_rejects_non_positive_limit() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValueError, match="limit"):
        preview_build(spec, client=client, limit=0)


# ---------------------------------------------------------------------------
# Sampling (#497)
# ---------------------------------------------------------------------------


class TestSampling:
    def test_default_sample_mode_is_first(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(10)]})

        result = preview_build(spec, client=client, limit=3)

        preview = result.previews[0]
        assert preview.sample_mode == "first"
        assert [row["id"] for row in preview.preview.rows] == ["0", "1", "2"]
        assert [row["id"] for row in preview.source_sample] == ["0", "1", "2"]

    def test_explicit_first_matches_default(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(10)]})

        default_result = preview_build(spec, client=client, limit=3)
        explicit_result = preview_build(spec, client=client, limit=3, sample_mode="first")

        assert default_result.previews[0].preview.rows == explicit_result.previews[0].preview.rows
        assert default_result.previews[0].source_sample == explicit_result.previews[0].source_sample

    def test_random_mode_is_reproducible_with_same_seed(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(200)]})

        first = preview_build(spec, client=client, limit=5, sample_mode="random", seed=42)
        second = preview_build(spec, client=client, limit=5, sample_mode="random", seed=42)

        assert first.previews[0].source_sample == second.previews[0].source_sample
        assert first.previews[0].preview.rows == second.previews[0].preview.rows

    def test_random_mode_differs_with_different_seed(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(200)]})

        seed_a = preview_build(spec, client=client, limit=5, sample_mode="random", seed=1)
        seed_b = preview_build(spec, client=client, limit=5, sample_mode="random", seed=2)

        assert seed_a.previews[0].source_sample != seed_b.previews[0].source_sample

    def test_random_mode_matches_select_indices_algorithm(self) -> None:
        # Verify the implementation actually delegates to random.Random(seed).sample(range(n), k),
        # not global random state—whitebox lock that it depends only on seed.
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        records = [{"id": str(i)} for i in range(50)]
        client = _FakeClient({"datago.apt_trade": records})

        result = preview_build(spec, client=client, limit=6, sample_mode="random", seed=7)

        expected_indices = sorted(random_module.Random(7).sample(range(50), 6))
        expected_ids = [records[i]["id"] for i in expected_indices]
        assert [row["id"] for row in result.previews[0].source_sample] == expected_ids

    def test_random_mode_does_not_disturb_global_random_state(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(50)]})

        random_module.seed(1234)
        before = random_module.random()
        random_module.seed(1234)
        preview_build(spec, client=client, limit=5, sample_mode="random", seed=99)
        after = random_module.random()

        assert before == after

    def test_rejects_invalid_sample_mode(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        with pytest.raises(ValueError, match="sample_mode"):
            preview_build(spec, client=client, sample_mode="shuffle")  # type: ignore[arg-type]

    def test_rejects_non_int_seed(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        with pytest.raises(TypeError, match="seed"):
            preview_build(spec, client=client, sample_mode="random", seed="7")  # type: ignore[arg-type]

    def test_rejects_bool_seed(self) -> None:
        # bool is a subtype of int, but seed makes no sense.
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        with pytest.raises(TypeError, match="seed"):
            preview_build(spec, client=client, sample_mode="random", seed=True)

    def test_random_sample_bounded_by_total_rows_and_limit(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(3)]})

        result = preview_build(spec, client=client, limit=10, sample_mode="random", seed=0)

        # Requesting limit(10) > total_rows(3) returns only actually-existing rows.
        assert len(result.previews[0].source_sample) == 3
        assert len(result.previews[0].preview.rows) == 3


# ---------------------------------------------------------------------------
# Diff (#497)
# ---------------------------------------------------------------------------


class TestDiff:
    def test_no_change_yields_empty_diffs(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient(
            {"datago.apt_trade": [{"id": "1", "label": "a"}, {"id": "2", "label": "b"}]}
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.diff_available is True
        assert preview.diffs == ()
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == 0
        assert preview.transform_summary.changed_rows == 0
        assert preview.diff_truncated is False

    def test_declared_cast_produces_diff_with_transform_label(self) -> None:
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        client = _FakeClient(
            {"datago.apt_trade": [{"id": "1", "amount": "128000"}, {"id": "2", "amount": "50"}]}
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.diff_available is True
        assert len(preview.diffs) == 2
        first = preview.diffs[0]
        assert first.row == 0
        assert first.column == "amount"
        assert first.before == "128000"
        assert first.after == 128000
        assert first.transform == "cast:int"
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == 2
        assert preview.transform_summary.changed_rows == 2
        assert preview.diff_truncated is False

    def test_wide_dataset_truncates_diffs_but_keeps_accurate_summary_end_to_end(self) -> None:
        # 497 sample/diff memory ceiling: limit (row count) alone cannot cap the number of diff
        # items in wide datasets
        # (high column count) — MAX_PREVIEW_DIFF_ITEMS actually caps the number of PreviewDiffItems
        # in the response
        # and marks it explicitly with diff_truncated; end-to-end test with 1 row ×
        # (MAX_PREVIEW_DIFF_ITEMS + 100) columns all cast.
        #
        max_items = preview_module.MAX_PREVIEW_DIFF_ITEMS
        column_count = max_items + 100
        columns = [f"c{i}" for i in range(column_count)]
        casts = dict.fromkeys(columns, "int")
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts=casts),
        )
        spec = _spec(source)
        row = {c: "1" for c in columns}
        client = _FakeClient({"datago.apt_trade": [row]})

        result = preview_build(spec, client=client, limit=1)

        preview = result.previews[0]
        assert preview.status == "ok"
        assert preview.diff_available is True
        assert len(preview.diffs) == max_items
        assert preview.diff_truncated is True
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == column_count
        assert preview.transform_summary.changed_rows == 1

    def test_columns_without_declared_cast_never_produce_a_diff_end_to_end(self) -> None:
        # Currently normalize_table() never changes values of columns not in casts
        # (records_to_dataframe() carries original values as-is), so columns not in casts produce
        # no diff end-to-end — the "transform=None" branch here verifies _diff_sample() directly
        # (TestDiffSampleHelper below).
        #
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": "1", "label": "x"}]})

        result = preview_build(spec, client=client, limit=5)

        diffs_by_column = {d.column: d for d in result.previews[0].diffs}
        assert diffs_by_column["amount"].transform == "cast:int"
        assert "label" not in diffs_by_column
        assert "id" not in diffs_by_column


class TestDiffSampleHelper:
    """Verify ``_diff_sample`` directly — situation where columns not in casts change is currently
    Silver pipeline (values must pass declared cast to change) cannot reproduce end-to-end, so
    transform=None branch fixed at this pure function level.
    """

    def test_transform_is_null_when_column_has_no_declared_cast(self) -> None:
        diffs, summary, truncated = preview_module._diff_sample(
            [{"note": "before"}],
            [{"note": "after"}],
            columns=("note",),
            casts=None,
            max_items=preview_module.MAX_PREVIEW_DIFF_ITEMS,
        )

        assert len(diffs) == 1
        assert diffs[0].transform is None
        assert summary.changed_cells == 1
        assert summary.changed_rows == 1
        assert truncated is False

    def test_transform_is_null_when_column_not_in_casts_mapping(self) -> None:
        diffs, _summary, _truncated = preview_module._diff_sample(
            [{"note": "before", "amount": "1"}],
            [{"note": "after", "amount": 1}],
            columns=("note", "amount"),
            casts={"amount": "int"},
            max_items=preview_module.MAX_PREVIEW_DIFF_ITEMS,
        )

        by_column = {d.column: d for d in diffs}
        assert by_column["note"].transform is None
        assert by_column["amount"].transform == "cast:int"

    def test_no_change_across_all_rows_yields_zero_summary(self) -> None:
        diffs, summary, truncated = preview_module._diff_sample(
            [{"a": 1}, {"a": 2}],
            [{"a": 1}, {"a": 2}],
            columns=("a",),
            casts=None,
            max_items=preview_module.MAX_PREVIEW_DIFF_ITEMS,
        )

        assert diffs == ()
        assert summary.changed_cells == 0
        assert summary.changed_rows == 0
        assert truncated is False

    def test_max_items_caps_materialized_diffs_but_keeps_accurate_summary(self) -> None:
        # 497 sample/diff memory ceiling: limit only restricts row count, so in wide datasets the
        # diffs list itself can grow unbounded
        # — max_items truncates only the list, but changed_cells/changed_rows must hold the actual
        # untruncated sums.
        #
        columns = tuple(f"c{i}" for i in range(10))
        source_rows = [dict.fromkeys(columns, "before")]
        transformed_rows = [dict.fromkeys(columns, "after")]

        diffs, summary, truncated = preview_module._diff_sample(
            source_rows,
            transformed_rows,
            columns=columns,
            casts=None,
            max_items=4,
        )

        assert len(diffs) == 4
        assert truncated is True
        assert (
            summary.changed_cells == 10
        )  # Even if truncated, actual changed cell count is accurate.
        assert summary.changed_rows == 1

    def test_max_items_not_exceeded_leaves_truncated_false(self) -> None:
        columns = ("a", "b")
        diffs, summary, truncated = preview_module._diff_sample(
            [{"a": "1", "b": "2"}],
            [{"a": 1, "b": 2}],
            columns=columns,
            casts=None,
            max_items=2,
        )

        assert len(diffs) == 2
        assert truncated is False
        assert summary.changed_cells == 2

    def test_multiple_changed_cells_across_rows(self) -> None:
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int", "active": "bool"}),
        )
        spec = _spec(source)
        client = _FakeClient(
            {
                "datago.apt_trade": [
                    {"id": "1", "amount": "100", "active": "true"},
                    {"id": "2", "amount": "200", "active": "false"},
                    {"id": "3", "amount": "3", "active": "yes"},
                ]
            }
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.diff_available is True
        # All 3 rows × 2 columns (amount, active) change from strings to cast values.
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == 6
        assert preview.transform_summary.changed_rows == 3
        assert {d.column for d in preview.diffs} == {"amount", "active"}

    def test_diff_row_index_is_position_within_sample_not_absolute_row(self) -> None:
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        # limit=2 so sample holds only rows 0/1; diff row is the position within that array.
        client = _FakeClient(
            {
                "datago.apt_trade": [
                    {"id": "1", "amount": "10"},
                    {"id": "2", "amount": "20"},
                    {"id": "3", "amount": "30"},
                ]
            }
        )

        result = preview_build(spec, client=client, limit=2)

        rows = {d.row for d in result.previews[0].diffs}
        assert rows == {0, 1}

    def test_diff_unavailable_when_cast_introduces_nulls(self) -> None:
        # #188 data-loss guard: if casting drops a value to null, the entire preview fails (#188),
        # and diff does not appear half-successful — this is the actual safety mechanism
        # in this codebase that prevents "null changes" from leaking as diff items.
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        client = _FakeClient(
            {"datago.apt_trade": [{"id": "1", "amount": "100"}, {"id": "2", "amount": "oops"}]}
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.status == "failed"
        assert preview.diff_available is False
        assert preview.diffs == ()
        assert preview.transform_summary is None
        assert preview.source_sample == ()
        assert preview.diff_truncated is False

    def test_diff_unavailable_on_source_fetch_failure(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="missing"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        result = preview_build(spec, client=client)

        preview = result.previews[0]
        assert preview.status == "failed"
        assert preview.diff_available is False
        assert preview.diffs == ()
        assert preview.transform_summary is None
        assert preview.source_sample == ()
        assert preview.diff_truncated is False

    def test_diff_unavailable_when_row_count_is_not_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Today's Silver path does not filter/reorder rows (see test_silver.py
        # TestRowPreservingInvariant),
        # but simulate a hypothetical future where that assumption breaks to verify the count guard
        # operates fail-closed.
        real_build_silver_dataset = build_silver_dataset

        def _dropping_build_silver_dataset(bronze, **kwargs):  # type: ignore[no-untyped-def]
            silver = real_build_silver_dataset(bronze, **kwargs)
            frame = to_polars(silver.table)
            dropped_table = handle_from_frame(
                frame.head(frame.height - 1),
                connection=silver.table.connection,
                workdir=silver.table.workdir,
            )
            return type(silver)(
                table=dropped_table,
                schema=silver.schema,
                statistics=type(silver.statistics)(
                    row_count=dropped_table.height,
                    null_counts=silver.statistics.null_counts,
                    duplicate_rate=silver.statistics.duplicate_rate,
                ),
                preview=silver.preview,
                validation=silver.validation,
                source_bronze=silver.source_bronze,
            )

        monkeypatch.setattr(preview_module, "build_silver_dataset", _dropping_build_silver_dataset)
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(5)]})

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.status == "ok"  # Only diff is safely invalidated, not failure.
        assert preview.diff_available is False
        assert preview.diffs == ()
        assert preview.transform_summary is None
        assert preview.source_sample == ()
        assert preview.diff_truncated is False
        # sample(transformed) itself is still populated — only diff is not trustworthy.
        assert len(preview.preview.rows) == 4


# ---------------------------------------------------------------------------
# Regression (#497) — verify existing fields/behavior are preserved.
# ---------------------------------------------------------------------------


class TestRegression:
    def test_existing_fields_unaffected_by_new_sample_mode_param(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i), "v": i} for i in range(10)]})

        result = preview_build(spec, client=client, limit=3)

        preview = result.previews[0]
        assert preview.source_key == "datago.apt_trade"
        assert preview.status == "ok"
        assert [c.name for c in preview.schema.columns] == ["id", "v"]
        assert preview.preview.total_rows == 10
        assert len(preview.preview.rows) == 3
        assert preview.statistics.row_count == 10


# ---------------------------------------------------------------------------
# Canonical source contract (#498) — file/url kind previewed identically to public_api.
# ---------------------------------------------------------------------------


class TestSourceKinds:
    def test_preview_file_source_returns_schema_and_sample(self, tmp_path: Path) -> None:
        from kpubdata_builder.uploads import SQLiteUploadRepository

        repo = SQLiteUploadRepository(tmp_path / "uploads.sqlite3")
        metadata = repo.put(
            "owner-1",
            content=b"id,amount\n1,1000\n2,2500\n",
            format="csv",
            encoding="utf-8",
            original_filename=None,
        )
        spec = _spec(
            SourceRef(kind="file", upload_id=metadata.upload_id, format="csv", alias="uploaded")
        )

        result = preview_build(
            spec, client=_FakeClient({}), upload_repository=repo, owner_id="owner-1"
        )

        preview = result.previews[0]
        assert preview.source_key == "uploaded"
        assert preview.status == "ok"
        assert [c.name for c in preview.schema.columns] == ["id", "amount"]
        assert preview.preview.total_rows == 2

    def test_preview_file_source_without_owner_reports_failed_status(self, tmp_path: Path) -> None:
        from kpubdata_builder.uploads import SQLiteUploadRepository

        repo = SQLiteUploadRepository(tmp_path / "uploads.sqlite3")
        spec = _spec(
            SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv", alias="uploaded")
        )

        # upload_repository present but owner_id absent — unauthenticated preview request.
        result = preview_build(spec, client=_FakeClient({}), upload_repository=repo)

        preview = result.previews[0]
        assert preview.status == "failed"
        assert "authenticated" in (preview.error or "")

    def test_preview_file_source_without_repository_reports_failed_status(self) -> None:
        spec = _spec(
            SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv", alias="uploaded")
        )

        result = preview_build(spec, client=_FakeClient({}))

        preview = result.previews[0]
        assert preview.status == "failed"
        assert "upload store" in (preview.error or "")

    def test_preview_url_source_returns_schema_and_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kpubdata_builder.ingestion.url_fetch import FetchResult
        from kpubdata_builder.stages.bronze import resolve as resolve_module

        def _fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
            return FetchResult(
                content=b'[{"id": 1}, {"id": 2}]', content_type="application/json", final_url=url
            )

        monkeypatch.setattr(resolve_module, "safe_fetch_get", _fake_fetch)
        spec = _spec(SourceRef(kind="url", endpoint="https://example.org/data.json", alias="feed"))

        result = preview_build(spec, client=_FakeClient({}))

        preview = result.previews[0]
        assert preview.source_key == "feed"
        assert preview.status == "ok"
        assert preview.preview.total_rows == 2


def test_preview_applies_the_same_transform_rules_as_build() -> None:
    """Preview and Build show same columns in same declaration (#611).

    Preview is user's path to check results before Build. If rename/derived missing only in
    Preview, state is "column absent in preview but present in build output"
    breaking the Preview↔Build identical judgment principle #486 established.
    """
    from kpubdata_builder.spec.models import DerivedColumn

    spec = _spec(
        SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(
                rename={"sggCd": "district_code"},
                derived=(
                    DerivedColumn(
                        name="deal_date",
                        kind="date_parts",
                        columns=("dealYear", "dealMonth", "dealDay"),
                    ),
                ),
            ),
        )
    )
    client = _FakeClient(
        {
            "datago.apt_trade": [
                {"sggCd": "11110", "dealYear": "2026", "dealMonth": "9", "dealDay": "8"}
            ]
        }
    )

    result = preview_build(spec, client=client, limit=5)

    columns = [c.name for c in result.previews[0].schema.columns]
    assert "district_code" in columns
    assert "deal_date" in columns


class TestPreviewAppliesSilverDeclarations:
    """preview and build see the same declaration (#620).

    if both see different data, preview confirmation does not guarantee build.
    """

    def _records(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "이동거리": "1210",
                "이동거리(M)": None,
                "대여소번호": "3",
                "ym": "2020-01",
                "성별": "",
            },
            {
                "이동거리": None,
                "이동거리(M)": "980",
                "대여소번호": "102",
                "ym": "202207",
                "성별": "M",
            },
        ]

    def _schema(self) -> SchemaContract:
        return SchemaContract(
            column_null_tokens={"성별": ColumnNullTokens(tokens=("",))},
            coalesce={"move_meter": ("이동거리", "이동거리(M)")},
            rename={"대여소번호": "station_no"},
            zfill={"station_no": 5},
            casts={"ym": "year_month"},
        )

    def test_preview_output_matches_build_output(self) -> None:
        source = SourceRef(provider="datago", dataset="apt_trade", schema=self._schema())
        client = _FakeClient({"datago.apt_trade": self._records()})

        preview = preview_build(_spec(source), client=client, limit=10).previews[0]

        assert preview.status == "ok"
        columns = [c.name for c in preview.schema.columns]
        assert "move_meter" in columns
        assert "이동거리" not in columns
        assert [row["station_no"] for row in preview.preview.rows] == ["00003", "00102"]
        assert [row["ym"] for row in preview.preview.rows] == ["2020-01", "2022-07"]
        assert [row["성별"] for row in preview.preview.rows] == [None, "M"]

    def test_preview_surfaces_the_same_failure_as_build(self) -> None:
        """zfill width overflow must appear as drift signal in preview too."""
        source = SourceRef(provider="datago", dataset="apt_trade", schema=self._schema())
        records = self._records()
        records[0]["대여소번호"] = "1234567"
        client = _FakeClient({"datago.apt_trade": records})

        preview = preview_build(_spec(source), client=client, limit=10).previews[0]

        assert preview.status == "failed"
