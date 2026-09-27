"""Integration tests for the agent pipeline modules.

Prerequisite: agent module must be available (feat/agent-discover-pipeline branch or merged).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kpubdata.core.spec import find_spec

try:
    from kpubdata_builder.agent.discover import DiscoveryResult, ParamInfo
    from kpubdata_builder.agent.monitor import MonitorState, check_approval
    from kpubdata_builder.agent.pipeline import run_pipeline
    from kpubdata_builder.cli import main

    _HAS_AGENT = True
except ImportError:
    _HAS_AGENT = False

pytestmark = pytest.mark.skipif(not _HAS_AGENT, reason="agent module not available")


class TestDiscoverIntegration:
    def test_generates_valid_spec_yaml(self) -> None:
        """DiscoveryResult.to_spec_yaml generates valid YAML."""
        import yaml

        result = DiscoveryResult(
            dataset_id="datago.test_integration",
            title="통합 테스트 API",
            base_url="http://apis.data.go.kr/9999999/TestSvc",
            operation="getTestList",
            data_go_kr_url="https://www.data.go.kr/data/99999/openapi.do",
            params=[
                ParamInfo(name="region", required=True, description="지역코드", example="11"),
                ParamInfo(name="year", required=False, type="integer"),
            ],
            response_fields=["name", "value", "date"],
        )
        parsed = yaml.safe_load(result.to_spec_yaml())
        assert parsed["id"] == "datago.test_integration"
        assert parsed["auth"]["type"] == "query_param"
        assert parsed["pagination"]["type"] == "page_no_rows"
        assert len(parsed["params"]) == 2
        assert len(parsed["fields"]) == 3


class TestMonitorIntegration:
    def test_state_roundtrip(self, tmp_path: Path) -> None:
        """Save state → load → modify → save integrity check."""
        state_file = tmp_path / "monitor.yaml"
        state = MonitorState()
        state.add("datago.apt_trade", status="HEALTHY")
        state.add("datago.rh_trade", status="NEEDS_APPLICATION")
        state.save(state_file)

        loaded = MonitorState.load(state_file)
        assert len(loaded.pending) == 2
        loaded.add("datago.rh_trade", status="HEALTHY")
        loaded.save(state_file)

        reloaded = MonitorState.load(state_file)
        rh = next(p for p in reloaded.pending if p.dataset_id == "datago.rh_trade")
        assert rh.status == "HEALTHY"

    def test_cli_add_and_list(self, tmp_path: Path) -> None:
        """CLI monitor --add → list check workflow."""
        state_file = str(tmp_path / "state.yaml")
        assert main(["monitor", "--state-file", state_file, "--add", "datago.test1"]) == 0
        assert main(["monitor", "--state-file", state_file, "--add", "datago.test2"]) == 0
        state = MonitorState.load(Path(state_file))
        assert len(state.pending) == 2

    def test_check_nonexistent_spec(self) -> None:
        """Nonexistent spec → SPEC_NOT_FOUND."""
        assert check_approval("nonexistent.dataset_xyz") == "SPEC_NOT_FOUND"


class TestPipelineIntegration:
    def test_nonexistent_spec_fails(self, tmp_path: Path) -> None:
        result = run_pipeline("nonexistent.xyz", kpubdata_root=tmp_path, skip_pr=True)
        assert not result.success
        assert result.step_reached == "verify_spec"


class TestCrossRepoIntegration:
    """Builder ↔ kpubdata spec system integration."""

    def test_all_specs_have_verify_required_fields(self) -> None:
        """All bundled specs have required fields for verify."""
        from kpubdata.core.spec import discover_specs

        for spec in discover_specs():
            assert spec.id, "spec에 id가 없음"
            assert spec.endpoint.base_url, f"{spec.id}: base_url 없음"
            assert spec.auth.type, f"{spec.id}: auth.type 없음"
            assert spec.pagination.type, f"{spec.id}: pagination.type 없음"
            assert spec.response.format, f"{spec.id}: response.format 없음"
            assert spec.response.envelope, f"{spec.id}: response.envelope 없음"

    def test_ocean_buoy_spec_loadable(self) -> None:
        spec = find_spec("datago.ocean_buoy")
        if spec is None:
            pytest.skip("datago.ocean_buoy not yet merged")
        assert spec.endpoint.operation == "GetTWRecentApiService"
        assert any(p.name == "obsCode" for p in spec.params)

    def test_license_field_accessible(self) -> None:
        # license is a kpubdata post-release addition. Floor version's SpecDefinition
        # has no such field, so the field's existence itself is not a premise of this test.
        spec = find_spec("datago.apt_trade")
        assert spec is not None
        license_spec = getattr(spec, "license", None)
        if license_spec is None:
            pytest.skip("this kpubdata version's SpecDefinition has no license field")
        assert hasattr(license_spec, "type")
        assert hasattr(license_spec, "commercial_use")
