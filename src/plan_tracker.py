"""
Alpha Trading — Phase 4C: the plan tracker
==========================================

Watches every journaled 4B plan (approved OR rejected) and resolves it
against what prices actually did, day by day:

  stop hit    -> the plan's stop-loss price traded; approved positions are
                 closed on paper at the stop, exactly as the approved plan
                 said they would be (a bracket order, in effect)
  target hit  -> the take-profit price traded; approved positions are closed
                 at the target
  time stop   -> neither hit within PLAN_MAX_DAYS; the plan is closed out
                 at the latest close so nothing dangles forever

Rejected plans resolve by the same rules but hypothetically (no portfolio
touch) — that's how we learn whether a skip was smart.

Outcomes land in the same journal `outcome` field review.py uses, with real
plan metrics: exit price/date, %, R-multiple, days in trade, rupee P&L.
Entries WITHOUT a stop-carrying plan (pre-4B entries, sell/exit decisions)
are not touched here — src/review.py keeps scoring those as before.

Phase 6 core loop: the moment a plan resolves, the tracker also (a) asks
the post-mortem analyst (src/analyst.py, Gemini) to compare the original
plan with what actually happened, and (b) writes the outcome + its
signal/pattern events + that post-mortem into the Brain Map
(data/brain_map.db), keyed by the entry's journal short_id. Both steps
are strictly fail-safe: no Gemini key, no network, or any Brain Map error
just prints a note — the journal outcome above is already saved and is
never blocked by the memory write.

Runs automatically at the start of every trade session, or manually:

    python3 -m src.plan_tracker

Same-day ambiguity is resolved pessimistically: if a day's range covers both
the stop and the target, we assume the stop hit first. The entry day itself
is never scanned (the trade happened near that day's close; its intraday low
usually predates the entry).
"""

from datetime import date

from src import analyst
from src import brain_map
from src import journal
from src import portfolio as pf
from src.config import PLAN_MAX_DAYS
from src.dhan_client import get_ohlc_since
from src.notifier import send_digest
from src.review import MOVE_THRESHOLD


def _get_instrument_type(ticker: str) -> str:
    """Infer the instrument type from the ticker symbol."""
    t_upper = ticker.upper()
    if t_upper.endswith(".NS") or t_upper.endswith(".BO"):
        return "STOCK"
    if "NIFTY" in t_upper or "BANK" in t_upper or t_upper.startswith("^"):
        if t_upper.endswith("CE") or t_upper.endswith("PE"):
            return "OPTION"
        return "INDEX"
    if t_upper.endswith("CE") or t_upper.endswith("PE"):
        return "OPTION"
    return "STOCK"


def apply_slippage(price: float, instrument_type: str) -> float:
    """Calculate the bid-ask slippage amount based on instrument type and liquidity."""
    instr_upper = instrument_type.upper()
    if instr_upper == "INDEX":
        return price * 0.0005  # 0.05%
    elif instr_upper == "OPTION":
        # Liquidity dummy lookup: lower premium option is assumed less liquid
        if price < 50:
            return price * 0.0050  # 0.50% (OTM/Illiquid option)
        elif price < 150:
            return price * 0.0030  # 0.30%
        else:
            return price * 0.0010  # 0.10% (Liquid/Near ATM option)
    return 0.0  # STOCK trades have 0.0% slippage under this rule


# --- Phase 5: options spread tracking ---------------------------------
# Exit discipline for defined-risk spreads (DECISIONS.md #27):
#   * auto-exit at OPTION_PROFIT_TAKE_FRACTION of max profit (the 60-70%
#     band) — late-cycle theta slows and gamma risk explodes, so the last
#     30-40% of profit is never worth chasing;
#   * NEVER hold into the last PRE_EXPIRY_EXIT_DAYS days before expiry;
#   * every exit is an ATOMIC BASKET: all legs close together in one
#     action. Closing legs sequentially (e.g. the protective long first)
#     would leave a naked short and spike SPAN margin 200-500% — the
#     tracker structurally cannot do that, there is no per-leg exit path.
OPTION_PROFIT_TAKE_FRACTION = 0.65
PRE_EXPIRY_EXIT_DAYS = 2


def _spread_trackable(entry: dict) -> bool:
    """Journal entries carrying a Phase 5 `spread` dict (strategy, legs,
    lot_size, lots, expiry, max_loss/max_profit — as built by
    strategy.StrategyConstructor) that haven't resolved yet."""
    s = entry.get("spread")
    return entry.get("outcome") is None and bool(
        s and s.get("legs") and s.get("expiry"))


