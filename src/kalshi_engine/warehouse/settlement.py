"""Settlement resolution for backtest joins.

Primary source: the burn-in ``kalshi_lifecycle_event`` table - a row with
status='determined' carries the official ``result`` and ``settlement_value``.
Fallback: synthetic settlement - the spot price at the market's close
compared against its strike. The synthetic method was validated at 97.6%
agreement with known real outcomes on 124 BTC markets.

Synthetic spot is taken from the clean fusion backfill parquet when it
covers the market's close; otherwise from the burn-in's own
``spot_quote_event`` table (which always co-covers that capture's markets).
``resolve()`` reports which source it used via ``ResolvedSettlement.source``.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from kalshi_engine.core.events import SettlementEvent
from kalshi_engine.core.types import Crypto, Side
from kalshi_engine.warehouse.adapters import SpotParquetReader, _ro_connect

_PARQUET_STALE_MS = 5 * 60 * 1000  # reject a fusion bar > 5 min from close


class FallbackPolicy(str, Enum):
    """How SettlementResolver chooses between lifecycle and synthetic sources."""

    STRICT = "strict"                      # lifecycle only; None if absent
    PERMISSIVE = "permissive"              # lifecycle, else synthetic
    FORCED_SYNTHETIC = "forced_synthetic"  # synthetic always (test mode)


@dataclass(frozen=True)
class ResolvedSettlement:
    """A settlement event plus the source that produced it."""

    event: SettlementEvent
    source: str  # 'lifecycle' | 'synthetic'


_TICKER_CRYPTO = (
    ("KXBTC", Crypto.BTC),
    ("KXETH", Crypto.ETH),
    ("KXSOL", Crypto.SOL),
    ("KXXRP", Crypto.XRP),
    ("KXDOGE", Crypto.DOGE),
)


def _crypto_of(ticker: str) -> Crypto | None:
    for prefix, crypto in _TICKER_CRYPTO:
        if ticker.startswith(prefix):
            return crypto
    return None


def _iso_to_ms(value: object) -> int | None:
    """Parse a market_dim time field (ISO-8601 string or epoch) to UTC ms."""
    if value is None or value == "":
        return None
    s = str(value)
    if s.isdigit():
        v = int(s)
        return v * 1000 if v < 1_000_000_000_000 else v
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


class SettlementResolver:
    """Resolves the terminal outcome of a Kalshi market for backtest joins."""

    def __init__(
        self,
        burnin_path: str,
        spot_dir: str | None = None,
        policy: FallbackPolicy = FallbackPolicy.PERMISSIVE,
    ) -> None:
        self.burnin_path = str(burnin_path)
        self.spot_dir = spot_dir
        self.policy = FallbackPolicy(policy)
        self._con = _ro_connect(self.burnin_path)
        self._parquet: dict[Crypto, tuple[list[int], list[float]] | None] = {}

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> SettlementResolver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API ------------------------------------------------------
    def resolve(self, ticker: str) -> ResolvedSettlement | None:
        """Resolve one ticker per the configured fallback policy."""
        if self.policy is FallbackPolicy.FORCED_SYNTHETIC:
            syn = self._synthetic(ticker)
            return ResolvedSettlement(syn, "synthetic") if syn else None
        lifecycle = self._from_lifecycle(ticker)
        if lifecycle is not None:
            return ResolvedSettlement(lifecycle, "lifecycle")
        if self.policy is FallbackPolicy.STRICT:
            return None
        syn = self._synthetic(ticker)
        return ResolvedSettlement(syn, "synthetic") if syn else None

    def resolve_all(self, tickers) -> dict[str, ResolvedSettlement | None]:
        """Bulk resolve - a {ticker: ResolvedSettlement|None} dict for joins."""
        return {t: self.resolve(t) for t in tickers}

    # -- primary: lifecycle ---------------------------------------------
    def _from_lifecycle(self, ticker: str) -> SettlementEvent | None:
        row = self._con.execute(
            "SELECT * FROM kalshi_lifecycle_event "
            "WHERE market_ticker = ? AND status = 'determined' "
            "ORDER BY received_ts_ms LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None:
            return None
        try:
            msg = json.loads(row["raw_json"]).get("msg", {})
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None
        result = msg.get("result")
        if result not in ("yes", "no"):
            return None
        det = msg.get("determination_ts")
        return SettlementEvent(
            ticker=ticker,
            ts_ms=row["received_ts_ms"],
            recv_ms=row["received_ts_ms"],
            result=Side(result),
            settle_value=float(msg.get("settlement_value") or 0.0),
            determined_ms=int(det) * 1000 if det else row["received_ts_ms"],
        )

    # -- fallback: synthetic spot-vs-strike -----------------------------
    def _synthetic(self, ticker: str) -> SettlementEvent | None:
        md = self._con.execute(
            "SELECT * FROM market_dim WHERE ticker = ?", (ticker,)
        ).fetchone()
        if md is None:
            return None
        try:
            raw = json.loads(md["raw_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        strike = raw.get("floor_strike")
        crypto = _crypto_of(ticker)
        close_ms = _iso_to_ms(md["close_time"])
        if strike is None or crypto is None or close_ms is None:
            return None
        spot = self._spot_at(crypto, close_ms)
        if spot is None:
            return None
        result = Side.YES if spot >= float(strike) else Side.NO
        return SettlementEvent(
            ticker=ticker,
            ts_ms=close_ms,
            recv_ms=close_ms,
            result=result,
            settle_value=1.0 if result is Side.YES else 0.0,
            determined_ms=close_ms,
        )

    def _spot_at(self, crypto: Crypto, close_ms: int) -> float | None:
        """Spot at/just before close: fusion parquet, else burn-in spot_quote."""
        px = self._parquet_spot(crypto, close_ms)
        if px is not None:
            return px
        return self._burnin_spot(crypto, close_ms)

    def _parquet_spot(self, crypto: Crypto, close_ms: int) -> float | None:
        if crypto not in self._parquet:
            try:
                df = SpotParquetReader(crypto, "fusion", self.spot_dir).frame()
                self._parquet[crypto] = (
                    [int(t) for t in df["ts_ms"]],
                    [float(c) for c in df["close"]],
                )
            except FileNotFoundError:
                self._parquet[crypto] = None
        cache = self._parquet[crypto]
        if cache is None:
            return None
        ts, px = cache
        i = bisect.bisect_right(ts, close_ms) - 1
        if i < 0 or (close_ms - ts[i]) > _PARQUET_STALE_MS:
            return None
        return px[i]

    def _burnin_spot(self, crypto: Crypto, close_ms: int) -> float | None:
        symbol = f"{crypto.value.lower()}usd"
        row = self._con.execute(
            "SELECT mid, last FROM spot_quote_event "
            "WHERE symbol = ? AND received_ts_ms <= ? "
            "ORDER BY received_ts_ms DESC LIMIT 1",
            (symbol, close_ms),
        ).fetchone()
        if row is None:
            return None
        px = row["mid"] if row["mid"] not in (None, "") else row["last"]
        return float(px) if px not in (None, "") else None
