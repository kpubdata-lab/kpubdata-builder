"""A param_grid fetch reports progress and can be stopped between combinations (#648).

1,500 combinations, each paginated, ran as one silent unit: no event until the whole
source was fetched, and a cancel only took effect after the last call. Each finished
combination is now a boundary — an event records it, and cancellation stops the
fetch there, before anything is written.
"""

from __future__ import annotations

import json
from pathlib import Path

from kpubdata_builder.events import BuildEventStore
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Dataset:
    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._calls = calls

    def list(self, **params: object) -> _Result:
        self._calls.append(dict(params))
        return _Result([{"region": str(params["LAWD_CD"]), "n": len(self._calls)}])


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(self.calls)


class _CancelAfterCalls:
    """Asks for cancellation once the client has made ``n`` calls."""

    def __init__(self, client: _Client, n: int) -> None:
        self._client = client
        self._n = n

    def cancel_requested(self) -> bool:
        return len(self._client.calls) >= self._n

    def commit(self) -> bool:
        return not self.cancel_requested()


def _spec(grid: dict[str, tuple[JsonValue, ...]] | None) -> BuildSpec:
    return BuildSpec(
        dataset_id="grid.progress",
        title="Grid",
        description="d",
        sources=(
            SourceRef(
                provider="datago",
                dataset="apt_trade",
                params={} if grid else {"LAWD_CD": "11110"},
                param_grid=grid or {},
            ),
        ),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


_GRID: dict[str, tuple[JsonValue, ...]] = {"LAWD_CD": ("11110", "11140", "11170", "11200")}


def _events(root: Path, run_id: str, name: str) -> list[dict[str, JsonValue] | None]:
    store = BuildEventStore(root)
    return [e.metrics for e in store.list_for_run(run_id, limit=500, tail=False) if e.event == name]


def test_each_combination_is_reported(tmp_path: Path) -> None:
    store = BuildEventStore(tmp_path)

    result = run_build(
        _spec(_GRID), client=_Client(), output_root=tmp_path, run_id="grid", event_store=store
    )

    assert result.status == "ok"
    assert _events(tmp_path, "grid", "source_fetch_progress") == [
        {"done": 1, "total": 4},
        {"done": 2, "total": 4},
        {"done": 3, "total": 4},
        {"done": 4, "total": 4},
    ]


def test_a_single_call_reports_no_progress(tmp_path: Path) -> None:
    """Without param_grid there is one call, so nothing to count."""
    store = BuildEventStore(tmp_path)

    run_build(_spec(None), client=_Client(), output_root=tmp_path, run_id="one", event_store=store)

    assert _events(tmp_path, "one", "source_fetch_progress") == []


def test_cancellation_stops_between_combinations(tmp_path: Path) -> None:
    """Negative: a cancel after two combinations fetches no third, and writes no Bronze."""
    client = _Client()
    store = BuildEventStore(tmp_path)

    result = run_build(
        _spec(_GRID),
        client=client,
        output_root=tmp_path,
        run_id="cancelled",
        event_store=store,
        cancellation=_CancelAfterCalls(client, 2),
    )

    assert result.status == "cancelled"
    assert len(client.calls) == 2
    outcome = result.outcomes[0]
    assert outcome.status == "cancelled"
    assert "bronze" not in outcome.stages_completed
    assert not list((tmp_path / "cancelled").glob("bronze/**/raw_records.jsonl"))
    manifest = json.loads((tmp_path / "cancelled" / "manifest.json").read_text("utf-8"))
    assert manifest["partial"] is True
    assert _events(tmp_path, "cancelled", "source_fetch_progress") == [
        {"done": 1, "total": 4},
        {"done": 2, "total": 4},
    ]


def test_a_cancel_after_the_last_combination_is_left_to_the_stage_boundary(
    tmp_path: Path,
) -> None:
    """The last combination is not a boundary of its own — the stage boundary follows."""
    client = _Client()

    result = run_build(
        _spec(_GRID),
        client=client,
        output_root=tmp_path,
        run_id="late",
        event_store=BuildEventStore(tmp_path),
        cancellation=_CancelAfterCalls(client, 4),
    )

    assert len(client.calls) == 4
    assert result.status == "cancelled"
