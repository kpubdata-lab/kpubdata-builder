"""A build leaves a line when it starts and one when it ends, and neither holds a secret (#1100).

The request log says a build was submitted; these say when it ran and how it ended,
joined by the run. The lines are read from the logger a deployment collects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder import logging_redaction
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, build_log, request_log
from kpubdata_builder.service.jobs import (
    AsyncBuildExecutor,
    BuildJobRunner,
    BuildJobSnapshot,
    generate_run_id,
)
from kpubdata_builder.spec import JsonValue

#: A key as data.go.kr sends it, in the two places a provider's URL carries one.
_KEY_CANARY = "CanaryBuildLogKey7f3a9c1e5b2d8046"
_URL_WITH_KEY = (
    f"https://apis.data.go.kr/B552584/{_KEY_CANARY}/getCtprvnRltmMesureDnsty"
    f"?serviceKey={_KEY_CANARY}&sidoName=canary-param-value-4b1e"
)
#: Text that is no key at all, as an exception or an error body may carry it.
_TEXT_CANARY = "canary-exception-text-a17c"
_OWNER = "oidc:build-log-owner"

_SPEC = """\
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
"""


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def parsed(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.lines]

    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture()
def lines() -> Iterator[_Lines]:
    handler = _Lines()
    logger = logging.getLogger("kpubdata_builder.build")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest.fixture()
def executor() -> Iterator[AsyncBuildExecutor]:
    made = AsyncBuildExecutor(max_workers=1)
    try:
        yield made
    finally:
        made.shutdown()


def _finished(executor: AsyncBuildExecutor, run_id: str) -> BuildJobSnapshot:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = executor.get(run_id)
        if snapshot is not None and snapshot.status in ("succeeded", "failed", "cancelled"):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"job {run_id} did not finish")


def _answers(status_code: int, body: dict[str, JsonValue]) -> BuildJobRunner:
    def runner(
        spec_yaml: str, run_id: str, created_by: str | None, cancellation: CancellationProbe
    ) -> ServiceResponse:
        return ServiceResponse(status_code, body)

    return runner


def _raises(error: Exception) -> BuildJobRunner:
    def runner(
        spec_yaml: str, run_id: str, created_by: str | None, cancellation: CancellationProbe
    ) -> ServiceResponse:
        raise error

    return runner


def _submit(executor: AsyncBuildExecutor, run_id: str, runner: BuildJobRunner) -> BuildJobSnapshot:
    result = executor.submit(
        spec_yaml=_SPEC,
        run_id=run_id,
        created_by="someone@example.com",
        owner_id=_OWNER,
        runner=runner,
    )
    assert result.status == "accepted"
    return _finished(executor, run_id)


# ------------------------------------------------------------------ an async job


def test_a_job_that_succeeds_leaves_a_start_and_an_end_joined_by_the_run(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    run_id = generate_run_id()

    _submit(executor, run_id, _answers(200, {"status": "ok"}))

    started, ended = lines.parsed()
    assert started["event"] == "build_started"
    assert ended["event"] == "build_ended"
    assert started["run_id"] == ended["run_id"] == run_id
    assert started["mode"] == ended["mode"] == "async"
    assert ended["status"] == "succeeded"
    assert isinstance(ended["duration_ms"], float) and ended["duration_ms"] >= 0
    # UTC, to the millisecond, as the request line writes it.
    for line in (started, ended):
        assert isinstance(line["ts"], str) and line["ts"].endswith("Z")
    assert "code" not in ended and "error_type" not in ended and "reason" not in ended


def test_the_owner_is_the_request_logs_opaque_value_not_the_id(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    _submit(executor, generate_run_id(), _answers(200, {"status": "ok"}))

    started, ended = lines.parsed()
    assert started["owner"] == ended["owner"] == request_log.opaque_owner(_OWNER)
    assert _OWNER not in lines.text()
    assert "someone@example.com" not in lines.text()


def test_a_job_that_fails_says_so_and_keeps_only_a_known_code(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    known, unknown = generate_run_id(), generate_run_id()

    _submit(
        executor,
        known,
        _answers(409, {"error": f"GET {_URL_WITH_KEY} failed", "code": "credentials_required"}),
    )
    _submit(executor, unknown, _answers(502, {"error": _TEXT_CANARY, "code": _KEY_CANARY}))

    by_run = {line["run_id"]: line for line in lines.parsed() if line["event"] == "build_ended"}
    assert by_run[known]["status"] == "failed"
    assert by_run[known]["code"] == "credentials_required"
    assert by_run[unknown]["status"] == "failed"
    assert by_run[unknown]["code"] == request_log.OTHER_CODE
    assert _KEY_CANARY not in lines.text()
    assert _TEXT_CANARY not in lines.text()
    assert "apis.data.go.kr" not in lines.text()


def test_an_unhandled_exception_is_named_by_its_class_and_nothing_of_its_text(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    run_id = generate_run_id()

    snapshot = _submit(
        executor, run_id, _raises(RuntimeError(f"{_TEXT_CANARY}: GET {_URL_WITH_KEY} timed out"))
    )

    assert snapshot.status == "failed"
    ended = lines.parsed()[-1]
    assert ended["event"] == "build_ended"
    assert ended["status"] == "failed"
    assert ended["error_type"] == "RuntimeError"
    assert _KEY_CANARY not in lines.text()
    assert _TEXT_CANARY not in lines.text()
    assert "canary-param-value-4b1e" not in lines.text()
    assert "apis.data.go.kr" not in lines.text()


def test_a_run_id_the_client_chose_is_written_as_a_reference(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    """A run id is any text of letters, digits, dots, hyphens and underscores: a key fits."""
    _submit(executor, _KEY_CANARY, _answers(200, {"status": "ok"}))

    started, ended = lines.parsed()
    reference = hashlib.sha256(_KEY_CANARY.encode("utf-8")).hexdigest()[:16]
    assert (
        started["run_ref"] == ended["run_ref"] == reference == build_log.run_reference(_KEY_CANARY)
    )
    assert "run_id" not in started and "run_id" not in ended
    assert _KEY_CANARY not in lines.text()


@pytest.mark.parametrize(
    "run_id", ["20261011T101500123456Z", "20261011T101500123456Z-0123456789ab"]
)
def test_a_run_id_builder_made_is_written_as_it_is(run_id: str, lines: _Lines) -> None:
    build_log.ended(
        run_id, mode="sync", status="succeeded", started_at=build_log.started(run_id, mode="sync")
    )

    assert [line["run_id"] for line in lines.parsed()] == [run_id, run_id]


@pytest.mark.parametrize(
    "run_id",
    [
        "20261011T101500123456Z-0123456789AB",
        "20261011T101500123456Z-0123456789ab-more",
        "x20261011T101500123456Z",
        "20261011T101500123456Z\n",
    ],
)
def test_a_run_id_that_only_resembles_one_of_builders_is_not(run_id: str, lines: _Lines) -> None:
    build_log.started(run_id, mode="sync")

    (line,) = lines.parsed()
    assert "run_id" not in line
    assert line["run_ref"] == build_log.run_reference(run_id)


def test_a_job_stopped_at_the_time_limit_ends_cancelled_and_says_why(
    executor: AsyncBuildExecutor, lines: _Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "_time_limit", lambda: 0.05)

    def until_cancelled(
        spec_yaml: str, run_id: str, created_by: str | None, cancellation: CancellationProbe
    ) -> ServiceResponse:
        deadline = time.monotonic() + 10
        while not cancellation.cancel_requested() and time.monotonic() < deadline:
            time.sleep(0.005)
        return ServiceResponse(409, {"status": "cancelled"})

    run_id = generate_run_id()
    snapshot = _submit(executor, run_id, until_cancelled)

    assert snapshot.status == "cancelled"
    ended = lines.parsed()[-1]
    assert ended["status"] == "cancelled"
    assert ended["reason"] == "time_limit"
    assert isinstance(ended["duration_ms"], float) and ended["duration_ms"] >= 50


def test_a_job_cancelled_by_its_user_gives_no_reason(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    entered = threading.Event()

    def until_cancelled(
        spec_yaml: str, run_id: str, created_by: str | None, cancellation: CancellationProbe
    ) -> ServiceResponse:
        entered.set()
        deadline = time.monotonic() + 10
        while not cancellation.cancel_requested() and time.monotonic() < deadline:
            time.sleep(0.005)
        return ServiceResponse(409, {"status": "cancelled", "code": "run_id_ended"})

    run_id = generate_run_id()
    executor.submit(spec_yaml=_SPEC, run_id=run_id, created_by=None, runner=until_cancelled)
    assert entered.wait(timeout=10)
    executor.request_cancel(run_id)
    _finished(executor, run_id)

    ended = lines.parsed()[-1]
    assert ended["status"] == "cancelled"
    # A cancelled job's response is dropped by the registry; so is its code here.
    assert "reason" not in ended and "code" not in ended


def test_a_job_that_ended_in_the_queue_leaves_no_line(
    executor: AsyncBuildExecutor, lines: _Lines
) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocks(
        spec_yaml: str, run_id: str, created_by: str | None, cancellation: CancellationProbe
    ) -> ServiceResponse:
        entered.set()
        release.wait(timeout=10)
        return ServiceResponse(200, {"status": "ok"})

    first, queued = generate_run_id(), generate_run_id()
    executor.submit(spec_yaml=_SPEC, run_id=first, created_by=None, runner=blocks)
    assert entered.wait(timeout=10)
    executor.submit(spec_yaml=_SPEC, run_id=queued, created_by=None, runner=blocks)
    executor.request_cancel(queued)
    release.set()
    _finished(executor, first)
    assert _finished(executor, queued).status == "cancelled"
    # The worker has to have passed the cancelled job before its absence means anything.
    last = generate_run_id()
    _submit(executor, last, _answers(200, {"status": "ok"}))

    assert [line.get("run_id") for line in lines.parsed()] == [first, first, last, last]


# ------------------------------------------------------------------ a synchronous build


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items: Iterable[dict[str, JsonValue]] = items


class _Dataset:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def list(self, **params: JsonValue) -> _Result:
        if self._error is not None:
            raise self._error
        return _Result([{"stationName": "A", "pm10Value": "20"}])


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def dataset(self, source_key: str) -> _Dataset:
        return _Dataset(self._error)


def test_a_synchronous_build_leaves_the_two_lines_under_the_run_it_was_given(
    tmp_path: Path, lines: _Lines
) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())

    answer = service.build(_SPEC, owner_id=_OWNER)

    assert answer.status_code == 200
    started, ended = lines.parsed()
    assert (started["event"], ended["event"]) == ("build_started", "build_ended")
    assert started["run_id"] == ended["run_id"] == answer.body["run_id"]
    assert started["mode"] == ended["mode"] == "sync"
    assert ended["status"] == "succeeded"
    assert started["owner"] == ended["owner"] == request_log.opaque_owner(_OWNER)


def test_a_synchronous_build_whose_source_fails_ends_failed_without_the_errors_text(
    tmp_path: Path, lines: _Lines
) -> None:
    error = RuntimeError(f"{_TEXT_CANARY}: GET {_URL_WITH_KEY} failed")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client(error))

    answer = service.build(_SPEC, run_id="chosen-by-the-client")

    assert answer.status_code == 502
    started, ended = lines.parsed()
    assert started["run_ref"] == ended["run_ref"] == build_log.run_reference("chosen-by-the-client")
    assert ended["status"] == "failed"
    assert "chosen-by-the-client" not in lines.text()
    assert _KEY_CANARY not in lines.text()
    assert _TEXT_CANARY not in lines.text()
    assert "apis.data.go.kr" not in lines.text()


def test_an_async_job_run_by_the_service_leaves_two_lines_not_four(
    tmp_path: Path, lines: _Lines
) -> None:
    """The worker writes the job's lines; the build it calls must not write its own."""
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: _Client(), async_max_workers=1
    )

    submitted = service.submit_build(_SPEC, owner_id=_OWNER)
    run_id = submitted.body["run_id"]
    assert isinstance(run_id, str)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and service.build_status(run_id).body.get("status") not in (
        "succeeded",
        "failed",
    ):
        time.sleep(0.01)
    deadline = time.monotonic() + 10
    while len(lines.lines) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert [(line["event"], line["mode"], line["run_id"]) for line in lines.parsed()] == [
        ("build_started", "async", run_id),
        ("build_ended", "async", run_id),
    ]
    assert lines.parsed()[-1]["status"] == "succeeded"


