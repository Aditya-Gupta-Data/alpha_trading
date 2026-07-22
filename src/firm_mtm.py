"""
src/firm_mtm.py — Dept 6: firm-level MTM + return line (#84, Directive 6)
=========================================================================

Owner's last pre-break ask: the firm's Mark-to-Market and its growth
rate, prominently on the EOD/CEO digests. READ-ONLY by hard constraint —
this module computes and renders; it imports no sizing, treasury or
execution logic and mutates nothing.

MTM = realized firm equity (the account: starting capital + settled P&L)
    + unrealized options (open spreads marked via portfolio_report's
      snapshot-first ladder — the one shared mark source, #56)
    + unrealized equity (open funded darlings marked via the desk's own
      live_quote — scrip-master ids, absent quote = unmarked, counted).

THE DAY-1 EDGE (owner's own framing, matching the performance.py #72
doctrine — no verdicts on thin samples): annualizing a 2-day return is
theatre, so below CAGR_MIN_DAYS (30) the card shows the honest ABSOLUTE
return with a "day N" stamp and says when CAGR unlocks; from day 30 the
true CAGR = (MTM/base)^(365/days) − 1 appears beside the absolute
number. Base capital and the run-start date are DERIVED — the account's
starting_capital and its `clean_sheet` event (fallback: created_at) —
never hard-coded, so the next capital reset re-bases both automatically.

Partial marks are stated, never hidden: an unmarked position makes the
MTM line say so.
"""
from datetime import datetime, timedelta, timezone

from src import portfolio_manager as pm

IST = timezone(timedelta(hours=5, minutes=30))
CAGR_MIN_DAYS = 30


def _connect(conn):
    if conn is not None:
        return conn, False
    from src import brain_map
    return brain_map.connect(), True


def _run_start(conn) -> str:
    """The measurement epoch: the latest clean-sheet reset, else the
    account's birth."""
    row = conn.execute(
        "SELECT ts FROM account_events WHERE event_type = 'clean_sheet' "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        return str(row[0])
    return str(pm.get_account(conn)["created_at"])


def _options_unrealized(entries=None, marks=None):
    """(unrealized_rs, marked, open) for the open options book, via the
    one shared mark ladder. Injectable for tests."""
    try:
        from src.portfolio_report import _open_entries, get_live_marks
        spreads, equities = _open_entries(entries)
        open_count = len(spreads) + len(equities)
        if marks is None:
            from src.equity_desk import market_data_muzzled
            if market_data_muzzled():
                return None, 0, open_count   # test envs never fetch marks
            marks, _src = get_live_marks(entries=spreads + equities)
        return (round(sum(m["live_pnl_rs"] for m in marks), 2),
                len(marks), open_count)
    except Exception:
        return None, 0, 0


def _equity_unrealized(ledger_path=None, quote_fn=None):
    """(unrealized_rs, marked, open) for the funded darling book."""
    try:
        from src import knowledge_graph_logger as kg
        from src.equity_desk import live_quote
        quote_fn = quote_fn or live_quote
        total, marked, open_count = 0.0, 0, 0
        for ticker, entry in kg.open_positions(path=ledger_path).items():
            funding = entry.get("funding") or {}
            if not funding.get("funded"):
                continue
            open_count += 1
            action = entry.get("kya_kara_action") or {}
            try:
                last = quote_fn(ticker)
            except Exception:
                last = None
            if last is None or action.get("entry_price") is None:
                continue
            total += (float(last) - float(action["entry_price"])) \
                * int(funding.get("qty") or 0)
            marked += 1
        return round(total, 2), marked, open_count
    except Exception:
        return None, 0, 0


def compute(conn=None, entries=None, marks=None, ledger_path=None,
            quote_fn=None, now=None) -> dict:
    """Everything the card line needs, honestly labeled."""
    conn, owns = _connect(conn)
    try:
        acct = pm.account_summary(conn)
        start = _run_start(conn)
    finally:
        if owns:
            conn.close()
    base = float(acct["starting_capital"])
    opt_u, opt_marked, opt_open = _options_unrealized(entries, marks)
    eq_u, eq_marked, eq_open = _equity_unrealized(ledger_path, quote_fn)
    mtm = round(float(acct["equity"]) + (opt_u or 0.0) + (eq_u or 0.0), 2)
    unmarked = (opt_open - opt_marked) + (eq_open - eq_marked)
    now = now or datetime.now(IST)
    try:
        started = datetime.fromisoformat(start)
        if started.tzinfo is None:
            days = (now.replace(tzinfo=None) - started).days
        else:
            days = (now - started).days
    except (ValueError, TypeError):
        days = None
    days = max(days, 0) if days is not None else None
    abs_return = (mtm - base) / base if base > 0 else None
    cagr = None
    if (days is not None and days >= CAGR_MIN_DAYS
            and abs_return is not None and mtm > 0):
        cagr = (mtm / base) ** (365.0 / days) - 1
    return {"base": base, "equity_realized": float(acct["equity"]),
            "options_unrealized": opt_u, "equity_unrealized": eq_u,
            "mtm": mtm, "unmarked": unmarked, "days": days,
            "abs_return": abs_return, "cagr": cagr}


def render_line(m: dict = None, **kwargs) -> str:
    """The one prominent line both digests carry."""
    try:
        if m is None:
            m = compute(**kwargs)
        pct = (f"{m['abs_return']:+.2%}"
               if m["abs_return"] is not None else "n/a")
        parts = [f"💹 Firm MTM Rs.{m['mtm']:,.0f} "
                 f"(base Rs.{m['base']:,.0f} · realized "
                 f"Rs.{m['equity_realized']:,.0f}"
                 + (f" · options unreal {m['options_unrealized']:+,.0f}"
                    if m["options_unrealized"] is not None else "")
                 + (f" · equity unreal {m['equity_unrealized']:+,.0f}"
                    if m["equity_unrealized"] is not None else "")
                 + ")"]
        day = f"day {m['days']}" if m["days"] is not None else "day ?"
        if m["cagr"] is not None:
            parts.append(f"Absolute {pct} · CAGR {m['cagr']:+.1%} ({day})")
        else:
            parts.append(f"Absolute return {pct} ({day} — CAGR unlocks "
                         f"at day {CAGR_MIN_DAYS}; annualizing a "
                         f"days-old number would be noise)")
        if m["unmarked"]:
            parts.append(f"{m['unmarked']} position(s) unmarked — MTM "
                         f"partial")
        return "\n".join(parts)
    except Exception as exc:
        return f"💹 Firm MTM unavailable ({exc})"


if __name__ == "__main__":
    print(render_line())
