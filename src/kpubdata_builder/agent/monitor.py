"""Monitor pending dataset applications for approval.

Periodically tests API endpoints that previously returned
NEEDS_APPLICATION to detect when access has been granted.

A dataset with a spec is checked by ``verify_dataset``. A dataset kpubdata serves from
an adapter catalogue has no spec (#1288): it is asked of kpubdata's public
``Client.probe`` first, and when probe has nothing to say about it — kpubdata 0.10 probes
only spec datasets — of one ``list`` call for a single record, read through kpubdata's
canonical exceptions. A check that cannot tell an approval from a refusal (a rate
limit, a timeout, a provider error, no key) gives no verdict, and the dataset keeps the
status it had.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

#: Seconds between two checks of ``monitor --check``: the datasets of one provider
#: service are checked back to back, and a provider throttles an address that does so.
CHECK_INTERVAL_SECONDS = 1.0
#: A check that gets no answer within this long gives no verdict rather than holding
#: the run; kpubdata's probe uses the same fast-fail budget.
CHECK_TIMEOUT_SECONDS = 15.0

#: kpubdata's ``PROBE_STATUSES`` that are a verdict on the key, as monitor statuses.
#: The others — ``auth_unknown`` (no key), ``params_invalid``, ``rate_limited``,
#: ``temporarily_unavailable``, ``network_error``, ``insufficient_metadata`` — say
#: nothing about an application and give no verdict.
_PROBE_VERDICTS = {
    "available": "HEALTHY",
    "application_required": "NEEDS_APPLICATION",
    "retired": "BROKEN_ENDPOINT",
}


class _Probed(Protocol):
    @property
    def status(self) -> str: ...


class _Listable(Protocol):
    def list(self, **kwargs: object) -> object: ...


class CatalogueClient(Protocol):
    """The part of kpubdata's ``Client`` a check of a dataset without a spec uses."""

    def probe(self, dataset_id: str) -> _Probed | None: ...

    def dataset(self, dataset_id: str) -> _Listable: ...


@dataclass(slots=True)
class PendingDataset:
    """A dataset awaiting API application approval."""

    dataset_id: str
    status: str  # NEEDS_APPLICATION, WAITING_APPROVAL, etc.
    applied_at: str | None = None
    last_checked: str | None = None
    data_go_kr_url: str = ""
    note: str = ""