def _leg_intrinsic(leg: dict, spot: float) -> float:
    if leg["option_type"] == "CE":
        return max(0.0, spot - leg["strike"])
    return max(0.0, leg["strike"] - spot)


def _leg_model_premium(leg: dict, spot: float, entry_spot, frac_left: float) -> float:
    """Offline mark for one leg: intrinsic at the current spot plus the
    leg's entry time-value decaying linearly to zero at expiry. A model,
    not a market quote — good enough for paper exit discipline, fully
    deterministic for tests. entry_spot None assumes legs entered OTM
    (time value = full entry premium)."""
    if entry_spot is None:
        tv_entry = leg["premium"]
    else:
        tv_entry = max(0.0, leg["premium"] - _leg_intrinsic(leg, entry_spot))
    return _leg_intrinsic(leg, spot) + tv_entry * frac_left


def _spread_mark(spread: dict, spot: float, frac_left: float) -> float:
    """Signed basket value per share (long legs +, short legs -). The
    P&L per share at any moment is mark_now - mark_at_entry."""
    entry_spot = spread.get("entry_spot")
    mark = 0.0
    for leg in spread["legs"]:
        sign = 1.0 if leg["side"].upper() == "BUY" else -1.0
        mark += sign * _leg_model_premium(leg, spot, entry_spot, frac_left)
    return mark


def _spread_entry_mark(spread: dict) -> float:
    return sum((1.0 if l["side"].upper() == "BUY" else -1.0) * l["premium"]
               for l in spread["legs"])


def _resolve_spread(entry: dict, bars: list):
    """(resolution, exit_mark_per_share, frac_left_at_exit, exit_date)
    once an exit trigger fires, else None while the spread is live.
    Triggers, checked on each daily close after the entry day:
      profit_take       modeled profit >= 65% of the structure's max profit
      pre_expiry_exit   PRE_EXPIRY_EXIT_DAYS or fewer days to expiry
    Defined-risk structures need no stop trigger: max loss is capped by
    construction and realized, at worst, at the pre-expiry exit."""
    spread = entry["spread"]
    expiry = date.fromisoformat(spread["expiry"])
    entry_day = date.fromisoformat(entry["date"])
    total_days = max(1, (expiry - entry_day).days)
    m_entry = _spread_entry_mark(spread)
    lot = int(spread["lot_size"])
    max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
    max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0

    for day, _low, _high, close in bars:
        if day <= entry["date"]:
            continue  # same convention as equity: never scan the entry day
        d = date.fromisoformat(day)
        days_left = (expiry - d).days
        frac_left = max(0.0, days_left / total_days)
        m_now = _spread_mark(spread, float(close), frac_left)
        # Clamp to the structure's no-arbitrage bounds: a defined-risk
        # spread can never lose more than max_loss or make more than
        # max_profit — the linear time-value model must not either.
        profit_ps = max(-max_loss_ps, min(m_now - m_entry, max_profit_ps))
        m_now = m_entry + profit_ps
        if max_profit_ps > 0 and profit_ps >= OPTION_PROFIT_TAKE_FRACTION * max_profit_ps:
            return "profit_take", m_now, frac_left, day
        if days_left <= PRE_EXPIRY_EXIT_DAYS:
            return "pre_expiry_exit", m_now, frac_left, day
    return None


def _spread_exit_costs(spread: dict, spot_exit: float, frac_left: float) -> tuple:
    """(total_frictions, total_slippage) across ALL legs, both ends of the
    trade, priced per leg: entry legs pay their side's 2026 frictions on
    the entry premium; the atomic basket exit flips each side (short legs
    are bought back -> stamp duty, long legs are sold -> STT) on the
    modeled exit premium. Slippage per leg uses the OPTION liquidity
    ladder (0.10%-0.50%, cheap OTM legs get the worst fill)."""
    qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
    entry_spot = spread.get("entry_spot")
    frictions = slippage = 0.0
    for leg in spread["legs"]:
        entry_side = leg["side"].upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        exit_premium = _leg_model_premium(leg, spot_exit, entry_spot, frac_left)
        frictions += pf.calculate_trade_frictions("OPTION", entry_side, leg["premium"], qty)
        frictions += pf.calculate_trade_frictions("OPTION", exit_side, exit_premium, qty)
        slippage += apply_slippage(leg["premium"], "OPTION") * qty
        slippage += apply_slippage(exit_premium, "OPTION") * qty
    return frictions, slippage


