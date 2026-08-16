"""
Alpha Trading — End-of-Day Summary broadcaster

Runs daily at 15:30 IST (10:00 UTC, market just closed) to push a terse
status card to the Discord channel:

    python3 -m src.eod_summary

Data sources (both local, no network other than the final Discord POST):
  data/journal.jsonl   — active approved positions + today's resolved exits
  data/brain_map.db    — today's outcomes rows (win/loss count)

Computes:
  * Daily MTM P&L       — sum of pnl_rs for exits with today's exit_date
  * Active positions    — approved entries with no outcome (spreads + equities)
  * Net delta exposure  — strategy-level directional bias across open spreads
  * Win/loss count      — from brain_map outcomes (cross-check vs journal)

Cron schedule on the VM (IST = UTC+5:30):
    0 10 * * 1-5  cd /home/aditya/alpha_trading && \
                  /home/aditya/alpha_trading/venv/bin/python3 -m src.eod_summary
"""

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "brain_map.db"
JOURNAL_PATH    = ROOT / "data" / "journal.jsonl"
BLOCKS_PATH     = ROOT / "logs" / "exposure_blocks.jsonl"

# Strategy-level net-delta bias approximation.
# bull call / bear put spreads carry directional exposure; iron condor /
# butterfly are balanced by construction (symmetric short strikes cancel).
# Multiplied by _ATM_DELTA × lots × lot_size to express in synthetic
# share-equivalents of the underlying.
_STRATEGY_DELTA_BIAS = {
    "bull_call_spread":  1.0,
    "bear_put_spread":  -1.0,
    "iron_condor":       0.0,
    "iron_butterfly":    0.0,
}
_ATM_DELTA = 0.5   # ATM-option delta approximation


def _today() -> str:
    return date.today().isoformat()


def _read_journal(path=None) -> list:
    p = Path(path or JOURNAL_PATH)
    if not p.exists():
        return []
    entries = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _open_approved_spreads(entries: list) -> list:
    return [
        e for e in entries
        if e.get("decision") == "approved"
        and e.get("outcome") is None
        and e.get("spread")
    ]


def _open_approved_equities(entries: list) -> list:
    return [
        e for e in entries
        if e.get("decision") == "approved"
        and e.get("outcome") is None
        and e.get("plan")
        and not e.get("spread")
    ]


def query_todays_resolutions(db_path=None) -> list:
    """Rows from brain_map.db outcomes table resolved on today's date."""
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.exists():
        return []
    today = _today()
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, archetype, r_multiple, result FROM outcomes WHERE date = ?",
            (today,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"  (eod_summary: brain_map query failed: {exc})")
        return []


def compute_net_delta_exposure(open_spreads: list) -> float:
    """Approximate net delta exposure (synthetic share-equivalents) across all
    open spread positions.

    Uses the strategy's directional bias (_STRATEGY_DELTA_BIAS) since individual
    leg strikes are not compared to a live spot price. Scaled by lots × lot_size.
    Unknown strategies contribute zero (market-neutral assumption).
    """
    net = 0.0
    for entry in open_spreads:
        spread = entry.get("spread") or {}
        strategy = spread.get("strategy", "")
        lots     = int(spread.get("lots", 1))
        lot_size = int(spread.get("lot_size", 1))
        qty      = lots * lot_size
        bias     = _STRATEGY_DELTA_BIAS.get(strategy, 0.0)
        net     += bias * _ATM_DELTA * qty
    return round(net, 2)


# ============================= Sequence 1: reporting honesty (2026-08-05)
#
# SYSTEM_XRAY §9 found the daily cards showing numbers without the context
# that decides what they mean. Four gaps, all closed here from data the
# system ALREADY writes — no new collector, no new cron, no new risk.
#
#   * a raw win rate with no n and no lower bound (and `stat_gates
#     .wilson_lower_bound` existed the whole time, described in MODULES.md
#     as "THE number every displayed win-rate must carry")
#   * an absolute return with no drawdown beside it, while `equity_curve`
#     held peak and drawdown_pct per settlement
#   * 645 risk-gate blocks the owner had never seen as a number
#   * positions open 14+ days with no age anywhere on a card
#
# Every helper below is pure + injectable and fails open to None, so a
# broken section costs its own line and never the card.

