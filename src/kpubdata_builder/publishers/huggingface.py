"""Hugging Face Hub publisher — uploads artifacts to HF dataset repository."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ..errors import PublishError
from .base import BasePublisher, PublishResult


def _repo_path_for(path: Path, common_root: Path | None) -> str:
    """Map file artifact to repo-relative path preserving layout.

    Use relative path from common root to preserve nesting structure;
    fall back to basename if common root not applicable.
    """
    if common_root is not None:
        try:
            return path.relative_to(common_root).as_posix()
        except ValueError:
            pass
    return path.name


class HuggingFacePublisher(BasePublisher):
    """Upload artifact files to Hugging Face Hub dataset repository."""

    @property
    def name(self) -> str:
        return "huggingface"

    def publish(
        self,
        artifact_paths: tuple[Path, ...],
        *,
        destination: str,
        private: bool = True,
        credentials: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Publish artifact to HuggingFace dataset repository.

        Args:
            artifact_paths: List of files or directories to upload.
            destination: HF repository ID (e.g., "kpubdata/air-quality").
            private: Visibility on new repo creation. Since ``exist_ok=True``, existing
                repo visibility is not changed.
        """
        try:
            from huggingface_hub import HfApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "huggingface_hub is required for HuggingFace publishing. "
                "Install it with: pip install huggingface_hub"
            ) from exc

        # Only ``credentials=None`` means "caller did not specify" — like CLI where
        # execution context and server environment share same path. If mapping received,
        # only values in it are used. Previously, empty mapping also went env var,
        # When result, kwarg itself omitted, so even with enforce-requester-creds config on,
        # publishing went through with server token (#635).
        token = os.environ.get("HF_TOKEN") if credentials is None else credentials.get("HF_TOKEN")
        if not token:
            raise RuntimeError(
                "No Hugging Face API token is available. Store one for this "
                "principal, or set HF_TOKEN on the server."
            )

        api = HfApi(token=token)
        # First ensure new dataset repo exists. For existing repo with exist_ok=True,
        # no visibility mutation or update_repo_settings call (#491).
        api.create_repo(
            repo_id=destination,
            repo_type="dataset",
            private=private,
            exist_ok=True,
        )
        count = 0

        # Determine repo path based on common parent directory of file artifacts.
        # Flattening to bare filename loses nested directory layout, same-named files in
        # different dirs silently overwrite (#170).
        file_parents = [str(p.parent) for p in artifact_paths if not p.is_dir()]
        common_root: Path | None
        try:
            common_root = Path(os.path.commonpath(file_parents)) if file_parents else None
        except ValueError:
            # Fallback to basename if common path cannot be computed
            # (mixed absolute/relative paths etc.).
            common_root = None
        # If common root is filesystem root ("/"), means no meaningful common parent.
        # Using as-is would leak unrelated absolute paths as host paths like tmp/..., var/...,
        # so treat as "no common root" and fallback to basename (#205).
        # Without is_absolute() guard, Path(".") as commonpath of
        # relative paths also hits parent==self
        # condition, causing recursion flattening to basename (must preserve relative path layout).
        if (
            common_root is not None
            and common_root.is_absolute()
            and common_root.parent == common_root
        ):
            common_root = None

        seen_repo_paths: dict[str, Path] = {}
        for path in artifact_paths:
            if path.is_dir():
                api.upload_folder(
                    folder_path=str(path),
                    repo_id=destination,
                    repo_type="dataset",
                    commit_message="Update dataset via kpubdata-builder",
                )
            else:
                repo_path = _repo_path_for(path, common_root)
                # If two artifacts map to same repo path, one gets buried, so fail explicitly.
                prior = seen_repo_paths.get(repo_path)
                if prior is not None and prior != path:
                    raise PublishError(
                        f"duplicate artifact target path {repo_path!r}: "
                        f"{prior} and {path} would overwrite each other in {destination}"
                    )
                seen_repo_paths[repo_path] = path
                api.upload_file(
                    path_or_fileobj=str(path),
                    path_in_repo=repo_path,
                    repo_id=destination,
                    repo_type="dataset",
                    commit_message="Update dataset via kpubdata-builder",
                )
            count += 1

        return PublishResult(
            publisher=self.name,
            reference=f"https://huggingface.co/datasets/{destination}",
            artifact_count=count,
        )
