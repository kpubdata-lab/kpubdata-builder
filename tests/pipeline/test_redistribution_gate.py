"""The legacy publish path refuses what the source's terms forbid (#688, owner decision D2)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPTS = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from pipeline.redistribution import publish_refusal, redistribution_verdict  # noqa: E402

_CONFIGS = _SCRIPTS / "configs"


def _config(name: str | None, licence: str | None = "other") -> dict[str, Any]:
    card: dict[str, Any] = {}
    if licence is not None:
        card["license"] = licence
    if name is not None:
        card["license_name"] = name
    return {"card": card, "output": {"staging_dir": "x", "hf_repo": "o/r"}}


@pytest.mark.parametrize(
    ("name", "licence", "verdict"),
    [
        ("korea-public-data-unrestricted", "other", "allowed"),
        ("kogl-type-1", "other", "allowed"),
        ("kogl-type-2", "other", "non_commercial"),
        ("kogl-type-3", "other", "forbidden"),
        ("kogl-type-4", "other", "forbidden"),
        ("krx-terms", "other", "unknown"),
        ("kipris-terms", "other", "unknown"),
        (None, "cc-by-4.0", "unknown"),
        (None, None, "unknown"),
    ],
)
def test_the_verdict_follows_the_stated_terms(
    name: str | None, licence: str | None, verdict: str
) -> None:
    assert redistribution_verdict(_config(name, licence)).verdict == verdict


@pytest.mark.parametrize(
    "config",
    [
        _config("kogl-type-3"),
        _config("kogl-type-4"),
        _config("krx-terms"),
        _config("kipris-terms"),
        _config(None, None),
        _config(None, "cc-by-4.0"),
    ],
    ids=["kogl-3", "kogl-4", "krx", "kipris", "no-terms", "unchecked-cc-by"],
)
@pytest.mark.parametrize("targets", [("hf",), ("kaggle",), ("hf", "kaggle")])
def test_forbidden_and_unknown_never_publish(
    config: dict[str, Any], targets: tuple[str, ...]
) -> None:
    """Negative: unknown is not permission, on any target, public or not."""
    for public in (True, False):
        assert (
            publish_refusal(
                config, targets=targets, kaggle_public=public, confirm_non_commercial=True
            )
            is not None
        )


def test_non_commercial_goes_only_to_a_private_kaggle_dataset_when_confirmed() -> None:
    config = _config("kogl-type-2")

    assert publish_refusal(
        config, targets=("hf",), kaggle_public=False, confirm_non_commercial=True
    )
    assert publish_refusal(
        config, targets=("kaggle",), kaggle_public=True, confirm_non_commercial=True
    )
    assert publish_refusal(
        config, targets=("kaggle",), kaggle_public=False, confirm_non_commercial=False
    )
    assert (
        publish_refusal(
            config, targets=("kaggle",), kaggle_public=False, confirm_non_commercial=True
        )
        is None
    )


def test_allowed_terms_publish() -> None:
    assert (
        publish_refusal(
            _config("kogl-type-1"),
            targets=("hf", "kaggle"),
            kaggle_public=True,
            confirm_non_commercial=False,
        )
        is None
    )


def test_every_legacy_config_gets_a_verdict_it_can_be_held_to() -> None:
    """The gate covers every config still on the legacy path (D2): each is judged."""
    verdicts: dict[str, str] = {}
    for path in sorted(_CONFIGS.rglob("*.yaml")):
        if "templates" in path.parts:
            continue
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        verdicts[str(path.relative_to(_CONFIGS))] = redistribution_verdict(config).verdict

    assert verdicts["air_quality.yaml"] == "forbidden"  # KOGL type 3, stopped (D6)
    assert verdicts["korea_base_rate.yaml"] == "allowed"  # BOK terms read (#677)
    assert set(verdicts.values()) <= {"allowed", "forbidden", "unknown"}


def test_the_publish_script_refuses_before_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import publish_to_hf

    config_path = tmp_path / "forbidden.yaml"
    config_path.write_text(yaml.safe_dump(_config("kogl-type-3")), encoding="utf-8")

    def no_fetch(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("a refused config must not be fetched")

    monkeypatch.setattr(publish_to_hf, "fetch_records", no_fetch)
    with pytest.raises(SystemExit) as exc:
        publish_to_hf.main([str(config_path), "--target", "hf"])

    assert exc.value.code == 2


def test_a_local_only_run_is_not_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import publish_to_hf

    config_path = tmp_path / "forbidden.yaml"
    config_path.write_text(yaml.safe_dump(_config("kogl-type-3")), encoding="utf-8")
    monkeypatch.setattr(publish_to_hf, "fetch_records", lambda *_a, **_k: [])

    with pytest.raises(SystemExit) as exc:
        publish_to_hf.main([str(config_path), "--local-only"])

    assert exc.value.code == 1  # past the gate: stops later, on "no records fetched"
