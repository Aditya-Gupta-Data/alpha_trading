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

from datetime import date, datetime

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


def _vix_slippage_mult(vix: float) -> float:
    """P1 fix — the crisis bid-ask BLOWOUT. Calm VIX (<15) = 1x (normal book).
    Ramps 1x->2x across VIX 15-25, then NON-LINEARLY +0.3x per point above 25
    (VIX 30 -> 3.5x, 40 -> 5.5x, capped 6x) — a wide, panicked book you cannot
    escape cheaply. None -> 1x (the pre-P1 behaviour)."""
    if vix is None:
        return 1.0
    if vix >= 25:
        return min(6.0, 2.0 + 0.3 * (vix - 25))
    if vix > 15:
        return 1.0 + (vix - 15) / 10.0
    return 1.0


def apply_slippage(price: float, instrument_type: str,
                   vix: float = None, lots: int = 1,
                   symbol: str = None) -> float:
    """Bid-ask slippage in Rs. per unit. `vix` scales OPTION slippage to model
    the crisis blowout (see _vix_slippage_mult); deep-OTM legs (lowest premium,
    0.50% base) suffer that multiple off the worst base, so a panicked wing is
    savaged. `lots` adds a synthetic book-depth penalty (+~0.05% per 10 lots) —
    no infinite top-of-book liquidity. vix=None + lots<=1 reproduces the
    original ladder byte-for-byte (backward compatible).

    Gap 4 (2026-08-17): `symbol` switches on the LIQUIDITY-TIER floor from
    `liquidity_slippage` (fo_liquidity.json: tier1 0.10% / tier2 0.25% /
    illiquid 0.50% per side). STOCK — which was a flat 0.0% — becomes the
    tier fraction; OPTION/INDEX pay max(their own ladder, the tier floor).
    symbol=None keeps every legacy number unchanged. Paper fills only."""
    instr_upper = instrument_type.upper()
    tier_frac = None
    if symbol:
        try:
            from src.liquidity_slippage import tier_slippage_frac
            tier_frac = tier_slippage_frac(symbol)
        except Exception:
            tier_frac = None
    if instr_upper == "INDEX":
        return price * max(0.0005, tier_frac or 0.0)   # 0.05% legacy floor
    if instr_upper != "OPTION":
        return price * (tier_frac or 0.0)   # STOCK: 0.0% legacy, tier when named
    base = 0.0050 if price < 50 else 0.0030 if price < 150 else 0.0010
    size_frac = (max(0, int(lots) - 1) / 10.0) * 0.0005   # +0.05% per 10 lots
    return price * max(base * _vix_slippage_mult(vix) + size_frac, tier_frac or 0.0)


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


# NO MID-TRADE STOP ON A SPREAD (decision #105, 2026-09-23). A defined-risk
# structure's max loss is capped by construction (and by the Rs.10k per-trade
# cap); it is held to the profit take or the pre-expiry exit. The #103
# premium stop was reversed and the #104 underlying stop withdrawn — neither
# predicate exists any more, and `tests/test_no_spread_stop.py` keeps it so.


