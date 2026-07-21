"""
src/equity_desk.py — Dept 3: the equity desk, VM-NATIVE (decision #83)
======================================================================

Owner override 2026-07-21 ("Break the Code Freeze & Execute the VM-Shift
… one unified database … live equity trading"): the desk now lives ON THE
VM, INSIDE the one firm database (`brain_map.db`), trading at LIVE marks
beside the options desk. The Mac remains the ANALYSIS side only — it
ships artifacts nightly (tier table, pricer levels, weekly scrip ids);
every rupee decision happens here.

THE UNIFIED LEDGER MODEL (supersedes #79's separate desk file and #80's
two-account/two-phase machinery — both existed only because the desks
sat on different machines):
  * ONE account — the firm's Phase-6G pool. Equity entries lock delivery
    notional through the SAME `pm.request_entry` door options margin
    uses; settlements release through the same `pm.release_margin`.
    Firm-level halts (10% ruin, daily 3% breaker) gate both desks at
    that one door. Double-spend is impossible by construction: one cash
    pool, one atomic gate.
  * Desk identity = the `eqd:` lock prefix. Deployed capital, realized
    P&L, and the desk's own books are VIEWS over tagged lock rows —
    never a second table of money.
  * The desk's ALLOCATION is the treasury's `equity_budget_rs`
    (`firm_treasury.get_budget` — a routed budget, not a lock): desk
    capital = budget + desk realized P&L; entries must fit inside it.
  * DESK RUIN HALT (the #79-era per-desk protection, kept): desk
    realized P&L at or below −10% of the budget blocks NEW desk entries
    (`equity_desk_ruin_halt` event) while options trade on.

LIVE TRADING (`run_darling_live_cycle`, wired into the VM market loop at
master_scheduler's composition root): exits first — open shadows marked
at LIVE quotes (stop/target/time via the proposer's own resolver, so
autopsies stay identical), Strong-Sell force-exits, settlements freed
into firm cash — then entries: a Buy-tier name whose LIVE price sits
INSIDE the strict buy zone (never the near-zone band) enters at that
quote, `fill_basis:"live"`. The tier table is freshness-gated
(`TIERS_MAX_AGE_DAYS`): stale analysis means no NEW entries, exits
always run.

QUOTES: darlings are non-F&O names outside SECURITY_ID_MAP — ids come
from `data/darling_ids.json` (built weekly ON THE MAC from Dhan's public
scrip master by `scrip_master.build_darling_ids`, shipped nightly;
exact-match only, never fuzzy — the #78 doctrine). A missing/stale id
file or absent quote = that name is unmarked/skipped, never guessed.

Telemetry contract unchanged: funding failures and vetoes still log the
zero-capital row ("log the false positives" survives every migration);
`equity_shadow_proposer` still imports nothing from Dept 3.

CLI:
    python3 -m src.equity_desk            # desk state + open book
    python3 -m src.equity_desk --sweep    # reconcile orphan locks
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import portfolio_manager as pm
from src.config import (EQUITY_DESK_ENABLED, EQUITY_DESK_MAX_NOTIONAL_PCT,
                        EQUITY_DESK_RISK_PER_TRADE_PCT,
                        MAX_RISK_PER_TRADE_RS)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
DARLING_IDS_PATH = ROOT / "data" / "darling_ids.json"

# 2026 delivery friction stack (April-2026 rates). The options rates live
# in src/portfolio.py — delivery differs on every line: STT hits BOTH
# sides at 0.1%, stamp duty is 5x the options rate, discount brokerage is
# zero, and the depository debits a flat charge per sell.
STT_RATE_DELIVERY = 0.001          # 0.1% of turnover, buy AND sell
STAMP_DUTY_RATE_BUY = 0.00015      # 0.015% buy side
EXCHANGE_CHARGE_RATE = 0.0000297   # NSE equity transaction charge
SEBI_FEE_RATE = 0.000001           # 0.0001% turnover fee
GST_RATE = 0.18                    # on service charges only
DP_CHARGE_SELL = 16.0              # flat depository debit per sell

LOCK_PREFIX = "eqd:"
DESK_RUIN_PCT = 10.0               # mirrors pm.MAX_DRAWDOWN_PCT, per desk
TIERS_MAX_AGE_DAYS = 3             # stale analysis = no NEW entries
IDS_MAX_AGE_DAYS = 14              # stale id file = unmarked, never guessed


def _connect(conn):
    """(conn, owns) — brain_map's connection unless the caller injected
    one. The desk holds NO database of its own (decision #83)."""
    if conn is not None:
        return conn, False
    from src import brain_map
    return brain_map.connect(), True


# ------------------------------------------------------------ desk views

def desk_state(conn=None) -> dict:
    """The desk's books as a VIEW over the firm account's tagged locks."""
    conn, owns = _connect(conn)
    try:
        pm.ensure_schema(conn)
        from src.firm_treasury import get_budget
        budget = get_budget(conn)
        deployed = float(conn.execute(
            "SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks WHERE "
            "released_at IS NULL AND journal_ref LIKE ?",
            (LOCK_PREFIX + "%",)).fetchone()[0])
        realized = float(conn.execute(
            "SELECT COALESCE(SUM(pnl_net), 0) FROM margin_locks WHERE "
            "released_at IS NOT NULL AND journal_ref LIKE ?",
            (LOCK_PREFIX + "%",)).fetchone()[0])
        open_locks = conn.execute(
            "SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL "
            "AND journal_ref LIKE ?", (LOCK_PREFIX + "%",)).fetchone()[0]
        capital = round(budget + realized, 2)
        return {"budget": budget, "deployed": round(deployed, 2),
                "realized": round(realized, 2), "capital": capital,
                "available": round(capital - deployed, 2),
                "open_locks": open_locks,
                "ruin_halted": realized <= -(DESK_RUIN_PCT / 100.0) * budget,
                "firm_halted": pm.trading_halted(conn)}
    finally:
        if owns:
            conn.close()


def delivery_frictions(side: str, price: float, qty: int) -> float:
    """Cost of one executed delivery leg at the 2026 rates."""
    turnover = float(price) * int(qty)
    stt = STT_RATE_DELIVERY * turnover
    stamp = STAMP_DUTY_RATE_BUY * turnover if side.upper() == "BUY" else 0.0
    services = (EXCHANGE_CHARGE_RATE + SEBI_FEE_RATE) * turnover
    dp = DP_CHARGE_SELL if side.upper() == "SELL" else 0.0
    return round(stt + stamp + services * (1 + GST_RATE) + dp, 2)


def size_entry(entry_price: float, stop: float, desk_capital: float,
               risk_pct: float = None) -> dict:
    """Whole-share qty from the risk budget, capped by the notional
    ceiling. qty 0 (with the reason) when nothing responsibly fits.
    `risk_pct` overrides the config default (the adaptive-sizing seam) —
    the notional ceiling binds regardless, so no boost can escape the
    desk's max risk cap."""
    risk_per_share = float(entry_price) - float(stop)
    if risk_per_share <= 0:
        return {"qty": 0, "notional": 0.0, "reason": "non-positive risk"}
    if risk_pct is None:
        risk_pct = EQUITY_DESK_RISK_PER_TRADE_PCT
    # Owner hard cap (decision #84): no single trade may risk more than
    # MAX_RISK_PER_TRADE_RS, whatever the percentage sizing says.
    risk_budget = min(desk_capital * risk_pct / 100.0, MAX_RISK_PER_TRADE_RS)
    qty = int(risk_budget // risk_per_share)
    cap = desk_capital * EQUITY_DESK_MAX_NOTIONAL_PCT / 100.0
    qty = min(qty, int(cap // float(entry_price)))
    if qty <= 0:
        return {"qty": 0, "notional": 0.0,
                "reason": "sizing zero (risk budget / notional cap)"}
    return {"qty": qty, "notional": round(qty * float(entry_price), 2),
            "reason": "sized"}


# --------------------------------------------------------- fund / settle

def fund_entry(entry: dict, conn=None) -> dict:
    """The capital_fn seam: adaptive consult → sizing on desk capital →
    desk ruin halt → desk budget → the firm's ONE entry door
    (pm.request_entry: cash + firm halts). Fails CLOSED; the caller logs
    its telemetry row regardless of the verdict."""
    if not EQUITY_DESK_ENABLED:
        return {"funded": False, "reason": "equity desk disabled"}
    action = entry.get("kya_kara_action") or {}
    price, stop = action.get("entry_price"), action.get("stop")
    if price is None or stop is None:
        return {"funded": False, "reason": "entry missing price/stop"}
    mult = 1.0
    try:
        from src import adaptive_sizing
        verdict = adaptive_sizing.equity_verdict(entry)
        if verdict.get("action") == "veto":
            return {"funded": False,
                    "reason": f"vetoed_by adaptive_sizing: "
                              f"{verdict.get('detail')}"}
        mult = float(verdict.get("multiplier", 1.0)) or 1.0
    except Exception:
        mult = 1.0
    try:
        conn, owns = _connect(conn)
        try:
            state = desk_state(conn)
            if state["ruin_halted"]:
                pm.log_event(conn, "equity_desk_ruin_halt",
                             f"{entry.get('ticker')}: desk realized "
                             f"Rs.{state['realized']:,.2f} breaches "
                             f"{DESK_RUIN_PCT:g}% of budget — desk entries "
                             f"blocked")
                return {"funded": False, "reason": "equity desk ruin halt"}
            sized = size_entry(price, stop, state["capital"],
                               risk_pct=EQUITY_DESK_RISK_PER_TRADE_PCT * mult)
            if sized["qty"] <= 0:
                pm.log_event(conn, "sizing_zero",
                             f"{entry.get('ticker')}: {sized['reason']}")
                return {"funded": False, "reason": sized["reason"]}
            if sized["notional"] > state["available"]:
                reason = (f"equity budget exhausted: needs "
                          f"Rs.{sized['notional']:,.2f}, desk has "
                          f"Rs.{state['available']:,.2f} of its "
                          f"Rs.{state['budget']:,.0f} budget")
                pm.log_event(conn, "equity_budget_exhausted",
                             f"{entry.get('ticker')}: {reason}")
                return {"funded": False, "reason": reason}
            ref = LOCK_PREFIX + str(entry.get("id"))
            gate = pm.request_entry(conn, ref, sized["notional"])
            if not gate["approved"]:
                return {"funded": False, "reason": gate["reason"]}
            return {"funded": True, "qty": sized["qty"],
                    "notional": sized["notional"], "lock_ref": ref,
                    "reason": ("funded" if mult == 1.0
                               else f"funded (sizing x{mult})")}
        finally:
            if owns:
                conn.close()
    except Exception as exc:
        return {"funded": False, "reason": f"desk unavailable ({exc})"}


def settle_exit(entry: dict, exit_event: dict, conn=None):
    """Net delivery P&L (both-side frictions) settles into the FIRM
    account; the lock releases; the desk's realized view moves with the
    tagged row. None for unfunded entries / already-released refs."""
    funding = entry.get("funding") or {}
    qty = funding.get("qty")
    price_in = (entry.get("kya_kara_action") or {}).get("entry_price")
    price_out = exit_event.get("exit_price")
    if (not funding.get("funded") or not qty
            or price_in is None or price_out is None):
        return None
    gross = (float(price_out) - float(price_in)) * int(qty)
    pnl_net = round(gross - delivery_frictions("BUY", price_in, qty)
                    - delivery_frictions("SELL", price_out, qty), 2)
    conn, owns = _connect(conn)
    try:
        ref = funding.get("lock_ref") or LOCK_PREFIX + str(entry.get("id"))
        result = pm.release_margin(conn, ref, pnl_net)
        if not result.get("released"):
            return None
        return {"ticker": exit_event.get("ticker"), "lock_ref": ref,
                "qty": int(qty), "pnl_net": pnl_net,
                "reason": exit_event.get("reason"),
                "equity": result.get("equity"),
                "halted": result.get("halted")}
    finally:
        if owns:
            conn.close()


def sweep_orphan_locks(ledger_path=None, conn=None) -> list:
    """Reconciler: a settle that crashed mid-run leaves an exited position
    still locked. Re-drive settlement for every active eqd: lock whose
    ledger entry already carries an exit."""
    from src import knowledge_graph_logger as kg
    events = kg.read_events(ledger_path)
    entries = {e.get("id"): e for e in events if e.get("event") == "entry"}
    exits = {e.get("id"): e for e in events if e.get("event") == "exit"}
    conn, owns = _connect(conn)
    try:
        active = [r[0] for r in conn.execute(
            "SELECT journal_ref FROM margin_locks WHERE released_at IS NULL "
            "AND journal_ref LIKE ?", (LOCK_PREFIX + "%",)).fetchall()]
        settled = []
        for ref in active:
            eid = ref[len(LOCK_PREFIX):]
            if eid in exits and eid in entries:
                s = settle_exit(entries[eid], exits[eid], conn=conn)
                if s:
                    settled.append(s)
        return settled
    finally:
        if owns:
            conn.close()


# --------------------------------------------------- ids + live quotes

def security_id_for(symbol: str, ids_path=None):
    """The scrip-master id for a darling symbol, from the weekly Mac-built
    artifact. None when the file is absent, stale past IDS_MAX_AGE_DAYS,
    or the symbol unresolved — an unmapped name is unmarked, never
    guessed (#78)."""
    p = Path(ids_path) if ids_path else DARLING_IDS_PATH
    try:
        data = json.loads(p.read_text())
        built = datetime.fromisoformat(data["built_at"])
        now = (datetime.now(IST).replace(tzinfo=None)
               if built.tzinfo is None else datetime.now(IST))
        if (now - built).days > IDS_MAX_AGE_DAYS:
            return None
        return (data.get("ids") or {}).get(
            symbol.replace(".NS", ""), {}).get("id")
    except Exception:
        return None


def live_quote(ticker: str, ids_path=None, quote_by_id_fn=None):
    """LIVE price for a darling via its scrip-master id. None = honest
    absence (no id / no quote); raises never."""
    sid = security_id_for(ticker, ids_path=ids_path)
    if sid is None:
        return None
    try:
        if quote_by_id_fn is None:
            from src.dhan_client import get_live_price_by_id as quote_by_id_fn
        return quote_by_id_fn(sid)
    except Exception:
        return None


def _tiers_fresh(tiers_path=None, now=None) -> bool:
    """No NEW entries on a stale tier table — the Mac's nightly analysis
    ship is the desk's eyes; blind means hold, never guess."""
    from src.equity_shadow_proposer import TIERS_PATH
    p = Path(tiers_path) if tiers_path else TIERS_PATH
    try:
        as_of = json.loads(p.read_text()).get("as_of")
        age = ((now or datetime.now(IST)).date()
               - datetime.fromisoformat(as_of).date()).days
        return age <= TIERS_MAX_AGE_DAYS
    except Exception:
        return False


# ------------------------------------------------------- the live cycle

def run_darling_live_cycle(tiers_path=None, levels_path=None, path=None,
                           conn=None, quote_fn=None, check_fn=None,
                           universe=None, now=None, vix_fn=None,
                           broadcast_fn=None) -> dict:
    """One VM market-hours desk cycle (decision #83): live exits →
    Strong-Sell force-exits → settlements into firm cash → live entries
    (quote INSIDE the strict buy zone, tier table fresh). Runs beside the
    block-leg shadow in the market loop; every stage fail-opens."""
    from src import equity_shadow_proposer as sp
    from src import knowledge_graph_logger as kg
    quote_fn = quote_fn or live_quote
    exits = sp.track_open_shadows(quote_fn=quote_fn, vix_fn=vix_fn,
                                  universe=universe or {},
                                  path=path, now=now)
    fresh = _tiers_fresh(tiers_path, now=now)
    if fresh:
        exits += sp.force_exit_strong_sell(tiers_path=tiers_path, path=path,
                                           quote_fn=quote_fn, now=now)
    settlements = []
    if exits:
        hosts = {e.get("id"): e for e in kg.read_events(path)
                 if e.get("event") == "entry"}
        for x in exits:
            host = hosts.get(x.get("id"))
            if not host or not (host.get("funding") or {}).get("funded"):
                continue
            try:
                s = settle_exit(host, x, conn=conn)
            except Exception as exc:
                print(f"  (darling settlement failed for "
                      f"{x.get('ticker')}: {exc}")
                continue
            if s:
                settlements.append(s)
    entries = []
    if fresh:
        entries = sp.propose_darling_entries(
            tiers_path=tiers_path, levels_path=levels_path, path=path,
            check_fn=check_fn, universe=universe,
            capital_fn=lambda e: fund_entry(e, conn=conn),
            quote_fn=quote_fn, fill_basis="live")
    result = {"entries": entries, "exits": exits,
              "settlements": settlements, "tiers_fresh": fresh}
    broadcast_activity(result, conn=conn, broadcast_fn=broadcast_fn)
    return result


def broadcast_activity(cycle: dict, conn=None, broadcast_fn=None) -> bool:
    """ONE card per cycle, only when money moved (funded entries and/or
    settlements). Quiet cycles stay silent — abstain = silent."""
    funded = [e for e in cycle.get("entries") or []
              if (e.get("funding") or {}).get("funded")]
    settlements = cycle.get("settlements") or []
    if not funded and not settlements:
        return False
    try:
        if broadcast_fn is None:
            from src.notifier import fire_broadcast
            broadcast_fn = fire_broadcast
        lines = []
        for e in funded:
            f = e["funding"]
            lines.append(f"BUY {e['ticker']}: {f['qty']} sh "
                         f"≈ Rs.{f['notional']:,.0f} (live fill)")
        for s in settlements:
            sign = "+" if s["pnl_net"] >= 0 else ""
            lines.append(f"EXIT {s['ticker']} ({s['reason']}): "
                         f"{sign}Rs.{s['pnl_net']:,.2f} net")
        state = desk_state(conn)
        lines.append(f"Desk: capital Rs.{state['capital']:,.0f} "
                     f"(budget Rs.{state['budget']:,.0f}), "
                     f"deployed Rs.{state['deployed']:,.0f}, "
                     f"realized Rs.{state['realized']:,.2f}"
                     + (" — ⛔ DESK HALTED" if state["ruin_halted"] else ""))
        broadcast_fn({"event": "equity_desk", "ticker": "EQUITY DESK",
                      "date": datetime.now(IST).date().isoformat(),
                      "description": ("💼 Equity desk (paper, live):\n"
                                      + "\n".join(lines))})
        return True
    except Exception as exc:
        print(f"  (equity desk card failed: {exc})")
        return False


# ------------------------------------------------------------- the view

def render_book_lines(conn=None, path=None, quote_fn=None) -> str:
    """The equity section every report card appends — desk state + the
    open funded book marked at live quotes (an absent quote shows '—',
    never a guess). All VM-local; no snapshot, no SSH (decision #83)."""
    try:
        from src import knowledge_graph_logger as kg
        state = desk_state(conn)
        quote_fn = quote_fn or live_quote
        positions = []
        for ticker, entry in sorted(kg.open_positions(path=path).items()):
            funding = entry.get("funding") or {}
            if not funding.get("funded"):
                continue
            action = entry.get("kya_kara_action") or {}
            try:
                last = quote_fn(ticker)
            except Exception:
                last = None
            qty = int(funding.get("qty") or 0)
            unreal = (round((float(last) - float(action["entry_price"]))
                            * qty, 2)
                      if last is not None
                      and action.get("entry_price") is not None else None)
            positions.append((ticker, qty, action.get("entry_price"),
                              last, unreal))
        marked = [u for *_, u in positions if u is not None]
        head = (f"EQUITY DESK (live): {len(positions)} open · "
                f"deployed Rs.{state['deployed']:,.0f} · "
                f"desk cash Rs.{state['available']:,.0f} · "
                f"budget Rs.{state['budget']:,.0f} · "
                f"realized {'+' if state['realized'] >= 0 else ''}"
                f"Rs.{state['realized']:,.2f}")
        if marked:
            total = sum(marked)
            head += f" · unrealized {'+' if total >= 0 else ''}Rs.{total:,.0f}"
        if positions and len(marked) < len(positions):
            head += f" ({len(positions) - len(marked)} unmarked)"
        if state["ruin_halted"]:
            head += " · ⛔ DESK HALTED"
        if not positions:
            return head
        rows = ["TICKER      QTY    ENTRY     LAST      P&L"]
        for ticker, qty, entry_px, last, unreal in positions:
            t = str(ticker).replace(".NS", "")[:10]
            rows.append(f"{t:<10} {qty:>4} {entry_px:>8} "
                        f"{last if last is not None else '—':>8} "
                        f"{f'{unreal:+,.0f}' if unreal is not None else '—':>8}")
        return head + "\n```\n" + "\n".join(rows) + "\n```"
    except Exception as exc:
        return f"EQUITY DESK: view unavailable ({exc})"


if __name__ == "__main__":
    import sys
    if "--sweep" in sys.argv:
        swept = sweep_orphan_locks()
        print(f"swept {len(swept)} orphan lock(s)")
        for s in swept:
            print(" ", s)
    else:
        print(render_book_lines())