def _settle_spread_cash(pnl_net: float) -> bool:
    """Net-settle a resolved approved spread against paper cash (entry
    premiums and exit values collapse to one net P&L figure — margin was
    only ever virtually blocked, never deducted)."""
    book = pf.load()
    book["cash"] = round(book["cash"] + pnl_net, 2)
    pf.save(book)
    return True


def _spread_verdict(entry: dict, resolution: str, pnl_net: float, capture_pct: float) -> str:
    approved = entry["decision"] == "approved"
    if resolution == "profit_take":
        return (f"WIN — auto-exit at {capture_pct:.0f}% of max profit (gamma discipline)"
                if approved else
                f"MISSED GAIN — it reached {capture_pct:.0f}% of max profit without you")
    if pnl_net > 0:
        return ("WIN — closed ahead at the pre-expiry exit" if approved
                else "MISSED GAIN — it closed ahead without you")
    if pnl_net < 0:
        return ("LOSS — closed behind at the pre-expiry exit" if approved
                else "GOOD SKIP — it closed behind")
    return "flat (pre-expiry exit, went nowhere)"


def _daily_bars(ticker: str, start_iso: str):
    """[(date_iso, low, high, close), ...] since start_iso.

    Uses Dhan's real daily OHLC so stop/target hits resolve on the true
    intraday low/high of each session (migrated off yfinance 2026-07-06) —
    NOT a naive last-price check. Dhan returns clean trading-day bars only."""
    return [
        (bar["date"], bar["low"], bar["high"], bar["close"])
        for bar in get_ohlc_since(ticker, start_iso)
    ]


def _trackable(entry: dict) -> bool:
    plan = entry.get("plan")
    return entry["outcome"] is None and bool(plan and plan.get("stop_loss"))


def _resolve(entry: dict, bars: list):
    """(resolution, exit_price, exit_date) once the plan has resolved,
    else None while it is still live."""
    stop = entry["plan"]["stop_loss"]["price"]
    target = entry["plan"]["target"]["price"]
    for day, low, high, _close in bars:
        if day <= entry["date"]:
            continue  # never scan the entry day itself
        if low <= stop:
            return "stop_hit", stop, day
        if high >= target:
            return "target_hit", target, day
    age = (date.today() - date.fromisoformat(entry["date"])).days
    if bars and age >= PLAN_MAX_DAYS:
        day, _low, _high, close = bars[-1]
        return "time_stop", round(float(close), 2), day
    return None


def _verdict(entry: dict, resolution: str, pct: float) -> str:
    approved = entry["decision"] == "approved"
    if resolution == "target_hit":
        return ("WIN — target hit" if approved
                else "MISSED GAIN — it hit the target without you")
    if resolution == "stop_hit":
        return ("LOSS — stop hit" if approved
                else "GOOD SKIP — it would have hit the stop")
    # time stop: score the drift, using review.py's same flat threshold
    if pct >= MOVE_THRESHOLD:
        return ("WIN — time stop, closed ahead" if approved
                else "MISSED GAIN — it drifted up without you")
    if pct <= -MOVE_THRESHOLD:
        return ("LOSS — time stop, closed behind" if approved
                else "GOOD SKIP — it drifted down")
    return "flat (time stop, went nowhere)"


def _close_paper_position(entry: dict, exit_price: float, instrument_type: str = "stock") -> bool:
    """Close the tracked holding at the plan's exit price. Returns False if
    the position was already closed some other way (e.g. a Death Cross sell
    the user approved in a session) — the outcome still gets recorded."""
    book = pf.load()
    if entry["ticker"] not in book["holdings"]:
        return False
    pf.sell(book, entry["ticker"], exit_price, instrument_type=instrument_type)
    pf.save(book)
    return True


# Patchable seam for tests (point it at a temp DB) — production always
# opens the real data/brain_map.db.
_brain_connect = brain_map.connect