# ------------------------------------------------------------------ every logger


def test_a_providers_url_in_an_exception_leaves_its_key_in_no_log_record(
    executor: AsyncBuildExecutor, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every record the process writes, tracebacks included — as ``serve`` runs.

    ``serve`` installs the record scrubber (``logging_redaction.install``). The key is
    in the URL twice: as ``serviceKey``, which the scrubber knows by name, and as a
    path segment, which it knows only while the key is registered as in use — as
    Builder registers a client's keys for as long as the client is open.
    """
    previous = logging.getLogRecordFactory()
    monkeypatch.setattr(logging_redaction, "_installed", False)
    logging_redaction.install()
    holder = object()
    logging_redaction.register(holder, (_KEY_CANARY,))
    caplog.set_level(logging.DEBUG)
    try:
        _submit(
            executor,
            generate_run_id(),
            _raises(RuntimeError(f"GET {_URL_WITH_KEY} timed out")),
        )
    finally:
        logging_redaction.release(holder)
        logging.setLogRecordFactory(previous)

    written = "\n".join(logging.Formatter().format(record) for record in caplog.records)
    # The diagnostic record was written, with its traceback: this is not an empty log.
    assert "failed with an unhandled exception" in written
    assert "Traceback" in written
    assert {record.name for record in caplog.records} >= {
        "kpubdata_builder.build",
        "kpubdata_builder.service.jobs",
    }
    assert _KEY_CANARY not in written


def test_the_check_above_fails_when_the_scrubber_is_not_installed(
    executor: AsyncBuildExecutor, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the scrubber the diagnostic record does hold the key: the check can fail.

    And the build lines still do not, because they are not handed the exception.
    """
    monkeypatch.setattr(logging, "_logRecordFactory", logging.LogRecord)
    caplog.set_level(logging.DEBUG)

    _submit(executor, generate_run_id(), _raises(RuntimeError(f"GET {_URL_WITH_KEY} timed out")))

    by_logger: dict[str, list[str]] = {}
    for record in caplog.records:
        by_logger.setdefault(record.name, []).append(logging.Formatter().format(record))
    assert _KEY_CANARY in "\n".join(by_logger["kpubdata_builder.service.jobs"])
    assert _KEY_CANARY not in "\n".join(by_logger["kpubdata_builder.build"])


# ------------------------------------------------------------------ the line itself


def test_a_status_or_a_class_name_that_is_not_one_is_not_written(lines: _Lines) -> None:
    started_at = build_log.started("20261011T101500123456Z", mode="sync")

    build_log.ended(
        "20261011T101500123456Z",
        mode=cast(build_log.BuildMode, _TEXT_CANARY),
        status=cast(build_log.BuildOutcome, _KEY_CANARY),
        started_at=started_at,
        error_type=f"{_TEXT_CANARY} {_URL_WITH_KEY}",
    )

    ended = lines.parsed()[-1]
    assert ended["status"] is None
    assert ended["mode"] is None
    assert ended["error_type"] == "other"
    assert _KEY_CANARY not in lines.text()
    assert _TEXT_CANARY not in lines.text()


def test_a_line_that_cannot_be_written_does_not_fail_the_build(
    executor: AsyncBuildExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("the log's disk is full")

    monkeypatch.setattr(logging.getLogger("kpubdata_builder.build"), "info", broken)

    snapshot = _submit(executor, generate_run_id(), _answers(200, {"status": "ok"}))

    assert snapshot.status == "succeeded"
