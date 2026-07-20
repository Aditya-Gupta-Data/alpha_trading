"""
Firm treasury (owner Directive 1, decision #80) — hermetic tests.

The dynamic capital router between the desks: mechanical regime tilts,
deadband/step/liquidity clamps, subscribe/redeem capital ops with
drawdown-honest peak shifts, and the RAISE-FIRST two-phase discipline
whose invariant (E_vm >= E_mac) makes double-spending impossible under
any partial failure. Fully offline: tmp desk DBs, :memory: VM accounts,
scripted vm_call recorders. Run:
    python -m pytest tests/test_firm_treasury.py
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.equity_desk as desk
import src.firm_treasury as ft
from src import portfolio_manager as pm

# Pin the knobs read at import time (same guard as test_equity_desk).
desk.EQUITY_DESK_CAPITAL_RS = 300000.0
ft.TREASURY_DEADBAND_RS = 50000.0
ft.TREASURY_MAX_STEP_RS = 100000.0
ft.TREASURY_EQUITY_MIN_PCT = 15.0
ft.TREASURY_EQUITY_MAX_PCT = 60.0


def _vm_account(equity_pnl=0.0, extra_locks=()):
    """A :memory: options account: 10L base + the 3L reservation."""
    conn = sqlite3.connect(":memory:")
    pm.get_account(conn)
    pm.request_entry(conn, ft.ALLOC_REF, 300000.0)
    for ref, rs in extra_locks:
        pm.request_entry(conn, ref, rs)
    if equity_pnl:
        pm.request_entry(conn, "x", 1000.0)
        pm.release_margin(conn, "x", equity_pnl)
    return conn


def _report(alloc=300000.0, vix=None, uptrend=None, exhaustion=0,
            equity=1_000_000.0, locked=300000.0, cash=700000.0):
    return {"equity_desk_allocation": alloc, "vix": vix,
            "nifty_uptrend": uptrend, "exhaustion_5d": exhaustion,
            "account": {"equity": equity, "locked_margin": locked,
                        "available_cash": cash}}


class _VmSpy:
    """Scripted vm_call: returns queued responses, records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.responses.pop(0) if self.responses else None


def test_compute_target_tilts_clamps_and_null_honesty():
    # All-unknown inputs -> base share, everything listed as unknown.
    r = ft.compute_target({})
    assert r["share"] == 0.30 and r["target_rs"] == 300000.0
    assert set(r["unknown_inputs"]) == {"nifty_uptrend", "buy_depth",
                                        "median_valuation", "vix",
                                        "options_util"}
    # Full bull-for-equity case clamps at the 60% ceiling.
    r = ft.compute_target({"nifty_uptrend": True, "buy_depth": 8,
                           "median_valuation": 28.0, "vix": 12.0,
                           "options_util": 0.10, "exhaustion_5d": 0})
    assert r["share"] == 0.55 and r["target_rs"] == 550000.0
    # Full options-demand case clamps at the 15% floor.
    r = ft.compute_target({"nifty_uptrend": False, "buy_depth": 0,
                           "median_valuation": None, "vix": 21.0,
                           "options_util": 0.75, "exhaustion_5d": 2})
    assert r["share"] == 0.15 and r["target_rs"] == 150000.0
    # Exhaustion alone triggers the demand tilt even with low util.
    r = ft.compute_target({"options_util": 0.05, "exhaustion_5d": 1})
    assert "options_demand" in r["tilts"]


def test_plan_move_deadband_step_and_liquidity_clamps():
    hold = ft.plan_move(300000.0, 320000.0, 300000.0, 700000.0)
    assert hold["move"] == 0.0                     # inside deadband
    up = ft.plan_move(300000.0, 550000.0, 300000.0, 700000.0)
    assert up["move"] == 100000.0                  # step cap
    down = ft.plan_move(300000.0, 150000.0, 300000.0, 700000.0)
    assert down["move"] == -100000.0
    # Raise clamped by the options desk's liquid cash.
    up = ft.plan_move(300000.0, 550000.0, 300000.0, 60000.0)
    assert up["move"] == 60000.0
    # Redeem clamped by the equity desk's liquid cash.
    down = ft.plan_move(300000.0, 150000.0, 80000.0, 700000.0)
    assert down["move"] == -80000.0
    # A clamp that lands under the deadband skips (no dribble moves).
    up = ft.plan_move(300000.0, 550000.0, 300000.0, 20000.0)
    assert up["move"] == 0.0 and "deadband" in up["reason"]


