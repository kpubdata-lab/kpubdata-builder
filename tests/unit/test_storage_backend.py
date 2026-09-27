""", CUBRID URL , serve   (ADR 0016).

 SQLAlchemy  CUBRID    —   URL ,
``validate_storage_config()``  **   **
.    ``tests/cubrid/test_cubrid_fail_closed.py``.
"""

from __future__ import annotations

import logging

import pytest

from kpubdata_builder.store.backend import (
    cubrid_url,
    normalize_cubrid_url,
    storage_backend,
    validate_storage_config,
)

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"
_TAIL = "dba:@127.0.0.1:33000/kpubdata?charset=utf8"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (_BACKEND_ENV, _URL_ENV):
        monkeypatch.delenv(key, raising=False)


class TestStorageBackendSelection:
    def test_defaults_to_sqlite(self) -> None:
        assert storage_backend() == "sqlite"

    @pytest.mark.parametrize("value", ["sqlite", "cubrid", "CUBRID", " cubrid "])
    def test_accepts_known_backends(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, value)
        assert storage_backend() == value.strip().lower()

    def test_rejects_unknown_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "postgres")
        with pytest.raises(RuntimeError, match="must be 'sqlite' or 'cubrid'"):
            storage_backend()


class TestCubridUrlDriver:
    """URL   pycubrid    (ADR 0016).

    sqlalchemy-cubrid  `cubrid`/`cubrid.cubrid`/`cubrid.cubriddb`  legacy
    C-extension(`CUBRIDdb`) dialect , `cubrid.pycubrid`
    . `[cubrid]` extra  pycubrid
    ImportError   —    .
    """

    def test_pycubrid_url_passes_through(self) -> None:
        url = f"cubrid+pycubrid://{_TAIL}"
        assert normalize_cubrid_url(url) == url

    def test_bare_cubrid_url_is_normalized(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert normalize_cubrid_url(f"cubrid://{_TAIL}") == f"cubrid+pycubrid://{_TAIL}"
        #    —      .
        assert any("pycubrid" in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize("driver", ["cubriddb", "cubrid"])
    def test_rejects_c_extension_drivers(self, driver: str) -> None:
        with pytest.raises(RuntimeError, match="pycubrid"):
            normalize_cubrid_url(f"cubrid+{driver}://{_TAIL}")

    def test_rejects_async_driver(self) -> None:
        # Engine   — async dialect    .
        with pytest.raises(RuntimeError, match="synchronous"):
            normalize_cubrid_url(f"cubrid+aiopycubrid://{_TAIL}")

    def test_rejects_other_dialects(self) -> None:
        with pytest.raises(RuntimeError, match="dialect"):
            normalize_cubrid_url("postgresql+psycopg://user:pass@host/db")

    def test_rejects_url_without_scheme(self) -> None:
        with pytest.raises(RuntimeError, match="SQLAlchemy URL"):
            normalize_cubrid_url("127.0.0.1:33000/kpubdata")


class TestCubridUrlFromEnv:
    def test_missing_url_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        with pytest.raises(RuntimeError, match=_URL_ENV):
            cubrid_url()

    def test_env_url_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        monkeypatch.setenv(_URL_ENV, f"  cubrid://{_TAIL}  ")
        assert cubrid_url() == f"cubrid+pycubrid://{_TAIL}"


class TestValidateStorageConfig:
    """``serve()``    (#587, ADR 0016).

        **  **  —  dev
    (sqlalchemy  )  ,
    ``tests/cubrid/test_cubrid_fail_closed.py``  .
    """

    def test_is_noop_for_the_default_backend(self) -> None:
        # sqlite   optional    ( ).
        assert validate_storage_config() is None

    def test_is_noop_for_explicit_sqlite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "sqlite")
        assert validate_storage_config() is None

    def test_refuses_to_start_without_a_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        with pytest.raises(RuntimeError, match=_URL_ENV):
            validate_storage_config()

    @pytest.mark.parametrize("driver", ["cubriddb", "cubrid", "aiopycubrid"])
    def test_refuses_to_start_on_an_unsupported_driver(
        self, driver: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        #   URL     sqlalchemy   .
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        monkeypatch.setenv(_URL_ENV, f"cubrid+{driver}://{_TAIL}")
        with pytest.raises(RuntimeError):
            validate_storage_config()

    def test_refuses_to_start_on_an_unknown_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "postgres")
        with pytest.raises(RuntimeError, match="must be 'sqlite' or 'cubrid'"):
            validate_storage_config()
