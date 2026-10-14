"""HTTP service authentication (#384 B2, #385 B3, ADR 0006/0009, #505).

Returns ``Principal`` instead of ``bool`` to preserve "who made the request" (B2).
This module unifies two authentication paths (B3, ADR 0009):

- ``X-API-Key`` — service accounts (scheduled workflows, users who can't use Google login).
- ``Authorization: Bearer <OIDC access token>`` — human users (Studio). JWKS offline
  verification. The token is the **access token** the IdP issued for this API
  (kpubdata-studio#722): what is checked is its claims, not its kind — the issuer, an
  ``aud`` that includes ``OIDC_AUDIENCE``, and ``email_verified``. A Keycloak realm puts
  the Builder audience into its access tokens with an audience mapper; a token without
  it is refused. (ADR 0009 wrote "Google ID token", from before Keycloak, ADR 0015.)

When ``OIDC_ISSUER`` is not set, the Bearer path is disabled, with no impact on
existing deployments. When set, ``OIDC_AUDIENCE`` is required and the ``pyjwt``
extra must be installed (fail-closed). By default, anyone who creates an IdP
account can log in — to restrict to specific organizations/individuals, set
``OIDC_ALLOWED_HD``/``OIDC_ALLOWED_SUBJECTS``/``OIDC_ALLOWED_EMAILS`` (optional,
see deploy.md). An entry is compared together with the token's issuer (#1074): with
several issuers configured each entry is ``<issuer>|<value>``, as the administrator
list always was.

Separates ``Principal`` display role from persistent ownership role (#505):

- ``identifier``/``label`` — human-readable display label, used for backward
  compatibility with prior (#388/#389) ``created_by``. For OIDC, only the first
  8 chars of ``sub`` are recorded to minimize log exposure — this field has
  truncation, so it alone doesn't guarantee collision prevention.
- ``owner_id`` — canonical, stable, persistent owner identity computed by
  ``compute_owner_id()``. OIDC hashes the full issuer+subject without truncation
  to prevent collisions. New resource ownership checks should prioritize this
  field (see ``principal_owns()``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from jwt import PyJWK, PyJWKSet

# Server-required API key. Injected via environment variable only (#248).
# Per ADR 0006, operates fail-closed: missing dev-mode and missing API key → deny auth.
_API_KEY_ENV = "KPUBDATA_BUILDER_API_KEY"
_DEV_MODE_ENV = "KPUBDATA_BUILDER_DEV_MODE"

# OIDC configuration (ADR 0009, #385). Bearer path disabled when OIDC_ISSUER not set.
_OIDC_ISSUER_ENV = "OIDC_ISSUER"
_OIDC_AUDIENCE_ENV = "OIDC_AUDIENCE"
_OIDC_JWKS_URL_ENV = "OIDC_JWKS_URL"
_OIDC_JWKS_TTL_ENV = "OIDC_JWKS_TTL"
_DEFAULT_JWKS_TTL_SECONDS = 3600
_DISCOVERY_TIMEOUT_SECONDS = 5
_TOKEN_LEEWAY_SECONDS = 60
_OIDC_ALLOWED_HD_ENV = "OIDC_ALLOWED_HD"
_OIDC_ALLOWED_SUBJECTS_ENV = "OIDC_ALLOWED_SUBJECTS"
_OIDC_ALLOWED_EMAILS_ENV = "OIDC_ALLOWED_EMAILS"

#: List of admin subjects as ``<issuer>|<sub>`` (#679). Comma-separated.
#:
#: **Always include issuer.** OIDC ``sub`` is unique only within its issuer, and
#: ``OIDC_ISSUER`` allows comma-separated multiple issuers. Comparing only ``sub``
#: would match an account from issuer B against an admin entry targeting issuer A.
#: So ``owner_id`` also uses ``compute_owner_id("oidc", issuer, sub)`` to bind both —
#: if admin checking is looser, the identity model splits in two.
#:
#: Admins are **configured only.** If we could add admins via API at runtime, there'd
#: be no way to roll back when a single admin is compromised — config file and restart
#: are the rollback path.
_ADMIN_SUBJECTS_ENV = "KPUBDATA_BUILDER_ADMIN_SUBJECTS"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Authenticated request principal (#384, #505).

    Used in authorization (C1/C2/#505) to determine run ownership. ``kind`` is
    extensible — ``'dev'`` (local development), ``'service'`` (X-API-Key),
    ``'oidc'`` (Bearer, ADR 0009). Identifiers don't hold sensitive values
    (raw API keys, etc.) — only safe labels (``apikey:<name>``) are used in
    manifest created_by, etc.

    ``identifier``/``label`` are for display/legacy compatibility; ``owner_id``
    is the canonical, stable identity for new persistent ownership checks
    (#505). These roles are intentionally separated — even if the display label
    (e.g., future profile name/email) changes, ``owner_id`` must not.
    """

    kind: str
    identifier: str | None = None
    owner_id: str | None = None
    #: Can perform admin actions (#679). Don't infer from ``kind`` — previously,
    #: ``kind in ("dev", "service")`` automatically meant admin, so **the only way
    #: to become admin was to not log in via OIDC**. By carrying the role as a value,
    #: OIDC-authenticated users can also be admins.
    is_admin: bool = False
    #: Whether sign-in alone admits this principal (#785): true for every non-OIDC
    #: principal, and for an OIDC one on an ``OIDC_ALLOWED_*`` list or an admin. An OIDC
    #: principal it is false for is admitted only once the Builder sign-up ledger says
    #: ``approved``.
    admitted: bool = True
    #: What an administrator sees to recognise a sign-up (#785): the verified email,
    #: else the subject prefix. Never used for ownership — that is ``owner_id``.
    display_name: str | None = None

    @property
    def label(self) -> str:
        """Display label for manifest created_by (#388).

        Retained for backward compatibility — used in ownership fallback checks
        for legacy (#505 era) resources (see ``principal_owns()``). For new
        ownership checks, prioritize ``owner_id`` instead.
        """
        return f"{self.kind}:{self.identifier}" if self.identifier else self.kind


