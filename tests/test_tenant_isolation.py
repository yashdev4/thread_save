"""Verification suite for Milestone X6: envelope encryption & account key derivation (AES-256-GCM + HKDF)."""

import pytest

from thread_save.security.encryption import (
    derive_account_key,
    encrypt_body,
    decrypt_body,
)


@pytest.mark.asyncio
async def test_x6_envelope_encryption():
    """Verify AES-256-GCM envelope encryption and account key derivation."""
    master_key = "global-master-encryption-key-for-threadvault-32bytes!"
    key_alice = derive_account_key(master_key, "alice")
    key_bob = derive_account_key(master_key, "bob")

    assert key_alice != key_bob
    assert len(key_alice) == 32
    assert len(key_bob) == 32

    plaintext = "Sensitive conversation turn content that requires encryption."
    ciphertext = encrypt_body(plaintext, key_alice)
    assert ciphertext != plaintext

    # Decrypt with Alice's key succeeds
    decrypted = decrypt_body(ciphertext, key_alice)
    assert decrypted == plaintext

    # Decrypt with Bob's key fails
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt_body(ciphertext, key_bob)

    # Tampered ciphertext fails
    tampered = ciphertext[:-4] + "AAAA"
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt_body(tampered, key_alice)

