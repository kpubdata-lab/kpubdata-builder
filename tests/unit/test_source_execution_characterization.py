"""What a build records, pinned before the source pipeline is restructured (#1265).

Each scenario runs a real ``BuilderService`` build on fixed provider answers and reduces
what it left behind to the parts a caller or an administrator reads: the manifest, the
run's events, its recorded ending, its build index row and the ``failures`` codes and
stages (#1120, #1267). Timestamps, environment facts and the temporary root are left
out; everything else is compared whole.

The scenarios cover a success (with quality, PII masking and drift against an earlier
run), a provider refusal, a quality and a PII gate, a cancellation at every boundary,
an export that fails, a run a restart interrupted, a param_grid retry that continues
from its checkpoint, and a composition.

The baselines in ``tests/golden/source_execution/`` are written only by running this
module: ``uv run python -m tests.unit.test_source_execution_characterization``. It runs
each scenario twice and refuses to write when the two runs differ. A change to a
baseline is a change to what builds record, and its diff is the record of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import duckdb
import kpubdata
import pytest

from kpubdata_builder.catalog_info import DatasetCatalogInfo
from kpubdata_builder.events.store import events_store_path
from kpubdata_builder.pipeline import card_facts, orchestrator
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import SourceClient

GOLDEN = Path(__file__).parents[1] / "golden" / "source_execution"

#: Manifest keys that hold a time or describe the machine, not the run.
_VARIABLE_KEYS = frozenset(
    {"started_at", "finished_at", "fetched_at", "collected_at", "build_environment"}
)
_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")
#: The minute a dataset card says the data was collected in.
_CARD_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
_TEXT_SUFFIXES = frozenset({".json", ".jsonl", ".csv", ".md", ".yaml"})


# ---------------------------------------------------------------------- providers


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


@dataclass(frozen=True)
class _License:
    pii_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Ref:
    license: _License | None = None


@dataclass
class _Source:
    """A provider answering ``rows``, or raising ``error``; ``gate`` holds a fetch."""

    rows: list[dict[str, JsonValue]] = field(default_factory=list)
    error: Exception | None = None
    pii: tuple[str, ...] = ()
    gate: threading.Event | None = None
    fail_on: str | None = None
    fetching: threading.Event = field(default_factory=threading.Event)

    @property
    def ref(self) -> _Ref:
        return _Ref(license=_License(pii_columns=self.pii))

    def list(self, **params: JsonValue) -> _Result:
        self.fetching.set()
        if self.gate is not None:
            self.gate.wait(timeout=20)
        if self.error is not None:
            raise self.error
        sido = params.get("sido")
        if sido is not None:
            if sido == self.fail_on:
                raise RuntimeError("provider went away")
            return _Result([{"sido": sido, "n": 1}, {"sido": sido, "n": 2}])
        return _Result([dict(row) for row in self.rows])

    def dataset(self, _key: str) -> _Source:
        return self


def _factory(source: _Source) -> Callable[..., SourceClient]:
    def create(
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> SourceClient:
        return cast(SourceClient, source)

    return create


class _CancelAt:
    """Reports a cancellation from its ``after``-th check on; ``None`` refuses the commit."""

    def __init__(self, *, after: int | None) -> None:
        self._after = after
        self._checks = 0

    def cancel_requested(self) -> bool:
        self._checks += 1
        return self._after is not None and self._checks > self._after

    def commit(self) -> bool:
        return self._after is not None


# -------------------------------------------------------------------------- specs

_PERMITS: list[dict[str, JsonValue]] = [
    {"id": "1", "name": "shop 1", "tel": "010-0000-0000", "v": 10},
    {"id": "2", "name": "shop 2", "tel": "02-000-0000", "v": 20},
    {"id": "3", "name": "shop 3", "tel": None, "v": 30},
]


def _spec(*, extra: str = "", source_extra: str = "", composition: bool = False) -> str:
    second = "  - provider: datago\n    dataset: air_station\n    alias: b\n" if composition else ""
    join = (
        "composition:\n  name: combined\n  join: {left: m, right: b, left_key: id, right_key: id}\n"
        if composition
        else ""
    )
    return (
        "dataset_id: permits.table\ntitle: Permits\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: m\n"
        "    schema:\n      required: [id]\n"
        + source_extra
        + second
        + join
        + extra
        + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
        "  - kind: csv\n    output_path: data.csv\n"
    )


_GRID = """\
dataset_id: grid.table
title: Grid
description: d
sources:
  - provider: datago
    dataset: air_quality
    param_grid:
      sido: [a, b, c, d]
exports:
  - kind: jsonl
    output_path: data.jsonl
