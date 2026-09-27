"""kpubdata → Builder full E2E pipeline (yeongseon/kpubdata#282).

Previous cross-repo validation only checked **shape** — `test_kpubdata_client_protocol.py`
verified that `kpubdata.Client` has the structure `.dataset().list().items`,
and `test_studio_contract.py` fixed only the wire format of Builder responses with mock clients.
So it was never confirmed in any repo's CI that records returned by kpubdata actually
pass through Bronze→Silver→Gold.

This test injects the **real `kpubdata.Client`** into Builder to close that gap.
Network and API keys are replaced with kpubdata's replay transport (`KPUBDATA_MODE=replay`) —
recorded fixtures are replayed, so it's deterministic and no real API key is needed.
Validated path:

    BuildSpec(wire) → dispatch(POST /build) → orchestrator → Bronze
      → kpubdata.Client → provider spec executor → replay fixture
      → Silver → Gold → export → manifest

Fixtures live in kpubdata repo's `tests/fixtures/` and are not in distributed wheels.
So we only run when `KPUBDATA_REPLAY_DIR` points to a fixture directory, else skip —
builder standalone CI and local runs are unaffected. This env var is set where it actually
runs: `.github/workflows/cross-repo-contract.yml`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from kpubdata_builder.service.app import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.spec import JsonValue

# Dataset selection criteria (two):
#
# 1. **Must fit one page** (totalCount 22 ≤ page_size 100). Builder Bronze calls
#    `list_all()` on kpubdata Dataset and walks pages to the end. If we request 2 pages,
#    the recorded fixture doesn't exist and replay fails.
# 2. **No mixed-type columns.** If a field declared as `integer` in spec has non-numeric
#    values mixed in, kpubdata casts only some to int, leaving int/str coexisting in one column,
#    and Builder Silver rejects it (yeongseon/kpubdata#452). apt_trade/sh_trade/
#    ultra_srt_ncst hit this — this test caught that bug on first run.
_DATASET = "air_station"
_PARAMS: dict[str, JsonValue] = {
    "station": "강남구",
    "term": "daily",
    "page": 1,
    "page_size": 100,
}
_EXPECTED_ROWS = 22


def _replay_dir() -> Path | None:
    raw = os.environ.get("KPUBDATA_REPLAY_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


requires_replay_fixtures = pytest.mark.skipif(
    _replay_dir() is None,
    reason=(
        "KPUBDATA_REPLAY_DIR이 kpubdata 레포의 tests/fixtures를 가리켜야 한다 "
        "(cross-repo-contract 워크플로가 설정한다)"
    ),
)


@pytest.fixture
def real_kpubdata_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    """BuilderService using real kpubdata.Client — transport only replaced with replay."""
    replay_dir = _replay_dir()
    assert replay_dir is not None  # guaranteed by skipif
    monkeypatch.setenv("KPUBDATA_MODE", "replay")
    monkeypatch.setenv("KPUBDATA_REPLAY_DIR", str(replay_dir))

    kpubdata = pytest.importorskip("kpubdata")
    # Replay excludes auth params from matching, so key value itself is meaningless.
    client = kpubdata.Client(provider_keys={"datago": "replay-dummy"})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)


def _spec_yaml() -> str:
    spec: dict[str, JsonValue] = {
        "dataset_id": "dataset.cross_repo_smoke",
        "title": "Cross-repo smoke",
        "description": "kpubdata replay fixture를 Builder 파이프라인 전 구간에 태운다.",
        "sources": [
            {
                "provider": "datago",
                "dataset": _DATASET,
                "params": _PARAMS,
                "alias": "measurements",
            }
        ],
        "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
    }
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


@requires_replay_fixtures
class TestCrossRepoPipeline:
    def test_real_client_records_flow_through_to_manifest(
        self, real_kpubdata_service: BuilderService, tmp_path: Path
    ) -> None:
        response = dispatch(
            real_kpubdata_service,
            "POST",
            "/build",
            {"spec": _spec_yaml(), "run_id": "cross-repo"},
        )

        assert isinstance(response, ServiceResponse)
        # On failure, show cause immediately — cross-repo regression message is itself diagnosis.
        assert response.status_code == 200, response.body
        assert response.body["status"] == "ok"

        outcomes = response.body["outcomes"]
        assert isinstance(outcomes, list) and len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, dict)
        assert outcome["status"] == "ok", outcome
        assert outcome["error"] is None

        manifest_path = tmp_path / "cross-repo" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Record count from kpubdata must be preserved all the way to pipeline end.
        # If adapter changes response parsing (items_path etc.), it breaks here first.
        row_counts = manifest["row_counts"]
        assert isinstance(row_counts, dict)
        assert sum(int(value) for value in row_counts.values()) == _EXPECTED_ROWS, row_counts

    def test_exported_rows_carry_real_provider_fields(
        self, real_kpubdata_service: BuilderService, tmp_path: Path
    ) -> None:
        response = dispatch(
            real_kpubdata_service,
            "POST",
            "/build",
            {"spec": _spec_yaml(), "run_id": "cross-repo-export"},
        )
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200, response.body

        # Export is recorded in source alias directory under Gold stage.
        exported = tmp_path / "cross-repo-export" / "gold" / "measurements" / "out" / "data.jsonl"
        assert exported.exists(), "jsonl export가 기록되지 않았다"
        rows = [json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == _EXPECTED_ROWS

        # Real field names from air measurement API must be preserved as-is. If kpubdata renames
        # fields or changes response structure, Studio UI breaks — this catches that regression.
        assert {"dataTime", "pm10Value", "khaiValue"} <= set(rows[0]), sorted(rows[0])
