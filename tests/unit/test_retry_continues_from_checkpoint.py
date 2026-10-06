"""A retry continues from the checkpoint of the run it retries (#1071).

A ``param_grid`` checkpoint resumes "a rebuild of the same run id" (#648). A run id is
one attempt (#1042), so the retry of an interrupted fetch has a new id — and it fetched
every combination again, spending the provider's quota twice.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV

from .test_param_grid_checkpoint import _SPEC, _Client

_OTHER_SPEC = _SPEC.replace("sido: [a, b, c, d]", "sido: [a, b, c, d, e]")


def _interrupted(tmp_path: Path, run_id: str = "r1") -> dict[str, str]:
    """A run that fetched ``a`` and ``b`` and failed on ``c``; its files, by content hash."""
    client = _Client(fail_on="c")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)
    assert service.build(_SPEC, run_id=run_id).status_code != 200
    assert client.calls == ["a", "b", "c"]
    return _files(tmp_path / run_id)


def _files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _always(client: _Client) -> Callable[..., _Client]:
    return lambda **_kwargs: client


def _manifest(tmp_path: Path, run_id: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(
        (tmp_path / run_id / "manifest.json").read_text(encoding="utf-8")
    )
    return loaded


def test_a_retry_does_not_fetch_what_the_retried_run_already_had(tmp_path: Path) -> None:
    before = _interrupted(tmp_path)
    assert any(name.startswith("_checkpoints/") for name in before)

    client = _Client()
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)
    response = service.build(_SPEC, run_id="r2", retry_of="r1")

    assert response.status_code == 200, response.body
    # Only what was missing was asked of the provider.
    assert client.calls == ["c", "d"]
    # The retried run's files are as they were.
    assert _files(tmp_path / "r1") == before


def test_the_retry_says_where_its_records_came_from(tmp_path: Path) -> None:
    _interrupted(tmp_path)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())

    assert service.build(_SPEC, run_id="r2", retry_of="r1").status_code == 200

    manifest = _manifest(tmp_path, "r2")
    assert manifest["retry_of"] == "r1"
    reproducibility = manifest["reproducibility"]
    assert isinstance(reproducibility, dict)
    assert reproducibility["reason"] == "resumed_from_checkpoint"
    (entry,) = reproducibility["resumed_sources"].values()
    assert entry == {"resumed_combinations": 2, "total_combinations": 4, "checkpoint_from": "r1"}
    events = service._event_store.list_for_run("r2", limit=100, tail=False)
    (started,) = [event for event in events if event.event == "source_fetch_started"]
    assert started.message == "continuing from the checkpoint of run r1"
    # Its own copy is gone once Bronze is written, like any checkpoint.
    assert not list((tmp_path / "r2" / "_checkpoints").rglob("*.jsonl"))


def test_the_complete_result_equals_a_run_fetched_in_one_go(tmp_path: Path) -> None:
    _interrupted(tmp_path)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())
    assert service.build(_SPEC, run_id="r2", retry_of="r1").status_code == 200
    assert service.build(_SPEC, run_id="whole").status_code == 200

    def rows(run_id: str) -> list[str]:
        path = tmp_path / run_id / "gold" / "datago.air_quality" / "data.jsonl"
        return sorted(path.read_text(encoding="utf-8").splitlines())

    assert rows("r2") == rows("whole")
    assert len(rows("r2")) == 8


def test_a_changed_spec_starts_from_nothing(tmp_path: Path) -> None:
    """Negative: the checkpoint is another spec's records."""
    before = _interrupted(tmp_path)
    client = _Client()
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)

    assert service.build(_OTHER_SPEC, run_id="r2", retry_of="r1").status_code == 200

    assert client.calls == ["a", "b", "c", "d", "e"]
    assert "reproducibility" not in _manifest(tmp_path, "r2")
    assert _files(tmp_path / "r1") == before


def test_a_run_that_retries_nothing_or_a_run_with_no_checkpoint_fetches_everything(
    tmp_path: Path,
) -> None:
    """Negative: there is something to continue from only after an interrupted fetch."""
    done = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())
    assert done.build(_SPEC, run_id="finished").status_code == 200

    for retry_of in (None, "finished", "no-such-run"):
        client = _Client()
        service = BuilderService(output_root=tmp_path, client_factory=_always(client))
        run_id = f"after-{retry_of}"
        assert service.build(_SPEC, run_id=run_id, retry_of=retry_of).status_code == 200
        assert client.calls == ["a", "b", "c", "d"], retry_of
        assert "reproducibility" not in _manifest(tmp_path, run_id)


def test_another_owners_run_is_not_continued_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative, through the route: naming someone else's run as ``retry_of`` is refused
    before any build starts, so nothing of theirs is read."""
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    alice = Principal(kind="oidc", identifier="alice", owner_id="oidc:alice")
    bob = Principal(kind="oidc", identifier="bob", owner_id="oidc:bob")

    failing = _Client(fail_on="c")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: failing)
    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: alice)
    first = dispatch(service, "POST", "/build", {"spec": _SPEC, "run_id": "r1"})
    assert isinstance(first, ServiceResponse) and first.status_code != 200
    before = _files(tmp_path / "r1")

    client = _Client()
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)
    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: bob)
    refused = dispatch(service, "POST", "/build", {"spec": _SPEC, "run_id": "r2", "retry_of": "r1"})

    assert isinstance(refused, ServiceResponse) and refused.status_code in (403, 404)
    assert client.calls == []
    assert not (tmp_path / "r2").exists()
    assert _files(tmp_path / "r1") == before

    # Its owner can.
    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: alice)
    allowed = dispatch(service, "POST", "/build", {"spec": _SPEC, "run_id": "r3", "retry_of": "r1"})
    assert isinstance(allowed, ServiceResponse) and allowed.status_code == 200, allowed.body
    assert client.calls == ["c", "d"]


def test_a_link_in_the_retried_runs_checkpoint_is_not_followed(tmp_path: Path) -> None:
    """A checkpoint directory that is a link could point outside the run."""
    _interrupted(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.jsonl").write_text("{}\\n", encoding="utf-8")
    (tmp_path / "r1" / "_checkpoints" / "linked").symlink_to(outside, target_is_directory=True)
    client = _Client()
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)

    assert service.build(_SPEC, run_id="r2", retry_of="r1").status_code == 200

    assert client.calls == ["c", "d"]
    assert not (tmp_path / "r2" / "_checkpoints" / "linked").exists()


def test_a_link_to_a_file_inside_a_checkpoint_is_not_copied(tmp_path: Path) -> None:
    """Neither a link to a file: its target could be anything on the machine."""
    _interrupted(tmp_path)
    (tmp_path / "secret.txt").write_text("not part of any checkpoint", encoding="utf-8")
    (source_dir,) = [path for path in (tmp_path / "r1" / "_checkpoints").iterdir() if path.is_dir()]
    (source_dir / "999999.jsonl").symlink_to(tmp_path / "secret.txt")
    copied: list[str] = []
    real_copy = shutil.copy2

    def recording(source: Path, target: Path) -> object:
        copied.append(Path(source).name)
        return real_copy(source, target)

    client = _Client()
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shutil, "copy2", recording)
        assert service.build(_SPEC, run_id="r2", retry_of="r1").status_code == 200

    # The two finished combinations and their index were taken over; the link was not.
    assert "999999.jsonl" not in copied
    assert "index.jsonl" in copied and len(copied) == 3
    assert client.calls == ["c", "d"]
