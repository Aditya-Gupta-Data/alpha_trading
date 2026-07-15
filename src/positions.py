"""
src/positions.py — read-only active-trade visibility (single source of truth)
=============================================================================

Answers one question three ways: "what paper positions are open right
now?" — for the terminal (src/view_positions.py), the gateway
(GET /api/discord/positions), and the Discord bot's /positions command.

The source of truth is data/journal.jsonl — the same store the plan
tracker resolves against: an OPEN position is an entry with
decision == "approved" and outcome == null, carrying either a 4B plan
(equity swing, has stop_loss/target prices) or a spread block (options,
has expiry/max_loss/max_profit). This mirrors plan_tracker's own two
"still open" predicates exactly, so what this module shows and what the
tracker will eventually resolve can never disagree.

(Deliberately NOT the simulator's `simulated_trades` table — that is the
Phase 7 backtest corpus, not the live paper book.)

STRICTLY READ-ONLY: this module only reads the journal file (plus an
optional injected entries list). It never writes, never locks
brain_map.db, never imports the proposer/tracker mutation paths.

Terminal check (quick SSH view):

    python3 -m src.view_positions
"""

from datetime import date, datetime, timedelta, timezone

from src import journal
# The tracker's own "still open" predicates — imported, not copied, so
# this view and the tracker can never drift apart (the whole point of
# this module per the docstring above).
from src.plan_tracker import _spread_trackable, _trackable

IST = timezone(timedelta(hours=5, minutes=30))


def _days_in_trade(opened_on: str, today: date = None) -> int | None:
    """Whole days since entry (0 = opened today). None if undated."""
    try:
        opened = date.fromisoformat((opened_on or "")[:10])
    except ValueError:
        return None
    today = today or datetime.now(IST).date()
    return max(0, (today - opened).days)


def _from_spread_entry(e: dict, today: date = None) -> dict:
    s = e.get("spread") or {}
    lots = int(s.get("lots", 1))
    net = (s.get("net_credit") if s.get("net_credit") is not None
           else s.get("net_debit"))
    return {
        "trade_id": e.get("short_id"),
        "ticker": e.get("ticker"),
        "kind": "spread",
        "strategy": s.get("strategy"),
        "direction": s.get("direction"),  # stamped by StrategyConstructor;
                                          # None on pre-stamp legacy lines
        "entry_price": net,               # net credit/debit per share
        "target": None,                   # spreads bound P&L structurally:
        "stop_loss": None,                # max_profit / max_loss below
        "max_profit_rs": (s.get("max_profit") or 0) * lots,
        "max_loss_rs": (s.get("max_loss") or 0) * lots,
        "expiry": s.get("expiry"),
        "lots": lots,
        "opened_on": e.get("date"),
        "days_in_trade": _days_in_trade(e.get("date"), today),
        "signal": e.get("signal"),
    }


def _from_equity_entry(e: dict, today: date = None) -> dict:
    plan = e.get("plan") or {}
    return {
        "trade_id": e.get("short_id"),
        "ticker": e.get("ticker"),
        "kind": "equity",
        "strategy": plan.get("variant") or "swing",
        "entry_price": e.get("price"),
        "target": plan.get("target"),
        "stop_loss": plan.get("stop_loss"),
        "max_profit_rs": None,
        "max_loss_rs": plan.get("max_loss_rs"),
        "expiry": None,
        "lots": None,
        "opened_on": e.get("date"),
        "days_in_trade": _days_in_trade(e.get("date"), today),
        "signal": e.get("signal"),
    }


def active_positions(entries: list = None, today: date = None) -> list:
    """Every open approved paper position, newest first. `entries` and
    `today` are injectable for offline tests; by default the journal
    file is read (a plain file read — no DB, no locks)."""
    if entries is None:
        entries = journal.read_all()
    open_positions = []
    for e in entries:
        if e.get("decision") != "approved" or e.get("outcome") is not None:
            continue
        if e.get("spread") and _spread_trackable(e):
            open_positions.append(_from_spread_entry(e, today))
        elif _trackable(e):
            open_positions.append(_from_equity_entry(e, today))
        # entries with neither a trackable spread (legs + expiry) nor a
        # stop-carrying plan are not tracker-managed positions (pre-4B
        # lines, exits, malformed blocks) — not "open".
    open_positions.reverse()   # journal is append-ordered; show newest first
    return open_positions


# ------------------------------------------------------------- rendering

