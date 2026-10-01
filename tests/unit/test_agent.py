"""Tests for the agent pipeline modules."""

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from kpubdata_builder.agent import discover as discover_mod
from kpubdata_builder.agent.discover import (
    DiscoveryResult,
    ParamInfo,
    PortalUnreachable,
    _fetch_page,
)
from kpubdata_builder.agent.monitor import MonitorState
from kpubdata_builder.cli import build_parser, dispatch


class TestDiscoveryResult:
    def test_to_spec_yaml_basic(self) -> None:
        result = DiscoveryResult(
            dataset_id="datago.test_api",
            title="Test API",
            base_url="http://apis.data.go.kr/1234/TestSvc",
            operation="getTestData",
            data_go_kr_url="https://www.data.go.kr/data/12345/openapi.do",
            params=[ParamInfo(name="region", required=True, description="지역코드")],
        )
        yaml_str = result.to_spec_yaml()
        assert "id: datago.test_api" in yaml_str
        assert "title: Test API" in yaml_str
        assert "base_url: http://apis.data.go.kr/1234/TestSvc" in yaml_str
        assert "operation: getTestData" in yaml_str
        assert "name: region" in yaml_str
        assert "required: true" in yaml_str
        assert "status: unstable" in yaml_str

    def test_to_spec_yaml_no_params(self) -> None:
        result = DiscoveryResult(
            dataset_id="datago.simple",
            title="Simple",
            base_url="http://example.com",
            operation="op",
        )
        yaml_str = result.to_spec_yaml()
        assert "params:" not in yaml_str


class TestMonitorState:
    def test_add_and_save_load(self, tmp_path: Path) -> None:
        state = MonitorState()
        state.add("datago.test1", status="NEEDS_APPLICATION")
        state.add("datago.test2", status="WAITING_APPROVAL")
        assert len(state.pending) == 2

        state_file = tmp_path / "monitor.yaml"
        state.save(state_file)
        assert state_file.is_file()

        loaded = MonitorState.load(state_file)
        assert len(loaded.pending) == 2
        assert loaded.pending[0].dataset_id == "datago.test1"
        assert loaded.pending[1].status == "WAITING_APPROVAL"

    def test_add_updates_existing(self) -> None:
        state = MonitorState()
        state.add("datago.test1", status="NEEDS_APPLICATION")
        state.add("datago.test1", status="HEALTHY")
        assert len(state.pending) == 1
        assert state.pending[0].status == "HEALTHY"

    def test_remove(self) -> None:
        state = MonitorState()
        state.add("datago.test1")
        removed = state.remove("datago.test1")
        assert removed is not None
        assert removed.dataset_id == "datago.test1"
        assert len(state.pending) == 0

    def test_remove_nonexistent(self) -> None:
        state = MonitorState()
        assert state.remove("datago.nope") is None

    def test_load_empty_file(self, tmp_path: Path) -> None:
        state_file = tmp_path / "empty.yaml"
        loaded = MonitorState.load(state_file)
        assert len(loaded.pending) == 0


class TestDiscoverCLI:
    def test_discover_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["discover", "https://www.data.go.kr/data/12345/openapi.do"])
        assert args.command == "discover"
        assert "12345" in args.url

    def test_discover_with_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "discover",
                "https://example.com",
                "--output",
                "out.yaml",
                "--dataset-id",
                "datago.custom",
            ]
        )
        assert args.output == "out.yaml"
        assert args.dataset_id == "datago.custom"


class TestFetchPage:
    """The portal refuses some origins; the failure must say where to go."""

    def test_http_refusal_names_the_next_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://www.data.go.kr/data/12345/openapi.do"

        def refused(_req, timeout: int = 0) -> None:
            raise HTTPError(url, 403, "Forbidden", None, None)  # type: ignore[arg-type]

        monkeypatch.setattr(discover_mod, "urlopen", refused)

        with pytest.raises(PortalUnreachable) as exc_info:
            _fetch_page(url)

        assert exc_info.value.status == 403
        assert "kr runner" in str(exc_info.value)

    def test_edge_error_is_the_same_diagnosis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://www.data.go.kr/data/12345/openapi.do"

        def edge_error(_req, timeout: int = 0) -> None:
            raise HTTPError(url, 500, "Server Error", None, None)  # type: ignore[arg-type]

        monkeypatch.setattr(discover_mod, "urlopen", edge_error)

        with pytest.raises(PortalUnreachable) as exc_info:
            _fetch_page(url)

        assert exc_info.value.status == 500

    def test_network_failure_is_not_a_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://www.data.go.kr/data/12345/openapi.do"

        def unreachable(_req, timeout: int = 0) -> None:
            raise URLError(TimeoutError("timed out"))

        monkeypatch.setattr(discover_mod, "urlopen", unreachable)

        with pytest.raises(PortalUnreachable) as exc_info:
            _fetch_page(url)

        assert exc_info.value.status is None
        assert "network" in str(exc_info.value)

    def test_request_carries_browser_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"<html></html>"

        def capture(req: object, timeout: int = 0) -> FakeResponse:
            captured.update(req.headers)  # type: ignore[attr-defined]
            return FakeResponse()

        monkeypatch.setattr(discover_mod, "urlopen", capture)

        html = _fetch_page("https://www.data.go.kr/data/12345/openapi.do")

        assert html == "<html></html>"
        assert "text/html" in str(captured.get("Accept", ""))
        assert "ko-KR" in str(captured.get("Accept-language", ""))


