"""
Tests for Phase 5 options: defined-risk spread construction
(src/strategy.py StrategyConstructor), the 2026 friction stack + SPAN
offsets (src/portfolio.py), and atomic basket resolution with
profit-take / pre-expiry exits (src/plan_tracker.py).

Offline — no Dhan, no Gemini, no email; the tracker runs against a fake
journal and a temp Brain Map, and NEVER touches the live data/ files.

Run either of these from the project folder:
    python tests/test_options_spreads.py     (simple, no extra installs)
    python -m pytest tests/                   (if you have pytest)
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analyst
from src import brain_map
from src.portfolio import calculate_trade_frictions, calculate_span_margin
from src.strategy import StrategyConstructor, VIX_BLOCK_ABOVE
import src.plan_tracker as plan_tracker


# A NIFTY iron condor used throughout: 24800/25200 body, 200-wide wings,
# net credit 117/share, lot 75 -> max loss (200-117)*75 = 6225,
# max profit 117*75 = 8775.
def make_condor(vix=13.0):
    sc = StrategyConstructor(vix=vix, lot_size=75)
    return sc.construct_iron_condor(24800, 25200, 200, 95, 38, 100, 40)


# ---------------------------------------------------------- construction

def test_iron_condor_max_loss_math():
    ic = make_condor()
    assert ic["net_credit"] == 117
    assert ic["spread_width"] == 200
    assert ic["max_loss"] == (200 - 117) * 75      # 6225
    assert ic["max_profit"] == 117 * 75            # 8775
    assert len(ic["legs"]) == 4
    # Zero naked legs: every SELL has a same-type BUY wing.
    for opt_type in ("CE", "PE"):
        sides = sorted(l["side"] for l in ic["legs"] if l["option_type"] == opt_type)
        assert sides == ["BUY", "SELL"]


def test_iron_butterfly_max_loss_math():
    sc = StrategyConstructor(vix=13.0, lot_size=75)
    ib = sc.construct_iron_butterfly(25000, 300, 180, 175, 60, 55)
    assert ib["net_credit"] == 240                 # (180+175) - (60+55)
    assert ib["max_loss"] == (300 - 240) * 75      # 4500
    assert ib["max_profit"] == 240 * 75            # 18000


def test_debit_spreads_max_loss_is_premium_paid():
    sc = StrategyConstructor(vix=20.0, lot_size=75)  # high VIX: debit still fine
    bcs = sc.construct_bull_call_spread(25000, 25200, 150, 60)
    assert bcs["net_debit"] == 90
    assert bcs["max_loss"] == 90 * 75              # the debit, not stop distance
    assert bcs["max_profit"] == (200 - 90) * 75
    bps = sc.construct_bear_put_spread(25000, 24800, 140, 55)
    assert bps["net_debit"] == 85
    assert bps["max_loss"] == 85 * 75
    # Incoherent strike ordering refuses to build:
    assert sc.construct_bull_call_spread(25200, 25000, 60, 150) is None
    assert sc.construct_bear_put_spread(24800, 25000, 55, 140) is None


# ---------------------------------------------------------- VIX regime

def test_vix_above_16_blocks_range_bound_strategies():
    assert make_condor(vix=16.5) is None
    sc = StrategyConstructor(vix=18.0, lot_size=75)
    assert sc.construct_iron_butterfly(25000, 300, 180, 175, 60, 55) is None
    allowed, reason = sc.validate_regime("iron_condor")
    assert not allowed and "18.00" in reason


def test_vix_at_or_below_16_allows_range_bound_strategies():
    assert make_condor(vix=16.0) is not None       # gate is strictly ">"
    assert make_condor(vix=12.0) is not None


def test_unknown_vix_fails_safe_for_range_bound_only():
    sc = StrategyConstructor(vix=None, lot_size=75)
    assert sc.construct_iron_condor(24800, 25200, 200, 95, 38, 100, 40) is None
    # ...but directional debit spreads still build without a VIX reading:
    assert sc.construct_bull_call_spread(25000, 25200, 150, 60) is not None


# ------------------------------------------------- frictions & margin

def test_stt_only_on_sell_legs():
    # Same option leg, same premium/qty — the ONLY sell-side extra is STT
    # (0.15%) and the only buy-side extra is stamp duty (0.003%).
    buy = calculate_trade_frictions("OPTION", "BUY", 100.0, 75)
    sell = calculate_trade_frictions("OPTION", "SELL", 100.0, 75)
    turnover = 100.0 * 75
    stt = 0.0015 * turnover                        # 11.25
    stamp = 0.00003 * turnover                     # 0.225
    # (each side rounds to the paisa first, so allow that 1p of wiggle)
    assert abs((sell - buy) - (stt - stamp)) <= 0.011
    # And absolute values: shared charges = brokerage 20 + exchange
    # 0.25875 + sebi 0.0075 + GST 3.64793 = 23.91
    assert buy == round(23.914175 + stamp, 2)      # 24.14
    assert sell == round(23.914175 + stt, 2)       # 35.16


def test_span_margin_offsets_hedged_spreads():
    ic = make_condor()
    margin = ic["margin"]
    # Hedged: each short is paired with its wing -> per-pair (width-credit)*lot
    assert margin["total_margin"] == (200 - 57) * 75 + (200 - 60) * 75  # 21225
    # Naked equivalent is punitively larger, and the savings are visible:
    assert margin["naked_margin"] > 10 * margin["total_margin"]
    assert margin["offset_savings"] == margin["naked_margin"] - margin["total_margin"]
    # A lone short leg gets NO offset:
    naked = calculate_span_margin(
        [{"side": "SELL", "option_type": "CE", "strike": 25200, "premium": 100}], 75)
    assert naked["offset_savings"] == 0.0
    assert naked["total_margin"] == naked["naked_margin"]


def test_size_lots_uses_absolute_max_loss_not_stop_distance():
    ic = make_condor()   # max_loss 6225/lot, margin 21225/lot
    sc = StrategyConstructor(vix=13.0, lot_size=75)
    # Risk budget = RISK_PER_TRADE_PCT (1%) of total value.
    # 20L portfolio -> budget 20,000 -> 3 lots by risk; margin cap
    # 20,00,000 cash // 21225 = 94 lots -> risk budget binds.
    big_book = {"cash": 2_000_000.0, "holdings": {}}
    assert sc.size_lots(ic, big_book, {}) == 3
    # The default 1L portfolio can't afford 6225 of max loss at 1% risk:
    small_book = {"cash": 100_000.0, "holdings": {}}
    assert sc.size_lots(ic, small_book, {}) == 0
    # Margin cap binds when cash is thin even if account value is big:
    thin_cash = {"cash": 25_000.0,
                 "holdings": {"X.NS": {"shares": 1000, "avg_price": 2000.0}}}
    assert sc.size_lots(ic, thin_cash, {"X.NS": 2000.0}) == 1


# ------------------------------------------------- tracker resolution

def make_open_spread(short_id="sp123456", decision="approved", lots=1):
    ic = make_condor()
    spread = dict(ic, lots=lots, expiry="2026-07-26", entry_spot=25000.0)
    return {
        "short_id": short_id,
        "date": "2026-07-06", "action": "SPREAD", "ticker": "NIFTY 50",
        "price": ic["net_credit"], "shares": 75 * lots,
        "signal": "neutral mean-reversion setup", "decision": decision,
        "why": "expecting a quiet range", "pattern_tags": ["Iron Condor"],
        "plan": None, "outcome": None,
        "spread": spread,
    }


class FakeJournal:
    def __init__(self, entries):
        self.entries = entries
        self.rewritten = None

    def read_all(self):
        return self.entries

    def rewrite_all(self, entries):
        self.rewritten = entries


def run_spread_tracker(tmp, entries, bars, on_episode=None):
    """run_tracker with every external surface patched out (incl. the
    paper portfolio — cash settlement is captured, never written)."""
    db_path = Path(tmp) / "brain_map.db"
    fake_journal = FakeJournal(entries)
    settled = []
    plan_tracker.journal = fake_journal
    plan_tracker._daily_bars = lambda ticker, start: bars
    plan_tracker._settle_spread_cash = lambda pnl: settled.append(pnl) or True
    plan_tracker._close_paper_position = lambda entry, price, *a, **k: True
    plan_tracker._brain_connect = lambda: brain_map.connect(db_path)
    analyst.generate_post_mortem = lambda plan, execution: None
    resolved = plan_tracker.run_tracker(email=False, on_episode=on_episode)
    return resolved, fake_journal, settled, db_path


RANGE_BARS = [(f"2026-07-{d:02d}", 24950.0, 25050.0, 25000.0) for d in range(7, 26)]
CRASH_BARS = [(f"2026-07-{d:02d}", 23900.0, 24100.0, 24000.0) for d in range(7, 26)]


def test_profit_take_exits_at_65pct_of_max_profit():
    with tempfile.TemporaryDirectory() as tmp:
        episodes = []
        resolved, fj, settled, db_path = run_spread_tracker(
            tmp, [make_open_spread()], RANGE_BARS, on_episode=episodes.append)
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["resolution"] == "profit_take"
        assert o["exit_style"] == "atomic_basket"
        assert o["exit_date"] < "2026-07-24"       # exited well before expiry
        assert o["pct"] >= 65.0                    # captured >= 65% of max profit
        # Net P&L = gross minus itemized frictions and slippage:
        gross = o["pnl_rs"] + o["frictions_rs"] + o["slippage_rs"]
        assert o["frictions_rs"] > 0 and o["slippage_rs"] > 0
        assert round(gross, 0) >= 0.65 * 8775 // 1
        assert o["verdict"].startswith("WIN")
        # Approved -> cash net-settled exactly once with the net figure:
        assert settled == [o["pnl_rs"]]
        # Brain Map + Discord episode flows fire for spreads too:
        conn = brain_map.connect(db_path)
        row = conn.execute("SELECT journal_ref, result FROM outcomes").fetchone()
        assert row["journal_ref"] == "sp123456" and row["result"] == "win"
        conn.close()
        assert len(episodes) == 1 and episodes[0]["resolution"] == "profit_take"


def test_pre_expiry_exit_fires_two_days_before_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        entry = make_open_spread()
        entry["spread"]["max_profit"] = 0.0        # profit take can never fire
        resolved, fj, _, _ = run_spread_tracker(tmp, [entry], RANGE_BARS)
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["resolution"] == "pre_expiry_exit"
        assert o["exit_date"] == "2026-07-24"      # expiry 26th - 2 days, strictly


def test_loss_is_clamped_to_defined_risk_max():
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, settled, _ = run_spread_tracker(
            tmp, [make_open_spread()], CRASH_BARS)
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["resolution"] == "pre_expiry_exit"
        # Gross loss is clamped at exactly max_loss (6225); only real costs
        # (frictions + slippage) may push the net figure past it:
        gross = o["pnl_rs"] + o["frictions_rs"] + o["slippage_rs"]
        assert round(gross, 2) == -6225.0
        assert o["verdict"].startswith("LOSS")
        assert settled == [o["pnl_rs"]]


def test_skipped_spread_resolves_hypothetically():
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, settled, _ = run_spread_tracker(
            tmp, [make_open_spread(decision="rejected")], RANGE_BARS)
        assert resolved == 1
        o = fj.rewritten[0]["outcome"]
        assert o["hypothetical"] is True
        assert settled == []                       # cash never touched
        assert o["verdict"].startswith("MISSED GAIN")


def test_mixed_journal_resolves_equity_and_spreads_independently():
    equity = {
        "short_id": "eq123456", "date": "2026-07-06", "action": "BUY",
        "ticker": "NIFTY 50", "shares": 5, "price": 25000.0,
        "signal": "Fresh Golden Cross", "decision": "approved", "why": "test",
        "pattern_tags": [],
        "plan": {"stop_loss": {"pct": 3.0, "price": 24250.0},
                 "target": {"price": 25050.0, "rr": 3.0}},
        "outcome": None,
    }
    with tempfile.TemporaryDirectory() as tmp:
        resolved, fj, _, _ = run_spread_tracker(
            tmp, [equity, make_open_spread()], RANGE_BARS)
        assert resolved == 2
        assert fj.rewritten[0]["outcome"]["resolution"] == "target_hit"
        assert fj.rewritten[1]["outcome"]["resolution"] == "profit_take"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")