def test_subscribe_and_redeem_shift_peak_with_base():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "desk.db"
        conn = desk.connect(db)
        pm.request_entry(conn, "pos", 100000.0)
        pm.release_margin(conn, "pos", -30000.0)   # 10% dd on 3L base
        assert pm.drawdown_pct(conn) == 10.0
        conn.close()
        s = ft.subscribe(300000.0, db_path=db)     # base 3L -> 6L
        assert s["starting_capital"] == 600000.0
        # The same Rs.30k loss is now 5% of the bigger book — the rupee
        # loss survives the injection, the ratio rebases. Never a reset.
        assert abs(s["drawdown_pct"] - 5.0) < 0.1
        r = ft.redeem(200000.0, db_path=db)
        assert r["starting_capital"] == 400000.0
        # Liquid guard: deployed capital can never be withdrawn.
        conn = desk.connect(db)
        pm.request_entry(conn, "pos2", 300000.0)
        liquid = pm.available_cash(conn)
        conn.close()
        try:
            ft.redeem(liquid + 1, db_path=db)
            raised = False
        except ValueError:
            raised = True
        assert raised


def test_vm_report_and_set_allocation_roundtrip():
    conn = _vm_account()
    ft._vm_vix, ft._vm_nifty_uptrend = (lambda: None), (lambda: None)
    rep = ft.vm_report(conn=conn)
    assert rep["equity_desk_allocation"] == 300000.0
    assert rep["exhaustion_5d"] == 0
    out = ft.vm_set_equity_allocation(425000.0, conn=conn)
    assert out["equity_desk_allocation"] == 425000.0
    assert pm.locked_margin(conn) == 425000.0      # updated, not doubled
    conn.close()


def test_rotation_raise_is_vm_first_then_subscribe():
    with tempfile.TemporaryDirectory() as tmp:
        db, ledger = Path(tmp) / "desk.db", Path(tmp) / "ledger.jsonl"
        desk.connect(db).close()
        spy = _VmSpy([_report(uptrend=True),        # -> target 4L
                      {"equity_desk_allocation": 400000.0}])
        cards = []
        res = ft.run_rotation(vm_call=spy, db_path=db,
                              tiers_path=Path(tmp) / "none.json",
                              ledger_path=ledger,
                              broadcast_fn=cards.append)
        assert res["rotated"] and res["split_after"]["equity"] == 400000.0
        # Two-phase order: the VM lock rose BEFORE the desk base did.
        assert spy.calls == [["--vm-report"],
                             ["--set-equity-allocation", "400000.00"]]
        conn = desk.connect(db)
        assert pm.get_account(conn)["starting_capital"] == 400000.0
        conn.close()
        [card] = cards
        assert "300,000 → Rs.400,000" in card["description"]
        actions = [json.loads(l)["action"]
                   for l in ledger.read_text().splitlines()]
        assert actions == ["rotated"]


def test_rotation_raise_aborts_clean_when_vm_set_fails():
    with tempfile.TemporaryDirectory() as tmp:
        db, ledger = Path(tmp) / "desk.db", Path(tmp) / "ledger.jsonl"
        desk.connect(db).close()
        spy = _VmSpy([_report(uptrend=True), None])  # set fails
        res = ft.run_rotation(vm_call=spy, db_path=db,
                              tiers_path=Path(tmp) / "none.json",
                              ledger_path=ledger, broadcast_fn=lambda c: None)
        assert not res["rotated"]
        conn = desk.connect(db)                      # desk untouched
        assert pm.get_account(conn)["starting_capital"] == 300000.0
        conn.close()
        assert json.loads(ledger.read_text().splitlines()[-1])[
            "action"] == "aborted"


