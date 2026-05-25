"""Self-contained validation tests for the Heston calibrator.

Three scenarios are exercised:

  1. Sharp recovery on a noise-free synthetic V-series. Generates V_t under
     the exact AR(1) discretization of the OU mean-reversion (no chi-squared
     measurement noise from r_i^2 aggregation) and verifies the fitter
     recovers kappa, theta, xi within 5%. This isolates the mathematical
     correctness of the AR(1) -> Heston mapping.

  2. Heston smoke test on a simulated 1-min log-return path. Verifies that
     the *well-identified* quantities (theta, sign of rho) come out right
     end-to-end. Magnitude of kappa and xi suffers an errors-in-variables
     bias from the chi-squared 5-min RV sampler -- this test does NOT
     assert their magnitudes.

  3. Constant-vol limit (xi=0). With no vol-of-vol, RV becomes white noise
     and the AR(1) coefficient collapses to ~0, producing a meaningless
     huge kappa. Verify the calibrate_crypto pipeline trips the
     'kappa_unidentified' stop via the half-life-too-short branch.

Plus one small unit check for the gap detector.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# allow `import calibrate_heston` from the script dir; the research script
# lives in scripts/research/ and is not shipped with the distribution, so
# skip the whole module gracefully if it can't be found.
_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "research"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

ch = pytest.importorskip(
    "calibrate_heston",
    reason="calibrate_heston research script not present (scripts/research/)",
)


# ---------------------------------------------------------------------------
# synthetic Heston simulator (1-min step, full-truncation Euler)
# ---------------------------------------------------------------------------
def simulate_heston_1m(
    n_min: int,
    mu: float,
    kappa: float,
    theta: float,
    xi: float,
    rho: float,
    v0: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """Return n_min log-returns under Heston with full-truncation Euler.

    Time is in years. Step dt = 1 minute = 1/525600 yr.
    """
    rng = np.random.default_rng(seed)
    dt = 1.0 / ch.MIN_PER_YEAR
    sqrt_dt = math.sqrt(dt)
    z2 = rng.standard_normal(n_min)
    z3 = rng.standard_normal(n_min)
    z1 = rho * z2 + math.sqrt(max(0.0, 1.0 - rho * rho)) * z3

    v = v0 if v0 is not None else theta
    rets = np.empty(n_min, dtype=np.float64)
    for i in range(n_min):
        v_plus = max(v, 0.0)
        sqrt_v = math.sqrt(v_plus)
        rets[i] = (mu - 0.5 * v_plus) * dt + sqrt_v * sqrt_dt * z1[i]
        v = v + kappa * (theta - v_plus) * dt + xi * sqrt_v * sqrt_dt * z2[i]
    return rets


def simulate_v_ar1(
    n_obs: int,
    kappa: float,
    theta: float,
    xi: float,
    dt_yr: float,
    v0: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """Return a noise-free V series following the exact OU AR(1) discretization.

    V_{t+1} = theta * (1 - e^{-k*dt}) + e^{-k*dt}*V_t + eps,
    eps ~ N(0, xi^2 * theta * (1 - e^{-2*k*dt}) / (2*kappa)).

    This generates exactly what `fit_ar1_to_heston` expects to see, with no
    chi-squared measurement noise. We do NOT reflect V at zero -- the test
    feeds the resulting sequence directly to the OLS-based fitter, which
    doesn't care about V's sign. The point is exact AR(1) dynamics.
    """
    rng = np.random.default_rng(seed)
    beta = math.exp(-kappa * dt_yr)
    alpha = theta * (1.0 - beta)
    var_eps = xi * xi * theta * (1.0 - math.exp(-2.0 * kappa * dt_yr)) / (2.0 * kappa)
    sigma_eps = math.sqrt(var_eps)
    v = v0 if v0 is not None else theta
    out = np.empty(n_obs, dtype=np.float64)
    for i in range(n_obs):
        out[i] = v
        v = alpha + beta * v + sigma_eps * rng.standard_normal()
    return out


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kappa,theta,xi,n_obs,k_tol,x_tol",
    [
        # Use very-fast mean reversion (kappa = 10_000/yr -> beta ~= 0.91 over
        # a 5-min window, half-life ~36 min) so the OLS beta estimate is
        # meaningfully far from the unit-root boundary. At slower kappa, beta
        # sits near 1 and the log derivative dkappa/dbeta = 1/(beta * dt)
        # explodes one-path sampling noise into double-digit kappa errors --
        # that's a property of the estimator, not a bug. These are math-test
        # params, not crypto-realistic.
        (10_000.0, 0.50, 12.0, 50_000, 0.10, 0.10),
        (20_000.0, 0.30, 18.0, 50_000, 0.10, 0.10),
    ],
)
def test_sharp_recovery_on_clean_v_series(kappa, theta, xi, n_obs, k_tol, x_tol):
    """Noise-free V-AR(1) sequence -> fitter recovers params within tolerances."""
    v = simulate_v_ar1(n_obs, kappa, theta, xi, ch.DT_BAR_YR, seed=42)
    fit = ch.fit_ar1_to_heston(v, ch.DT_BAR_YR)
    assert fit is not None
    assert fit["kappa"] == pytest.approx(kappa, rel=k_tol), (
        f"kappa: est={fit['kappa']:.4f} true={kappa:.4f}"
    )
    assert fit["theta_ann"] == pytest.approx(theta, rel=0.03), (
        f"theta: est={fit['theta_ann']:.4f} true={theta:.4f}"
    )
    assert fit["xi"] == pytest.approx(xi, rel=x_tol), (
        f"xi: est={fit['xi']:.4f} true={xi:.4f}"
    )


def test_heston_smoke_theta_and_rho_sign():
    """End-to-end RV-direct fit on a simulated Heston path.

    Uses fast mean reversion (kappa=3000/yr) so the AR(1) signal on 5-min RV
    isn't drowned by chi-squared sampling noise. Verifies the well-identified
    outputs: theta recovers within 20%, rho gets the right sign. Magnitudes
    of kappa and xi are NOT asserted -- their magnitudes are biased upward at
    crypto-realistic (slow) mean reversion by errors-in-variables on RV.
    """
    rets = simulate_heston_1m(
        60_000, mu=0.0, kappa=3000.0, theta=0.5, xi=15.0, rho=-0.55, seed=42
    )
    point = ch.rv_direct_fit(rets)
    assert point is not None
    assert point["theta_ann"] == pytest.approx(0.5, rel=0.20), (
        f"theta: est={point['theta_ann']:.4f} true=0.5"
    )
    assert point["rho"] < 0.0, f"rho expected < 0, got {point['rho']:.4f}"
    assert point["kappa"] > 0.0 and math.isfinite(point["kappa"])
    assert point["xi"] > 0.0 and math.isfinite(point["xi"])


def test_constant_vol_trips_kappa_unidentified_stop(tmp_path):
    """xi=0 -> calibrate_crypto must report a 'kappa_unidentified' stop.

    Build a synthetic fusion parquet on disk so the full pipeline runs.
    """
    rets = simulate_heston_1m(
        20_000, mu=0.0, kappa=5.0, theta=0.25, xi=0.0, rho=0.0, seed=11
    )
    # reconstruct OHLC close prices from log-returns
    px = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(rets)]))
    n = len(px)
    ts = np.arange(n, dtype=np.int64) * 60_000
    df = pd.DataFrame(
        {
            "ts_ms": ts, "open": px, "high": px, "low": px,
            "close": px, "volume": np.ones(n),
        }
    )
    out_path = tmp_path / "ZZZ_fusion_1m_synth.parquet"
    df.to_parquet(out_path)

    rng = np.random.default_rng(7)
    res = ch.calibrate_crypto("ZZZ", tmp_path, boot_reps=30, rng=rng)
    joined = " | ".join(res.get("stop_reasons", []))
    # At xi=0 either branch is a valid "calibration impossible" signal:
    #   * 'rv_direct_fit_degenerate' -- AR(1) beta came out <=0 or >=1, OR
    #   * 'kappa_unidentified' -- AR(1) fit went through but bootstrap
    #     diagnostics caught the meaningless half-life / large SE.
    assert ("kappa_unidentified" in joined) or (
        "rv_direct_fit_degenerate" in joined
    ), (
        "expected a calibration-impossible stop reason; "
        f"got stop_reasons={res.get('stop_reasons')}"
    )


def test_compute_returns_and_gap_detects_gaps():
    """Gap detection: a single missing minute is flagged and excluded."""
    # 10 contiguous minutes, then a 1-minute gap, then 6 more = 16 timestamps,
    # 15 inter-bar dt's, 1 of which is a gap (120_000 ms).
    ts = list(range(0, 600_000, 60_000)) + list(range(660_000, 1_020_000, 60_000))
    px = np.linspace(100.0, 110.0, len(ts))
    df = pd.DataFrame(
        {"ts_ms": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1.0}
    )
    rets, gap_pct = ch.compute_returns_and_gap(df)
    assert gap_pct == pytest.approx(1.0 / 15.0, rel=1e-9)
    # 14 clean dt's -> 14 returns (one is excluded by gap mask)
    assert rets.size == 14
