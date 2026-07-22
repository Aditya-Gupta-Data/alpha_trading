"""
src/firm_treasury.py — Dept 3: capital routing, SINGLE-MACHINE (v2, #83)
========================================================================

Decision #83 (owner override: VM-shift + one database) collapsed v1's
whole cross-machine apparatus: with both desks living in the VM's one
firm account, the two-phase RAISE-FIRST protocol, the mirror locks, the
reconcile rule and the SSH state calls are GONE — the treasury is now
one row (`treasury_state.equity_budget_rs`) updated in one atomic
transaction. Double-spend is impossible because there is ONE cash pool
behind ONE `pm.request_entry` door; the budget only decides how much of
it the equity desk may claim.

THE ROUTER (unchanged from #80 — the approved mechanical formula):
    equity_share = 0.30
      +0.10  NIFTY uptrend        +0.10  Buy-tier depth >= 5
      +0.05  median valuation<=30 −0.10  VIX >= 16
      −0.10  options demand (util > 60% or margin_exhaustion in 5d)
    clamp [treasury_equity_min_pct, max_pct] → ₹25k rounding →
    ₹50k deadband → ₹1L max step; unknown inputs tilt ZERO (#50).
All inputs are now VM-LOCAL: live VIX + NIFTY trend (the token lives
here), utilization from the account's own locks, Buy-tier depth from the
tier table the Mac ships nightly. Cron: 19:50 IST (after the ~19:20
artifact ship, before the next session).

The budget is SOFT by design: nothing is locked for it, so a raise
beyond the firm's liquid cash simply leaves part of the budget
unspendable until options margin frees — `pm.request_entry` remains the
only authority over actual cash.

`vm_push_file` remains — it is the Mac→VM ARTIFACT lane (tier table,
levels, weekly darling ids), pure data delivery with no state invariants.

Every action → `logs/treasury_ledger.jsonl`; ONE Discord card per
rotation. Kill switch `treasury_enabled` (code default OFF).

CLI:
    python3 -m src.firm_treasury                      # status + ledger tail
    python3 -m src.firm_treasury --rotate [--dry-run]
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import portfolio_manager as pm
from src.config import (EQUITY_DESK_CAPITAL_RS, GCLOUD_PATH,
                        TREASURY_DEADBAND_RS, TREASURY_ENABLED,
                        TREASURY_EQUITY_MAX_PCT, TREASURY_EQUITY_MIN_PCT,
                        TREASURY_MAX_STEP_RS, TREASURY_ROUND_RS)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "treasury_ledger.jsonl"


def firm_pool(conn) -> float:
    """The firm's capital base = the account's OWN starting_capital —
    never a constant (decision #84 reset the pool to Rs.2,00,000; a
    hard-coded pool would silently mis-route the day the owner changes
    capital again)."""
    return float(pm.get_account(conn)["starting_capital"])

BASE_EQUITY_SHARE = 0.30
TILT_NIFTY_UPTREND = 0.10
TILT_BUY_DEPTH = 0.10
TILT_DEEP_VALUE = 0.05
TILT_HIGH_VIX = -0.10
TILT_OPTIONS_DEMAND = -0.10
DEPTH_MIN = 5
VALUE_MAX = 30.0
VIX_HIGH = 16.0
UTIL_HIGH = 0.60

VM_SSH_TARGET = "adigupta1998@alpha-trading-vm"
VM_SSH_PROJECT = "project-37632031-10d0-47dd-b6f"
VM_SSH_ZONE = "us-central1-a"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS treasury_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    equity_budget_rs  REAL NOT NULL,
    updated_at        TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _connect(conn):
    if conn is not None:
        return conn, False
    from src import brain_map
    return brain_map.connect(), True


# ------------------------------------------------------------ the budget

def get_budget(conn=None) -> float:
    """The equity desk's routed allocation. Seeded once at the config
    default (`equity_desk_capital_rs`); thereafter only rotations move
    it."""
    conn, owns = _connect(conn)
    try:
        conn.executescript(_SCHEMA)
        row = conn.execute(
            "SELECT equity_budget_rs FROM treasury_state WHERE id = 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO treasury_state (id, equity_budget_rs, "
                "updated_at) VALUES (1, ?, ?)",
                (float(EQUITY_DESK_CAPITAL_RS), _now_iso()))
            conn.commit()
            return float(EQUITY_DESK_CAPITAL_RS)
        return float(row[0])
    finally:
        if owns:
            conn.close()


def set_budget(conn, budget_rs: float, why: str) -> float:
    """One atomic budget move + its audit event. The caller (rotation)
    owns the ledger row and the card."""
    get_budget(conn)                                  # ensure the row
    budget_rs = round(float(budget_rs), 2)
    conn.execute("UPDATE treasury_state SET equity_budget_rs = ?, "
                 "updated_at = ? WHERE id = 1", (budget_rs, _now_iso()))
    conn.commit()
    pm.log_event(conn, "treasury_rotation",
                 f"equity budget set to Rs.{budget_rs:,.2f} ({why})")
    return budget_rs


# ---------------------------------------------------------------- ledger

def _ledger_append(row: dict, ledger_path=None) -> None:
    p = Path(ledger_path) if ledger_path else LEDGER_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(dict(row, ts=_now_iso())) + "\n")
    except OSError:
        pass


def _ledger_tail(n: int, ledger_path=None) -> list:
    p = Path(ledger_path) if ledger_path else LEDGER_PATH
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# --------------------------------------------------------- the router

def compute_target(inputs: dict, pool_rs: float) -> dict:
    """Mechanical share → rupee target against the LIVE pool. Unknown
    inputs (None) contribute zero tilt and are recorded as such —
    absence is never a verdict."""
    share = BASE_EQUITY_SHARE
    tilts = {}
    if inputs.get("nifty_uptrend") is True:
        share += TILT_NIFTY_UPTREND
        tilts["nifty_uptrend"] = TILT_NIFTY_UPTREND
    depth = inputs.get("buy_depth")
    if depth is not None and depth >= DEPTH_MIN:
        share += TILT_BUY_DEPTH
        tilts["buy_depth"] = TILT_BUY_DEPTH
    med = inputs.get("median_valuation")
    if med is not None and depth and med <= VALUE_MAX:
        share += TILT_DEEP_VALUE
        tilts["deep_value"] = TILT_DEEP_VALUE
    vix = inputs.get("vix")
    if vix is not None and vix >= VIX_HIGH:
        share += TILT_HIGH_VIX
        tilts["high_vix"] = TILT_HIGH_VIX
    util = inputs.get("options_util")
    if ((util is not None and util > UTIL_HIGH)
            or (inputs.get("exhaustion_5d") or 0) > 0):
        share += TILT_OPTIONS_DEMAND
        tilts["options_demand"] = TILT_OPTIONS_DEMAND
    lo = TREASURY_EQUITY_MIN_PCT / 100.0
    hi = TREASURY_EQUITY_MAX_PCT / 100.0
    share = min(max(share, lo), hi)
    target = round(share * pool_rs / TREASURY_ROUND_RS) * TREASURY_ROUND_RS
    return {"share": round(share, 4), "target_rs": target, "tilts": tilts,
            "unknown_inputs": [k for k in ("nifty_uptrend", "buy_depth",
                                           "median_valuation", "vix",
                                           "options_util")
                               if inputs.get(k) is None]}


def plan_move(current_rs: float, target_rs: float) -> dict:
    """Deadband + step cap. No liquidity clamps in v2 — the budget is
    soft; actual cash is judged per entry at pm.request_entry."""
    delta = target_rs - current_rs
    if abs(delta) < TREASURY_DEADBAND_RS:
        return {"move": 0.0, "reason": "within deadband"}
    step = max(-TREASURY_MAX_STEP_RS, min(TREASURY_MAX_STEP_RS, delta))
    return {"move": round(step, 2),
            "reason": "raise" if step > 0 else "reduce"}


def _gather_tier_inputs(tiers_path=None) -> dict:
    """Buy-side demand from the tier table the Mac shipped tonight."""
    from src.equity_shadow_proposer import TIERS_PATH
    path = Path(tiers_path) if tiers_path else TIERS_PATH
    try:
        from src.equity_shadow_proposer import entry_eligible_rows
        rows = entry_eligible_rows(json.loads(path.read_text()))
    except Exception:
        return {"buy_depth": None, "median_valuation": None}
    vals = sorted(r.get("valuation") for r in rows
                  if isinstance(r.get("valuation"), (int, float)))
    med = vals[len(vals) // 2] if vals else None
    return {"buy_depth": len(rows), "median_valuation": med}


def _local_inputs(conn, budget: float, vix_fn=None, nifty_fn=None) -> dict:
    """VIX + NIFTY trend live off the VM's own token; utilization and
    exhaustion off the account's own rows. Every miss is None, never 0."""
    if vix_fn is None:
        def vix_fn():
            from src.dhan_client import get_india_vix
            return get_india_vix()
    if nifty_fn is None:
        def nifty_fn():
            from src.suggestions import analyze
            a = analyze("NIFTY 50")
            return None if a is None else bool(a.get("uptrend"))
    try:
        vix = vix_fn()
    except Exception:
        vix = None
    try:
        uptrend = nifty_fn()
    except Exception:
        uptrend = None
    util = None
    try:
        from src.equity_desk import LOCK_PREFIX
        options_locked = float(conn.execute(
            "SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks WHERE "
            "released_at IS NULL AND journal_ref NOT LIKE ?",
            (LOCK_PREFIX + "%",)).fetchone()[0])
        options_capital = max(pm.equity(conn) - budget, 1.0)
        util = round(options_locked / options_capital, 4)
        cutoff = (datetime.now(IST) - timedelta(days=5)).replace(
            tzinfo=None).isoformat(timespec="seconds")
        exhaustion = conn.execute(
            "SELECT COUNT(*) FROM account_events WHERE event_type = ? "
            "AND ts >= ?", ("margin_exhaustion", cutoff)).fetchone()[0]
    except Exception:
        exhaustion = None
    return {"vix": vix, "nifty_uptrend": uptrend, "options_util": util,
            "exhaustion_5d": exhaustion}


