"""Publisher for publishing datasets via Kaggle API."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from threading import Lock

from ..errors import PublishError
from .base import BasePublisher, PublishResult

#: Environment variables that Kaggle SDK reads credentials from. When service path
#: receives empty credentials, we clear these keys so SDK doesn't auth as server.
_KAGGLE_ENVIRONMENT_KEYS = frozenset({"KAGGLE_USERNAME", "KAGGLE_KEY"})

#: Directory where SDK looks for kaggle.json. When credentials given, we override so
#: Conversely, closes path where even empty environment authenticates with server file account.
_KAGGLE_CONFIG_DIR_ENV = "KAGGLE_CONFIG_DIR"


#: Serializes section touching process-wide environment.
#:
#: receipt serialization is per ``(owner, run, target)``, so **doesn't prevent concurrent
#: publish of different principals.** If both enter section together, one can
#: authenticate as other —
#: env vars belong to process, not thread. As long as SDK doesn't take creds as arg,
#: this lock is only boundary.
_KAGGLE_ENVIRONMENT_LOCK = Lock()


@contextlib.contextmanager
def _kaggle_environment(credentials: Mapping[str, str] | None) -> Iterator[None]:
    """Place received Kaggle credentials in environment exclusively for this block.

    Kaggle SDK has no way to pass credentials as argument, so we use environment.
    Exiting block restores original values, so one request's credentials don't leak
    to next request.

    ``None`` means "caller didn't specify" (CLI path), so environment is unchanged.
    **Empty mapping means "nothing to pass"**, so SDK doesn't pick up server account;
    we clear related env vars for this block — previously didn't distinguish, so
    even when requiring caller credentials, publish went as server account (#635).

    Clearing env vars alone isn't enough. ``KaggleApi.authenticate()`` reads
    ``~/.kaggle/kaggle.json`` if credentials not found in environment, so if file
    exists on server, auth becomes server account. When caller specifies credentials,
    redirect ``KAGGLE_CONFIG_DIR`` to empty temp directory to close file path too.
    """
    if credentials is None:
        yield
        return
    managed = dict(credentials) if credentials else {}
    with _KAGGLE_ENVIRONMENT_LOCK, tempfile.TemporaryDirectory() as empty_config_dir:
        keys = set(managed) | _KAGGLE_ENVIRONMENT_KEYS | {_KAGGLE_CONFIG_DIR_ENV}
        previous = {key: os.environ.get(key) for key in keys}
        for key in keys:
            if key in managed:
                os.environ[key] = managed[key]
            elif key == _KAGGLE_CONFIG_DIR_ENV:
                os.environ[key] = empty_config_dir
            else:
                os.environ.pop(key, None)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class KagglePublisher(BasePublisher):
    """Upload directory containing dataset-metadata.json via Kaggle API."""

    @property
    def name(self) -> str:
        return "kaggle"

    @property
    def expects_directory(self) -> bool:
        # Kaggle API uploads per directory containing dataset-metadata.json (#176).
        return True

    def publish(
        self,
        artifact_paths: tuple[Path, ...],
        *,
        destination: str,
        public: bool = False,
        credentials: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Upload Kaggle dataset as new version.

        Args:
            artifact_paths: Directory path containing dataset-metadata.json.
            destination: Kaggle dataset ID (e.g. "username/dataset-name").
                Must match ``id`` in directory's ``dataset-metadata.json``.
            public: Whether new dataset is public. For safety, default is private;
                pass explicit ``True`` to make public intentionally (#177).
        """
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "kaggle is required for Kaggle publishing. Install it with: pip install kaggle"
            ) from exc

        api = KaggleApi()
        # Kaggle SDK reads credentials from env vars only. If passed per-requester credential,
        # place in environment only for this call and restore original — if SDK doesn't use
        # credential arg, all publishing goes through server account (#635).
        with _kaggle_environment(credentials):
            # Convert auth failure exception to PublishError to prevent
            # CLI raw traceback exposure (#178).
            try:
                api.authenticate()
            except Exception as exc:
                raise PublishError(f"Kaggle authentication failed: {exc}") from exc

        count = 0
        for path in artifact_paths:
            if not path.is_dir():
                raise PublishError(
                    f"KagglePublisher expects a directory with dataset-metadata.json, "
                    f"got file: {path}"
                )

            # Validate dataset-metadata.json before upload, verify actual
            # upload target (metadata id)
            # matches destination. Kaggle API determines target by metadata id,
            # so mismatch can create/update
            # wrong dataset (#177).
            metadata_path = path / "dataset-metadata.json"
            if not metadata_path.is_file():
                raise PublishError(f"KagglePublisher requires dataset-metadata.json in {path}")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PublishError(
                    f"Failed to read dataset-metadata.json in {path}: {exc}"
                ) from exc
            metadata_id = metadata.get("id") if isinstance(metadata, dict) else None
            if metadata_id != destination:
                raise PublishError(
                    f"dataset-metadata.json id {metadata_id!r} does not match "
                    f"destination {destination!r}; they must be identical so the upload "
                    "targets the intended dataset"
                )

            # Swallowing dataset_list failure risks unintended creation of
            # new (public) dataset on network error,
            # so propagate as PublishError (#177).
            try:
                results = api.dataset_list(mine=True, search=destination.split("/")[-1])
            except Exception as exc:
                raise PublishError(
                    f"Failed to query existing Kaggle datasets for {destination}: {exc}"
                ) from exc
            dataset_exists = any(str(d) == destination for d in results)

            try:
                if dataset_exists:
                    api.dataset_create_version(
                        str(path),
                        version_notes="Update via kpubdata-builder",
                        dir_mode="zip",
                    )
                else:
                    api.dataset_create_new(folder=str(path), dir_mode="zip", public=public)
            except Exception as exc:
                raise PublishError(
                    f"Failed to publish Kaggle dataset to {destination}: {exc}"
                ) from exc
            count += 1

        return PublishResult(
            publisher=self.name,
            reference=f"https://www.kaggle.com/datasets/{destination}",
            artifact_count=count,
        )
