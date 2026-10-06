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
see deploy.md).

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
from dataclasses import dataclass

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
# OIDC discovery result cache: issuer → (jwks_uri, expires_at). #435.
_discovery_cache: dict[str, tuple[str, float]] = {}


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
    guessing of the path broke. Result is TTL-cached.
    """
    import json
    import urllib.request

    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached is not None:
        jwks_uri, expires_at = cached
        if now < expires_at:
            return jwks_uri

    base = issuer if issuer.startswith("http") else "https://" + issuer
    discovery_url = base.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(discovery_url, timeout=_DISCOVERY_TIMEOUT_SECONDS) as resp:
        doc = json.loads(resp.read())
    jwks_uri_raw = doc.get("jwks_uri")
    if not isinstance(jwks_uri_raw, str) or not jwks_uri_raw:
        raise RuntimeError(f"discovery at {discovery_url} has no jwks_uri")
    ttl = int(os.environ.get(_OIDC_JWKS_TTL_ENV, "") or _DEFAULT_JWKS_TTL_SECONDS)
    _discovery_cache[issuer] = (jwks_uri_raw, now + ttl)
    return jwks_uri_raw


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


def _oidc_allowlists() -> tuple[set[str], set[str], set[str]]:
    """(hd, subjects, emails) allowlists — optional restrictions applied only when set (#386).

    Default policy is open registration (anyone with IdP account can log in). Only
    restricted deployments allowing specific domains/accounts set these lists,
    and when set, at least one must match to pass.
    """

    def _parse(env_name: str) -> set[str]:
        raw = os.environ.get(env_name, "")
        return {s.strip() for s in raw.replace(" ", ",").split(",") if s.strip()}

    return (
        _parse(_OIDC_ALLOWED_HD_ENV),
        _parse(_OIDC_ALLOWED_SUBJECTS_ENV),
        _parse(_OIDC_ALLOWED_EMAILS_ENV),
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
    """Create and cache PyJWKClient lazily (thread-safe).

    Without ``issuer``: the deployment's one set of keys (``OIDC_JWKS_URL``, or the first
    issuer's). With it: that issuer's keys, kept apart per issuer (#1074).
    """
    global _jwks_client, _jwks_url_cached
    with _jwks_lock:
        if issuer is not None:
            url = _discover_jwks_uri(issuer)
            cached = _issuer_jwks_clients.get(issuer)
            if cached is None or cached[0] != url:
                cached = (url, _new_jwks_client(url))
                _issuer_jwks_clients[issuer] = cached
            return cached[1]
        url = _oidc_jwks_url()
        if _jwks_client is None or _jwks_url_cached != url:
            _jwks_client = _new_jwks_client(url)
            _jwks_url_cached = url
    return _jwks_client


def _new_jwks_client(url: str) -> object:
    from jwt import PyJWKClient

    ttl = int(os.environ.get(_OIDC_JWKS_TTL_ENV, "") or _DEFAULT_JWKS_TTL_SECONDS)
    return PyJWKClient(url, cache_jwk_set=True, lifespan=ttl)


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
    hd_set, sub_set, email_set = _oidc_allowlists()
    allowlisted = (
        (bool(hd_set) and str(payload.get("hd", "")) in hd_set)
        or (bool(sub_set) and str(payload.get("sub", "")) in sub_set)
        or (bool(email_set) and str(payload.get("email", "")) in email_set)
    )

    sub = str(payload.get("sub", ""))
    if not sub:
        # jwt.decode's require=["sub"] only enforces claim "existence", not that value is
        # non-empty. Allowing empty sub causes different tokens to converge on the same
        # (issuer, "") owner_id, mixing up ownership (#505).
        return AuthError(reason="missing subject claim")
    issuer = str(payload.get("iss", ""))
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