"""

_QUALITY_WARN = "quality:\n  min_rows: 100\n"
_QUALITY_FAIL = "quality:\n  min_rows: 100\n  min_rows_severity: fail\n"
_PII_WARN = "pii:\n  mode: warn\n"
_PII_BLOCK = "pii:\n  mode: block\n"


# ---------------------------------------------------------------------- snapshot


def _scrub(value: object, root: Path) -> object:
    if isinstance(value, dict):
        return {
            key: _scrub(item, root)
            for key, item in sorted(value.items())
            if key not in _VARIABLE_KEYS
        }
    if isinstance(value, list):
        return [_scrub(item, root) for item in value]
    if isinstance(value, str):
        return _CARD_TIME.sub("<time>", _ISO_TIME.sub("<time>", value.replace(str(root), "<root>")))
    return value


def _files(run_dir: Path) -> dict[str, object]:
    """Every file the run wrote besides its manifest: text as text, Parquet as rows.

    The manifest's ``artifact_digest`` covers Gold's bytes, a card's collection time
    among them, so it is compared here instead, file by file, with the time left out.
    """
    files: dict[str, object] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        name = str(path.relative_to(run_dir))
        if path.suffix in _TEXT_SUFFIXES:
            text = path.read_text("utf-8")
            if path.suffix == ".json":
                files[name] = json.loads(text)
            elif path.suffix == ".jsonl":
                files[name] = [json.loads(line) for line in text.splitlines()]
            else:
                files[name] = text.splitlines()
        elif path.suffix == ".parquet":
            files[name] = _parquet(path)
        else:
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _parquet(path: Path) -> dict[str, object]:
    """A Parquet file's columns and rows: its bytes carry the writer's version."""
    with duckdb.connect() as connection:
        result = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        columns = [[column[0], str(column[1])] for column in result.description or ()]
        rows = [[None if v is None else str(v) for v in row] for row in result.fetchall()]
    return {"columns": columns, "rows": rows}


def _events(service: BuilderService, run_id: str) -> dict[str, list[list[object]]]:
    """The run's events, run-level ones apart and each source's in its own order.

    Sources run concurrently, so only the order within a source is the pipeline's.
    """
    grouped: dict[str, list[list[object]]] = {}
    for event in service._event_store.list_for_run(run_id, limit=1000, tail=False):
        grouped.setdefault(event.source_key or "<run>", []).append(
            [event.event, event.status, event.stage, event.message, event.metrics]
        )
    return dict(sorted(grouped.items()))


def _endings(root: Path, run_id: str) -> list[list[object]]:
    if not events_store_path(root).exists():
        return []
    with sqlite3.connect(events_store_path(root)) as conn:
        rows = conn.execute(
            "SELECT status, stage, code, summary FROM run_endings WHERE run_id = ?", (run_id,)
        ).fetchall()
    return [list(row) for row in rows]


def _index(service: BuilderService, run_id: str) -> dict[str, object] | None:
    for entry in service._build_index.list_builds(limit=None):
        if entry.run_id == run_id:
            return {
                "status": entry.status,
                "error": entry.error,
                "error_code": entry.error_code,
                "error_stage": entry.error_stage,
                "dataset_id": entry.dataset_id,
            }
    return None


def _snapshot(service: BuilderService, root: Path, run_id: str) -> dict[str, object]:
    manifest_path = root / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else None
    for artifact in (manifest or {}).get("artifacts", {}).values():
        artifact["artifact_digest"] = "<digest>"
    return cast(
        dict[str, object],
        _scrub(
            {
                "manifest": manifest,
                "files": _files(root / run_id),
                "events": _events(service, run_id),
                "endings": _endings(root, run_id),
                "index": _index(service, run_id),
            },
            root,
        ),
    )


def _service(root: Path, source: _Source) -> BuilderService:
    return BuilderService(output_root=root, client_factory=_factory(source))


def _stop(*services: BuilderService) -> None:
    for service in services:
        service._async_builds._executor.shutdown(wait=True)


# ---------------------------------------------------------------------- scenarios

Scenario = Callable[[Path], dict[str, object]]


def _run(
    root: Path,
    spec: str,
    source: _Source,
    *,
    run_id: str = "r1",
    before: tuple[str, ...] = (),
    cancellation: _CancelAt | None = None,
    retry_of: str | None = None,
) -> dict[str, object]:
    service = _service(root, source)
    try:
        for earlier in before:
            assert service.build(spec, run_id=earlier).status_code == 200
        response = service.build(spec, run_id=run_id, cancellation=cancellation, retry_of=retry_of)
        snapshot = _snapshot(service, root, run_id)
        snapshot["response_status"] = response.status_code
        return snapshot
    finally:
        _stop(service)


def _success(root: Path) -> dict[str, object]:
    return _run(
        root,
        _spec(extra=_QUALITY_WARN + _PII_WARN),
        _Source(rows=_PERMITS, pii=("tel",)),
        before=("baseline",),
    )


def _provider_error(root: Path) -> dict[str, object]:
    error = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    return _run(root, _spec(), _Source(error=error))


def _quality_fail(root: Path) -> dict[str, object]:
    return _run(root, _spec(extra=_QUALITY_FAIL), _Source(rows=_PERMITS))


def _pii_block(root: Path) -> dict[str, object]:
    return _run(root, _spec(extra=_PII_BLOCK), _Source(rows=_PERMITS))


def _cancelled(after: int | None) -> Scenario:
    def scenario(root: Path) -> dict[str, object]:
        return _run(root, _spec(), _Source(rows=_PERMITS), cancellation=_CancelAt(after=after))

    return scenario


@contextmanager
def _failing_export() -> Iterator[None]:
    original = getattr(orchestrator, "export_gold_package")  # noqa: B009

    def fail(*_args: object, **_kwargs: object) -> list[Path]:
        raise OSError("disk full")

    setattr(orchestrator, "export_gold_package", fail)  # noqa: B010 - also run outside pytest
    try:
        yield
    finally:
        setattr(orchestrator, "export_gold_package", original)  # noqa: B010


def _export_failure(root: Path) -> dict[str, object]:
    with _failing_export():
        return _run(root, _spec(), _Source(rows=_PERMITS))


def _interrupted(root: Path) -> dict[str, object]:
    gate = threading.Event()
    held = _Source(rows=_PERMITS, gate=gate)
    first = _service(root, held)
    second: BuilderService | None = None
    try:
        assert first.submit_build(_spec(), run_id="in-flight").status_code == 202
        assert held.fetching.wait(10)
        second = _service(root, _Source(rows=_PERMITS))
        assert second.mark_interrupted_runs() == ("in-flight",)
        return _snapshot(second, root, "in-flight")
    finally:
        gate.set()
        _stop(first, *((second,) if second is not None else ()))


def _retry(root: Path) -> dict[str, object]:
    service = _service(root, _Source(fail_on="c"))
    try:
        assert service.build(_GRID, run_id="r1").status_code != 200
        failed = _snapshot(service, root, "r1")
    finally:
        _stop(service)
    retried = _run(root, _GRID, _Source(), run_id="r2", retry_of="r1")
    return {"failed": failed, "retried": retried}


def _composition(root: Path) -> dict[str, object]:
    return _run(root, _spec(composition=True), _Source(rows=_PERMITS))


SCENARIOS: dict[str, Scenario] = {
    "success": _success,
    "provider_error": _provider_error,
    "quality_fail": _quality_fail,
    "pii_block": _pii_block,
    "cancel_before_bronze": _cancelled(0),
    "cancel_after_bronze": _cancelled(1),
    "cancel_after_silver": _cancelled(2),
    "cancel_after_gold": _cancelled(3),
    "cancel_before_commit": _cancelled(None),
    "export_failure": _export_failure,
    "interrupted": _interrupted,
    "retry": _retry,
    "composition": _composition,
}


#: What every source's card reads from the catalog, whatever dataset it names. The
#: sources are fakes, so the catalog facts of the datasets they are named after are not
#: under test here, and reading them from the installed kpubdata would move these
#: baselines whenever a kpubdata release corrects them (kpubdata#932, kpubdata#933).
#: ``tests/unit/test_dataset_cards.py`` covers the lookup itself. The values are what
#: kpubdata declares for ``datago.air_station``, read from its data.go.kr page.
_CATALOG = DatasetCatalogInfo(
    source_url="https://www.data.go.kr/data/15073861/openapi.do",
    license_type="공공누리_3유형",
    attribution="공공누리 제3유형(출처표시, 변경금지) — 한국환경공단 에어코리아 대기오염정보",
)


@contextmanager
def _fixed_catalog() -> Iterator[None]:
    original = getattr(card_facts, "catalog_info")  # noqa: B009

    def lookup(_dataset_id: str) -> DatasetCatalogInfo:
        return _CATALOG

    setattr(card_facts, "catalog_info", lookup)  # noqa: B010 - also run outside pytest
    try:
        yield
    finally:
        setattr(card_facts, "catalog_info", original)  # noqa: B010


def _render(name: str) -> str:
    with _fixed_catalog(), TemporaryDirectory() as directory:
        result = SCENARIOS[name](Path(directory))
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# -------------------------------------------------------------------------- tests


def test_every_scenario_has_a_baseline_and_no_baseline_is_orphaned() -> None:
    assert {path.stem for path in GOLDEN.glob("*.json")} == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_matches_its_baseline(name: str) -> None:
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert json.loads(_render(name)) == expected, (
        f"{name} no longer matches tests/golden/source_execution/{name}.json"
    )


def _regenerate() -> int:
    # What the autouse fixtures in ``tests/conftest.py`` give the tests above, so a
    # baseline written here is what they compare against.
    os.environ["KPUBDATA_BUILDER_DEV_MODE"] = "true"
    for name in [n for n in os.environ if n.startswith("KPUBDATA_") and n.endswith("_API_KEY")]:
        del os.environ[name]
    from kpubdata_builder.service import redistribution

    setattr(redistribution, "_catalog_terms", lambda _dataset_id: None)  # noqa: B010
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name in sorted(SCENARIOS):
        first, second = _render(name), _render(name)
        if first != second:
            print(f"{name}: two runs differ; not written", file=sys.stderr)
            return 1
        (GOLDEN / f"{name}.json").write_text(first, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_regenerate())
