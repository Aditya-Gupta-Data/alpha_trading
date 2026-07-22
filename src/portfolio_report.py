"""
src/portfolio_report.py — the 2-hour intraday portfolio report card
===================================================================

A cron-triggered, strictly READ-ONLY snapshot of the open paper book,
posted to Discord as one embed: open position count, the current top
winner and top loser (marked live), and portfolio exposure (locked SPAN
margin vs. account equity).

Reuses what already exists instead of re-deriving anything:
  * open positions      — the same journal predicates as src/positions.py
  * live spread marks   — live_bridge.evaluate_open_positions(): the plan
                          tracker's exact exit arithmetic (modeled mark,
                          no-arbitrage clamp) at the current spot
  * equity marks        — live price vs. entry price × shares
  * spots               — the hardened SafeDhanClient (Phase 1)
  * exposure            — plain SELECTs on brain_map.db opened with the
                          SQLite READ-ONLY URI (mode=ro): this job can
                          never write or lock the database. It calls no
                          portfolio_manager function (those ensure schema
                          = a write) — missing tables just mean "no data".

Cron contract (scripts/setup_cron.sh, every 2 hours): the SCRIPT decides
whether posting makes sense — outside NSE market hours (Mon-Fri
09:15-15:30 IST) it exits quietly instead of spamming the channel at
02:00, so the crontab line stays a simple `0 */2 * * *`. `--force` posts
regardless (manual checks).

Run manually from the project folder:

    python3 -m src.portfolio_report            # posts only if market open
    python3 -m src.portfolio_report --force    # posts right now
"""

import argparse
import sqlite3
from pathlib import Path

from src import journal
from src.live_bridge import evaluate_open_positions
from src.market_loop import is_market_open, ist_now
from src.plan_tracker import _spread_trackable, _trackable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "brain_map.db"


# ------------------------------------------------------------- open book

def _open_entries(entries: list = None) -> tuple:
    """(open_spread_entries, open_equity_entries) — the same "approved and
    unresolved" predicates positions.py and the tracker use."""
    if entries is None:
        entries = journal.read_all()
    spreads, equities = [], []
    for e in entries:
        if e.get("decision") != "approved" or e.get("outcome") is not None:
            continue
        if e.get("spread") and _spread_trackable(e):
            spreads.append(e)
        elif _trackable(e):
            equities.append(e)
    return spreads, equities


def _spread_detail(capture_pct, days_left) -> str:
    """THE one wording for a spread mark's detail — the dashboard and the
    report card must read identically for the same position."""
    return (f"{float(capture_pct or 0):.0f}% of max profit, "
            f"{days_left}d to expiry")


def mark_positions(spread_entries: list, equity_entries: list,
                   spot_fn) -> list:
    """Every open position marked live: [{short_id, ticker, strategy,
    live_pnl_rs, detail}]. Positions whose ticker has no quote this cycle
    are skipped (never guessed) — the embed reports how many."""
    marked = []
    spots = {}
    for e in spread_entries + equity_entries:
        t = e.get("ticker")
        if t not in spots:
            try:
                spots[t] = spot_fn(t)
            except Exception:
                spots[t] = None

    for sig in evaluate_open_positions(
            {t: s for t, s in spots.items() if s is not None},
            entries=spread_entries):
        marked.append({"short_id": sig["short_id"], "ticker": sig["ticker"],
                       "strategy": sig["strategy"],
                       "live_pnl_rs": sig["live_pnl_rs"],
                       "detail": _spread_detail(sig["capture_pct"],
                                                sig["days_left"])})
    for e in equity_entries:
        spot = spots.get(e.get("ticker"))
        if spot is None or not e.get("price") or not e.get("shares"):
            continue
        pnl = round((float(spot) - float(e["price"])) * float(e["shares"]), 2)
        marked.append({"short_id": e.get("short_id"), "ticker": e["ticker"],
                       "strategy": (e.get("plan") or {}).get("variant") or "swing",
                       "live_pnl_rs": pnl,
                       "detail": f"entry Rs.{e['price']} → Rs.{spot}"})
    return marked


