"""HTTP side of the revision store (#820): save, read, history, revert."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast

from ..spec import JsonValue
from . import ownership
from .auth import Principal
from .responses import ServiceResponse
from .revisions import (
    REVISION_KINDS,
    CredentialInContent,
    RevisionConflict,
    RevisionStore,
)

_SAVE_FIELDS = {"content", "expected_revision", "note", "idempotency_key"}
_REVERT_FIELDS = {"to_revision", "expected_revision"}
_MAX_DOC_ID = 200


def _error(status: int, code: str, message: str, **extra: JsonValue) -> ServiceResponse:
    return ServiceResponse(status, {"error": message, "code": code, **extra})


def _bad(message: str) -> ServiceResponse:
    return _error(400, "invalid_request", message)


def _check_target(kind: str, doc_id: str) -> ServiceResponse | None:
    if kind not in REVISION_KINDS:
        return _bad(f"kind must be one of {', '.join(REVISION_KINDS)}")
    if not doc_id or len(doc_id) > _MAX_DOC_ID or "/" in doc_id:
        return _bad(f"doc_id must be one path segment of at most {_MAX_DOC_ID} characters")
    return None


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


class RevisionsApiService:
    def __init__(self, *, store: Callable[[], RevisionStore]) -> None:
        self._store = store

    @staticmethod
    def _scope(principal: Principal) -> tuple[str, str]:
        workspace = ownership.warehouse_workspace(principal.owner_id)
        return workspace, principal.owner_id or principal.label

    def save(
        self, kind: str, doc_id: str, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        refusal = _check_target(kind, doc_id)
        if refusal is not None:
            return refusal
        try:
            if body is None or not set(body).issubset(_SAVE_FIELDS) or "content" not in body:
                raise ValueError(f"body takes {sorted(_SAVE_FIELDS)} and needs content")
            expected = _non_negative_int(body.get("expected_revision"), "expected_revision")
            note, key = body.get("note"), body.get("idempotency_key")
            if note is not None and not isinstance(note, str):
                raise ValueError("note must be a string")
            if key is not None and (not isinstance(key, str) or not key or len(key) > 200):
                raise ValueError("idempotency_key must be a string of 1 to 200 characters")
            content = body["content"]
            if kind == "spec" and not (
                isinstance(content, Mapping) and isinstance(content.get("yaml"), str)
            ):
                raise ValueError('a spec\'s content is {"yaml": <BuildSpec YAML text>}')
        except ValueError as exc:
            return _bad(str(exc))
        workspace, author = self._scope(principal)
        try:
            revision = self._store().save(
                workspace,
                kind,
                doc_id,
                content,
                expected_revision=expected,
                author=author,
                note=note,
                idempotency_key=key,
            )
        except RevisionConflict as exc:
            return _error(409, "revision_conflict", str(exc), current_revision=exc.current)
        except CredentialInContent as exc:
            return _error(400, "credential_in_content", str(exc))
        except ValueError as exc:
            return _bad(str(exc))
        return ServiceResponse(200, revision.body())

    def get(
        self, kind: str, doc_id: str, revision: int | None, *, principal: Principal
    ) -> ServiceResponse:
        refusal = _check_target(kind, doc_id)
        if refusal is not None:
            return refusal
        workspace, _ = self._scope(principal)
        found = self._store().get(workspace, kind, doc_id, revision)
        if found is None:
            return _error(404, "revision_not_found", f"no such {kind}: {doc_id}")
        return ServiceResponse(200, found.body())

    def history(self, kind: str, doc_id: str, *, principal: Principal) -> ServiceResponse:
        refusal = _check_target(kind, doc_id)
        if refusal is not None:
            return refusal
        workspace, _ = self._scope(principal)
        store = self._store()
        revisions = store.history(workspace, kind, doc_id)
        if not revisions:
            return _error(404, "revision_not_found", f"no such {kind}: {doc_id}")
        return ServiceResponse(
            200,
            {
                "revisions": [cast(JsonValue, r.body(with_content=False)) for r in revisions],
                "audit": cast(JsonValue, store.audit(workspace, kind, doc_id)),
            },
        )

    def revert(
        self, kind: str, doc_id: str, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        refusal = _check_target(kind, doc_id)
        if refusal is not None:
            return refusal
        try:
            if body is None or set(body) != _REVERT_FIELDS:
                raise ValueError(f"body takes exactly {sorted(_REVERT_FIELDS)}")
            target = _non_negative_int(body.get("to_revision"), "to_revision")
            expected = _non_negative_int(body.get("expected_revision"), "expected_revision")
        except ValueError as exc:
            return _bad(str(exc))
        workspace, author = self._scope(principal)
        store = self._store()
        old = store.get(workspace, kind, doc_id, target) if target else None
        if old is None:
            return _error(404, "revision_not_found", f"{kind} {doc_id} has no revision {target}")
        try:
            revision = store.save(
                workspace,
                kind,
                doc_id,
                old.content,
                expected_revision=expected,
                author=author,
                note=f"revert to revision {target}",
                reverted_from=target,
            )
        except RevisionConflict as exc:
            return _error(409, "revision_conflict", str(exc), current_revision=exc.current)
        return ServiceResponse(200, revision.body())


__all__ = ["RevisionsApiService"]
