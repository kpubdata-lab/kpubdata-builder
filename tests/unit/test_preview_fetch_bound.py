"""A preview reads a few pages of a source, a build reads it in the largest pages (#1185).

The first group runs the real kpubdata client against a fake data.go.kr endpoint and
counts the HTTP requests, so the bound is checked where the quota is spent: kpubdata's
spec datasets request every page before yielding one (#481), which a fake dataset
would not show.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import kpubdata
import pytest

from kpubdata_builder.pipeline import preview_build
from kpubdata_builder.pipeline.preview import PREVIEW_MAX_PAGES
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.stages.bronze import FetchBound, build_bronze_artifact

_FORECAST: dict[str, JsonValue] = {
    "base_date": "20260908",
    "base_time": "2300",
    "nx": 55,
    "ny": 127,
}


@dataclass
class _Upstream:
    """A village_fcst endpoint serving ``total`` rows, page by page."""

    total: int
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        size = int(request.url.params["numOfRows"])
        page = int(request.url.params["pageNo"])
        first = (page - 1) * size
        rows = [
            {"baseDate": 20260908, "category": "TMP", "fcstValue": str(index)}
            for index in range(first, min(first + size, self.total))
        ]
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "OK"},
                    "body": {"items": {"item": rows}, "totalCount": self.total},
                }
            },
        )

    def sizes(self) -> list[int]:
        return [int(request.url.params["numOfRows"]) for request in self.requests]


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Upstream]:
    monkeypatch.delenv("KPUBDATA_REPLAY_DIR", raising=False)
    monkeypatch.delenv("KPUBDATA_MODE", raising=False)
    served = _Upstream(total=2_000_000)
    real_client = httpx.Client

    def mocked_client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(served.handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", mocked_client)
    yield served


def _client() -> Any:
    # kpubdata.Client is the SourceClient Builder runs with; its types are its own.
    return kpubdata.Client(provider_keys={"datago": "test-key"}, env_keys=False, max_retries=0)


def _spec(source: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="forecast",
        title="Forecast",
        description="village forecast",
        sources=(source,),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def _forecast(**extra: JsonValue) -> SourceRef:
    return SourceRef(provider="datago", dataset="village_fcst", params={**_FORECAST, **extra})


def test_a_preview_of_five_rows_makes_one_request_of_five(upstream: _Upstream) -> None:
    preview = preview_build(_spec(_forecast()), client=_client(), limit=5).previews[0]

    assert preview.status == "ok", preview.error
    assert upstream.sizes() == [5]
    assert preview.preview.total_rows == 5
    assert preview.fetch_complete is False
    assert preview.source_reported_total == 2_000_000


def test_a_preview_larger_than_a_page_stops_at_the_page_bound(upstream: _Upstream) -> None:
    # The largest limit the API takes is the dataset's max page size, so make the
    # pages small through the parameters to need more than three.
    preview = preview_build(_spec(_forecast(page_size=100)), client=_client(), limit=1000).previews[
        0
    ]

    assert preview.status == "ok", preview.error
    assert len(upstream.requests) == PREVIEW_MAX_PAGES == 3
    assert upstream.sizes() == [100, 100, 100]
    assert preview.preview.total_rows == 300
    assert preview.fetch_complete is False


def test_a_preview_of_a_small_source_is_complete(upstream: _Upstream) -> None:
    upstream.total = 4

    preview = preview_build(_spec(_forecast()), client=_client(), limit=5).previews[0]

    assert len(upstream.requests) == 1
    assert preview.preview.total_rows == 4
    assert preview.fetch_complete is True
    assert preview.source_reported_total == 4


@pytest.mark.parametrize("total", [1, 999, 1000, 1001, 2500])
def test_a_build_requests_pages_of_the_largest_size(upstream: _Upstream, total: int) -> None:
    upstream.total = total

    artifact = build_bronze_artifact(
        _client(), source_key="datago.village_fcst", fetch_params=dict(_FORECAST)
    )

    assert artifact.record_count == total
    assert len(upstream.requests) == math.ceil(total / 1000)
    assert set(upstream.sizes()) == {1000}
    assert artifact.stopped_early is False
    # page_size is a request detail, not part of what the source was asked for.
    assert artifact.fetch_params == _FORECAST


def test_a_page_size_in_the_parameters_is_kept_by_a_build(upstream: _Upstream) -> None:
    upstream.total = 250

    build_bronze_artifact(
        _client(),
        source_key="datago.village_fcst",
        fetch_params={**_FORECAST, "page_size": 100},
    )

    assert upstream.sizes() == [100, 100, 100]


def test_a_preview_of_a_param_grid_shares_one_page_budget(upstream: _Upstream) -> None:
    source = SourceRef(
        provider="datago",
        dataset="village_fcst",
        params={key: value for key, value in _FORECAST.items() if key != "nx"},
        param_grid={"nx": tuple(range(25))},
    )

    preview = preview_build(_spec(source), client=_client(), limit=1000).previews[0]

    assert preview.status == "ok", preview.error
    # Each combination's first page holds 1,000 rows, which meets the limit.
    assert len(upstream.requests) == 1
    assert preview.fetch_complete is False
    assert preview.source_reported_total is None


# --- Datasets kpubdata does not page for Builder --------------------------------------


class _Batch:
    def __init__(self, items: list[dict[str, JsonValue]], *, next_page: int | None) -> None:
        self.items = items
        self.total_count: int | None = None
        self.next_page = next_page
        self.next_cursor: str | None = None


class _LazyPages:
    """Pages of two rows, whatever page size is asked for, fetched as they are read."""

    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.fetched = 0
        self.calls: list[dict[str, JsonValue]] = []

    def list(self, **params: JsonValue) -> _Batch:
        raise AssertionError("list_all is the paginated path")

    def list_all(self, *, max_pages: int | None = None, **params: JsonValue) -> Iterator[_Batch]:
        self.calls.append({**params, "max_pages": max_pages})
        for page in range(1, self.pages + 1):
            self.fetched += 1
            yield _Batch(
                [{"page": page, "row": 0}, {"page": page, "row": 1}],
                next_page=page + 1 if page < self.pages else None,
            )


class _EagerPages(_LazyPages):
    """kpubdata's spec path: every page up to max_pages first, then a raise if more."""

    def list_all(self, *, max_pages: int | None = None, **params: JsonValue) -> Iterator[_Batch]:
        self.calls.append({**params, "max_pages": max_pages})
        limit = max_pages or 1000
        fetched = [
            _Batch([{"page": page}], next_page=page + 1)
            for page in range(1, min(self.pages, limit) + 1)
        ]
        self.fetched = len(fetched)
        yield from fetched
        if self.pages > limit:
            raise RuntimeError("Pagination limit exceeded")