def _fmt_rs(value) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "-"


def _row(p: dict) -> list:
    if p["kind"] == "spread":
        target = f"max +{_fmt_rs(p['max_profit_rs'])}"
        stop = f"max -{_fmt_rs(p['max_loss_rs'])}"
    else:
        target = _fmt_rs(p["target"])
        stop = _fmt_rs(p["stop_loss"])
    days = p["days_in_trade"]
    return [
        str(p["trade_id"] or "-"),
        str(p["ticker"] or "-"),
        str(p["strategy"] or p["kind"]).replace("_", " "),
        _fmt_rs(p["entry_price"]),
        target,
        stop,
        str(p["expiry"] or "-"),
        f"{days}d" if days is not None else "-",
    ]


_HEADERS = ["ID", "TICKER", "STRATEGY", "ENTRY", "TARGET", "STOP",
            "EXPIRY", "IN TRADE"]


def format_table(positions: list) -> str:
    """The ASCII table for the terminal — pure string building, no I/O."""
    if not positions:
        return "No open paper positions."
    rows = [_row(p) for p in positions]
    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(_HEADERS)]
    def line(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [sep, line(_HEADERS), sep]
    out += [line(r) for r in rows]
    out.append(sep)
    out.append(f"{len(positions)} open position(s) — paper only.")
    return "\n".join(out)


# --- Discord code-block table (phone-scannable, monospace) ---------------
# Discord does NOT render pipe-style Markdown tables — a fenced code block
# with fixed-width columns is the clean aligned form. Compact by design so
# it fits a phone screen: NIFTY BANK -> BNF, bear_put_spread -> BPS, rupee
# amounts as 6.0k. Separate from format_table (terminal) on purpose — the
# terminal has width to spare; the phone card does not.

_DISCORD_TICKER_ABBR = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BNF"}
_DISCORD_STRAT_ABBR = {"bull_call_spread": "BCS", "bear_put_spread": "BPS",
                       "iron_condor": "IC", "iron_butterfly": "IB"}
_DISCORD_MAX_ROWS = 25


def _compact_rs(v) -> str:
    """795 -> '795', 6000 -> '6.0k', None -> '-'."""
    if not isinstance(v, (int, float)):
        return "-"
    return f"{v / 1000:.1f}k" if abs(v) >= 1000 else str(int(round(v)))


def _discord_row(p: dict) -> tuple:
    ticker = _DISCORD_TICKER_ABBR.get(p.get("ticker"),
                                      (p.get("ticker") or "?").replace(".NS", "")[:6])
    if p.get("kind") == "spread":
        strat = _DISCORD_STRAT_ABBR.get(p.get("strategy"),
                                        (p.get("strategy") or "?")[:3].upper())
        maxpl = f"+{_compact_rs(p.get('max_profit_rs'))}/-{_compact_rs(p.get('max_loss_rs'))}"
        expiry = (p.get("expiry") or "-")[5:] or "-"   # YYYY-MM-DD -> MM-DD
    else:
        strat = "EQ"
        maxpl = f"T{_compact_rs(p.get('target'))}/S{_compact_rs(p.get('stop_loss'))}"
        expiry = "-"
    entry = p.get("entry_price")
    entry = f"{entry:g}" if isinstance(entry, (int, float)) else "-"
    days = p.get("days_in_trade")
    return (ticker, strat, entry, expiry, maxpl,
            f"{days}d" if days is not None else "-")


_DISCORD_HEADERS = ("UNDER", "STRAT", "ENTRY", "EXPIRY", "MAX P/L", "DAYS")


def format_discord_table(positions: list) -> str:
    """A fenced code-block table for the Discord /positions card — the
    columns the owner asked for (Underlying, Strategy, Entry, Expiry, Max
    Profit/Loss, Days), aligned and phone-compact. Pure string building."""
    if not positions:
        return "No open paper positions."
    shown = positions[:_DISCORD_MAX_ROWS]
    rows = [_discord_row(p) for p in shown]
    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(_DISCORD_HEADERS)]

    def line(cells):
        # pad every column (last included) so the block is a clean rectangle
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    out = ["```", line(_DISCORD_HEADERS)] + [line(r) for r in rows] + ["```"]
    footer = f"{len(positions)} open — paper only."
    if len(positions) > len(shown):
        footer = (f"{len(shown)} of {len(positions)} shown "
                  "(Discord caps the card) — " + footer)
    out.append(footer)
    return "\n".join(out)
