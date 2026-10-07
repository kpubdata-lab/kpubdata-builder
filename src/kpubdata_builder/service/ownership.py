"""Single canonical gate for run/dataset ownership determination (#389/#504/#505).

All ownership consumers (``service.app`` / ``service.datasets`` / ``query.resolver`` etc.)
share this module — we do not duplicate comparison logic in each endpoint (#504 review).

Gating policy:
    - ``ENFORCE_OWNERSHIP`` off → always allow (backward compatible).
    - ``dev`` principal → allow all runs. ``service`` (the API key) owns only the runs it
      made (#1072); one run (``ownership_allows``) and every list
      (``lists_only_own_runs``, #1091) ask the same rule.

**OIDC admins do not pass through here.** ``Principal.is_admin`` only opens admin
endpoints (``routes/admin.py``), not other users' run artifacts. Whether admins can see
user data is still undecided (#679) — this product is BYOK, and the promise is "data
received with your key belongs to you". We do not broaden authority with new principal
kinds before a decision.

A single-user deployment does not enforce ownership, so nothing here is asked there and
the API key reads everything as before.

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
    """Whether runs are private to their owner (#389, #635).

    On when ``ENFORCE_OWNERSHIP`` says so, and **always** when OIDC sign-in is
    configured, whatever the variable says: an OIDC deployment serves more than one
    user, and ADR 0012's 2026-09-30 amendment forces ownership on in multi-user mode.
    The default is not flipped — a single-user deployment (no OIDC, variable unset)
    keeps sharing its runs as before, so no transition period is needed.
    """
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1") or oidc_enabled()


def multi_user_mode() -> bool:
    """Whether this deployment serves more than one user (#684).

    Either switch means another user's requests reach the same process: OIDC lets
    anyone on the allowlist log in, and ``ENFORCE_OWNERSHIP`` exists only because
    runs belong to different people. A single ``X-API-Key`` or dev mode alone is
    one user.
    """
    return oidc_enabled() or enforce_ownership()


def hides_foreign_runs() -> bool:
    """Whether another owner's run is answered as missing rather than forbidden (#796).

    ADR 0012's 2026-09-30 amendment: in a multi-user deployment another owner's run is
    not revealed — the answer is the one a run that does not exist gets. A single-user
    deployment keeps 403, as before.
    """
    return multi_user_mode()


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
    """Whether the principal reads every owner's runs where ownership is enforced.

    Only ``dev``: local development with authentication off. ``serve`` refuses that
    together with enforced ownership (#1081), so it exists there only for code that
    builds the service directly — tests.

    ``service`` — the deployment's ``X-API-Key`` — had the same access (#679 onwards).
    ADR 0012's decision of 2026-10-01 is that it is like an administrator: it sees run
    metadata through the administration routes, and another user's run data is not
    there for it (#1072). It still owns the runs it made itself, like any principal.
    Where ownership is not enforced — a single-user deployment — nothing here is asked.

    We do not use ``Principal.is_admin``. Including OIDC admins here would implicitly
    confirm "admins see other users' data" without a decision (#679).
    """
    return principal.kind == "dev"


def lists_only_own_runs(principal: Principal | None, *, enforce: bool | None = None) -> bool:
    """Whether a list answered to ``principal`` keeps only the runs it owns (#1091).

    The lists — ``GET /builds``, the dataset views, monitoring — ask this, so they follow
    the rule ``ownership_allows`` applies to one run: where ownership is enforced only
    ``dev`` sees every owner's runs. The ``service`` principal (``X-API-Key``) used to
    pass through here as well, because each list tested for an ``oidc`` principal rather
    than asking this module; every run's metadata was in its lists although ADR 0012
    (2026-10-01) gives it only its own runs, and the administration routes for the rest.

    ``principal=None`` — a call from inside the process, not a request — is not filtered.
    """
    if enforce is None:
        enforce = enforce_ownership()
    return enforce and principal is not None and not _has_grandfathered_full_access(principal)


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