def get_live_marks(entries: list = None, spot_fn=None,
                   max_age_seconds: float = None) -> tuple:
    """(marks list, source) — THE mark ladder every consumer shares (the
    dashboard's /api/web/positions, this report card, anything future):

      1. the engine's published snapshot (src.market_snapshot) first —
         the live loop already fetched those quotes, so these marks cost
         zero Dhan calls and never race the loop on the one shared token
         (decision #48);
      2. a direct SafeDhanClient fetch ONLY for open positions the
         snapshot didn't cover: equity swings (the loop marks spreads
         only) and everything when the snapshot is stale/absent (Mac,
         engine down — exactly when contention is nil).

    Returns ([{short_id, ticker, strategy, live_pnl_rs, detail}, ...],
    "engine_snapshot" | "direct_fetch" | "mixed" | None). Fail-safe:
    a broken snapshot read degrades to the direct path; a broken direct
    path returns whatever the snapshot gave."""
    from src import market_snapshot
    spreads, equities = _open_entries(entries)
    marks, covered, source = [], set(), None
    try:
        snap = market_snapshot.read(
            max_age_seconds=(market_snapshot.DEFAULT_MAX_AGE_SECONDS
                             if max_age_seconds is None else max_age_seconds))
    except Exception as e:
        print(f"  (marks: snapshot read failed: {e})")
        snap = None
    if snap is not None:
        by_id = market_snapshot.marks_by_id(snap)
        for e in spreads:
            m = by_id.get(e.get("short_id"))
            if m is None:
                continue
            marks.append({"short_id": m.get("short_id"),
                          "ticker": m.get("ticker"),
                          "strategy": m.get("strategy"),
                          "live_pnl_rs": m.get("live_pnl_rs"),
                          "detail": _spread_detail(m.get("capture_pct"),
                                                   m.get("days_left"))})
            covered.add(e.get("short_id"))
        if covered:
            source = "engine_snapshot"
    left_spreads = [e for e in spreads if e.get("short_id") not in covered]
    left_equities = [e for e in equities if e.get("short_id") not in covered]
    if left_spreads or left_equities:
        try:
            if spot_fn is None:
                from src.dhan_guard import SafeDhanClient
                spot_fn = SafeDhanClient().get_live_price
            direct = mark_positions(left_spreads, left_equities, spot_fn)
        except Exception as e:
            print(f"  (marks: direct fetch failed: {e})")
            direct = []
        if direct:
            marks.extend(direct)
            source = "mixed" if source else "direct_fetch"
    return marks, source


# -------------------------------------------------------------- exposure

