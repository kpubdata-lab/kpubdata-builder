"""Live dataset verification runner."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .models import CheckName, CheckResult, DatasetStatus, VerifyResult
from .schema_hash import schema_hash

if TYPE_CHECKING:
    from kpubdata.core.spec import SpecDefinition


#: HEAD responses that prove the endpoint is live even though the probe failed.
#: 400/405 mean the method or request shape was rejected; 401/403 mean the route
#: exists but requires credentials the probe deliberately does not send.
_ENDPOINT_LIVE_STATUSES = frozenset({400, 401, 403, 405})

#: Statuses that mean authentication itself did not succeed.
_AUTH_FAILURE_STATUSES = frozenset(
    {
        DatasetStatus.NEEDS_APPLICATION,
        DatasetStatus.WAITING_APPROVAL,
        DatasetStatus.INVALID_KEY,
        DatasetStatus.RATE_LIMITED,
    }
)


def _check_endpoint(spec: SpecDefinition) -> CheckResult:
    """Check that the endpoint URL is well-formed and reachable."""
    import urllib.error
    import urllib.request

    url = spec.endpoint.base_url
    try:
        req = urllib.request.Request(url, method="HEAD")
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            latency = (time.monotonic() - t0) * 1000
        return CheckResult(CheckName.ENDPOINT, passed=True, latency_ms=latency)
    except urllib.error.HTTPError as exc:
        # Many public APIs reject HEAD but accept GET, and an auth-protected
        # endpoint answers an unauthenticated probe with 401/403. Those replies
        # still prove the endpoint is live, so let the authenticated fetch below
        # decide the real status.
        #
        # 404 is deliberately NOT in this set: "route not found" is exactly the
        # symptom of a removed or mistyped base URL, and treating it as reachable
        # would print "Endpoint pass" for a dead endpoint and mislabel the
        # failure. 5xx is excluded for the same reason — a server that cannot
        # answer is not a verified endpoint.
        if exc.code in _ENDPOINT_LIVE_STATUSES:
            return CheckResult(
                CheckName.ENDPOINT,
                passed=True,
                detail=f"endpoint reachable (HEAD rejected with {exc.code})",
            )
        return CheckResult(
            CheckName.ENDPOINT,
            passed=False,
            detail=f"unreachable: HTTP {exc.code}",
        )
    except Exception as exc:
        return CheckResult(
            CheckName.ENDPOINT,
            passed=False,
            detail=f"unreachable: {str(exc)[:120]}",
        )


def _classify_auth_error(exc: Exception) -> DatasetStatus:
    """Map an authentication exception to a dataset status."""
    from kpubdata.exceptions import AuthError, RateLimitError

    if isinstance(exc, RateLimitError):
        return DatasetStatus.RATE_LIMITED
    if isinstance(exc, AuthError):
        msg = str(exc).lower()
        # Message text first: it is the only thing that distinguishes "key is
        # valid but this dataset needs a separate application" from "key is
        # rejected", and both arrive as 403.
        if "활용신청" in msg or "not activated" in msg or "application" in msg:
            return DatasetStatus.NEEDS_APPLICATION
        if "승인" in msg or "approval" in msg or "waiting" in msg:
            return DatasetStatus.WAITING_APPROVAL
        # Then the structured status the exception carries. Relying on free-form
        # text alone classified every plain AuthError(403) as INVALID_KEY, which
        # defeated the documented 403 -> NEEDS_APPLICATION status whenever the
        # provider's wording did not match one of the phrases above.
        if getattr(exc, "status_code", None) == 403:
            return DatasetStatus.NEEDS_APPLICATION
        return DatasetStatus.INVALID_KEY
    return DatasetStatus.BROKEN_ENDPOINT


def verify_dataset(
    spec: SpecDefinition,
    *,
    api_key: str | None = None,
    previous_hash: str | None = None,
    page_size: int = 10,
) -> VerifyResult:
    """Run all verification checks against a live API.

    Parameters:
        spec: The dataset spec to verify.
        api_key: API key override. If None, reads from environment.
        previous_hash: Previous schema hash for drift detection.
        page_size: Number of records to request for testing.

    Returns:
        VerifyResult with all check outcomes and final status.
    """
    # Below eight are NOT kpubdata public API (not in ``kpubdata.__all__``).
    # coupling from verify re-implementing kpubdata's ``make verify``, and
    # kpubdata can move these in minor release without breaking change.
    # ``tests/unit/test_kpubdata_internal_surface.py`` pins this list,
    # so upgrade breaks in CI, not at runtime. When starting to use new internal symbols,
    # add them to that list too.
    from kpubdata import KPubDataConfig
    from kpubdata.core.executor import (
        SpecExecutor,
        check_payload_error,
        extract_items,
        extract_total_count,
    )
    from kpubdata.core.models import Query
    from kpubdata.core.spec import ExampleSpec
    from kpubdata.exceptions import (
        AuthError,
        PublicDataError,
        RateLimitError,
        TransportError,
    )
    from kpubdata.transport.http import HttpTransport

    result = VerifyResult(dataset_id=spec.id, status=DatasetStatus.HEALTHY)
    total_t0 = time.monotonic()

    # 1. Endpoint check
    endpoint_check = _check_endpoint(spec)
    result.checks.append(endpoint_check)
    if not endpoint_check.passed:
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = endpoint_check.detail
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.ENDPOINT)
        return result

    # 2-4. Auth + Response + Parser — done via a live API call
    config = KPubDataConfig.from_env()
    if api_key:
        config = KPubDataConfig(
            provider_keys={spec.auth.provider_key or spec.provider: api_key},
            timeout=config.timeout,
            max_retries=1,
        )

    # Pick example params or use defaults
    example = spec.examples[0] if spec.examples else ExampleSpec(name="default")
    query = Query(
        filters=dict(example.params) if hasattr(example, "params") and example.params else {},
        page=example.page or 1,
        page_size=page_size,
    )

    # HttpTransport owns a persistent HTTP client. Left to the garbage
    # collector, `--all` and repeated library calls pile up sockets and
    # connection-pool entries, so close it on every path out of the fetch.
    transport = HttpTransport()
    try:
        t0 = time.monotonic()
        executor = SpecExecutor(transport, config)
        _params, payload = executor.fetch(spec, query, format_hint=getattr(example, "format", None))
        fetch_latency = (time.monotonic() - t0) * 1000
    except (AuthError, RateLimitError) as exc:
        result.checks.append(
            CheckResult(
                CheckName.AUTH,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = _classify_auth_error(exc)
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.AUTH)
        return result
    except TransportError as exc:
        result.checks.append(
            CheckResult(
                CheckName.AUTH,
                passed=True,
                detail="transport reached",
            )
        )
        result.checks.append(
            CheckResult(
                CheckName.RESPONSE,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.RESPONSE)
        return result
    except PublicDataError as exc:
        result.checks.append(
            CheckResult(
                CheckName.AUTH,
                passed=True,
            )
        )
        result.checks.append(
            CheckResult(
                CheckName.RESPONSE,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.RESPONSE)
        return result

    finally:
        _close_transport(transport)

    # Auth passed (we got a response)
    result.checks.append(
        CheckResult(
            CheckName.AUTH,
            passed=True,
            latency_ms=fetch_latency,
        )
    )

    # 3. Response — check envelope error codes
    try:
        check_payload_error(spec, payload)
    except PublicDataError as exc:
        result.checks.append(
            CheckResult(
                CheckName.RESPONSE,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        auth_status = _classify_auth_error(exc)
        result.status = auth_status
        # A successful HTTP response can still carry an authentication failure in
        # its envelope. The AUTH check was recorded as passed above, so without
        # this the report would read "Auth pass" next to NEEDS_APPLICATION —
        # contradicting its own six-step result.
        if auth_status in _AUTH_FAILURE_STATUSES:
            _mark_failed(result, CheckName.AUTH, detail=str(exc)[:120])
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.RESPONSE)
        return result

    result.checks.append(CheckResult(CheckName.RESPONSE, passed=True))

    # 4. Parser — extract items
    try:
        items = extract_items(spec, payload)
        total_count = extract_total_count(spec, payload)
    except Exception as exc:  # noqa: BLE001
        result.checks.append(
            CheckResult(
                CheckName.PARSER,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.PARSER)
        return result

    result.checks.append(
        CheckResult(
            CheckName.PARSER,
            passed=True,
            detail=f"{len(items)} items, total={total_count}",
        )
    )
    result.records_tested = len(items)

    # 5. Pagination — verify total_count is plausible
    pagination_ok = True
    pagination_detail = ""
    if spec.pagination.type == "none":
        pagination_detail = "no pagination (spec: none)"
    elif total_count is not None:
        pagination_detail = f"total_count={total_count}"
        if total_count < 0:
            pagination_ok = False
            pagination_detail = f"invalid total_count={total_count}"
        elif total_count < len(items):
            # A page that returned more items than the advertised total is an
            # inconsistent response, not a healthy one.
            pagination_ok = False
            pagination_detail = (
                f"total_count={total_count} is below the {len(items)} items returned"
            )
    elif items:
        pagination_detail = f"no total_count, got {len(items)} items"
    else:
        pagination_detail = "empty response, pagination untestable"

    result.checks.append(
        CheckResult(
            CheckName.PAGINATION,
            passed=pagination_ok,
            detail=pagination_detail,
        )
    )
    if not pagination_ok:
        # `passed` and the CLI exit code read only `status`, so leaving it
        # HEALTHY reported a malformed response as healthy and exited 0.
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = pagination_detail

    # 6. Schema — hash field structure and compare
    if items:
        current_hash = schema_hash(items)
        result.schema_hash = current_hash
        result.previous_schema_hash = previous_hash

        if previous_hash and previous_hash != current_hash:
            result.checks.append(
                CheckResult(
                    CheckName.SCHEMA,
                    passed=False,
                    detail=f"changed: {previous_hash[:8]}.. -> {current_hash[:8]}..",
                )
            )
            result.status = DatasetStatus.SCHEMA_CHANGED
        else:
            schema_detail = "unchanged" if previous_hash else f"baseline {current_hash[:8]}.."
            result.checks.append(
                CheckResult(
                    CheckName.SCHEMA,
                    passed=True,
                    detail=schema_detail,
                )
            )
    else:
        result.checks.append(
            CheckResult(
                CheckName.SCHEMA,
                passed=True,
                detail="no items to hash",
            )
        )

    result.total_latency_ms = (time.monotonic() - total_t0) * 1000
    return result


def _close_transport(transport: object) -> None:
    """Release the transport's HTTP client if it owns one."""
    close = getattr(transport, "close", None)
    if callable(close):
        close()


