"""What a source collected, and under which contract — as comparable digests (#700).

A drift baseline is only meaningful against the same population read under the same
rules. Row counts from Seoul 2025 and Busan 2026 are not a trend, and a row count
from before a ``casts`` change is not the same measurement as one after it. These
digests are how a later run tells whether an earlier one is comparable.

All three are computed from :func:`~.serializer.canonical_source_mapping`, the same
text a run's ``buildspec.yaml`` snapshot records. That is the point: a past run's
fingerprints are recomputed from its snapshot, so both sides must go through one
function or a formatting difference would read as a coverage change.

Secrets are already redacted in that mapping, so a credential rotation does not look
like a new population, and no digest is derived from a key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from .models import JsonValue, SourceRef
from .serializer import canonical_source_mapping

# Fields that do not change which records were collected. ``alias`` renames the
# output; ``schema`` decides how records are read, which is the contract's digest.
_NOT_COVERAGE = frozenset({"alias", "schema"})


@dataclass(frozen=True)
class SourceFingerprints:
    """Digests a baseline must match before a row count may be compared.

    Attributes:
        coverage: What population was collected — provider, dataset, parameters and
            parameter grid for an API source, the upload or endpoint otherwise.
        source_params: The request parameters alone. Recorded for provenance; the
            coverage digest already includes them.
        schema_contract: The ``schema`` block that turned raw records into Silver.
            ``null`` digests too, so two runs with no contract compare as equal.
    """

    coverage: str
    source_params: str
    schema_contract: str


def _digest(value: JsonValue) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprints_from_mapping(entry: Mapping[str, JsonValue]) -> SourceFingerprints:
    """Fingerprint one canonical source entry, as found in a run snapshot."""
    return SourceFingerprints(
        coverage=_digest({key: value for key, value in entry.items() if key not in _NOT_COVERAGE}),
        source_params=_digest(
            {"params": entry.get("params", {}), "param_grid": entry.get("param_grid", {})}
        ),
        schema_contract=_digest(entry.get("schema")),
    )


def fingerprint_source(source: SourceRef) -> SourceFingerprints:
    """Fingerprint a source from the current spec."""
    return fingerprints_from_mapping(canonical_source_mapping(source))


__all__ = ["SourceFingerprints", "fingerprint_source", "fingerprints_from_mapping"]