def _post_mortem_payloads(entry: dict) -> tuple:
    """(initial_plan, actual_execution) for the analyst — captured at the
    exact moment of resolution. initial_plan is the forecasting thesis we
    journaled at entry (signal, user reasoning, pattern tags, the full 4B
    plan JSON); actual_execution is what the market really did (the
    trigger that fired plus the realized metrics)."""
    o = entry["outcome"]
    initial_plan = {
        "date": entry["date"],
        "ticker": entry["ticker"],
        "action": entry["action"],
        "thesis_signal": entry.get("signal"),
        "user_reasoning": entry.get("why"),
        "pattern_tags": entry.get("pattern_tags") or [],
        "plan": entry.get("plan"),
        "entry_price": entry.get("price"),
        "shares": entry.get("shares"),
        "spread": entry.get("spread"),  # present for Phase 5 options entries
    }
    actual_execution = {
        "trigger": o["resolution"],  # stop_hit / target_hit / time_stop / profit_take / pre_expiry_exit
        "entry_price": entry["price"],
        "exit_price": o["price"],
        "exit_date": o["exit_date"],
        "pct_change": o["pct"],
        "r_multiple": o["r_multiple"],
        "days_in_trade": o["days_in_trade"],
        "pnl_rs": o["pnl_rs"],
        "verdict": o["verdict"],
        "hypothetical": o.get("hypothetical", False),
    }
    return initial_plan, actual_execution


def record_post_mortem(entry: dict, brain) -> None:
    """Phase 6 core loop, per resolved entry: generate the analyst's
    post-mortem (None is fine — the trade is recorded either way) and
    write outcome + events + post-mortem to the Brain Map, keyed by the
    entry's short_id via brain_map.journal_ref_for."""
    initial_plan, actual_execution = _post_mortem_payloads(entry)
    post_mortem = analyst.generate_post_mortem(initial_plan, actual_execution)
    brain_map.record_resolved_entry(brain, entry, post_mortem=post_mortem)


def _fmt_signed(value, spec: str = "+.1f", suffix: str = "") -> str:
    """None-safe numeric formatting for digest lines. A hypothetical
    (rejected/pending) resolution can legitimately carry r_multiple=None;
    formatting None with +.1f raises — and a digest-line crash used to
    abort the WHOLE sweep before outcomes were persisted, replaying the
    same resolutions (and Discord cards) every hour. Found live 2026-07-09."""
    return f"{value:{spec}}{suffix}" if isinstance(value, (int, float)) else "n/a"


def _outcome_line(entry: dict) -> str:
    o = entry["outcome"]
    label = {"stop_hit": "STOPPED OUT", "target_hit": "TARGET HIT",
             "time_stop": "TIME STOP"}[o["resolution"]]
    if entry["decision"] == "approved":
        head = (f"{label}: {entry['ticker']} bought {entry['date']} at "
                f"Rs.{entry['price']:,.2f} exited at Rs.{o['price']:,.2f} "
                f"on {o['exit_date']} — Rs.{o['pnl_rs']:+,.2f} "
                f"({_fmt_signed(o['pct'], '+.1f', '%')}, "
                f"{_fmt_signed(o['r_multiple'], '+.1f', 'R')}), "
                f"{o['days_in_trade']} day(s) in trade.")
        if not o["position_closed"]:
            head += " (Position was already closed earlier — outcome recorded for scoring only.)"
    else:
        head = (f"{label} (you skipped this one): {entry['ticker']} plan from "
                f"{entry['date']} would have exited at Rs.{o['price']:,.2f} "
                f"on {o['exit_date']} ({_fmt_signed(o['pct'], '+.1f', '%')}, "
                f"{_fmt_signed(o['r_multiple'], '+.1f', 'R')}).")
    return (f"{head}\n   Verdict: {o['verdict']}\n"
            f"   Engine's reason at the time: {entry['signal']}\n"
            f"   Your reason at the time: {entry['why']}")


def _spread_outcome_line(entry: dict) -> str:
    o = entry["outcome"]
    s = entry["spread"]
    label = {"profit_take": "PROFIT TAKE (65% of max)",
             "pre_expiry_exit": "PRE-EXPIRY EXIT (2-day gamma rule)"}[o["resolution"]]
    head = (f"{label}: {s['strategy']} on {entry['ticker']} from {entry['date']} "
            f"closed atomically (all {len(s['legs'])} legs together) on "
            f"{o['exit_date']} — net Rs.{o['pnl_rs']:+,.2f} "
            f"({_fmt_signed(o['r_multiple'], '+.2f', 'R')} vs max loss), "
            f"{o['days_in_trade']} day(s) in trade.")
    if entry["decision"] != "approved":
        head = f"{label} (you skipped this one): " + head.split(": ", 1)[1]
    return (f"{head}\n   Verdict: {o['verdict']}\n"
            f"   Frictions Rs.{o['frictions_rs']:,.2f} + slippage "
            f"Rs.{o['slippage_rs']:,.2f} already deducted from that P&L.")


