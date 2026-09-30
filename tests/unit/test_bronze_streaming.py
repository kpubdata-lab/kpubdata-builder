"""Bronze is written as records arrive, never held whole (#622)."""

from __future__ import annotations

import datetime as dt
import gc
import io
import json
import math
import random
import tracemalloc
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from kpubdata_builder.ingestion import IngestionError, tabular_ingest
from kpubdata_builder.ingestion.tabular_ingest import iter_tabular_batches, parse_tabular_bytes
from kpubdata_builder.manifest.provenance import (
    compute_data_checksum,
    compute_data_checksum_from_jsonl,
)
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.pipeline.preview import preview_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.stages.bronze import persist_bronze_artifact
from kpubdata_builder.stages.bronze.build import build_bronze_artifact
from kpubdata_builder.stages.bronze.models import BronzeArtifact
from kpubdata_builder.stages.bronze.writer import (
    WORKING_NAME,
    BronzeWriter,
    decode_record,
    encode_record,
)
from kpubdata_builder.uploads import SQLiteUploadRepository
from kpubdata_builder.uploads.store import UploadContentCorrupted

# ------------------------------------------------------------------ fake sources


class _Page:
    def __init__(self, items: list[dict[str, JsonValue]], total: int | None = None) -> None:
        self.items = items
        self.total_count = total


class _PagedDataset:
    """A ``list_all`` source whose pages are made on demand, like a real paginated API."""

    def __init__(
        self,
        pages: int,
        per_page: int,
        *,
        fail_on: int | None = None,
        on_page: object = None,
    ) -> None:
        self._pages = pages
        self._per_page = per_page
        self._fail_on = fail_on
        self._on_page = on_page
        self.served = 0

    def list(self, **_params: object) -> _Page:  # pragma: no cover - list_all is used
        raise AssertionError("list_all is the paginated path")

    def list_all(self, **_params: object) -> Iterator[_Page]:
        for page in range(self._pages):
            if self._on_page is not None:
                self._on_page(page)  # type: ignore[operator]
            if page == self._fail_on:
                raise RuntimeError("provider went away")
            self.served += 1
            yield _Page(
                [
                    {"page": page, "row": row, "pad": "x" * 200, "name": f"station-{row}"}
                    for row in range(self._per_page)
                ],
                total=self._pages * self._per_page,
            )


class _Client:
    def __init__(self, dataset: object) -> None:
        self._dataset = dataset

    def dataset(self, _key: str) -> object:
        return self._dataset


# ------------------------------------------------------------------ writer


