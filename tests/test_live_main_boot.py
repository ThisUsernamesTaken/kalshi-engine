"""bin.live: CLI parsing, env-file loader, mock-driven boot integration."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_engine.bin import live


# ---- CLI / env-file helpers ----------------------------------------------


def test_parse_args_defaults():
    args = live.parse_args([])
    assert args.strategy == "favorite_chase"
    assert args.model == "phase4_cutpoints"
    assert args.cryptos == "BTC,ETH,SOL,XRP,DOGE"
    assert args.stop_mode == "none"
    assert args.bps_gate == "enabled"
    assert args.max_contracts == 10  # Phase-12.7: lifted from 5
    assert args.daily_cap_cents == 1000
    assert args.dry_run is False
    assert args.align_mode == "5tier_v13b_h1h4_loose"  # Phase-13.4 default
    assert args.reentry_mode == "disabled"  # Phase-12.5 Rec 1
    assert args.time_of_day_skip == "enabled"  # Phase-12.5 Rec 2
    assert args.cutpoints_version == "v3"  # Phase-12.5 Rec 3
    assert args.pre_trigger_observation == "enabled"  # Phase-12.8


def test_parse_args_dry_run_flag():
    args = live.parse_args(["--dry-run", "--duration-s", "0.5"])
    assert args.dry_run is True
    assert args.duration_s == 0.5


def test_read_env_file(tmp_path):
    env = tmp_path / "k.env"
    env.write_text(
        "# comment line\n"
        "KALSHI_API_KEY=abc-uuid\n"
        'KALSHI_PRIVATE_KEY_PATH="C:\\path\\to\\key.pem"\n'
        "\n"
        "EMPTY_LINE_OK=value\n",
        encoding="utf-8",
    )
    out = live._read_env_file(str(env))
    assert out["KALSHI_API_KEY"] == "abc-uuid"
    assert out["KALSHI_PRIVATE_KEY_PATH"] == "C:\\path\\to\\key.pem"
    assert out["EMPTY_LINE_OK"] == "value"


# ---- mock-driven boot integration ----------------------------------------


def _gen_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _MockClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a, **kw):
        return False

    @property
    def signer(self):
        return None

    async def list_markets(self, series_ticker=None, status=None, limit=200):
        # One synthetic open market per series.
        return [{
            "ticker": f"{series_ticker}-T",
            "floor_strike": 100000.0,
            "open_time": "2026-05-22T18:00:00Z",
            "close_time": "2026-05-22T18:15:00Z",
        }]

    async def get_positions(self):
        return []

    async def subscribe_order_updates(self):
        return
        yield  # pragma: no cover - empty async gen marker


class _MockSpotFeed:
    def __init__(self, *a, **kw):
        pass

    async def bootstrap_warmup_into(self, strategy, risk_state):
        return 0

    async def events(self):
        return
        yield  # pragma: no cover


class _MockKalshiWS:
    def __init__(self, *a, **kw):
        pass

    async def events(self):
        return
        yield  # pragma: no cover


@pytest.fixture
def mock_creds(tmp_path, monkeypatch):
    pem_path = tmp_path / "key.pem"
    pem_path.write_bytes(_gen_pem())
    env_path = tmp_path / "kalshi.env"
    env_path.write_text(
        f"KALSHI_API_KEY=test-key\nKALSHI_PRIVATE_KEY_PATH={pem_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KALSHI_API_KEY_PATH", str(env_path))
    return env_path


def test_boot_writes_event(tmp_path, monkeypatch, mock_creds):
    monkeypatch.setattr("kalshi_engine.bin.live.KalshiClient", _MockClient)
    monkeypatch.setattr("kalshi_engine.bin.live.SpotFeed", _MockSpotFeed)
    monkeypatch.setattr(
        "kalshi_engine.bin.live.KalshiWebSocketFeed", _MockKalshiWS
    )
    log_path = tmp_path / "live.jsonl"
    rc = live.main([
        "--dry-run",
        "--duration-s", "0.5",
        "--log-path", str(log_path),
        "--cryptos", "BTC",
    ])
    assert rc == 0
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    boot = next(e for e in events if e["kind"] == "boot")
    assert boot["strategy"] == "favorite_chase"
    assert boot["model"] == "phase4_cutpoints"
    assert boot["dry_run"] is True
    assert boot["markets_registered"] >= 1
    assert boot["cryptos"] == ["BTC"]
    assert boot["stop_mode"] == "none"
    assert boot["bps_gate"] == "enabled"
    assert boot["max_contracts"] == 10  # Phase-12.7 default
    assert boot["daily_cap_cents"] == 1000
    assert boot["align_mode"] == "5tier_v13b_h1h4_loose"  # Phase-13.4 default
    assert boot["pre_trigger_observation"] == "enabled"  # Phase-12.8
    assert boot["reentry_mode"] == "disabled"  # Phase-12.5 Rec 1
    assert boot["time_of_day_skip"] == "enabled"  # Phase-12.5 Rec 2
    assert boot["cutpoints_version"] == "v3"  # Phase-12.5 Rec 3
    # shutdown event present at the end
    assert any(e["kind"] == "shutdown" for e in events)


def test_boot_missing_env_var_errors(monkeypatch, tmp_path):
    monkeypatch.delenv("KALSHI_API_KEY_PATH", raising=False)
    rc = live.main([
        "--dry-run", "--duration-s", "0.1",
        "--log-path", str(tmp_path / "log.jsonl"),
    ])
    assert rc == 2  # the explicit "env var not set" exit code
