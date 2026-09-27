"""``AesGcmCredentialCipher`` rejection path regression test (#593).

This module is a cryptographic boundary wrapping provider credentials.
Only normal round-trip was tested; if rejection logic breaks (accepting
wrong keys or overlooking tampering), the suite stays green. This tests
only "what must be rejected".
"""

from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kpubdata_builder.credentials import AesGcmCredentialCipher
from kpubdata_builder.credentials.crypto import CredentialCryptoError

_KEY = b"k" * 32
_AAD = b"owner-1:datago"


def _cipher() -> AesGcmCredentialCipher:
    return AesGcmCredentialCipher(_KEY)


# --- master key validation -------------------------------------------------------


@pytest.mark.parametrize("size", [0, 16, 24, 31, 33, 64])
def test_master_key_must_be_exactly_32_bytes(size: int) -> None:
    """Only AES-256 allowed — 16/24 bytes (AES-128/192) also rejected."""
    with pytest.raises(CredentialCryptoError, match="exactly 32 bytes"):
        AesGcmCredentialCipher(b"k" * size)


def test_from_base64_accepts_urlsafe_key() -> None:
    encoded = base64.urlsafe_b64encode(_KEY).decode("ascii")
    cipher = AesGcmCredentialCipher.from_base64(encoded)
    assert cipher.decrypt(cipher.encrypt("secret", associated_data=_AAD), associated_data=_AAD) == (
        "secret"
    )


def test_from_base64_rejects_malformed_base64() -> None:
    """Broken base64 padding: binascii.Error → CredentialCryptoError."""
    with pytest.raises(CredentialCryptoError, match="URL-safe base64"):
        AesGcmCredentialCipher.from_base64("AAAAA")


def test_from_base64_rejects_non_ascii_key() -> None:
    """Non-ASCII chars caught at encode — UnicodeEncodeError also wrapped as same error."""
    with pytest.raises(CredentialCryptoError, match="URL-safe base64"):
        AesGcmCredentialCipher.from_base64("마스터키")


def test_from_base64_propagates_length_check() -> None:
    """Key with correct format but doesn't resolve to 32 bytes rejected at length validation."""
    encoded = base64.urlsafe_b64encode(b"short").decode("ascii")
    with pytest.raises(CredentialCryptoError, match="exactly 32 bytes"):
        AesGcmCredentialCipher.from_base64(encoded)


# --- encrypt ---------------------------------------------------------------


def test_encrypt_rejects_empty_plaintext() -> None:
    """Saving empty credential makes 'set' and 'empty' indistinguishable."""
    with pytest.raises(CredentialCryptoError, match="must not be empty"):
        _cipher().encrypt("", associated_data=_AAD)


def test_encrypt_uses_a_fresh_nonce_each_call() -> None:
    """Even same plaintext, ciphertext must differ (nonce reuse = AES-GCM fatal)."""
    cipher = _cipher()
    first = cipher.encrypt("secret", associated_data=_AAD)
    second = cipher.encrypt("secret", associated_data=_AAD)
    assert first[:12] != second[:12]
    assert first != second


# --- decrypt ---------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 11, 12])
def test_decrypt_rejects_ciphertext_shorter_than_nonce(size: int) -> None:
    """Value without even nonce/tag rejected before slicing."""
    with pytest.raises(CredentialCryptoError, match="stored credential is invalid"):
        _cipher().decrypt(b"\x00" * size, associated_data=_AAD)


def test_decrypt_rejects_tampered_ciphertext() -> None:
    """Flip one byte of body and GCM tag validation fails with InvalidTag."""
    cipher = _cipher()
    blob = bytearray(cipher.encrypt("secret", associated_data=_AAD))
    blob[-1] ^= 0x01
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        cipher.decrypt(bytes(blob), associated_data=_AAD)


def test_decrypt_rejects_tampered_nonce() -> None:
    cipher = _cipher()
    blob = bytearray(cipher.encrypt("secret", associated_data=_AAD))
    blob[0] ^= 0x01
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        cipher.decrypt(bytes(blob), associated_data=_AAD)


def test_decrypt_rejects_mismatched_associated_data() -> None:
    """AAD binds credential to owner/provider — must not decrypt for different owner."""
    cipher = _cipher()
    blob = cipher.encrypt("secret", associated_data=b"owner-1:datago")
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        cipher.decrypt(blob, associated_data=b"owner-2:datago")


def test_decrypt_rejects_ciphertext_from_another_key() -> None:
    """Rotating master key prevents decryption of existing credentials
    (see README rotation warning)."""
    blob = AesGcmCredentialCipher(b"a" * 32).encrypt("secret", associated_data=_AAD)
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        AesGcmCredentialCipher(b"b" * 32).decrypt(blob, associated_data=_AAD)


def test_decrypt_rejects_non_utf8_plaintext() -> None:
    """tag correct but body not utf-8 (row injected externally) also wrapped as same error."""
    nonce = os.urandom(12)
    blob = nonce + AESGCM(_KEY).encrypt(nonce, b"\xff\xfe", _AAD)
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        _cipher().decrypt(blob, associated_data=_AAD)


def test_roundtrip_preserves_unicode_credential() -> None:
    cipher = _cipher()
    secret = "서비스키-Ω-🔑"
    assert cipher.decrypt(cipher.encrypt(secret, associated_data=_AAD), associated_data=_AAD) == (
        secret
    )
