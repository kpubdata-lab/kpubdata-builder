"""What every SQLite state store connects with (#1096).

Ten modules open a store (``store/inventory.py``), and each chose how long a connection
waits for a lock: two waited five seconds and the rest thirty. Nothing said why the two
differed, and a locked store then failed some requests after five seconds while others,
behind the same lock, were still waiting.

One wait for all of them is written here. It is thirty seconds — what eight of the ten
already used, and no longer than a request's socket may stay open
(``service/http.py``): a writer that holds a store for longer than that has failed, and
waiting further would only hide it.

This module imports nothing of the package, so any store can import it.
"""

from __future__ import annotations

#: Seconds a connection waits for a lock before the statement fails.
BUSY_TIMEOUT_SECONDS = 30.0

__all__ = ["BUSY_TIMEOUT_SECONDS"]
