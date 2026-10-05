"""Whether a build's data may leave Builder, from each source's declared terms (#688).

kpubdata 0.8 declares, per dataset, whether its data may be redistributed
(``DatasetRef.license.redistribution``, kpubdata#525): ``allowed``, ``non_commercial``,
``forbidden`` or ``unknown``. A dataset that declares nothing is ``unknown`` — not
knowing is never read as permission. One that says ``allowed`` without the ``attribution``
text that shows the terms were confirmed is ``unknown`` too (#1030). A file or URL source
has no provider terms and is ``unknown`` as well.

A build is as restricted as its most restricted source:
``forbidden`` > ``unknown`` > ``non_commercial`` > ``allowed``.

What each verdict permits:

- **forbidden** — nothing leaves: no publish (public or private), and no rows through
  ``/query``, ``/preview``, stage samples, warehouse reads and exports, or artifact
  downloads (:func:`forbidden_response`).
- **unknown** — no *public* publish. Private publishing and reading are allowed: the
  terms are undecided, which is what kpubdata#524 is for.
- **non_commercial** — publishing only with ``confirm_non_commercial: true``, and a
  public publish also needs a non-commercial licence marker on the dataset (e.g.
  ``cc-by-nc-4.0``).
- **allowed** — no restriction from the terms (the BuildSpec licence gate, #443, still
  applies).

The legacy publish path has its own gate on config licences (#688, owner decision D2).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ..spec import BuildSpec, JsonValue, SourceRef
from .responses import ServiceResponse

Verdict = Literal["allowed", "non_commercial", "unknown", "forbidden"]

#: Most restrictive first.
_ORDER: tuple[Verdict, ...] = ("forbidden", "unknown", "non_commercial", "allowed")
_VERDICTS = frozenset(_ORDER)
_NON_COMMERCIAL_MARKER = re.compile(r"(?i)(^|[-_ ])nc([-_ ]|$)|non[-_ ]?commercial")

#: What the catalog lookup answers for a dataset that says ``redistribution: allowed``
#: without the ``attribution`` text. kpubdata's rule is that a term nobody confirmed must
#: not read as permission (kpubdata#785), and the attribution text is what shows the
#: terms were read from the provider's page (kpubdata#525). kpubdata still ships such
#: specs — frozen in its ``scripts/unconfirmed_terms_baseline.txt`` — so the gate reads
#: the text itself instead of taking ``allowed`` at its word (#1030).
UNCONFIRMED_ALLOWED = "allowed_unconfirmed"

#: ``"provider.dataset"`` → the dataset's declared ``redistribution`` (None when it
#: declares no licence, :data:`UNCONFIRMED_ALLOWED` when it says ``allowed`` without a
#: confirmed attribution), or raises ``LookupError`` when the dataset is not in the catalog.
TermsLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class SourceVerdict:
    source: str
    verdict: Verdict
    reason: str

    def body(self) -> dict[str, JsonValue]:
        return {"source": self.source, "verdict": self.verdict, "reason": self.reason}


@dataclass(frozen=True)
class BuildVerdict:
    verdict: Verdict
    sources: tuple[SourceVerdict, ...]

    def body(self) -> dict[str, JsonValue]:
        return {
            "verdict": self.verdict,
            "sources": [s.body() for s in self.sources],
        }


def kpubdata_terms(dataset_id: str) -> str | None:
    """The dataset's declared redistribution from the kpubdata catalog (no network).

    Every service binds this as its default lookup. It reads the catalog through
    :func:`_catalog_terms` at call time, so the test suite can pin the terms in one
    place whatever kpubdata is installed.
    """
    return _catalog_terms(dataset_id)


@functools.lru_cache(maxsize=4096)
def _catalog_terms(dataset_id: str) -> str | None:
    """Cached: the terms come with the installed kpubdata and do not change while it runs."""
    import kpubdata

    client = kpubdata.Client()
    try:
        ref = client.dataset(dataset_id).ref
    except Exception as exc:  # kpubdata raises its own not-found errors
        raise LookupError(dataset_id) from exc
    finally:
        client.close()
    return declared_terms(getattr(ref, "license", None))


def declared_terms(license_spec: object | None) -> str | None:
    """A licence declaration as the gate reads it: ``allowed`` counts only with its
    attribution text."""
    if license_spec is None:
        return None
    declared = getattr(license_spec, "redistribution", None)
    if declared == "allowed":
        attribution = getattr(license_spec, "attribution", None)
        if not isinstance(attribution, str) or not attribution.strip():
            return UNCONFIRMED_ALLOWED
    return declared if isinstance(declared, str) else None


def source_verdict(source: SourceRef, lookup: TermsLookup) -> SourceVerdict:
    if source.kind != "public_api":
        label = source.alias or source.kind
        return SourceVerdict(label, "unknown", f"a {source.kind} source has no provider terms")
    dataset_id = f"{source.provider}.{source.dataset}"
    try:
        declared = lookup(dataset_id)
    except LookupError:
        return SourceVerdict(dataset_id, "unknown", "the dataset is not in the kpubdata catalog")
    if declared == UNCONFIRMED_ALLOWED:
        return SourceVerdict(
            dataset_id,
            "unknown",
            "the dataset says redistribution: allowed without the attribution text "
            "that shows its terms were confirmed",
        )
    if declared is None or declared not in _VERDICTS:
        return SourceVerdict(dataset_id, "unknown", "the dataset declares no redistribution terms")
    return SourceVerdict(
        dataset_id,
        declared,
        f"the dataset declares redistribution: {declared}",
    )


def build_verdict(spec: BuildSpec | None, lookup: TermsLookup = kpubdata_terms) -> BuildVerdict:
    """The verdict for a build: its most restricted source's. A missing spec is unknown."""
    if spec is None:
        return BuildVerdict(
            "unknown", (SourceVerdict("run", "unknown", "the run's spec could not be read"),)
        )
    sources = tuple(source_verdict(s, lookup) for s in spec.sources)
    verdict = next(v for v in _ORDER if any(s.verdict == v for s in sources))
    return BuildVerdict(verdict, sources)


