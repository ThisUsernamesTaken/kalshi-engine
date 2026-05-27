"""LiveExecution: dry-run intent logging, real-order path, reconcile, fill listener."""

from __future__ import annotations

import json

import pytest

from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.execution.kalshi_live import LiveExecution
from kalshi_engine.warehouse.adapters import LiveLogReader, LiveLogWriter


class _MockClient:
    """A fully-async mock KalshiClient. Records calls; canned return values."""

    def __init__(
        self,
        place_return: dict | None = None,
        positions: list[dict] | None = None,
        fills_stream: list[dict] | None = None,
        place_raises: Exception | None = None,
    ) -> None:
        self.placed: list[dict] = []
        self._place_return = place_return or {"order_id": "ord-1", "filled_count": 1}
        self._positions = positions or []
        self._fills_stream = fills_stream or []
        self._place_raises = place_raises

    async def place_limit_order(self, **kwargs):
        self.placed.append(kwargs)
        if self._place_raises is not None:
            raise self._place_raises
        return self._place_return

    async def get_positions(self):
        return self._positions

    async def subscribe_order_updates(self):
        for msg in self._fills_stream:
            yield msg


def _enter(ticker="KXBTC15M-T", side=Side.NO, size=1) -> Decision:
    return Decision(
        ticker=ticker, action=Action.ENTER, side=side,
        size=size, confidence=0.6, reason="signal",
        diagnostics={"vol_30m": 5.0},
    )


def _enter_with_limit(limit_price_decicents: int) -> Decision:
    return Decision(
        ticker="KXBTCD-T", action=Action.ENTER, side=Side.YES,
        size=1, confidence=0.8, reason="limited signal",
        diagnostics={"limit_price_decicents": limit_price_decicents},
    )


def test_construction_rejects_stop_mode_price(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    with pytest.raises(ValueError, match="stop_mode"):
        LiveExecution(_MockClient(), log, stop_mode="price")


async def test_dry_run_logs_intent_but_does_not_call_client(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient()
    exe = LiveExecution(client, log, dry_run=True)
    await exe.submit(_enter())
    assert client.placed == []  # no real order
    events = list(LiveLogReader(log.path).iter())
    intents = [e for e in events if e["kind"] == "order_intent"]
    assert len(intents) == 1
    assert intents[0]["dry_run"] is True
    assert intents[0]["price_decicents"] == 990  # marketable IOC buy
    assert intents[0]["side"] == "no"


async def test_enter_decision_places_order(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient(place_return={"order_id": "abc", "filled_count": 1})
    exe = LiveExecution(client, log, dry_run=False)
    await exe.submit(_enter())
    assert len(client.placed) == 1
    call = client.placed[0]
    assert call["ticker"] == "KXBTC15M-T"
    assert call["side"] == "no"
    assert call["action"] == "buy"
    assert call["price_decicents"] == 990
    assert call["count"] == 1
    # filled -> open_positions populated
    assert exe.open_positions["KXBTC15M-T"]["count"] == 1
    assert exe.open_positions["KXBTC15M-T"]["side"] == "no"


async def test_enter_decision_can_attach_lower_buy_limit(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient(place_return={"order_id": "abc", "filled_count": 1})
    exe = LiveExecution(client, log, dry_run=False)
    await exe.submit(_enter_with_limit(970))
    assert client.placed[0]["price_decicents"] == 970


async def test_skip_decision_is_a_noop(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient()
    exe = LiveExecution(client, log, dry_run=False)
    skip = Decision(
        ticker="KXBTC15M-T", action=Action.SKIP, side=Side.NO,
        size=0, reason="RISK-SKIP", diagnostics={},
    )
    await exe.submit(skip)
    assert client.placed == []


async def test_place_error_is_logged_not_raised(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient(place_raises=RuntimeError("HTTP 503"))
    exe = LiveExecution(client, log, dry_run=False)
    await exe.submit(_enter())
    errors = [
        json.loads(line)
        for line in open(log.path, encoding="utf-8") if line.strip()
    ]
    assert any(e["kind"] == "order_error" for e in errors)
    assert exe.open_positions == {}


async def test_reconcile_clean(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    exe = LiveExecution(_MockClient(positions=[]), log, dry_run=True)
    await exe.reconcile()
    events = list(LiveLogReader(log.path).iter())
    done = [e for e in events if e["kind"] == "reconcile_done"]
    assert done and done[0]["local_count"] == 0 and done[0]["account_count"] == 0


async def test_reconcile_detects_orphan_local(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    exe = LiveExecution(_MockClient(positions=[]), log, dry_run=True)
    exe.open_positions["KXBTC15M-T"] = {"side": "no", "count": 1}
    await exe.reconcile()
    events = list(LiveLogReader(log.path).iter())
    assert any(e["kind"] == "orphan_local" for e in events)


async def test_reconcile_detects_orphan_account(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient(positions=[
        {"ticker": "KXBTC15M-X", "position": 1},
    ])
    exe = LiveExecution(client, log, dry_run=True)
    await exe.reconcile()
    events = list(LiveLogReader(log.path).iter())
    assert any(
        e["kind"] == "orphan_account" and e["ticker"] == "KXBTC15M-X"
        for e in events
    )


async def test_order_update_listener_applies_buy_fill(tmp_path):
    log = LiveLogWriter(str(tmp_path / "log.jsonl"))
    client = _MockClient(fills_stream=[
        {"type": "fill",
         "msg": {"ticker": "KXBTC15M-T", "side": "no",
                 "count": 1, "action": "buy"}},
    ])
    exe = LiveExecution(client, log, dry_run=True)
    await exe.run_order_update_listener()  # gen ends after one msg
    assert exe.open_positions["KXBTC15M-T"]["side"] == "no"
    assert exe.open_positions["KXBTC15M-T"]["count"] == 1
