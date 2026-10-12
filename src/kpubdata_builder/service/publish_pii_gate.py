"""A hosted publish refuses a run whose personal-data scan is not known to be clean (#1113).

In a multi-user (hosted) deployment a user can build from data that holds other
people's personal information, and publish the result to Hugging Face, Kaggle or the
server's local publish root. The user's own consent does not cover a third party's data
(#1113). Which inputs count as personal data, and what the law allows, are decisions
that have not been made (kpubdata#836, kpubdata#846, kpubdata's
``docs/compliance/data-flows.md``). Until they are, this gate refuses by default: a run
is published only when a check says its scan passed.

The check is pluggable (``PiiScanCheck``). The default, ``default_pii_scan_check``,
reads the one scan Builder has: the BuildSpec's ``pii`` policy, which scans Silver's
values during the build (``stages/silver/pii.py``, #441). Its outcome is not recorded
in the manifest, so the default reads it from what the build implies:

- ``pending`` — the run has not finished, so the scan has not either.
- ``failed`` — the run failed or was cancelled; a ``pii.mode: block`` finding fails it.
- ``missing`` — the BuildSpec has no ``pii`` policy, so nothing was scanned.
- ``unverified`` — the scan ran but did not stop on a finding (``warn`` or ``allow``),
  or ``allow_columns`` accepted some columns despite it: nothing shows the values clean.
- ``passed`` — the run succeeded with ``pii.mode: block`` and no accepted columns.

A pass means only that the scan found nothing it looks for. The scan does not find
every kind of personal information, and this gate does not claim it does.

The gate is on whenever the deployment serves more than one user (OIDC or
``ENFORCE_OWNERSHIP``), whatever ``KPUBDATA_BUILDER_REQUIRE_PII_SCAN_FOR_PUBLISH`` says.
A single-user deployment leaves it off unless that variable is ``true``: the person
publishing is the person who built, and their local SDK use is not changed (#1113).
"""

# The input scope and the legal basis are owner and legal decisions, not this module's:
# https://github.com/kpubdata-lab/kpubdata/issues/836,
# https://github.com/kpubdata-lab/kpubdata/issues/846,
# https://github.com/kpubdata-lab/kpubdata/blob/main/docs/compliance/data-flows.md.
# When they are made, replace the check; until then the gate refuses.

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Literal

from ..spec import BuildSpec
from .ownership import multi_user_mode

__all__ = [
    "PII_GATE_ENV",
    "PII_SCAN_ISSUE_CODES",
    "PiiScanCheck",
    "PiiScanState",
    "default_pii_scan_check",
    "pii_gate_required",
    "pii_scan_state",
]

logger = logging.getLogger(__name__)

#: Turns the gate on in a single-user deployment. A multi-user one has it on regardless.
PII_GATE_ENV = "KPUBDATA_BUILDER_REQUIRE_PII_SCAN_FOR_PUBLISH"

PiiScanState = Literal["passed", "missing", "pending", "failed", "unverified"]

#: ``(run status, the run's BuildSpec or None) -> state``. ``RunStatus`` values are the
#: publish vocabulary (``queued``/``running``/``cancelling``/``succeeded``/``failed``/
#: ``cancelled``).
PiiScanCheck = Callable[[str, BuildSpec | None], PiiScanState]

#: The blocker codes the gate reports, one per state other than ``passed``.
PII_SCAN_ISSUE_CODES: tuple[str, ...] = (
    "pii_scan_missing",
    "pii_scan_pending",
    "pii_scan_failed",
    "pii_scan_unverified",
)

_STATES: frozenset[str] = frozenset({"passed", "missing", "pending", "failed", "unverified"})


def pii_gate_required() -> bool:
    """Whether a publish must pass the personal-data scan gate."""
    if multi_user_mode():
        return True
    return os.environ.get(PII_GATE_ENV, "").lower() in ("true", "1")


def default_pii_scan_check(status: str, spec: BuildSpec | None) -> PiiScanState:
    """The scan's state, read from the run's status and its BuildSpec's ``pii`` policy."""
    if status in ("queued", "running", "cancelling"):
        return "pending"
    if status != "succeeded":
        return "failed"
    if spec is None or spec.pii is None:
        return "missing"
    if spec.pii.mode != "block" or spec.pii.allow_columns:
        return "unverified"
    return "passed"


def pii_scan_state(check: PiiScanCheck, status: str, spec: BuildSpec | None) -> PiiScanState:
    """Ask ``check``, failing closed: an error or an unknown answer counts as ``failed``."""
    try:
        state = check(status, spec)
    except Exception as exc:
        logger.error("personal-data scan check failed: error_type=%s", type(exc).__name__)
        return "failed"
    if state not in _STATES:
        logger.error("personal-data scan check returned an unknown state")
        return "failed"
    return state
