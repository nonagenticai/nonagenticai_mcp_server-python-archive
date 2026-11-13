"""Authenticated encryption of the one stored secret of a source (AES-256-GCM).

The associated data is the source id, so a ciphertext copied onto another source row fails
authentication instead of silently sending one upstream's credential to another. Tokens never
leave this module except as ``SecretStr``.
"""

import base64
import binascii
import os
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from src.execution.models import ExecutionDeniedError

NONCE_BYTES = 12


def decode_key(value: str) -> bytes:
    """A 32-byte key from unpadded or padded base64url; raises ValueError."""
    try:
        key = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    except binascii.Error, ValueError:
        raise ValueError("source secret key must be base64url") from None
    if len(key) != 32:
        raise ValueError("source secret key must be 32 bytes")
    return key


def valid_token(value: str) -> str:
    """A bearer token, header value or key: printable ASCII without spaces."""
    if not 1 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("invalid credential material")
    return value


def valid_secret(value: str) -> str:
    """Stored secret material: printable ASCII, spaces allowed (a basic-auth password)."""
    if not 1 <= len(value) <= 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("invalid credential material")
    return value


def encrypt_token(key: SecretStr, source_id: UUID, token: SecretStr) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(decode_key(key.get_secret_value())).encrypt(
        nonce, valid_secret(token.get_secret_value()).encode("ascii"), source_id.bytes
    )
    return nonce + sealed


def decrypt_token(key: SecretStr | None, source_id: UUID, blob: bytes) -> SecretStr:
    """Fails closed (ExecutionDeniedError) for a missing key, a swapped row or any corruption."""
    if key is None or len(blob) <= NONCE_BYTES:
        raise ExecutionDeniedError("source credential unavailable")
    try:
        plain = AESGCM(decode_key(key.get_secret_value())).decrypt(
            bytes(blob[:NONCE_BYTES]), bytes(blob[NONCE_BYTES:]), source_id.bytes
        )
        return SecretStr(valid_secret(plain.decode("ascii")))
    except InvalidTag, ValueError:
        raise ExecutionDeniedError("source credential unavailable") from None
