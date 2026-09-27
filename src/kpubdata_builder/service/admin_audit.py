"""Admin action audit logging (#679).

Admins can view resources that aren't theirs. Therefore, **what they did must be
recorded** — without records, admin privileges become unaccountable powers with
no post-action verification.

The work done here is just a single-line record. We don't maintain a separate
repository — where and how long to keep records is the deployment's decision.
By exporting via standard logging, that decision is delegated to the
deployment's log collection config.

**Never put credentials in records.** This module only receives actor, action,
and target. The signature enforces receiving names instead of values.
"""

from __future__ import annotations

import logging
import threading

from .auth import Principal

__all__ = ["record_admin_action"]

#: Audit-only logger. Deployment can collect/retain this separately by isolating
#: its name.
_audit_logger = logging.getLogger("kpubdata_builder.admin_audit")

# This service has no logging config (zero ``basicConfig``/``dictConfig`` calls).
# So the root logger's default WARNING threshold applies, and audit INFO records
# **don't go out at all.** Verified in practice.
#
# Audit records vanishing silently is worse than no audit records — it creates
# the false impression that they exist. So this logger sets its own threshold
# and outputs to stderr via a fallback handler only when deployment hasn't
# attached any handler.
#
# If deployment has attached its own handler, don't touch it. That's the entity
# that knows collection and retention policy.
_audit_logger.setLevel(logging.INFO)


#: Fallback handler attached directly by this module. If already attached once,
#: don't attach again — multiple attachments cause the same audit record to be
#: logged multiple times, making counts impossible to verify.
_fallback_handler: logging.Handler | None = None

#: Decision is made at first record, so two concurrent requests could arrive.
#: Without locking, both pass the check, leading to two handlers and duplicate
#: records.
_output_lock = threading.Lock()


def _ensure_audit_output() -> None:
    """Attach stderr handler only when no handler exists anywhere in the chain.

    **Decision is made at first record time, not import time.** Import can occur
    before deployment configures logging. If we attach early, we'll duplicate
    records later when deployment attaches its root handler.
    """
    global _fallback_handler
    if _fallback_handler is not None:
        return
    with _output_lock:
        if _fallback_handler is not None:
            return
        logger: logging.Logger | None = _audit_logger
        while logger is not None:
            if logger.handlers:
                return
            if not logger.propagate:
                break
            logger = logger.parent
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        _audit_logger.addHandler(handler)
        _fallback_handler = handler


def record_admin_action(
    principal: Principal,
    action: str,
    *,
    target: str | None = None,
    outcome: str = "allowed",
) -> None:
    """Log a single line recording an admin action.

    From ``principal``, we only extract ``label`` and ``owner_id`` — both are
    designed not to hold secrets (see ``Principal`` docstring).

    ``target`` is the resource **identifier**, not the resource content.
    """
    _ensure_audit_output()
    _audit_logger.info(
        "admin action: actor=%s owner_id=%s action=%s target=%s outcome=%s",
        principal.label,
        principal.owner_id,
        action,
        target if target is not None else "-",
        outcome,
    )