def test_rotation_redeem_is_local_first_and_survives_vm_failure():
    with tempfile.TemporaryDirectory() as tmp:
        db, ledger = Path(tmp) / "desk.db", Path(tmp) / "ledger.jsonl"
        desk.connect(db).close()
        # High VIX + options demand -> floor 15% -> step-capped to -1L.
        spy = _VmSpy([_report(vix=21.0, exhaustion=1), None])  # vm set FAILS
        res = ft.run_rotation(vm_call=spy, db_path=db,
                              tiers_path=Path(tmp) / "none.json",
                              ledger_path=ledger, broadcast_fn=lambda c: None)
        assert res["rotated"]                        # local redeem stands
        conn = desk.connect(db)
        assert pm.get_account(conn)["starting_capital"] == 200000.0
        conn.close()
        actions = [json.loads(l)["action"]
                   for l in ledger.read_text().splitlines()]
        assert "rotated_pending_vm" in actions
        # Next run: VM still shows 3L -> reconcile lowers it to 2L, then
        # holds (fresh report already at the 2L target -> deadband).
        spy2 = _VmSpy([_report(alloc=300000.0, vix=21.0, exhaustion=1),
                       {"equity_desk_allocation": 200000.0},
                       {"equity_desk_allocation": 150000.0}])
        res2 = ft.run_rotation(vm_call=spy2, db_path=db,
                               tiers_path=Path(tmp) / "none.json",
                               ledger_path=ledger,
                               broadcast_fn=lambda c: None)
        assert ["--set-equity-allocation", "200000.00"] in spy2.calls
        # Invariant held throughout: E_vm >= E_mac at every step.
        assert res2["split_before"]["equity"] == 200000.0


def test_reconcile_cancels_a_half_done_raise():
    with tempfile.TemporaryDirectory() as tmp:
        db, ledger = Path(tmp) / "desk.db", Path(tmp) / "ledger.jsonl"
        desk.connect(db).close()                     # E_mac = 3L
        # VM claims 4L (raise succeeded, local subscribe crashed).
        spy = _VmSpy([_report(alloc=400000.0),
                      {"equity_desk_allocation": 300000.0}])
        cards = []
        res = ft.run_rotation(vm_call=spy, db_path=db,
                              tiers_path=Path(tmp) / "none.json",
                              ledger_path=ledger, broadcast_fn=cards.append)
        assert ["--set-equity-allocation", "300000.00"] in spy.calls
        assert not res["rotated"]                    # then base-case hold
        assert any("reconcile" in c["description"] for c in cards)


def test_vm_unreachable_freezes_split_and_third_night_cards():
    with tempfile.TemporaryDirectory() as tmp:
        db, ledger = Path(tmp) / "desk.db", Path(tmp) / "ledger.jsonl"
        desk.connect(db).close()
        cards = []
        for _ in range(3):
            res = ft.run_rotation(vm_call=lambda a: None, db_path=db,
                                  tiers_path=Path(tmp) / "none.json",
                                  ledger_path=ledger,
                                  broadcast_fn=cards.append)
            assert not res["rotated"]
        conn = desk.connect(db)
        assert pm.get_account(conn)["starting_capital"] == 300000.0
        conn.close()
        [card] = cards                               # exactly one warning
        assert "3 consecutive nights" in card["description"]


def test_dry_run_writes_nothing_but_shows_the_plan():
    with tempfile.TemporaryDirectory() as tmp:
        db, ledger = Path(tmp) / "desk.db", Path(tmp) / "ledger.jsonl"
        desk.connect(db).close()
        spy = _VmSpy([_report(uptrend=True)])
        res = ft.run_rotation(vm_call=spy, db_path=db,
                              tiers_path=Path(tmp) / "none.json",
                              ledger_path=ledger,
                              broadcast_fn=lambda c: None, dry_run=True)
        assert not res["rotated"] and res["move"]["move"] == 100000.0
        assert spy.calls == [["--vm-report"]]        # no set call
        conn = desk.connect(db)
        assert pm.get_account(conn)["starting_capital"] == 300000.0
        conn.close()


def test_tier_inputs_read_fresh_table():
    with tempfile.TemporaryDirectory() as tmp:
        tiers = Path(tmp) / "darling_tiers.json"
        rows = [{"symbol": f"S{i}", "valuation": v, "in_zone": True}
                for i, v in enumerate((22, 28, 31, 35, 40))]
        tiers.write_text(json.dumps({"tiers": {"strong_buy": rows}}))
        got = ft._gather_tier_inputs(tiers)
        assert got["buy_depth"] == 5 and got["median_valuation"] == 31
        # Missing table -> honest unknowns, never zeros.
        got = ft._gather_tier_inputs(Path(tmp) / "absent.json")
        assert got == {"buy_depth": None, "median_valuation": None}


if __name__ == "__main__":
    print("Run via pytest: python -m pytest tests/test_firm_treasury.py")
