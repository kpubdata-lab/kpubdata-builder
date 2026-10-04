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
    serialize_spec,
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