def _mark_failed(result: VerifyResult, name: CheckName, *, detail: str) -> None:
    """Turn an already-recorded check into a failure."""
    for check in result.checks:
        if check.name == name:
            check.passed = False
            check.skipped = False
            check.detail = detail
            return


def _fill_skipped(result: VerifyResult, *, after: CheckName) -> None:
    """Mark the checks that never ran after a failure as skipped.

    Skipped is not failed: collapsing the two made one auth failure look like
    every later stage was broken, and left machine consumers unable to tell
    "this stage failed" from "this stage never ran".
    """
    all_checks = list(CheckName)
    seen = {c.name for c in result.checks}
    started = False
    for name in all_checks:
        if name == after:
            started = True
            continue
        if started and name not in seen:
            result.checks.append(CheckResult(name, passed=False, skipped=True, detail="skipped"))


def verify_datasets(
    specs: list[SpecDefinition],
    *,
    previous_hashes: dict[str, str] | None = None,
    page_size: int = 10,
) -> list[VerifyResult]:
    """Verify multiple datasets sequentially.

    Parameters:
        specs: List of spec definitions to verify.
        previous_hashes: Map of dataset_id -> previous schema hash.
        page_size: Records per test request.

    Returns:
        List of VerifyResult for each dataset.
    """
    hashes = previous_hashes or {}
    results: list[VerifyResult] = []
    for spec in specs:
        prev_hash = hashes.get(spec.id)
        result = verify_dataset(spec, previous_hash=prev_hash, page_size=page_size)
        results.append(result)
    return results
