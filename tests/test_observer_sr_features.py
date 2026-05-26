"""Phase 14.2a — verify the observer adds bb_pos / pivot / liquidity fields
to its book_at_1hr_pretrigger envelopes."""

from __future__ import annotations

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.strategies.hourglass_observer import HourglassObserverStrategy


class _CollectingLog:
    def __init__(self):
        self.events: list[dict] = []
    def write(self, env: dict) -> None:
        self.events.append(env)


class _FakePoller:
    """Stub liquidity poller returning canned depth data."""
    def __init__(self, depth=None):
        self._depth = depth or {
            "mid": 75000.0, "spread_bps": 1.5,
            "bid_depth_0p5pct": 12.5, "ask_depth_0p5pct": 15.7,
            "bid_depth_1pct": 38.8, "ask_depth_1pct": 70.4,
        }
    def get_depth(self, crypto):
        return self._depth


def _warmup_24h_spot(obs, base_ms, crypto=Crypto.BTC, price=75000.0):
    """Seed 25 hours of 1-min spot ticks for full S/R window coverage."""
    import math
    for i in range(25 * 60):  # 1500 samples
        ts = base_ms + i * 60_000
        # Sine wave around base price ±0.5%
        p = price + price * 0.005 * math.sin(i * 0.1)
        obs.on_event(SpotEvent(
            crypto=crypto, venue=Venue.BITSTAMP,
            ts_ms=ts, recv_ms=ts, price=p,
        ))


def _book(ticker, ts_ms, yes_bid=400, yes_ask=420, no_bid=580, no_ask=600):
    return BookEvent(
        ticker=ticker, ts_ms=ts_ms, recv_ms=ts_ms,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_levels=(), no_levels=(),
    )


# ---- envelope contains the new fields ------------------------------------

def test_envelope_has_bb_pos_fields():
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_24h_spot(obs, base)
    open_ms = base + 25 * 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book("KXBTCD-T", open_ms + 30 * 60_000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    e = envs[0]
    for key in ("bb_pos_1h", "bb_pos_4h", "bb_pos_24h"):
        assert key in e, f"missing {key}"
        assert e[key] is not None, f"{key} unexpectedly None"


def test_envelope_has_pivot_fields():
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_24h_spot(obs, base)
    open_ms = base + 25 * 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book("KXBTCD-T", open_ms + 30 * 60_000))
    e = [x for x in log.events if x["kind"] == "book_at_1hr_pretrigger"][0]
    for key in ("pivot", "pivot_R1", "pivot_S1", "dist_to_R1", "dist_to_S1",
                 "window_24h_high", "window_24h_low"):
        assert key in e
        assert e[key] is not None
    # Sanity: R1 > pivot > S1
    assert e["pivot_R1"] > e["pivot"] > e["pivot_S1"]


def test_envelope_no_liquidity_fields_when_poller_absent():
    """When liquidity_poller is None, envelope must NOT include bitstamp_*
    fields (we don't want spurious None entries cluttering downstream)."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log, liquidity_poller=None)
    _warmup_24h_spot(obs, base)
    open_ms = base + 25 * 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book("KXBTCD-T", open_ms + 30 * 60_000))
    e = [x for x in log.events if x["kind"] == "book_at_1hr_pretrigger"][0]
    assert "bitstamp_bid_depth_0p5pct" not in e
    assert "bitstamp_spread_bps" not in e


def test_envelope_includes_liquidity_when_poller_present():
    base = 1_700_000_000_000
    log = _CollectingLog()
    poller = _FakePoller()
    obs = HourglassObserverStrategy(log_writer=log, liquidity_poller=poller)
    _warmup_24h_spot(obs, base)
    open_ms = base + 25 * 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book("KXBTCD-T", open_ms + 30 * 60_000))
    e = [x for x in log.events if x["kind"] == "book_at_1hr_pretrigger"][0]
    assert e["bitstamp_bid_depth_0p5pct"] == 12.5
    assert e["bitstamp_ask_depth_0p5pct"] == 15.7
    assert e["bitstamp_spread_bps"] == 1.5
    assert e["bitstamp_mid"] == 75000.0


def test_envelope_handles_poller_exception_gracefully():
    """Poller that raises must not crash the observer; envelope still emits
    with a bitstamp_poll_error field."""
    class _BoomPoller:
        def get_depth(self, crypto):
            raise RuntimeError("network down")
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log, liquidity_poller=_BoomPoller())
    _warmup_24h_spot(obs, base)
    open_ms = base + 25 * 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book("KXBTCD-T", open_ms + 30 * 60_000))
    e = [x for x in log.events if x["kind"] == "book_at_1hr_pretrigger"][0]
    assert "bitstamp_poll_error" in e
    assert "network down" in e["bitstamp_poll_error"]
    # Core envelope fields still present
    assert e["spot"] is not None
    assert e["bb_pos_4h"] is not None


def test_long_spot_history_trimmed_to_25h():
    """Long history retention window is 25h. Older ticks must be dropped."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    # Inject 30h of spot ticks
    for i in range(30 * 60):
        ts = base + i * 60_000
        obs.on_event(SpotEvent(crypto=Crypto.BTC, venue=Venue.BITSTAMP,
                                ts_ms=ts, recv_ms=ts, price=75000.0 + i * 0.1))
    buf = obs._long_spot_history.get("BTC", [])
    # The buffer should retain ~25h worth; tolerance for one extra
    assert 25 * 60 <= len(buf) <= 25 * 60 + 5
    # Oldest entry should be within 25h of the latest
    assert buf[-1][0] - buf[0][0] <= 25 * 3_600_000 + 60_000


# ---- backward compatibility: existing tests must still pass --------------

def test_envelope_still_has_core_fields():
    """Phase 14.2a additions are purely additive — core schema unchanged."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_24h_spot(obs, base)
    open_ms = base + 25 * 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book("KXBTCD-T", open_ms + 30 * 60_000))
    e = [x for x in log.events if x["kind"] == "book_at_1hr_pretrigger"][0]
    for key in ("ticker", "ts_ms", "cycle_open_ms", "cycle_close_ms",
                 "elapsed_min", "tau_min", "window_label",
                 "yes_bid", "yes_ask", "no_bid", "no_ask",
                 "spot", "vol_30m", "bb_div", "bps_margin",
                 "favorite_side", "favorite_mid_decicents", "strike"):
        assert key in e, f"missing core field {key}"