WILSON_MIN_N = 5          # below this a bound is wider than it is useful


def wilson_line(wins: int, losses: int) -> str | None:
    """`12W/7L (n=19) · 63% · 95% lower bound 41%` — or None when there is
    nothing honest to say.

    The lower bound is the whole point: 63% off 19 trades is not evidence
    of a 63% edge, and the card must say so in the same breath. Below
    WILSON_MIN_N the bound is so wide it misleads in the other direction,
    so we print the raw counts and explicitly withhold it."""
    n = int(wins) + int(losses)
    if n <= 0:
        return None
    raw = wins / n * 100
    base = f"{wins}W/{losses}L (n={n}) · {raw:.0f}%"
    if n < WILSON_MIN_N:
        return base + f" · too few for a bound (n<{WILSON_MIN_N})"
    try:
        from src.validation.stat_gates import wilson_lower_bound
        lo = wilson_lower_bound(int(wins), n) * 100
    except Exception:
        return base
    return base + f" · 95% lower bound {lo:.0f}%"


def drawdown_line(db_path=None) -> str | None:
    """`peak Rs.244,215 · now Rs.239,424 · drawdown -1.96%` from the
    equity_curve table, or None when the curve is empty.

    The EOD card has shown `Absolute return +18%` for weeks with no
    drawdown beside it. The data was already there — 18 rows carrying
    equity, peak_equity and drawdown_pct — and nothing read it."""
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.exists():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT equity, peak_equity, drawdown_pct FROM equity_curve "
                "ORDER BY ts DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    equity, peak, dd = row
    if equity is None or peak is None:
        return None
    dd = float(dd or 0.0)
    flat = " (at peak)" if dd <= 0.0001 else ""
    return (f"peak Rs.{float(peak):,.0f} · now Rs.{float(equity):,.0f} · "
            f"drawdown -{dd:.2f}%{flat}")


# Proposal-ledger fates that mean "a trade was proposed and a RISK rule said
# no" — the opportunity-cost set (M1, 2026-08-16). Quote/VIX/structure
# refusals are data problems, not risk blocks, and are deliberately excluded.
RISK_REFUSAL_FATES = ("REJECTED_RISK_CAP", "REJECTED_RISK_BUDGET",
                      "REJECTED_MARGIN", "REJECTED_EXPOSURE")


def risk_refusals_today(today: str = None, ledger_path=None) -> dict | None:
    """{fate: count} of today's proposal-ledger rows whose fate is a risk
    refusal (per-trade cap, risk budget, margin, exposure), or None when
    the ledger is absent/empty for the day. Read-only; the ledger is
    written by the proposer, never here."""
    try:
        from src import proposal_ledger
        rows = proposal_ledger.read_rows(path=ledger_path,
                                         session_date=today or _today())
    except Exception:
        return None
    if not rows:
        return None
    out = {}
    for r in rows:
        f = r.get("fate")
        if f in RISK_REFUSAL_FATES:
            out[f] = out.get(f, 0) + 1
    return out


def blocked_line(today: str = None, blocks_path=None,
                 ledger_path=None) -> str | None:
    """`Blocked today: 3 (total 645)` from the exposure gate's ledger —
    plus, when the proposal ledger has any for the day, the risk/margin
    refusals: ` · risk refusals today: 2 (margin 1, risk cap 1)`.

    The single most under-reported number in the system: 645 blocks
    against ~25 entries, and the owner only ever saw at most one Discord
    note per (ticker, direction) per day. A gate nobody can count is a
    gate nobody can evaluate. (M1 2026-08-16: margin / risk-cap / budget
    refusals were counted nowhere on a daily card either — the proposal
    ledger had them all along.)"""
    path = Path(blocks_path) if blocks_path else BLOCKS_PATH
    today = today or _today()
    total = todays = 0
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            total += 1
            if f'"ts": "{today}' in line or f'"ts":"{today}' in line:
                todays += 1
    except OSError:
        total = None
    refusals = risk_refusals_today(today, ledger_path)
    parts = []
    if total:
        parts.append(f"Blocked today: {todays} (total {total})")
    if refusals:
        n = sum(refusals.values())
        detail = ", ".join(f"{k.replace('REJECTED_', '').replace('_', ' ').lower()} {v}"
                           for k, v in sorted(refusals.items(), key=lambda kv: -kv[1]))
        parts.append(f"risk refusals today: {n} ({detail})")
    return " · ".join(parts) if parts else None


