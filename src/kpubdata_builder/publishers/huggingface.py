"""Hugging Face Hub publisher — uploads artifacts to HF dataset repository."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import PublishError
from .base import BasePublisher, PublishResult


def _hub_error_names(exc: BaseException) -> set[str]:
    """Class names in ``exc``'s MRO.

    huggingface_hub moved its error classes between modules across releases
    (``huggingface_hub.utils`` to ``huggingface_hub.errors``), so they are matched by
    name, as ``_probe_remote_publish_target`` does, rather than imported.
    """
    return {cls.__name__ for cls in type(exc).__mro__}


def _repo_visibility(api: Any, destination: str) -> str:
    """The dataset repo's visibility: ``"public"``, ``"private"`` or ``"absent"``.

    Raises ``PublishError`` when it cannot be known: a lookup that failed for any
    reason other than not found (network, auth, gated), or a repo that does not
    report it. What is unknown is not permission (#688).
    """
    try:
        info = api.repo_info(repo_id=destination, repo_type="dataset")
    except Exception as exc:
        names = _hub_error_names(exc)
        # GatedRepoError subclasses RepositoryNotFoundError, but a gated repo exists,
        # so it is not "absent".
        if "RepositoryNotFoundError" in names and "GatedRepoError" not in names:
            return "absent"
        raise PublishError(
            f"refusing private publish to {destination}: could not look up the "
            f"repository's visibility ({type(exc).__name__})"
        ) from exc
    visibility = getattr(info, "private", None)
    if visibility is True:
        return "private"
    if visibility is False:
        return "public"
    raise PublishError(
        f"refusing private publish to {destination}: the repository's visibility "
        "could not be determined"
    )


def _require_private_target(api: Any, destination: str) -> None:
    """Refuse a private publish unless the target repo is private or does not exist.

    ``create_repo(exist_ok=True)`` never changes an existing repo's visibility, so the
    ``private`` option alone does not say where the data lands: an existing public
    repo would receive it (#901). The actual visibility is looked up here, before
    anything is created or uploaded (:func:`_repo_visibility`); not found means the
    caller creates it private.
    """
    if _repo_visibility(api, destination) == "public":
        raise PublishError(
            f"refusing private publish: Hugging Face dataset {destination} already "
            "exists and is public. Make it private first, or publish to another repository."
        )


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


_COMMIT_MESSAGE = "Update dataset via kpubdata-builder"
#: Prefix of the commit-description line that names the artifact set a commit holds
#: (#1330). A retry finds it on the remote head and does not commit the set twice.
_SET_MARKER = "kpubdata-builder-artifact-set: "


def _is_ignored(relative: Path) -> bool:
    """Files ``upload_folder`` never uploaded: anything under ``.git`` or
    ``.cache/huggingface``. A directory artifact keeps excluding them (#1330)."""
    parts = relative.parts
    if ".git" in parts:
        return True
    return any(
        parts[i] == ".cache" and parts[i + 1] == "huggingface" for i in range(len(parts) - 1)
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_digest(files: Mapping[str, Path]) -> str:
    """One digest for the whole artifact set: every target path and its content.

    Deletions are left out on purpose: once a commit has removed stale files, a
    retry of the same set finds nothing left to delete, and must still recognise
    the set as published.
    """
    digest = hashlib.sha256()
    for repo_path in sorted(files):
        digest.update(f"{repo_path}\0{_file_sha256(files[repo_path])}\n".encode())
    return digest.hexdigest()


def _head(api: Any, destination: str) -> tuple[str, str]:
    """The dataset repo's head commit: its id and its full message."""
    commits = api.list_repo_commits(repo_id=destination, repo_type="dataset")
    if not commits:
        raise PublishError(f"could not read the head revision of {destination}")
    head = commits[0]
    return str(head.commit_id), f"{head.title or ''}\n{head.message or ''}"


def _holds_set(message: str, set_digest: str) -> bool:
    return f"{_SET_MARKER}{set_digest}" in message.splitlines()


class HuggingFacePublisher(BasePublisher):
    """Upload artifact files to Hugging Face Hub dataset repository."""

    @property
    def name(self) -> str:
        return "huggingface"

    def destination_visibility(
        self, destination: str, *, credentials: Mapping[str, str] | None = None
    ) -> str:
        """The dataset repo's visibility, read with the credentials the publish would use.

        The same lookup :meth:`publish` makes before a private publish (#901), so the
        service can refuse with a reason before anything is claimed (#688).
        """
        from huggingface_hub import HfApi

        token: str | bool | None = (
            os.environ.get("HF_TOKEN") if credentials is None else credentials.get("HF_TOKEN")
        )
        if credentials is not None and not token:
            # A caller that passed credentials without a token gets an anonymous lookup.
            # ``token=None`` would let huggingface_hub pick up the server's HF_TOKEN or
            # cached login, which is the operator's account (#925); ``False`` forbids it.
            token = False
        return _repo_visibility(HfApi(token=token), destination)

    def publish(
        self,
        artifact_paths: tuple[Path, ...],
        *,
        destination: str,
        private: bool = True,
        credentials: Mapping[str, str] | None = None,
        delete_patterns: tuple[str, ...] = (),
    ) -> PublishResult:
        """Publish artifact to HuggingFace dataset repository.

        The whole artifact set goes up in one Hub commit (#1330): every file, and
        every deletion, lands together or not at all, so readers see the previous
        revision or the new one and never a mix. The commit names the repo's head
        as its parent, so a commit someone else made in between makes it fail
        instead of being silently mixed with it. Its description carries a digest
        of the set; when the head already carries that digest — a retry after a
        commit whose answer was lost — nothing is committed again.

        Args:
            artifact_paths: List of files or directories to upload.
            destination: HF repository ID (e.g., "kpubdata/air-quality").
            private: Requested visibility. A new repo is created with it. An existing
                repo's visibility is never changed (``exist_ok=True``), so when
                ``private`` is true the repo's actual visibility is looked up first and
                the publish is refused, before anything is uploaded, if the repo is
                public or its visibility cannot be confirmed (#901). A public publish
                (``private=False``) to an existing private repo is not checked: it
                exposes nothing.
            delete_patterns: Remote paths (``fnmatch`` patterns) the new set
                replaces. Matching files the set does not contain are deleted in the
                same commit. Empty by default: nothing is deleted unless asked.

        Raises:
            PublishError: A private publish targets a public or unconfirmable repo.
        """
        try:
            from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi
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
        if private:
            _require_private_target(api, destination)
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

        files: dict[str, Path] = {}

        def _add(repo_path: str, path: Path) -> None:
            # If two artifacts map to same repo path, one gets buried, so fail explicitly.
            prior = files.get(repo_path)
            if prior is not None and prior != path:
                raise PublishError(
                    f"duplicate artifact target path {repo_path!r}: "
                    f"{prior} and {path} would overwrite each other in {destination}"
                )
            files[repo_path] = path

        for path in artifact_paths:
            if path.is_dir():
                # A directory lands at the repo root, as upload_folder put it.
                for child in sorted(path.rglob("*")):
                    relative = child.relative_to(path)
                    if child.is_file() and not _is_ignored(relative):
                        _add(relative.as_posix(), child)
            else:
                _add(_repo_path_for(path, common_root), path)
            count += 1

        if files:
            parent, head_message = _head(api, destination)
            deletions: list[str] = []
            if delete_patterns:
                remote = api.list_repo_files(
                    repo_id=destination, repo_type="dataset", revision=parent
                )
                deletions = sorted(
                    remote_path
                    for remote_path in remote
                    if remote_path not in files
                    and any(fnmatch.fnmatchcase(remote_path, p) for p in delete_patterns)
                )
            set_digest = _set_digest(files)
            if not _holds_set(head_message, set_digest):
                operations: list[Any] = [
                    CommitOperationDelete(path_in_repo=remote_path) for remote_path in deletions
                ]
                operations += [
                    CommitOperationAdd(path_in_repo=repo_path, path_or_fileobj=str(local))
                    for repo_path, local in sorted(files.items())
                ]
                try:
                    api.create_commit(
                        repo_id=destination,
                        repo_type="dataset",
                        operations=operations,
                        commit_message=_COMMIT_MESSAGE,
                        commit_description=f"{_SET_MARKER}{set_digest}",
                        parent_commit=parent,
                    )
                except Exception:
                    # The answer may be lost while the commit landed (a timeout). Read
                    # the head again: the set is there, or the commit did not happen.
                    try:
                        _, head_message = _head(api, destination)
                    except Exception:
                        head_message = ""
                    if not _holds_set(head_message, set_digest):
                        raise

        return PublishResult(
            publisher=self.name,
            reference=f"https://huggingface.co/datasets/{destination}",
            artifact_count=count,
        )
