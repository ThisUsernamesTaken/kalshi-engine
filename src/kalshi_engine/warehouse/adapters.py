"""Typed readers and writers for the four warehouse data formats.

All readers are read-only and deliberately simple: plain lazy iterators, no
caching, no order-book reconstruction. A backtest replay is just::

    for event in reader.iter():
        ...

Every reader exposes ``iter()``, ``iter_range(start_ms, end_ms)`` and
``iter_ticker(ticker)``. Burn-in numeric fields are stored as dollar-strings
and converted to integer deci-cents here (0-1000, the engine-wide price unit)
- exact for Kalshi's tapered deci-cent tick. Contract sizes/counts are floats
since Kalshi supports fractional trading.
"""

from __future__ import annotations

import glob
import heapq
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from kalshi_engine.config import DERIVED_DIR
from kalshi_engine.core.events import BookEvent, SettlementEvent, SpotEvent, TradeEvent
from kalshi_engine.core.types import Crypto, Side, Venue

_SYMBOL_TO_CRYPTO = {
    "btcusd": Crypto.BTC,
    "ethusd": Crypto.ETH,
    "solusd": Crypto.SOL,
    "xrpusd": Crypto.XRP,
    "dogeusd": Crypto.DOGE,
}


def _decicents(dollar_str: object) -> int | None:
    """Convert a Kalshi dollar-string ('0.2200') to integer deci-cents (220)."""
    if dollar_str is None or dollar_str == "":
        return None
    return round(float(dollar_str) * 1000)


def _parse_levels(json_str: object) -> tuple[tuple[int, float], ...]:
    """Parse a ladder JSON ('[["0.0010","1652.00"],...]') into a tuple of
    (price_decicents, size_contracts)."""
    if not json_str:
        return ()
    out: list[tuple[int, float]] = []
    for price, size in json.loads(json_str):
        out.append((round(float(price) * 1000), float(size)))
    return tuple(out)


def _ro_connect(path: str, immutable: bool = True) -> sqlite3.Connection:
    """Open a SQLite database strictly read-only, Row-factory set.

    Pass ``immutable=False`` for an actively-written WAL database (e.g. the
    live burn-in capture). The default ``immutable=True`` is the standard
    safe-read path when the DB is known to be quiescent.
    """
    qs = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"file:{Path(path).as_posix()}?{qs}"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


