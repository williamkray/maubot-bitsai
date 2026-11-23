"""AES-256-GCM envelope for secrets stored in the database.

Blob format: ``base64(0x01 || nonce(12) || tag(16) || ciphertext)``. The caller supplies
associated data (``aad``) -- by convention ``mxid + NUL + column name`` -- which is authenticated
but not stored, so a blob copied to another user's row or another column fails to decrypt.
Error messages never include key material, plaintext, or blobs.
"""

from __future__ import annotations

import base64
import binascii
import os

from Crypto.Cipher import AES

_VERSION = 1
_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_ERROR = "token_encryption_key must be the base64 encoding of exactly 32 random bytes"


class SecretBox:
    def __init__(self, key_b64: str) -> None:
        if not isinstance(key_b64, str):
            raise ValueError(_KEY_ERROR)
        try:
            key = base64.b64decode(key_b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(_KEY_ERROR) from None
        if len(key) != 32:
            raise ValueError(_KEY_ERROR)
        self._key = key

    def __repr__(self) -> str:
        return "SecretBox()"

    def encrypt(self, plaintext: str, aad: str) -> str:
        nonce = os.urandom(_NONCE_LEN)  # random 96-bit nonce per message
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=_TAG_LEN)
        cipher.update(aad.encode("utf-8"))
        ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
        return base64.b64encode(bytes([_VERSION]) + nonce + tag + ct).decode("ascii")

    def decrypt(self, blob: str, aad: str) -> str:
        try:
            raw = base64.b64decode(blob, validate=True)
            if len(raw) < 1 + _NONCE_LEN + _TAG_LEN or raw[0] != _VERSION:
                raise ValueError
            nonce = raw[1 : 1 + _NONCE_LEN]
            tag = raw[1 + _NONCE_LEN : 1 + _NONCE_LEN + _TAG_LEN]
            ct = raw[1 + _NONCE_LEN + _TAG_LEN :]
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=_TAG_LEN)
            cipher.update(aad.encode("utf-8"))
            return cipher.decrypt_and_verify(ct, tag).decode("utf-8")
        except (ValueError, TypeError, binascii.Error):
            raise ValueError("decryption failed") from None
