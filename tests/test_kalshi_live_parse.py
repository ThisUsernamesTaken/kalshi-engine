"""Parse-defense tests for LiveExecution.

Covers the bug class that crashed the engine on 2026-05-22 right after the
first live entry: Kalshi returns ``filled_count`` as a decimal string
(``"1.00"``), the engine called ``int(...)`` directly, ``ValueError``
unwound the asyncio loop, and a real position was left unmanaged.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.execution.kalshi_live import (
    LiveExecution,
    _position_entry_price_decicents,
    _safe_int_count,
)
from kalshi_engine.warehouse.adapters import LiveLogWriter


# ---- pure helper ----------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.00", 1),     # the exact string that crashed prod
        ("0.00", 0),
        ("0", 0),
        ("1", 1),
        ("5", 5),
        ("0.99", 0),     # partial fill rounds toward 0
        ("1.50", 1),     # partial fill rounds toward 0
        (1, 1),          # already int
        (1.0, 1),        # already float
        (None, 0),
        ("", 0),
        ("garbage", 0),  # unparseable -> default
    ],
)
def test_safe_int_count_handles_decimal_strings(value, expected):
    assert _safe_int_count(value, default=0) == expected


def test_safe_int_count_custom_default():
    assert _safe_int_count(None, default=99) == 99
    assert _safe_int_count("xx", default=-1) == -1


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"side": "yes", "yes_price_dollars": "0.87"}, 870),
        ({"side": "no", "yes_price_dollars": "0.13"}, 870),
        ({"purchased_side": "no", "yes_price_dollars": "0.22"}, 780),
        ({"side": "yes", "yes_price": "0.91"}, 910),
        ({"side": "no"}, None),
        ({"side": "maybe", "yes_price_dollars": "0.50"}, None),
    ],
)
def test_position_entry_price_decicents(payload, expected):
    assert _position_entry_price_decicents(payload) == expected


# ---- LiveExecution mocks --------------------------------------------------

class _FakeClient:
    """Minimal stand-in for KalshiClient supporting the calls we exercise."""

    def __init__(self, order_response=None, positions=None, raise_on_place=False):
        self._order_response = order_response or {}
        self._positions = positions or []
        self._raise_on_place = raise_on_place
        self.calls: list[str] = []

    async def place_limit_order(self, **kw):
        self.calls.append("place")
        if self._raise_on_place:
            raise RuntimeError("simulated REST failure")
        return self._order_response

    async def get_positions(self):
        self.calls.append("positions")
        return self._positions


def _enter(ticker="KXBTC15M-T", side=Side.YES, size=1) -> Decision:
    return Decision(
        ticker=ticker, action=Action.ENTER, side=side,
        size=size, confidence=0.6, reason="ENTER_1X", diagnostics={},
    )


def _read_log(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# ---- the regression --------------------------------------------------------

def test_filled_count_decimal_string_does_not_crash(tmp_path):
    """The exact 2026-05-22 crash payload now books the position cleanly."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    client = _FakeClient(order_response={
        "filled_count": "1.00",  # the literal Kalshi response that crashed
        "order_id": "ord_test_abc",
    })
    exec_ = LiveExecution(client, log)
    asyncio.run(exec_.submit(_enter()))
    assert exec_.open_positions["KXBTC15M-T"]["count"] == 1
    assert exec_.open_positions["KXBTC15M-T"]["order_id"] == "ord_test_abc"
    events = _read_log(log_path)
    kinds = [e["kind"] for e in events]
    assert "order_filled" in kinds
    assert "order_parse_error" not in kinds


def test_entry_price_stored_from_order_response(tmp_path):
    """Stored entry price supports shadow-stop audits without rereading fills."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    client = _FakeClient(order_response={
        "filled_count": "1.00",
        "order_id": "ord_test_abc",
        "side": "no",
        "yes_price_dollars": "0.12",
    })
    exec_ = LiveExecution(client, log)
    asyncio.run(exec_.submit(_enter(side=Side.NO)))
    assert exec_.open_positions["KXBTC15M-T"]["entry_price_decicents"] == 880


def test_filled_count_zero_does_not_book_position(tmp_path):
    """A 0-fill response must not create a phantom position."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    client = _FakeClient(order_response={"filled_count": "0.00", "order_id": "ord_z"})
    exec_ = LiveExecution(client, log)
    asyncio.run(exec_.submit(_enter()))
    assert "KXBTC15M-T" not in exec_.open_positions


def test_malformed_order_response_does_not_raise(tmp_path):
    """A pathological response object logs ``order_parse_error`` cleanly."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))

    class _Broken:
        """Mimics a dict-shaped Kalshi response whose .get() blows up."""
        def get(self, *a, **kw):
            raise RuntimeError("simulated dict failure")

    client = _FakeClient(order_response=_Broken())
    exec_ = LiveExecution(client, log)
    # Must not raise.
    asyncio.run(exec_.submit(_enter()))
    events = _read_log(log_path)
    assert any(e["kind"] == "order_parse_error" for e in events)


def test_rest_failure_logs_order_error_not_crash(tmp_path):
    """A REST exception is caught and logged; submit() returns normally."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    client = _FakeClient(raise_on_place=True)
    exec_ = LiveExecution(client, log)
    asyncio.run(exec_.submit(_enter()))
    events = _read_log(log_path)
    assert any(e["kind"] == "order_error" for e in events)


# ---- boot reconciliation --------------------------------------------------

class _FakeStrategy:
    def __init__(self):
        self.decided: set[str] = set()


def test_boot_reconcile_imports_existing_position(tmp_path):
    """A live position on the account is imported to local + marked decided."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    client = _FakeClient(positions=[
        {"ticker": "KXSOL15M-T", "position": "1.00"},      # decimal string
        {"ticker": "KXETH15M-T", "position": -2},           # negative -> NO
        {"ticker": "KXBTC15M-T", "position": 0},            # zero -> ignored
        {"ticker": "KXXRP15M-T", "position": "0.00"},       # zero string -> ignored
    ])
    exec_ = LiveExecution(client, log)
    strategy = _FakeStrategy()
    asyncio.run(exec_.reconcile_from_account_at_boot(strategy))
    assert "KXSOL15M-T" in exec_.open_positions
    assert exec_.open_positions["KXSOL15M-T"]["side"] == "yes"
    assert exec_.open_positions["KXSOL15M-T"]["count"] == 1
    assert exec_.open_positions["KXETH15M-T"]["side"] == "no"
    assert exec_.open_positions["KXETH15M-T"]["count"] == 2
    assert "KXBTC15M-T" not in exec_.open_positions
    assert "KXXRP15M-T" not in exec_.open_positions
    # Strategy is told not to re-evaluate these tickers in this cycle.
    assert "KXSOL15M-T" in strategy.decided
    assert "KXETH15M-T" in strategy.decided
    # Boot envelope written.
    events = _read_log(log_path)
    boot = [e for e in events if e["kind"] == "boot_reconcile"]
    assert len(boot) == 1
    assert boot[0]["imported_count"] == 2


def test_boot_reconcile_error_does_not_crash(tmp_path):
    """A REST failure during reconcile logs and returns cleanly."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))

    class _Broken(_FakeClient):
        async def get_positions(self):
            raise RuntimeError("simulated /portfolio/positions failure")

    exec_ = LiveExecution(_Broken(), log)
    asyncio.run(exec_.reconcile_from_account_at_boot(_FakeStrategy()))
    events = _read_log(log_path)
    assert any(e["kind"] == "boot_reconcile_error" for e in events)
