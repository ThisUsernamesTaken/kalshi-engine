"""Phase 14.18 — DCA dense book sampler (log-only sidecar).

Tier-2 dense sampler for the DCA accumulate-into-dips design
(`_tmp_analysis/dca_design/DCA_IMPLEMENTATION.md` item D). Polls the favorite
(and near-favorite) 1hr crypto markets every few seconds and logs the
top-of-book quote, depth, and the V13B score components plus seconds-into-cycle
to a dedicated JSONL file. The existing 1hr trader produced a log too sparse to
backtest DCA add-rungs (a resting limit ladder fills on intra-cycle pullbacks);
this captures the dense mid path needed for that backtest.

Pure observation: no orders, no risk envelope, no execution. Runs as a
SEPARATE process/service from the 1hr trader — it only opens read-only Kalshi
WS subscriptions and a Bitstamp spot poll, both safe concurrent with live
trading. The trader (`KalshiEngine1hr`) is untouched.

Differs from ``observe_1hr.py``: instead of one-shot envelopes at fixed
T+x windows, this samples on a wall-clock timer (every ``--sample-interval-s``
seconds, default 7s — in the 5-10s band) so the intra-cycle book path is
captured densely. Markets are sampled only once their favorite mid clears
``--min-favorite-mid-dc`` (default 550 = $0.55) so far-OTM flatline strikes
don't flood the log.

Run:
    python -m kalshi_engine.bin.observe_dca_sample --cryptos BTC,ETH
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from kalshi_engine.config import MODELS_DIR, RAW_DIR
from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto
from kalshi_engine.execution.kalshi_client import KalshiClient
from kalshi_engine.feeds.kalshi_ws import KalshiWebSocketFeed
from kalshi_engine.feeds.spot_ws import SpotFeed
from kalshi_engine.risk.envelope import crypto_of_ticker
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_BPS_MULT,
    BPS_STRONG_MULT,
    DEEP_DIV_SKIP,
    DIV_BAND_UPPER,
    Phase4CutpointsModel,
    SUPER_BAND_HIGH,
    SUPER_BAND_LOW,
)
from kalshi_engine.strategies.favorite_chase.rules import (
    compute_strike_distance_bps,
    select_favorite,
)
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = str(RAW_DIR / "live_logs" / "dca_book_sample.jsonl")

# Phase 14.8 — reject non-1hr markets (25h-cycle pollution), mirroring
# observe_1hr / observe_inxu.
MAX_1HR_CYCLE_MIN = 90

SERIES_1HR_FOR_CRYPTO = {
    Crypto.BTC: "KXBTCD",
    Crypto.ETH: "KXETHD",
    Crypto.SOL: "KXSOLD",
    Crypto.XRP: "KXXRPD",
    Crypto.DOGE: "KXDOGED",
}


def _diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


def _strike_from_market(m: dict) -> float:
    """Best-effort strike extraction (mirrors observe_1hr._strike_from_market):
    try ``floor_strike``, else parse the trailing ``-T<numeric>`` ticker tail."""
    fs = m.get("floor_strike")
    if fs is not None:
        try:
            return float(fs)
        except (TypeError, ValueError):
            pass
    ticker = m.get("ticker") or ""
    idx = ticker.rfind("-T")
    if idx == -1:
        return 0.0
    try:
        return float(ticker[idx + 2:])
    except (TypeError, ValueError):
        return 0.0


def _read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="kalshi_engine DCA dense book sampler (log-only sidecar)")
    p.add_argument("--cryptos", default="BTC,ETH",
                   help="comma-separated crypto symbols (default BTC,ETH — the "
                        "DCA forward-test cohort)")
    p.add_argument("--sample-interval-s", type=float, default=7.0,
                   help="seconds between dense samples per active market "
                        "(default 7.0; the design's 5-10s band)")
    p.add_argument("--min-favorite-mid-dc", type=float, default=600.0,
                   help="lower bound of the favorite-mid sampling band "
                        "(decicents). Below this a market is undecided "
                        "(~50/50) noise (default 600 = $0.60)")
    p.add_argument("--max-favorite-mid-dc", type=float, default=970.0,
                   help="upper bound of the favorite-mid sampling band "
                        "(decicents). Above this the market is effectively "
                        "decided (deep-ITM flatline, no DCA dip to catch). "
                        "Together with --min, samples only the 'chase zone' "
                        "where DCA adds would rest/fill — bounds log volume "
                        "and excludes both useless extremes (default 970 = "
                        "$0.97; cf. the 1hr ladder's 750-950 entry band)")
    p.add_argument("--cutpoints-version", default="v1",
                   help="cutpoints artifact version for the V13B bps thresholds "
                        "(default v1 — matches the live 1hr trader)")
    p.add_argument("--spot-source", default="bitstamp",
                   choices=["bitstamp", "bitstamp-ws", "coinbase"],
                   help="spot price source (default bitstamp, as the 1hr engine)")
    p.add_argument("--discovery-interval-s", type=float, default=60.0,
                   help="how often to refresh market registration from REST "
                        "(default 60s, handles cycle rollovers)")
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH,
                   help="output JSONL path (default: HDD warehouse)")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run forever; else exit after N seconds")
    return p.parse_args(argv)


def _size_at(levels, price):
    """Top-of-book size (contracts) at the given price, or None if absent.

    Each ``levels`` entry is (price_decicents, size_contracts); the best bid is
    the entry whose price matches ``yes_bid``/``no_bid``, etc.
    """
    for p, sz in levels:
        if p == price:
            return float(sz)
    return None


def build_dca_sample(
    book: BookEvent,
    meta: dict,
    state: FavoriteChaseState,
    bps_threshold: float,
    sample_ms: int,
) -> dict:
    """Build one ``dca_book_sample`` record from the latest book + warmed state.

    Pure (no I/O, no WS) so it is unit-testable without the live feeds. The
    favorite is chosen by mid (yes_mid vs no_mid) so the record exists before a
    side hits the 75c rule; the 75c-rule favorite is recorded separately for
    cross-reference. The V13B score components reuse the same Brownian-bridge
    and bps math as the live model (single source of truth via
    FavoriteChaseState + the phase4_cutpoints constants), so the dense sample
    is directly comparable to the trader's gate.
    """
    strike = float(meta["strike"])
    open_ms = int(meta["open_ms"])
    close_ms = int(meta["close_ms"])

    yes_mid = (book.yes_bid + book.yes_ask) / 2.0
    no_mid = (book.no_bid + book.no_ask) / 2.0
    if yes_mid >= no_mid:
        fav_side, fav_mid = "yes", yes_mid
    else:
        fav_side, fav_mid = "no", no_mid

    fav_75c = select_favorite(book)
    fav_75c_side = fav_75c.value if fav_75c is not None else None

    spot = state.latest_spot()
    vol = state.vol_30m()
    tau_min = (close_ms - sample_ms) / 60_000.0

    bb_div = None
    bps_margin = None
    d_norm = None
    vol_pct = None
    bb_div_band = side_no = side_yes = bps_strong = super_band = s_bps = None
    v13b_score = None

    if spot is not None and vol is not None and strike > 0:
        bps_margin = abs(compute_strike_distance_bps(spot, strike))
        sigma = vol / 1e4
        if sigma > 0 and tau_min > 0:
            bb_yes = state.bb_fair(spot, strike, sigma, tau_min)
            bb_fav = bb_yes if fav_side == "yes" else 1.0 - bb_yes
            bb_div = state.bb_div(fav_mid, bb_fav)
        if vol > 0 and tau_min > 0:
            d_norm = bps_margin / (vol * (tau_min ** 0.5))
        vol_pct = state.vol_30m_percentile(vol)

        # V13B score components — same formula + constants as the live
        # 5tier_v13b model (phase4_cutpoints). Computed unconditionally here
        # (gate-independent) so every dense sample carries the full feature
        # vector for backtest, rather than only the samples that would pass
        # the trader's hard gates.
        side_no = 1 if fav_side == "no" else 0
        side_yes = 1 - side_no
        bps_strong = 1 if bps_margin > BPS_STRONG_MULT * bps_threshold else 0
        s_bps = 1 if bps_margin > ALIGN_BPS_MULT * bps_threshold else 0
        if bb_div is not None:
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            v13b_score = (2.0 * bb_div_band + 1.5 * side_no
                          + 2.0 * bps_strong + 1.0 * super_band)

    fav_bid = book.yes_bid if fav_side == "yes" else book.no_bid
    fav_bid_levels = book.yes_levels if fav_side == "yes" else book.no_levels
    book_size_top_fav_bid = _size_at(fav_bid_levels, fav_bid)

    return {
        "kind": "dca_book_sample",
        "ticker": book.ticker,
        "crypto": crypto_of_ticker(book.ticker),
        "series": meta.get("series"),
        "ts_ms": sample_ms,
        "book_recv_ms": book.recv_ms,
        "book_age_ms": sample_ms - book.recv_ms,
        "cycle_open_ms": open_ms,
        "cycle_close_ms": close_ms,
        "sec_into_cycle": (sample_ms - open_ms) / 1000.0,
        "elapsed_min": (sample_ms - open_ms) / 60_000.0,
        "tau_min": tau_min,
        "strike": strike,
        "yes_bid": book.yes_bid, "yes_ask": book.yes_ask,
        "no_bid": book.no_bid, "no_ask": book.no_ask,
        "yes_bid_size_fp": _size_at(book.yes_levels, book.yes_bid),
        "yes_ask_size_fp": _size_at(book.yes_levels, book.yes_ask),
        "no_bid_size_fp": _size_at(book.no_levels, book.no_bid),
        "no_ask_size_fp": _size_at(book.no_levels, book.no_ask),
        "favorite_side": fav_side,
        "favorite_mid_decicents": fav_mid,
        "favorite_75c_side": fav_75c_side,
        "book_size_top_fav_bid_fp": book_size_top_fav_bid,
        "spot": spot,
        "vol_30m": vol,
        "vol_30m_pct": vol_pct,
        "bb_div": bb_div,
        "bps_margin": bps_margin,
        "d_norm": d_norm,
        "bps_threshold": bps_threshold,
        "bb_div_band": bb_div_band,
        "side_no": side_no,
        "side_yes": side_yes,
        "bps_strong": bps_strong,
        "super_band": super_band,
        "s_bps": s_bps,
        "v13b_score": v13b_score,
    }


class _DcaSampleState:
    """Registered markets + latest book per ticker + per-crypto rolling state.

    Holds one ``FavoriteChaseState`` per crypto (warmed from spot history for
    vol_30m / Brownian-bridge fair) and the most-recent ``BookEvent`` per
    ticker. The sampler reads the latest book on a timer rather than emitting
    per book event, so the cycle path is captured at a steady cadence even
    when the Kalshi WS goes quiet between updates.
    """

    def __init__(self) -> None:
        self.markets: dict[str, dict] = {}  # ticker -> {strike, open_ms, close_ms, series}
        self.latest_book: dict[str, BookEvent] = {}
        self._states: dict[str, FavoriteChaseState] = {}

    def register(self, ticker: str, strike: float, open_ms: int,
                 close_ms: int, series: str) -> None:
        self.markets[ticker] = {
            "strike": float(strike), "open_ms": int(open_ms),
            "close_ms": int(close_ms), "series": series,
        }

    def state_for(self, crypto: str) -> FavoriteChaseState:
        if crypto not in self._states:
            self._states[crypto] = FavoriteChaseState(crypto)
        return self._states[crypto]

    def on_spot(self, event: SpotEvent) -> None:
        self.state_for(event.crypto.value).update_spot(event)

    def on_book(self, event: BookEvent) -> None:
        self.latest_book[event.ticker] = event

    def on_event(self, event) -> None:
        """Route an event — supports SpotFeed.bootstrap_warmup_into, which
        calls strategy.on_event(spot_event) during warmup."""
        if isinstance(event, SpotEvent):
            self.on_spot(event)
        elif isinstance(event, BookEvent):
            self.on_book(event)


class _RiskStateStub:
    """Minimal stub matching the spot warmup API; sampler has no risk state."""

    def __init__(self):
        self.now_ms = 0
        self.last_spot_ms: dict[str, int] = {}


def emit_samples(
    state: _DcaSampleState,
    bps_thresholds: dict,
    log: LiveLogWriter,
    sample_ms: int,
    min_favorite_mid_dc: float,
    max_favorite_mid_dc: float,
) -> int:
    """Emit one dense sample per active market whose favorite mid is in the
    chase-zone band [min, max]. Returns the number of samples written.
    Synchronous + side-effecting only through ``log`` so it is straightforward
    to test against a mocked book feed.

    The band excludes both useless extremes: below ``min`` the market is an
    undecided ~50/50 (no favorite yet), above ``max`` it is effectively decided
    (deep-ITM flatline — no DCA dip left to catch). Note a deep-ITM strike's
    favorite (winning) side sits near 1000, so a single floor would NOT exclude
    it; the upper bound is what keeps far-from-money flatlines out of the log.
    """
    written = 0
    for ticker, meta in list(state.markets.items()):
        book = state.latest_book.get(ticker)
        if book is None:
            continue
        # Only sample markets whose cycle is currently in progress.
        if not (meta["open_ms"] <= sample_ms < meta["close_ms"]):
            continue
        crypto = crypto_of_ticker(ticker)
        threshold = float(bps_thresholds.get(crypto, 0.0))
        rec = build_dca_sample(book, meta, state.state_for(crypto),
                               threshold, sample_ms)
        fav_mid = rec["favorite_mid_decicents"]
        if not (min_favorite_mid_dc <= fav_mid <= max_favorite_mid_dc):
            continue  # outside the chase-zone band — skip to bound log volume
        log.write(rec)
        written += 1
    return written


async def _discover_1hr_markets(
    client: KalshiClient, cryptos: list[Crypto], log: LiveLogWriter,
) -> list[dict]:
    out: list[dict] = []
    skipped_long = 0
    for crypto in cryptos:
        series = SERIES_1HR_FOR_CRYPTO[crypto]
        try:
            markets = await client.list_markets(
                series_ticker=series, status="open", limit=200,
            )
        except Exception as exc:
            log.write({"kind": "discovery_error", "series": series,
                       "error": repr(exc)})
            continue
        for m in markets:
            ticker = m.get("ticker")
            strike = _strike_from_market(m)
            open_ms = _iso_to_ms(m.get("open_time"))
            close_ms = _iso_to_ms(m.get("close_time"))
            if not ticker or strike <= 0 or open_ms is None or close_ms is None:
                continue
            dur_min = (close_ms - open_ms) / 60_000.0
            if dur_min > MAX_1HR_CYCLE_MIN:
                log.write({"kind": "discovery_skip_long_cycle", "series": series,
                           "ticker": ticker, "duration_minutes": dur_min,
                           "cap_minutes": MAX_1HR_CYCLE_MIN})
                skipped_long += 1
                continue
            out.append({"ticker": ticker, "strike": strike, "open_ms": open_ms,
                        "close_ms": close_ms, "series": series})
    counts: dict[str, int] = {}
    for m in out:
        counts[m["series"]] = counts.get(m["series"], 0) + 1
    log.write({"kind": "discovery", "count": len(out), "by_series": counts,
               "skipped_long_cycle_count": skipped_long})
    return out


async def _discovery_loop(
    client: KalshiClient, state: _DcaSampleState, cryptos: list[Crypto],
    log: LiveLogWriter, interval_seconds: float, kalshi_ws,
) -> None:
    """Periodically refresh market registration so cycle rollovers don't leave
    the sampler blind, extending the WS subscription for new tickers."""
    from collections import defaultdict
    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        try:
            newly: dict[str, list[str]] = defaultdict(list)
            for crypto in cryptos:
                series = SERIES_1HR_FOR_CRYPTO[crypto]
                try:
                    markets = await client.list_markets(
                        series_ticker=series, status="open", limit=200,
                    )
                except Exception as exc:
                    log.write({"kind": "discovery_error", "series": series,
                               "error": repr(exc)})
                    continue
                for m in markets:
                    ticker = m.get("ticker")
                    if not ticker or ticker in state.markets:
                        continue
                    strike = _strike_from_market(m)
                    open_ms = _iso_to_ms(m.get("open_time"))
                    close_ms = _iso_to_ms(m.get("close_time"))
                    if strike <= 0 or open_ms is None or close_ms is None:
                        continue
                    dur_min = (close_ms - open_ms) / 60_000.0
                    if dur_min > MAX_1HR_CYCLE_MIN:
                        log.write({"kind": "discovery_skip_long_cycle",
                                   "series": series, "ticker": ticker,
                                   "duration_minutes": dur_min,
                                   "cap_minutes": MAX_1HR_CYCLE_MIN})
                        continue
                    state.register(ticker, strike, open_ms, close_ms, series)
                    newly[series].append(ticker)
            if newly:
                log.write({"kind": "market_discovery",
                           "newly_registered_count": sum(len(v) for v in newly.values()),
                           "total_registered": len(state.markets),
                           "by_series": {s: len(t) for s, t in newly.items()}})
                new_tickers = [t for ts in newly.values() for t in ts]
                try:
                    added = await kalshi_ws.add_tickers(new_tickers)
                    log.write({"kind": "ws_subscription_extended",
                               "added_count": added, "tickers": new_tickers})
                except Exception as exc:
                    log.write({"kind": "ws_subscription_extend_error",
                               "error": repr(exc), "tickers": new_tickers})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.write({"kind": "discovery_loop_error", "error": repr(exc)})


async def _run_loop(
    state: _DcaSampleState, bps_thresholds: dict, kalshi_ws, spot_feed,
    log: LiveLogWriter, sample_interval_s: float, min_favorite_mid_dc: float,
    max_favorite_mid_dc: float, duration_s: float,
) -> None:
    """Run the WS pump, spot pump, and sampling timer until the deadline.

    The Kalshi pump restarts its iterator on any unexpected error (Phase 14.17
    OverflowError survival net) so a single bad frame can't leave the sampler
    permanently deaf; malformed frames are already skipped inside
    KalshiWebSocketFeed, this is the outer belt-and-suspenders.
    """
    deadline = (time.time() + duration_s) if duration_s > 0 else None

    async def pump_kalshi():
        while True:
            try:
                async for ev in kalshi_ws.events():
                    if isinstance(ev, BookEvent):
                        state.on_book(ev)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.write({"kind": "feed_error", "source": "kalshi",
                           "error": repr(exc)})
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    return

    async def pump_spot():
        while True:
            try:
                async for ev in spot_feed.events():
                    state.on_spot(ev)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.write({"kind": "feed_error", "source": "spot",
                           "error": repr(exc)})
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    return

    async def sampler():
        while True:
            try:
                await asyncio.sleep(sample_interval_s)
            except asyncio.CancelledError:
                return
            try:
                emit_samples(state, bps_thresholds, log,
                             int(time.time() * 1000), min_favorite_mid_dc,
                             max_favorite_mid_dc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.write({"kind": "sampler_error", "error": repr(exc)})

    tasks = [
        asyncio.create_task(pump_kalshi()),
        asyncio.create_task(pump_spot()),
        asyncio.create_task(sampler()),
    ]
    try:
        if deadline is None:
            await asyncio.Event().wait()  # run until cancelled
        else:
            remaining = deadline - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
    finally:
        for t in tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


async def _amain(args: argparse.Namespace) -> int:
    _diag("amain entered")
    key_path = os.environ.get("KALSHI_API_KEY_PATH")
    if not key_path or not Path(key_path).exists():
        print("ERROR: KALSHI_API_KEY_PATH missing or invalid", file=sys.stderr)
        return 2
    creds = _read_env_file(key_path)
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path or not Path(pem_path).exists():
        print("ERROR: bad Kalshi credentials", file=sys.stderr)
        return 2
    pem_bytes = Path(pem_path).read_bytes()
    _diag(f"creds loaded; pem={len(pem_bytes)}B")

    try:
        cryptos = [Crypto(c.strip().upper()) for c in args.cryptos.split(",") if c.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --cryptos: {exc}", file=sys.stderr)
        return 2

    cutpoints_path = (
        MODELS_DIR / "phase4_cutpoints" / args.cutpoints_version / "cutpoints.json"
    )
    if not cutpoints_path.exists():
        print(f"ERROR: cutpoints artifact not found: {cutpoints_path}",
              file=sys.stderr)
        return 2
    model = Phase4CutpointsModel(cutpoints_path=str(cutpoints_path))
    bps_thresholds = dict(model.bps_thresholds)

    log = LiveLogWriter(args.log_path)
    state = _DcaSampleState()
    spot_feed = SpotFeed(cryptos, spot_source=args.spot_source)

    _diag("entering KalshiClient")
    async with KalshiClient(api_key, pem_bytes) as client:
        _diag("discovery start")
        markets = await _discover_1hr_markets(client, cryptos, log)
        _diag(f"discovered {len(markets)} 1hr markets")
        for m in markets:
            state.register(m["ticker"], m["strike"], m["open_ms"],
                           m["close_ms"], m["series"])
        if not markets:
            log.write({"kind": "boot_abort", "reason": "no_1hr_markets_discovered"})
            print("ERROR: no 1hr markets discovered", file=sys.stderr)
            return 3

        _diag("draining spot warmup ...")
        warmup_n = await spot_feed.bootstrap_warmup_into(state, _RiskStateStub())
        _diag(f"warmup drained; {warmup_n} spot events")

        log.write({
            "kind": "boot",
            "process": "dca_book_sampler",
            "cryptos": [c.value for c in cryptos],
            "sample_interval_s": args.sample_interval_s,
            "min_favorite_mid_dc": args.min_favorite_mid_dc,
            "max_favorite_mid_dc": args.max_favorite_mid_dc,
            "cutpoints_version": args.cutpoints_version,
            "bps_thresholds": bps_thresholds,
            "spot_source": args.spot_source,
            "markets_registered": len(markets),
            "warmup_events_drained": warmup_n,
            "log_path": str(args.log_path),
        })

        kalshi_ws = KalshiWebSocketFeed(
            client.signer, tickers=[m["ticker"] for m in markets],
        )
        discovery_task = asyncio.create_task(_discovery_loop(
            client, state, cryptos, log, args.discovery_interval_s, kalshi_ws,
        ))
        _diag("entering run loop")
        try:
            await _run_loop(
                state, bps_thresholds, kalshi_ws, spot_feed, log,
                args.sample_interval_s, args.min_favorite_mid_dc,
                args.max_favorite_mid_dc, args.duration_s,
            )
        finally:
            discovery_task.cancel()
            try:
                await discovery_task
            except (asyncio.CancelledError, Exception):
                pass
        log.write({"kind": "shutdown", "process": "dca_book_sampler"})
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
