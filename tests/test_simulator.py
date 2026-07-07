"""
Tests for the Phase 7 Time-Travel Simulator (src/simulator.py).

Fully offline: bars, VIX, the Brain Map connection (':memory:'), and the
causal extractor are all injected — no Dhan, no Ollama, no Discord, and
the real data/ files are never touched (guard-tested explicitly).

The centerpiece scenario: a gentle uptrend into a flat range with VIX 12
-> the real Phase 5 logic proposes an IRON CONDOR -> the flat tape decays
it to the 65% profit take -> the outcome carries the full 2026 fiscal
friction stack and lands (idempotently) in simulated_trades + outcomes.

Run:
    python tests/test_simulator.py
    pytest tests/test_simulator.py -v
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map
from src import simulator as sim


def business_days(start: date, n: int) -> list:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def make_history(pre_days: int = 210, post_days: int = 40):
    """Rising closes (+2/day, uptrend, RSI high -> NEUTRAL view) for the
    SMA warmup, then a dead-flat range — the perfect condor tape. Returns
    (bars, sim_start_iso, sim_end_iso). Bars: (date, low, high, close)."""
    days = business_days(date(2025, 1, 1), pre_days + post_days)
    bars, price = [], 24000.0
    for i, d in enumerate(days):
        if i < pre_days:
            price += 2.0
        # flat afterwards
        bars.append((d.isoformat(), price - 10, price + 10, price))
    return bars, days[pre_days].isoformat(), days[pre_days + 8].isoformat()


def run_once(conn, bars=None, start=None, end=None):
    if bars is None:
        bars, start, end = make_history()
    vix = {b[0]: 12.0 for b in bars}
    stats = sim.run_simulation(start, end, ("NIFTY 50",), conn=conn,
                               bars_by_underlying={"NIFTY 50": bars},
                               vix_by_date=vix)
    return stats, bars, start, end


# --------------------------------------------------------- the main event

def test_iron_condor_replay_with_fiscal_costs():
    conn = brain_map.connect(":memory:")
    stats, *_ = run_once(conn)
    assert stats["resolved"] >= 1

    row = conn.execute("SELECT * FROM simulated_trades").fetchone()
    assert row["journal_ref"].startswith("sim:")
    assert row["underlying"] == "NIFTY 50"
    assert row["strategy"] == "iron_condor" and row["view"] == "neutral"
    assert row["vix"] == 12.0
    # The 2026 fiscal friction stack actually bit into the P&L:
    assert row["frictions_rs"] > 0 and row["slippage_rs"] > 0
    assert row["result"] in ("win", "loss", "scratch")
    # Flat tape -> time decay -> the 65% profit take should have fired:
    assert row["resolution"] == "profit_take"
    assert row["result"] == "win" and row["pnl_net"] > 0
    assert row["exit_date"] > row["proposed_on"]
    conn.close()


def test_outcome_feeds_brain_map_with_sim_ref():
    conn = brain_map.connect(":memory:")
    run_once(conn)
    outcome = conn.execute("SELECT * FROM outcomes").fetchone()
    assert outcome is not None and outcome["journal_ref"].startswith("sim:")
    assert outcome["result"] in ("win", "loss", "scratch")
    assert outcome["r_multiple"] is not None
    # ...and the strategy/view pattern events are linked "in the air":
    tags = {r["tag"] for r in conn.execute(
        "SELECT e.tag FROM events e JOIN event_outcome_link l ON l.event_id = e.id")}
    assert "iron_condor" in tags and "neutral" in tags
    conn.close()


def test_rerun_is_idempotent():
    conn = brain_map.connect(":memory:")
    first, bars, start, end = run_once(conn)
    counts1 = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
               for t in ("simulated_trades", "outcomes", "events",
                         "event_outcome_link")}
    second, *_ = run_once(conn, bars, start, end)
    counts2 = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
               for t in ("simulated_trades", "outcomes", "events",
                         "event_outcome_link")}
    assert counts1 == counts2                      # nothing duplicated
    assert second["resolved"] == 0                 # everything recognized
    assert second["duplicates_skipped"] >= 1
    conn.close()


def test_one_position_at_a_time():
    """While a simulated spread is open, no second proposal fires — entries
    never overlap, mirroring the live cool-down spirit."""
    conn = brain_map.connect(":memory:")
    run_once(conn)
    rows = conn.execute("SELECT proposed_on, exit_date FROM simulated_trades "
                        "ORDER BY proposed_on").fetchall()
    for a, b in zip(rows, rows[1:]):
        assert b["proposed_on"] > a["exit_date"]
    conn.close()


# -------------------------------------------------- feedback loop (6D/#34)

def test_causal_encoding_from_simulated_outcomes():
    class FakeExtractor:
        def __init__(self):
            self.texts = []

        def extract_causal_triples(self, text):
            self.texts.append(text)
            return [{"subject": "iron_condor", "predicate": "RESULTS_IN",
                     "object": "win", "condition": "low VIX"}]

    conn = brain_map.connect(":memory:")
    _, bars, start, end = run_once(conn)
    ex = FakeExtractor()
    stats = sim.encode_causal_links(conn, start, extractor=ex,
                                    today=date.fromisoformat(end))
    assert stats["outcomes_considered"] >= 1
    assert stats["triples_written"] == 1
    # The summary the LLM saw was built from the SIMULATED outcome:
    assert "iron_condor" in ex.texts[0]
    edge = conn.execute("SELECT * FROM graph_edges").fetchone()
    assert (edge["source_node"], edge["relation"], edge["target_node"]) == \
           ("iron_condor", "RESULTS_IN", "win")
    assert edge["confidence_score"] == 1.0
    conn.close()


# ------------------------------------------------------------ safety rails

def test_never_touches_real_portfolio_or_journal():
    """Runtime spies: the real paper book and journal must never be read
    or written during a full simulation."""
    from src import plan_tracker as pt
    conn = brain_map.connect(":memory:")
    with mock.patch.object(pt.pf, "load",
                           side_effect=AssertionError("real book read!")), \
         mock.patch.object(pt.pf, "save",
                           side_effect=AssertionError("real book write!")):
        stats, *_ = run_once(conn)
    assert stats["resolved"] >= 1
    conn.close()


def test_no_outbound_or_journal_imports():
    """Source guard: no notifier/Discord/journal/network imports — every
    outbound path is structurally absent, not just mocked."""
    source = Path(sim.__file__).read_text()
    import_lines = [l.strip() for l in source.splitlines()
                    if l.strip().startswith(("import ", "from "))]
    for line in import_lines:
        assert "notifier" not in line and "discord" not in line, line
        assert "httpx" not in line and "urllib" not in line, line
        assert "journal" not in line, line
        assert "genai" not in line, line


# ------------------------------------------------------------ small parts

def test_next_expiry_is_a_thursday_min_days_out():
    exp = date.fromisoformat(sim.next_expiry(date(2025, 6, 2)))  # a Monday
    assert exp.weekday() == 3
    assert (exp - date(2025, 6, 2)).days >= sim.MIN_DAYS_TO_EXPIRY


def test_synthetic_chain_shape_and_decay():
    chain = sim.build_synthetic_chain(spot=24420.0, vix=12.0,
                                      days_to_expiry=9, step=50.0)
    strikes = sorted(float(s) for s in chain["oc"])
    assert len(strikes) == 2 * sim.CHAIN_SPAN_STEPS + 1
    assert chain["last_price"] == 24420.0
    atm = min(strikes, key=lambda s: abs(s - 24420.0))
    far = strikes[-1]
    tv_atm = chain["oc"][f"{atm:.6f}"]["ce"]["last_price"]
    tv_far = chain["oc"][f"{far:.6f}"]["ce"]["last_price"]
    assert tv_atm > tv_far > 0  # time value decays away from the money


def test_analysis_from_closes_matches_live_contract():
    closes = [100.0 + 0.5 * i for i in range(220)]
    a = sim.analysis_from_closes("NIFTY 50", closes)
    assert a["uptrend"] is True and a["price"] == closes[-1]
    assert set(a) == {"ticker", "uptrend", "fresh_cross", "rsi", "price"}
    assert sim.analysis_from_closes("NIFTY 50", closes[:150]) is None


def test_sim_ref_is_deterministic():
    a = sim.sim_ref("NIFTY 50", "2025-01-10", "iron_condor", "2025-01-23")
    b = sim.sim_ref("NIFTY 50", "2025-01-10", "iron_condor", "2025-01-23")
    c = sim.sim_ref("NIFTY 50", "2025-01-11", "iron_condor", "2025-01-23")
    assert a == b != c and a.startswith("sim:")


if __name__ == "__main__":
    test_iron_condor_replay_with_fiscal_costs()
    test_outcome_feeds_brain_map_with_sim_ref()
    test_rerun_is_idempotent()
    test_one_position_at_a_time()
    test_causal_encoding_from_simulated_outcomes()
    test_never_touches_real_portfolio_or_journal()
    test_no_outbound_or_journal_imports()
    test_next_expiry_is_a_thursday_min_days_out()
    test_synthetic_chain_shape_and_decay()
    test_analysis_from_closes_matches_live_contract()
    test_sim_ref_is_deterministic()
    print("All simulator tests passed.")