def run_tracker(email: bool = True, on_episode=None) -> int:
    """Sweep all open plans; returns how many resolved this run.

    `on_episode`, when given, is called once per resolution with the
    entry's "Trade Episode" context snapshot (brain_map.build_episode_snapshot)
    — how src/api.py's async loop forwards resolutions to Discord without
    this sync module doing any network I/O itself. Fail-safe: a callback
    error never blocks resolution."""
    entries = journal.read_all()
    open_plans = [e for e in entries if _trackable(e)]
    open_spreads = [e for e in entries if _spread_trackable(e)]
    if not open_plans and not open_spreads:
        print("Plan tracker: no open plans to check.")
        return 0

    # One Brain Map connection for the whole sweep. Optional by design:
    # if it can't open, plans still resolve and journal normally.
    try:
        brain = _brain_connect()
    except Exception as e:
        print(f"Plan tracker: Brain Map unavailable ({e}) — resolving without memory writes.")
        brain = None

    resolved_lines, resolved = [], 0
    for entry in open_plans:
        bars = _daily_bars(entry["ticker"], entry["date"])
        if not bars:
            print(f"Plan tracker: no price data for {entry['ticker']} — will retry next run.")
            continue
        hit = _resolve(entry, bars)
        stop_price = entry["plan"]["stop_loss"]["price"]
        if hit is None:
            _day, low, high, close = bars[-1]
            print(f"Plan tracker: {entry['ticker']} still live "
                  f"(now Rs.{close:,.2f}; stop Rs.{stop_price:,.2f}, "
                  f"target Rs.{entry['plan']['target']['price']:,.2f}).")
            continue

        resolution, exit_price, exit_day = hit
        pct = (exit_price - entry["price"]) / entry["price"] * 100
        risk_per_share = entry["price"] - stop_price
        approved = entry["decision"] == "approved"

        # Infer instrument type and apply trade frictions and slippage
        instrument_type = _get_instrument_type(entry["ticker"])
        closed = _close_paper_position(entry, exit_price, instrument_type) if approved else False

        entry_frictions = pf.calculate_trade_frictions(instrument_type, "BUY", entry["price"], entry["shares"])
        exit_frictions = pf.calculate_trade_frictions(instrument_type, "SELL", exit_price, entry["shares"])
        total_frictions = entry_frictions + exit_frictions

        entry_slippage = apply_slippage(entry["price"], instrument_type) * entry["shares"]
        exit_slippage = apply_slippage(exit_price, instrument_type) * entry["shares"]
        total_slippage = entry_slippage + exit_slippage

        gross_pnl = entry["shares"] * (exit_price - entry["price"])
        net_pnl_rs = round(gross_pnl - total_frictions - total_slippage, 2)

        entry["outcome"] = {
            "checked": date.today().isoformat(),
            "resolution": resolution,
            "price": exit_price,
            "exit_date": exit_day,
            "pct": round(pct, 2),
            "r_multiple": round((exit_price - entry["price"]) / risk_per_share, 2)
                          if risk_per_share > 0 else None,
            "days_in_trade": (date.fromisoformat(exit_day)
                              - date.fromisoformat(entry["date"])).days,
            "pnl_rs": net_pnl_rs,
            "frictions_rs": round(total_frictions, 2),
            "slippage_rs": round(total_slippage, 2),
            "hypothetical": not approved,
            "position_closed": closed,
            "verdict": _verdict(entry, resolution, pct),
        }
        # Persist THIS resolution immediately — a crash anywhere later in
        # the sweep (digest formatting, another entry, email) must never
        # un-resolve it. Before this line existed, one such crash replayed
        # every resolution (and its Discord card) hourly (2026-07-09).
        journal.rewrite_all(entries)
        resolved += 1
        resolved_lines.append(_outcome_line(entry))
        print(f"Plan tracker: resolved {entry['ticker']} — {entry['outcome']['verdict']}")

        # Broadcast embed alert for this resolution (fail-safe — journal
        # outcome is already written above; Discord outage cannot block it).
        try:
            from src.notifier import fire_broadcast
            fire_broadcast({
                "event": "stop_loss" if resolution == "stop_hit" else "closed",
                "ticker": entry["ticker"],
                "date": exit_day,
                "short_id": entry.get("short_id"),
                "resolution": resolution,
                "pnl_rs": net_pnl_rs,
                "r_multiple": entry["outcome"]["r_multiple"],
                "verdict": entry["outcome"]["verdict"],
                "days_in_trade": entry["outcome"]["days_in_trade"],
                "frictions_rs": round(total_frictions, 2),
            })
        except Exception as _bcast_err:
            print(f"  (plan_tracker: broadcast alert skipped: {_bcast_err})")

        # Phase 6 core loop: post-mortem + Brain Map write. Fail-safe —
        # the journal outcome above is already set and never blocked.
        if brain is not None:
            try:
                record_post_mortem(entry, brain)
                print(f"Plan tracker: {entry['ticker']} outcome recorded in the Brain Map.")
            except Exception as e:
                print(f"Plan tracker: Brain Map write failed for {entry['ticker']} "
                      f"({e}) — outcome still journaled.")

        # Episodic encoding hand-off: build the context snapshot and give
        # it to the caller (the API loop sends it to Discord). Fail-safe.
        if on_episode is not None:
            try:
                episode = brain_map.build_episode_snapshot(entry)
                if episode:
                    on_episode(episode)
            except Exception as e:
                print(f"Plan tracker: episode capture failed for {entry['ticker']} ({e}).")

    # ---- Phase 5: options spread sweep (atomic basket exits) ----------
    for entry in open_spreads:
        spread = entry["spread"]
        bars = _daily_bars(entry["ticker"], entry["date"])
        if not bars:
            print(f"Plan tracker: no price data for {entry['ticker']} spread — will retry next run.")
            continue
        hit = _resolve_spread(entry, bars)
        if hit is None:
            print(f"Plan tracker: {spread['strategy']} on {entry['ticker']} still live "
                  f"(expiry {spread['expiry']}).")
            continue

        resolution, m_exit, frac_left, exit_day = hit
        approved = entry["decision"] == "approved"
        qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
        m_entry = _spread_entry_mark(spread)
        gross_pnl = (m_exit - m_entry) * qty

        _day, _low, _high, exit_close = bars[[b[0] for b in bars].index(exit_day)]
        total_frictions, total_slippage = _spread_exit_costs(spread, float(exit_close), frac_left)
        pnl_net = round(gross_pnl - total_frictions - total_slippage, 2)

        max_profit_total = float(spread["max_profit"]) * int(spread.get("lots", 1))
        max_loss_total = float(spread["max_loss"]) * int(spread.get("lots", 1))
        capture_pct = (gross_pnl / max_profit_total * 100) if max_profit_total > 0 else 0.0

        settled = _settle_spread_cash(pnl_net) if approved else False
        # Phase 6G: release the capital layer's margin lock (safe no-op if
        # this entry never passed through the gate). Hypothetical trades
        # never consumed real capital, so they settle at zero P&L.
        from src import portfolio_manager as pm
        pm.release_entry(entry.get("short_id", ""),
                         pnl_net if approved else 0.0)

        entry["outcome"] = {
            "checked": date.today().isoformat(),
            "resolution": resolution,
            "price": round(m_exit, 2),          # basket mark per share at exit
            "exit_date": exit_day,
            "pct": round(capture_pct, 2),        # % of max profit captured
            "r_multiple": round(pnl_net / max_loss_total, 2) if max_loss_total > 0 else None,
            "days_in_trade": (date.fromisoformat(exit_day)
                              - date.fromisoformat(entry["date"])).days,
            "pnl_rs": pnl_net,
            "frictions_rs": round(total_frictions, 2),
            "slippage_rs": round(total_slippage, 2),
            "exit_style": "atomic_basket",       # all legs closed together, always
            "hypothetical": not approved,
            "position_closed": settled,
            "verdict": _spread_verdict(entry, resolution, pnl_net, capture_pct),
        }
        # Same immediate-persistence rule as the equity sweep above.
        journal.rewrite_all(entries)
        resolved += 1
        resolved_lines.append(_spread_outcome_line(entry))
        print(f"Plan tracker: resolved {spread['strategy']} on {entry['ticker']} "
              f"— {entry['outcome']['verdict']}")

        # Broadcast embed alert for this spread resolution (fail-safe).
        try:
            from src.notifier import fire_broadcast
            fire_broadcast({
                "event": "closed",
                "ticker": entry["ticker"],
                "date": exit_day,
                "strategy": spread.get("strategy"),
                "short_id": entry.get("short_id"),
                "resolution": resolution,
                "pnl_rs": pnl_net,
                "r_multiple": entry["outcome"]["r_multiple"],
                "verdict": entry["outcome"]["verdict"],
                "days_in_trade": entry["outcome"]["days_in_trade"],
                "frictions_rs": round(total_frictions, 2),
            })
        except Exception as _bcast_err:
            print(f"  (plan_tracker: broadcast alert skipped: {_bcast_err})")

        if brain is not None:
            try:
                record_post_mortem(entry, brain)
                print(f"Plan tracker: {entry['ticker']} spread outcome recorded in the Brain Map.")
            except Exception as e:
                print(f"Plan tracker: Brain Map write failed for {entry['ticker']} "
                      f"({e}) — outcome still journaled.")

        if on_episode is not None:
            try:
                episode = brain_map.build_episode_snapshot(entry)
                if episode:
                    on_episode(episode)
            except Exception as e:
                print(f"Plan tracker: episode capture failed for {entry['ticker']} ({e}).")

    if brain is not None:
        brain.close()
    if resolved:
        journal.rewrite_all(entries)
        if email:
            send_digest("Paper Trading: plans resolved", resolved_lines)
    return resolved