class BurninReader:
    """Streams events from a burn-in capture SQLite (the 121 GB DB pattern).

    Emits ``BookEvent`` (kalshi_l2_event), ``TradeEvent`` (kalshi_trade_event),
    ``SpotEvent`` (spot_quote_event) and ``SettlementEvent`` (kalshi_lifecycle_
    event rows with status='determined'). Events are merged across tables in
    receipt-timestamp order. Read-only.
    """

    _SPEC = (
        ("kalshi_l2_event", "_book"),
        ("kalshi_trade_event", "_trade"),
        ("spot_quote_event", "_spot"),
        ("kalshi_lifecycle_event", "_settlement"),
    )

    def __init__(self, path: str, immutable: bool = True) -> None:
        self.path = str(path)
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        self._con = _ro_connect(self.path, immutable=immutable)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> BurninReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- row -> event mappers --------------------------------------------
    @staticmethod
    def _book(r: sqlite3.Row) -> BookEvent:
        yes_bid = _decicents(r["best_yes_bid"]) or 0
        yes_ask = _decicents(r["best_yes_ask"]) or 0
        return BookEvent(
            ticker=r["market_ticker"],
            ts_ms=r["exchange_ts_ms"] or r["received_ts_ms"],
            recv_ms=r["received_ts_ms"],
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=1000 - yes_ask,
            no_ask=1000 - yes_bid,
            yes_levels=_parse_levels(r["yes_levels_json"]),
            no_levels=_parse_levels(r["no_levels_json"]),
        )

    @staticmethod
    def _trade(r: sqlite3.Row) -> TradeEvent:
        taker = r["taker_side"]
        return TradeEvent(
            ticker=r["market_ticker"],
            ts_ms=r["exchange_ts_ms"] or r["received_ts_ms"],
            recv_ms=r["received_ts_ms"],
            price=_decicents(r["yes_price"]) or 0,
            count=float(r["count"] or 0.0),
            taker_side=Side(taker) if taker in ("yes", "no") else Side.YES,
        )

    @staticmethod
    def _spot(r: sqlite3.Row) -> SpotEvent | None:
        crypto = _SYMBOL_TO_CRYPTO.get((r["symbol"] or "").lower())
        if crypto is None:
            return None
        try:
            venue = Venue((r["venue"] or "").lower())
        except ValueError:
            return None
        px = r["mid"] if r["mid"] not in (None, "") else r["last"]
        if px in (None, ""):
            return None
        return SpotEvent(
            crypto=crypto,
            venue=venue,
            ts_ms=r["exchange_ts_ms"] or r["received_ts_ms"],
            recv_ms=r["received_ts_ms"],
            price=float(px),
        )

    @staticmethod
    def _settlement(r: sqlite3.Row) -> SettlementEvent | None:
        try:
            msg = json.loads(r["raw_json"]).get("msg", {})
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None
        result = msg.get("result")
        if result not in ("yes", "no"):
            return None
        det = msg.get("determination_ts")
        return SettlementEvent(
            ticker=r["market_ticker"],
            ts_ms=r["received_ts_ms"],
            recv_ms=r["received_ts_ms"],
            result=Side(result),
            settle_value=float(msg.get("settlement_value") or 0.0),
            determined_ms=int(det) * 1000 if det else r["received_ts_ms"],
        )

    # -- streaming -------------------------------------------------------
    def _gen(self, table: str, mapper, where: list[str], params: list):
        cur = self._con.cursor()
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY received_ts_ms"
        cur.execute(sql, params)
        for row in cur:
            event = mapper(row)
            if event is not None:
                yield (row["received_ts_ms"], event)

    def _iter(self, start_ms=None, end_ms=None, ticker=None):
        gens = []
        for table, mapper_name in self._SPEC:
            is_spot = table == "spot_quote_event"
            if ticker is not None and is_spot:
                continue  # spot is not ticker-scoped
            where: list[str] = []
            params: list = []
            if start_ms is not None:
                where.append("received_ts_ms >= ?")
                params.append(start_ms)
            if end_ms is not None:
                where.append("received_ts_ms <= ?")
                params.append(end_ms)
            if ticker is not None and not is_spot:
                where.append("market_ticker = ?")
                params.append(ticker)
            if table == "kalshi_lifecycle_event":
                where.append("status = 'determined'")
            gens.append(self._gen(table, getattr(self, mapper_name), where, params))
        for _ts, event in heapq.merge(*gens, key=lambda pair: pair[0]):
            yield event

    def iter(self) -> Iterator:
        """All Book/Trade/Spot/Settlement events, receipt-time ordered."""
        return self._iter()

    def iter_range(self, start_ms: int, end_ms: int) -> Iterator:
        """Events with received_ts_ms in [start_ms, end_ms]."""
        return self._iter(start_ms=start_ms, end_ms=end_ms)

    def iter_ticker(self, ticker: str) -> Iterator:
        """Market events (Book/Trade/Settlement) for one ticker. Spot excluded."""
        return self._iter(ticker=ticker)

    # -- indexed per-ticker streaming (fast over huge live captures) -----
    def _gen_indexed_kalshi(
        self,
        table: str,
        mapper,
        ticker: str,
        start_ms: int,
        end_ms: int,
        extra_where: str = "",
    ):
        """Index-using per-ticker query against a kalshi_* event table.

        The index ``(market_ticker, COALESCE(exchange_ts_ms, received_ts_ms))``
        makes both the ticker filter and the time window O(log n) -- a full
        scan against a 100+ GB capture would otherwise take hours.
        """
        cur = self._con.cursor()
        sql = (
            f"SELECT * FROM {table} "
            f"WHERE market_ticker = ? "
            f"AND COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ?"
        )
        if extra_where:
            sql += " AND " + extra_where
        sql += " ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id"
        cur.execute(sql, (ticker, start_ms, end_ms))
        for row in cur:
            event = mapper(row)
            if event is None:
                continue
            ts = row["exchange_ts_ms"] or row["received_ts_ms"]
            yield (ts, event)

    def _gen_indexed_spot(
        self,
        symbols: list[str],
        start_ms: int,
        end_ms: int,
    ):
        """Index-using spot query (per (symbol, venue) prefix)."""
        if not symbols:
            return
        cur = self._con.cursor()
        placeholders = ",".join(["?"] * len(symbols))
        sql = (
            f"SELECT * FROM spot_quote_event "
            f"WHERE symbol IN ({placeholders}) "
            f"AND COALESCE(exchange_ts_ms, received_ts_ms) BETWEEN ? AND ? "
            f"ORDER BY COALESCE(exchange_ts_ms, received_ts_ms), event_id"
        )
        cur.execute(sql, (*symbols, start_ms, end_ms))
        for row in cur:
            event = self._spot(row)
            if event is None:
                continue
            ts = row["exchange_ts_ms"] or row["received_ts_ms"]
            yield (ts, event)

    def iter_window(
        self,
        start_ms: int,
        end_ms: int,
        tickers: Iterable[str],
        symbols: Iterable[str] = (),
    ) -> Iterator:
        """Indexed streaming of events in [start_ms, end_ms] for a fixed set
        of tickers (kalshi book/trade/lifecycle) and symbols (spot).

        This path uses the live-capture indexes, so it stays fast against a
        100+ GB DB. Use it from a replay loop that has pre-resolved which
        markets and which spot symbols are in scope.
        """
        ticker_list = list(tickers)
        symbol_list = list(symbols)
        gens = []
        for ticker in ticker_list:
            gens.append(self._gen_indexed_kalshi(
                "kalshi_l2_event", self._book, ticker, start_ms, end_ms,
            ))
            gens.append(self._gen_indexed_kalshi(
                "kalshi_trade_event", self._trade, ticker, start_ms, end_ms,
            ))
            gens.append(self._gen_indexed_kalshi(
                "kalshi_lifecycle_event", self._settlement, ticker, start_ms, end_ms,
                extra_where="status = 'determined'",
            ))
        if symbol_list:
            gens.append(self._gen_indexed_spot(symbol_list, start_ms, end_ms))
        for _ts, event in heapq.merge(*gens, key=lambda pair: pair[0]):
            yield event


