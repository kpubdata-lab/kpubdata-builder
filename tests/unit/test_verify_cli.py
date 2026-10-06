"""Tests for the verify CLI subcommand."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kpubdata_builder.cli import build_parser, dispatch
from kpubdata_builder.verify.models import CheckName, CheckResult, DatasetStatus, VerifyResult

# All tests in this module patch the verify.runner module which is imported
# lazily inside _run_verify via ``from .verify import runner as _verify_runner``.
_RUNNER_MOD = "kpubdata_builder.verify.runner"
_SPEC_MOD = "kpubdata"


class TestVerifyParser:
    """Parser recognizes the verify subcommand and its arguments."""

    def test_verify_single_dataset(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify", "datago.apt_trade"])
        assert args.command == "verify"
        assert args.dataset == "datago.apt_trade"
        assert args.verify_all is False

    def test_verify_all(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify", "--all"])
        assert args.command == "verify"
        assert args.dataset is None
        assert args.verify_all is True

    def test_verify_with_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify", "datago.apt_trade", "--output", "results.yaml"])
        assert args.output == "results.yaml"

    def test_verify_page_size(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify", "datago.apt_trade", "--page-size", "5"])
        assert args.page_size == 5

    def test_verify_default_page_size(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify", "datago.apt_trade"])
        assert args.page_size == 10


def _healthy_result(dataset_id: str = "datago.apt_trade") -> VerifyResult:
    return VerifyResult(
        dataset_id=dataset_id,
        status=DatasetStatus.HEALTHY,
        checks=[
            CheckResult(CheckName.ENDPOINT, passed=True),
            CheckResult(CheckName.AUTH, passed=True),
            CheckResult(CheckName.RESPONSE, passed=True),
            CheckResult(CheckName.PARSER, passed=True, detail="10 items, total=100"),
            CheckResult(CheckName.PAGINATION, passed=True),
            CheckResult(CheckName.SCHEMA, passed=True, detail="baseline abc123.."),
        ],
        records_tested=10,
        total_latency_ms=412.0,
        schema_hash="abc123def456ghij",
    )


class TestVerifyDispatch:
    """Dispatch routes to verify handler."""

    def test_verify_single_healthy(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake_spec = MagicMock()
        fake_spec.id = "datago.apt_trade"

        parser = build_parser()
        args = parser.parse_args(["verify", "datago.apt_trade"])

        with (
            patch(f"{_SPEC_MOD}.find_spec", return_value=fake_spec),
            patch(f"{_RUNNER_MOD}.verify_dataset", return_value=_healthy_result()),
        ):
            code = dispatch(args)

        assert code == 0
        out = capsys.readouterr().out
        assert "HEALTHY" in out

    def test_verify_single_not_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify", "nonexistent.dataset"])

        with patch(f"{_SPEC_MOD}.find_spec", return_value=None):
            code = dispatch(args)

        assert code == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_verify_needs_application_exit_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake_spec = MagicMock()
        fake_spec.id = "datago.rh_trade"

        result = VerifyResult(
            dataset_id="datago.rh_trade",
            status=DatasetStatus.NEEDS_APPLICATION,
            checks=[
                CheckResult(CheckName.ENDPOINT, passed=True),
                CheckResult(CheckName.AUTH, passed=False, detail="활용신청 필요"),
            ],
            error="활용신청 필요",
        )

        parser = build_parser()
        args = parser.parse_args(["verify", "datago.rh_trade"])

        with (
            patch(f"{_SPEC_MOD}.find_spec", return_value=fake_spec),
            patch(f"{_RUNNER_MOD}.verify_dataset", return_value=result),
        ):
            code = dispatch(args)

        assert code == 1
        out = capsys.readouterr().out
        assert "NEEDS_APPLICATION" in out

    def test_verify_no_args_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify"])

        code = dispatch(args)

        assert code == 2
        err = capsys.readouterr().err
        assert "specify a dataset" in err
