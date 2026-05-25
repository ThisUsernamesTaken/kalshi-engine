"""Async Kalshi REST client - orders, fills, positions, balance.

Wraps the Kalshi trade API with RSA-PSS-signed requests; must be used as an
async context manager (``async with KalshiClient(...) as c:``). Engine
prices are deci-cents (0-1000); the Kalshi order API takes whole cents
(1-99), so order prices are converted on the way out.

Also exposes ``subscribe_order_updates()`` - an async generator over the
authenticated WS ``fill`` channel, with reconnect-and-backoff.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, AsyncIterator

import aiohttp
import websockets

from kalshi_engine.execution.kalshi_auth import KalshiSigner

PROD_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
_REST_PATH_PREFIX = "/trade-api/v2"
_WS_SIGN_PATH = "/trade-api/ws/v2"

# Marketable-IOC price caps (deci-cents): buy near the ceiling, sell near
# the floor, so a limit order fills immediately at the real book price.
BUY_PRICE_DECICENTS = 990
SELL_PRICE_DECICENTS = 10
DEFAULT_TIF = "immediate_or_cancel"

# Reconnect backoff for the order-updates WS.
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0


class KalshiClient:
    """Async-context Kalshi REST client with RSA-PSS request signing."""

    def __init__(
        self,
        key_id: str,
        private_key_pem: str | bytes,
        rest_base: str = PROD_REST_BASE,
        ws_url: str = PROD_WS_URL,
    ) -> None:
        self._signer = KalshiSigner(key_id, private_key_pem)
        self._rest_base = rest_base.rstrip("/")
        self._ws_url = ws_url
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KalshiClient":
        ssl_ctx = ssl.create_default_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx, force_close=True)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=10, connect=5),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def signer(self) -> KalshiSigner:
        """The shared RSA-PSS signer (also used by the market-data WS feed)."""
        return self._signer

    async def list_markets(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """REST market discovery. Returns the ``markets`` array verbatim."""
        params: dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        data = await self._request("GET", "/markets", params=params)
        return data.get("markets", [])

    # -- internal --------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError(
                "KalshiClient must be used as an async context manager"
            )
        signed_path = _REST_PATH_PREFIX + path
        headers = self._signer.headers(method, signed_path)
        headers["Content-Type"] = "application/json"
        url = self._rest_base + path
        async with self._session.request(
            method, url, json=json_body, params=params, headers=headers
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(
                    f"Kalshi {method} {path} -> HTTP {resp.status}: {text[:300]}"
                )
            if not text:
                return {}
            return json.loads(text)

    # -- orders ----------------------------------------------------------
    async def place_limit_order(
        self,
        ticker: str,
        side: str,
        action: str,
        price_decicents: int,
        count: int,
        time_in_force: str = DEFAULT_TIF,
    ) -> dict:
        """Place a marketable limit order.

        ``side`` is 'yes' or 'no', ``action`` is 'buy' or 'sell'.
        ``price_decicents`` is the engine-native unit (0-1000); converted to
        the whole-cent value the Kalshi order API expects.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be yes/no, got {side!r}")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be buy/sell, got {action!r}")
        price_cents = round(price_decicents / 10)
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": "limit",
            ("yes_price" if side == "yes" else "no_price"): price_cents,
            "time_in_force": time_in_force,
        }
        data = await self._request("POST", "/portfolio/orders", json_body=body)
        return data.get("order", data)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    # -- account ---------------------------------------------------------
    async def get_positions(self) -> list[dict]:
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    async def get_balance(self) -> dict:
        return await self._request("GET", "/portfolio/balance")

    async def get_fills(
        self, ticker: str | None = None, limit: int = 100
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return data.get("fills", [])

    # -- order-update stream --------------------------------------------
    async def subscribe_order_updates(self) -> AsyncIterator[dict]:
        """Async generator of order/fill messages from the WS ``fill`` channel.

        Reconnects with exponential backoff on connection drop. Yields raw
        Kalshi message dicts; the caller dispatches by ``type``/``msg``.
        """
        attempt = 0
        while True:
            try:
                headers = self._signer.headers("GET", _WS_SIGN_PATH)
                async with websockets.connect(
                    self._ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    attempt = 0
                    await ws.send(
                        json.dumps(
                            {
                                "id": 1,
                                "cmd": "subscribe",
                                "params": {"channels": ["fill"]},
                            }
                        )
                    )
                    async for raw in ws:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                delay = min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S)
                await asyncio.sleep(delay)