def run_rotation(conn=None, tiers_path=None, ledger_path=None,
                 broadcast_fn=None, vix_fn=None, nifty_fn=None,
                 dry_run=False) -> dict:
    """One local treasury cycle: gather → route → one atomic budget move.
    No SSH, no phases, no reconcile — decision #83's whole point."""
    conn, owns = _connect(conn)
    try:
        budget = get_budget(conn)
        pool = firm_pool(conn)
        inputs = dict(_gather_tier_inputs(tiers_path),
                      **_local_inputs(conn, budget, vix_fn=vix_fn,
                                      nifty_fn=nifty_fn))
        routed = compute_target(inputs, pool)
        move = plan_move(budget, routed["target_rs"])
        result = {"inputs": inputs, "routed": routed, "move": move,
                  "pool": pool,
                  "split_before": {"equity": budget,
                                   "options": pool - budget}}
        if dry_run or move["move"] == 0.0:
            _ledger_append({"action": "dry_run" if dry_run else "hold",
                            **result}, ledger_path)
            return dict(result, rotated=False)
        new_budget = set_budget(conn, budget + move["move"],
                                move["reason"])
        result["split_after"] = {"equity": new_budget,
                                 "options": pool - new_budget}
        _ledger_append({"action": "rotated", **result}, ledger_path)
        tilt_line = ", ".join(f"{k} {v:+.2f}" for k, v in
                              routed["tilts"].items()) or "no tilts"
        _broadcast(broadcast_fn,
                   f"🏦 Treasury rotation: equity budget "
                   f"Rs.{budget:,.0f} → Rs.{new_budget:,.0f} "
                   f"(options Rs.{pool - new_budget:,.0f} of the "
                   f"Rs.{pool:,.0f} pool).\n"
                   f"Router: share {routed['share']:.0%} [{tilt_line}]"
                   + (f"\nUnknown inputs: "
                      f"{', '.join(routed['unknown_inputs'])}"
                      if routed["unknown_inputs"] else ""))
        return dict(result, rotated=True)
    finally:
        if owns:
            conn.close()


def _broadcast(broadcast_fn, description: str) -> None:
    try:
        if broadcast_fn is None:
            from src.notifier import fire_broadcast
            broadcast_fn = fire_broadcast
        broadcast_fn({"event": "firm_treasury", "ticker": "TREASURY",
                      "date": datetime.now(IST).date().isoformat(),
                      "description": description})
    except Exception as exc:
        print(f"  (treasury card failed: {exc})")


# ------------------------------------------------ the Mac artifact lane

def vm_push_file(local_path, remote_rel: str = "data/") -> bool:
    """Mac→VM artifact shipping (tier table, levels, weekly darling ids):
    pure data delivery, no state invariants. False on any failure — the
    VM freshness-gates everything it consumes."""
    cmd = [GCLOUD_PATH, "compute", "scp", str(local_path),
           f"{VM_SSH_TARGET}:~/alpha_trading/{remote_rel}",
           f"--project={VM_SSH_PROJECT}", f"--zone={VM_SSH_ZONE}"]
    try:
        return subprocess.run(cmd, capture_output=True,
                              timeout=90).returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    if "--rotate" in sys.argv:
        if not TREASURY_ENABLED:
            print("treasury disabled (treasury_enabled=false)")
        else:
            res = run_rotation(dry_run="--dry-run" in sys.argv)
            print(json.dumps(res, indent=2, default=str))
    else:
        from src import brain_map
        _c = brain_map.connect()
        try:
            budget, pool = get_budget(_c), firm_pool(_c)
        finally:
            _c.close()
        print(f"FIRM TREASURY — equity budget Rs.{budget:,.2f} | "
              f"options Rs.{pool - budget:,.2f} | "
              f"pool Rs.{pool:,.0f}")
        for row in _ledger_tail(5):
            print(" ", row.get("ts"), row.get("action"),
                  (row.get("move") or {}).get("reason", ""))
