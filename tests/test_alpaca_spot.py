"""Tests for AlpacaSpotPoller (Phase 14.0 equity-index spot adapter).

Covers:
- RTH gate (Mon-Fri 9:30-16:00 ET, weekends + overnight return None)
- Parsing of Alpaca's /trades/latest payload
- Empty / malformed payload handling
- Credentials loading from env or dotenv path

Network is mocked at the aiohttp.ClientSession level so tests are
deterministic and offline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from kalshi_engine.feeds.alpaca_spot import (
    AlpacaSpotPoller, EquityTrade, credentials_from_env,
)


# ---- RTH gate -----------------------------------------------------------

def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)

def test_rth_open_at_10am_et_weekday():
    # 2026-05-26 is a Tuesday. 14:00 UTC = 10:00 EDT.
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 26, 14, 0)) is True

def test_rth_closed_overnight_3am_et():
    # Tue 3am ET = Tue 07:00 UTC during EDT
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 26, 7, 0)) is False

def test_rth_closed_weekend_saturday():
    # 2026-05-30 is a Saturday
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 30, 14, 0)) is False

def test_rth_closed_weekend_sunday():
    # 2026-05-31 Sunday
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 31, 18, 0)) is False

def test_rth_edge_9_30_et_open():
    # 9:30 EDT = 13:30 UTC
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 26, 13, 30)) is True

def test_rth_edge_16_00_et_open():
    # 16:00 EDT = 20:00 UTC — inclusive per implementation
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 26, 20, 0)) is True

def test_rth_edge_after_close_16_01_et_closed():
    # 16:01 EDT = 20:01 UTC
    assert AlpacaSpotPoller.is_market_open(_utc(2026, 5, 26, 20, 1)) is False


# ---- credentials loading ------------------------------------------------

def test_credentials_from_env_vars(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    assert credentials_from_env() == ("test-key", "test-secret")

def test_credentials_from_dotenv_path(tmp_path, monkeypatch):
    # Clear any env-var fallback so dotenv is the only source
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    f = tmp_path / "alpaca.env"
    f.write_text('ALPACA_API_KEY_ID="kid-xyz"\nALPACA_API_SECRET_KEY="sec-abc"\n')
    assert credentials_from_env(str(f)) == ("kid-xyz", "sec-abc")

def test_credentials_missing_raises(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_CREDENTIALS_PATH", raising=False)
    with pytest.raises(RuntimeError, match="credentials not found"):
        credentials_from_env()


# ---- constructor --------------------------------------------------------

def test_constructor_rejects_empty_creds():
    with pytest.raises(ValueError, match="credentials required"):
        AlpacaSpotPoller("", "")


# ---- get_last_trade parse path ------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}
    async def json(self):
        return self._payload
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.requests: list[str] = []
    def get(self, url):
        self.requests.append(url)
        return self._response
    async def close(self): pass


async def _run_with_session(payload, status=200, respect_rth=False, symbol="SPY"):
    p = AlpacaSpotPoller("kid", "sec")
    p._session = _FakeSession(_FakeResponse(status=status, payload=payload))
    return await p.get_last_trade(symbol, respect_rth=respect_rth)


def test_get_last_trade_parses_well_formed_payload():
    payload = {"trade": {"p": 502.34, "t": "2026-05-26T19:59:30.123456Z",
                          "x": "V", "s": 100}}
    r = asyncio.run(_run_with_session(payload))
    assert isinstance(r, EquityTrade)
    assert r.price == 502.34
    assert r.symbol == "SPY"
    assert r.exchange == "V"
    assert r.ts_ms > 0

def test_get_last_trade_empty_trade_returns_none():
    r = asyncio.run(_run_with_session({"trade": {}}))
    assert r is None

def test_get_last_trade_missing_trade_key_returns_none():
    r = asyncio.run(_run_with_session({}))
    assert r is None

def test_get_last_trade_http_error_returns_none():
    r = asyncio.run(_run_with_session({"trade": {"p": 1}}, status=500))
    assert r is None

def test_get_last_trade_handles_nanosecond_precision_timestamp():
    payload = {"trade": {"p": 100.0, "t": "2026-05-26T20:00:00.123456789Z", "x": "V"}}
    r = asyncio.run(_run_with_session(payload))
    assert r is not None
    assert r.price == 100.0

def test_get_last_trade_handles_no_subsecond_timestamp():
    payload = {"trade": {"p": 100.0, "t": "2026-05-26T20:00:00Z", "x": "V"}}
    r = asyncio.run(_run_with_session(payload))
    assert r is not None
    assert r.ts_ms > 0


# ---- RTH gate integration -----------------------------------------------

def test_get_last_trade_respects_rth_gate_when_closed():
    p = AlpacaSpotPoller("kid", "sec")
    p._session = _FakeSession(_FakeResponse(payload={"trade": {"p": 100, "t": "2026-05-26T07:00:00Z"}}))
    # Force RTH-closed by patching is_market_open
    with patch.object(AlpacaSpotPoller, "is_market_open", return_value=False):
        r = asyncio.run(p.get_last_trade("SPY", respect_rth=True))
    assert r is None
    # No request should have been issued during RTH-closed
    assert p._session.requests == []

def test_get_last_trade_overrides_rth_gate_when_false():
    payload = {"trade": {"p": 100.0, "t": "2026-05-26T07:00:00Z", "x": "V"}}
    with patch.object(AlpacaSpotPoller, "is_market_open", return_value=False):
        r = asyncio.run(_run_with_session(payload, respect_rth=False))
    assert r is not None


# ---- session lifecycle --------------------------------------------------

def test_must_enter_context_manager_before_calling():
    p = AlpacaSpotPoller("kid", "sec")
    with pytest.raises(RuntimeError, match="context manager"):
        asyncio.run(p.get_last_trade("SPY", respect_rth=False))


# ---- batch poll ---------------------------------------------------------

def test_get_last_trades_returns_dict_per_symbol():
    payload = {"trade": {"p": 100.0, "t": "2026-05-26T20:00:00Z", "x": "V"}}
    p = AlpacaSpotPoller("kid", "sec")
    p._session = _FakeSession(_FakeResponse(payload=payload))
    with patch.object(AlpacaSpotPoller, "is_market_open", return_value=True):
        r = asyncio.run(p.get_last_trades(["SPY", "QQQ"]))
    assert set(r.keys()) == {"SPY", "QQQ"}
    assert r["SPY"] is not None
    assert r["QQQ"] is not None
