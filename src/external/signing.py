"""HMAC signing for the fr <-> MCP channel (spec section 3.1).

Signature is ``sha256=<hex>`` over ``<timestamp>.<raw body>``; receivers reject
timestamps more than 300 s from their own clock.
"""

import hashlib
import hmac
from collections.abc import Mapping
from uuid import uuid4

SKEW_SECONDS = 300
SIGNATURE_HEADER = "X-Channel-Signature"
TIMESTAMP_HEADER = "X-Channel-Timestamp"
IDEMPOTENCY_HEADER = "Idempotency-Key"


def _digest(body: bytes, secret: str, timestamp: str) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def sign(body: bytes, secret: str, now: int) -> dict[str, str]:
    timestamp = str(now)
    return {
        SIGNATURE_HEADER: _digest(body, secret, timestamp),
        TIMESTAMP_HEADER: timestamp,
        IDEMPOTENCY_HEADER: str(uuid4()),
    }


def verify(body: bytes, headers: Mapping[str, str], secret: str, now: int) -> bool:
    """True only for a fresh, correctly signed body. Never raises."""
    try:
        lowered = {str(k).lower(): v for k, v in headers.items()}
        signature = lowered.get(SIGNATURE_HEADER.lower())
        timestamp = lowered.get(TIMESTAMP_HEADER.lower())
        if not isinstance(signature, str) or not isinstance(timestamp, str):
            return False
        if not (timestamp.isascii() and timestamp.isdigit()):
            return False
        if abs(now - int(timestamp)) > SKEW_SECONDS:
            return False
        return hmac.compare_digest(signature.encode(), _digest(body, secret, timestamp).encode())
    except Exception:
        return False
