"""Credential encryption boundary.

Store records only nonce and AES-GCM ciphertext/tag. Master key is injected from
separate configuration, not recorded in this module or DB.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12


class CredentialCryptoError(ValueError):
    """Credential encryption/decryption configuration or integrity error."""


class CredentialCipher(Protocol):
    """Authentication cipher interface used by repository."""

    def encrypt(self, plaintext: str, *, associated_data: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes, *, associated_data: bytes) -> str: ...


class AesGcmCredentialCipher:
    """256-bit AES-GCM credential cipher."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise CredentialCryptoError("credential master key must decode to exactly 32 bytes")
        self._cipher = AESGCM(master_key)

    @classmethod
    def from_base64(cls, encoded_key: str) -> AesGcmCredentialCipher:
        """Create cipher from URL-safe base64 master key."""
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise CredentialCryptoError("credential master key must be URL-safe base64") from exc
        return cls(key)

    def encrypt(self, plaintext: str, *, associated_data: bytes) -> bytes:
        if not plaintext:
            raise CredentialCryptoError("credential must not be empty")
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._cipher.encrypt(nonce, plaintext.encode("utf-8"), associated_data)

    def decrypt(self, ciphertext: bytes, *, associated_data: bytes) -> str:
        if len(ciphertext) <= _NONCE_BYTES:
            raise CredentialCryptoError("stored credential is invalid")
        nonce, encrypted = ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:]
        try:
            plaintext = self._cipher.decrypt(nonce, encrypted, associated_data)
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise CredentialCryptoError("stored credential failed integrity validation") from exc


__all__ = ["AesGcmCredentialCipher", "CredentialCipher", "CredentialCryptoError"]
