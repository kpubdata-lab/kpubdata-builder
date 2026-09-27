"""Structured event emission tests for ``pipeline.orchestrator.run_build`` (#496).

HTTP route/ownership/bounded query covered by test_events_api.py. This file verifies at
orchestrator level whether events fire only at actual execution boundaries (run/source
fetch/medallion stage/quality checkpoint), non-executed stages are not faked as complete, and
successful events persist even on failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from kpubdata_builder.events import BuildEvent, BuildEventStore
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.models import SchemaContract


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **_params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _spec(*sources: SourceRef, quality: object = None) -> BuildSpec:
    kwargs: dict[str, object] = {}
    if quality is not None:
        kwargs["quality"] = quality
    return BuildSpec(
        dataset_id="events.fixture",
        title="Events Fixture",
        description="fixture",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        **kwargs,
    )


def _names(events: tuple[BuildEvent, ...]) -> list[str]:
    return [e.event for e in events]


class TestRunLifecycle:
    def test_successful_run_emits_started_then_finished(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        events = store.list_for_run("r1", limit=100, tail=False)
        assert events[0].event == "run_started"
        assert events[-1].event == "run_finished"
        assert events[-1].status == "ok"

    def test_failed_run_emits_run_failed_with_status_fail(self, tmp_path: Path) -> None:
        client = _FakeClient({})  # All sources fail fetch.
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        assert events[-1].event == "run_failed"
        assert events[-1].status == "fail"

    def test_no_event_store_does_not_raise(self, tmp_path: Path) -> None:
        """Existing callers omitting event_store (CLI, etc.) remain unchanged."""
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(spec, client=client, output_root=tmp_path, run_id="r1")

        assert result.status == "ok"

    def test_run_events_are_timezone_aware_utc(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        assert events
        for event in events:
            assert event.timestamp.tzinfo is not None


class TestSourceFetchLifecycle:
    def test_fetch_success_emits_started_then_completed(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}, {"id": "2"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        fetch_events = [e for e in events if e.event.startswith("source_fetch")]
        assert [e.event for e in fetch_events] == ["source_fetch_started", "source_fetch_completed"]
        assert fetch_events[0].source_key == "air"
        assert fetch_events[1].metrics == {"records": 2}

    def test_fetch_failure_emits_failed_not_completed(self, tmp_path: Path) -> None:
        client = _FakeClient({})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        fetch_events = [e for e in events if e.event.startswith("source_fetch")]
        assert [e.event for e in fetch_events] == ["source_fetch_started", "source_fetch_failed"]
        assert fetch_events[1].status == "fail"

    def test_source_key_is_output_facing_alias(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="my-alias"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        source_keys = {e.source_key for e in events if e.source_key is not None}
        assert source_keys == {"my-alias"}


class TestStageLifecycle:
    def test_full_success_emits_all_four_stages_in_order(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        stage_events = [(e.stage, e.event) for e in events if e.stage is not None]
        assert stage_events == [
            ("bronze", "stage_started"),
            ("bronze", "stage_completed"),
            ("silver", "stage_started"),
            ("silver", "stage_completed"),
            ("gold", "stage_started"),
            ("gold", "stage_completed"),
            ("export", "stage_started"),
            ("export", "stage_completed"),
        ]

    def test_bronze_failure_does_not_emit_silver_or_gold(self, tmp_path: Path) -> None:
        client = _FakeClient({})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        stages_seen = {e.stage for e in events if e.stage is not None}
        assert stages_seen == {"bronze"}
        bronze_events = [e.event for e in events if e.stage == "bronze"]
        assert bronze_events == ["stage_started", "stage_failed"]

    def test_silver_validation_failure_bronze_stays_completed(self, tmp_path: Path) -> None:
        """Bronze success events are not deleted even if silver fails (append-only)."""
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        # required column does not actually exist, so silver validation fails.
        spec = BuildSpec(
            dataset_id="events.fixture",
            title="Events Fixture",
            description="fixture",
            sources=(
                SourceRef(
                    provider="datago",
                    dataset="air",
                    alias="air",
                    schema=SchemaContract(required=("does_not_exist",)),
                ),
            ),
            exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        )

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        bronze_events = [e.event for e in events if e.stage == "bronze"]
        assert bronze_events == ["stage_started", "stage_completed"]
        silver_events = [e.event for e in events if e.stage == "silver"]
        assert silver_events == ["stage_started", "stage_failed"]
        assert "gold" not in {e.stage for e in events}

    def test_not_reached_stage_never_marked_completed(self, tmp_path: Path) -> None:
        """Unreached stages are recorded as neither completed nor failed."""
        client = _FakeClient({})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        for stage in ("silver", "gold", "export"):
            assert stage not in {e.stage for e in events}


class TestQualityCheckpoint:
    def test_quality_evaluated_emitted_once_per_source(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        quality_events = [e for e in events if e.event == "quality_evaluated"]
        assert len(quality_events) == 1
        assert quality_events[0].status == "ok"

    def test_quality_evaluated_survives_downstream_gate_failure(self, tmp_path: Path) -> None:
        """Even if a source fails due to quality FAIL, the quality_evaluated event itself
        remains.
        """
        from kpubdata_builder.spec.models import QualityPolicy

        client = _FakeClient({"datago.air": [{"id": "1"}, {"id": "2"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(
            SourceRef(provider="datago", dataset="air", alias="air"),
            quality=QualityPolicy(min_rows=100, min_rows_severity="fail"),
        )

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        quality_events = [e for e in events if e.event == "quality_evaluated"]
        assert len(quality_events) == 1
        assert quality_events[0].status == "fail"
        assert quality_events[0].metrics is not None
        assert quality_events[0].metrics["fail_count"] >= 1


class TestPartialRunMultiSource:
    def test_one_success_one_failure_timeline_shows_both(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.good": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(
            SourceRef(provider="datago", dataset="good", alias="good"),
            SourceRef(provider="datago", dataset="bad", alias="bad"),
        )

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        good_events = [e.event for e in events if e.source_key == "good"]
        bad_events = [e.event for e in events if e.source_key == "bad"]
        assert "stage_completed" in good_events  # Events of successful source survive.
        assert "stage_failed" in bad_events
        assert events[-1].event == "run_failed"


def _selective_failing_append(
    monkeypatch: pytest.MonkeyPatch, should_fail: Callable[[BuildEvent], bool]
) -> None:
    """Fault injection that fails append only when ``event`` matches ``should_fail``.

    Remaining events delegated as-is to original implementation (``BuildEventStore.append``) —
    simulates realistic transient failure (disk hiccup, etc.) where only one boundary fails
    and rest records normally.
    """
    original_append = BuildEventStore.append

    def _append(self: BuildEventStore, event: BuildEvent) -> BuildEvent:
        if should_fail(event):
            raise RuntimeError(f"simulated event store outage for {event.event}")
        return original_append(self, event)

    monkeypatch.setattr(BuildEventStore, "append", _append)


def _manifest_warnings(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(manifest["warnings"])


class TestRecorderFailureIsolation:
    def test_event_append_failure_does_not_fail_otherwise_successful_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Event recording infrastructure failure does not cause already-successful builds to
            fail (#496).

        BuildEventStore.append() itself does not swallow failure (test_event_store.py);
        recorder absorbs it to not affect build progress — but (not because "derived index so
        loss OK" per ADR 0003)
        to prevent event recording failure from corrupting the *different* source of truth
        (manifest/source outcome).
        That absorption is not silent disappearance; fault-injection tests below verify it via
        ``BuildManifest.warnings``.
        """
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)

        def _broken_append(self: BuildEventStore, event: BuildEvent) -> BuildEvent:
            raise RuntimeError("simulated event store outage")

        monkeypatch.setattr(BuildEventStore, "append", _broken_append)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.outcomes[0].status == "ok"
        assert _manifest_warnings(result.manifest_path)  # Failure does not completely disappear.

    def test_run_started_append_failure_still_builds_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if run_started append fails, build continues and manifest is recorded (#496)."""
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        _selective_failing_append(monkeypatch, lambda e: e.event == "run_started")
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.manifest_path.exists()  # AGENTS.md 'manifest omission is forbidden'.
        warnings = _manifest_warnings(result.manifest_path)
        assert any("run_started" in w for w in warnings)
        # Subsequent events (run_finished, etc.) are recorded normally because store is recovered —
        # one failure does not erase the rest of the timeline (append-only).
        events = store.list_for_run("r1", limit=100, tail=False)
        assert events[-1].event == "run_finished"

    def test_stage_completed_append_failure_does_not_flip_successful_source_to_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stage_completed append failure must not flip actually-successful sources to failed.

        stage_completed is called *after* actual persist (``persist_bronze_artifact``) completes —
        if exception leaks to pipeline control flow here,
        the common except in ``_run_source_pipeline`` misinterprets it as "this source failed" and
        reports already-written bronze artifacts as failed (#496).
        """
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        _selective_failing_append(
            monkeypatch, lambda e: e.event == "stage_completed" and e.stage == "bronze"
        )
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.outcomes[0].status == "ok"
        assert result.outcomes[0].stages_completed == ("bronze", "silver", "gold")
        bronze_dir = tmp_path / "r1" / "bronze"
        assert bronze_dir.exists() and any(bronze_dir.iterdir())  # Actual artifacts remain intact.
        warnings = _manifest_warnings(result.manifest_path)
        assert any("stage_completed" in w and "bronze" in w for w in warnings)

    def test_run_finished_append_failure_still_writes_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_finished append failure does not prevent manifest recording (#496).

        recorder.run_finished() is called before manifest_writer
        (pipeline/orchestrator.py) — if this failure propagates and aborts run_build,
        manifest.json never gets created, violating AGENTS.md 'manifest omission forbidden' far
        more severely.
        """
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        _selective_failing_append(monkeypatch, lambda e: e.event == "run_finished")
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.manifest_path.exists()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["errors"] == []  # Actual source execution did not fail at all.
        assert any("run_finished" in w for w in manifest["warnings"])

    def test_run_failed_append_failure_still_writes_manifest_with_real_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_failed append failure also does not obscure manifest recording or actual
        failure reason (#496).
        """
        client = _FakeClient({})  # All sources fail fetch.
        store = BuildEventStore(tmp_path)
        _selective_failing_append(monkeypatch, lambda e: e.event == "run_failed")
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        assert result.manifest_path.exists()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest[
            "errors"
        ]  # Actual failure reason remains regardless of event recording failure.
        assert any("run_failed" in w for w in manifest["warnings"])