def compute_owner_id(kind: str, *material: str) -> str:
    """Compute canonical, stable persistent owner identity (#505).

    Hash input frames all fields (kind and each material part) with 8-byte
    big-endian length prefix and concatenates them — delimiter-based joining
    (``"\\0"``, etc.) is not used because if a delimiter appears in a field value,
    collisions can occur (e.g., issuer="a", sub="b\\0c" vs issuer="a\\0b",
    sub="c"), breaking the concatenation collision prevention guarantee in #505.
    Length prefixes deterministically fix field boundaries with no ambiguity.

    Include ``kind`` in the hash input (if principal kinds differ, same material
    never produces the same owner_id — domain separation).

    Return value is SHA-256 hex digest-based, so original claims (sub/email, etc.)
    cannot be recovered — owner IDs can be logged/stored without exposing raw claims.
    """
    framed = b"".join(_frame_owner_id_field(value) for value in (kind, *material))
    digest = hashlib.sha256(framed).hexdigest()
    return f"{kind}:{digest}"


def _frame_owner_id_field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(8, "big") + raw


def principal_owns(*, created_by: str | None, owner_id: str | None, principal: Principal) -> bool:
    """Canonical single implementation checking if a record is owned by ``principal`` (#505).

    All ownership consumers — ``/query``, ``/builds``, dataset/stage/quality queries,
    etc. — share this function. Don't duplicate comparison logic per endpoint.

    - If both record and principal have stable ``owner_id`` (new path), compare these
      first — safe without OIDC subject truncation collisions.
    - If either lacks ``owner_id`` (legacy record or principal without owner_id),
      fall back to existing ``created_by``/``label`` comparison (#388/#389 after
      backward compatibility — don't make existing resources inaccessible immediately).
    - If both values are missing (e.g., legacy record never recorded created_by),
      comparison always fails — "no owner info = anyone can access" is not fail-closed.
    """
    if owner_id is not None and principal.owner_id is not None:
        return owner_id == principal.owner_id
    return created_by == principal.label


@dataclass(frozen=True)
class AuthError:
    """Authentication failure. ``status_code`` determines dispatch response (#385).

    Default 401 (auth denied). Temporary infrastructure failures like JWKS
    fetches are distinguished as 503, so clients can tell retryable errors apart.
    """

    reason: str
    status_code: int = 401
    # Set where the failure is made, so ``code`` does not depend on how ``reason`` is
    # worded.
    expired: bool = False
    # The token verified — signature, issuer, audience, expiry — and its e-mail address
    # is not verified at the identity provider (#1074). Not a guess at a credential.
    email_unverified: bool = False

    @property
    def code(self) -> str:
        """A stable code for the failure, so a client does not branch on ``reason`` (#1000).

                ``token_expired`` — the bearer token's ``exp`` has passed: get a new token and
        send the request again. ``email_not_verified`` — the token is valid and its
                e-mail address is not verified: verify it at the identity provider (#1074).
                ``auth_unavailable`` — the JWKS could not be fetched (503): the credentials were
                not judged, try again. ``unauthorized`` — every other refusal: a missing or
                wrong API key, a token that does not verify.
        """
        if self.status_code == 503:
            return "auth_unavailable"
        if self.expired:
            return "token_expired"
        if self.email_unverified:
            return "email_not_verified"
        return "unauthorized"

    @property
    def counts_as_a_failed_attempt(self) -> bool:
        """Whether the failure throttle counts this refusal.

        Only a 401 for credentials that did not verify. An expired token (#1031) and an
        unverified e-mail (#1074) are 401s whose signature verified: they are not guesses,
        and counting them lets an ordinary user reach the limit by doing nothing wrong.
        """
        return self.status_code == 401 and not self.expired and not self.email_unverified


def _is_dev_mode() -> bool:
    """Check if in local development mode (#321, ADR 0006).

    If KPUBDATA_BUILDER_DEV_MODE is 'true'/'1', skip authentication.
    Production deployments must not set this variable.
    """
    return os.environ.get(_DEV_MODE_ENV, "").lower() in ("true", "1")


