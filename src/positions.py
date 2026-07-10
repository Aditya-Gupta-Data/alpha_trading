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
        if e.get("spread"):
            open_positions.append(_from_spread_entry(e, today))
        elif (e.get("plan") or {}).get("stop_loss"):
            open_positions.append(_from_equity_entry(e, today))
        # entries with neither a spread nor a stop-carrying plan are not
        # tracker-managed positions (pre-4B lines, exits) — not "open".
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
