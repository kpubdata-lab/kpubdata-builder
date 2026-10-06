"""Monitor pending dataset applications for approval.

Periodically tests API endpoints that previously returned
NEEDS_APPLICATION to detect when access has been granted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


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


def check_approval(dataset_id: str, *, api_key: str | None = None) -> str:
    """Check if a dataset API application has been approved.

    Uses kpubdata's verify infrastructure to test the endpoint.

    Returns:
        Status string: HEALTHY, NEEDS_APPLICATION, INVALID_KEY, etc.
    """
    from kpubdata import find_spec

    from kpubdata_builder.verify import verify_dataset

    spec = find_spec(dataset_id)
    if spec is None:
        return "SPEC_NOT_FOUND"

    result = verify_dataset(spec, api_key=api_key, page_size=1)
    return result.status.value