class CaptureReader:
    """Streams gradient-engine 4-stream JSONL captures from a capture dir.

    Validates ``schema_version`` on every file and raises loudly on mismatch.
    ``iter()`` merges raw_events / decisions / paper_fills records across all
    sessions in the directory, time-ordered. Summary and settlement files are
    exposed via ``summaries()`` and ``settlements()``.
    """

    SCHEMA_VERSION = 1

    def __init__(self, directory: str) -> None:
        self.dir = str(directory)
        if not os.path.isdir(self.dir):
            raise NotADirectoryError(self.dir)
        self._streams = {
            "raw_events": sorted(glob.glob(os.path.join(self.dir, "raw_events_*.jsonl"))),
            "decisions": sorted(glob.glob(os.path.join(self.dir, "decisions_*.jsonl"))),
            "paper_fills": sorted(glob.glob(os.path.join(self.dir, "paper_fills_*.jsonl"))),
        }
        self._validate_versions()

    def _validate_versions(self) -> None:
        for files in self._streams.values():
            for fp in files:
                ver = self._first_version(fp)
                if ver is not None and ver != self.SCHEMA_VERSION:
                    raise ValueError(
                        f"schema_version mismatch in {fp}: got {ver}, "
                        f"expected {self.SCHEMA_VERSION}"
                    )

    @staticmethod
    def _first_version(fp: str):
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    return json.loads(line).get("schema_version")
        return None

    @staticmethod
    def _ts(rec: dict) -> int:
        return rec.get("ts_ms") or rec.get("wall_clock_ts_ms") or 0

    @staticmethod
    def _ticker(rec: dict):
        return (
            rec.get("ticker")
            or rec.get("market_ticker")
            or (rec.get("market_state") or {}).get("market_ticker")
        )

    def _file_gen(self, stream: str, fp: str):
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                rec["_stream"] = stream
                yield (self._ts(rec), rec)

    def _iter(self, start_ms=None, end_ms=None, ticker=None):
        gens = [
            self._file_gen(stream, fp)
            for stream, files in self._streams.items()
            for fp in files
        ]
        for ts, rec in heapq.merge(*gens, key=lambda pair: pair[0]):
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            if ticker is not None and self._ticker(rec) != ticker:
                continue
            yield rec

    def iter(self) -> Iterator:
        """All raw_events / decisions / paper_fills records, time-ordered."""
        return self._iter()

    def iter_range(self, start_ms: int, end_ms: int) -> Iterator:
        return self._iter(start_ms=start_ms, end_ms=end_ms)

    def iter_ticker(self, ticker: str) -> Iterator:
        return self._iter(ticker=ticker)

    def summaries(self) -> list[dict]:
        """Parsed summary_*.json session files in the directory."""
        out = []
        for fp in sorted(glob.glob(os.path.join(self.dir, "summary_*.json"))):
            txt = Path(fp).read_text(encoding="utf-8").strip()
            if txt:
                out.append(json.loads(txt))
        return out

    def settlements(self) -> list[dict]:
        """The settlements list from settlements.json, or [] if absent."""
        fp = os.path.join(self.dir, "settlements.json")
        if not os.path.exists(fp):
            return []
        txt = Path(fp).read_text(encoding="utf-8").strip()
        return json.loads(txt).get("settlements", []) if txt else []