class TestDiscoverUnreachableExit:
    def test_portal_refusal_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def refused(_url: str) -> DiscoveryResult:
            raise PortalUnreachable("the portal answered HTTP 403 — run from the kr runner")

        monkeypatch.setattr(discover_mod, "discover_from_url", refused)
        parser = build_parser()
        args = parser.parse_args(["discover", "https://www.data.go.kr/data/12345/openapi.do"])

        code = dispatch(args)

        assert code == 2
        assert "kr runner" in capsys.readouterr().err


class TestMonitorCLI:
    def test_monitor_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["monitor", "--add", "datago.test"])
        assert args.command == "monitor"
        assert args.add == "datago.test"

    def test_monitor_add(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        state_file = str(tmp_path / "state.yaml")
        parser = build_parser()
        args = parser.parse_args(["monitor", "--state-file", state_file, "--add", "datago.xyz"])
        code = dispatch(args)
        assert code == 0
        out = capsys.readouterr().out
        assert "Added" in out

        loaded = MonitorState.load(Path(state_file))
        assert len(loaded.pending) == 1


class TestPipelineCLI:
    def test_pipeline_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pipeline", "datago.apt_trade", "--skip-pr"])
        assert args.command == "pipeline"
        assert args.dataset == "datago.apt_trade"
        assert args.skip_pr is True


class TestPipelineDoesNotInterpolateIntoSource:
    """dataset_id not interpolated into `python -c` source string.

    Even a single quote becomes arbitrary code, and this value comes from
    CLI args and HTTP paths.
    """

    def test_the_dataset_id_is_passed_as_an_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kpubdata_builder.agent import pipeline as pipeline_module

        seen: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(list(cmd))
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(pipeline_module, "_run", _fake_run)
        pipeline_module.run_pipeline(
            "datago.'; import os; os.system('id'); '", kpubdata_root=Path("/tmp/x")
        )

        source = seen[0][2]
        assert "os.system" not in source
        assert "sys.argv[1]" in source
        assert seen[0][3] == "datago.'; import os; os.system('id'); '"


class TestPipelineCommitsOnlyWhatItMade:
    """`git add -A` also stages others' changes and tool logs.

    The .omx/ committed to kpubdata repo is evidence of that outcome.
    """

    @staticmethod
    def _runner(monkeypatch: pytest.MonkeyPatch, status: str) -> list[list[str]]:
        """spec exists and first verify fails, flows through record to commit phase."""
        from kpubdata_builder.agent import pipeline as pipeline_module

        seen: list[list[str]] = []
        verifies = {"n": 0}

        def _fake_run(cmd: list[str], **_kwargs: object) -> object:
            seen.append(list(cmd))
            if cmd[:3] == ["git", "status", "--porcelain"]:
                return type("R", (), {"returncode": 0, "stdout": status, "stderr": ""})()
            if cmd[:2] == ["make", "verify"]:
                verifies["n"] += 1
                # First verify fails to send record path, recheck passes.
                code = 1 if verifies["n"] == 1 else 0
                return type("R", (), {"returncode": code, "stdout": "", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(pipeline_module, "_run", _fake_run)
        return seen

    def test_an_unrelated_dirty_file_stops_the_commit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kpubdata_builder.agent import pipeline as pipeline_module

        seen = self._runner(monkeypatch, "?? .omx/session.json\n")

        result = pipeline_module.run_pipeline("datago.bakery", kpubdata_root=Path("/tmp/x"))

        assert result.success is False
        assert result.step_reached == "commit"
        assert not any(cmd[:2] == ["git", "commit"] for cmd in seen)

    def test_it_never_runs_git_add_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kpubdata_builder.agent import pipeline as pipeline_module

        seen = self._runner(monkeypatch, "")
        pipeline_module.run_pipeline("datago.bakery", kpubdata_root=Path("/tmp/x"))

        assert not any(cmd[:3] == ["git", "add", "-A"] for cmd in seen)
