"""An uploaded Parquet file's parse cost is bounded by its footer, not its size (#1335).

Upload admission parses the whole file to prove it parses. A Parquet file of a few
kilobytes can declare millions of rows of one repeated value, thousands of columns or
deeply nested types, so the upload byte limit alone did not bound the work. The footer
is now read first and a file that asks for more than the upload budget is refused
with the usual 400 ``{"error": ...}`` before a single row is read, and nothing is stored.
"""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.tabular_ingest import ParquetLimits, iter_tabular_batches
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.upload_limits import (
    DEFAULT_UPLOAD_MAX_CELLS,
    UPLOAD_MAX_CELLS_ENV,
    resolve_upload_parquet_limits,
)
from kpubdata_builder.spec import JsonValue

_PRINCIPAL = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")


def _parquet(tmp_path: Path, select: str, name: str = "upload.parquet") -> bytes:
    path = tmp_path / name
    with duckdb.connect(":memory:") as connection:
        connection.execute(f"COPY ({select}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")
    return path.read_bytes()


def _service(tmp_path: Path) -> BuilderService:
    (tmp_path / "out").mkdir(exist_ok=True)
    return BuilderService(
        output_root=tmp_path / "out",
        client_factory=lambda **_: None,
        warehouse_root=tmp_path / "wh",
    )


def _upload(service: BuilderService, raw: bytes) -> tuple[int, dict[str, object]]:
    response = service.create_upload(
        raw,
        format="parquet",
        encoding="utf-8",
        original_filename="upload.parquet",
        principal=_PRINCIPAL,
    )
    return response.status_code, dict(response.body)


def _read(raw: bytes, limits: ParquetLimits) -> Iterator[list[dict[str, JsonValue]]]:
    return iter_tabular_batches(io.BytesIO(raw), format="parquet", parquet_limits=limits)


def _bomb(tmp_path: Path) -> bytes:
    """A few tens of kilobytes declaring 6M rows x 10 columns of zeros: 60M cells."""
    columns = ", ".join(f"0::BIGINT c{i}" for i in range(10))
    return _parquet(tmp_path, f"SELECT {columns} FROM range(6000000)", "bomb.parquet")


