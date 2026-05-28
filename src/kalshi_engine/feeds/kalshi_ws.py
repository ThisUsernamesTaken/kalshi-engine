"""Authenticated Kalshi market-data WebSocket feed with auto-reconnect.

Subscribes to ``orderbook_delta``, ``trade``, and ``market_lifecycle_v2``
channels for a set of market tickers. Maintains a local L2 ladder per ticker
by applying deltas onto snapshots; emits typed events (BookEvent, TradeEvent,
SettlementEvent, LifecycleEvent) into the engine.

Reconnects with exponential backoff. Per-ticker ladder state and last-seq
numbers persist across reconnects; on reconnect Kalshi sends fresh
snapshots which replace the local ladders cleanly.

All prices in event output are integer **deci-cents** (0-1000); Kalshi's WS
emits dollar-strings ("0.0140") which are converted on read.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import AsyncIterator, Iterable

import websockets

from kalshi_engine.core.events import (
    BookEvent,
    LifecycleEvent,
    SettlementEvent,
    TradeEvent,
)
from kalshi_engine.core.types import Side
from kalshi_engine.execution.kalshi_auth import KalshiSigner
from kalshi_engine.execution.kalshi_client import PROD_WS_URL

_WS_SIGN_PATH = "/trade-api/ws/v2"
_DEFAULT_CHANNELS = ("orderbook_delta", "trade", "market_lifecycle_v2")
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_TERMINAL_STATUSES = ("determined",)


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_ts(msg: dict) -> int:
    """Read a ts field that may be seconds or ms; return ms."""
    ts = msg.get("ts_ms") or msg.get("ts") or msg.get("exchange_ts_ms")
    if ts is None:
        return _utc_now_ms()
    ts = int(ts)
    # Auto-detect seconds vs ms: anything below 10^10 is seconds.
    return ts if ts > 10_000_000_000 else ts * 1000


def _first(d: dict, keys: Iterable[str]):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _extract_levels(msg: dict) -> tuple[list, list]:
    """Pull (yes_levels, no_levels) out of a snapshot message.

    Kalshi snapshots can store the ladders directly in ``msg`` or nested
    under ``orderbook_fp`` / ``orderbook``, under several legacy key names.
    """
    yes_keys = ("yes_dollars_fp", "yes_dollars", "yes", "yes_levels", "yes_bids")
    no_keys = ("no_dollars_fp", "no_dollars", "no", "no_levels", "no_bids")
    for container in (msg.get("orderbook_fp"), msg.get("orderbook"), msg):
        if not isinstance(container, dict):
            continue
        yes = _first(container, yes_keys)
        if yes is None:
            continue
        no = _first(container, no_keys) or []
        return list(yes), list(no)
    return [], []


class KalshiWebSocketFeed:
    """Authenticated Kalshi market-data WS feed with auto-reconnect."""

    def __init__(
        self,
        signer: KalshiSigner,
        tickers: Iterable[str],
        channels: Iterable[str] = _DEFAULT_CHANNELS,
        ws_url: str = PROD_WS_URL,
    ) -> None:
        self._signer = signer
        self._tickers: list[str] = sorted(set(tickers))
        if not self._tickers:
            raise ValueError("KalshiWebSocketFeed needs at least one ticker")
        self._channels = list(channels)
        self._ws_url = ws_url
        # Per-ticker ladder state.
        # books[ticker] = {"yes_bids": {dc: size}, "no_bids": {dc: size},
        #                  "last_seq": int|None, "gaps": int, "duplicates": int}
        self.books: dict[str, dict] = {}
        # Live WS connection ref (set on connect, cleared on disconnect) -
        # exposed so ``add_tickers`` can extend the subscription mid-stream.
        self._ws = None
        self._sid: int | None = None
        self._cmd_id_counter = 1
        # Phase 14.17 - count malformed-frame parse failures (e.g. a Kalshi
        # payload carrying an oversized integer that raises OverflowError on
        # float() conversion). Skipping the frame keeps the feed alive instead
        # of bubbling the error to the reconnect loop (a single bad frame on
        # repeat would otherwise reconnect-storm forever).
        self.parse_errors = 0
        self._last_parse_error: dict | None = None

    def _next_cmd_id(self) -> int:
        self._cmd_id_counter += 1
        return self._cmd_id_counter

    @property
    def tickers(self) -> list[str]:
        """Current subscribed ticker list (kept in sync with ``add_tickers``)."""
        return list(self._tickers)

    async def add_tickers(self, new_tickers: Iterable[str]) -> int:
        """Extend the subscription to additional tickers; returns count added.

        New 15-min crypto markets open every quarter-hour. The boot-time
        subscribe sees only the currently-open cycle per series, so without
        this hook the engine goes deaf after the first cycle settles.

        Strategy: append the new tickers to ``self._tickers`` and force the
        active WS connection to close. The outer ``events()`` reconnect loop
        will then issue a fresh ``subscribe`` carrying the full extended
        ticker list. Empirically Kalshi's ``update_subscription`` was a no-op
        for the orderbook_delta channel (the 2026-05-23 trigger window
        verification showed zero book events for newly-added tickers), so we
        rely on a clean reconnect instead -- ~1-2 s of WS downtime per cycle.

        If no WS connection is live yet, the ticker is still recorded so the
        next subscribe carries it in.
        """
        wanted = [t for t in new_tickers if t and t not in self._tickers]
        if not wanted:
            return 0
        for t in wanted:
            self._tickers.append(t)
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass  # Outer reconnect loop will handle it.
        return len(wanted)

    # -- public API ------------------------------------------------------
    async def events(self) -> AsyncIterator:
        """Async generator yielding typed events forever (with reconnect)."""
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
                    self._ws = ws
                    self._sid = None
                    try:
                        await self._subscribe(ws)
                        async for raw in ws:
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            for event in self._dispatch(payload):
                                yield event
                    finally:
                        self._ws = None
                        self._sid = None
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                delay = min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S)
                await asyncio.sleep(delay)

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "id": self._next_cmd_id(),
            "cmd": "subscribe",
            "params": {
                "channels": self._channels,
                "market_tickers": self._tickers,
            },
        }))

    # -- payload dispatch -----------------------------------------------
    def _dispatch(self, payload: dict):
        """Synchronous generator yielding events from one WS payload."""
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        msg_type = (payload.get("type") or msg.get("type") or "").lower()
        # Capture the subscription id from Kalshi's ack so ``add_tickers``
        # can target the live subscription with ``update_subscription``.
        if msg_type == "subscribed":
            sid = payload.get("sid") or msg.get("sid")
            if sid is not None:
                try:
                    self._sid = int(sid)
                except (TypeError, ValueError):
                    pass
            return
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if not msg_type or not ticker:
            return
        seq = msg.get("seq") if msg.get("seq") is not None else payload.get("seq")
        if "snapshot" in msg_type:
            yield from self._on_snapshot(ticker, msg, seq)
        elif "delta" in msg_type:
            yield from self._on_delta(ticker, msg, seq)
        elif "trade" in msg_type:
            yield from self._on_trade(ticker, msg)
        elif "lifecycle" in msg_type or msg_type == "market_lifecycle_v2":
            yield from self._on_lifecycle(ticker, msg)

    # -- handlers --------------------------------------------------------
    def _book(self, ticker: str) -> dict:
        return self.books.setdefault(
            ticker,
            {"yes_bids": {}, "no_bids": {}, "last_seq": None, "gaps": 0, "duplicates": 0},
        )

    def _on_snapshot(self, ticker: str, msg: dict, seq):
        book = self._book(ticker)
        yes_levels, no_levels = _extract_levels(msg)
        try:
            yes_bids = {
                round(float(p) * 1000): float(s)
                for p, s in yes_levels
                if float(s) > 0
            }
            no_bids = {
                round(float(p) * 1000): float(s)
                for p, s in no_levels
                if float(s) > 0
            }
        except (OverflowError, ValueError, TypeError) as exc:
            self._record_parse_error("snapshot", ticker, msg, exc)
            return
        book["yes_bids"] = yes_bids
        book["no_bids"] = no_bids
        if seq is not None:
            book["last_seq"] = int(seq)
        yield self._build_book_event(ticker, msg)

    def _on_delta(self, ticker: str, msg: dict, seq):
        book = self._book(ticker)
        if seq is not None:
            seq = int(seq)
            last = book["last_seq"]
            if last is not None and seq == last:
                book["duplicates"] += 1
                return
            if last is not None and seq > last + 1:
                # Gap recorded but not repaired - mirrors the reference engine.
                # Kalshi will resync on the next snapshot from a reconnect.
                book["gaps"] += seq - last - 1
            book["last_seq"] = seq

        side = (msg.get("side") or msg.get("market_side") or "").lower()
        price = (
            msg.get("price_dollars")
            or msg.get("price")
            or msg.get("yes_price_dollars")
        )
        if side not in ("yes", "no") or price is None:
            return
        # Use ``in`` lookup (not ``or`` chain) so a legitimate value of 0
        # is not silently coerced to None.
        delta = next(
            (msg[k] for k in ("delta_fp", "delta", "delta_size", "change") if k in msg),
            None,
        )
        size = next(
            (msg[k] for k in ("size", "count", "quantity") if k in msg),
            None,
        )

        levels = book["yes_bids"] if side == "yes" else book["no_bids"]
        try:
            dc = round(float(price) * 1000)
            if delta is not None:
                new_size = levels.get(dc, 0.0) + float(delta)
            elif size is not None:
                new_size = float(size)
            else:
                return
        except (OverflowError, ValueError, TypeError) as exc:
            self._record_parse_error("delta", ticker, msg, exc)
            return
        if new_size <= 0:
            levels.pop(dc, None)
        else:
            levels[dc] = new_size
        yield self._build_book_event(ticker, msg)

    def _on_trade(self, ticker: str, msg: dict):
        yes_price = msg.get("yes_price_dollars") or msg.get("yes_price")
        if yes_price is None:
            return
        taker = (msg.get("taker_side") or "").lower()
        count_raw = msg.get("count_fp") or msg.get("count") or 0
        try:
            price_dc = round(float(yes_price) * 1000)
            count = float(count_raw)
        except (OverflowError, ValueError, TypeError) as exc:
            self._record_parse_error("trade", ticker, msg, exc)
            return
        yield TradeEvent(
            ticker=ticker,
            ts_ms=_normalize_ts(msg),
            recv_ms=_utc_now_ms(),
            price=price_dc,
            count=count,
            taker_side=Side(taker) if taker in ("yes", "no") else Side.YES,
        )

    def _record_parse_error(self, kind: str, ticker: str, msg: dict,
                             exc: Exception) -> None:
        """Phase 14.17 - record + skip a frame whose numeric fields failed to
        parse (OverflowError on an oversized int, or a malformed string). The
        frame is dropped; the feed continues. Keeps a truncated sample of the
        numeric-ish fields only (no auth/headers) for diagnosis."""
        self.parse_errors += 1
        sample_keys = (
            "price_dollars", "price", "yes_price_dollars", "yes_price",
            "delta_fp", "delta", "size", "count_fp", "count", "seq",
        )
        sample = {k: msg.get(k) for k in sample_keys if k in msg}
        self._last_parse_error = {
            "kind": kind,
            "ticker": ticker,
            "error": repr(exc),
            "sample": str(sample)[:200],
        }
        print(
            f"[kalshi_ws] parse_error kind={kind} ticker={ticker} "
            f"err={exc!r} sample={str(sample)[:200]}",
            file=sys.stderr, flush=True,
        )

    def _on_lifecycle(self, ticker: str, msg: dict):
        status = (msg.get("status") or msg.get("event_type") or "").lower()
        if not status:
            return
        ts_ms = _normalize_ts(msg)
        recv_ms = _utc_now_ms()
        if status in _TERMINAL_STATUSES:
            result = (msg.get("result") or "").lower()
            if result in ("yes", "no"):
                det_raw = msg.get("determination_ts")
                if det_raw is not None:
                    det = (
                        int(det_raw) * 1000
                        if int(det_raw) <= 10_000_000_000
                        else int(det_raw)
                    )
                else:
                    det = ts_ms
                yield SettlementEvent(
                    ticker=ticker,
                    ts_ms=ts_ms,
                    recv_ms=recv_ms,
                    result=Side(result),
                    settle_value=float(msg.get("settlement_value") or 0.0),
                    determined_ms=det,
                )
            return
        # non-terminal lifecycle transition
        meta = msg.get("additional_metadata") or {}
        strike = (
            msg.get("floor_strike")
            if isinstance(msg.get("floor_strike"), (int, float))
            else meta.get("floor_strike")
        )
        open_raw = msg.get("open_ts") or msg.get("open_time")
        close_raw = msg.get("close_ts") or msg.get("close_time")
        yield LifecycleEvent(
            ticker=ticker,
            ts_ms=ts_ms,
            recv_ms=recv_ms,
            status=status,
            open_ms=_coerce_epoch_ms(open_raw),
            close_ms=_coerce_epoch_ms(close_raw),
            strike=float(strike) if strike is not None else None,
        )

    # -- helpers ---------------------------------------------------------
    def _build_book_event(self, ticker: str, msg: dict) -> BookEvent:
        book = self.books[ticker]
        yes_bids = book["yes_bids"]
        no_bids = book["no_bids"]
        yes_bid = max(yes_bids) if yes_bids else 0
        no_bid = max(no_bids) if no_bids else 0
        # Kalshi binary complement: best YES ask = 1000 - best NO bid.
        yes_ask = 1000 - no_bid if no_bids else 1000
        no_ask = 1000 - yes_bid if yes_bids else 1000
        yes_levels = tuple(sorted(yes_bids.items()))
        no_levels = tuple(sorted(no_bids.items()))
        return BookEvent(
            ticker=ticker,
            ts_ms=_normalize_ts(msg),
            recv_ms=_utc_now_ms(),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_levels=yes_levels,
            no_levels=no_levels,
        )


def _coerce_epoch_ms(value) -> int | None:
    """Accept epoch seconds or ms (or None); return ms or None."""
    if value is None or value == "":
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 10_000_000_000 else v * 1000
