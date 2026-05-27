"""Audit-only shadow stop logging for the 1hr live runner."""

from __future__ import annotations

import json

from kalshi_engine.bin.live_1hr import ShadowStopAudit
from kalshi_engine.core.events import BookEvent
from kalshi_engine.warehouse.adapters import LiveLogWriter


def _book(ticker="KXBTCD-T", recv_ms=120_000, yes_bid=900, no_bid=100):
    return BookEvent(
        ticker=ticker,
        ts_ms=recv_ms,
        recv_ms=recv_ms,
        yes_bid=yes_bid,
        yes_ask=1000 - no_bid,
        no_bid=no_bid,
        no_ask=1000 - yes_bid,
        yes_levels=((yes_bid, 10.0),),
        no_levels=((no_bid, 10.0),),
    )


def _events(log_path):
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_shadow_stop_logs_once_when_held_side_bid_breaks_threshold(tmp_path):
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    audit = ShadowStopAudit(
        enabled=True,
        bid_threshold_decicents=650,
        min_age_ms=60_000,
    )
    positions = {
        "KXBTCD-T": {
            "side": "yes",
            "count": 7,
            "filled_at_ms": 0,
            "entry_price_decicents": 900,
        }
    }
    audit.on_book(_book(yes_bid=640), positions, log)
    audit.on_book(_book(yes_bid=630), positions, log)
    events = _events(log_path)
    assert len(events) == 1
    assert events[0]["kind"] == "shadow_stop_triggered"
    assert events[0]["current_bid_decicents"] == 640
    assert events[0]["entry_price_decicents"] == 900


def test_shadow_stop_respects_min_age_and_disabled_mode(tmp_path):
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    positions = {
        "KXBTCD-T": {
            "side": "no",
            "count": 7,
            "filled_at_ms": 100_000,
            "entry_price_decicents": 900,
        }
    }
    audit = ShadowStopAudit(True, bid_threshold_decicents=650, min_age_ms=60_000)
    audit.on_book(_book(recv_ms=120_000, no_bid=640), positions, log)
    disabled = ShadowStopAudit(False, bid_threshold_decicents=650, min_age_ms=0)
    disabled.on_book(_book(recv_ms=200_000, no_bid=640), positions, log)
    assert _events(log_path) == []
