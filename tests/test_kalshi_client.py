"""KalshiClient + KalshiSigner: RSA-PSS auth, marketable-IOC body construction."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_engine.execution.kalshi_auth import KalshiSigner
from kalshi_engine.execution.kalshi_client import (
    BUY_PRICE_DECICENTS,
    DEFAULT_TIF,
    SELL_PRICE_DECICENTS,
    KalshiClient,
)


def _gen_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def pem() -> bytes:
    return _gen_pem()


def test_signer_headers_shape(pem):
    signer = KalshiSigner("key-uuid-test", pem)
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "key-uuid-test"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    # signature is base64-encoded 2048-bit RSA (256 bytes raw)
    raw = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    assert len(raw) == 256


def test_signer_signature_verifies_with_public_key(pem):
    """The signed message uses METHOD uppercase + path with query stripped.

    RSA-PSS signatures are non-deterministic (random salt), so we cannot
    compare two signatures directly; instead we verify with the public key
    that the produced signature is valid over the expected message bytes.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    signer = KalshiSigner("k", pem)
    sig_b64 = signer.sign("1700000000000", "get", "/trade-api/v2/markets?limit=10")
    public_key = load_pem_private_key(pem, password=None).public_key()
    expected_message = b"1700000000000GET/trade-api/v2/markets"
    # Raises InvalidSignature if the signature does not match.
    public_key.verify(
        base64.b64decode(sig_b64),
        expected_message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_marketable_defaults_are_decicent():
    assert BUY_PRICE_DECICENTS == 990
    assert SELL_PRICE_DECICENTS == 10
    assert DEFAULT_TIF == "immediate_or_cancel"


async def test_place_yes_buy_body(pem):
    client = KalshiClient("kid", pem)
    captured: dict = {}

    async def fake_request(method, path, json_body=None, params=None):
        captured.update(method=method, path=path, body=json_body, params=params)
        return {"order": {"order_id": "ord-1"}}

    client._request = fake_request  # type: ignore[method-assign]
    out = await client.place_limit_order(
        ticker="KXBTC15M-T", side="yes", action="buy",
        price_decicents=BUY_PRICE_DECICENTS, count=1,
    )
    assert out == {"order_id": "ord-1"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/orders"
    body = captured["body"]
    assert body["ticker"] == "KXBTC15M-T"
    assert body["side"] == "yes"
    assert body["action"] == "buy"
    assert body["count"] == 1
    assert body["type"] == "limit"
    # 990 deci-cents -> 99 cents at the API
    assert body["yes_price"] == 99
    assert "no_price" not in body
    assert body["time_in_force"] == "immediate_or_cancel"


async def test_place_no_sell_uses_no_price(pem):
    client = KalshiClient("kid", pem)
    captured: dict = {}

    async def fake_request(method, path, json_body=None, params=None):
        captured.update(body=json_body)
        return {"order": {"order_id": "ord-2"}}

    client._request = fake_request  # type: ignore[method-assign]
    await client.place_limit_order(
        ticker="KXBTC15M-T", side="no", action="sell",
        price_decicents=SELL_PRICE_DECICENTS, count=1,
    )
    body = captured["body"]
    assert body["side"] == "no"
    assert body["action"] == "sell"
    # 10 deci-cents -> 1 cent
    assert body["no_price"] == 1
    assert "yes_price" not in body


async def test_request_requires_async_context(pem):
    client = KalshiClient("kid", pem)
    with pytest.raises(RuntimeError, match="async context"):
        await client._request("GET", "/portfolio/balance")


async def test_list_markets_passes_params(pem):
    client = KalshiClient("kid", pem)
    captured: dict = {}

    async def fake_request(method, path, json_body=None, params=None):
        captured.update(method=method, path=path, params=params)
        return {"markets": [{"ticker": "KXBTC15M-T"}]}

    client._request = fake_request  # type: ignore[method-assign]
    out = await client.list_markets(series_ticker="KXBTC15M", status="active", limit=50)
    assert out == [{"ticker": "KXBTC15M-T"}]
    assert captured["params"] == {"limit": 50, "series_ticker": "KXBTC15M", "status": "active"}
