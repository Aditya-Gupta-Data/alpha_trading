"""Decision #160 (Architect 2026-10-28): GEX steering for PAPER_2L only, and the sector bars moved to the
Mini PC. A steered book copies an SMA entry only when the calibrated GEX zone agrees; every other book keeps
the SMA routing. Offline."""
from pathlib import Path

import pytest

from src import brain_map, options_proposer as op, portfolio_manager as pm
from src.analysis import gex_steering as gs
from tests.test_dual_paper_accounts import _proposal

SHORT = {"zero_gamma": 54_000.0, "regime_at_spot": "negative", "day": "2026-10-27"}
LONG = {"zero_gamma": 50_000.0, "regime_at_spot": "positive", "day": "2026-10-27"}
DOWN = {"closes": [53_000.0, 52_340.0]}          # -1.25% session
FLAT = {"closes": [52_400.0, 52_340.0]}          # -0.11%


def test_short_gamma_lets_only_the_d6_momentum_spread_through():
    ok = gs.steer("NIFTY BANK", "bear_put_spread", 52_340.0, DOWN, gex_fn=lambda u: SHORT)
    assert ok["allowed"] and ok["zone"] == "short_gamma" and ok["wanted"] == "bear_put_spread"
    no = gs.steer("NIFTY BANK", "iron_condor", 52_340.0, DOWN, gex_fn=lambda u: SHORT)
    assert not no["allowed"] and "wants bear put spread, the SMA built iron condor (#160)" in no["reason"]
    flat = gs.steer("NIFTY BANK", "bear_put_spread", 52_340.0, FLAT, gex_fn=lambda u: SHORT)
    assert not flat["allowed"] and flat["wanted"] == "long_straddle"          # a straddle cannot be copied


def test_any_other_zone_or_no_calibrated_read_leaves_the_sma_routing():
    assert gs.steer("NIFTY BANK", "iron_condor", 52_340.0, DOWN, gex_fn=lambda u: LONG)["allowed"]
    assert gs.steer("NIFTY BANK", "iron_condor", 52_340.0, DOWN, gex_fn=lambda u: {})["allowed"]
    near = dict(SHORT, zero_gamma=52_400.0)                                  # within 0.5% of S*: transition
    assert gs.steer("NIFTY BANK", "iron_condor", 52_340.0, DOWN, gex_fn=lambda u: near)["zone"] == "transition"


@pytest.fixture
def conn(monkeypatch):
    c = brain_map.connect(":memory:")
    pm.ensure_accounts_schema(c)
    pm.get_account(c)
    monkeypatch.setattr(pm, "PAPER_2L_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(pm, "CAPITAL_ROTATION_ENABLED", True)
    monkeypatch.setattr(pm, "PAPER_2L_LIVE_ACCOUNT_ENABLED", False)
    yield c
    c.close()


def test_only_the_steered_account_refuses_a_contradicted_entry(conn, monkeypatch):
    monkeypatch.setattr("src.config.GEX_STEERING_ACCOUNTS", ("PAPER_2L",))
    p = dict(_proposal(), gex_steering={"allowed": False, "reason": "gex steering: short-gamma zone wants x (#160)"})
    out = pm.evaluate_shadow_accounts("sp0001", p, conn=conn)
    assert out["PAPER_2L"]["status"] == "rejected" and out["PAPER_2L"]["reason"].startswith("gex steering")
    assert out["PAPER_2L_ROT"]["status"] == "approved"                        # the SMA routing, untouched
    ev = [r[0] for r in conn.execute("SELECT event_type FROM paper_account_events WHERE account_id='PAPER_2L'")]
    assert "gex_steering_refused" in ev
    agree = dict(_proposal(), short_id="sp0002", gex_steering={"allowed": True})
    assert pm.evaluate_shadow_accounts("sp0002", agree, conn=conn)["PAPER_2L"]["status"] == "approved"


def test_with_no_steered_account_the_verdict_is_ignored(conn, monkeypatch):
    monkeypatch.setattr("src.config.GEX_STEERING_ACCOUNTS", ())
    p = dict(_proposal(), gex_steering={"allowed": False, "reason": "x"})
    assert pm.evaluate_shadow_accounts("sp0001", p, conn=conn)["PAPER_2L"]["status"] == "approved"


def test_the_proposer_reads_gex_only_when_a_book_is_steered(monkeypatch):
    p = {"spread": {"strategy": "iron_condor", "entry_spot": 52_340.0}}
    monkeypatch.setattr("src.config.GEX_STEERING_ACCOUNTS", ())
    assert op._gex_steering_read("NIFTY BANK", p, {"analysis": DOWN}) is None
    monkeypatch.setattr("src.config.GEX_STEERING_ACCOUNTS", ("PAPER_2L",))
    monkeypatch.setattr(gs, "latest_gex", lambda u, *a, **k: SHORT)
    got = op._gex_steering_read("NIFTY BANK", p, {"analysis": DOWN})
    assert got["allowed"] is False and got["wanted"] == "bear_put_spread"
    monkeypatch.setattr(gs, "steer", lambda *a, **k: 1 / 0)
    assert op._gex_steering_read("NIFTY BANK", p, {})["allowed"] is True      # fail-open: no read, no refusal


def test_the_shipped_config_steers_paper_2l_alone():
    import json
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
    assert cfg["gex_steering_accounts"] == ["PAPER_2L"]


def test_the_sector_bars_ship_from_the_node_and_no_longer_from_the_mac():
    root = Path(__file__).resolve().parent.parent
    node = (root / "scripts" / "setup_node_replica_cron.sh").read_text()
    assert "10 16 * * 1-5 cd \\\"$REPO_ROOT\\\" && bash scripts/node_sector_bars.sh" in node
    run = [ln for ln in (root / "scripts" / "mac_auto_sync.sh").read_text().splitlines()
           if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("fetch_sector_bars.py" in ln or '"sector_index_bars.json"' in ln for ln in run)
    sh = (root / "scripts" / "node_sector_bars.sh").read_text()
    assert "fetch_sector_bars.py" in sh and "vm_push_file('data/sector_index_bars.json')" in sh