def _forced_exit_days(underlying: str) -> int:
    """Days-before-expiry at which an OPEN spread is force-closed.
    Delegates to options_proposer, the ONE place that knows which
    underlyings are physically settled — never re-derived from a suffix.
    Fail-safe: any import problem keeps the index rule rather than
    silently extending a stock option's life."""
    try:
        from src.options_proposer import forced_exit_days_for
        return forced_exit_days_for(underlying)
    except Exception:
        return PRE_EXPIRY_EXIT_DAYS


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
      profit_take       NEUTRAL structures only: modeled profit >= 65% of
                        max profit (theta harvest, unchanged)
      ratchet_hit       DIRECTIONAL structures (decision #110): capture fell
                        below the profit ratchet's locked level (armed at 40%
                        of max profit → breakeven; 60→30, 80→50, 90→70);
                        only on bars on/after RATCHET_EFFECTIVE_DATE
      pre_expiry_exit   PRE_EXPIRY_EXIT_DAYS or fewer days to expiry
    There is NO mid-trade stop (decision #105): a defined-risk structure's
    max loss is capped by construction. The walk stamps `entry["ratchet"]`
    (peak / lock / armed) for directional spreads so the live bridge and the
    journal share one state."""
    spread = entry["spread"]
    expiry = date.fromisoformat(spread["expiry"])
    entry_day = date.fromisoformat(entry["date"])
    total_days = max(1, (expiry - entry_day).days)
    m_entry = _spread_entry_mark(spread)
    lot = int(spread["lot_size"])
    max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
    max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0
    from src import profit_ratchet as pr
    from src.config import RATCHET_EFFECTIVE_DATE, RATCHET_ENABLED
    ratcheted = RATCHET_ENABLED and pr.is_directional(spread) and max_profit_ps > 0
    rstate = dict(entry.get("ratchet") or {})
    peak = rstate.get("peak_capture_pct")
    lock = rstate.get("locked_pct")

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
        if ratcheted:
            capture = profit_ps / max_profit_ps * 100.0
            peak = capture if peak is None else max(float(peak), capture)
            st = pr.state(peak, lock)
            lock = st["locked_pct"]
            entry["ratchet"] = dict(st, as_of=day)
            if day >= RATCHET_EFFECTIVE_DATE and pr.ratchet_hit(capture, lock):
                entry["ratchet"]["hit_capture_pct"] = round(capture, 2)
                return "ratchet_hit", m_now, frac_left, day
        elif max_profit_ps > 0 and profit_ps >= OPTION_PROFIT_TAKE_FRACTION * max_profit_ps:
            return "profit_take", m_now, frac_left, day
        # PHYSICAL SETTLEMENT (2026-08-05): a STOCK option leaves before
        # expiry WEEK, not two days out. An ITM short leg held to expiry
        # is a delivery obligation on the full notional — not the spread's
        # max loss — and NSE's delivery margin escalates through the final
        # week, so a "defined-risk" structure stops being defined-risk
        # exactly there. Index options are cash-settled and keep the
        # existing 2-day rule.
        if days_left <= _forced_exit_days(entry.get("ticker")):
            return "pre_expiry_exit", m_now, frac_left, day
    return None


def _resolve_spread_trailed(entry: dict, bars: list):
    """M4A (2026-09-11, decision #96): `_resolve_spread` PLUS an ATR trail
    on the UNDERLYING, for entries that carry `plan.trailing`. This is a
    SEPARATE resolver on purpose: the live sweep keeps calling the
    trail-free `_resolve_spread` (the 2026-08-05 anti-drift lock — a
    vertical is defined-risk and a trail can only cut winners — still
    holds byte-for-byte there). Only the Glassbreaking SHADOW grader calls
    this one, so whether a trail helps a debit spread is a question the
    Proving Court answers before the live path ever asks it.

    Bullish structures trail BELOW the running peak close; bearish ones
    ABOVE the running trough. One-way ratchet, tested against the NEXT
    bar, exactly like the equity trail. Without `plan.trailing` this is
    `_resolve_spread` exactly."""
    trail = trailing_config(entry)
    if trail is None:
        return _resolve_spread(entry, bars)
    spread = entry["spread"]
    expiry = date.fromisoformat(spread["expiry"])
    total_days = max(1, (expiry - date.fromisoformat(entry["date"])).days)
    m_entry = _spread_entry_mark(spread)
    lot = int(spread["lot_size"])
    max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
    max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0
    bullish = str(spread.get("direction", "bullish")).lower() != "bearish"
    ratchet, extreme, seen = None, None, []

    for idx, (day, _low, _high, close) in enumerate(bars):
        seen.append(bars[idx])
        if day <= entry["date"]:
            continue
        frac_left = max(0.0, (expiry - date.fromisoformat(day)).days / total_days)
        profit_ps = max(-max_loss_ps, min(_spread_mark(spread, float(close), frac_left)
                                          - m_entry, max_profit_ps))
        m_now = m_entry + profit_ps
        c = float(close)
        if ratchet is not None and ((bullish and c <= ratchet)
                                    or (not bullish and c >= ratchet)):
            return "trail_hit", m_now, frac_left, day
        if max_profit_ps > 0 and profit_ps >= OPTION_PROFIT_TAKE_FRACTION * max_profit_ps:
            return "profit_take", m_now, frac_left, day
        if (expiry - date.fromisoformat(day)).days <= _forced_exit_days(entry.get("ticker")):
            return "pre_expiry_exit", m_now, frac_left, day
        extreme = c if extreme is None else (max(extreme, c) if bullish
                                             else min(extreme, c))
        a = atr_from_bars(seen, trail["atr_n"])
        if a is not None:
            ratchet = atr_trailing_stop(extreme, a, trail["atr_mult"],
                                        floor=None, current=ratchet,
                                        bullish=bullish)
    return None


# ------------------------------------------------- expiry backstop (Issue 28)
#
# 2026-09-11 hotfix. `_resolve_spread` only ever exits on a DAILY BAR: with
# no bars (dead token, lapsed Data API plan — Issue 26) it printed "will
# retry next run" forever, and three NIFTY FIN SERVICE spreads sat 15 days
# past their 2026-08-25 expiry with ₹73,845 of margin locked, blocking every
# new FIN SERVICE proposal through the exposure gate (Issue 28).
#
# The backstop is a WALL-CLOCK rule: once today's date is past the expiry,
# the spread no longer exists at the exchange, so the journal must not say
# it is open. Settlement price, in order of honesty:
#
#   1. the last available close ON OR BEFORE expiry -> intrinsic value only
#      (time value is zero at expiry). Named `last_close_on_or_before_expiry`.
#      Fires the first sweep after expiry.
#   2. NO price data at all -> the defined max loss, named
#      `no_price_data_max_loss`. Deliberately CONSERVATIVE (the ledger's
#      loss-permanence bias, RULE 3): a settlement is never marked up on a
#      guess. Waits EXPIRY_BACKSTOP_GRACE_DAYS after expiry so a one-day
#      outage can still recover the real close via (1); after that the
#      margin release matters more than the mark, and the outcome carries
#      the basis so the row can be reviewed by hand.
#
# Both paths release the margin lock and journal the outcome exactly like a
# normal resolution. Nothing here places an order (Rule 7).
EXPIRY_BACKSTOP_RESOLUTION = "expiry_backstop"
EXPIRY_BACKSTOP_GRACE_DAYS = 3        # calendar days past expiry before path (2)


def _today() -> date:
    """Wall clock seam — tests replace this to move the calendar."""
    return date.today()


def _expiry_backstop(entry: dict, bars: list, today: date = None):
    """Force-settle a spread whose expiry date has passed.

    Returns None while the backstop does not apply (expiry not yet past, or
    no data and still inside the grace window). Otherwise returns
      (resolution, exit_mark_per_share, frac_left, exit_day, exit_close,
       settlement_basis, close_date)
    where exit_close/close_date are None on the no-data path."""
    spread = entry["spread"]
    expiry = date.fromisoformat(spread["expiry"])
    today = today or _today()
    if today <= expiry:
        return None
    lot = int(spread["lot_size"])
    m_entry = _spread_entry_mark(spread)
    max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
    max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0

    usable = [b for b in (bars or []) if b[0] <= spread["expiry"]]
    if usable:
        close_day, _low, _high, close = max(usable, key=lambda b: b[0])
        m_now = _spread_mark(spread, float(close), 0.0)   # intrinsic only
        profit_ps = max(-max_loss_ps, min(m_now - m_entry, max_profit_ps))
        return (EXPIRY_BACKSTOP_RESOLUTION, m_entry + profit_ps, 0.0,
                spread["expiry"], float(close),
                "last_close_on_or_before_expiry", close_day)
    if (today - expiry).days < EXPIRY_BACKSTOP_GRACE_DAYS:
        return None
    return (EXPIRY_BACKSTOP_RESOLUTION, m_entry - max_loss_ps, 0.0,
            spread["expiry"], None, "no_price_data_max_loss", None)


def _spread_exit_costs(spread: dict, spot_exit: float, frac_left: float,
                       vix: float = None, exit_slipped: bool = False) -> tuple:
    """(total_frictions, total_slippage) across ALL legs, both ends of the
    trade, priced per leg: entry legs pay their side's 2026 frictions on
    the entry premium; the atomic basket exit flips each side (short legs
    are bought back -> stamp duty, long legs are sold -> STT) on the
    modeled exit premium. Slippage per leg uses the OPTION liquidity
    ladder (0.10%-0.50%, cheap OTM legs get the worst fill)."""
    qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
    entry_spot = spread.get("entry_spot")
    # P1: size/VIX penalties activate ONLY when a `vix` is supplied (the
    # crisis-aware path). Every legacy caller (vix=None) keeps lots=1 -> the
    # exact pre-P1 slippage, so nothing downstream changes.
    lots = int(spread.get("lots", 1)) if vix is not None else 1
    frictions = slippage = 0.0
    for leg in spread["legs"]:
        entry_side = leg["side"].upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        exit_premium = _leg_model_premium(leg, spot_exit, entry_spot, frac_left)
        frictions += pf.calculate_trade_frictions("OPTION", entry_side, leg["premium"], qty)
        frictions += pf.calculate_trade_frictions("OPTION", exit_side, exit_premium, qty)
        # #70: a "quoted" fill already crossed the bid-ask at entry (the
        # premium IS the worse side) — the ladder would double-charge it.
        if leg.get("fill_basis") not in ("quoted", "venue"):   # #70; "venue" = paper venue already slipped (#101)
            slippage += apply_slippage(leg["premium"], "OPTION", lots=lots) * qty
        # The EXIT crossing pays the CRISIS blowout at the exit-day VIX (P1).
        # #103: when the paper venue filled the exit, its slipped fills ARE
        # the exit cost — the ladder must not charge the crossing twice.
        if not exit_slipped:
            slippage += apply_slippage(exit_premium, "OPTION", vix=vix, lots=lots) * qty
    return frictions, slippage


def _settle_spread_cash(pnl_net: float) -> bool:
    """Net-settle a resolved approved spread against paper cash (entry
    premiums and exit values collapse to one net P&L figure — margin was
    only ever virtually blocked, never deducted)."""
    book = pf.load()
    book["cash"] = round(book["cash"] + pnl_net, 2)
    pf.save(book)
    return True


# ------------------------------------------------- intraday square-off

def _spread_exit_costs_quoted(spread: dict, leg_exit_premiums: dict,
                              exit_slipped: bool = False) -> tuple:
    """(total_frictions, total_slippage) like _spread_exit_costs, but the
    exit side is priced on REAL quoted premiums instead of the linear
    model — the intraday square-off's cost basis (decision #69)."""
    qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
    frictions = slippage = 0.0
    for leg in spread["legs"]:
        entry_side = leg["side"].upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        exit_premium = float(leg_exit_premiums[(float(leg["strike"]),
                                                leg["option_type"].upper())])
        frictions += pf.calculate_trade_frictions("OPTION", entry_side, leg["premium"], qty)
        frictions += pf.calculate_trade_frictions("OPTION", exit_side, exit_premium, qty)
        # #70: same double-charge guard as _spread_exit_costs.
        if leg.get("fill_basis") not in ("quoted", "venue"):   # #70; "venue" = paper venue already slipped (#101)
            slippage += apply_slippage(leg["premium"], "OPTION") * qty
        if not exit_slipped:                                   # #103: venue exit already slipped
            slippage += apply_slippage(exit_premium, "OPTION") * qty
    return frictions, slippage


# ------------------------------------------------- the OMS exit (decision #103)
#
# An exit the tracker decided (profit_take / pre_expiry_exit /
# trail_hit, or the intraday square-off) becomes an EXIT Order Ticket —
# every leg flipped, worked as one atomic basket — that the PAPER venue
# fills at the leg's exit limit worsened by tier slippage. The venue's
# fills then ARE the exit prices: the basket mark is rebuilt from them and
# the exit-side slippage ladder is skipped. STILL ONE SETTLEMENT PATH: the
# ticket is a record of the exit this function is already performing; the
# decision, the P&L and the journal write stay here. Flag-gated by the same
# PAPER_VENUE_ENABLED as entries; fail-OPEN to the modeled exit, error
# named on the outcome. The expiry backstop never goes through the venue
# (there is no price to fill against).

def _execute_paper_exit(entry: dict, leg_limits: dict, resolution: str,
                        conn=None, venue_mod=None, today: date = None) -> dict:
    """Issue + fill the exit ticket(s) — the primary's, and one per shadow
    paper account holding this entry (#102). Returns {mode, ticket_id,
    status, exit_mark, venue_slippage_ps, fills, accounts, error}.
    `venue_slippage_ps` = the per-share cost the venue's fills added over
    the exit limits (always adverse: Σ |fill − limit|); None unless every
    leg of the primary ticket FILLED. The caller keeps its own (clamped)
    exit mark and books this in place of the exit-side slippage ladder —
    so the no-arbitrage clamp still holds and the venue's cost is a COST."""
    from src.config import PAPER_VENUE_ENABLED
    record = {"mode": "model", "ticket_id": None, "status": None, "exit_mark": None,
              "venue_slippage_ps": None, "fills": {}, "accounts": {}, "error": None}
    if not PAPER_VENUE_ENABLED or not (entry.get("spread") or {}).get("legs"):
        return record
    try:
        from src import oms, strategy_router
        venue = venue_mod
        if venue is None:
            from src.execution import paper_venue as venue
        own = conn is None
        if own:
            conn = _brain_connect()
        try:
            tickets = [(oms.PRIMARY_ACCOUNT, None)]
            for acct, v in (entry.get("accounts") or {}).items():
                if (v or {}).get("status") == "approved" and v.get("ticket_id") \
                        and int(v.get("lots") or 0) > 0:
                    tickets.append((acct, int(v["lots"])))
            tids = {}
            for acct, lots in tickets:
                issued = strategy_router.issue_exit(conn, entry, leg_limits, resolution,
                                                    source=f"plan_tracker.{resolution}",
                                                    account_id=acct, lots=lots)
                tids[acct] = issued["ticket_id"]
            venue.sweep(conn, today=today, stamp=False)
            tid = tids[oms.PRIMARY_ACCOUNT]
            view = oms.ticket_view(conn, tid) or {}
            record.update(mode="paper_venue", ticket_id=tid, status=view.get("status"))
            if view.get("status") == oms.FILLED:
                mark = slip = 0.0
                for l in view["legs"]:
                    key = (float(l["strike"]), str(l.get("option_type") or "").upper())
                    fill = float(l["avg_fill_price"])
                    limit = float(l.get("limit_price") or fill)
                    record["fills"][f"{key[0]:g}{key[1]}"] = {"limit": limit, "fill": fill,
                                                             "side": l["side"]}
                    # the EXIT leg's side is the flipped one: a SELL here closes
                    # a long (value received), a BUY closes a short (value paid)
                    mark += fill if str(l["side"]).upper() == "SELL" else -fill
                    slip += abs(fill - limit)
                record["exit_mark"] = round(mark, 4)
                record["venue_slippage_ps"] = slip          # full precision; rounded in rupees
            for acct, stid in tids.items():
                if acct != oms.PRIMARY_ACCOUNT:
                    sv = oms.ticket_view(conn, stid) or {}
                    record["accounts"][acct] = {"ticket_id": stid, "status": sv.get("status"),
                                                "lots": sv.get("lots")}
        finally:
            if own:
                conn.close()
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
    return record


def note_ratchet(short_id: str, peak_capture_pct: float, locked_pct: float) -> bool:
    """Persist a ratchet rung crossing seen intraday (decision #110): the
    live bridge saw the modeled capture reach a new arm level, so the peak
    and the lock survive the day even if the close gives it back. Only ever
    RAISES the stored values; race-safe via journal.update_entry. Never
    raises; False when nothing was written."""
    try:
        def _mutate(entry):
            if entry.get("outcome") is not None or not entry.get("spread"):
                return False
            cur = dict(entry.get("ratchet") or {})
            new_peak = max(float(peak_capture_pct), float(cur.get("peak_capture_pct") or -1e9))
            new_lock = (max(float(locked_pct), float(cur["locked_pct"]))
                        if cur.get("locked_pct") is not None else float(locked_pct))
            if cur.get("locked_pct") is not None and new_lock <= float(cur["locked_pct"]) \
                    and new_peak <= float(cur.get("peak_capture_pct") or -1e9):
                return False
            entry["ratchet"] = dict(cur, peak_capture_pct=round(new_peak, 2),
                                    locked_pct=new_lock, armed=True,
                                    as_of=date.today().isoformat(), source="live_bridge")
            return True
        return bool(journal.update_entry(short_id, _mutate))
    except Exception as exc:
        print(f"  (ratchet note skipped for {short_id}: {exc})")
        return False


def resolve_intraday_profit_take(short_id: str, leg_quotes: dict,
                                 model_capture_pct: float = None,
                                 today: date = None, resolution: str = "profit_take",
                                 locked_pct: float = None) -> dict:
    """Decision #69 (owner, 2026-07-14): square off ONE approved open
    spread the moment its profit-take fires intraday — priced on REAL
    option-chain quotes, never the linear model. The live loop calls this
    (the ONE settlement seam it is allowed); the same helpers the EOD
    path uses do the arithmetic, and journal.update_entry makes the write
    race-safe against the hourly tracker sweep.

    `leg_quotes` maps (strike, 'CE'/'PE') -> last traded premium for every
    leg. The threshold is RE-VERIFIED on these real quotes: a modeled 70%
    that is really 55% does NOT exit (model-vs-market divergence guard) —
    the EOD path keeps owning it. Returns {"status", ...}; every non-
    "squared_off" status leaves the trade untouched for the EOD path.
    Never raises."""
    today = today or date.today()
    result = {"status": "error", "short_id": short_id}
    try:
        def _mutate(entry):
            spread = entry.get("spread")
            if not spread or entry.get("outcome") is not None:
                result["status"] = "already_resolved"
                return False
            if entry.get("decision") != "approved":
                result["status"] = "not_approved"
                return False
            # Every leg must have a real quote or we refuse (fail to EOD).
            try:
                m_exit = sum((1.0 if l["side"].upper() == "BUY" else -1.0)
                             * float(leg_quotes[(float(l["strike"]),
                                                 l["option_type"].upper())])
                             for l in spread["legs"])
            except (KeyError, TypeError, ValueError):
                result["status"] = "missing_leg_quote"
                return False
            lot = int(spread["lot_size"])
            lots = int(spread.get("lots", 1))
            qty = lot * lots
            max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
            max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0
            m_entry = _spread_entry_mark(spread)
            profit_ps = max(-max_loss_ps, min(m_exit - m_entry, max_profit_ps))
            # The real-quote verification gate.
            real_capture = (profit_ps / max_profit_ps * 100) if max_profit_ps else 0.0
            if resolution == "ratchet_hit":
                # decision #110: the ratchet fired because the MODELED capture
                # fell under the lock; on REAL quotes it must still be under
                # the lock, or the EOD path keeps owning it.
                from src import profit_ratchet as pr
                if locked_pct is None or not pr.ratchet_hit(real_capture, locked_pct):
                    result["status"] = "above_lock_on_real_quotes"
                    result["real_capture_pct"] = round(real_capture, 2)
                    return False
            elif not (max_profit_ps > 0 and profit_ps
                      >= OPTION_PROFIT_TAKE_FRACTION * max_profit_ps):
                result["status"] = "below_threshold_on_real_quotes"
                result["real_capture_pct"] = round(real_capture, 2)
                return False

            m_exit = m_entry + profit_ps          # clamped basket exit mark
            gross_pnl = profit_ps * qty
            frictions, slippage = _spread_exit_costs_quoted(
                spread, leg_quotes)
            # Decision #103: the square-off is an EXIT ticket the paper
            # venue fills at the REAL quotes (+ tier slippage).
            execution = _execute_paper_exit(entry, leg_quotes, resolution, today=today)
            if execution.get("venue_slippage_ps") is not None:
                frictions, slippage = _spread_exit_costs_quoted(
                    spread, leg_quotes, exit_slipped=True)
                slippage = round(slippage + float(execution["venue_slippage_ps"]) * qty, 2)
                frictions = round(frictions, 2)
            pnl_net = round(gross_pnl - frictions - slippage, 2)
            capture_pct = (gross_pnl / (max_profit_ps * qty) * 100
                           if max_profit_ps > 0 else 0.0)
            max_loss_total = float(spread["max_loss"]) * lots

            _settle_spread_cash(pnl_net)
            from src import portfolio_manager as pm
            pm.release_entry(short_id, pnl_net)

            entry["outcome"] = {
                "checked": today.isoformat(),
                "resolution": resolution,
                "price": round(m_exit, 2),
                "exit_date": today.isoformat(),
                "pct": round(capture_pct, 2),
                "r_multiple": round(pnl_net / max_loss_total, 2)
                              if max_loss_total > 0 else None,
                "days_in_trade": (today
                                  - date.fromisoformat(entry["date"])).days,
                "pnl_rs": pnl_net,
                "frictions_rs": round(frictions, 2),
                "slippage_rs": round(slippage, 2),
                "exit_style": "atomic_basket",
                "exit_basis": "intraday_chain",    # vs the EOD path's bars
                "model_capture_pct": model_capture_pct,  # signal-vs-fill gap
                **({"execution": execution,
                    "venue_slippage_rs": (round(float(execution["venue_slippage_ps"]) * qty, 2)
                                          if execution.get("venue_slippage_ps") is not None
                                          else None)}
                   if execution.get("mode") == "paper_venue" or execution.get("error") else {}),
                "hypothetical": False,
                "position_closed": True,
                "verdict": _spread_verdict(entry, resolution, pnl_net,
                                           capture_pct),
            }
            result.update(status="squared_off", pnl_rs=pnl_net,
                          capture_pct=round(capture_pct, 2))
            return True

        updated = journal.update_entry(short_id, _mutate)
        if updated is None:
            if result["status"] == "error":
                result["status"] = "not_found"
            return result

        # Post-settlement recording (both fail-open; the trade IS closed).
        try:
            brain = brain_map.connect()
            try:
                record_post_mortem(updated, brain)
            finally:
                brain.close()
        except Exception:
            pass
        try:
            from src.notifier import fire_broadcast
            fire_broadcast({
                "event": "closed",
                "ticker": updated["ticker"],
                "date": today.isoformat(),
                "strategy": (updated.get("spread") or {}).get("strategy"),
                "pnl_rs": result.get("pnl_rs"),
                "note": (f"intraday square-off at "
                         f"{result.get('capture_pct', 0):.0f}% of max "
                         "profit (real chain quotes, decision #69)"),
            })
        except Exception:
            pass
        return result
    except Exception as exc:
        result["reason"] = str(exc)
        return result


def _spread_verdict(entry: dict, resolution: str, pnl_net: float, capture_pct: float) -> str:
    approved = entry["decision"] == "approved"
    if resolution == EXPIRY_BACKSTOP_RESOLUTION:
        basis = (entry.get("outcome") or {}).get("settlement_basis")
        if basis == "no_price_data_max_loss":
            return ("EXPIRY BACKSTOP — NO PRICE DATA: settled at the defined max loss "
                    "(conservative, wall clock; review this row by hand)")
        return (f"EXPIRY BACKSTOP — settled at intrinsic value on the last close "
                f"before expiry (wall clock), net Rs.{pnl_net:+,.2f}")
    if resolution == "ratchet_hit":
        rs = entry.get("ratchet") or {}
        return ((f"WIN — profit ratchet took {capture_pct:.0f}% of max profit "
                 f"(peak {rs.get('peak_capture_pct')}%, lock {rs.get('locked_pct')}%; "
                 f"decision #110)" if pnl_net > 0 else
                 f"flat — profit ratchet closed at breakeven lock "
                 f"(peak {rs.get('peak_capture_pct')}%; decision #110)")
                if approved else
                f"MISSED GAIN — the ratchet would have banked {capture_pct:.0f}% without you")
    if resolution == "profit_take":
        return (f"WIN — auto-exit at {capture_pct:.0f}% of max profit (gamma discipline)"
                if approved else
                f"MISSED GAIN — it reached {capture_pct:.0f}% of max profit without you")
    if resolution == "trail_hit":
        return (f"{'WIN' if pnl_net > 0 else 'LOSS' if pnl_net < 0 else 'flat'} — "
                f"ATR trail on the underlying ratcheted us out at "
                f"{capture_pct:.0f}% of max profit")
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
    # .get(): pre-Phase-4B journal lines may lack the outcome key entirely
    # (same tolerance _spread_trackable already has).
    plan = entry.get("plan")
    return entry.get("outcome") is None and bool(plan and plan.get("stop_loss"))


# ==================== ATR trailing stop (Level 1, 2026-08-05) ==========
#
# SCOPE, stated first because it is the safety property that matters:
# this touches ONLY the plan-carrying LONG equity path (`_resolve`). It
# does NOT touch `_resolve_spread`, which owns all 19 resolved options
# trades and their 65% profit-take. That is not timidity, it is
# correctness — a vertical spread is DEFINED-RISK: max loss is capped by
# construction and the module already documents "defined-risk structures
# need no stop trigger". A trailing stop on a capped-loss structure adds
# no protection and would only cut winners early.
#
# WHAT IT DOES. The fixed bracket exits at the plan's original stop no
# matter how far price has run. The trail ratchets a floor upward at
# `TRAIL_ATR_MULT` x ATR below the highest high seen since entry, and
# NEVER downward. Once armed it can only ever exit at or above the plan's
# own stop, so it is strictly risk-REDUCING versus the bracket.
#
# ARMING IS OPT-IN AND PER-ENTRY: the plan must carry
# `{"trailing": {"atr_mult": 2.0}}`. With it absent — every one of the
# existing journal entries — `_resolve` is byte-identical to before.
# Nothing retro-fits a trail onto a position that was opened without one.
#
# INTERACTION WITH THE EXISTING GATES (unchanged, and deliberately so):
#   * the hard stop still wins if it trades first on the same bar (the
#     module's standing pessimistic same-bar rule);
#   * the target still closes the trade at the target;
#   * `PLAN_MAX_DAYS` time-stop is untouched;
#   * the 10% risk-of-ruin breaker and the 65% option profit-take live in
#     `portfolio_manager` / `_resolve_spread` and are not on this path.

TRAIL_ATR_N = 14
TRAIL_ATR_MULT_DEFAULT = 2.0


def atr_from_bars(bars: list, n: int = TRAIL_ATR_N):
    """Wilder true range averaged over the last `n` bars of
    (day, low, high, close) tuples. None below n+1 bars — an ATR from a
    short history is a guess, and a guess must not move a stop.

    Deliberately mirrors `analysis/dynamic_pricer.atr` (same TR formula,
    same "None below n+1" rule); it is re-expressed here only because
    that one takes dicts and this path carries tuples, and importing a
    Dept-8 module into the settlement path would be a layering break."""
    if not bars or len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        prev_close = bars[i - 1][3]
        low, high = bars[i][1], bars[i][2]
        if prev_close is None or low is None or high is None:
            return None
        trs.append(max(high, prev_close) - min(low, prev_close))
    return round(sum(trs) / n, 4)


def atr_trailing_stop(extreme: float, atr: float, atr_mult: float,
                      floor: float = None, current: float = None,
                      bullish: bool = True) -> float:
    """The ratchet, as one pure function (M4A, 2026-09-11). For a long,
    the candidate stop is `extreme − mult×ATR`, never below `floor` (the
    plan's own hard stop) and never below the `current` trail — it only
    ever rises. For a short/bearish structure the mirror: `trough +
    mult×ATR`, only ever falls. `_resolve` (equity) and `_resolve_spread`
    (spreads with `plan.trailing`) both call this."""
    if bullish:
        candidate = extreme - atr_mult * atr
        if floor is not None:
            candidate = max(candidate, float(floor))
        return candidate if current is None else max(current, candidate)
    candidate = extreme + atr_mult * atr
    if floor is not None:
        candidate = min(candidate, float(floor))
    return candidate if current is None else min(current, candidate)


# ------------------------------------------------- tranches (M4A pyramiding)
#
# STRUCTURE ONLY, deliberately (2026-09-11, decision #96). A plan may carry
#     plan.tranches = {"levels": [{"at_r": 1.0, "add_lots": 1},
#                                 {"at_r": 2.0, "add_lots": 1}],
#                      "max_lots": 3}
# and the tracker then OBSERVES where each add-on would have fired (the
# first bar whose favourable excursion reaches +at_r × initial risk) and
# what the add-ons would have earned to the exit. That observation lands
# on the outcome as `tranches` with `pyramid_pnl_rs` kept SEPARATE from
# `pnl_rs`: no add-on is booked to cash, no margin is locked for it, and
# the capital layer never sees it. Booking pyramids for real is a Dept-5
# decision gated on the Proving Court's read of exactly these rows.
TRANCHE_MAX_MULT_DEFAULT = 3


def tranche_config(entry: dict) -> dict | None:
    """Normalised `plan.tranches` or None. Levels sorted by at_r; a level
    with at_r <= 0 or add_lots < 1 is dropped; an empty ladder is None."""
    cfg = (entry.get("plan") or {}).get("tranches")
    if not isinstance(cfg, dict):
        return None
    levels = []
    for lv in cfg.get("levels") or []:
        try:
            at_r, add = float(lv.get("at_r")), int(lv.get("add_lots"))
        except (TypeError, ValueError, AttributeError):
            continue
        if at_r > 0 and add >= 1:
            levels.append({"at_r": at_r, "add_lots": add})
    if not levels:
        return None
    levels.sort(key=lambda lv: lv["at_r"])
    try:
        max_lots = int(cfg.get("max_lots") or 0)
    except (TypeError, ValueError):
        max_lots = 0
    return {"levels": levels, "max_lots": max_lots}


def tranche_events(cfg: dict, base_lots: int, r_path: list) -> list:
    """Walk [(day, r_high, add_price)] in order; the FIRST bar whose
    favourable excursion reaches a level fires that level, once, capped so
    base + adds never exceed max_lots. `add_price` is either a number (the
    fill for any level on that bar — a spread's close mark) or a callable
    `f(at_r) -> price` (an equity add fills AT the level price, entry +
    at_r × risk, where the trigger was crossed intrabar — never at the
    bar's high). Pure."""
    events, total = [], int(base_lots)
    pending = list(cfg["levels"])
    cap = int(cfg.get("max_lots") or 0)
    for day, r_high, add_price in r_path:
        while pending and r_high >= pending[0]["at_r"]:
            lv = pending.pop(0)
            add = lv["add_lots"]
            if cap and total + add > cap:
                add = max(0, cap - total)
            if add <= 0:
                continue
            total += add
            px = add_price(lv["at_r"]) if callable(add_price) else add_price
            events.append({"at_r": lv["at_r"], "date": day,
                           "price": round(float(px), 2), "add_lots": add})
    return events


def _equity_r_path(entry: dict, bars: list, until_day: str) -> list:
    """(day, r_high, add_price) per bar after entry up to the exit day,
    R measured on the plan's own initial risk per share."""
    price = float(entry["price"])
    risk = price - float(entry["plan"]["stop_loss"]["price"])
    if risk <= 0:
        return []
    out = []
    for day, _low, high, _close in bars:
        if day <= entry["date"]:
            continue
        if day > until_day:
            break
        r_high = (float(high) - price) / risk
        out.append((day, r_high, lambda at_r, p=price, k=risk: p + at_r * k))
    return out


def _spread_r_path(entry: dict, bars: list, until_day: str) -> list:
    """(day, r, mark) per bar after entry up to the exit day, R = modelled
    profit per share ÷ max loss per share (the same clamp as _resolve_spread)."""
    spread = entry["spread"]
    expiry = date.fromisoformat(spread["expiry"])
    total_days = max(1, (expiry - date.fromisoformat(entry["date"])).days)
    lot = int(spread["lot_size"])
    max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0
    max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
    m_entry = _spread_entry_mark(spread)
    if max_loss_ps <= 0:
        return []
    out = []
    for day, _low, _high, close in bars:
        if day <= entry["date"]:
            continue
        if day > until_day:
            break
        frac_left = max(0.0, (expiry - date.fromisoformat(day)).days / total_days)
        profit_ps = max(-max_loss_ps, min(_spread_mark(spread, float(close), frac_left)
                                          - m_entry, max_profit_ps))
        out.append((day, profit_ps / max_loss_ps, m_entry + profit_ps))
    return out


def observe_tranches(entry: dict, bars: list, exit_day: str, exit_price: float,
                     base_lots: int, unit: int) -> dict | None:
    """The outcome's `tranches` block, or None when the plan has no ladder.
    `unit` is shares-per-lot (1 for equity, lot_size for spreads);
    `exit_price` the per-share exit price/mark the add-ons would close at.
    pyramid_pnl_rs is a SHADOW figure — see the block comment above."""
    cfg = tranche_config(entry)
    if cfg is None:
        return None
    if entry.get("spread"):
        path = _spread_r_path(entry, bars, exit_day)
    else:
        path = _equity_r_path(entry, bars, exit_day)
    events = tranche_events(cfg, base_lots, path)
    pyramid = sum(ev["add_lots"] * unit * (float(exit_price) - ev["price"])
                  for ev in events)
    return {"ladder": cfg, "events": events,
            "added_lots": sum(ev["add_lots"] for ev in events),
            "pyramid_pnl_rs": round(pyramid, 2),
            "booked": False,
            "note": "shadow observation — not in pnl_rs, no margin locked (decision #96)"}


def trailing_config(entry: dict) -> dict | None:
    """The entry's own trail spec, or None when it was opened without one."""
    cfg = (entry.get("plan") or {}).get("trailing")
    if not isinstance(cfg, dict):
        return None
    try:
        mult = float(cfg.get("atr_mult", TRAIL_ATR_MULT_DEFAULT))
    except (TypeError, ValueError):
        mult = TRAIL_ATR_MULT_DEFAULT
    if mult <= 0:
        return None
    return {"atr_mult": mult, "atr_n": int(cfg.get("atr_n", TRAIL_ATR_N))}


def _resolve(entry: dict, bars: list):
    """(resolution, exit_price, exit_date) once the plan has resolved,
    else None while it is still live.

    Walks the bars once. When the entry carries a `trailing` spec, a
    ratcheting ATR floor is maintained alongside the fixed bracket and
    can trigger a `trail_hit` exit — see the block comment above for why
    this is strictly risk-reducing and why spreads are excluded."""
    stop = entry["plan"]["stop_loss"]["price"]
    target = entry["plan"]["target"]["price"]
    trail = trailing_config(entry)

    trail_stop = None
    peak = None
    seen = []            # bars up to and including the current one, for ATR

    for idx, (day, low, high, _close) in enumerate(bars):
        seen.append(bars[idx])
        if day <= entry["date"]:
            continue  # never scan the entry day itself

        # Same-bar pessimism, unchanged: the hard stop is checked first,
        # then the trail (which always sits at or above it), then target.
        if low <= stop:
            return "stop_hit", stop, day
        if trail_stop is not None and low <= trail_stop:
            return "trail_hit", round(float(trail_stop), 2), day
        if high >= target:
            return "target_hit", target, day

        # Ratchet AFTER the exit checks, so a floor can never be raised
        # using the same bar it would then be tested against.
        if trail:
            peak = high if peak is None else max(peak, high)
            a = atr_from_bars(seen, trail["atr_n"])
            if a is not None:
                # Never below the plan's own stop, and never downward.
                trail_stop = atr_trailing_stop(peak, a, trail["atr_mult"],
                                               floor=stop, current=trail_stop)

    age = (_today() - date.fromisoformat(entry["date"])).days
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
    if resolution == "trail_hit":
        # A trail exit is scored on the REALISED move, not assumed a win:
        # a trail that ratchets and then gets hit below entry is still a
        # loss, and calling it a win would flatter the record.
        if pct >= MOVE_THRESHOLD:
            return ("WIN — ATR trailing stop, gains locked" if approved
                    else "MISSED GAIN — the trail would have banked it")
        if pct <= -MOVE_THRESHOLD:
            return ("LOSS — ATR trailing stop" if approved
                    else "GOOD SKIP — the trail would have cut it")
        return "flat (ATR trailing stop, went nowhere)"
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
    # Phase 2 (holy-grail plan §5.1): the entry's proposal-time evidence
    # snapshot joins its outcome in brain_map — the labeled row per-layer
    # reliability learns from. Pre-substrate entries (no stamp) skip
    # silently; a persistence failure never blocks resolution.
    try:
        from src.confluence.evidence import persist_entry_snapshot
        persist_entry_snapshot(brain, entry)
    except Exception:
        pass


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
             "time_stop": "TIME STOP", "trail_hit": "ATR TRAIL HIT",
             }.get(o["resolution"], str(o["resolution"]).upper())
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
             "ratchet_hit": "PROFIT RATCHET HIT (locked gain banked, #110)",
             "pre_expiry_exit": "PRE-EXPIRY EXIT (2-day gamma rule)",
             "trail_hit": "ATR TRAIL HIT (underlying ratchet)",
             EXPIRY_BACKSTOP_RESOLUTION: "EXPIRY BACKSTOP (wall-clock settlement)",
             }.get(o["resolution"], o["resolution"].upper())
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

        # Gap 4: STOCK fills pay the liquidity-tier slippage (was 0.0%).
        entry_slippage = apply_slippage(entry["price"], instrument_type,
                                        symbol=entry.get("ticker")) * entry["shares"]
        exit_slippage = apply_slippage(exit_price, instrument_type,
                                       symbol=entry.get("ticker")) * entry["shares"]
        total_slippage = entry_slippage + exit_slippage

        gross_pnl = entry["shares"] * (exit_price - entry["price"])
        net_pnl_rs = round(gross_pnl - total_frictions - total_slippage, 2)

        entry["outcome"] = {
            "checked": date.today().isoformat(),
            "settled_at": datetime.now().isoformat(timespec="seconds"),  # wall clock (Issue 31)
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
        tr = observe_tranches(entry, bars, exit_day, exit_price,
                              base_lots=int(entry["shares"]), unit=1)
        if tr is not None:
            entry["outcome"]["tranches"] = tr
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
                # A trail exit is a CLOSE, not a stop_loss page: the
                # floor ratcheted above the plan's stop, so this is the
                # trade working, not the thesis failing.
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
        # A dead feed must not kill the sweep: the expiry backstop below is
        # exactly for the days the feed is dead (Issues 26/28).
        try:
            bars = _daily_bars(entry["ticker"], entry["date"])
        except Exception as e:
            print(f"Plan tracker: price feed error for {entry['ticker']} spread ({e}).")
            bars = []
        hit = _resolve_spread(entry, bars) if bars else None
        backstop = None
        # The bar walk only settles on a bar. Past expiry with no usable
        # exit (no bars, a gap over the exit window, or a "pre-expiry" hit
        # that landed on a POST-expiry bar) the wall clock takes over.
        if hit is None or hit[3] > spread["expiry"]:
            backstop = _expiry_backstop(entry, bars)
            if backstop is not None:
                hit = backstop[:4]
        if hit is None:
            if not bars:
                print(f"Plan tracker: no price data for {entry['ticker']} spread — will retry next run.")
            else:
                print(f"Plan tracker: {spread['strategy']} on {entry['ticker']} still live "
                      f"(expiry {spread['expiry']}).")
            continue

        resolution, m_exit, frac_left, exit_day = hit
        approved = entry["decision"] == "approved"
        qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
        m_entry = _spread_entry_mark(spread)
        gross_pnl = (m_exit - m_entry) * qty

        if backstop is not None:
            exit_close = backstop[4]
            if exit_close is None:
                # No price to cost an exit against: the mark is already the
                # full defined loss; frictions/slippage are not invented.
                total_frictions, total_slippage = 0.0, 0.0
            else:
                total_frictions, total_slippage = _spread_exit_costs(spread, float(exit_close), 0.0)
        else:
            _day, _low, _high, exit_close = bars[[b[0] for b in bars].index(exit_day)]
            total_frictions, total_slippage = _spread_exit_costs(spread, float(exit_close), frac_left)
        execution = None
        if backstop is None and approved:
            # Decision #103: the exit is an OMS ticket the paper venue fills
            # at the modeled per-leg exit premium (+ tier slippage). Filled
            # -> the venue's basket mark is the exit and the exit-side
            # ladder is skipped; anything else -> the modeled exit above.
            limits = {(float(l["strike"]), str(l.get("option_type") or "").upper()):
                      round(_leg_model_premium(l, float(exit_close), spread.get("entry_spot"),
                                               frac_left), 2)
                      for l in spread["legs"]}
            execution = _execute_paper_exit(entry, limits, resolution,
                                            today=date.fromisoformat(exit_day))
            if execution.get("venue_slippage_ps") is not None:
                total_frictions, total_slippage = _spread_exit_costs(
                    spread, float(exit_close), frac_left, exit_slipped=True)
                total_slippage = round(total_slippage
                                       + float(execution["venue_slippage_ps"]) * qty, 2)
                total_frictions = round(total_frictions, 2)
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
            "settled_at": datetime.now().isoformat(timespec="seconds"),  # wall clock (Issue 31)
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
        }
        if execution is not None and (execution.get("mode") == "paper_venue"
                                      or execution.get("error")):
            # #103: the exit ticket record. `exit_basis` keeps describing
            # the PRICE basis (bars / intraday chain); the venue's cost sits
            # inside slippage_rs and is itemised here.
            entry["outcome"]["execution"] = execution
            if execution.get("venue_slippage_ps") is not None:
                entry["outcome"]["venue_slippage_rs"] = round(
                    float(execution["venue_slippage_ps"]) * qty, 2)
        if backstop is not None:
            # Additive keys (older rows lack them; readers tolerate absence):
            # WHICH price settled the row, and from WHICH session.
            entry["outcome"]["settlement_basis"] = backstop[5]
            entry["outcome"]["settlement_close_date"] = backstop[6]
            entry["outcome"]["settled_on"] = _today().isoformat()
        tr = observe_tranches(entry, bars, exit_day, m_exit,
                              base_lots=int(spread.get("lots", 1)),
                              unit=int(spread["lot_size"]))
        if tr is not None:
            entry["outcome"]["tranches"] = tr
        entry["outcome"]["verdict"] = _spread_verdict(entry, resolution, pnl_net, capture_pct)
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