def run_mock_trade(strategy_name: str = "IRON_BUTTERFLY") -> bool:
    """Discord connectivity dry run: build a SYNTHETIC resolved spread
    episode and push it through the exact notifier path the API loop uses.
    Touches NOTHING real — no journal, no portfolio, no Brain Map writes.
    Returns True only if Discord actually accepted the message."""
    import asyncio
    from src import notifier

    name = (strategy_name or "IRON_BUTTERFLY").strip().lower()
    if "condor" in name:
        strategy, legs = "iron_condor", [
            {"side": "SELL", "option_type": "PE", "strike": 24800, "premium": 95},
            {"side": "BUY",  "option_type": "PE", "strike": 24600, "premium": 38},
            {"side": "SELL", "option_type": "CE", "strike": 25200, "premium": 100},
            {"side": "BUY",  "option_type": "CE", "strike": 25400, "premium": 40},
        ]
    else:
        strategy, legs = "iron_butterfly", [
            {"side": "SELL", "option_type": "CE", "strike": 25000, "premium": 180},
            {"side": "SELL", "option_type": "PE", "strike": 25000, "premium": 175},
            {"side": "BUY",  "option_type": "CE", "strike": 25300, "premium": 60},
            {"side": "BUY",  "option_type": "PE", "strike": 24700, "premium": 55},
        ]

    credit = (sum(l["premium"] for l in legs if l["side"] == "SELL")
              - sum(l["premium"] for l in legs if l["side"] == "BUY"))
    mock_entry = {
        "short_id": "mock0000", "date": date.today().isoformat(),
        "ticker": "NIFTY 50", "action": "SPREAD", "decision": "approved",
        "price": round(credit, 2),  # net credit/share at entry
        "signal": f"[MOCK] {strategy} connectivity test", "why": "dry run",
        "pattern_tags": [strategy],
        "spread": {"strategy": strategy, "legs": legs, "lot_size": 75, "lots": 1},
        "outcome": {"resolution": "profit_take",
                    "price": round(credit * 0.35, 2),  # buyback mark at 65% profit
                    "exit_date": date.today().isoformat(), "pct": 65.0,
                    "r_multiple": 0.85,
                    "pnl_rs": round(credit * 0.65 * 75, 2), "verdict":
                    "WIN — auto-exit at 65% of max profit (gamma discipline)"},
    }
    episode = brain_map.build_episode_snapshot(mock_entry, news={})
    message = ("🧪 **MOCK TRADE — connectivity test, nothing journaled**\n"
               + notifier.format_episode(episode))
    print(message)
    ok = asyncio.run(notifier.send_discord_message(message))
    print(f"\nDiscord delivery: {'OK' if ok else 'FAILED (webhook unconfigured or unreachable)'}")
    return ok


if __name__ == "__main__":
    import sys
    if "--mock-trade-strategy" in sys.argv:
        i = sys.argv.index("--mock-trade-strategy")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else "IRON_BUTTERFLY"
        sys.exit(0 if run_mock_trade(arg) else 1)
    run_tracker()