def _verify_api_key(api_key: str | None) -> Principal | AuthError:
    """X-API-Key path (B2). Fail-closed: missing/mismatched key → AuthError.

    Current config supports only a single shared static key per instance (ADR 0006) —
    don't use the key value itself as owner_id source (raw secret must not appear in
    owner_id/logs, #505). Hash a fixed label ("default") to grant one stable service
    owner identity.
    """
    expected = os.environ.get(_API_KEY_ENV)
    if not expected:
        return AuthError(reason="api key not configured")
    # compare_digest only accepts ASCII when given two strings — a single non-ASCII
    # header causes TypeError → 500, and that path isn't even logged as auth failure,
    # slipping past throttle. Byte comparison treats such input as ordinary mismatch.
    if api_key is not None and hmac.compare_digest(
        api_key.encode("utf-8"), expected.encode("utf-8")
    ):
        # service principal remains admin — same authority as the old
        # ``kind in ("dev", "service")`` gate (#679). The difference is that
        # this fact is now visible as a value.
        return Principal(
            kind="service",
            owner_id=compute_owner_id("service", "default"),
            is_admin=True,
        )
    return AuthError(reason="invalid api key")


# --- OIDC Bearer (B3, ADR 0009) -------------------------------------------------
# JWKS client created lazily and cached. None when OIDC disabled.
_jwks_client: object | None = None
_jwks_url_cached: str | None = None
_jwks_lock = threading.Lock()

#: Each request to the identity provider (discovery document, keys) gives up after this.
_IDP_TIMEOUT_SECONDS: float = _DISCOVERY_TIMEOUT_SECONDS
#: After a failed fetch the provider is not asked again for this long, doubling with
#: each failure in a row up to the ceiling (#1323).
_BACKOFF_INITIAL_SECONDS = 2.0
_BACKOFF_MAX_SECONDS = 60.0
#: How long past their TTL the last keys fetched keep verifying while the provider cannot
#: be reached (#1323). Bounded: a key the provider has since revoked must stop working.
_JWKS_STALE_GRACE_SECONDS = 600.0

#: The clock the caches below read; a test replaces it.
_clock: Callable[[], float] = time.monotonic

_T = TypeVar("_T")


class _IdpUnavailableError(RuntimeError):
    """The identity provider's discovery document or keys could not be had (#1323).

    The message is for the log only. A client is told ``auth_unavailable`` and nothing
    about the provider's address or what it answered.
    """


@dataclass
class _CacheEntry(Generic[_T]):
    value: _T | None = None
    fresh_until: float = 0.0
    fetched_at: float = 0.0
    failures: int = 0
    retry_at: float = 0.0
    in_flight: threading.Event | None = None


class _RefreshingCache(Generic[_T]):
    """A value fetched from the identity provider, one fetch at a time per key (#1323).

    The lock guards only the bookkeeping; the fetch runs outside it. While one request
    fetches, the others are given the last good value when there is one, and otherwise
    wait for that one fetch — never for longer than ``_IDP_TIMEOUT_SECONDS`` — instead
    of each fetching in turn. A failure is remembered: until a backoff that doubles up
    to ``_BACKOFF_MAX_SECONDS`` has passed, nobody asks the provider again, and the
    answer is the last good value or ``_IdpUnavailableError`` at once.
    """

    def __init__(self, fetch: Callable[[str], _T], *, stale_grace: float | None) -> None:
        self._fetch = fetch
        #: How long after its TTL a value may stand in for a failed refresh; None: always.
        self._stale_grace = stale_grace
        self._lock = threading.Lock()
        self._entries: dict[str, _CacheEntry[_T]] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, key: str, *, ttl: float, max_age: float | None = None) -> _T:
        """The value for ``key``; with ``max_age``, one fetched no longer ago than that.

        A ``max_age`` request is never given a value past its TTL in place of a failed
        fetch: it asks because the value in hand was not enough.

        Raises:
            _IdpUnavailableError: No usable value, and the provider could not give one.
        """
        with self._lock:
            entry = self._entries.setdefault(key, _CacheEntry())
            now = _clock()
            young = max_age is None or now - entry.fetched_at < max_age
            if entry.value is not None and now < entry.fresh_until and young:
                return entry.value
            stale = None if max_age is not None else self._usable_stale(entry, now)
            if entry.in_flight is None and now >= entry.retry_at:
                entry.in_flight = threading.Event()
                flight = entry.in_flight
                leader = True
            elif stale is not None:
                return stale
            elif entry.in_flight is None:
                raise _IdpUnavailableError("backing off after a failed fetch")
            else:
                flight = entry.in_flight
                leader = False

        if not leader:
            if not flight.wait(_IDP_TIMEOUT_SECONDS):
                raise _IdpUnavailableError("another request's fetch did not finish in time")
            with self._lock:
                if entry.failures == 0 and entry.value is not None:
                    return entry.value
            raise _IdpUnavailableError("another request's fetch failed")

        try:
            value = self._fetch(key)
        except _IdpUnavailableError as exc:
            with self._lock:
                entry.failures += 1
                backoff = _BACKOFF_INITIAL_SECONDS * 2 ** min(entry.failures - 1, 16)
                entry.retry_at = _clock() + min(backoff, _BACKOFF_MAX_SECONDS)
            _logger.warning("identity provider fetch failed: %s", exc)
            if stale is not None:
                return stale
            raise
        else:
            with self._lock:
                entry.value = value
                entry.fetched_at = _clock()
                entry.fresh_until = entry.fetched_at + ttl
                entry.failures = 0
                entry.retry_at = 0.0
            return value
        finally:
            with self._lock:
                entry.in_flight = None
            flight.set()

    def _usable_stale(self, entry: _CacheEntry[_T], now: float) -> _T | None:
        if entry.value is None:
            return None
        if self._stale_grace is not None and now >= entry.fresh_until + self._stale_grace:
            return None
        return entry.value


