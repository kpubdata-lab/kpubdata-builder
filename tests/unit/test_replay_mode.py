"""Replay is Builder's own setting, with fixtures Builder ships (#837).

Studio's end-to-end run read ``../kpubdata/tests/fixtures`` and set kpubdata's own
variables to start Builder in replay mode. Builder now ships the fixture and takes
the setting itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder import cli
from kpubdata_builder.replay import (
    BUNDLED_FIXTURES,
    PLACEHOLDER_KEY,
    REPLAY_DIR_ENV,
    enable_replay,
    export_fixtures,
)
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import JsonValue

_KPUBDATA_VARS = ("KPUBDATA_MODE", "KPUBDATA_REPLAY_DIR", "KPUBDATA_DATAGO_API_KEY")

_SPEC = """\
dataset_id: replay.air_station
title: Replay
description: d
sources:
  - provider: datago
    dataset: air_station
    params:
      stationName: 강남구
      dataTerm: daily
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """enable_replay writes os.environ; touching the variables first restores them."""
    for name in (*_KPUBDATA_VARS, REPLAY_DIR_ENV):
        monkeypatch.delenv(name, raising=False)


def _metas(root: Path) -> list[Path]:
    return sorted(root.rglob("*.meta.json"))


def test_every_bundled_recording_matches_its_digest_and_carries_no_key() -> None:
    metas = _metas(BUNDLED_FIXTURES)
    assert metas, "the package must ship at least one recording"
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = meta_path.with_name(meta_path.name.replace(".meta.json", ".raw.json"))
        assert hashlib.sha256(raw.read_bytes()).hexdigest() == meta["response_sha256"]
        assert meta["params"].get("serviceKey") == "[REDACTED]"


def test_enabling_replay_translates_into_kpubdatas_variables() -> None:
    import os

    placeholders = enable_replay(BUNDLED_FIXTURES)

    assert os.environ["KPUBDATA_MODE"] == "replay"
    assert os.environ["KPUBDATA_REPLAY_DIR"] == str(BUNDLED_FIXTURES.resolve())
    assert placeholders == ["datago"]
    assert os.environ["KPUBDATA_DATAGO_API_KEY"] == PLACEHOLDER_KEY


def test_a_configured_key_is_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-key")

    assert enable_replay(BUNDLED_FIXTURES) == []
    assert os.environ["KPUBDATA_DATAGO_API_KEY"] == "operator-key"


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_a_directory_with_nothing_to_replay_is_refused(tmp_path: Path, make: str) -> None:
    """Negative: replay that replays nothing would send every request live."""
    import os

    target = tmp_path / "fixtures"
    if make == "empty":
        (target / "datago").mkdir(parents=True)

    with pytest.raises(ValueError):
        enable_replay(target)
    assert "KPUBDATA_MODE" not in os.environ


def test_export_copies_the_set_and_never_overwrites(tmp_path: Path) -> None:
    copied = export_fixtures(tmp_path)

    assert [p.relative_to(tmp_path) for p in _metas(tmp_path)] == [
        p.relative_to(BUNDLED_FIXTURES) for p in _metas(BUNDLED_FIXTURES)
    ]
    assert all(p.is_file() for p in copied)
    first = copied[0]
    first.write_text("edited", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export_fixtures(tmp_path)
    assert first.read_text(encoding="utf-8") == "edited"


def test_the_exported_set_replays(tmp_path: Path) -> None:
    export_fixtures(tmp_path / "fx")

    assert enable_replay(tmp_path / "fx") == ["datago"]


def test_a_build_runs_on_the_bundled_fixture_without_a_key(tmp_path: Path) -> None:
    """End to end with the real kpubdata client: no key, no network, rows out."""
    enable_replay(BUNDLED_FIXTURES)
    service = BuilderService(output_root=tmp_path, client_factory=cli._create_client)

    response = service.build(_SPEC, run_id="replay-1")

    assert response.status_code == 200, response.body
    body = cast(dict[str, JsonValue], response.body)
    (outcome,) = cast(list[dict[str, JsonValue]], body["outcomes"])
    assert outcome["status"] == "ok"
    assert outcome["stages_completed"] == ["bronze", "silver", "gold"]
    rows = (
        (tmp_path / "replay-1" / "gold" / "datago.air_station" / "data.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert rows


def test_serve_refuses_an_unusable_replay_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPLAY_DIR_ENV, str(tmp_path / "nowhere"))

    code = cli.main(["serve", "--output-dir", str(tmp_path / "out")])

    assert code == 1
    assert "no such replay fixture directory" in capsys.readouterr().err


def test_the_fixtures_command_exports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["fixtures", "export", str(tmp_path)]) == 0
    assert _metas(tmp_path)
    assert cli.main(["fixtures", "export", str(tmp_path)]) == 1
    assert "nothing was copied" in capsys.readouterr().err
