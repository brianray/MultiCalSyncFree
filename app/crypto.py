"""Encryption for OAuth refresh tokens at rest.

Tokens are encrypted with Fernet (AES-128-CBC + HMAC) before being written to
GCS. The key comes from Secret Manager via the TOKEN_ENCRYPTION_KEY env var.
If no key is configured we fall back to a deterministic key derived from the
session secret so local development still works -- with a loud warning.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)


class TokenCipher:
    def __init__(self, key: str | None, fallback_secret: str = "") -> None:
        if not key:
            log.warning("TOKEN_ENCRYPTION_KEY not set; deriving a key from SESSION_SECRET (dev only)")
            key = base64.urlsafe_b64encode(hashlib.sha256(fallback_secret.encode()).digest()).decode()
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Could not decrypt token; TOKEN_ENCRYPTION_KEY may have changed") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()
