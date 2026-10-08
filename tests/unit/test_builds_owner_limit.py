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


# ---------------------------------------------------------------- nobody else's runs

#: A machine caller with the deployment's API key: it owns what it made, like anyone.
SERVICE = Principal(kind="service", identifier="scheduler", owner_id="service:scheduler")

#: Whose run each id below is. ``collision`` is another person's run that carries the
#: requester's label in ``created_by`` — the owner id decides, not the label.
_OWNERS: dict[str, Principal] = {
    **{f"mine-{n}": ME for n in range(1, 5)},
    **{f"theirs-{n}": OTHER for n in range(5, 12)},
    **{f"service-{n}": SERVICE for n in range(12, 15)},
}
_COLLISION = "collision-15"


def _populate_index(service: BuilderService) -> None:
    for run_id, owner in _OWNERS.items():
        _index(service, run_id, int(run_id.rsplit("-", 1)[1]), owner)
    service._build_index.insert_or_replace(
        run_id=_COLLISION,
        status="ok",
        started_at="2026-01-15T10:00:00Z",
        finished_at="2026-01-15T10:05:00Z",
        created_by=ME.label,
        owner_id=OTHER.owner_id,
    )


def _populate_disk(root: Path) -> None:
    for run_id, owner in _OWNERS.items():
        _manifest(root, run_id, int(run_id.rsplit("-", 1)[1]), owner)
    _manifest(root, _COLLISION, 15, OTHER)
    manifest = root / _COLLISION / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["created_by"] = ME.label
    manifest.write_text(json.dumps(data), encoding="utf-8")


def _owned_by(principal: Principal) -> set[str]:
    return {run_id for run_id, owner in _OWNERS.items() if owner is principal}


@pytest.mark.parametrize("where", ["index", "disk"])
@pytest.mark.parametrize("who", [ME, OTHER, SERVICE], ids=["me", "other", "service"])
def test_no_limit_shows_a_run_of_anyone_else(
    service: BuilderService, tmp_path: Path, where: str, who: Principal
) -> None:
    """Whatever the limit, on either path: only the requester's own runs (#1191, #1198).

    The lists above compare exact answers; this one states the rule itself, for every
    limit from one to more than there are runs, for a person, another person and the
    API key, and with a run whose label says one owner and whose owner id says another.
    """
    if where == "index":
        _populate_index(service)
    else:
        _populate_disk(tmp_path)
    total = len(_OWNERS) + 1
    # The collision run is OTHER's by owner id, whatever its label says.
    own = _owned_by(who) | ({_COLLISION} if who is OTHER else set())

    for limit in range(1, total + 3):
        listed = _ids(service, limit=limit, principal=who)

        assert set(listed) <= own, f"limit={limit}: {set(listed) - own} are not theirs"
        assert len(listed) == min(limit, len(own)), f"limit={limit}"
        assert len(set(listed)) == len(listed)


@pytest.mark.parametrize("where", ["index", "disk"])
def test_a_run_with_my_label_and_another_owner_id_is_not_mine(
    service: BuilderService, tmp_path: Path, where: str
) -> None:
    if where == "index":
        _populate_index(service)
    else:
        _populate_disk(tmp_path)

    assert _COLLISION not in _ids(service, limit=50, principal=ME)
    assert _COLLISION in _ids(service, limit=50, principal=OTHER)