def has_non_commercial_marker(spec: BuildSpec | None) -> bool:
    """Whether the dataset's own licence says non-commercial (``cc-by-nc-4.0`` and alike)."""
    if spec is None:
        return False
    return any(
        value and _NON_COMMERCIAL_MARKER.search(value)
        for value in (spec.license, spec.license_name)
    )


@dataclass(frozen=True)
class PublishTermsIssue:
    code: str
    message: str


def publish_issues(
    verdict: BuildVerdict,
    *,
    public: bool,
    confirmed_non_commercial: bool,
    spec: BuildSpec | None,
) -> list[PublishTermsIssue]:
    """What the terms forbid about this publish; empty when they allow it."""
    restricted = ", ".join(
        f"{s.source} ({s.verdict})" for s in verdict.sources if s.verdict != "allowed"
    )
    if verdict.verdict == "forbidden":
        return [
            PublishTermsIssue(
                "redistribution_forbidden",
                f"the source terms forbid redistribution: {restricted}",
            )
        ]
    if verdict.verdict == "unknown" and public:
        return [
            PublishTermsIssue(
                "redistribution_unknown",
                "the source terms are not known, and unknown is not permission to publish "
                f"publicly: {restricted}. Publish privately, or wait for the terms to be "
                "declared.",
            )
        ]
    if verdict.verdict == "non_commercial":
        issues = []
        if not confirmed_non_commercial:
            issues.append(
                PublishTermsIssue(
                    "non_commercial_unconfirmed",
                    f"the source terms allow non-commercial use only: {restricted}. "
                    "Confirm with options.confirm_non_commercial: true.",
                )
            )
        if public and not has_non_commercial_marker(spec):
            issues.append(
                PublishTermsIssue(
                    "non_commercial_marker_missing",
                    "a public publish of non-commercial data needs a non-commercial "
                    "licence on the dataset (e.g. license: cc-by-nc-4.0); publish privately "
                    "otherwise",
                )
            )
        return issues
    return []


def needs_private_destination(
    verdict: BuildVerdict, *, public: bool, spec: BuildSpec | None
) -> bool:
    """Whether the terms allow this publish only because it is private.

    Then the destination itself must not already be public: publishing to an existing
    repo or dataset does not change its visibility.
    """
    if public:
        return False
    if verdict.verdict == "unknown":
        return True
    return verdict.verdict == "non_commercial"


def visibility_issue(visibility: str | None) -> PublishTermsIssue | None:
    """The refusal a destination's visibility calls for; None when it is fine.

    ``None`` means the visibility could not be read — not knowing is never permission.
    """
    if visibility in ("private", "absent"):
        return None
    if visibility == "public":
        return PublishTermsIssue(
            "destination_public",
            "the destination already exists and is public, and publishing does not change "
            "that; the source terms allow only a private publish",
        )
    return PublishTermsIssue(
        "destination_visibility_unknown",
        "the destination's visibility could not be read, and the source terms allow "
        "only a private publish",
    )


def kpubdata_version() -> str | None:
    """The kpubdata release whose catalog the verdict was read from."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("kpubdata")
    except PackageNotFoundError:
        return None


def forbidden_response(verdict: BuildVerdict, *, what: str) -> ServiceResponse | None:
    """403 when the terms forbid redistribution; None otherwise.

    ``what`` names the way out being refused (``query results``, ``a preview`` …), so
    the message says what was stopped and why.
    """
    if verdict.verdict != "forbidden":
        return None
    return ServiceResponse(
        403,
        {
            "error": f"the source terms forbid redistribution, so {what} cannot leave Builder",
            "code": "redistribution_forbidden",
            "redistribution": verdict.body(),
        },
    )


def is_public(target: str, options: dict[str, object]) -> bool:
    """Whether a publish to ``target`` with ``options`` is public."""
    if target == "huggingface":
        return options.get("private") is False
    if target == "kaggle":
        return options.get("public") is True
    return False


def sources_of(specs: Sequence[BuildSpec | None]) -> tuple[SourceRef, ...]:
    return tuple(source for spec in specs if spec is not None for source in spec.sources)


__all__ = [
    "UNCONFIRMED_ALLOWED",
    "BuildVerdict",
    "PublishTermsIssue",
    "SourceVerdict",
    "TermsLookup",
    "Verdict",
    "build_verdict",
    "declared_terms",
    "forbidden_response",
    "has_non_commercial_marker",
    "is_public",
    "kpubdata_version",
    "needs_private_destination",
    "visibility_issue",
    "kpubdata_terms",
    "publish_issues",
    "source_verdict",
]
