"""Event-driven backtest replay engine.

The ``Replayer`` drives the favorite-chase pipeline (strategy + cutpoints
model + risk envelope + FillSimulator) over historical warehouse events. It
heap-merges multiple time-ordered sources - the burn-in SQLite for Kalshi
book / trade / lifecycle / settlement events, and the gap-free fusion spot
parquets for spot ticks - and dispatches each event through the same routing
logic the live engine uses.

Output is JSONL using the same schema the live engine writes, so a replay
log and a live log are interchangeable downstream.

This is a *concept-integrity* layer (per the project framing): it verifies
the strategy code computes the same decisions over captured data that the
live engine would. PnL numbers are reported but are not predictive of live
PnL - small-sized live forward-verification remains the only ground truth.
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from kalshi_engine.core.events import (
    BookEvent,
    LifecycleEvent,
    SettlementEvent,
    SpotEvent,
    TradeEvent,
)
from kalshi_engine.core.types import Crypto
from kalshi_engine.risk.envelope import RiskEnvelope, RiskState
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy
from kalshi_engine.warehouse.adapters import BurninReader, LiveLogWriter, SpotParquetReader
from kalshi_engine.warehouse.settlement import _iso_to_ms
from kalshi_engine.backtest.fill_simulator import FillSimulator

SERIES_FOR_CRYPTO = {
    Crypto.BTC: "KXBTC15M",
    Crypto.ETH: "KXETH15M",
    Crypto.SOL: "KXSOL15M",
    Crypto.XRP: "KXXRP15M",
    Crypto.DOGE: "KXDOGE15M",
}

SPOT_SYMBOL_FOR_CRYPTO = {
    Crypto.BTC: "btcusd",
    Crypto.ETH: "ethusd",
    Crypto.SOL: "solusd",
    Crypto.XRP: "xrpusd",
    Crypto.DOGE: "dogeusd",
}


class Replayer:
    """Replays warehouse events through the live decision pipeline."""

    def __init__(
        self,
        strategy: FavoriteChaseStrategy,
        envelope: RiskEnvelope,
        execution: FillSimulator,
        log_writer: LiveLogWriter,
    ) -> None:
        self.strategy = strategy
        self.envelope = envelope
        self.execution = execution
        self.log = log_writer
        self.risk_state = RiskState()
        self._decision_count = 0

    def replay_window(
        self,
        burnin_path: str,
        spot_dir: str | None,
        cryptos: Iterable[Crypto],
        start_ms: int,
        end_ms: int,
        immutable_db: bool = True,
    ) -> dict:
        """Replay events in ``[start_ms, end_ms]`` across cryptos.

        Pass ``immutable_db=False`` when the burn-in SQLite is the live
        actively-written capture (otherwise SQLite's immutable assumption
        is violated and reads may be inconsistent).
        """
        cryptos = list(cryptos)

        # RiskState.now_ms defaults to wall-clock time (correct for live, but
        # in replay it desynchronises every freshness check against simulated
        # event times). Reset to 0 so the first event's recv_ms wins via the
        # max() update in _dispatch.
        self.risk_state.now_ms = 0

        tickers = self._collect_tickers(
            burnin_path, cryptos, start_ms, end_ms, immutable_db=immutable_db,
        )
        n_registered = len(tickers)

        self.log.write({
            "kind": "replay_boot",
            "burnin_path": str(burnin_path),
            "spot_dir": str(spot_dir) if spot_dir else None,
            "cryptos": [c.value for c in cryptos],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "markets_registered": n_registered,
        })

        # Spot source: prefer the gap-free fusion parquets when supplied;
        # otherwise fall back to the burn-in capture's own spot_quote_event.
        use_parquet_spot = spot_dir is not None
        burnin_spot_symbols: list[str] = []
        if not use_parquet_spot:
            for c in cryptos:
                sym = SPOT_SYMBOL_FOR_CRYPTO.get(c)
                if sym:
                    burnin_spot_symbols.append(sym)

        burnin = BurninReader(burnin_path, immutable=immutable_db)
        sources = [self._burnin_events_indexed(
            burnin, start_ms, end_ms, tickers, burnin_spot_symbols,
        )]
        if use_parquet_spot:
            for crypto in cryptos:
                try:
                    parq = SpotParquetReader(crypto, "fusion", spot_dir=spot_dir)
                except FileNotFoundError:
                    self.log.write({
                        "kind": "spot_parquet_missing",
                        "crypto": crypto.value,
                        "spot_dir": str(spot_dir),
                    })
                    continue
                sources.append(self._parquet_events(parq, start_ms, end_ms))

        n_events = 0
        try:
            for _ts_ms, event in heapq.merge(*sources, key=lambda pair: pair[0]):
                n_events += 1
                self._dispatch(event)
        finally:
            burnin.close()

        summary = {
            "events_processed": n_events,
            "markets_registered": n_registered,
            "decisions_emitted": self._decision_count,
            **self.execution.summary(),
            "daily_realized_cents": self.risk_state.daily_realized_cents,
        }
        self.log.write({"kind": "replay_done", **summary})
        return summary

    # -- event sources --------------------------------------------------
    def _burnin_events(self, burnin: BurninReader, start_ms: int, end_ms: int):
        """Yield (ts, event) from burn-in via the legacy full-scan path,
        **skipping SpotEvent** (preferred from the gap-free fusion parquet).

        Kept for compatibility with small quiescent burn-in DBs that lack the
        per-ticker indexes. New code should use ``_burnin_events_indexed``.
        """
        for ev in burnin.iter_range(start_ms, end_ms):
            if isinstance(ev, SpotEvent):
                continue
            ts = getattr(ev, "recv_ms", None) or getattr(ev, "ts_ms", 0)
            yield (ts, ev)

    def _burnin_events_indexed(
        self,
        burnin: BurninReader,
        start_ms: int,
        end_ms: int,
        tickers: list[str],
        spot_symbols: list[str],
    ):
        """Yield (ts, event) from burn-in via the indexed per-ticker path.

        ``spot_symbols`` lists the symbols (e.g. 'btcusd') to surface from the
        capture's ``spot_quote_event`` table -- pass an empty list when spot
        is being supplied by the fusion parquets instead.
        """
        for ev in burnin.iter_window(start_ms, end_ms, tickers, spot_symbols):
            ts = getattr(ev, "recv_ms", None) or getattr(ev, "ts_ms", 0)
            yield (ts, ev)

    def _parquet_events(self, parq: SpotParquetReader, start_ms: int, end_ms: int):
        for ev in parq.iter_range(start_ms, end_ms):
            yield (ev.ts_ms, ev)

    # -- registration ---------------------------------------------------
    def _collect_tickers(
        self,
        burnin_path: str,
        cryptos: list[Crypto],
        start_ms: int,
        end_ms: int,
        immutable_db: bool,
    ) -> list[str]:
        """Walk market_dim for tickers whose cycle overlaps [start_ms, end_ms],
        register each with the strategy, and return the in-scope ticker list.
        The returned list drives the indexed per-ticker event streams.
        """
        qs = "mode=ro&immutable=1" if immutable_db else "mode=ro"
        uri = f"file:{Path(burnin_path).as_posix()}?{qs}"
        con = sqlite3.connect(uri, uri=True)
        tickers: list[str] = []
        try:
            for crypto in cryptos:
                series = SERIES_FOR_CRYPTO.get(crypto)
                if series is None:
                    continue
                rows = con.execute(
                    "SELECT ticker, open_time, close_time, raw_json "
                    "FROM market_dim "
                    "WHERE ticker LIKE ? AND open_time IS NOT NULL "
                    "AND close_time IS NOT NULL",
                    (series + "%",),
                ).fetchall()
                for ticker, ot, ct, rj in rows:
                    om = _iso_to_ms(ot)
                    cm = _iso_to_ms(ct)
                    if om is None or cm is None:
                        continue
                    if cm < start_ms or om > end_ms:
                        continue  # cycle outside window
                    try:
                        strike = float(json.loads(rj).get("floor_strike") or 0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if strike <= 0:
                        continue
                    self.strategy.register_market(ticker, strike, om, cm)
                    tickers.append(ticker)
        finally:
            con.close()
        return tickers

    # -- dispatch (mirrors live.py _route) -----------------------------
    def _dispatch(self, event) -> None:
        # Book state for fill simulation
        if isinstance(event, BookEvent):
            self.execution.on_book(event)
        # Terminal settlement -> close position + PnL
        if isinstance(event, SettlementEvent):
            self.execution.on_settlement(event)
            return

        # Risk-state freshness updates
        if isinstance(event, SpotEvent):
            self.risk_state.last_spot_ms[event.crypto.value] = event.ts_ms
            self.risk_state.now_ms = max(self.risk_state.now_ms, event.ts_ms)
        elif isinstance(event, (BookEvent, TradeEvent)):
            self.risk_state.now_ms = max(self.risk_state.now_ms, event.recv_ms)
        elif isinstance(event, LifecycleEvent):
            if event.strike and event.open_ms and event.close_ms:
                self.strategy.register_market(
                    event.ticker, event.strike, event.open_ms, event.close_ms,
                )
            return

        decision = self.strategy.on_event(event)
        if decision is None:
            return
        decision = self.envelope.check(decision, self.risk_state)
        self.log.write({
            "kind": "decision",
            "ticker": decision.ticker,
            "action": decision.action.value,
            "side": decision.side.value if decision.side else None,
            "size": decision.size,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "diagnostics": decision.diagnostics,
        })
        self._decision_count += 1
        self.execution.submit(decision)