def _idp_ttl() -> int:
    return int(os.environ.get(_OIDC_JWKS_TTL_ENV, "") or _DEFAULT_JWKS_TTL_SECONDS)


def _read_json_object(url: str, response_body: bytes) -> dict[str, object]:
    import json

    try:
        doc = json.loads(response_body)
    except ValueError as exc:
        raise _IdpUnavailableError(f"{url} did not answer JSON") from exc
    if not isinstance(doc, dict):
        raise _IdpUnavailableError(f"{url} did not answer a JSON object")
    return doc


def _fetch_jwks_uri(issuer: str) -> str:
    """Read ``jwks_uri`` from the issuer's discovery document (RFC 8414, #435)."""
    import http.client
    import urllib.request
    from urllib.parse import urlparse

    base = issuer if issuer.startswith("http") else "https://" + issuer
    discovery_url = base.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(discovery_url, timeout=_IDP_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except (OSError, http.client.HTTPException) as exc:
        # URLError, HTTPError (4xx/5xx), a timeout and a refused connection are OSErrors.
        raise _IdpUnavailableError(
            f"discovery at {discovery_url} failed: {type(exc).__name__}"
        ) from exc
    jwks_uri = _read_json_object(discovery_url, body).get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise _IdpUnavailableError(f"discovery at {discovery_url} has no jwks_uri")
    scheme = urlparse(jwks_uri).scheme.lower()
    # The keys are fetched over TLS whenever the issuer is: a discovery document cannot
    # downgrade them to plain HTTP. An issuer configured over plain HTTP is a local setup
    # its operator chose.
    allowed = ("https",) if urlparse(base).scheme.lower() == "https" else ("http", "https")
    if scheme not in allowed:
        raise _IdpUnavailableError(f"discovery at {discovery_url} names a {scheme!r} jwks_uri")
    return jwks_uri


# OIDC discovery result cache: issuer -> jwks_uri (#435). A discovered URL is served
# past its TTL while a refresh fails: it says only where to fetch keys, and the keys
# themselves are checked against their own, bounded, grace.
_discovery_cache: _RefreshingCache[str] = _RefreshingCache(_fetch_jwks_uri, stale_grace=None)


def _oidc_issuers() -> list[str]:
    raw = os.environ.get(_OIDC_ISSUER_ENV, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def oidc_enabled() -> bool:
    """Whether OIDC sign-in is configured, i.e. at least one issuer is set."""
    return bool(_oidc_issuers())


def _discover_jwks_uri(issuer: str) -> str:
    """Fetch jwks_uri from OIDC discovery document (RFC 8414, #435).

    Query issuer's ``/.well-known/openid-configuration`` and return the ``jwks_uri``
    field. JWKS paths differ by IdP (Google, Auth0, Keycloak, etc.), so prior
    guessing of the path broke. Result is TTL-cached, fetched once at a time, and a
    failure backs off (#1323).

    Raises:
        _IdpUnavailableError: The document could not be fetched or does not name a
            usable ``jwks_uri``.
    """
    return _discovery_cache.get(issuer, ttl=_idp_ttl())


def _oidc_jwks_url() -> str:
    """JWKS URL. When OIDC_JWKS_URL is explicit, skip discovery (#435).

    Without it, read jwks_uri from the first issuer's discovery document (RFC 8414).
    Prior ``issuer + /.well-known/jwks.json`` guessing caused 404 → 503 failures
    because Google doesn't use that path.
    """
    explicit = os.environ.get(_OIDC_JWKS_URL_ENV, "").strip()
    if explicit:
        return explicit
    issuers = _oidc_issuers()
    if not issuers:
        raise RuntimeError("OIDC_ISSUER not set")
    return _discover_jwks_uri(issuers[0])


#: An allowlist entry: the issuer it is for (None when the entry names none) and the
#: ``hd``, subject or e-mail it admits.
_AllowlistEntry = tuple[str | None, str]

_ALLOWLIST_ENVS = (_OIDC_ALLOWED_HD_ENV, _OIDC_ALLOWED_SUBJECTS_ENV, _OIDC_ALLOWED_EMAILS_ENV)


def _parse_allowlist(env_name: str) -> set[_AllowlistEntry]:
    """One allowlist's entries, each with the issuer it is written for (#1074).

    ``<issuer>|<value>`` admits the value from that issuer only. The part before the
    first ``|`` is read as an issuer only when it is one of ``OIDC_ISSUER`` — some IdPs
    put a ``|`` inside the subject itself (``google-oauth2|1234``), and such an entry is
    the whole value, with no issuer named.
    """
    issuers = _oidc_issuers()
    entries: set[_AllowlistEntry] = set()
    raw = os.environ.get(env_name, "")
    for item in raw.replace(" ", ",").split(","):
        item = item.strip()
        if not item:
            continue
        issuer, separator, value = item.partition("|")
        if separator and value and issuer in issuers:
            entries.add((issuer, value))
        else:
            entries.add((None, item))
    return entries


def _oidc_allowlists() -> tuple[set[_AllowlistEntry], set[_AllowlistEntry], set[_AllowlistEntry]]:
    """(hd, subjects, emails) allowlists — optional restrictions applied only when set (#386).

    Default policy is open registration (anyone with IdP account can log in). Only
    restricted deployments allowing specific domains/accounts set these lists,
    and when set, at least one must match to pass.
    """
    return (
        _parse_allowlist(_OIDC_ALLOWED_HD_ENV),
        _parse_allowlist(_OIDC_ALLOWED_SUBJECTS_ENV),
        _parse_allowlist(_OIDC_ALLOWED_EMAILS_ENV),
    )


def _on_allowlist(entries: set[_AllowlistEntry], issuer: str, value: str) -> bool:
    """Whether ``value``, as ``issuer`` states it, is on the list (#1074).

    A subject is unique only within its issuer, and an e-mail or ``hd`` is whatever the
    issuer says it is — so an entry admits a value from the issuer it names. An entry
    naming none means the deployment's one issuer; with several configured it admits
    nobody (``validate_oidc_config`` refuses to start that way), because it would give
    the account one issuer vouches for to whoever holds the same name at another.
    """
    if not value:
        return False
    sole_issuer = len(_oidc_issuers()) == 1
    return any(
        listed == value and (entry_issuer == issuer or (entry_issuer is None and sole_issuer))
        for entry_issuer, listed in entries
    )


def _admin_identities() -> set[tuple[str, str]]:
    """Set of (issuer, sub) to treat as admins (#679). Empty set if not configured.

    Only accepts ``issuer|sub`` format. Entries missing issuer are **not silently
    ignored; a warning is logged and they're discarded** — prevent both silent
    loss of admin rights due to typo and silent accidental granting of admin
    rights due to typo.
    """
    raw = os.environ.get(_ADMIN_SUBJECTS_ENV, "")
    identities: set[tuple[str, str]] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        issuer, separator, subject = entry.partition("|")
        if not separator or not issuer.strip() or not subject.strip():
            _logger.warning(
                "ignoring a malformed %s entry: expected '<issuer>|<subject>'. "
                "An OIDC subject is only unique within its issuer, so a bare "
                "subject cannot grant administrator rights.",
                _ADMIN_SUBJECTS_ENV,
            )
            continue
        identities.add((issuer.strip(), subject.strip()))
    return identities


def validate_oidc_config() -> None:
    """Validate OIDC config at server startup. Raise RuntimeError on error (fail-closed, #385).

    - OIDC_ISSUER not set → no-op (Bearer disabled, no impact on existing deployments).
    - OIDC_ISSUER set + OIDC_AUDIENCE not set → reject.
    - pyjwt not installed → reject (``auth`` extra required).
    - OIDC set + neither an allowlist (OIDC_ALLOWED_HD/SUBJECTS/EMAILS) nor an
      administrator (KPUBDATA_BUILDER_ADMIN_SUBJECTS) → reject (#635, #785). An OIDC
      deployment is multi-user, and ADR 0012's 2026-09-30 amendment makes an allowlist
      mandatory there. The Builder sign-up ledger is that allowlist at run time: a user
      on no list signs up as ``pending`` and waits for an administrator — so with an
      administrator and no list, nobody gets in unapproved. With neither, nobody could
      ever get in, which is a configuration mistake.
    """
    if not _oidc_issuers():
        return
    if not os.environ.get(_OIDC_AUDIENCE_ENV, "").strip():
        raise RuntimeError(
            "OIDC_ISSUER is set but OIDC_AUDIENCE is missing; "
            "refusing to start (fail-closed, ADR 0009)."
        )
    try:
        import jwt  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "OIDC is enabled but pyjwt is not installed; install with: uv sync --extra auth"
        ) from e
    # An allowlist is mandatory with OIDC (#635). This replaces the #644 startup
    # warning about open sign-up, and the opt-in OIDC_LEGACY_REQUIRE_ALLOWLIST switch
    # that made it mandatory: both assumed open sign-up could be a valid choice.
    hd, subs, emails = _oidc_allowlists()
    if not (hd or subs or emails or _admin_identities()):
        raise RuntimeError(
            "OIDC_ISSUER is set but no allowlist is configured "
            "(OIDC_ALLOWED_HD/SUBJECTS/EMAILS) and no administrator "
            "(KPUBDATA_BUILDER_ADMIN_SUBJECTS) could approve sign-ups; an OIDC deployment "
            "serves more than one user and open sign-up is refused — refusing to start "
            "(fail-closed, ADR 0012 amendment of 2026-09-30, #635, #785)."
        )
    # With more than one issuer, an entry must say which issuer it is for (#1074). The
    # message names the variables, never an entry: these are people's e-mail addresses.
    if len(_oidc_issuers()) > 1:
        unbound = [
            name
            for name, entries in zip(_ALLOWLIST_ENVS, (hd, subs, emails), strict=True)
            if any(issuer is None for issuer, _ in entries)
        ]
        if unbound:
            raise RuntimeError(
                f"OIDC_ISSUER names more than one issuer, and {', '.join(unbound)} "
                "has an entry that names none; write each entry as '<issuer>|<value>' "
                "with the issuer exactly as it is in OIDC_ISSUER. A subject, e-mail or "
                "domain is only what its issuer says it is, so an entry without one "
                "would admit the same name from every issuer — refusing to start "
                "(fail-closed, #1074)."
            )


#: Read here rather than from ``ownership`` — that module imports this one.
_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


def _ownership_enforced() -> bool:
    # The same reading ``ownership.enforce_ownership`` gives the variable.
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


def validate_dev_mode() -> None:
    """Called at server startup (serve). Prevent production accidents from dev-mode.

    Dev-mode is the first branch of ``authenticate()``, skipping API key and Bearer
    token checks. Pass all requests without authentication (#321, ADR 0006). Local
    development only:

    - If dev-mode is enabled, log a warning at startup - the fact that service is
      unauthenticated must be visible just from logs.
    - If dev-mode and ``ENFORCE_OWNERSHIP`` are both set, fail to start (#1072). Either
      of OIDC and ``ENFORCE_OWNERSHIP`` makes a deployment multi-user (ADR 0012), and
      the dev principal has full access to every run.
    - If both dev-mode and OIDC are configured, fail to start. Configuring user
      authentication and then bypassing it entirely is never intentional in any
      environment; it is a classic production accident when a dev flag is left in
      (fail-closed).
    """
    if not _is_dev_mode():
        return
    if _oidc_issuers():
        raise RuntimeError(
            f"{_DEV_MODE_ENV} is enabled while {_OIDC_ISSUER_ENV} is configured; "
            "refusing to start — dev-mode bypasses authentication entirely, so a "
            "deployment that configures user authentication must not set it "
            "(fail-closed, ADR 0006)."
        )
    if _ownership_enforced():
        raise RuntimeError(
            f"{_DEV_MODE_ENV} is enabled while {_OWNERSHIP_ENV} is set; refusing to start "
            "— a deployment that keeps each user's runs apart serves more than one user, "
            "and the dev principal reads every user's runs without authenticating "
            "(fail-closed, ADR 0012 decision of 2026-10-01, #1072)."
        )
    _logger.warning(
        "%s is enabled: every request is accepted without authentication. "
        "This is for local development only — never set it in a deployment.",
        _DEV_MODE_ENV,
    )
    if os.environ.get(_API_KEY_ENV):
        _logger.warning("%s is set but ignored while dev-mode is enabled.", _API_KEY_ENV)


def _get_jwks_client(issuer: str | None = None) -> object:
    """Create and cache the key source lazily (thread-safe).

    Without ``issuer``: the deployment's one set of keys (``OIDC_JWKS_URL``, or the first
    issuer's). With it: that issuer's keys, kept apart per issuer (#1074). Discovery runs
    before the lock is taken (#1323): a slow provider must not hold every other request.

    Raises:
        _IdpUnavailableError: The issuer's discovery document could not be had.
    """
    global _jwks_client, _jwks_url_cached
    if issuer is not None:
        url = _discover_jwks_uri(issuer)
        with _jwks_lock:
            cached = _issuer_jwks_clients.get(issuer)
            if cached is None or cached[0] != url:
                cached = (url, _new_jwks_client(url))
                _issuer_jwks_clients[issuer] = cached
            return cached[1]
    url = _oidc_jwks_url()
    with _jwks_lock:
        if _jwks_client is None or _jwks_url_cached != url:
            _jwks_client = _new_jwks_client(url)
            _jwks_url_cached = url
        return _jwks_client


def _new_jwks_client(url: str) -> object:
    return _JwksKeys(url)


#: An unknown ``kid`` refetches the keys only when they are older than this, as
#: PyJWKClient's ``cooldown_duration`` did: a made-up ``kid`` cannot make every request
#: a call to the provider.
_JWKS_REFRESH_COOLDOWN_SECONDS = 30.0


def _fetch_jwk_set(url: str) -> PyJWKSet:
    """The key set at ``url``, or ``_IdpUnavailableError`` (#1323)."""
    import http.client
    import urllib.request
    from urllib.parse import urlparse

    import jwt
    from jwt import PyJWKSet

    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise _IdpUnavailableError("the JWKS URL is not http(s)")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        # As PyJWKClient: the keys are read from the URL configured or discovered,
        # not from wherever it redirects.
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(url, timeout=_IDP_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except (OSError, http.client.HTTPException) as exc:
        raise _IdpUnavailableError(f"keys at {url} failed: {type(exc).__name__}") from exc
    doc = _read_json_object(url, body)
    try:
        return PyJWKSet.from_dict(doc)
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _IdpUnavailableError(f"keys at {url} hold no usable key") from exc


# JWKS URL -> its key set (#1323). Served past its TTL while a refresh fails, for a
# bounded grace only.
_jwks_cache: _RefreshingCache[PyJWKSet] = _RefreshingCache(
    _fetch_jwk_set, stale_grace=_JWKS_STALE_GRACE_SECONDS
)


class _JwksKeys:
    """The signing keys at one JWKS URL, for ``_verify_bearer_token`` (#1323).

    Replaces PyJWKClient, which holds a lock across its network fetch and keeps no key
    set once its lifespan ends: during an outage every request fetched, in turn, for up
    to 30 seconds each. The keys now come from ``_jwks_cache``.
    """

    def __init__(self, uri: str) -> None:
        self.uri = uri

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        """The key for the token's ``kid``.

        Raises:
            jwt.PyJWTError: The token's header cannot be read.
            jwt.PyJWKClientError: The keys hold no key with this ``kid``.
            _IdpUnavailableError: The keys could not be had.
        """
        import jwt
        from jwt import PyJWKClientError

        kid = jwt.get_unverified_header(token).get("kid")
        key = self._match(_jwks_cache.get(self.uri, ttl=_idp_ttl()), kid)
        if key is None:
            # A rotated key: ask again, unless the keys in hand are already that recent.
            fresh = _jwks_cache.get(
                self.uri, ttl=_idp_ttl(), max_age=_JWKS_REFRESH_COOLDOWN_SECONDS
            )
            key = self._match(fresh, kid)
        if key is None:
            raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return key

    @staticmethod
    def _match(keys: PyJWKSet, kid: object) -> PyJWK | None:
        for key in keys.keys:
            if key.key_id and key.key_id == kid and key.public_key_use in ("sig", None):
                return key
        return None


#: issuer -> (JWKS URL, client), for a deployment with more than one issuer.
_issuer_jwks_clients: dict[str, tuple[str, object]] = {}


def _jwks_client_for(token: str) -> object | None:
    """The JWKS client holding the keys of the issuer ``token`` claims (#1074).

    With one issuer, or an explicit ``OIDC_JWKS_URL``, there is one set of keys and it is
    used as before. With several issuers the keys came from the **first** one only, so a
    token from any other could never verify. The token's own ``iss`` — read unverified,
    used for nothing but choosing among the configured issuers — now selects the keys;
    the signature check that follows is what makes the claim count. None when the token
    names an issuer that is not configured.

    Raises:
        jwt.PyJWTError: The token cannot be parsed.
    """
    import jwt

    issuers = _oidc_issuers()
    if len(issuers) <= 1 or os.environ.get(_OIDC_JWKS_URL_ENV, "").strip():
        return _get_jwks_client()
    claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
    if not isinstance(claimed, str) or claimed not in issuers:
        return None
    return _get_jwks_client(claimed)


#: Text PyJWT uses when "this kid not in JWKS". Only one exception type, so we
#: must distinguish by message — if unrecognized, fail safe with 503 like before.
_UNKNOWN_KEY_MARKERS = ("unable to find a signing key", "no matching key")


def _is_unknown_signing_key(exc: Exception) -> bool:
    """Check if JWKS was fetched but doesn't contain this token's kid."""
    message = str(exc).casefold()
    return any(marker in message for marker in _UNKNOWN_KEY_MARKERS)


def _verify_bearer_token(token: str) -> Principal | AuthError:
    """Offline verify the bearer token via JWKS (#385, ADR 0009).

    The token is the IdP's access token for this API (kpubdata-studio#722). Its kind is
    not inspected (``typ``, ``azp`` and ``nonce`` are ignored): it is accepted for what
    it claims — issuer, audience, expiry, subject and a verified e-mail.

    - RS256 fixed (reject alg:none / HS*).
    - Verify iss/aud/exp/nbf/iat (60s leeway), require email_verified.
    - JWKS fetch failure → 503 (not 401; temporary infrastructure fault).
    """
    import jwt
    from jwt import PyJWKClientError

    try:
        client = _jwks_client_for(token)
        if client is None:
            # The token names an issuer this deployment does not accept. Refused here,
            # before any request goes out for that issuer's keys.
            return AuthError(reason="invalid token")
        signing_key = client.get_signing_key_from_jwt(token)  # type: ignore[attr-defined]
    except _IdpUnavailableError:
        # The provider's discovery document or keys could not be had (#1323): the
        # credentials were not judged. Never a 401 — Studio signs out on one.
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)
    except PyJWKClientError as exc:
        # "No signing key found" and "couldn't reach JWKS" are completely different
        # events but were grouped. Returning 503 for the former means (a) throttle
        # won't count it — 503 is client-agnostic, deliberately excluded — (b)
        # PyJWKClient fetches JWKS on each cache miss, so arbitrary kid means
        # repeated requests to send the token triggers one IdP outbound per request.
        # Unknown kid is just invalid credentials.
        if _is_unknown_signing_key(exc):
            return AuthError(reason="invalid token")
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)
    except (ConnectionError, OSError):
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)
    except jwt.PyJWTError:
        # PyJWKClient parses the unverified JWT header before selecting a key.
        # Malformed compact serialization/header errors are invalid credentials,
        # not JWKS infrastructure failures.
        return AuthError(reason="invalid token")

    audience = os.environ.get(_OIDC_AUDIENCE_ENV, "").strip()
    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=_oidc_issuers(),
            leeway=_TOKEN_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        return AuthError(
            reason=f"invalid token: {type(exc).__name__}",
            expired=isinstance(exc, jwt.ExpiredSignatureError),
        )

    if not payload.get("email_verified", False):
        return AuthError(reason="email not verified", email_unverified=True)

    # Allowlist check (#386). A principal on a configured list is admitted by sign-in
    # alone. One on no list is not refused here any more (#785): the service asks the
    # Builder sign-up ledger, which records it as pending until an administrator
    # decides. With no list at all nobody is admitted by sign-in alone (#635).
    #
    # Each list is asked with the token's issuer (#1074): the same subject or e-mail
    # from another configured issuer is another account.
    sub = str(payload.get("sub", ""))
    if not sub:
        # jwt.decode's require=["sub"] only enforces claim "existence", not that value is
        # non-empty. Allowing empty sub causes different tokens to converge on the same
        # (issuer, "") owner_id, mixing up ownership (#505).
        return AuthError(reason="missing subject claim")
    issuer = str(payload.get("iss", ""))
    hd_set, sub_set, email_set = _oidc_allowlists()
    allowlisted = (
        _on_allowlist(hd_set, issuer, str(payload.get("hd", "")))
        or _on_allowlist(sub_set, issuer, sub)
        or _on_allowlist(email_set, issuer, str(payload.get("email", "")))
    )
    # owner_id hashes full issuer+subject without truncation (#505) — passing issuer
    # and sub as separate fields ensures unambiguous collision prevention without
    # delimiters (length-prefix framing). Different from identifier below (log/display
    # use, truncated to first 8 chars of sub) — collision prevention is needed for
    # persistent ownership judgment.
    owner_id = compute_owner_id("oidc", issuer, sub)
    # Trace identifier using first 8 chars of sub only (minimize raw sub exposure).
    #
    # Admin judgment uses **(issuer, full sub)**. Comparing only first 8 chars of sub
    # would let different accounts with matching prefix become admin, and omitting
    # issuer would let the same sub from a different issuer become admin — same reason
    # owner_id binds both.
    is_admin = (issuer, sub) in _admin_identities()
    email = payload.get("email")
    return Principal(
        kind="oidc",
        identifier=sub[:8],
        owner_id=owner_id,
        is_admin=is_admin,
        admitted=allowlisted or is_admin,
        display_name=email if isinstance(email, str) and email else sub[:8],
    )