class SpotParquetReader:
    """Reads 1-minute OHLC spot data from a derived/spot_backfill parquet.

    `crypto` in {BTC,ETH,SOL,XRP,DOGE}; `source` in {coinbase,bitstamp,fusion}.
    ``iter()`` yields one SpotEvent per minute bar (price = bar close).
    """

    def __init__(self, crypto, source, spot_dir: str | None = None) -> None:
        self.crypto = Crypto(crypto)
        self.venue = Venue(source)
        base = Path(spot_dir) if spot_dir else (DERIVED_DIR / "spot_backfill")
        pattern = str(base / f"{self.crypto.value}_{self.venue.value}_*.parquet")
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"no spot parquet matching {pattern}")
        self.path = matches[0]
        self._df = pd.read_parquet(self.path)

    def _iter(self, start_ms=None, end_ms=None):
        for ts, close in zip(self._df["ts_ms"], self._df["close"]):
            ts = int(ts)
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts > end_ms:
                continue
            yield SpotEvent(
                crypto=self.crypto,
                venue=self.venue,
                ts_ms=ts,
                recv_ms=ts,
                price=float(close),
            )

    def iter(self) -> Iterator:
        return self._iter()

    def iter_range(self, start_ms: int, end_ms: int) -> Iterator:
        return self._iter(start_ms=start_ms, end_ms=end_ms)

    def iter_ticker(self, ticker: str) -> Iterator:
        raise NotImplementedError(
            "spot data is crypto-scoped, not ticker-scoped; use iter()"
        )

    def frame(self) -> pd.DataFrame:
        """The raw OHLC DataFrame (ts_ms, open, high, low, close, volume)."""
        return self._df


class LiveLogReader:
    """Replays the live engine's JSONL event log (boot/entry/exit/skip/...)."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)

    def _iter(self, start_ms=None, end_ms=None, ticker=None):
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("log_ts_ms", 0)
                if start_ms is not None and ts < start_ms:
                    continue
                if end_ms is not None and ts > end_ms:
                    continue
                if ticker is not None and rec.get("ticker") != ticker:
                    continue
                yield rec

    def iter(self) -> Iterator:
        return self._iter()

    def iter_range(self, start_ms: int, end_ms: int) -> Iterator:
        return self._iter(start_ms=start_ms, end_ms=end_ms)

    def iter_ticker(self, ticker: str) -> Iterator:
        return self._iter(ticker=ticker)


class LiveLogWriter:
    """Append-only writer for the live engine's JSONL event log.

    Each event is flushed and fsync'd so a crash cannot lose a recorded
    event. ``log_ts_ms`` is stamped if the caller did not supply it.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> dict:
        """Append one event; returns the record actually written."""
        rec = dict(event)
        rec.setdefault("log_ts_ms", int(time.time() * 1000))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rec