def read_exposure(db_path: Path = None) -> dict | None:
    """Locked SPAN margin vs. account equity via read-only SELECTs.
    None when the capital tables don't exist yet (fresh DB) or the file
    is absent — the embed then simply omits the exposure field."""
    db_path = Path(db_path) if db_path is not None else DB_PATH
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            locked = conn.execute(
                "SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks "
                "WHERE released_at IS NULL").fetchone()[0]
            row = conn.execute(
                "SELECT starting_capital + realized_pnl, realized_pnl "
                "FROM account_state WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return None   # tables not created yet — nothing to report
    if row is None:
        return None
    equity = float(row[0])
    locked = float(locked)
    return {"locked_margin_rs": round(locked, 2),
            "equity_rs": round(equity, 2),
            "realized_pnl_rs": round(float(row[1]), 2),
            "exposure_pct": round(locked / equity * 100, 2) if equity else None}


# ----------------------------------------------------------------- embed

def _rs(v) -> str:
    return f"Rs.{v:+,.2f}" if v is not None else "n/a"


# --- report-card presentation (Discord code-block table) -----------------
# Discord does NOT render pipe-style Markdown tables — the clean, aligned
# result is a fenced code block (monospace, fixed-width columns). Kept
# self-contained to this report; the positions table (positions.py) owns
# its own so the two departments stay independently changeable.

_TICKER_ABBR = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BNF"}
_STRAT_ABBR = {"bull_call_spread": "BCS", "bear_put_spread": "BPS",
               "iron_condor": "IC", "iron_butterfly": "IB"}


def _abbr_ticker(t: str) -> str:
    return _TICKER_ABBR.get(t, (t or "?").replace(".NS", "")[:6])


def _abbr_strat(s: str) -> str:
    return _STRAT_ABBR.get(s, (s or "EQ")[:3].upper())


def _report_table(marked: list) -> str:
    """A fenced code-block table of the marked positions, sorted best P&L
    first (so the top row IS the winner and the last the loser). Columns:
    ticker · strategy · live P&L · note."""
    rows = [(_abbr_ticker(m["ticker"]), _abbr_strat(m.get("strategy")),
             f"{m['live_pnl_rs']:+,.0f}", m.get("detail") or "")
            for m in sorted(marked, key=lambda m: m["live_pnl_rs"],
                            reverse=True)]
    head = ("TICKER", "STRAT", "LIVE P&L", "NOTE")
    w0 = max(len(head[0]), *(len(r[0]) for r in rows))
    w1 = max(len(head[1]), *(len(r[1]) for r in rows))
    w2 = max(len(head[2]), *(len(r[2]) for r in rows))
    out = [f"{head[0]:<{w0}}  {head[1]:<{w1}}  {head[2]:>{w2}}  {head[3]}"]
    for t, s, p, n in rows:
        out.append(f"{t:<{w0}}  {s:<{w1}}  {p:>{w2}}  {n}")
    return "```\n" + "\n".join(out) + "\n```"


def build_report_payload(marked: list, open_count: int, unmarked: int,
                         exposure: dict | None, now,
                         equity_section: str = None) -> dict:
    """The broadcast_alert payload — pure function, tests pin its shape.

    Presentation is a single code-block TABLE of the marked positions plus
    one summary line, in the embed description (was: chunky stacked embed
    fields). Marks are the caller's — run() sources them snapshot-first
    (get_live_marks), so the table is the latest available price, never a
    stale EOD fetch."""
    summary_bits = [f"{open_count} open"]
    if marked:
        net = round(sum(m["live_pnl_rs"] for m in marked), 2)
        summary_bits.append(f"net live P&L {_rs(net)}")
    if exposure is not None and exposure.get("realized_pnl_rs") is not None:
        summary_bits.append(f"realized {_rs(exposure['realized_pnl_rs'])}")
    if exposure is not None and exposure.get("exposure_pct") is not None:
        summary_bits.append(
            f"exposure {exposure['exposure_pct']:.1f}% "
            f"(Rs.{exposure['locked_margin_rs']:,.0f}/"
            f"{exposure['equity_rs']:,.0f})")
    if unmarked:
        summary_bits.append(f"{unmarked} unmarked (no live quote)")

    description = ("Automated 2-hourly snapshot — read-only; the plan "
                  "tracker owns every exit. Paper only.\n"
                  + " · ".join(summary_bits))
    if marked:
        description += "\n" + _report_table(marked)
    # One-firm-view (decision #82): the equity desk's EOD book rides on
    # the same card — the owner sees ONE portfolio, never two ledgers.
    if equity_section:
        description += "\n" + equity_section

    return {
        "event": "portfolio_report",
        "ticker": "paper book",
        "date": now.date().isoformat(),
        "time": now.strftime("%Y-%m-%d %H:%M IST"),
        "description": description,
        "fields": [],
    }


def build_pnl_card(entries: list = None, spot_fn=None,
                   db_path: Path = None, summary: dict = None,
                   marked: list = None, mark_source=None,
                   now=None) -> dict:
    """The owner's on-demand P&L answer (2026-07-14 ask): REALIZED
    (banked, from the capital layer) + LIVE MARKED (open positions via
    the one shared mark ladder) + the honest total, in one phone-first
    text. Injectable summary/marks for tests; the live path reads the
    real account + marks. Never raises — every layer degrades to an
    honest absence line, never a guess."""
    from datetime import datetime
    now = now or datetime.now()
    if summary is None:
        try:
            from src import brain_map, portfolio_manager as pm
            conn = brain_map.connect(db_path)
            try:
                summary = pm.account_summary(conn)
            finally:
                conn.close()
        except Exception as e:
            summary = {"error": str(e)}
    if marked is None:
        try:
            marked, mark_source = get_live_marks(entries=entries,
                                                 spot_fn=spot_fn)
        except Exception:
            marked, mark_source = [], None

    realized = summary.get("realized_pnl")
    live_net = (round(sum(m["live_pnl_rs"] for m in marked), 2)
                if marked else None)
    total = (round((realized or 0.0) + (live_net or 0.0), 2)
             if realized is not None or live_net is not None else None)

    lines = [f"💰 **P&L — {now.strftime('%Y-%m-%d %H:%M IST')}** (paper)"]
    if realized is not None:
        lines.append(f"Realized (banked): {_rs(realized)}")
    else:
        lines.append(f"Realized: unavailable ({summary.get('error', '?')})")
    if live_net is not None:
        src_note = {"engine_snapshot": "engine snapshot",
                    "direct_fetch": "direct quotes",
                    "mixed": "snapshot + quotes"}.get(mark_source,
                                                      mark_source or "?")
        lines.append(f"Live marked ({len(marked)} open): {_rs(live_net)} "
                     f"[{src_note}]")
    else:
        lines.append("Live marked: no marks available "
                     "(market closed / no open positions / stale snapshot)")
    if total is not None:
        lines.append(f"**Total if all closed at marks: {_rs(total)}**")
    if summary.get("equity") is not None:
        lines.append(f"Equity Rs.{summary['equity']:,.2f} | free "
                     f"Rs.{summary.get('available_cash', 0):,.2f} | locked "
                     f"Rs.{summary.get('locked_margin', 0):,.2f} "
                     f"({summary.get('open_locks', 0)} open)")
    if summary.get("trading_halted"):
        lines.append("⛔ RISK-OF-RUIN HALT ACTIVE")
    if marked:
        w = max(marked, key=lambda m: m["live_pnl_rs"])
        l = min(marked, key=lambda m: m["live_pnl_rs"])
        lines.append(f"Best: {w['ticker']} {_rs(w['live_pnl_rs'])}"
                     + (f" · Worst: {l['ticker']} {_rs(l['live_pnl_rs'])}"
                        if l["short_id"] != w["short_id"] else ""))
    return {"realized_pnl": realized, "live_net": live_net, "total": total,
            "open_marked": len(marked or []), "mark_source": mark_source,
            "text": "\n".join(lines)}


# ------------------------------------------------------------------ main

def run(entries: list = None, spot_fn=None, db_path: Path = None,
        now_fn=ist_now, notify_fn=None, force: bool = False) -> dict:
    """One report cycle. Everything injectable for offline tests.
    Returns {"posted": bool, "reason": str, "payload": dict-or-None}."""
    now = now_fn()
    if not force and not is_market_open(now):
        print(f"[Report Card] {now:%H:%M IST} — market closed, not posting.")
        return {"posted": False, "reason": "market closed", "payload": None}

    if notify_fn is None:
        from src.notifier import fire_broadcast
        notify_fn = fire_broadcast

    # get_live_marks prefers the engine's published snapshot, so during
    # market hours this card usually costs ZERO Dhan calls instead of
    # racing the live loop on the shared token; spot_fn stays injectable
    # (and lazily defaults inside the ladder only if a direct fetch is
    # actually needed).
    spreads, equities = _open_entries(entries)
    open_count = len(spreads) + len(equities)
    marked, _mark_source = get_live_marks(entries=spreads + equities,
                                          spot_fn=spot_fn)
    exposure = read_exposure(db_path)
    try:
        from src import equity_desk
        equity_section = equity_desk.render_book_lines()   # VM-local, live
    except Exception:
        equity_section = None                # fail-open: options card intact
    payload = build_report_payload(marked, open_count,
                                   open_count - len(marked), exposure, now,
                                   equity_section=equity_section)
    notify_fn(payload)
    print(f"[Report Card] posted — {open_count} open, "
          f"{len(marked)} marked live.")
    return {"posted": True, "reason": "ok", "payload": payload}


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post the read-only portfolio report card to Discord")
    parser.add_argument("--force", action="store_true",
                        help="post even outside market hours")
    args = parser.parse_args(argv)
    run(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
