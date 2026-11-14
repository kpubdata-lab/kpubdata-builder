"""Protect BuildSpec validation consistency across the existing entry points.

This exercises accepted and rejected specs without making provider requests.
The public interfaces intentionally return different response envelopes, so
the invariant here is validation acceptance rather than byte-identical output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.errors import SpecLoadError, ValidationError
from kpubdata_builder.service.app import BuilderService, dispatch
from kpubdata_builder.spec import load_spec
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.bronze.build import SourceClient


def _unexpected_client() -> SourceClient:
    raise AssertionError("BuildSpec validation must not fetch provider data")


@pytest.mark.parametrize(
    ("spec_yaml", "expected_valid"),
    [
        pytest.param(
            """
dataset_id: dataset.validation_parity
title: Validation Parity
description: Same spec through all entry points
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip(),
            True,
            id="valid-spec",
        ),
        pytest.param(
            """
dataset_id: dataset.validation_parity
title: Validation Parity
description: Source list must not be empty
sources: []
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip(),
            False,
            id="empty-sources",
        ),
    ],
)
def test_python_cli_and_service_agree_on_spec_validity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    spec_yaml: str,
    expected_valid: bool,
) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(spec_yaml + "\n", encoding="utf-8")

    try:
        validate_spec(load_spec(spec_path))
    except (SpecLoadError, ValidationError):
        python_valid = False
    else:
        python_valid = True

    cli_exit_code = main(["validate", str(spec_path)])
    capsys.readouterr()

    service = BuilderService(output_root=tmp_path, client_factory=_unexpected_client)
    response = dispatch(service, "POST", "/validate", {"spec": spec_yaml})

    assert python_valid is expected_valid
    assert (cli_exit_code == 0) is expected_valid
    assert (response.status_code == 200) is expected_valid