class _OneDataset:
    def __init__(self, dataset: object) -> None:
        self._dataset = dataset

    def dataset(self, source_key: str) -> Any:
        return self._dataset


def test_a_lazy_source_stops_reading_at_the_page_bound() -> None:
    dataset = _LazyPages(pages=2000)

    artifact = build_bronze_artifact(
        _OneDataset(dataset),
        source_key="p.d",
        fetch_params={"page_size": 2},
        bound=FetchBound(rows=1000, pages=3),
    )

    assert dataset.calls[0]["max_pages"] == 3
    assert dataset.fetched == 3
    assert artifact.record_count == 6
    assert artifact.stopped_early is True


def test_pages_are_asked_for_at_the_bound_when_no_max_page_size_is_known() -> None:
    dataset = _LazyPages(pages=2000)

    artifact = build_bronze_artifact(
        _OneDataset(dataset), source_key="p.d", bound=FetchBound(rows=1000, pages=3)
    )

    # One page of 1,000 is what was asked for. A provider that serves fewer gives the
    # preview fewer rows, not more requests.
    assert dataset.calls[0]["page_size"] == 1000
    assert dataset.calls[0]["max_pages"] == 1
    assert dataset.fetched == 1
    assert artifact.stopped_early is True


def test_an_eager_source_is_asked_for_only_the_pages_the_rows_need() -> None:
    dataset = _EagerPages(pages=2000)

    artifact = build_bronze_artifact(
        _OneDataset(dataset), source_key="p.d", bound=FetchBound(rows=5, pages=3)
    )

    # page_size 5 makes one page enough, so one is requested, not three; reaching the
    # limit is not an error.
    assert dataset.calls[0]["max_pages"] == 1
    assert dataset.fetched == 1
    assert artifact.record_count == 1
    assert artifact.stopped_early is True


def test_a_source_that_ends_inside_the_bound_is_complete() -> None:
    dataset = _LazyPages(pages=2)

    artifact = build_bronze_artifact(
        _OneDataset(dataset),
        source_key="p.d",
        fetch_params={"page_size": 2},
        bound=FetchBound(rows=1000, pages=3),
    )

    assert artifact.record_count == 4
    assert artifact.stopped_early is False


def test_a_bound_cannot_take_a_checkpoint(tmp_path: Any) -> None:
    from kpubdata_builder.stages.bronze.checkpoint import CombinationCheckpoint

    with pytest.raises(ValueError, match="checkpoint"):
        build_bronze_artifact(
            _OneDataset(_LazyPages(pages=1)),
            source_key="p.d",
            param_combinations=[{"a": 1}],
            checkpoint=CombinationCheckpoint(tmp_path),
            bound=FetchBound(rows=1, pages=1),
        )