def authenticate(
    *, api_key: str | None = None, bearer_token: str | None = None
) -> Principal | AuthError:
    """Authenticate request, returning ``Principal`` or ``AuthError`` (B2/B3).

    Priority:
    - dev-mode → ``Principal(kind='dev')`` (local development convenience).
    - Bearer token + OIDC enabled → Bearer verification (human user).
    - Otherwise → X-API-Key verification (service account).

    When OIDC is disabled, Bearer is ignored with no impact on existing deployments.
    """
    if _is_dev_mode():
        # dev principal owner_id is a fixed local identifier that doesn't change per run (#505) —
        # OIDC principal owner_id always starts with "oidc:" so namespaces don't overlap.
        return Principal(kind="dev", owner_id=compute_owner_id("dev", "local"), is_admin=True)

    if bearer_token and _oidc_issuers():
        if bearer_token.lower().startswith("bearer "):
            return _verify_bearer_token(bearer_token[7:].strip())
        return AuthError(reason="malformed authorization header")

    if _oidc_issuers() and not os.environ.get(_API_KEY_ENV):
        # A deployment that signs users in and has no API key at all: the request lacks a
        # token, and "api key not configured" would send its reader looking for a key
        # this deployment does not use.
        return AuthError(reason="sign-in required: send a bearer token")

    return _verify_api_key(api_key)


__all__ = [
    "AuthError",
    "Principal",
    "authenticate",
    "compute_owner_id",
    "oidc_enabled",
    "principal_owns",
    "validate_dev_mode",
    "validate_oidc_config",
]
