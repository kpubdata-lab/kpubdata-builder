"""``GET /builds`` cuts the newest N from the requester's own runs (#1191).

Where a list is the requester's own, the newest N of everyone's runs were taken first
and the requester's kept from among them. With enough runs by other people in front, a
user got a short page or an empty one, and with no cursor there was no way to the runs
behind them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.stages.bronze.build import SourceClient

ME = Principal(kind="oidc", identifier="me", owner_id="oidc:me")
OTHER = Principal(kind="oidc", identifier="other", owner_id="oidc:other")


def _no_client(**_kwargs: object) -> SourceClient:
    raise AssertionError("no provider is called here")


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    return BuilderService(output_root=tmp_path, client_factory=_no_client)


def _index(
    service: BuilderService,
    run_id: str,
    day: int,
    owner: Principal,
    *,
    dataset_id: str | None = None,
    legacy: bool = False,
) -> None:
    """One finished run in the index, on the given day of January."""
    service._build_index.insert_or_replace(
        run_id=run_id,
        status="ok",
        started_at=f"2026-01-{day:02d}T10:00:00Z",
        finished_at=f"2026-01-{day:02d}T10:05:00Z",
        created_by=owner.label,
        dataset_id=dataset_id,
        # A run from before owner ids were recorded has only the label.
        owner_id=None if legacy else owner.owner_id,
    )


def _ids(service: BuilderService, **kwargs: object) -> list[str]:
    response = service.list_builds(**kwargs)  # type: ignore[arg-type]
    assert response.status_code == 200
    return [
        cast(str, build["run_id"])
        for build in cast(list[dict[str, object]], response.body["builds"])
    ]


def test_own_run_behind_other_peoples_newer_runs_is_listed(service: BuilderService) -> None:
    _index(service, "mine-old", 1, ME)
    for day in range(2, 8):
        _index(service, f"theirs-{day}", day, OTHER)

    assert _ids(service, limit=3, principal=ME) == ["mine-old"]


def test_the_limit_is_taken_from_the_requesters_own_runs(service: BuilderService) -> None:
    for day in (1, 3, 5, 7):
        _index(service, f"mine-{day}", day, ME)
    for day in (2, 4, 6, 8, 9, 10):
        _index(service, f"theirs-{day}", day, OTHER)

    assert _ids(service, limit=2, principal=ME) == ["mine-7", "mine-5"]
    assert _ids(service, limit=50, principal=ME) == ["mine-7", "mine-5", "mine-3", "mine-1"]


def test_the_dataset_filter_is_applied_before_the_limit_too(service: BuilderService) -> None:
    _index(service, "mine-air-old", 1, ME, dataset_id="air")
    _index(service, "mine-bike", 5, ME, dataset_id="bike")
    _index(service, "mine-bike-newer", 6, ME, dataset_id="bike")
    for day in range(7, 12):
        _index(service, f"theirs-air-{day}", day, OTHER, dataset_id="air")

    assert _ids(service, limit=2, principal=ME, dataset_id="air") == ["mine-air-old"]
    assert _ids(service, limit=1, principal=ME, dataset_id="bike") == ["mine-bike-newer"]


def test_a_run_from_before_owner_ids_is_found_by_its_label(service: BuilderService) -> None:
    _index(service, "mine-legacy", 1, ME, legacy=True)
    for day in range(2, 6):
        _index(service, f"theirs-{day}", day, OTHER, legacy=True)

    assert _ids(service, limit=2, principal=ME) == ["mine-legacy"]


def test_a_user_with_no_runs_gets_none_and_no_scan(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index that has runs and none of theirs is the answer; the disk is not read."""
    for day in range(1, 4):
        _index(service, f"theirs-{day}", day, OTHER)

    def no_scan(_self: Path) -> object:
        raise AssertionError("the run directories were scanned")

    monkeypatch.setattr(Path, "iterdir", no_scan)

    assert _ids(service, limit=3, principal=ME) == []


def test_a_list_that_is_not_filtered_is_the_newest_of_all_as_before(
    service: BuilderService,
) -> None:
    """A call from inside the process (no principal) is not the requester's own list."""
    _index(service, "mine-old", 1, ME)
    for day in range(2, 6):
        _index(service, f"theirs-{day}", day, OTHER)

    assert _ids(service, limit=2) == ["theirs-5", "theirs-4"]


def _manifest(root: Path, run_id: str, day: int, owner: Principal) -> None:
    run_dir = root / run_id
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "started_at": f"2026-01-{day:02d}T10:00:00Z",
                "finished_at": f"2026-01-{day:02d}T10:05:00Z",
                "created_by": owner.label,
                "owner_id": owner.owner_id,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    # The scan orders by modification time: later days are newer.
    stamp = 1_700_000_000 + day * 86_400
    os.utime(run_dir, (stamp, stamp))


def test_the_filesystem_scan_cuts_from_the_requesters_own_runs_too(
    service: BuilderService, tmp_path: Path
) -> None:
    """With nothing in the index, the manifests are read — in the same order."""
    _manifest(tmp_path, "mine-older", 1, ME)
    _manifest(tmp_path, "mine-old", 2, ME)
    for day in range(3, 9):
        _manifest(tmp_path, f"theirs-{day}", day, OTHER)

    assert _ids(service, limit=3, principal=ME) == ["mine-old", "mine-older"]
    assert _ids(service, limit=1, principal=ME) == ["mine-old"]
    # Unfiltered, the scan opens only the newest N, as it did.
    assert _ids(service, limit=2) == ["theirs-8", "theirs-7"]


def test_each_of_two_owners_gets_a_full_page_of_their_own(service: BuilderService) -> None:
    """Interleaved runs of two people: each list fills to the limit with its owner's (#1198)."""
    for day in range(1, 11):
        _index(service, f"mine-{day:02d}", day, ME)
        _index(service, f"theirs-{day:02d}", day, OTHER)

    mine = _ids(service, limit=4, principal=ME)
    theirs = _ids(service, limit=4, principal=OTHER)

    assert mine == ["mine-10", "mine-09", "mine-08", "mine-07"]
    assert theirs == ["theirs-10", "theirs-09", "theirs-08", "theirs-07"]