def test_the_working_copy_keeps_key_order_and_types() -> None:
    record: dict[str, object] = {
        "z": 1,
        "a": {"y": 2, "b": [1, 2.5, None]},
        "d": dt.date(2025, 1, 2),
        "ts": dt.datetime(2025, 1, 1, 21, 30, tzinfo=dt.timezone(dt.timedelta(hours=9))),
        "zoned": dt.datetime(2025, 3, 1, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        "naive": dt.datetime(2025, 1, 1, 12, 0),
        "t": dt.time(8, 15),
        "gap": dt.timedelta(days=1, seconds=5, microseconds=7),
        "amount": Decimal("12.50"),
        "pair": (1, "a"),
        "raw": b"\x00\xff",
        "nan": math.nan,
        "tricky": {"\x00kpubdata": "date", "v": "not a tag"},
    }

    back = decode_record(encode_record(record))

    assert list(back) == list(record)
    assert list(cast(dict[str, object], back["a"])) == ["y", "b"]
    for key in ("d", "ts", "naive", "t", "gap", "amount", "pair", "raw", "tricky"):
        assert back[key] == record[key], key
        assert type(back[key]) is type(record[key]), key
    zoned = cast(dt.datetime, back["zoned"])
    assert zoned == record["zoned"] and zoned.tzinfo == ZoneInfo("Asia/Seoul")
    assert math.isnan(cast(float, back["nan"]))


def test_an_unknown_object_is_refused_as_json_would() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        encode_record({"x": object()})


def test_a_writer_left_without_commit_removes_everything(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    with BronzeWriter(staging) as writer:
        writer.write_batch([{"a": 1}])

    assert not staging.exists()


def test_a_failing_write_removes_everything(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    with pytest.raises(RuntimeError), BronzeWriter(staging) as writer:
        writer.write_batch([{"a": 1}])
        raise RuntimeError("disk full")

    assert not staging.exists()


def test_persisted_bronze_bytes_are_the_sorted_key_lines_as_before(tmp_path: Path) -> None:
    records: list[dict[str, JsonValue]] = [{"b": 1, "a": {"d": 2, "c": "한"}}, {"x": None}]
    artifact = BronzeArtifact.from_records("datago.t", records, staging_dir=tmp_path / "s")

    paths = persist_bronze_artifact(artifact, output_root=tmp_path / "out", run_id="r1")

    expected = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for r in records
    )
    assert paths.records_path.read_text(encoding="utf-8") == expected
    # The working copy keeps the source's order for the stages that read it.
    assert list(next(artifact.iter_records())) == ["b", "a"]


def test_nan_still_fails_at_persist_not_before(tmp_path: Path) -> None:
    """#201 is kept where it was: Bronze is fetched, and persisting it fails."""
    artifact = BronzeArtifact.from_records("datago.t", [{"v": math.nan}])

    with pytest.raises(ValueError, match="Out of range float"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="r1")
    artifact.discard()


# ------------------------------------------------------------------ pages


def test_each_page_is_on_disk_before_the_next_is_fetched(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    seen: list[int] = []

    def before_page(page: int) -> None:
        if page:
            with (staging / WORKING_NAME).open(encoding="utf-8") as handle:
                seen.append(sum(1 for _ in handle))

    build_bronze_artifact(
        _Client(_PagedDataset(4, 10, on_page=before_page)),
        source_key="datago.paged",
        staging_dir=staging,
    )

    assert seen == [10, 20, 30]


def test_a_failing_page_leaves_no_partial_bronze(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    dataset = _PagedDataset(5, 10, fail_on=3)

    with pytest.raises(RuntimeError, match="went away"):
        build_bronze_artifact(_Client(dataset), source_key="datago.paged", staging_dir=staging)

    assert dataset.served == 3
    assert not staging.exists()


def test_a_large_paged_source_is_never_held_in_memory(tmp_path: Path) -> None:
    """A source far larger than the memory Bronze is allowed goes to disk page by page."""
    pages, per_page = 60, 1_000  # ~15 MB of records; one page is ~0.25 MB
    gc.collect()
    tracemalloc.start()
    try:
        artifact = build_bronze_artifact(
            _Client(_PagedDataset(pages, per_page)),
            source_key="datago.paged",
            staging_dir=tmp_path / "s",
        )
        paths = persist_bronze_artifact(artifact, output_root=tmp_path / "out", run_id="r1")
        checksum = compute_data_checksum_from_jsonl(paths.records_path, run_bytes=2_000_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    size = paths.records_path.stat().st_size
    assert artifact.record_count == pages * per_page
    assert size > 10_000_000
    assert peak < size / 4, f"peak {peak} bytes for {size} bytes of Bronze"
    assert checksum.startswith("sha256:")


# ------------------------------------------------------------------ checksum


def test_the_file_checksum_equals_the_in_memory_one(tmp_path: Path) -> None:
    rng = random.Random(622)
    records: list[dict[str, JsonValue]] = [
        {"k": rng.randint(0, 50), "s": rng.choice(["가", "b", "c"]) * rng.randint(0, 5)}
        for _ in range(500)
    ]
    path = tmp_path / "raw_records.jsonl"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )

    expected = compute_data_checksum(records)

    assert compute_data_checksum_from_jsonl(path) == expected
    # Many small sorted runs merged give the same value as one sort.
    assert compute_data_checksum_from_jsonl(path, run_bytes=200) == expected
    assert not list(tmp_path.glob(".checksum-*"))


def test_the_checksum_of_no_records(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    assert compute_data_checksum_from_jsonl(path) == compute_data_checksum([])


# ------------------------------------------------------------------ parser


@pytest.fixture
def tiny_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunks of 7 bytes, so every boundary case — a split character, a split line, a
    split JSON value — happens on small inputs."""
    monkeypatch.setattr(tabular_ingest, "_CHUNK_BYTES", 7)


def _batches(raw: bytes, **kwargs: object) -> list[list[dict[str, JsonValue]]]:
    return list(iter_tabular_batches(io.BytesIO(raw), batch_records=2, **kwargs))  # type: ignore[arg-type]


@pytest.mark.usefixtures("tiny_chunks")
def test_json_is_read_element_by_element() -> None:
    raw = json.dumps(
        [{"이름": "강남", "n": 12345}, {"이름": "서초", "n": 1.5}, {"x": [1, {"y": None}]}],
        ensure_ascii=False,
    ).encode("utf-8")

    batches = _batches(raw, format="json")

    assert [len(b) for b in batches] == [2, 1]
    assert [r for b in batches for r in b] == json.loads(raw)


@pytest.mark.usefixtures("tiny_chunks")
@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'{"a": 1}', "array of objects"),
        (b"[1, 2]", "array of objects"),
        (b'[{"a": 1},]', "failed to parse json"),
        (b'[{"a": 1}] x', "failed to parse json"),
        (b'[{"a": 1} {"b": 2}]', "failed to parse json"),
        (b'[{"a": ', "failed to parse json"),
        (b"not json", "failed to parse json"),
    ],
)
def test_json_errors_are_the_same_kinds_as_before(raw: bytes, message: str) -> None:
    with pytest.raises(IngestionError, match=message):
        _batches(raw, format="json")


@pytest.mark.usefixtures("tiny_chunks")
def test_jsonl_split_across_chunks() -> None:
    raw = '{"a": "가나다"}\r\n\n{"a": 2}\n{"a": 3}'.encode()

    records = [r for b in _batches(raw, format="jsonl") for r in b]

    assert records == [{"a": "가나다"}, {"a": 2}, {"a": 3}]


@pytest.mark.usefixtures("tiny_chunks")
def test_csv_in_batches_keeps_whole_file_inference() -> None:
    """A float in the last row still makes the column Float64 in the first batch."""
    rows = "".join(f"{i},00{i},서울\n" for i in range(9)) + "1.5,7,부산\n"
    raw = ("a,code,city\n" + rows).encode("cp949")

    batches = _batches(raw, format="csv", encoding="cp949", read_as={"code": "str"})

    assert len(batches) == 5
    assert batches[0][0] == {"a": 0.0, "code": "000", "city": "서울"}
    whole = parse_tabular_bytes(raw, format="csv", encoding="cp949", read_as={"code": "str"})
    assert [r for b in batches for r in b] == list(whole)


def test_a_decode_error_is_reported_as_before() -> None:
    with pytest.raises(IngestionError, match="failed to decode"):
        _batches("a\n가\n".encode("cp949"), format="csv", encoding="utf-8")


# ------------------------------------------------------------------ uploads


def test_a_large_upload_is_streamed_from_its_file(tmp_path: Path) -> None:
    repository = SQLiteUploadRepository(tmp_path / "u.sqlite3", spill_threshold_bytes=16)
    content = b'{"a": 1}\n{"a": 2}\n'
    metadata = repository.put(
        "owner", content=content, format="jsonl", encoding="utf-8", original_filename=None
    )

    stream = repository.open_content("owner", metadata.upload_id)

    assert stream is not None and not isinstance(stream, io.BytesIO)
    with stream:
        assert stream.read() == content
    assert repository.open_content("someone-else", metadata.upload_id) is None


def test_a_small_upload_opens_from_its_row(tmp_path: Path) -> None:
    repository = SQLiteUploadRepository(tmp_path / "u.sqlite3")
    metadata = repository.put(
        "owner", content=b"a\n1\n", format="csv", encoding="utf-8", original_filename=None
    )

    stream = repository.open_content("owner", metadata.upload_id)

    assert stream is not None and stream.read() == b"a\n1\n"


def test_a_tampered_upload_file_is_refused(tmp_path: Path) -> None:
    repository = SQLiteUploadRepository(tmp_path / "u.sqlite3", spill_threshold_bytes=16)
    metadata = repository.put(
        "owner",
        content=b'{"a": 1}\n{"a": 2}\n',
        format="jsonl",
        encoding="utf-8",
        original_filename=None,
    )
    (blob,) = (tmp_path / "u.sqlite3.blobs").iterdir()
    blob.write_bytes(b'{"a": 9}\n{"a": 9}\n')

    with pytest.raises(UploadContentCorrupted, match="checksum"):
        repository.open_content("owner", metadata.upload_id)


# ------------------------------------------------------------------ runs


def _spec(**source: object) -> BuildSpec:
    return BuildSpec(
        dataset_id="stream.test",
        title="Stream",
        description="d",
        sources=(SourceRef(provider="datago", dataset="paged", **source),),  # type: ignore[arg-type]
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def test_a_build_leaves_no_staging_behind(tmp_path: Path) -> None:
    result = run_build(
        _spec(), client=_Client(_PagedDataset(3, 5)), output_root=tmp_path, run_id="r1"
    )

    assert result.status == "ok"
    assert not (tmp_path / "r1" / "_bronze_staging").exists()
    (records,) = (tmp_path / "r1").glob("bronze/*/*/raw_records.jsonl")
    assert len(records.read_text(encoding="utf-8").splitlines()) == 15


def test_staging_a_crashed_attempt_left_is_removed(tmp_path: Path) -> None:
    leftover = tmp_path / "r1" / "_bronze_staging" / "datago.paged"
    leftover.mkdir(parents=True)
    (leftover / WORKING_NAME).write_text('{"stale": true}\n', encoding="utf-8")

    run_build(_spec(), client=_Client(_PagedDataset(1, 2)), output_root=tmp_path, run_id="r1")

    (records,) = (tmp_path / "r1").glob("bronze/*/*/raw_records.jsonl")
    assert "stale" not in records.read_text(encoding="utf-8")
    assert not leftover.exists()


def test_a_failing_fetch_leaves_neither_bronze_nor_staging(tmp_path: Path) -> None:
    result = run_build(
        _spec(),
        client=_Client(_PagedDataset(4, 5, fail_on=2)),
        output_root=tmp_path,
        run_id="r1",
    )

    assert result.outcomes[0].status == "failed"
    assert not list((tmp_path / "r1").glob("bronze/**/raw_records.jsonl"))
    assert not (tmp_path / "r1" / "_bronze_staging").exists()


def test_preview_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    result = preview_build(_spec(), client=_Client(_PagedDataset(2, 3)), limit=2)

    assert result.previews[0].status == "ok"
    assert not list(tmp_path.glob("kpubdata-bronze-*"))
