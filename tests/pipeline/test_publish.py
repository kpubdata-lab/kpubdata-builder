"""Tests for scripts/pipeline/publish.py — upload logic (fully mocked, no network).

All HuggingFace Hub and Kaggle calls are mocked via sys.modules stubs so
these tests work without the optional publish extras installed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub optional dependencies before loading publish.py.
# publish.py does lazy imports (inside functions), so we pre-populate
# sys.modules with MagicMock stubs.
# ---------------------------------------------------------------------------
_hf_stub = MagicMock()
sys.modules.setdefault("huggingface_hub", _hf_stub)

_kaggle_stub = MagicMock()
_kaggle_api_stub = MagicMock()
_kaggle_api_extended_stub = MagicMock()
sys.modules.setdefault("kaggle", _kaggle_stub)
sys.modules.setdefault("kaggle.api", _kaggle_api_stub)
sys.modules.setdefault("kaggle.api.kaggle_api_extended", _kaggle_api_extended_stub)

_PUBLISH_PATH = Path(__file__).parents[2] / "scripts" / "pipeline" / "publish.py"


def _load_publish() -> Any:
    spec = importlib.util.spec_from_file_location("_pipeline_publish", _PUBLISH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


publish_mod = _load_publish()
upload_to_hf = publish_mod.upload_to_hf
upload_to_kaggle = publish_mod.upload_to_kaggle
_map_kaggle_license = publish_mod._map_kaggle_license


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _staging_dir(tmp_path: Path, *, with_data: bool = True) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "README.md").write_text("# Test", encoding="utf-8")
    if with_data:
        data = staging / "data"
        data.mkdir()
        (data / "train.parquet").write_bytes(b"fake parquet bytes")
    return staging


def _kaggle_config(slug: str = "user/test-dataset") -> dict[str, Any]:
    return {
        "output": {"hf_repo": "kpubdata/test", "kaggle_slug": slug},
        "card": {
            "title": "Test Dataset",
            "description": "A dataset for testing.",
            "license": "cc-by-4.0",
            "tags": ["korea", "test"],
            "subtitle": "Testing the kaggle upload path",
        },
    }


# ---------------------------------------------------------------------------
# upload_to_hf — dry_run
# ---------------------------------------------------------------------------


def test_upload_to_hf_dry_run_does_not_call_api(tmp_path: Path) -> None:
    staging = _staging_dir(tmp_path)
    _hf_stub.HfApi.reset_mock()

    upload_to_hf(staging, "kpubdata/test", dry_run=True)

    _hf_stub.HfApi.assert_not_called()


def test_upload_to_hf_dry_run_logs_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    staging = _staging_dir(tmp_path)
    with caplog.at_level(logging.INFO, logger="publish_to_hf.publish"):
        upload_to_hf(staging, "kpubdata/test", dry_run=True)

    assert any("DRY RUN" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# upload_to_hf — live (mocked HfApi via sys.modules stub)
# ---------------------------------------------------------------------------


def test_upload_to_hf_live_calls_create_and_upload(tmp_path: Path) -> None:
    staging = _staging_dir(tmp_path)
    mock_api_instance = MagicMock()
    _hf_stub.HfApi.return_value = mock_api_instance

    upload_to_hf(staging, "kpubdata/test-live", dry_run=False)

    # Explicitly set private. Omitting it delegates to huggingface_hub defaults,
    # which may vary by version.
    mock_api_instance.create_repo.assert_called_once_with(
        repo_id="kpubdata/test-live", repo_type="dataset", exist_ok=True, private=False
    )
    mock_api_instance.upload_folder.assert_called_once()
    # Delete files that existed only in prior revisions. Otherwise renamed
    # old files persist forever.
    assert mock_api_instance.upload_folder.call_args.kwargs["delete_patterns"] == [
        "data/*",
        "README.md",
    ]


def test_upload_to_hf_copies_readme_and_data(tmp_path: Path) -> None:
    """Verify README.md and data/ are present in the upload folder before cleanup."""
    staging = _staging_dir(tmp_path)
    found_files: list[list[str]] = []
    mock_api_instance = MagicMock()

    def _inspect_and_upload(**kwargs: Any) -> None:
        # Called while upload dir still exists — record what's inside.
        upload_dir = Path(kwargs["folder_path"])
        found_files.append([str(p.relative_to(upload_dir)) for p in upload_dir.rglob("*")])

    mock_api_instance.upload_folder.side_effect = _inspect_and_upload
    _hf_stub.HfApi.return_value = mock_api_instance

    upload_to_hf(staging, "kpubdata/test", dry_run=False)

    assert len(found_files) == 1
    flat = found_files[0]
    assert "README.md" in flat
    assert str(Path("data") / "train.parquet") in flat


# ---------------------------------------------------------------------------
# upload_to_kaggle — dry_run
# ---------------------------------------------------------------------------


def test_upload_to_kaggle_dry_run_does_not_authenticate(tmp_path: Path) -> None:
    staging = _staging_dir(tmp_path)
    config = _kaggle_config()
    mock_api_instance = MagicMock()
    _kaggle_api_extended_stub.KaggleApi.return_value = mock_api_instance

    upload_to_kaggle(staging, config, dry_run=True)

    mock_api_instance.authenticate.assert_not_called()


def test_upload_to_kaggle_dry_run_logs_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    staging = _staging_dir(tmp_path)
    config = _kaggle_config()

    with caplog.at_level(logging.INFO, logger="publish_to_hf.publish"):
        upload_to_kaggle(staging, config, dry_run=True)

    assert any("DRY RUN" in r.message for r in caplog.records)


def test_upload_to_kaggle_dry_run_cleans_up_upload_dir(tmp_path: Path) -> None:
    staging = _staging_dir(tmp_path)
    config = _kaggle_config()

    upload_to_kaggle(staging, config, dry_run=True)

    # The temp upload dir must not linger after dry_run
    assert not (staging / ".kaggle_upload").exists()


def test_upload_to_kaggle_dry_run_writes_metadata_json(tmp_path: Path) -> None:
    """Metadata JSON content is correct (inspected before cleanup via mock)."""
    staging = _staging_dir(tmp_path)
    config = _kaggle_config("myorg/my-dataset")
    written_metadata: list[dict[str, Any]] = []

    # Intercept shutil.rmtree to capture the metadata before cleanup
    import shutil as shutil_mod

    original_rmtree = shutil_mod.rmtree

    def _capture_and_remove(path: str, **kwargs: Any) -> None:
        p = Path(path)
        meta_file = p / "dataset-metadata.json"
        if meta_file.exists():
            written_metadata.append(json.loads(meta_file.read_text(encoding="utf-8")))
        original_rmtree(path, **kwargs)

    import unittest.mock as mock_mod

    with mock_mod.patch.object(shutil_mod, "rmtree", side_effect=_capture_and_remove):
        upload_to_kaggle(staging, config, dry_run=True)

    assert len(written_metadata) == 1
    md = written_metadata[0]
    assert md["id"] == "myorg/my-dataset"
    assert md["title"] == "Test Dataset"
    assert "CC-BY-4.0" in md["licenses"][0]["name"]


def test_upload_to_kaggle_no_slug_skips_gracefully(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    staging = _staging_dir(tmp_path)
    config: dict[str, Any] = {
        "output": {"hf_repo": "kpubdata/test"},  # no kaggle_slug
        "card": {"title": "T", "description": "D", "license": "cc0-1.0", "tags": []},
    }

    with caplog.at_level(logging.ERROR, logger="publish_to_hf.publish"):
        upload_to_kaggle(staging, config)

    assert any("kaggle_slug" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _map_kaggle_license
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hf_license", "expected"),
    [
        ("cc-by-4.0", "CC-BY-4.0"),
        ("cc0-1.0", "CC0-1.0"),
        ("cc-by-sa-4.0", "CC-BY-SA-4.0"),
        ("apache-2.0", "apache-2.0"),
        ("mit", "other"),
        ("odc-by", "ODC-BY-1.0"),
        ("odbl", "ODbL-1.0"),
        ("unknown-license", "other"),
    ],
)
def test_map_kaggle_license(hf_license: str, expected: str) -> None:
    assert _map_kaggle_license(hf_license) == expected


def test_kaggle_create_is_private_unless_public_is_requested(tmp_path: Path) -> None:
    """Public publication is an explicit choice.

    CLI publish requires --public opt-in, but this path had public=True
    hardcoded. Visibility policy differed depending on which path uploaded.
    """
    staging = _staging_dir(tmp_path)
    api = MagicMock()
    api.dataset_list.return_value = []
    _kaggle_api_extended_stub.KaggleApi.return_value = api

    upload_to_kaggle(staging, _kaggle_config(), dry_run=False)

    assert api.dataset_create_new.call_args.kwargs["public"] is False


def test_kaggle_create_can_be_made_public_explicitly(tmp_path: Path) -> None:
    staging = _staging_dir(tmp_path)
    api = MagicMock()
    api.dataset_list.return_value = []
    _kaggle_api_extended_stub.KaggleApi.return_value = api

    upload_to_kaggle(staging, _kaggle_config(), dry_run=False, public=True)

    assert api.dataset_create_new.call_args.kwargs["public"] is True


def test_a_failed_kaggle_lookup_does_not_become_create(tmp_path: Path) -> None:
    """Query failure means "unknown", not "absent".

    Treating it as absent risks calling create_new on existing datasets,
    or worse, creating unintended new datasets.
    """
    staging = _staging_dir(tmp_path)
    api = MagicMock()
    api.dataset_list.side_effect = RuntimeError("kaggle api unavailable")
    _kaggle_api_extended_stub.KaggleApi.return_value = api

    with pytest.raises(RuntimeError):
        upload_to_kaggle(staging, _kaggle_config(), dry_run=False)

    api.dataset_create_new.assert_not_called()
    api.dataset_create_version.assert_not_called()
