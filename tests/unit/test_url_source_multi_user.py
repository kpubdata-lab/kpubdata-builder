"""A multi-user deployment refuses ``url`` sources before any request (#685).

ADR 0012's 2026-09-30 amendment (D5): a bare ``url`` source carries no credential, but
lets any user make the server fetch any public host. A single-user deployment keeps
allowing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.ingestion.url_fetch import FetchResult
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze import resolve as resolve_module

_URL_SPEC = """\
dataset_id: feed.table
title: Feed
description: d
sources:
  - provider: datago
    dataset: air_quality
    alias: api
  - kind: url
    endpoint: https://example.org/data.json
    alias: feed
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


class _Result:
    items = [{"id": "1"}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


@pytest.fixture()
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
        seen.append(url)
        return FetchResult(
            content=b'[{"id": 1}, {"id": 2}]', content_type="application/json", final_url=url
        )

    monkeypatch.setattr(resolve_module, "safe_fetch_get", fake_fetch)
    return seen


def _clients(opened: list[int]) -> object:
    def factory(**_: object) -> _Client:
        opened.append(1)
        return _Client()

    return factory


def _service(tmp_path: Path, opened: list[int]) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=_clients(opened))  # type: ignore[arg-type]


def _assert_refused(response: ServiceResponse) -> None:
    assert response.status_code == 403, response.body
    body = cast(dict[str, JsonValue], response.body)
    assert body["code"] == "url_source_forbidden"
    assert body["sources"] == [{"index": 1, "alias": "feed", "path": "sources[1].kind"}]


@pytest.mark.parametrize("switch", ["ENFORCE_OWNERSHIP", "OIDC_ISSUER"])
def test_multi_user_mode_refuses_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetches: list[str], switch: str
) -> None:
    """Negative: no fetch, no client, nothing queued — on every entry point."""
    monkeypatch.setenv(switch, "true" if switch == "ENFORCE_OWNERSHIP" else "https://idp")
    opened: list[int] = []
    service = _service(tmp_path, opened)

    _assert_refused(service.preview(_URL_SPEC))
    _assert_refused(service.build(_URL_SPEC, run_id="r1"))
    _assert_refused(service.submit_build(_URL_SPEC, run_id="r2"))

    assert fetches == []
    assert opened == []
    assert not (tmp_path / "r1").exists()
    assert service._async_builds.get("r2") is None


def test_a_spec_without_a_url_source_is_not_affected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    spec = _URL_SPEC.replace(
        "  - kind: url\n    endpoint: https://example.org/data.json\n    alias: feed\n", ""
    )

    response = _service(tmp_path, []).build(spec, run_id="r1")

    assert response.status_code == 200, response.body


def test_a_single_user_deployment_still_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetches: list[str]
) -> None:
    """Regression: without OIDC or ENFORCE_OWNERSHIP a url source works as before."""
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)

    response = _service(tmp_path, []).build(_URL_SPEC, run_id="r1")

    assert response.status_code == 200, response.body
    assert fetches == ["https://example.org/data.json"]
