"""Log records carry no provider key, whatever logger writes them (#686)."""

from __future__ import annotations

import logging

import pytest

from kpubdata_builder import logging_redaction


@pytest.fixture(autouse=True)
def _installed() -> None:
    logging_redaction.install()


def _logged(caplog: pytest.LogCaptureFixture, logger: str, message: str, *args: object) -> str:
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger(logger).info(message, *args)
    return caplog.text


def test_a_credential_query_parameter_is_masked_by_name(caplog: pytest.LogCaptureFixture) -> None:
    """The line httpx itself writes for every request."""
    text = _logged(
        caplog,
        "httpx",
        'HTTP Request: GET %s "HTTP/1.1 200 OK"',
        "http://apis.data.go.kr/x?serviceKey=abc%2B%2F123%3D%3D&pageNo=1",
    )

    assert "abc%2B" not in text
    assert "serviceKey=[REDACTED]" in text
    assert "pageNo=1" in text


def test_an_ordinary_parameter_is_left_alone(caplog: pytest.LogCaptureFixture) -> None:
    text = _logged(caplog, "httpx", "GET http://h/x?district_code=11110&keyword=bus")

    assert "district_code=11110" in text and "keyword=bus" in text


def test_a_key_in_a_path_segment_is_masked_while_its_client_is_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Providers such as seoul put the key in the path (kpubdata #354)."""
    client = object()
    logging_redaction.register(client, ["pathKey987654"])
    try:
        text = _logged(caplog, "kpubdata.transport", "GET http://h/pathKey987654/json/1/5")
        assert "pathKey987654" not in text
    finally:
        logging_redaction.release(client)

    assert logging_redaction.active_count() == 0
    assert "pathKey987654" in _logged(caplog, "x", "after close: pathKey987654")


def test_a_traceback_is_scrubbed_too(caplog: pytest.LogCaptureFixture) -> None:
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        try:
            raise RuntimeError("failed GET http://h/x?serviceKey=secretvalue1")
        except RuntimeError:
            logging.getLogger("kpubdata_builder.x").exception("boom")

    assert "secretvalue1" not in caplog.text


def test_install_is_idempotent() -> None:
    factory = logging.getLogRecordFactory()
    logging_redaction.install()

    assert logging.getLogRecordFactory() is factory


def test_credential_carriers_keep_the_key_out_of_their_repr() -> None:
    """docs/CREDENTIAL_SURFACE.md item 18: both used to print the plaintext."""
    from kpubdata_builder.service.providers import ResolvedCredential
    from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

    assert "reprKey1" not in repr(ResolvedCredential("user", "reprKey1"))
    assert "reprKey2" not in repr(PublishCredentialResolution(values={"HF_TOKEN": "reprKey2"}))