class TestUploadAdmission:
    def test_small_file_declaring_many_cells_is_refused_without_reading(
        self, tmp_path: Path
    ) -> None:
        raw = _bomb(tmp_path)
        assert len(raw) < 1024 * 1024
        service = _service(tmp_path)

        started = time.monotonic()
        status, body = _upload(service, raw)
        elapsed = time.monotonic() - started

        assert status == 400
        assert set(body) == {"error"}
        error = str(body["error"])
        assert "60000000 cells" in error
        assert str(DEFAULT_UPLOAD_MAX_CELLS) in error
        # Refused from the footer: reading every row took about ten seconds.
        assert elapsed < 2.0
        # Nothing is stored for a refused upload: no metadata, no content.
        assert service.list_uploads(principal=_PRINCIPAL).body["uploads"] == []

    def test_deeply_nested_columns_are_refused(self, tmp_path: Path) -> None:
        value = "1"
        for _ in range(40):
            value = f"struct_pack(x := {value})"
        raw = _parquet(tmp_path, f"SELECT {value} AS deep")

        status, body = _upload(_service(tmp_path), raw)

        assert status == 400
        # The column, 39 structs inside it, and the leaf.
        assert "nested 41 levels deep" in str(body["error"])

    def test_normal_file_still_uploads(self, tmp_path: Path) -> None:
        raw = _parquet(
            tmp_path,
            "SELECT range AS id, 'name-' || range AS name, {'a': [range, 1]} AS nested "
            "FROM range(50000)",
        )

        status, body = _upload(_service(tmp_path), raw)

        assert status == 200
        assert body["format"] == "parquet"

    def test_invalid_file_keeps_the_existing_error(self, tmp_path: Path) -> None:
        status, body = _upload(_service(tmp_path), b"PAR1 not really parquet PAR1")

        assert status == 400
        assert body == {
            "error": "failed to parse parquet content: not a Parquet file Builder can read"
        }

    def test_cell_limit_follows_the_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = _parquet(tmp_path, "SELECT range AS a, range AS b FROM range(1000)")
        monkeypatch.setenv(UPLOAD_MAX_CELLS_ENV, "1999")
        status, body = _upload(_service(tmp_path), raw)
        assert status == 400
        assert "2000 cells" in str(body["error"])

        monkeypatch.setenv(UPLOAD_MAX_CELLS_ENV, "2000")
        assert _upload(_service(tmp_path), raw)[0] == 200

    def test_malformed_bomb_does_not_hold_up_a_normal_upload(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        bomb = _bomb(tmp_path)
        normal = _parquet(tmp_path, "SELECT range AS id FROM range(1000)", "normal.parquet")
        results: dict[str, int] = {}

        def send(name: str, raw: bytes) -> None:
            results[name] = _upload(service, raw)[0]

        threads = [threading.Thread(target=send, args=(f"bomb{i}", bomb)) for i in range(4)]
        threads.append(threading.Thread(target=send, args=("normal", normal)))
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert time.monotonic() - started < 5.0
        assert results == {"bomb0": 400, "bomb1": 400, "bomb2": 400, "bomb3": 400, "normal": 200}
        uploads = service.list_uploads(principal=_PRINCIPAL).body["uploads"]
        assert isinstance(uploads, list) and len(uploads) == 1


class TestFooterLimits:
    def test_too_many_columns(self, tmp_path: Path) -> None:
        raw = _parquet(tmp_path, "SELECT " + ", ".join(f"1 c{i}" for i in range(12)))
        with pytest.raises(IngestionError, match="12 columns.*at most 10"):
            list(_read(raw, ParquetLimits(max_cells=None, max_columns=10)))

    def test_nested_leaves_count_as_columns(self, tmp_path: Path) -> None:
        raw = _parquet(tmp_path, "SELECT {'a': 1, 'b': 2, 'c': 3} AS s")
        with pytest.raises(IngestionError, match="3 columns"):
            list(_read(raw, ParquetLimits(max_cells=None, max_columns=2)))

    def test_too_many_row_groups(self, tmp_path: Path) -> None:
        path = tmp_path / "groups.parquet"
        with duckdb.connect(":memory:") as connection:
            connection.execute(
                f"COPY (SELECT range AS a FROM range(10000)) TO '{path}' "
                "(FORMAT parquet, ROW_GROUP_SIZE 2048)"
            )
        with pytest.raises(IngestionError, match="5 row groups.*at most 4"):
            list(_read(path.read_bytes(), ParquetLimits(max_cells=None, max_row_groups=4)))

    def test_too_many_decompressed_bytes(self, tmp_path: Path) -> None:
        raw = _parquet(tmp_path, "SELECT repeat('x', 1000) || range AS s FROM range(2000)")
        assert len(raw) < 100_000
        with pytest.raises(IngestionError, match="decompress"):
            list(_read(raw, ParquetLimits(max_cells=None, max_uncompressed_bytes=1_000_000)))

    def test_rows_beyond_the_footer_are_not_read(self, tmp_path: Path) -> None:
        raw = _parquet(tmp_path, "SELECT range AS a FROM range(100)")
        # Within the limit by the footer; the guard while reading holds the same limit.
        assert sum(map(len, _read(raw, ParquetLimits(max_cells=100)))) == 100

    def test_without_limits_nothing_changes(self, tmp_path: Path) -> None:
        raw = _parquet(tmp_path, "SELECT " + ", ".join(f"1 c{i}" for i in range(12)))
        batches = list(iter_tabular_batches(io.BytesIO(raw), format="parquet"))
        assert len(batches[0][0]) == 12


class TestSetting:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(UPLOAD_MAX_CELLS_ENV, raising=False)
        assert resolve_upload_parquet_limits().max_cells == DEFAULT_UPLOAD_MAX_CELLS

    @pytest.mark.parametrize(
        ("raw", "expected"), [("0", None), ("abc", 50_000_000), ("-3", 50_000_000), ("7", 7)]
    )
    def test_values(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int | None) -> None:
        monkeypatch.setenv(UPLOAD_MAX_CELLS_ENV, raw)
        assert resolve_upload_parquet_limits().max_cells == expected
