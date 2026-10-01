"""Versioned data checksums (#867, ADR 0021 D5).

A source's ``data_checksum`` says which records it fetched, independent of their order.
Two things about it are now explicit:

- **Which algorithm made it.** ``data_checksum_algorithm`` names it. A manifest written
  before the name existed used :data:`LEGACY_ALGORITHM`, and is read as such
  (:func:`algorithm_of`). Checksums made by different algorithms are **never compared**:
  equal strings from two algorithms prove nothing, and unequal ones do not prove the
  data changed (:func:`same_data`).
- **Logical, not bytes.** A checksum is over the records, not over any file. What a
  file's bytes are is the separate ``artifact_digest``, with the writer that produced
  them beside it, so an engine that writes the same table differently changes the
  digest and not the checksum.

The algorithms:

``canonical-json-sort-v1`` (legacy)
    Each record serialised as sorted-key JSON, the lines sorted, joined as a JSON array,
    SHA-256. Order-independent, but the sort needs every line at once — in memory, or
    spilled and merged (``provenance.compute_data_checksum_from_jsonl``).

``canonical-multiset-v2`` (current)
    The same serialised lines, combined as a multiset hash: each line is hashed to a
    number modulo the prime ``2**3072 - 1103717`` (the MuHash3072 modulus) and the
    numbers are multiplied. Multiplication is commutative, so order does not matter, and
    each record is folded in as it is read — no sort, no second pass, constant memory.
    Finding two different multisets with the same product is as hard as a discrete
    logarithm in that group; the additive variant (a sum modulo ``2**256``) is not used,
    because k-sum attacks break it. The product and the record count are then hashed
    with SHA-256 so the checksum keeps its ``sha256:`` shape.

    How the product is computed does not change what it is (#917): the modulus is
    ``2**3072 - c`` with a small ``c``, so a product is reduced by folding its high
    bits back in (``2**3072`` is congruent to ``c``) instead of a general division,
    and the running value is kept below ``2**3072`` rather than below the modulus
    until the end. ``tests/unit/test_checksums.py`` checks the result against the
    straightforward ``(product * value) % modulus`` on varied inputs, and
    ``scripts/bench_checksums.py`` measures the cost.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..spec import JsonValue

LEGACY_ALGORITHM = "canonical-json-sort-v1"
MULTISET_ALGORITHM = "canonical-multiset-v2"
#: What new manifests are written with.
CURRENT_ALGORITHM = MULTISET_ALGORITHM
ALGORITHMS = (LEGACY_ALGORITHM, MULTISET_ALGORITHM)

#: ``inputs_fingerprint`` combines per-source checksums; its own version moves with
#: theirs, so a fingerprint over v1 checksums is never taken for one over v2.
LEGACY_FINGERPRINT_ALGORITHM = "sources-sha256-v1"
FINGERPRINT_ALGORITHM = "sources-sha256-v2"

#: The MuHash3072 modulus (a prime; tests/unit/test_checksums.py checks it).
_MODULUS = 2**3072 - 1103717
_ELEMENT_BYTES = 384
_ELEMENT_BITS = _ELEMENT_BYTES * 8
#: ``2**3072`` modulo :data:`_MODULUS`; small, so folding by it is a cheap multiply.
_FOLD = 2**_ELEMENT_BITS - _MODULUS
_LOW_MASK = 2**_ELEMENT_BITS - 1
_DOMAIN = MULTISET_ALGORITHM.encode("ascii") + b"\x00"


def record_line(record: Mapping[str, JsonValue]) -> str:
    """A record as both algorithms serialise it: sorted keys, text kept as text."""
    return json.dumps(dict(record), ensure_ascii=False, sort_keys=True, default=str)


class MultisetChecksum:
    """Folds records into a ``canonical-multiset-v2`` checksum one at a time."""

    def __init__(self) -> None:
        # Congruent to the product modulo _MODULUS and below 2**3072, but not
        # necessarily below _MODULUS; hexdigest() reduces it the rest of the way.
        self._product = 1
        self._count = 0

    def add_line(self, line: str) -> None:
        """Fold in one serialised record (:func:`record_line`, or a canonical Bronze line)."""
        element = hashlib.shake_256(_DOMAIN + line.encode("utf-8")).digest(_ELEMENT_BYTES)
        value = int.from_bytes(element, "big")
        # value < 2**3072 < 2 * _MODULUS, so one subtraction is ``value % _MODULUS``.
        if value >= _MODULUS:
            value -= _MODULUS
        # A zero would erase the product; its probability is 2**-3072, but map it anyway.
        product = self._product * (value or 1)
        # Reduce modulo 2**3072 - _FOLD without dividing: high * 2**3072 + low is
        # congruent to high * _FOLD + low. Each pass removes about 3051 bits.
        while product > _LOW_MASK:
            product = (product >> _ELEMENT_BITS) * _FOLD + (product & _LOW_MASK)
        self._product = product
        self._count += 1

    def add(self, record: Mapping[str, JsonValue]) -> None:
        self.add_line(record_line(record))

    @property
    def count(self) -> int:
        return self._count

    def hexdigest(self) -> str:
        final = hashlib.sha256(_DOMAIN)
        final.update(self._count.to_bytes(8, "big"))
        product = self._product
        if product >= _MODULUS:
            product -= _MODULUS
        final.update(product.to_bytes(_ELEMENT_BYTES, "big"))
        return f"sha256:{final.hexdigest()}"


def multiset_checksum(records: Iterable[Mapping[str, JsonValue]]) -> str:
    """The ``canonical-multiset-v2`` checksum of ``records``."""
    checksum = MultisetChecksum()
    for record in records:
        checksum.add(record)
    return checksum.hexdigest()


def multiset_checksum_of_jsonl(path: Path) -> str:
    """The ``canonical-multiset-v2`` checksum of a canonical Bronze file, in one pass."""
    checksum = MultisetChecksum()
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line:
                checksum.add_line(line)
    return checksum.hexdigest()


def algorithm_of(entry: Mapping[str, object]) -> str:
    """The algorithm a provenance entry's checksum was made with.

    An entry without ``data_checksum_algorithm`` predates it and used the legacy one.
    """
    value = entry.get("data_checksum_algorithm")
    return value if isinstance(value, str) and value else LEGACY_ALGORITHM


def fingerprint_algorithm_of(manifest: Mapping[str, object]) -> str:
    """The algorithm a manifest's ``inputs_fingerprint`` was made with."""
    value = manifest.get("inputs_fingerprint_algorithm")
    return value if isinstance(value, str) and value else LEGACY_FINGERPRINT_ALGORITHM


def same_data(left: Mapping[str, object], right: Mapping[str, object]) -> bool | None:
    """Whether two provenance entries fetched the same records.

    None when the question cannot be answered: a checksum is missing, or the two were
    made by different algorithms. Never compare the strings directly.
    """
    a, b = left.get("data_checksum"), right.get("data_checksum")
    if not isinstance(a, str) or not isinstance(b, str):
        return None
    if algorithm_of(left) != algorithm_of(right):
        return None
    return a == b


__all__ = [
    "ALGORITHMS",
    "CURRENT_ALGORITHM",
    "FINGERPRINT_ALGORITHM",
    "LEGACY_ALGORITHM",
    "LEGACY_FINGERPRINT_ALGORITHM",
    "MULTISET_ALGORITHM",
    "MultisetChecksum",
    "algorithm_of",
    "fingerprint_algorithm_of",
    "multiset_checksum",
    "multiset_checksum_of_jsonl",
    "record_line",
    "same_data",
]
