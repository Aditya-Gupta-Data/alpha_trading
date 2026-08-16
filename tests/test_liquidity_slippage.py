"""Gap 4 — liquidity-tier slippage (src/liquidity_slippage.py) and its
wiring into plan_tracker.apply_slippage / equity_desk.settle_exit."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import liquidity_slippage as ls

FO = {"banned": ["BANNEDCO"], "symbols": {"RELIANCE": {"tier": "tier1"},
                                          "AMBER": {"tier": "tier2"},
                                          "BANNEDCO": {"tier": "tier1"}}}


def _fo(tmp_path):
    p = tmp_path / "fo.json"; p.write_text(json.dumps(FO)); return p


def test_the_ladder_is_tier1_10bp_tier2_25bp_illiquid_50bp(tmp_path):
    p = _fo(tmp_path)
    assert ls.liquidity_tier("RELIANCE.NS", p) == "tier1"
    assert ls.liquidity_tier("AMBER", p) == "tier2"
    assert ls.liquidity_tier("PENNYCO", p) == "illiquid"
    assert ls.liquidity_tier("BANNEDCO", p) == "illiquid"
    assert ls.liquidity_tier("NIFTY 50", p) == "tier1"       # indices by definition
    assert ls.liquidity_tier("NIFTY BANK", p) == "tier1"
    assert ls.tier_slippage_frac("RELIANCE", p) == 0.0010
    assert ls.tier_slippage_frac("AMBER", p) == 0.0025
    assert ls.tier_slippage_frac("PENNYCO", p) == 0.0050


def test_a_missing_liquidity_file_defaults_to_the_EXPENSIVE_tier(tmp_path):
    assert ls.liquidity_tier("RELIANCE", tmp_path / "nope.json") == "illiquid"
    assert ls.liquidity_tier("", tmp_path / "nope.json") == "illiquid"


def test_slippage_rupees_is_price_times_qty_times_tier(tmp_path):
    p = _fo(tmp_path)
    assert ls.slippage_rs(1000.0, 10, "RELIANCE", p) == 10.0     # 0.10%
    assert ls.slippage_rs(1000.0, 10, "AMBER", p) == 25.0        # 0.25%
    assert ls.slippage_rs(1000.0, 10, "PENNYCO", p) == 50.0      # 0.50%
    assert ls.slippage_rs("x", 10, "AMBER", p) == 0.0


def test_apply_slippage_keeps_legacy_numbers_when_no_symbol_is_named():
    from src.plan_tracker import apply_slippage
    assert apply_slippage(100.0, "STOCK") == 0.0
    assert apply_slippage(100.0, "INDEX") == 0.05
    assert apply_slippage(200.0, "OPTION") == 0.2                 # 0.10% ladder
    assert apply_slippage(30.0, "OPTION") == 0.15                 # 0.50% ladder


def test_apply_slippage_uses_the_tier_when_the_symbol_is_named(tmp_path, monkeypatch):
    from src import plan_tracker as pt
    p = _fo(tmp_path)
    monkeypatch.setattr(ls, "FO_PATH", p)
    ls._cache.update(path=None, mtime=None, data=None)
    assert pt.apply_slippage(1000.0, "STOCK", symbol="RELIANCE") == 1.0      # 0.10%
    assert pt.apply_slippage(1000.0, "STOCK", symbol="AMBER") == 2.5         # 0.25%
    assert pt.apply_slippage(1000.0, "STOCK", symbol="PENNYCO") == 5.0       # 0.50%
    # options: max(ladder, tier floor) — a rich NIFTY leg keeps the 0.10% ladder,
    # a stock option on an illiquid name is floored at 0.50%
    assert pt.apply_slippage(200.0, "OPTION", symbol="NIFTY 50") == 0.2
    assert pt.apply_slippage(200.0, "OPTION", symbol="PENNYCO") == 1.0
    assert pt.apply_slippage(1000.0, "INDEX", symbol="NIFTY 50") == 1.0      # 0.10% > 0.05%


def test_equity_desk_settle_pays_tier_slippage_on_both_sides(tmp_path, monkeypatch):
    from src import equity_desk as ed
    p = _fo(tmp_path)
    monkeypatch.setattr(ls, "FO_PATH", p)
    ls._cache.update(path=None, mtime=None, data=None)
    seen = {}
    def fake_release(conn, ref, pnl):
        seen["pnl"] = pnl; return {"released": True, "equity": 1.0, "halted": False}
    monkeypatch.setattr(ed.pm, "release_margin", fake_release)
    monkeypatch.setattr(ed, "_connect", lambda conn: (None, False))
    entry = {"id": "e1", "funding": {"funded": True, "qty": 10, "lock_ref": "eqd:e1"},
             "kya_kara_action": {"entry_price": 1000.0}, "ticker": "AMBER"}
    r = ed.settle_exit(entry, {"exit_price": 1100.0, "ticker": "AMBER", "reason": "target"})
    gross = 1000.0
    fr = ed.delivery_frictions("BUY", 1000.0, 10) + ed.delivery_frictions("SELL", 1100.0, 10)
    assert r["slippage_rs"] == 25.0 + 27.5                       # 0.25% each side
    assert r["pnl_net"] == round(gross - fr - 52.5, 2)
    assert seen["pnl"] == r["pnl_net"]


def test_no_broker_path_this_is_paper_only():
    src = Path("src/liquidity_slippage.py").read_text()
    for forbidden in ("dhan_client", "place_order", "requests.", "urllib"):
        assert forbidden not in src
