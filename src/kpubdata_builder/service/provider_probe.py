"""Probe a provider with the key a request carries, and keep nothing (#802).

``POST /providers/{provider}/probe`` tells a user what the key they just typed can reach:
per source dataset, one of kpubdata's ``PROBE_STATUSES`` — available, application
required, parameters to check and so on. It differs from the connection test in three
ways:

- **Only the request's key is used.** It comes in the ``X-Provider-Key`` header (#683).
  A stored credential or one in the server's environment is never tried, in any
  deployment mode: the client is built with that one key and ``env_keys=False``.
- **Nothing about the key is kept.** The client is closed when the request ends, the
  result is not stored, and the response holds nothing derived from the key, so a caller
  may keep it under the account.
- **It answers per dataset**, with the provider service each belongs to — an application
  at the provider is granted per service, so datasets of one service share a verdict.

kpubdata makes one call per dataset with a fast-fail transport (15 s, no retry, no cache).
A provider that is down would hold the request for that long per dataset, so the probe
stops starting new calls after ``PROBE_BUDGET_SECONDS`` and names what it did not reach.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .. import logging_redaction
from ..spec import JsonValue
from .redaction import redact_secret_text

#: After this long no further dataset is probed in one request.
PROBE_BUDGET_SECONDS = 45.0
#: Most datasets one request may name.
MAX_PROBE_DATASETS = 50


class ProbeOutcome(Protocol):
    """What the probe reads off kpubdata's ``ProbeResult``."""

    @property
    def service_id(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def detail(self) -> str: ...
    @property
    def http_status(self) -> int | None: ...


ProbeOne = Callable[[str], ProbeOutcome | None]
"""``dataset_id -> outcome``, or None when the dataset has no spec to probe."""

OpenProbe = Callable[[str, str], AbstractContextManager[ProbeOne]]
"""``(provider, key) -> probe``, open for one request."""

ListDatasets = Callable[[str], Sequence[str]]
"""``provider -> the dataset ids ("<provider>.<name>") that can be probed``."""


def spec_dataset_ids(provider: str) -> list[str]:
    """Ids of the spec-defined datasets of ``provider`` — the ones kpubdata can probe."""
    from kpubdata import discover_specs

    return sorted(spec.id for spec in discover_specs() if spec.provider == provider)


@contextmanager
def open_kpubdata_probe(provider: str, key: str) -> Iterator[ProbeOne]:
    """A kpubdata client holding ``key`` for ``provider`` and nothing else.

    kpubdata looks a key up under the name of the provider that owns it: ``localdata``,
    ``lofin`` and ``semas`` call with ``datago``'s. Given under the probed provider's own
    name, the key was never found and every dataset answered ``auth_unknown`` (#1066).
    """
    from kpubdata import Client

    from .providers import CredentialResolver

    slot = CredentialResolver.client_key_slot(provider)
    client = Client(provider_keys={slot: key}, cache=False, env_keys=False)
    # A provider that puts the key in a path or echoes it would otherwise reach the log.
    logging_redaction.register(client, (key,))
    try:
        yield client.probe
    finally:
        try:
            client.close()
        finally:
            logging_redaction.release(client)


@dataclass(frozen=True)
class ProbeRequest:
    """The datasets a probe request asks for; ``None`` means every one of the provider."""

    datasets: tuple[str, ...] | None


def parse_probe_body(body: Mapping[str, JsonValue] | None) -> ProbeRequest | str:
    """The request body, or the reason it is refused."""
    if body is None or not body:
        return ProbeRequest(None)
    if set(body) != {"datasets"}:
        return "the body may hold 'datasets' only"
    datasets = body["datasets"]
    if not isinstance(datasets, list) or not datasets:
        return "datasets must be a non-empty list of dataset names"
    if len(datasets) > MAX_PROBE_DATASETS:
        return f"datasets may name at most {MAX_PROBE_DATASETS} datasets"
    names: list[str] = []
    for item in datasets:
        if not isinstance(item, str) or not item.strip():
            return "datasets must be a non-empty list of dataset names"
        names.append(item.strip())
    return ProbeRequest(tuple(dict.fromkeys(names)))


def run_probe(
    provider: str,
    key: str,
    dataset_ids: Sequence[str],
    *,
    open_probe: OpenProbe,
    budget_seconds: float = PROBE_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, JsonValue]:
    """Probe ``dataset_ids`` in order and return the response body.

    A dataset the time budget did not reach is listed in ``not_probed`` and ``complete``
    is false; it gets no status, because nothing was observed about it.
    """
    prefix = f"{provider}."
    results: list[JsonValue] = []
    not_probed: list[JsonValue] = []
    started = clock()
    with open_probe(provider, key) as probe_one:
        for dataset_id in dataset_ids:
            name = dataset_id.removeprefix(prefix)
            if clock() - started >= budget_seconds:
                not_probed.append(name)
                continue
            outcome = probe_one(dataset_id)
            if outcome is None:
                not_probed.append(name)
                continue
            results.append(
                {
                    "dataset": name,
                    "service_id": outcome.service_id,
                    "status": outcome.status,
                    # kpubdata keeps a configured key out of ``detail``; this does not
                    # rely on it.
                    "detail": redact_secret_text(outcome.detail, (key,)) or "",
                    "http_status": outcome.http_status,
                }
            )
    return {
        "provider": provider,
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "complete": not not_probed,
        "datasets": results,
        "not_probed": not_probed,
    }
