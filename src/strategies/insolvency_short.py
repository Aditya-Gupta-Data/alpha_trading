"""
src/strategies/insolvency_short.py — the Insolvency Short (Week-1 release, SHADOW)
================================================================================

WHAT IT IS. The sandbox's one OOS-verified event study — distress filings
are followed by negative median returns (TRAIN 2019-23 median −2.50%/−3.03%,
VERIFY 2024-26 −4.94%/−5.12%, hit ~31-40%) — turned into a strategy module:

  trigger  : a corporate-events filing whose SEBI subject is EXACTLY
             "Corporate Insolvency Resolution Process" or "Defaults on Payment"
  universe : the symbol must be in the active F&O list (`data/fo_liquidity.json`)
             and NOT banned; a bear put spread additionally needs LISTED STOCK
             OPTIONS, which the house defines as `tier1` in that same file
  structure: BEAR PUT SPREAD ONLY (StrategyConstructor.construct_bear_put_spread)
             — defined max loss; never a naked short, never a bare put
  sizing   : hard cap RISK_PCT_PER_SETUP = 0.5% of the pool per setup, where
             risk = the spread's max_loss per lot × lots; lots = floor(cap /
             max_loss_per_lot); zero lots ⇒ rejected, never rounded up
  exit     : time-based at HOLD_SESSIONS = 5 sessions after entry (the
             backtest's measured median-drop horizon), stamped on the setup
             so the tracker can close it — plus plan_tracker's own
             pre-expiry rule, whichever comes first

WHAT IT IS NOT — READ THIS BEFORE WIRING IT TO ANYTHING.
This module is SHADOW-ONLY: `run()` writes setups to
`logs/insolvency_short_shadow.jsonl` and returns them; nothing here touches
the journal, the treasury or a lock. That is not caution for its own sake —
it is what the F&O measurement said (2026-08-16, same event-study
machinery, `tests` pin the numbers' provenance in the docstring below):

    ALL symbols   CIRP  TRAIN n=1209 med −2.50%   VERIFY n=666 med −4.83%
    F&O symbols   CIRP  TRAIN n=10   med +0.59%   VERIFY n=9   med +0.37%   (3 symbols total)
    tier1 (options) CIRP / Defaults          ZERO events in 7 years

The edge lives in distressed non-F&O small caps — exactly the names that
have no listed options and where the delisting bias lives. Once the F&O
gate is applied (as it must be for a put spread), the sample is 19 filings
from 3 symbols with the OPPOSITE sign, and the option-tradeable subset has
never fired. So this module, run live, would either never trigger or
trigger on a sub-population the backtest does not support. It ships as
the honest scaffold the release asked for; promotion to capital is a
Department-5 decision that needs the F&O-subset study to pass on its own,
which today it cannot (n<10, wrong sign). Decision #89.

Fail-open like every advisory module: a missing lake, an unreadable F&O
file or a broken constructor yields a named skip reason, never a trade
and never a crash.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FO_PATH = ROOT / "data" / "fo_liquidity.json"
SHADOW_LEDGER = ROOT / "logs" / "insolvency_short_shadow.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

# The two SEBI-taxonomy subjects — EXACT, case-insensitive match on the
# filing's `subject`, not a substring search: "Insolvency" alone also
# catches a creditor announcing CIRP against a borrower.
TRIGGER_SUBJECTS = frozenset({
    "corporate insolvency resolution process",
    "defaults on payment",
})
RISK_PCT_PER_SETUP = 0.005      # 0.5% of the pool, hard cap, per setup
HOLD_SESSIONS = 5               # time exit — the backtest's median-drop horizon
STRUCTURE = "bear_put_spread"   # the ONLY structure this module will build
MODE = "shadow"                 # nothing here reaches capital


# ------------------------------------------------------------- triggers

def is_trigger(subject) -> bool:
    return str(subject or "").strip().lower() in TRIGGER_SUBJECTS


def scan_triggers(day: str, rows: list = None, read_day_fn=None) -> list:
    """Filings on `day` whose subject is one of the two trigger subjects.
    `rows` may be injected (tests); otherwise the events lake is read
    through `src.lake.read_day`. Unreadable lake ⇒ [] (fail-open)."""
    if rows is None:
        try:
            if read_day_fn is None:
                from src import lake
                read_day_fn = lambda d: lake.read_day("events", d)
            rows = read_day_fn(day) or []
        except Exception:
            return []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not is_trigger(r.get("subject")):
            continue
        sym = str(r.get("symbol") or "").split(".")[0].upper()
        if not sym:
            continue
        out.append({"date": r.get("as_of") or day, "symbol": sym,
                    "subject": r.get("subject"),
                    "attachment": r.get("attachment")})
    return out


# ------------------------------------------------------------- F&O gate

def load_fo(path=None) -> dict | None:
    try:
        return json.loads(Path(path or FO_PATH).read_text())
    except (OSError, ValueError):
        return None


def fo_gate(symbol: str, fo: dict = None, path=None) -> tuple:
    """(allowed, reason). Requires: in the F&O list, not banned, and
    option-tradeable (`tier1`) — a bear put spread cannot exist on a
    futures-only name. Missing file ⇒ FAIL-CLOSED here (unlike the
    advisory gates): with no universe we cannot say the name is
    hedgeable, and an unhedgeable short is exactly what this refuses."""
    fo = fo if fo is not None else load_fo(path)
    if not fo:
        return False, "fo_list_unavailable"
    sym = str(symbol or "").upper()
    if sym in set(fo.get("banned") or []):
        return False, "fo_banned"
    row = (fo.get("symbols") or {}).get(sym)
    if row is None:
        return False, "not_in_fo"
    if row.get("tier") != "tier1":
        return False, "no_listed_options"
    return True, "fo_tier1"


# ---------------------------------------------------------------- sizing

def lots_for(pool_rupees: float, max_loss_per_lot: float,
             risk_pct: float = RISK_PCT_PER_SETUP) -> int:
    """floor(0.5% × pool ÷ max_loss_per_lot). Never rounds up; a spread
    whose one-lot max loss exceeds the cap gets ZERO lots."""
    try:
        cap = float(pool_rupees) * float(risk_pct)
        ml = float(max_loss_per_lot)
    except (TypeError, ValueError):
        return 0
    if cap <= 0 or ml <= 0:
        return 0
    return int(math.floor(cap / ml))


# ---------------------------------------------------------------- setup

def build_setup(trigger: dict, spot: float, buy_strike: float,
                sell_strike: float, buy_premium: float, sell_premium: float,
                lot_size: int, expiry: str, pool_rupees: float,
                vix: float = None, fo: dict = None, fo_path=None,
                sessions_fn=None) -> dict:
    """One trigger → one setup dict, or a rejection with a named reason.
    Strikes/premiums are injected — this module owns the RULES, not the
    chain read (the chain door is dhan_guard, one door per concern)."""
    sym = trigger["symbol"]
    ok, why = fo_gate(sym, fo=fo, path=fo_path)
    base = {"mode": MODE, "symbol": sym, "trigger": trigger,
            "structure": STRUCTURE, "hold_sessions": HOLD_SESSIONS}
    if not ok:
        return {**base, "accepted": False, "reason": why}
    from src.strategy import StrategyConstructor
    spread = StrategyConstructor(vix=vix, lot_size=lot_size)\
        .construct_bear_put_spread(buy_strike, sell_strike,
                                   buy_premium, sell_premium)
    if not spread:
        return {**base, "accepted": False, "reason": "spread_incoherent"}
    max_loss = float(spread.get("max_loss") or 0)
    lots = lots_for(pool_rupees, max_loss)
    if lots < 1:
        return {**base, "accepted": False,
                "reason": f"risk_cap: one lot risks Rs.{max_loss:,.0f} > "
                          f"{RISK_PCT_PER_SETUP:.1%} of Rs.{pool_rupees:,.0f}"}
    entry_day = date.fromisoformat(str(trigger["date"]))
    exit_by = (sessions_fn(entry_day, HOLD_SESSIONS) if sessions_fn
               else _calendar_sessions_after(entry_day, HOLD_SESSIONS))
    spread = {**spread, "expiry": expiry, "lots": lots,
              "risk_rupees": round(max_loss * lots, 2)}
    return {**base, "accepted": True, "reason": "ok", "spot": spot,
            "spread": spread, "lots": lots,
            "risk_rupees": spread["risk_rupees"],
            "risk_pct_of_pool": round(max_loss * lots / float(pool_rupees) * 100, 3),
            "time_exit_on": exit_by.isoformat(),
            "exit_rule": f"time exit at session +{HOLD_SESSIONS} "
                         f"({exit_by.isoformat()}) or plan_tracker pre-expiry, "
                         f"whichever first"}


def _calendar_sessions_after(d: date, n: int) -> date:
    """Weekday-only fallback for the exit date (no holiday calendar
    here; the tracker uses real sessions when it settles)."""
    out, k = d, 0
    while k < n:
        out += timedelta(days=1)
        if out.weekday() < 5:
            k += 1
    return out


# ------------------------------------------------------------------ run

def run(day: str = None, rows: list = None, pool_rupees: float = 200_000.0,
        fo_path=None, quote_fn=None, ledger_path=None, now=None) -> dict:
    """Shadow pass for one day: scan → gate → (setup if a chain reader is
    injected) → append to the shadow ledger. With no `quote_fn` the pass
    still records every trigger and its gate verdict — the F&O rejection
    IS the finding worth logging."""
    day = day or datetime.now(tz=IST).date().isoformat()
    triggers = scan_triggers(day, rows=rows)
    fo = load_fo(fo_path)
    out = {"day": day, "mode": MODE, "triggers": len(triggers),
           "setups": [], "rejected": []}
    for t in triggers:
        ok, why = fo_gate(t["symbol"], fo=fo)
        if not ok:
            out["rejected"].append({**t, "reason": why}); continue
        if quote_fn is None:
            out["rejected"].append({**t, "reason": "no_chain_reader_injected"})
            continue
        try:
            q = quote_fn(t["symbol"]) or {}
            setup = build_setup(t, pool_rupees=pool_rupees, fo=fo, **q)
        except Exception as e:                       # fail-open, named
            setup = {"mode": MODE, "symbol": t["symbol"], "accepted": False,
                     "reason": f"setup_error: {e}"}
        (out["setups"] if setup.get("accepted") else out["rejected"]).append(setup)
    _append(ledger_path, {"ts": (now or datetime.now(tz=IST)).isoformat(),
                          **out})
    return out


def _append(path, row: dict) -> None:
    try:
        p = Path(path or SHADOW_LEDGER)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Insolvency short — SHADOW pass")
    ap.add_argument("--day", default=None)
    a = ap.parse_args(argv)
    r = run(day=a.day)
    print(f"insolvency_short [{MODE}] {r['day']}: {r['triggers']} trigger(s), "
          f"{len(r['setups'])} setup(s), {len(r['rejected'])} rejected")
    for x in r["rejected"]:
        print(f"  ✗ {x.get('symbol')}: {x.get('reason')}")
    for s in r["setups"]:
        print(f"  ✓ {s['symbol']}: {s['lots']} lot(s), risk Rs.{s['risk_rupees']:,.0f} "
              f"({s['risk_pct_of_pool']}%), exit {s['time_exit_on']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
