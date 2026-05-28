"""Commodity daily-ladder enum + Pyth-feed spec.

Separate from ``Crypto`` and ``Equity`` so the existing code paths are
untouched. Each ``Commodity`` value names a Kalshi daily above/below series
(``KX*D``) plus the Pyth Hermes price feed that is the *exact Kalshi
settlement source* for that product.

Discovery (2026-05-28, live against Kalshi + Pyth Hermes):

* **Gold ``KXGOLDD``** settles on Pyth ``Metal.XAU/USD``
  (feed ``765d2ba9…34bb2``). Verified live: $4499.56, ~7s lag, conf ±$0.52,
  strikes $4399–$4689 spaced $10. This is a clean, point-in-time spot feed
  and the exact settlement reference — ``bps_margin`` / ``bb_div`` are
  computed on the true settlement variable (no SPY/SPX-style basis problem).

* **Brent ``KXBRENTD``** settles on the **front-month ICE Brent futures
  contract**, named explicitly in each market's ``rules_primary`` (e.g.
  "using the BRENTQ6 contract" as of 2026-05-28). Pyth lists every Brent
  month feed (``Commodities.BRENT{M}{Y}/USD``) — but the active settlement
  contract (``BRENTQ6``) and all other near-month Brent feeds return
  ``price=0 / publish_time=0`` (never published) or are 130–200 days stale.
  The only *live* Brent reference on Pyth is ``Commodities.UKOILSPOT``
  ($92.44, ~7s) — a **spot** feed, NOT the futures contract Kalshi settles
  on. Trading Brent on UKOILSPOT would reintroduce a spot↔futures basis,
  the same defect that sank the SPY/SPX equity shadow. So Brent is
  framework-supported but ``pyth_live=False`` / ``live_enabled=False`` until
  its exact settlement feed publishes (or its spot↔Q6 basis is measured and
  accepted). ``WTI`` will hit the identical futures-feed gap (only
  ``USOILSPOT`` is live).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Commodity(str, Enum):
    """Commodity underlying tracked by a Kalshi daily above/below series."""

    GOLD = "GOLD"
    BRENT = "BRENT"


@dataclass(frozen=True, slots=True)
class CommoditySpec:
    """Static mapping from a Commodity to its Kalshi series + Pyth feed.

    ``pyth_feed_id`` is the *exact Kalshi settlement source* (hex, no 0x
    prefix) — the feed whose 1-minute candlestick close at 5pm ET settles the
    contract. ``strike_spacing_usd`` is the observed ladder spacing (read from
    the live chain at discovery, used only to seed the provisional
    ``bps_threshold``; the live observer captures the real spacing per cycle).

    ``pyth_live`` records whether that exact feed currently publishes a usable
    price on Pyth Hermes. ``live_enabled`` gates whether this product trades
    real money in the current launch (a product with a dead settlement feed
    must not). ``contract_rolls`` flags products whose settlement feed is a
    dated futures month that rolls over time (Brent/WTI) — for those the
    feed id here is the *current* contract and must be re-verified on roll.
    """

    commodity: Commodity
    kalshi_series: str
    pyth_feed_id: str
    pyth_symbol: str
    title: str
    settlement_unit: str
    strike_spacing_usd: float
    bps_threshold: float
    pyth_live: bool
    live_enabled: bool
    contract_rolls: bool
    notes: str = ""


# Pyth Hermes feed id for live spot gold (Metal.XAU/USD) — the KXGOLDD
# settlement source named in the series settlement_sources.
GOLD_XAU_USD_FEED = (
    "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"
)

# All ICE Brent month feeds Pyth lists (Commodities.BRENT{code}{year}/USD).
# Month codes: F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct
# X=Nov Z=Dec; trailing digit = year (5=2025, 6=2026). NONE of these were
# publishing a live price as of 2026-05-28 (all price=0 or months-stale).
# Kept so a future roll/relist can resolve the named contract to a feed id
# without re-running discovery; see ``feed_for_brent_contract``.
BRENT_MONTH_FEEDS: dict[str, str] = {
    "BRENTF6": "14cc780e57246819f68589d9646f507e70b637d14ac0dff2d384cfbc792a0256",
    "BRENTG6": "0169b040900764f5ec0d1c54861057c2696ed5dee8814c0a711c7e4385e6a151",
    "BRENTH6": "4a98349a329fc4b4ffbbd924447174f4308f3721351f2ecc31eb305b6929a510",
    "BRENTJ6": "37446cb6a2cdf2a017fd89a401ca895f587a0fdf63d96a65375c0907eaf4bfc0",
    "BRENTK6": "e51dd42e6cf3fef7e1afcfac913c68bbcb0e6cb6154017f8ad98203dba5241fa",
    "BRENTM6": "16599f19706cca02fd0bc054128b3cef54c517c0085eeb18d87f69a7ed2b6ce4",
    "BRENTN6": "1a898bc6959f5c420a596ee6f074601e8e66e61eb042af194b37b89565991f1a",
    "BRENTQ6": "cfdf6d7bd0e4221d8fc74a35caee1bf3c203c177781a5c9eef0b29c150698dab",
    "BRENTU6": "93fdb7c6f23c6ba97baf2f086891e6749461a5f6cd620338102845acf210e96b",
    "BRENTV6": "6e3607735df0f027dc63890cc48055cccf1551003cc7a7c934cabe04485d1193",
    "BRENTX6": "25b67e140c4f6683d86ddaea0efaa20a0c13722da04b16d60361ad0b05d0d394",
    "BRENTZ5": "7e990daa483e54a9a2b25ed3312285c867a049b5b3c84d27fe8c2ad9e0d24c57",
    "BRENTZ6": "6db688b9ec9e90a3e53f75891c3581e29ba157edf2a9ae98dffb5e5b5e595742",
}

# Brent's current Kalshi settlement contract (parsed from KXBRENTD
# rules_primary on 2026-05-28). Rolls — re-verify against the live rules.
BRENT_CURRENT_CONTRACT = "BRENTQ6"


def feed_for_brent_contract(contract_code: str) -> str | None:
    """Resolve a Brent futures contract code (e.g. 'BRENTQ6') to its Pyth
    feed id, or None if Pyth does not list that month."""
    return BRENT_MONTH_FEEDS.get(contract_code.strip().upper())


SPECS: dict[Commodity, CommoditySpec] = {
    Commodity.GOLD: CommoditySpec(
        commodity=Commodity.GOLD,
        kalshi_series="KXGOLDD",
        pyth_feed_id=GOLD_XAU_USD_FEED,
        pyth_symbol="Metal.XAU/USD",
        title="Gold Daily (above/below, 5pm ET settle)",
        settlement_unit="USD/t.oz",
        strike_spacing_usd=10.0,        # observed: $4399–$4689 step $10
        bps_threshold=7.0,              # provisional; ~1/3 of $10/$4500≈22bps spacing
        pyth_live=True,
        live_enabled=True,
        contract_rolls=False,
        notes=("Exact settlement source verified live 2026-05-28: "
               "Metal.XAU/USD = $4499.56, ~7s lag, conf ±$0.52."),
    ),
    Commodity.BRENT: CommoditySpec(
        commodity=Commodity.BRENT,
        kalshi_series="KXBRENTD",
        pyth_feed_id=BRENT_MONTH_FEEDS[BRENT_CURRENT_CONTRACT],
        pyth_symbol="Commodities.BRENTQ6/USD",
        title="Brent Oil Daily (above/below, 5pm ET settle)",
        settlement_unit="USD/Bbl",
        strike_spacing_usd=0.5,         # observed: $88–$97.50 step $0.50
        bps_threshold=15.0,             # provisional; ~1/3 of $0.50/$92≈54bps spacing
        pyth_live=False,
        live_enabled=False,
        contract_rolls=True,
        notes=("DATA-BLOCKED 2026-05-28: settlement contract BRENTQ6 returns "
               "price=0/publish_time=0 on Pyth Hermes (never published); all "
               "Brent month feeds dead or 130-200d stale. Only live Brent "
               "reference is Commodities.UKOILSPOT ($92.44) — a SPOT proxy, "
               "not the BRENTQ6 futures settlement source. Enabling Brent on "
               "UKOILSPOT would reintroduce a spot/futures basis. Disabled "
               "until BRENTQ6 publishes or the basis is measured + accepted."),
    ),
}


def live_specs() -> list[CommoditySpec]:
    """Specs whose exact settlement feed is live AND enabled for trading."""
    return [s for s in SPECS.values() if s.pyth_live and s.live_enabled]