@dataclass(slots=True)
class MonitorState:
    """Persistent state for the approval monitor."""

    pending: list[PendingDataset] = field(default_factory=list)

    def save(self, path: Path) -> None:
        """Save state to a YAML file."""
        data = {
            "pending": [
                {
                    "dataset_id": p.dataset_id,
                    "status": p.status,
                    "applied_at": p.applied_at,
                    "last_checked": p.last_checked,
                    "data_go_kr_url": p.data_go_kr_url,
                    "note": p.note,
                }
                for p in self.pending
            ]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    @classmethod
    def load(cls, path: Path) -> MonitorState:
        """Load state from a YAML file."""
        if not path.is_file():
            return cls()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        pending = []
        for item in data.get("pending", []):
            pending.append(
                PendingDataset(
                    dataset_id=item["dataset_id"],
                    status=item.get("status", "NEEDS_APPLICATION"),
                    applied_at=item.get("applied_at"),
                    last_checked=item.get("last_checked"),
                    data_go_kr_url=item.get("data_go_kr_url", ""),
                    note=item.get("note", ""),
                )
            )
        return cls(pending=pending)

    def add(self, dataset_id: str, *, status: str = "NEEDS_APPLICATION", **kwargs: str) -> None:
        """Add or update a pending dataset."""
        for p in self.pending:
            if p.dataset_id == dataset_id:
                p.status = status
                for k, v in kwargs.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
                return
        self.pending.append(PendingDataset(dataset_id=dataset_id, status=status, **kwargs))

    def remove(self, dataset_id: str) -> PendingDataset | None:
        """Remove a dataset from pending (e.g. after approval)."""
        for i, p in enumerate(self.pending):
            if p.dataset_id == dataset_id:
                return self.pending.pop(i)
        return None


@contextmanager
def open_catalogue_client(api_key: str | None = None) -> Iterator[CatalogueClient]:
    """A kpubdata client holding the data.go.kr key, and nothing else, for one run.

    The key is ``api_key`` or the one kpubdata reads from the environment
    (``KPUBDATA_DATAGO_API_KEY``). ``localdata``, ``lofin`` and ``semas`` call with
    ``datago``'s key, so it is given under that name (#1066). It is registered with the
    log redaction while the client is open, and never printed.
    """
    from kpubdata import Client, KPubDataConfig

    from .. import logging_redaction

    key = api_key or KPubDataConfig.from_env().get_provider_key("datago")
    client = Client(
        provider_keys={"datago": key} if key else None,
        env_keys=False,
        cache=False,
        timeout=CHECK_TIMEOUT_SECONDS,
        max_retries=0,
    )
    logging_redaction.register(client, (key,) if key else ())
    try:
        yield client
    finally:
        try:
            client.close()
        finally:
            logging_redaction.release(client)


def _classify_list_error(exc: Exception) -> str | None:
    """The monitor status a failed ``list`` call shows, or ``None`` for no verdict."""
    from kpubdata import AuthError, RateLimitError, TransportError

    from kpubdata_builder.verify.runner import _classify_auth_error

    if isinstance(exc, RateLimitError):
        return None
    if isinstance(exc, AuthError):
        return _classify_auth_error(exc).value
    # Some kpubdata adapters (semas, localdata in 0.10) let data.go.kr's refusal of a
    # key through as a plain TransportError carrying the status, not an AuthError.
    if isinstance(exc, TransportError) and exc.status_code in (401, 403):
        return "INVALID_KEY" if exc.status_code == 401 else "NEEDS_APPLICATION"
    return None


def _check_catalogue_dataset(dataset_id: str, client: CatalogueClient) -> str | None:
    from kpubdata import DatasetNotFoundError, ProviderNotRegisteredError, PublicDataError

    from .. import egress

    provider = dataset_id.partition(".")[0]
    # One call to the provider under its egress limit (#1245), shared with builds:
    # probe makes no call for a dataset it cannot probe, so this is the list's turn.
    egress.wait_for_call(provider)
    try:
        probed = client.probe(dataset_id)
        if probed is not None:
            return _PROBE_VERDICTS.get(probed.status)
        dataset = client.dataset(dataset_id)
    except (DatasetNotFoundError, ProviderNotRegisteredError):
        return "SPEC_NOT_FOUND"
    try:
        dataset.list(page_size=1)
    except PublicDataError as exc:
        return _classify_list_error(exc)
    return "HEALTHY"


def check_approval(
    dataset_id: str,
    *,
    api_key: str | None = None,
    client: CatalogueClient | None = None,
) -> str | None:
    """Check if a dataset API application has been approved.

    A dataset with a spec is tested by ``verify_dataset``; one without is checked
    through ``client`` (see the module docstring), a client opened here when none is
    given.

    Returns:
        Status string: HEALTHY, NEEDS_APPLICATION, INVALID_KEY, etc.;
        ``SPEC_NOT_FOUND`` when kpubdata does not know the dataset; ``None`` when the
        check gave no verdict and the dataset's status should stay as it was.
    """
    from kpubdata import find_spec

    from kpubdata_builder.verify import verify_dataset

    spec = find_spec(dataset_id)
    if spec is not None:
        result = verify_dataset(spec, api_key=api_key, page_size=1)
        return result.status.value
    if client is not None:
        return _check_catalogue_dataset(dataset_id, client)
    with open_catalogue_client(api_key) as opened:
        return _check_catalogue_dataset(dataset_id, opened)
