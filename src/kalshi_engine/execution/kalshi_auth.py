"""RSA-PSS request signing for the Kalshi API.

Kalshi authenticates every REST and WebSocket request with an RSA-PSS
signature over ``{timestamp_ms}{METHOD}{path}``. The signing key is an RSA
private key (PEM); the key id is a UUID. Shared by the REST client and the
WS feed so both speak the same auth.

Signature scheme (verified against the archived live engine):

    message   = f"{timestamp_ms}{METHOD_UPPER}{path}"      # path no query string
    padding   = RSA-PSS, MGF1+SHA256, salt_length = DIGEST_LENGTH
    hash      = SHA256
    signature = base64(rsa_pss_sign(message))

Three headers travel with every request:
    KALSHI-ACCESS-KEY        the key id (UUID)
    KALSHI-ACCESS-TIMESTAMP  the same timestamp_ms used in the signature
    KALSHI-ACCESS-SIGNATURE  the base64 signature
"""

from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiSigner:
    """Signs Kalshi API requests with RSA-PSS (MGF1+SHA256, digest salt)."""

    def __init__(self, key_id: str, private_key_pem: str | bytes) -> None:
        if not key_id:
            raise ValueError("key_id is required")
        self.key_id = key_id
        pem_bytes = (
            private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
        )
        self._key = serialization.load_pem_private_key(pem_bytes, password=None)

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS signature (base64) over timestamp + METHOD + path.

        `path` must include the ``/trade-api/v2`` (or ws) prefix and exclude
        any query string. `method` is case-insensitive; uppercased here.
        """
        signed_path = path.split("?", 1)[0]
        message = f"{timestamp_ms}{method.upper()}{signed_path}".encode()
        sig = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Auth headers for an HTTP request or a WS handshake."""
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }
