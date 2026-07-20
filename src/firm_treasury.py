"""
src/firm_treasury.py — Dept 3: dynamic capital routing between the desks
========================================================================

Owner Directive 1 (2026-07-20, approved with pushbacks): the firm's 10L
paper pool is no longer a static 7L/3L split — capital flows nightly to
the desk with the higher return scope, via a MECHANICAL regime router
(numbers, never vibes) and a two-phase move discipline that makes
double-spending structurally impossible.

THE LEDGER MODEL (one refinement over the approved sketch, applied for
risk-of-ruin honesty): the Mac desk's account base IS its allocation —
the treasury moves capital by SUBSCRIBE/REDEEM on the desk's
`starting_capital` (peak_equity shifts by the same delta, so drawdown
stays measured in real rupees against CURRENT capital). A 10L-based desk
account with a mirror lock would have diluted the desk's 10% ruin halt
to near-uselessness (a 30k loss on a 10L base is 0.3%); this design was
raised as pushback-#4-during-build and keeps #40's ruin protection
meaningful per desk. The VM options account keeps today's mechanism
unchanged: the `equity_desk_allocation` reservation lock equals the
equity desk's allocation. (Documented asymmetry to revisit: the options
account's own dd% stays on its historical 10L base.)

THE INVARIANT (never violated, even mid-crash):
    E_vm  (VM reservation lock)  >=  E_mac  (Mac desk starting_capital)
Equity can spend at most E_mac; options at most 10L − E_vm; so the firm
can never deploy more than 10L. Every move follows RAISE-FIRST:
  * equity gains: raise E_vm on the VM (verified echo) → then raise
    E_mac locally. A crash between the two leaves E_vm > E_mac —
    idle capital, and the next run's reconcile CANCELS the raise.
  * equity sheds: redeem E_mac locally (clamped to the desk's liquid
    cash — deployed capital is never yanked) → then lower E_vm. A crash
    leaves E_vm > E_mac — and the next run's reconcile COMPLETES the
    move by lowering E_vm to E_mac.
Reconcile rule (start of every run): E_vm := E_mac. Deterministic, needs
no intent file, converges after any partial failure.

THE ROUTER (constants below; bounds/steps config-tunable):
    equity_share = 0.30
      +0.10  NIFTY uptrend (VM report — the Mac holds no token)
      +0.10  entry-eligible Buy-tier depth >= 5 (fresh tier table)
      +0.05  median valuation of those names <= 30 (deeply undervalued)
      -0.10  VIX >= 16 (premium rich — the options desk's regime)
      -0.10  options demand: trading-margin utilization > 60% OR any
             margin_exhaustion event in the last 5 days
    clamp [treasury_equity_min_pct, max_pct] → round to Rs.25,000 →
    deadband Rs.50,000 → max step Rs.1,00,000/run. An input the run
    cannot observe contributes ZERO tilt and is recorded as unknown
    (#50 NULL-honesty — absence is never a verdict).

CADENCE: wired inside `patience_basket.eod_chain` AFTER tier grading and
BEFORE the shadow leg — the freshest possible inputs, minutes before the
only moment the equity desk ever spends, hours before the next options
session. The desks trade at disjoint times, so this IS "instant" for
every decision either desk makes (approved pushback #1).

Every action → `logs/treasury_ledger.jsonl`; ONE Discord card on a
rotation/reconcile (quiet nights stay silent; 3 consecutive
VM-unreachable nights fire one warning card, ledger-as-memory).

CLI:
    python3 -m src.firm_treasury                      # status + ledger tail
    python3 -m src.firm_treasury --rotate [--dry-run] # Mac: one cycle
    python3 -m src.firm_treasury --vm-report          # VM: inputs JSON
    python3 -m src.firm_treasury --set-equity-allocation N   # VM: set lock
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import equity_desk
from src import portfolio_manager as pm
from src.config import (GCLOUD_PATH, TREASURY_DEADBAND_RS,
                        TREASURY_ENABLED, TREASURY_EQUITY_MAX_PCT,
                        TREASURY_EQUITY_MIN_PCT, TREASURY_MAX_STEP_RS)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "treasury_ledger.jsonl"

FIRM_POOL_RS = 1_000_000.0
ALLOC_REF = equity_desk.FIRM_ALLOCATION_REF      # "equity_desk_allocation"
ROUND_RS = 25_000.0

# Router tilts — module constants by design (the bounds/steps are config;
# the tilt STRUCTURE changing should be a reviewed code change).
BASE_EQUITY_SHARE = 0.30
TILT_NIFTY_UPTREND = 0.10
TILT_BUY_DEPTH = 0.10        # entry-eligible Buy-tier names >= DEPTH_MIN
TILT_DEEP_VALUE = 0.05       # median valuation of those names <= VALUE_MAX
TILT_HIGH_VIX = -0.10        # vix >= regime high band
TILT_OPTIONS_DEMAND = -0.10  # util > 60% or exhaustion events in 5d
DEPTH_MIN = 5
VALUE_MAX = 30.0
VIX_HIGH = 16.0
UTIL_HIGH = 0.60

VM_SSH_TARGET = "adigupta1998@alpha-trading-vm"
VM_SSH_PROJECT = "project-37632031-10d0-47dd-b6f"
VM_SSH_ZONE = "us-central1-a"


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- ledger

def _ledger_append(row: dict, ledger_path=None) -> None:
    p = Path(ledger_path) if ledger_path else LEDGER_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(dict(row, ts=_now_iso())) + "\n")
    except OSError:
        pass                                     # ledger is telemetry


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


# ------------------------------------------------- VM-side ops (run there)

def vm_report(conn=None) -> dict:
    """Runs ON the VM: everything the Mac router needs, in one JSON.
    Every field fail-opens to None/0 — an unobservable input must tilt
    nothing, never crash the report."""
    from src import brain_map
    owns = conn is None
    if conn is None:
        conn = brain_map.connect()
    try:
        pm.ensure_schema(conn)
        row = conn.execute(
            "SELECT margin_rs FROM margin_locks WHERE journal_ref = ? "
            "AND released_at IS NULL", (ALLOC_REF,)).fetchone()
        alloc = float(row[0]) if row else 0.0
        summary = pm.account_summary(conn)
        cutoff = (datetime.now(IST) - timedelta(days=5)).replace(
            tzinfo=None).isoformat(timespec="seconds")
        exhaustion = conn.execute(
            "SELECT COUNT(*) FROM account_events WHERE event_type = ? "
            "AND ts >= ?", ("margin_exhaustion", cutoff)).fetchone()[0]
    finally:
        if owns:
            conn.close()
    return {"equity_desk_allocation": alloc, "account": summary,
            "vix": _vm_vix(), "nifty_uptrend": _vm_nifty_uptrend(),
            "exhaustion_5d": int(exhaustion)}


def _vm_vix():
    """Snapshot first (zero API calls), live VIX second, None last."""
    try:
        snap = json.loads((ROOT / "data" / "market_snapshot.json").read_text())
        for key, val in (snap.get("spots") or {}).items():
            if "VIX" in str(key).upper() and val:
                return float(val)
    except Exception:
        pass
    try:
        from src.dhan_client import get_india_vix
        return get_india_vix()
    except Exception:
        return None


def _vm_nifty_uptrend():
    try:
        from src.suggestions import analyze
        a = analyze("NIFTY 50")
        return None if a is None else bool(a.get("uptrend"))
    except Exception:
        return None


def vm_set_equity_allocation(amount: float, conn=None) -> dict:
    """Runs ON the VM: set the reservation lock to `amount` (insert on
    first touch). Deliberately NOT request_entry — this is an accounting
    transfer between desks, not a trade entry, so the entry halt stack
    does not apply; the amount was already clamped Mac-side to the
    options desk's liquid cash."""
    from src import brain_map
    owns = conn is None
    if conn is None:
        conn = brain_map.connect()
    try:
        pm.ensure_schema(conn)
        amount = round(float(amount), 2)
        cur = conn.execute(
            "UPDATE margin_locks SET margin_rs = ? WHERE journal_ref = ? "
            "AND released_at IS NULL", (amount, ALLOC_REF))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO margin_locks (journal_ref, margin_rs, locked_at) "
                "VALUES (?, ?, ?)",
                (ALLOC_REF, amount,
                 datetime.now(IST).replace(tzinfo=None).isoformat(
                     timespec="seconds")))
        conn.commit()
        pm.log_event(conn, "treasury_rotation",
                     f"equity desk allocation set to Rs.{amount:,.2f}")
        return {"equity_desk_allocation": amount}
    finally:
        if owns:
            conn.close()


# --------------------------------------------- Mac-side desk capital ops

def subscribe(delta_rs: float, db_path=None) -> dict:
    """Increase the desk's capital base by `delta_rs`. peak_equity shifts
    by the same delta so drawdown keeps measuring the desk's real rupee
    losses against its CURRENT capital (a capital injection is not a
    recovery)."""
    delta = round(float(delta_rs), 2)
    if delta <= 0:
        raise ValueError("subscribe needs a positive delta")
    conn = equity_desk.connect(db_path)
    try:
        conn.execute(
            "UPDATE account_state SET starting_capital = starting_capital + ?, "
            "peak_equity = peak_equity + ? WHERE id = 1", (delta, delta))
        conn.commit()
        pm.log_event(conn, "treasury_subscribe",
                     f"+Rs.{delta:,.2f} from firm treasury")
        return pm.account_summary(conn)
    finally:
        conn.close()


def redeem(delta_rs: float, db_path=None) -> dict:
    """Decrease the desk's capital base. HARD GUARD: never withdraws past
    the desk's liquid cash — deployed capital (open locks) stays until
    positions close, whatever the router wants."""
    delta = round(float(delta_rs), 2)
    if delta <= 0:
        raise ValueError("redeem needs a positive delta")
    conn = equity_desk.connect(db_path)
    try:
        liquid = pm.available_cash(conn)
        if delta > liquid:
            raise ValueError(f"redeem Rs.{delta:,.2f} exceeds liquid "
                             f"Rs.{liquid:,.2f}")
        conn.execute(
            "UPDATE account_state SET starting_capital = starting_capital - ?, "
            "peak_equity = peak_equity - ? WHERE id = 1", (delta, delta))
        conn.commit()
        pm.log_event(conn, "treasury_redeem",
                     f"-Rs.{delta:,.2f} to firm treasury")
        return pm.account_summary(conn)
    finally:
        conn.close()


# --------------------------------------------------------- the router

def compute_target(inputs: dict) -> dict:
    """Mechanical share → rupee target. Unknown inputs (None) contribute
    zero tilt and are recorded as such — absence is never a verdict."""
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
    target = round(share * FIRM_POOL_RS / ROUND_RS) * ROUND_RS
    return {"share": round(share, 4), "target_rs": target, "tilts": tilts,
            "unknown_inputs": [k for k in ("nifty_uptrend", "buy_depth",
                                           "median_valuation", "vix",
                                           "options_util")
                               if inputs.get(k) is None]}


def plan_move(current_rs: float, target_rs: float, desk_liquid: float,
              vm_liquid: float) -> dict:
    """Deadband → step cap → liquidity clamps. A clamp that shrinks the
    move back under the deadband skips it (no dribble moves)."""
    delta = target_rs - current_rs
    if abs(delta) < TREASURY_DEADBAND_RS:
        return {"move": 0.0, "reason": "within deadband"}
    step = max(-TREASURY_MAX_STEP_RS, min(TREASURY_MAX_STEP_RS, delta))
    if step > 0:
        clamped = min(step, max(vm_liquid, 0.0))
        reason = "raise clamped to options liquid" if clamped < step else "raise"
    else:
        clamped = -min(-step, max(desk_liquid, 0.0))
        reason = "redeem clamped to desk liquid" if clamped > step else "redeem"
    if abs(clamped) < TREASURY_DEADBAND_RS:
        return {"move": 0.0, "reason": f"{reason} → under deadband, skipped"}
    return {"move": round(clamped, 2), "reason": reason}


# ------------------------------------------------------------ SSH bridge

def _vm_call(args: list) -> dict | None:
    """Run a firm_treasury CLI mode on the VM, parse the LAST stdout line
    as JSON. None on any failure — the caller keeps the current split."""
    cmd = [GCLOUD_PATH, "compute", "ssh", VM_SSH_TARGET,
           f"--project={VM_SSH_PROJECT}", f"--zone={VM_SSH_ZONE}",
           "--command",
           "cd ~/alpha_trading && venv/bin/python -m src.firm_treasury "
           + " ".join(str(a) for a in args)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=90)
        if out.returncode != 0:
            return None
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


# ------------------------------------------------------------- the cycle

def _gather_tier_inputs(tiers_path=None) -> dict:
    """Buy-side demand from the FRESH tier table (the treasury runs right
    after grading): entry-eligible depth + median valuation."""
    path = Path(tiers_path) if tiers_path else equity_desk.ROOT / "data" / "darling_tiers.json"
    try:
        from src.equity_shadow_proposer import entry_eligible_rows
        rows = entry_eligible_rows(json.loads(path.read_text()))
    except Exception:
        return {"buy_depth": None, "median_valuation": None}
    vals = sorted(r.get("valuation") for r in rows
                  if isinstance(r.get("valuation"), (int, float)))
    med = vals[len(vals) // 2] if vals else None
    return {"buy_depth": len(rows), "median_valuation": med}


def run_rotation(vm_call=None, db_path=None, tiers_path=None,
                 ledger_path=None, broadcast_fn=None,
                 dry_run=False) -> dict:
    """One treasury cycle: reconcile → gather → route → two-phase move.
    Fail-safe throughout: any missing piece keeps the current split."""
    vm_call = vm_call or _vm_call
    conn = equity_desk.connect(db_path)
    try:
        e_mac = float(pm.get_account(conn)["starting_capital"])
        desk_liquid = pm.available_cash(conn)
    finally:
        conn.close()

    vm = vm_call(["--vm-report"])
    if not isinstance(vm, dict) or "equity_desk_allocation" not in vm:
        _ledger_append({"action": "vm_unreachable",
                        "detail": "no VM report — split unchanged"},
                       ledger_path)
        tail = _ledger_tail(3, ledger_path)
        if (len(tail) == 3
                and all(r.get("action") == "vm_unreachable" for r in tail)):
            _broadcast(broadcast_fn,
                       "⚠️ Treasury: 3 consecutive nights without a VM "
                       "report — capital split is frozen at its last "
                       "value. Check the VM/SSH path.")
        return {"rotated": False, "reason": "vm unreachable"}

    e_vm = float(vm["equity_desk_allocation"])
    acct = vm.get("account") or {}

    # RECONCILE: E_vm must equal E_mac. Lowering E_vm to E_mac both
    # cancels a half-done raise and completes a half-done redeem.
    if abs(e_vm - e_mac) >= 0.01 and not dry_run:
        fixed = vm_call(["--set-equity-allocation", f"{e_mac:.2f}"])
        if not fixed or abs(float(fixed.get("equity_desk_allocation", -1))
                            - e_mac) >= 0.01:
            _ledger_append({"action": "aborted",
                            "detail": f"reconcile failed (vm {e_vm:,.0f} "
                                      f"!= mac {e_mac:,.0f})"}, ledger_path)
            return {"rotated": False, "reason": "reconcile failed"}
        _ledger_append({"action": "reconciled",
                        "detail": f"vm lock {e_vm:,.0f} → {e_mac:,.0f}"},
                       ledger_path)
        _broadcast(broadcast_fn,
                   f"🏦 Treasury reconcile: VM lock reset to the desk's "
                   f"Rs.{e_mac:,.0f} (a prior move half-completed).")
        e_vm = e_mac

    vm_equity = acct.get("equity")
    vm_locked = acct.get("locked_margin")
    util = None
    if vm_equity is not None and vm_locked is not None:
        trading_capital = max(float(vm_equity) - e_vm, 1.0)
        util = max(float(vm_locked) - e_vm, 0.0) / trading_capital
    inputs = dict(_gather_tier_inputs(tiers_path),
                  vix=vm.get("vix"), nifty_uptrend=vm.get("nifty_uptrend"),
                  options_util=None if util is None else round(util, 4),
                  exhaustion_5d=vm.get("exhaustion_5d"))
    routed = compute_target(inputs)
    vm_liquid = float(acct.get("available_cash") or 0.0)
    move = plan_move(e_mac, routed["target_rs"], desk_liquid, vm_liquid)

    result = {"inputs": inputs, "routed": routed, "move": move,
              "split_before": {"equity": e_mac,
                               "options": FIRM_POOL_RS - e_mac}}
    if dry_run or move["move"] == 0.0:
        _ledger_append({"action": "dry_run" if dry_run else "hold",
                        **result}, ledger_path)
        return dict(result, rotated=False)

    delta = move["move"]
    new_e = round(e_mac + delta, 2)
    if delta > 0:                    # equity gains: RAISE VM lock first
        set_ok = vm_call(["--set-equity-allocation", f"{new_e:.2f}"])
        if not set_ok or abs(float(set_ok.get("equity_desk_allocation", -1))
                             - new_e) >= 0.01:
            _ledger_append({"action": "aborted",
                            "detail": "vm raise failed — split unchanged",
                            **result}, ledger_path)
            return dict(result, rotated=False)
        subscribe(delta, db_path=db_path)
    else:                            # equity sheds: REDEEM locally first
        redeem(-delta, db_path=db_path)
        set_ok = vm_call(["--set-equity-allocation", f"{new_e:.2f}"])
        if not set_ok:               # E_vm > E_mac → next-run reconcile
            _ledger_append({"action": "rotated_pending_vm",
                            "detail": "vm lower failed — reconcile will "
                                      "complete it", **result}, ledger_path)

    result["split_after"] = {"equity": new_e,
                             "options": FIRM_POOL_RS - new_e}
    _ledger_append({"action": "rotated", **result}, ledger_path)
    tilt_line = ", ".join(f"{k} {v:+.2f}" for k, v in
                          routed["tilts"].items()) or "no tilts"
    _broadcast(broadcast_fn,
               f"🏦 Treasury rotation: equity Rs.{e_mac:,.0f} → "
               f"Rs.{new_e:,.0f} (options Rs.{FIRM_POOL_RS - new_e:,.0f})."
               f"\nRouter: share {routed['share']:.0%} [{tilt_line}]"
               + (f"\nUnknown inputs: "
                  f"{', '.join(routed['unknown_inputs'])}"
                  if routed["unknown_inputs"] else ""))
    return dict(result, rotated=True)


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


if __name__ == "__main__":
    import sys
    if "--vm-report" in sys.argv:
        print(json.dumps(vm_report()))
    elif "--set-equity-allocation" in sys.argv:
        idx = sys.argv.index("--set-equity-allocation")
        print(json.dumps(vm_set_equity_allocation(float(sys.argv[idx + 1]))))
    elif "--rotate" in sys.argv:
        res = run_rotation(dry_run="--dry-run" in sys.argv)
        print(json.dumps(res, indent=2, default=str))
    else:
        conn = equity_desk.connect()
        try:
            e_mac = pm.get_account(conn)["starting_capital"]
        finally:
            conn.close()
        print(f"FIRM TREASURY — equity desk allocation Rs.{e_mac:,.2f} | "
              f"options desk Rs.{FIRM_POOL_RS - e_mac:,.2f} | "
              f"pool Rs.{FIRM_POOL_RS:,.0f}")
        for row in _ledger_tail(5):
            print(" ", row.get("ts"), row.get("action"),
                  row.get("detail", ""))
