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

import os

from .auth import Principal, principal_owns

_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


def enforce_ownership() -> bool:
    """Check if ownership enforcement is enabled (#389). Default off — backward compatible."""
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


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
