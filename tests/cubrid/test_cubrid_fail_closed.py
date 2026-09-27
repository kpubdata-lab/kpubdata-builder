"""serve startup fail-closed gate validation on real driver/server (#587, ADR 0016).

validate_storage_config() goes beyond URL/driver checks to **actually open
a connection**. With only config correct and server down, startup appears
successful and dies on the first request; write failures are best-effort
swallowed so state is silently lost.

tests/unit/test_storage_backend.py covers the pre-connection branch
(backend selection, URL, driver), and this file covers the part after —
only verifiable when real driver is installed. Marked cubrid; runs in
dedicated CI job (cubrid.yml).

- **Connection refused** is validated even without a server (closed port →
  immediate failure). Runs locally too.
- **Connection success** requires real server; skips if URL is absent.
  But KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID=1 (set by dedicated CI job)
  fails instead of skipping — prevents silent passing when URL injection
  is omitted.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

pytest.importorskip("sqlalchemy")

from kpubdata_builder.store.backend import (  # noqa: E402
    dispose_engine,
    get_engine,
    validate_storage_config,
)

pytestmark = pytest.mark.cubrid

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"
_REQUIRE_REAL_ENV = "KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID"

_DRIVER_INSTALLED = importlib.util.find_spec("sqlalchemy_cubrid") is not None

# Port 1 is not a CUBRID broker port, so connection is immediately refused
# (no timeout wait).
_UNREACHABLE_URL = "cubrid+pycubrid://dba:@127.0.0.1:1/kpubdata?charset=utf8"


def _require_real_cubrid() -> bool:
    """Whether running in the dedicated CI job. If true, forbids skip without URL (#587)."""
    return os.environ.get(_REQUIRE_REAL_ENV, "").strip().lower() in ("1", "true", "yes")


@pytest.fixture(autouse=True)
def _dispose_global_engine():  # type: ignore[no-untyped-def]
    """Global Engine is process-scoped, leaks across tests — dispose before and after."""
    dispose_engine()
    yield
    dispose_engine()


@pytest.mark.skipif(not _DRIVER_INSTALLED, reason="sqlalchemy-cubrid 미설치")
def test_startup_accepts_a_reachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get(_URL_ENV, "").strip()
    if not url:
        if _require_real_cubrid():
            raise AssertionError(
                f"{_REQUIRE_REAL_ENV} is set but {_URL_ENV} is empty — this run would skip "
                "the only check that proves the startup gate can reach a real CUBRID server."
            )
        pytest.skip(f"{_URL_ENV} 미설정 — 실 서버 검증은 전용 CI 잡에서 수행")
    monkeypatch.setenv(_BACKEND_ENV, "cubrid")

    validate_storage_config()

    # Gate opened a connection but returns it; Engine must stay alive — serve uses
    # this same pool next.
    assert get_engine() is get_engine()


@pytest.mark.skipif(not _DRIVER_INSTALLED, reason="sqlalchemy-cubrid 미설치")
def test_startup_refuses_an_unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup refuses server even if URL and driver are valid but unreachable.

    This test is the regression gate: removing connection check here breaks.
    """
    monkeypatch.setenv(_BACKEND_ENV, "cubrid")
    monkeypatch.setenv(_URL_ENV, _UNREACHABLE_URL)

    with pytest.raises(RuntimeError, match="not reachable"):
        validate_storage_config()

    # Failed Engine disposed, retry fails cleanly with same error — doesn't mutate
    # to different error from holding dead pool.
    with pytest.raises(RuntimeError, match="not reachable"):
        validate_storage_config()
