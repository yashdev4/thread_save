"""Envelope encryption and key derivation for custodial security (§2 S8, Milestone X6).

Provides:
- HKDF key derivation per account from master key
- AES-256-GCM authenticated encryption with unique random nonces
- Transparent base64url encoding for database storage
"""

from __future__ import annotations

import base64
import os
from typing import Union
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_account_key(
    master_key: Union[str, bytes],
    account_id: str,
    salt: bytes = b"threadvault-custodial-salt",
) -> bytes:
    """Derive a 256-bit account-specific encryption key using HKDF-SHA256."""
    key_bytes = master_key.encode("utf-8") if isinstance(master_key, str) else master_key
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=f"threadvault.account.{account_id}".encode("utf-8"),
    )
    return hkdf.derive(key_bytes)


def encrypt_body(plaintext: str, key: bytes) -> str:
    """Encrypt plaintext using AES-256-GCM.

    Returns base64url-encoded string: nonce (12 bytes) + ciphertext + auth tag.
    """
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # Standard 96-bit nonce for AES-GCM
    data = plaintext.encode("utf-8")
    ct = aesgcm.encrypt(nonce, data, None)
    payload = nonce + ct
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decrypt_body(ciphertext_b64: str, key: bytes) -> str:
    """Decrypt base64url-encoded AES-256-GCM ciphertext.

    Raises ValueError on tampering or invalid key.
    """
    try:
        payload = base64.urlsafe_b64decode(ciphertext_b64.encode("ascii"))
        if len(payload) < 28:  # 12 nonce + at least 16 auth tag
            raise ValueError("Ciphertext too short")
        nonce = payload[:12]
        ct = payload[12:]
        aesgcm = AESGCM(key)
        decrypted = aesgcm.decrypt(nonce, ct, None)
        return decrypted.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e
