"""
Firm treasury v2 — single-machine (decision #83) — hermetic tests.

The router's mechanical tilts (unchanged from #80), the one-row budget
state with its atomic move, deadband/step discipline, and the VM-local
input gathering. The v1 two-phase/reconcile apparatus is GONE with the
second machine — double-spend safety now lives at pm.request_entry's one
door, tested in test_equity_desk. Run:
    python -m pytest tests/test_firm_treasury.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.firm_treasury as ft
from src import portfolio_manager as pm

ft.EQUITY_DESK_CAPITAL_RS = 300000.0
ft.TREASURY_DEADBAND_RS = 50000.0
ft.TREASURY_MAX_STEP_RS = 100000.0
ft.TREASURY_EQUITY_MIN_PCT = 15.0
ft.TREASURY_EQUITY_MAX_PCT = 60.0
ft.TREASURY_ROUND_RS = 25000.0
POOL = 1_000_000.0     # :memory: accounts bootstrap at the classic 10L


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pm.get_account(c)
    return c


def _tiers(tmp, rows):
    p = Path(tmp) / "darling_tiers.json"
    p.write_text(json.dumps({"tiers": {"strong_buy": rows}}))
    return p


def test_budget_seeds_once_and_moves_atomically():
    conn = _conn()
    assert ft.get_budget(conn) == 300000.0            # config seed
    ft.set_budget(conn, 400000.0, "test")
    assert ft.get_budget(conn) == 400000.0            # persisted
    events = [r[0] for r in conn.execute(
        "SELECT event_type FROM account_events").fetchall()]
    assert "treasury_rotation" in events
    conn.close()


def test_compute_target_tilts_clamps_and_null_honesty():
    r = ft.compute_target({}, POOL)
    assert r["share"] == 0.30 and r["target_rs"] == 300000.0
    assert set(r["unknown_inputs"]) == {"nifty_uptrend", "buy_depth",
                                        "median_valuation", "vix",
                                        "options_util"}
    r = ft.compute_target({"nifty_uptrend": True, "buy_depth": 8,
                           "median_valuation": 28.0, "vix": 12.0,
                           "options_util": 0.10, "exhaustion_5d": 0}, POOL)
    assert r["share"] == 0.55 and r["target_rs"] == 550000.0
    r = ft.compute_target({"nifty_uptrend": False, "buy_depth": 0,
                           "median_valuation": None, "vix": 21.0,
                           "options_util": 0.75, "exhaustion_5d": 2}, POOL)
    assert r["share"] == 0.15 and r["target_rs"] == 150000.0
    r = ft.compute_target({"options_util": 0.05, "exhaustion_5d": 1}, POOL)
    assert "options_demand" in r["tilts"]


def test_plan_move_deadband_and_step():
    assert ft.plan_move(300000.0, 320000.0)["move"] == 0.0
    assert ft.plan_move(300000.0, 550000.0)["move"] == 100000.0
    assert ft.plan_move(300000.0, 150000.0)["move"] == -100000.0


def test_local_inputs_utilization_excludes_desk_locks():
    conn = _conn()
    ft.get_budget(conn)
    pm.request_entry(conn, "eqd:a", 100000.0)         # desk lock: excluded
    pm.request_entry(conn, "opt:a", 140000.0)         # options margin
    got = ft._local_inputs(conn, 300000.0, vix_fn=lambda: 14.0,
                           nifty_fn=lambda: True)
    assert got["vix"] == 14.0 and got["nifty_uptrend"] is True
    assert got["options_util"] == round(140000.0 / 700000.0, 4)
    assert got["exhaustion_5d"] == 0
    # A crashed source is None, never zero.
    got = ft._local_inputs(conn, 300000.0, vix_fn=lambda: 1 / 0,
                           nifty_fn=lambda: 1 / 0)
    assert got["vix"] is None and got["nifty_uptrend"] is None
    conn.close()


def test_rotation_moves_the_budget_in_one_transaction():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn()
        ledger = Path(tmp) / "ledger.jsonl"
        tiers = _tiers(tmp, [{"symbol": f"S{i}", "valuation": 28,
                              "in_zone": True} for i in range(6)])
        cards = []
        res = ft.run_rotation(conn=conn, tiers_path=tiers,
                              ledger_path=ledger,
                              broadcast_fn=cards.append,
                              vix_fn=lambda: 12.0, nifty_fn=lambda: None)
        # depth(6)+deep value -> 45% -> target 450k -> step-capped +1L.
        assert res["rotated"] and res["split_after"]["equity"] == 400000.0
        assert ft.get_budget(conn) == 400000.0
        [card] = cards
        assert "300,000 → Rs.400,000" in card["description"]
        actions = [json.loads(l)["action"]
                   for l in ledger.read_text().splitlines()]
        assert actions == ["rotated"]
        conn.close()


def test_rotation_holds_inside_deadband_and_dry_runs_clean():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn()
        ledger = Path(tmp) / "ledger.jsonl"
        tiers = _tiers(tmp, [])                       # empty table
        res = ft.run_rotation(conn=conn, tiers_path=tiers,
                              ledger_path=ledger,
                              broadcast_fn=lambda c: None,
                              vix_fn=lambda: None, nifty_fn=lambda: None)
        assert not res["rotated"]                     # base 30% = current
        assert ft.get_budget(conn) == 300000.0
        res = ft.run_rotation(conn=conn, tiers_path=tiers,
                              ledger_path=ledger, dry_run=True,
                              broadcast_fn=lambda c: None,
                              vix_fn=lambda: 21.0, nifty_fn=lambda: None)
        assert not res["rotated"] and res["move"]["move"] == -100000.0
        assert ft.get_budget(conn) == 300000.0        # dry = untouched
        conn.close()


def test_tier_inputs_read_fresh_table():
    with tempfile.TemporaryDirectory() as tmp:
        tiers = _tiers(tmp, [{"symbol": f"S{i}", "valuation": v,
                              "in_zone": True}
                             for i, v in enumerate((22, 28, 31, 35, 40))])
        got = ft._gather_tier_inputs(tiers)
        assert got["buy_depth"] == 5 and got["median_valuation"] == 31
        got = ft._gather_tier_inputs(Path(tmp) / "absent.json")
        assert got == {"buy_depth": None, "median_valuation": None}


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_firm_treasury.py")


# ---------------------------------------- the pool-scale rebase (08-07)
# `get_budget` seeds from the config ONCE and never reads it again, so
# raising `equity_desk_capital_rs` after the ₹2L -> ₹10L injection moved
# NOTHING: the live row still said ₹60,000 and the step-capped rotation
# would have walked it up over three sessions.

def test_a_rebase_jumps_the_step_cap_a_rotation_could_not():
    conn = _conn()
    ft.set_budget(conn, 60_000.0, "the 2L-era budget")
    out = ft.rebase_budget(300_000.0, why="pool 2L -> 10L", conn=conn,
                           ledger_path=Path(tempfile.mkdtemp()) / "l.jsonl")
    assert out["before"] == 60_000.0 and out["after"] == 300_000.0
    # A rotation could only have moved one step per day toward that.
    assert ft.plan_move(60_000.0, 300_000.0)["move"] == ft.TREASURY_MAX_STEP_RS


def test_a_rebase_is_written_to_the_ledger_and_the_audit_trail():
    conn = _conn()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "treasury_ledger.jsonl"
        ft.rebase_budget(300_000.0, why="architect order", conn=conn,
                         ledger_path=ledger)
        rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        assert rows[-1]["action"] == "rebase"
        assert rows[-1]["after_rs"] == 300_000.0
        assert rows[-1]["why"] == "architect order"
    events = [r[0] for r in conn.execute(
        "SELECT detail FROM account_events WHERE event_type = "
        "'treasury_rotation'")]
    assert any("300,000.00" in e for e in events)


def test_the_rotation_step_cap_is_untouched_by_the_rebase_door():
    """The cap exists so the router cannot lurch the book on a noisy
    signal. The manual door must not become a way around it on a cron."""
    conn = _conn()
    ft.rebase_budget(300_000.0, conn=conn,
                     ledger_path=Path(tempfile.mkdtemp()) / "l.jsonl")
    # From 300k, a target of 600k still moves exactly one capped step.
    assert ft.plan_move(300_000.0, 600_000.0)["move"] == ft.TREASURY_MAX_STEP_RS
    assert ft.plan_move(300_000.0, 320_000.0)["move"] == 0.0   # deadband holds