def position_age_lines(entries: list, today: str = None,
                       max_lines: int = 6) -> list:
    """`NIFTY BANK bear_put_spread — 12d open` per open position.

    `book_context.position_dossier` already computed `days_in_trade` and
    was CLI-only. An equity-desk position has been open since 2026-07-22
    and no card has ever said so."""
    from datetime import date as _date
    try:
        t = _date.fromisoformat(today) if today else _date.fromisoformat(_today())
    except ValueError:
        return []
    out = []
    for e in entries or []:
        opened = (e.get("date") or "")[:10]
        try:
            age = max(0, (t - _date.fromisoformat(opened)).days)
        except ValueError:
            continue
        what = ((e.get("spread") or {}).get("strategy")
                or ("delivery" if e.get("plan") is not None else "position"))
        tick = e.get("ticker") or (e.get("spread") or {}).get("underlying") or "?"
        out.append((age, f"• {tick} {what} — {age}d open"))
    out.sort(key=lambda x: -x[0])
    lines = [s for _, s in out[:max_lines]]
    if len(out) > max_lines:
        lines.append(f"…and {len(out) - max_lines} more")
    return lines


def build_eod_card(db_path=None, halt_lines_fn=None, blocks_path=None,
                   ledger_path=None) -> dict:
    """Build the EOD broadcast payload from journal + brain_map.db.

    Returns a payload dict ready for broadcast_alert(payload). Exported so
    tests can call it directly with mocked data sources.
    """
    today   = _today()
    entries = _read_journal()

    # Today's exits from the journal (approved entries that resolved today).
    todays_exits = [
        e for e in entries
        if (e.get("outcome") or {}).get("exit_date") == today
        and e.get("decision") == "approved"
    ]

    open_spreads   = _open_approved_spreads(entries)
    open_equities  = _open_approved_equities(entries)
    active_total   = len(open_spreads) + len(open_equities)

    # Daily MTM P&L from the journal's pnl_rs field (net of frictions).
    daily_pnl = sum(
        float((e.get("outcome") or {}).get("pnl_rs") or 0.0)
        for e in todays_exits
    )

    # Brain Map win/loss count for today (cross-check, not the primary P&L).
    db_rows = query_todays_resolutions(db_path=db_path)
    wins    = sum(1 for r in db_rows if r.get("result") == "win")
    losses  = sum(1 for r in db_rows if r.get("result") == "loss")

    # Net delta from open spread positions.
    net_delta = compute_net_delta_exposure(open_spreads)

    # Build Discord field list.
    fields: list = []

    # Directive 2 (CEO-View Discord, 2026-07-27): one narrated line over
    # numbers this card already computes — no new data, plain English
    # instead of a bare field grid.
    try:
        from src import ceo_language
        fields.append({"name": "📋 Plain English",
                       "value": ceo_language.book_summary_sentence(
                           active_total, daily_pnl, net_delta),
                       "inline": False})
    except Exception:
        pass

    if todays_exits:
        sign = "+" if daily_pnl >= 0 else ""
        fields.append({
            "name":   "Today's MTM P&L",
            "value":  f"Rs.{sign}{daily_pnl:,.0f}",
            "inline": True,
        })
        fields.append({
            "name":   "Resolved Today",
            "value":  f"{len(todays_exits)} trade(s)",
            "inline": True,
        })
    else:
        fields.append({"name": "Resolved Today", "value": "None", "inline": True})

    if wins + losses > 0:
        fields.append({
            "name":   "Brain Map W/L",
            "value":  wilson_line(wins, losses) or f"{wins}W / {losses}L",
            "inline": False,
        })

    fields += [
        {"name": "Active Spreads",   "value": str(len(open_spreads)),  "inline": True},
        {"name": "Active Equities",  "value": str(len(open_equities)), "inline": True},
        {"name": "Total Active",     "value": str(active_total),       "inline": True},
    ]

    if net_delta != 0.0:
        direction = "long" if net_delta > 0 else "short"
        fields.append({
            "name":   "Net Delta",
            "value":  f"{net_delta:+.1f} ({direction} bias)",
            "inline": True,
        })
    else:
        fields.append({"name": "Net Delta", "value": "±0 (flat)", "inline": True})

    # Sequence 1 (2026-08-05): risk context the card never carried.
    # Drawdown rides WITH the return — an absolute return shown alone is
    # the number that flatters; the pair is the number that informs.
    try:
        risk = [x for x in (drawdown_line(db_path),
                            blocked_line(today, blocks_path, ledger_path)) if x]
        if risk:
            fields.append({"name": "📉 Drawdown & Gate Activity",
                           "value": "\n".join(risk)[:1024], "inline": False})
    except Exception:
        pass

    try:
        ages = position_age_lines(open_spreads + open_equities, today)
        if ages:
            fields.append({"name": "⏳ Position Age",
                           "value": "\n".join(ages)[:1024], "inline": False})
    except Exception:
        pass

    # Directive 6 (#84): the firm's MTM + return line, prominent (first
    # field). Read-only compute, fail-open like every section here.
    try:
        from src.firm_mtm import render_line
        fields.insert(0, {"name": "💹 Firm MTM & Return",
                          "value": render_line()[:1024], "inline": False})
    except Exception:
        pass

    # Walkaway Protocol (Directive 3): while a risk-of-ruin halt is up,
    # this card opens with the red banner — and reading the banner is what
    # re-fires the daily 🔴 SYSTEM PAUSED card (self de-duped inside pm).
    # The live read is INJECTED by the cron entrypoint only (07-23 lesson:
    # a build_* called from a test must never touch live state on its
    # own); halt_lines_fn=None or healthy = zero change to this card.
    try:
        halt_lines = halt_lines_fn() if halt_lines_fn else []
        if halt_lines:
            fields.insert(0, {"name": "🔴 SYSTEM PAUSED",
                              "value": "\n".join(halt_lines)[:1024],
                              "inline": False})
    except Exception:
        pass

    # One-firm-view (decision #82, VM-native since #83): the equity
    # desk's live book rides on this card too — all local, fail-open.
    try:
        from src import equity_desk
        fields.append({"name": "💼 Equity Desk",
                       "value": equity_desk.render_book_lines(),
                       "inline": False})
    except Exception:
        pass

    # Directive 4 (#84): everything the daily Discord budget spooled —
    # trades, rotations, sizing changes, review flags — lands HERE.
    try:
        from src.notifier import drain_digest_queue
        batched = drain_digest_queue()
        if batched:
            fields.append({"name": "📦 Batched signals",
                           "value": batched[:1024], "inline": False})
    except Exception:
        pass

    if active_total == 0 and not todays_exits:
        description = "No open positions. Engine idle until next signal."
    else:
        description = "Market closed. Open positions monitored by plan_tracker."

    return {
        "event":       "eod",
        "ticker":      "",
        "date":        today,
        "description": description,
        "fields":      fields,
    }


async def broadcast_eod(db_path=None) -> bool:
    """Build the EOD card and send it to Discord. Returns True on success.

    The PRODUCTION composition root: this is where the live halt-banner
    read is injected (Walkaway Protocol) — build_eod_card itself stays
    inert so tests can call it without touching brain_map."""
    from src.notifier import broadcast_alert
    from src import portfolio_manager as pm
    payload = build_eod_card(db_path=db_path,
                             halt_lines_fn=pm.halt_banner_lines)
    return await broadcast_alert(payload)


def main() -> int:
    today = _today()
    print(f"Alpha Trading EOD Summary — {today}")
    ok = asyncio.run(broadcast_eod())
    if ok:
        print("EOD summary broadcast to Discord ✓")
    else:
        print("EOD summary Discord delivery failed "
              "(webhook unconfigured or unreachable)")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
