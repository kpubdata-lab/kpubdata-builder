"""A spec snapshot redacts every credential name kpubdata masks (#999).

The serializer kept its own list of secret field names, and it had drifted from
kpubdata's: ``key``, ``oc`` (the law provider's API key), ``consumer_key`` and
``consumer_secret`` (sgis) were missing, so a spec carrying one under ``params`` wrote
the value into ``buildspec.yaml`` in the clear. The list is now derived from kpubdata's.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.logging_redaction import SENSITIVE_PARAM_KEYS
from kpubdata_builder.spec import BuildSpec, JsonValue
from kpubdata_builder.spec.loader import parse_spec
from kpubdata_builder.spec.serializer import (
    REDACTED_VALUE,
    canonical_spec_mapping,
    compute_spec_digest,
    serialize_spec,
    serialize_spec_bytes,
    write_buildspec_snapshot,
)

_CANARY = "CANARY-do-not-store-7f3a"


def _spec(params: dict[str, object], **source: object) -> BuildSpec:
    return parse_spec(
        {
            "dataset_id": "leak.check",
            "title": "Leak check",
            "description": "d",
            "sources": [{"provider": "law", "dataset": "statute", "params": params, **source}],
            "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
        }
    )


@pytest.mark.parametrize("name", ["oc", "key", "consumer_key", "consumer_secret"])
def test_the_four_names_the_old_list_lacked_leave_no_value(tmp_path: Path, name: str) -> None:
    spec = _spec({name: _CANARY, "query": "민법"})

    path, _digest = write_buildspec_snapshot(spec, output_root=tmp_path, run_id="r1")

    text = path.read_text(encoding="utf-8")
    assert _CANARY not in text
    assert f"{name}: {REDACTED_VALUE}" in text
    assert "query: 민법" in text


@pytest.mark.parametrize("name", sorted(SENSITIVE_PARAM_KEYS))
def test_every_name_kpubdata_masks_is_redacted(name: str) -> None:
    """Derived, not copied: a name kpubdata adds is covered without a change here."""
    assert _CANARY not in serialize_spec(_spec({name: _CANARY}))


@pytest.mark.parametrize("name", ["OC", "Consumer-Secret", "KEY"])
def test_the_match_ignores_case_and_hyphens(name: str) -> None:
    assert _CANARY not in serialize_spec(_spec({name: _CANARY}))


def test_a_credential_named_grid_axis_is_redacted_too() -> None:
    spec = _spec({}, param_grid={"oc": [_CANARY, _CANARY + "-2"], "page": [1, 2]})

    sources = cast(list[dict[str, JsonValue]], canonical_spec_mapping(spec)["sources"])
    (source,) = sources

    assert source["param_grid"] == {"oc": REDACTED_VALUE, "page": [1, 2]}
    assert _CANARY not in serialize_spec(spec)


def test_a_parameter_that_only_contains_a_secret_name_is_kept() -> None:
    """Names match exactly: ``keyword`` and ``district_code`` are data, not credentials."""
    text = serialize_spec(_spec({"keyword": "서울", "district_code": "11"}))

    assert "keyword: 서울" in text
    assert "district_code: '11'" in text


def test_outside_request_parameters_a_bare_key_is_data_and_the_other_names_are_not() -> None:
    """``key`` names a credential only as a request parameter; ``oc`` and the sgis names
    are credentials wherever they appear."""
    data: dict[str, object] = {
        "dataset_id": "leak.check",
        "title": "Leak check",
        "description": "d",
        "sources": [{"provider": "law", "dataset": "statute"}],
        "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
        "metadata": {"key": "business-key", "oc": _CANARY, "consumer_secret": _CANARY},
    }

    text = serialize_spec(parse_spec(data))

    assert "key: business-key" in text
    assert _CANARY not in text


# --- A ``url`` source's endpoint (#1029) ---


def _url_spec(endpoint: str) -> BuildSpec:
    return parse_spec(
        {
            "dataset_id": "leak.check",
            "title": "Leak check",
            "description": "d",
            "sources": [{"kind": "url", "endpoint": endpoint, "alias": "feed"}],
            "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
        }
    )


def _recorded_endpoint(endpoint: str) -> str:
    sources = cast(
        list[dict[str, JsonValue]], canonical_spec_mapping(_url_spec(endpoint))["sources"]
    )
    return cast(str, sources[0]["endpoint"])


def test_a_key_in_the_endpoint_query_is_not_written_to_the_snapshot(tmp_path: Path) -> None:
    spec = _url_spec(f"https://apis.data.go.kr/B552584/getList?serviceKey={_CANARY}&pageNo=1")

    path, _digest = write_buildspec_snapshot(spec, output_root=tmp_path, run_id="r1")

    assert _CANARY not in path.read_text(encoding="utf-8")
    assert _CANARY not in serialize_spec(spec)
    # What was asked for stays readable; only the credential's value is gone.
    assert _recorded_endpoint(spec.sources[0].endpoint) == (
        f"https://apis.data.go.kr/B552584/getList?serviceKey={REDACTED_VALUE}&pageNo=1"
    )


@pytest.mark.parametrize("name", sorted(SENSITIVE_PARAM_KEYS))
def test_every_credential_name_is_redacted_in_the_query(name: str) -> None:
    recorded = _recorded_endpoint(f"https://example.org/data.json?page=2&{name}={_CANARY}")

    assert _CANARY not in recorded
    assert "page=2" in recorded


@pytest.mark.parametrize("name", ["ServiceKey", "SERVICEKEY", "api-key", "Api%5FKey", "KEY"])
def test_the_query_match_ignores_case_hyphens_and_percent_encoding(name: str) -> None:
    assert _CANARY not in _recorded_endpoint(f"https://example.org/d.json?{name}={_CANARY}")


def test_userinfo_is_redacted_whole() -> None:
    recorded = _recorded_endpoint(f"https://reader:{_CANARY}@example.org/data.json?page=1")

    assert _CANARY not in recorded
    assert "reader" not in recorded
    assert recorded == f"https://{REDACTED_VALUE}@example.org/data.json?page=1"


def test_a_repeated_credential_parameter_is_redacted_each_time() -> None:
    recorded = _recorded_endpoint(
        f"https://example.org/d.json?serviceKey={_CANARY}&q=1&serviceKey={_CANARY}-2"
    )

    assert _CANARY not in recorded
    assert recorded.count(REDACTED_VALUE) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.org/data.json",
        "https://example.org/data.json?page=1&numOfRows=100",
        # A parameter that only contains a credential name is ordinary data.
        "https://example.org/data.json?keyword=%EB%AF%BC%EB%B2%95&monkey=1",
        "https://example.org/data.json?flag&empty=#section",
        "https://example.org/path@v2/data.json",
    ],
)
def test_an_endpoint_without_a_credential_is_recorded_byte_for_byte(endpoint: str) -> None:
    # Unchanged text means an unchanged digest for every existing url spec.
    assert _recorded_endpoint(endpoint) == endpoint


def test_the_same_endpoint_with_another_key_has_the_same_digest() -> None:
    def spec_digest(spec: BuildSpec) -> str:
        return compute_spec_digest(serialize_spec_bytes(spec))

    one = _url_spec("https://example.org/d.json?serviceKey=first-key&page=1")
    two = _url_spec("https://example.org/d.json?serviceKey=second-key&page=1")
    other_page = _url_spec("https://example.org/d.json?serviceKey=first-key&page=2")

    assert spec_digest(one) == spec_digest(two)
    assert spec_digest(one) != spec_digest(other_page)
