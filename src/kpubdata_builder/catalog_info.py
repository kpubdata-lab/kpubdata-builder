"""What the kpubdata catalog says about a dataset, for dataset cards (#694).

Read from the installed kpubdata's local catalog — no network, no key — and cached: it
does not change while Builder runs.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetCatalogInfo:
    """The public facts kpubdata declares for a dataset (#609, kpubdata#525)."""

    source_url: str | None
    license_type: str | None
    attribution: str | None


@functools.lru_cache(maxsize=4096)
def catalog_info(dataset_id: str) -> DatasetCatalogInfo | None:
    """``dataset_id``'s catalog facts, or None when the dataset is not in the catalog."""
    import kpubdata

    client = kpubdata.Client()
    try:
        ref = client.dataset(dataset_id).ref
    except Exception:  # kpubdata raises its own not-found errors
        return None
    finally:
        client.close()
    license_spec = getattr(ref, "license", None)
    return DatasetCatalogInfo(
        source_url=getattr(ref, "source_url", None),
        license_type=getattr(license_spec, "type", None) if license_spec else None,
        attribution=getattr(license_spec, "attribution", None) if license_spec else None,
    )


__all__ = ["DatasetCatalogInfo", "catalog_info"]
