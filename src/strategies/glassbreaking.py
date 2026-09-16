"""
src/strategies/glassbreaking.py — "Glassbreaking Profits" (Phase 4 / M4A), SHADOW
=================================================================================

WHAT IT IS. Two EARLY-ENTRY primitives that hunt asymmetric returns, each
bolted to a DEFINED-RISK option structure and prepared for pyramiding:

  falling_knife    catch a deep sell-off early. Fires on EITHER
                   (a) Wilder RSI(14) <= RSI_OVERSOLD (30), or
                   (b) anchored-VWAP support: the close sits within
                       AVWAP_TOUCH_PCT above the VWAP anchored at the
                       highest-volume session of the lookback (where the
                       most money is positioned),
                   and ONLY when the name has actually fallen
                   KNIFE_MIN_DRAWDOWN_PCT from its lookback high — a
                   knife must be falling before it can be caught.
  early_breakout   catch a move before the moving averages can see it.
                   Fires on a pre-market / opening GAP UP >= GAP_UP_MIN_PCT
                   over the previous close TOGETHER WITH institutional
                   volume: today's volume >= VOLUME_SPIKE_MULT × the
                   20-session average, or a bulk/block deal print. The
                   SMA 50/200 read is DELIBERATELY NOT CONSULTED
                   (`sma_bypassed: True` is stamped on every signal so the
                   court can see what was skipped).

RISK-GATED ROUTING (the "strategy router" of this module). Each primitive
maps to exactly ONE structure and nothing else:
      falling_knife  -> bull_call_spread   (debit, long-premium)
      early_breakout -> bull_call_spread   (debit, long-premium)
`assert_defined_risk` then re-checks the BUILT spread: >= 2 legs, every
SELL leg hedged by a BUY leg of the same option type, finite positive
max_loss, strategy in DEFINED_RISK_STRUCTURES. A naked option — any
structure that fails that check — is REJECTED with a named reason. This
is what caps the loss at the spread's max_loss even if the underlying is
halted, goes into insolvency or gaps to zero: the long leg is owned, the
short leg is covered.

PYRAMIDING PREP (M4A). Every accepted setup carries
      plan.tranches = {"levels": [{"at_r": 1.0, "add_lots": base},
                                  {"at_r": 2.0, "add_lots": base}],
                       "max_lots": 3 × base}
      plan.trailing = {"atr_mult": 2.0, "atr_n": 14, "on": "underlying"}
which `plan_tracker` OBSERVES (where the add-ons would have fired and
what they would have earned) and ratchets an ATR trail on the underlying.
Add-ons are never booked to cash or margin in this release (decision #96).

SHADOW-ONLY, like `insolvency_short` (decision #89): `run()` writes setups
to `logs/glassbreaking_shadow.jsonl` and enrols each firing in the Proving
Court (Department 5: `validation.registry` + `validation.trial`), where
`court_scorecard` reports n / wins / one-sided 95% Wilson lower bound.
Nothing here touches the journal, the treasury or a margin lock, and
nothing schedules it — promotion to capital is a Department-5 decision on
a positive Wilson lower bound over MIN_COURT_N resolutions.

Fail-open like every advisory module: missing volume, a short history, a
missing F&O file or a broken constructor yields a named skip reason,
never a trade and never a crash. Every price input is injected — this
module owns the RULES, not a data door (one door per concern).
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.indicators import rsi as wilder_rsi
from src.strategies.insolvency_short import fo_gate, lots_for

ROOT = Path(__file__).resolve().parent.parent.parent
SHADOW_LEDGER = ROOT / "logs" / "glassbreaking_shadow.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

MODE = "shadow"
STRATEGY = "glassbreaking"
KIND = "strategy_rule"                    # registry kind

# --- falling knife ---------------------------------------------------------
RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
KNIFE_LOOKBACK = 60                       # sessions: the high we fell from
KNIFE_MIN_DRAWDOWN_PCT = 8.0              # must have fallen this much
AVWAP_TOUCH_PCT = 1.0                     # close within 1% ABOVE the AVWAP

# --- early breakout --------------------------------------------------------
GAP_UP_MIN_PCT = 2.0
VOLUME_LOOKBACK = 20
VOLUME_SPIKE_MULT = 2.0

# --- routing + risk --------------------------------------------------------
STRUCTURES = {"falling_knife": "bull_call_spread",
              "early_breakout": "bull_call_spread"}
DEFINED_RISK_STRUCTURES = frozenset({"bull_call_spread", "bear_put_spread",
                                     "iron_condor", "iron_butterfly"})
RISK_PCT_PER_SETUP = 0.005                # 0.5% of the pool per setup
HOLD_SESSIONS = 10                        # time exit for the shadow grader

# --- pyramiding + trailing -------------------------------------------------
PYRAMID_LEVELS_R = (1.0, 2.0)
PYRAMID_MAX_MULT = 3
TRAIL_ATR_MULT = 2.0
TRAIL_ATR_N = 14

# --- Proving Court ---------------------------------------------------------
# The bar is the court's own (stat_gates.promotable): combined n >= the
# configured floor (harness_min_resolutions, default 7), at least one REAL
# resolution, and the one-sided 95% Wilson LOWER bound above the structural
# breakeven null for a debit spread's payoff shape. No private threshold.
COURT_AVG_WIN_R = 1.5                     # payoff shape the null is built from
COURT_AVG_LOSS_R = 1.0


# ============================================================ indicators

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def anchored_vwap(bars: list, anchor_index: int):
    """Volume-weighted typical price from `anchor_index` to the last bar.
    Bars are dicts with high/low/close and volume. None when any volume in
    the span is missing or the total is zero — an unweighted average is
    not a VWAP and must not pose as one."""
    if not bars or anchor_index is None or anchor_index < 0 or anchor_index >= len(bars):
        return None
    pv = vol = 0.0
    for b in bars[anchor_index:]:
        v = _f(b.get("volume"))
        h, l, c = _f(b.get("high")), _f(b.get("low")), _f(b.get("close"))
        if v is None or v <= 0 or None in (h, l, c):
            return None
        pv += (h + l + c) / 3.0 * v
        vol += v
    return round(pv / vol, 4) if vol > 0 else None


def anchor_index(bars: list, lookback: int = KNIFE_LOOKBACK):
    """The highest-VOLUME session of the lookback — the institutional
    anchor. None when no bar in the window carries volume."""
    if not bars:
        return None
    start = max(0, len(bars) - lookback)
    best, best_v = None, 0.0
    for i in range(start, len(bars)):
        v = _f(bars[i].get("volume"))
        if v is not None and v > best_v:
            best, best_v = i, v
    return best


def lookback_high(bars: list, lookback: int = KNIFE_LOOKBACK):
    highs = [_f(b.get("high")) for b in bars[-lookback:]]
    highs = [h for h in highs if h is not None]
    return max(highs) if highs else None


# ============================================================ primitives

def falling_knife_signal(bars: list, rsi_period: int = RSI_PERIOD) -> dict | None:
    """Bars: chronological dicts {date, open, high, low, close, volume?}.
    None unless the name is a falling knife AND at least one trigger
    fires. Pure."""
    if not bars or len(bars) < 2:
        return None
    last = bars[-1]
    close = _f(last.get("close"))
    hi = lookback_high(bars)
    if close is None or hi is None or hi <= 0:
        return None
    drawdown_pct = (hi - close) / hi * 100.0
    if drawdown_pct < KNIFE_MIN_DRAWDOWN_PCT:
        return None                       # not falling — not a knife
    closes = [_f(b.get("close")) for b in bars]
    if any(c is None for c in closes):
        return None
    r = wilder_rsi(closes, rsi_period)
    ai = anchor_index(bars)
    av = anchored_vwap(bars, ai) if ai is not None else None
    triggers = []
    if r is not None and r <= RSI_OVERSOLD:
        triggers.append("rsi_oversold")
    if av is not None and av > 0:
        above_pct = (close - av) / av * 100.0
        if 0.0 <= above_pct <= AVWAP_TOUCH_PCT:
            triggers.append("avwap_support")
    if not triggers:
        return None
    return {"primitive": "falling_knife", "direction": "bullish",
            "date": str(last.get("date")), "spot": close,
            "triggers": triggers,
            "rsi": round(r, 2) if r is not None else None,
            "anchored_vwap": av,
            "anchor_date": str(bars[ai].get("date")) if ai is not None else None,
            "lookback_high": hi, "drawdown_pct": round(drawdown_pct, 2)}


def early_breakout_signal(history: list, today: dict) -> dict | None:
    """`history`: chronological bars up to the PREVIOUS session (dicts).
    `today`: {"date", "open" or "premarket_price", "volume", optional
    "bulk_block_deal": bool}. Fires on gap-up + institutional volume.
    The SMA 50/200 read is not consulted — by design. Pure."""
    if not history or not today:
        return None
    prev_close = _f(history[-1].get("close"))
    px = _f(today.get("open"))
    if px is None:
        px = _f(today.get("premarket_price"))
    if prev_close is None or prev_close <= 0 or px is None:
        return None
    gap_pct = (px - prev_close) / prev_close * 100.0
    if gap_pct < GAP_UP_MIN_PCT:
        return None
    vols = [_f(b.get("volume")) for b in history[-VOLUME_LOOKBACK:]]
    vols = [v for v in vols if v is not None and v > 0]
    avg_vol = sum(vols) / len(vols) if vols else None
    v_today = _f(today.get("volume"))
    spike = (avg_vol is not None and v_today is not None
             and avg_vol > 0 and v_today >= VOLUME_SPIKE_MULT * avg_vol)
    deal = bool(today.get("bulk_block_deal"))
    if not (spike or deal):
        return None
    return {"primitive": "early_breakout", "direction": "bullish",
            "date": str(today.get("date")), "spot": px,
            "triggers": [t for t, on in (("gap_up", True), ("volume_spike", spike),
                                         ("bulk_block_deal", deal)) if on],
            "gap_pct": round(gap_pct, 2), "prev_close": prev_close,
            "volume_mult": (round(v_today / avg_vol, 2)
                            if spike or (avg_vol and v_today) else None),
            "sma_bypassed": True}


# ============================================================ routing

def route_structure(primitive: str) -> tuple:
    """(structure, why). The ONLY map from a primitive to a structure."""
    s = STRUCTURES.get(primitive)
    if s is None:
        return None, f"no_route: unknown primitive {primitive!r}"
    return s, "ok"


def assert_defined_risk(spread: dict) -> tuple:
    """(ok, why). Rejects anything whose loss is not capped by construction:
    a bare option, an unhedged short leg, a non-finite max loss."""
    if not isinstance(spread, dict):
        return False, "naked_forbidden: no spread"
    if spread.get("strategy") not in DEFINED_RISK_STRUCTURES:
        return False, f"naked_forbidden: {spread.get('strategy')!r} is not a defined-risk structure"
    legs = spread.get("legs") or []
    if len(legs) < 2:
        return False, "naked_forbidden: fewer than two legs"
    for leg in legs:
        if str(leg.get("side", "")).upper() == "SELL":
            covered = any(str(o.get("side", "")).upper() == "BUY"
                          and o.get("option_type") == leg.get("option_type")
                          for o in legs)
            if not covered:
                return False, f"naked_forbidden: unhedged short {leg.get('option_type')}"
    ml = _f(spread.get("max_loss"))
    if ml is None or not math.isfinite(ml) or ml <= 0:
        return False, "naked_forbidden: max loss is not a finite positive number"
    return True, "ok"


def pyramid_plan(base_lots: int) -> dict:
    """The tranche ladder for one setup: add `base_lots` at each level of
    PYRAMID_LEVELS_R, total capped at PYRAMID_MAX_MULT × base."""
    base = max(1, int(base_lots))
    return {"levels": [{"at_r": r, "add_lots": base} for r in PYRAMID_LEVELS_R],
            "max_lots": base * PYRAMID_MAX_MULT}


def trailing_plan() -> dict:
    return {"atr_mult": TRAIL_ATR_MULT, "atr_n": TRAIL_ATR_N, "on": "underlying"}


# ============================================================ setup

def build_setup(signal: dict, symbol: str, buy_strike: float, sell_strike: float,
                buy_premium: float, sell_premium: float, lot_size: int,
                expiry: str, pool_rupees: float, vix: float = None,
                is_index: bool = False, fo: dict = None, fo_path=None,
                halt_fn=None) -> dict:
    """One signal → one setup dict, or a rejection with a named reason.
    Strikes/premiums are injected (the chain door is dhan_guard).
    `halt_fn(symbol) -> (ok, why)` is the corporate-risk halt seam; when
    None the setup says so (`halt_check: not_run`) rather than pretending."""
    primitive = signal.get("primitive")
    base = {"mode": MODE, "strategy": STRATEGY, "primitive": primitive,
            "symbol": symbol, "signal": signal, "date": signal.get("date")}
    structure, why = route_structure(primitive)
    if structure is None:
        return {**base, "accepted": False, "reason": why}
    base["structure"] = structure
    if not is_index:
        # the F&O file keys bare NSE names (RELIANCE); callers may pass the
        # option-underlying spelling (RELIANCE.NS)
        ok, why = fo_gate(str(symbol).split(".")[0], fo=fo, path=fo_path)
        if not ok:
            return {**base, "accepted": False, "reason": why}
    halt = "not_run"
    if halt_fn is not None:
        try:
            ok, why = halt_fn(symbol)
        except Exception as e:                       # fail CLOSED on the halt
            ok, why = False, f"halt_check_error: {type(e).__name__}: {e}"
        if not ok:
            return {**base, "accepted": False, "reason": f"corporate_risk_halt: {why}"}
        halt = "passed"
    from src.strategy import StrategyConstructor
    sc = StrategyConstructor(vix=vix, lot_size=lot_size)
    builder = getattr(sc, f"construct_{structure}", None)
    spread = builder(buy_strike, sell_strike, buy_premium, sell_premium) if builder else None
    if not spread:
        return {**base, "accepted": False, "reason": "spread_incoherent"}
    ok, why = assert_defined_risk(spread)
    if not ok:
        return {**base, "accepted": False, "reason": why}
    # decision #98: the same reward-to-risk floor the live proposer enforces
    # (1.5 for these directional debit spreads) — a shadow that would be
    # refused live must not be graded as if it could have traded.
    from src.strategy import reward_risk_gate
    rr_ok, rr, rr_floor, rr_why = reward_risk_gate(spread)
    if not rr_ok:
        return {**base, "accepted": False, "reason": rr_why,
                "reward_risk": rr, "reward_risk_floor": rr_floor}
    max_loss = float(spread["max_loss"])
    lots = lots_for(pool_rupees, max_loss, RISK_PCT_PER_SETUP)
    if lots < 1:
        return {**base, "accepted": False,
                "reason": f"risk_cap: one lot risks Rs.{max_loss:,.0f} > "
                          f"{RISK_PCT_PER_SETUP:.1%} of Rs.{float(pool_rupees):,.0f}"}
    spread = {**spread, "expiry": expiry, "lots": lots,
              "entry_spot": _f(signal.get("spot")),
              "risk_rupees": round(max_loss * lots, 2)}
    entry_day = date.fromisoformat(str(signal["date"]))
    exit_by = _sessions_after(entry_day, HOLD_SESSIONS)
    return {**base, "accepted": True, "reason": "ok", "halt_check": halt,
            "spread": spread, "lots": lots, "risk_rupees": spread["risk_rupees"],
            "risk_pct_of_pool": round(max_loss * lots / float(pool_rupees) * 100, 3),
            "plan": {"tranches": pyramid_plan(lots), "trailing": trailing_plan()},
            "time_exit_on": exit_by.isoformat(),
            "exit_rule": (f"ATR trail on the underlying, 65% profit-take, "
                          f"pre-expiry rule, or time exit at session +{HOLD_SESSIONS} "
                          f"({exit_by.isoformat()}), whichever first")}


def _sessions_after(d: date, n: int) -> date:
    out, left = d, n
    while left > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            left -= 1
    return out


def as_tracker_entry(setup: dict) -> dict:
    """The journal-SHAPED dict `plan_tracker`'s pure resolvers accept — used
    by the shadow grader only; never written to data/journal.jsonl."""
    return {"short_id": setup.get("ref") or f"gb:{setup['symbol']}:{setup['date']}",
            "date": setup["date"], "ticker": setup["symbol"], "action": "SPREAD",
            "decision": "approved", "outcome": None,
            "spread": setup["spread"], "plan": setup.get("plan")}


# ============================================================ the court

def court_definition(primitive: str) -> dict:
    """FROZEN predicate for the registry (its hash is the pattern id)."""
    rules = {"falling_knife": {"rsi_period": RSI_PERIOD, "rsi_oversold": RSI_OVERSOLD,
                               "lookback": KNIFE_LOOKBACK,
                               "min_drawdown_pct": KNIFE_MIN_DRAWDOWN_PCT,
                               "avwap_touch_pct": AVWAP_TOUCH_PCT},
             "early_breakout": {"gap_up_min_pct": GAP_UP_MIN_PCT,
                                "volume_lookback": VOLUME_LOOKBACK,
                                "volume_spike_mult": VOLUME_SPIKE_MULT,
                                "sma_bypassed": True}}
    return {"strategy": STRATEGY, "primitive": primitive,
            "structure": STRUCTURES.get(primitive),
            "risk_pct_per_setup": RISK_PCT_PER_SETUP,
            "pyramid_levels_r": list(PYRAMID_LEVELS_R),
            "trail_atr_mult": TRAIL_ATR_MULT, "rules": rules.get(primitive, {})}


def enrol_in_court(conn, primitive: str) -> str:
    """Register the primitive (idempotent) and open its TRIAL. Returns the
    pattern id. A DEAD or QUARANTINED row stays as it is — dead ends stay
    dead (the lineage rule)."""
    from src.validation import registry as rg
    reg = rg.register(conn, KIND, court_definition(primitive),
                      description=f"{STRATEGY}.{primitive} -> {STRUCTURES.get(primitive)} (shadow, M4A)",
                      mining_run="glassbreaking_m4a_2026-09-11")
    pid = reg["pattern_id"]
    if reg["status"] == "CANDIDATE":
        rg.transition(conn, pid, "TRIAL", "M4A shadow trial opened")
    return pid


def record_fire(conn, setup: dict) -> str:
    from src.validation import trial
    pid = enrol_in_court(conn, setup["primitive"])
    ref = trial.record_shadow_fire(conn, pid, setup["date"], setup["symbol"],
                                   direction=(setup.get("signal") or {}).get("direction"))
    return ref["ref"]


def resolve_fire(conn, ref: str, outcome: dict) -> bool:
    from src.validation import trial
    r = outcome.get("r_multiple")
    result = "scratch" if r is None or abs(r) < 1e-9 else ("win" if r > 0 else "loss")
    return trial.resolve_shadow(conn, ref, result, float(r or 0.0),
                                str(outcome.get("exit_date")))


def court_scorecard(conn, primitive: str, windows: dict = None) -> dict:
    """n / wins / one-sided 95% Wilson lower bound for one primitive, plus
    the verdict the bar implies. Reads only."""
    from src.validation import registry as rg, trial
    from src.validation import stat_gates as sg
    pid = rg.pattern_id_for(court_definition(primitive))
    ev = trial.shadow_evidence(conn, pid, windows)
    null_rate = sg.breakeven_win_rate(COURT_AVG_WIN_R, COURT_AVG_LOSS_R)
    min_res = sg.configured_floors()["min_resolutions"]
    v = sg.promotable(ev["wins"], ev["n"], 0, 0, null_rate=null_rate,
                      min_resolutions=min_res)
    status = (rg.get(conn, pid) or {}).get("status")
    return {"primitive": primitive, "pattern_id": pid, "status": status,
            "n": ev["n"], "wins": ev["wins"],
            "wilson_lb": round(float(v.get("wilson_lb") or 0.0), 4),
            "null_rate": round(null_rate, 4), "min_n": min_res,
            "promote": bool(v.get("promote")), "reason": v.get("reason")}


# ============================================================ run + grade

def run(day: str, candidates: list, pool_rupees: float = 200_000.0,
        vix: float = None, conn=None, ledger_path=None, fo: dict = None,
        halt_fn=None) -> list:
    """One shadow pass. `candidates`: [{"symbol", "bars", "today"?, "chain":
    {"buy_strike","sell_strike","buy_premium","sell_premium","lot_size",
    "expiry"}, "is_index"?}]. Every accepted AND rejected setup is
    appended to the ledger; accepted ones are enrolled in the court when
    a brain_map connection is given. Never raises for one bad candidate."""
    out = []
    for c in candidates:
        try:
            sym = c["symbol"]
            bars = c.get("bars") or []
            sig = falling_knife_signal(bars)
            if sig is None and c.get("today"):
                # history must END the session before "today": when the
                # feed already includes today's bar, drop it.
                hist = bars[:-1] if (bars and str(bars[-1].get("date")) ==
                                     str(c["today"].get("date"))) else bars
                sig = early_breakout_signal(hist, c["today"])
            if sig is None:
                continue
            sig.setdefault("date", day)
            ch = c.get("chain") or {}
            if not ch:
                _append(ledger_path or SHADOW_LEDGER,
                        {"mode": MODE, "strategy": STRATEGY, "run_day": day, "symbol": sym,
                         "primitive": sig.get("primitive"), "signal": sig, "accepted": False,
                         "reason": "no_chain_for_underlying"})
                out.append({"mode": MODE, "symbol": sym, "primitive": sig.get("primitive"),
                            "accepted": False, "reason": "no_chain_for_underlying"})
                continue
            setup = build_setup(sig, sym, ch.get("buy_strike"), ch.get("sell_strike"),
                                ch.get("buy_premium"), ch.get("sell_premium"),
                                int(ch.get("lot_size") or 0), ch.get("expiry"),
                                pool_rupees, vix=vix, is_index=bool(c.get("is_index")),
                                fo=fo, halt_fn=halt_fn)
            setup["run_day"] = day
            if setup["accepted"] and conn is not None:
                setup["ref"] = record_fire(conn, setup)
            _append(ledger_path or SHADOW_LEDGER, setup)
            out.append(setup)
        except Exception as e:
            _append(ledger_path or SHADOW_LEDGER,
                    {"mode": MODE, "strategy": STRATEGY, "run_day": day,
                     "symbol": c.get("symbol") if isinstance(c, dict) else None,
                     "accepted": False, "reason": f"error: {type(e).__name__}: {e}"})
    return out


def grade(setups: list, bars_fn, today: date = None, conn=None,
          ledger_path=None) -> list:
    """Resolve accepted shadow setups with the live tracker's pure resolvers
    (`plan_tracker._resolve_spread_trailed` — the trail-aware variant the
    live sweep does NOT use — + the expiry backstop + tranche
    observation), never the journal. `bars_fn(symbol, start_iso)`
    returns (day, low, high, close) tuples. Returns outcome rows; each is
    appended to the ledger and, with `conn`, resolves the court fire."""
    from src import plan_tracker as pt
    today = today or date.today()
    out = []
    for s in setups:
        if not s.get("accepted") or s.get("outcome"):
            continue
        entry = as_tracker_entry(s)
        try:
            bars = bars_fn(s["symbol"], s["date"]) or []
        except Exception:
            bars = []
        hit = pt._resolve_spread_trailed(entry, bars) if bars else None
        if hit is None or hit[3] > entry["spread"]["expiry"]:
            bs = pt._expiry_backstop(entry, bars, today)
            hit = bs[:4] if bs else None
        if hit is None:
            # time exit: last bar on/after time_exit_on
            late = [b for b in bars if b[0] >= s.get("time_exit_on", "9999")]
            if late:
                day, _l, _h, close = late[0]
                r = pt._spread_r_path(entry, bars, day)
                mark = r[-1][2] if r else pt._spread_entry_mark(entry["spread"])
                hit = ("time_stop", mark, 0.0, day)
        if hit is None:
            continue
        resolution, m_exit, _frac, exit_day = hit
        spread = entry["spread"]
        qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
        gross = (m_exit - pt._spread_entry_mark(spread)) * qty
        max_loss_total = float(spread["max_loss"]) * int(spread.get("lots", 1))
        outcome = {"resolution": resolution, "exit_date": exit_day,
                   "price": round(m_exit, 2), "pnl_rs": round(gross, 2),
                   "r_multiple": round(gross / max_loss_total, 2) if max_loss_total else None,
                   "checked": today.isoformat(), "hypothetical": True,
                   "costs": "not modelled in the shadow grader"}
        tr = pt.observe_tranches(entry, bars, exit_day, m_exit,
                                 base_lots=int(spread.get("lots", 1)),
                                 unit=int(spread["lot_size"]))
        if tr is not None:
            outcome["tranches"] = tr
        row = {"mode": MODE, "strategy": STRATEGY, "event": "outcome",
               "primitive": s.get("primitive"), "symbol": s["symbol"],
               "date": s["date"], "ref": s.get("ref"), "outcome": outcome}
        if conn is not None and s.get("ref"):
            row["court_resolved"] = resolve_fire(conn, s["ref"], outcome)
        s["outcome"] = outcome                 # so a re-run skips it
        _append(ledger_path or SHADOW_LEDGER, row)
        out.append(row)
    return out


# ============================================================ ledger

def read_ledger(path=None) -> list:
    p = Path(path or SHADOW_LEDGER)
    rows = []
    if not p.is_file():
        return rows
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def _append(path, row: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(dict(row, written_at=datetime.now(IST).isoformat(
            timespec="seconds")), default=str) + "\n")
