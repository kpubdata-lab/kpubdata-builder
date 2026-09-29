"""Single canonical gate for run/dataset ownership determination (#389/#504/#505).

All ownership consumers (``service.app`` / ``service.datasets`` / ``query.resolver`` etc.)
share this module — we do not duplicate comparison logic in each endpoint (#504 review).

Gating policy:
    - ``ENFORCE_OWNERSHIP`` off → always allow (backward compatible).
    - dev/service principal → allow all runs (#679 onwards. see below).

**OIDC admins do not pass through here.** ``Principal.is_admin`` only opens admin
endpoints (``routes/admin.py``), not other users' run artifacts. Whether admins can see
user data is still undecided (#679) — this product is BYOK, and the promise is "data
received with your key belongs to you". We do not broaden authority with new principal
kinds before a decision.

Full access for dev/service has existed since #679, so we keep it. Removing it would break
single-user deployments and API-key-based Studio deployments.

Record comparison itself delegates to ``service.auth.principal_owns`` (#505 canonical:
stable ``owner_id`` first, legacy ``created_by``/label fallback, fail-closed) — we keep
comparison logic singular in auth so both implementations do not drift.
"""

from __future__ import annotations

import hashlib
import os

from .auth import Principal, oidc_enabled, principal_owns

_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


def enforce_ownership() -> bool:
    """Check if ownership enforcement is enabled (#389). Default off — backward compatible."""
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


def multi_user_mode() -> bool:
    """Whether this deployment serves more than one user (#684).

    Either switch means another user's requests reach the same process: OIDC lets
    anyone on the allowlist log in, and ``ENFORCE_OWNERSHIP`` exists only because
    runs belong to different people. A single ``X-API-Key`` or dev mode alone is
    one user.
    """
    return oidc_enabled() or enforce_ownership()


PERSONAL_WORKSPACE = "ws_personal"


def warehouse_workspace(owner_id: str | None) -> str:
    """The warehouse workspace a build by ``owner_id`` commits into (#789).

    Tables are unique per (workspace, logical name). With one shared workspace, two
    owners building the same spec committed into the same table, and each refresh
    replaced the other owner's current snapshot. When ownership is enforced, each owner
    gets a workspace of their own, named from a hash of the owner id (it is a path and
    a catalog key, so the id itself does not appear). Otherwise nothing changes: a
    single-user deployment keeps its one personal workspace.
    """
    if owner_id is None or not enforce_ownership():
        return PERSONAL_WORKSPACE
    return "ws_" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]


def _has_grandfathered_full_access(principal: Principal) -> bool:
    """Dev/service principal have unconditional full run access (#679 onwards).

    We do not use ``Principal.is_admin``. Including OIDC admins here would implicitly
    confirm "admins see other users' data" without a decision (#679).
    """
    return principal.kind in ("dev", "service")


def ownership_allows(
    *,
    created_by: str | None,
    owner_id: str | None,
    principal: Principal,
    enforce: bool | None = None,
) -> bool:
    """Determine whether principal can access the record (created_by/owner_id).

    If ``enforce`` is omitted, reads from environment variable (``ENFORCE_OWNERSHIP``).
    Comparison delegates to ``principal_owns`` (#505).
    """
    if enforce is None:
        enforce = enforce_ownership()
    if not enforce:
        return True
    if _has_grandfathered_full_access(principal):
        return True
    return principal_owns(created_by=created_by, owner_id=owner_id, principal=principal)
