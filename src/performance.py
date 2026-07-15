"""
src/performance.py — risk-adjusted track-record metrics (advisory)
==================================================================

The honest, RL-adjacent step (see docs/gemini_research_gap_analysis.md and
the 2026-07-15 RL discussion): the reward function BEFORE the agent. An RL
trader would optimize a risk-adjusted return like Sharpe/Sortino — but the
same number is useful on its own as a portfolio-health read, and building
the MEASUREMENT first (honest, abstaining until there is signal) is the
right order. No agent is meaningful until this can be trusted and there is
a real track record to score.

What it computes over the REAL paper track record (the actual resolved
journal trades — NOT the simulator's `simulated_trades` backtest corpus,
which is a different population; #49/#65 keep real and sim strata apart):

  * per-trade Sharpe   = mean(R) / stdev(R)     — consistency of the edge
  * per-trade Sortino  = mean(R) / downside dev — same, penalizing only
                          losses (the metric a seller book actually cares
                          about; upside "volatility" isn't risk)
  * max drawdown       = deepest peak-to-trough on the cumulative-R and
                          cumulative-rupee equity curves
  * win rate, avg R (expectancy), total P&L, n

The per-trade "return" unit is the R-MULTIPLE (realized P&L ÷ the trade's
defined max loss) — the risk-normalized number the tracker already stamps
on every resolved spread, so a Sharpe of R measures whether the edge is
consistent per unit of risk taken. Deliberately NOT annualized: scaling
per-trade to annual needs a trade-frequency assumption that would inject
noise into a small sample. The card says "per-trade" so nobody misreads it.

DOCTRINE (identical to the Greeks advisory #71, the exposure gate #68):
  * ADVISORY / READ-ONLY — reads the journal, writes nothing, proposes
    nothing.
  * HONEST ABSTENTION — below `performance_min_trades` (default 20)
    resolved REAL trades it returns "abstain" and says so; a Sharpe on 4
    trades is theatre (#50 NULL-honesty, the same reason the skeptic gate
    #44 stays closed on thin data).
  * HYPOTHETICALS EXCLUDED — outcomes flagged `hypothetical` (the #31
    tracked-but-not-real veto/skip shadows) never enter the numbers.
  * FAIL-OPEN — any error degrades to "no read", never raises.

Standalone CLI: `python3 -m src.performance`. Config: `performance_min_trades`.
"""

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_MIN_TRADES = 20


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def resolved_returns(entries: list = None) -> list:
    """Real resolved trades, oldest first: [{"r","pnl","date","resolution"}].
    Excludes un-resolved and `hypothetical` outcomes; skips rows missing
    an r_multiple (a return we can't honestly place)."""
    from src import journal
    if entries is None:
        entries = journal.read_all()
    out = []
    for e in entries:
        o = e.get("outcome")
        if not o or o.get("hypothetical"):
            continue
        r = o.get("r_multiple")
        if r is None:
            continue
        out.append({"r": float(r),
                    "pnl": float(o.get("pnl_rs") or 0.0),
                    "date": o.get("exit_date") or o.get("checked") or "",
                    "resolution": o.get("resolution")})
    out.sort(key=lambda x: x["date"])
    return out


def _mean(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list) -> float:
    """Sample standard deviation (n-1). 0.0 for <2 points."""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(returns: list) -> float | None:
    """mean/stdev of the return series. None when stdev is 0 (no dispersion
    to divide by — an undefined ratio, never a fabricated infinity)."""
    sd = _stdev(returns)
    if sd == 0:
        return None
    return round(_mean(returns) / sd, 3)


def sortino(returns: list) -> float | None:
    """mean over DOWNSIDE deviation (root-mean-square of the negative
    returns only). None when there is no downside dispersion — an all-wins
    sample has no honest Sortino, don't invent one."""
    downside = [r for r in returns if r < 0]
    if len(downside) < 1:
        return None
    dd = math.sqrt(sum(r ** 2 for r in downside) / len(returns))
    if dd == 0:
        return None
    return round(_mean(returns) / dd, 3)


def max_drawdown(series: list) -> float:
    """Deepest peak-to-trough drop on the cumulative sum of `series`
    (a per-step increment list). Returned as a non-negative magnitude in
    the series' own units; 0.0 for an always-rising or empty curve."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for x in series:
        cum += x
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return round(-worst, 4)


def compute(entries: list = None, min_trades: int = None,
            config: dict = None) -> dict:
    """The full read, or an abstain marker. min_trades falls back to
    config `performance_min_trades` then the module default."""
    cfg = config if config is not None else _config()
    floor = (min_trades if min_trades is not None
             else int(cfg.get("performance_min_trades", DEFAULT_MIN_TRADES)))
    rows = resolved_returns(entries)
    n = len(rows)
    if n < floor:
        return {"verdict": "abstain", "n": n, "min_trades": floor,
                "reason": (f"only {n} resolved real trade(s); need "
                           f"{floor} for an honest risk-adjusted read")}

    rs = [row["r"] for row in rows]
    pnls = [row["pnl"] for row in rows]
    wins = sum(1 for r in rs if r > 0)
    return {
        "verdict": "ok",
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_r": round(_mean(rs), 3),
        "total_pnl": round(sum(pnls), 2),
        "sharpe": sharpe(rs),
        "sortino": sortino(rs),
        "max_drawdown_r": max_drawdown(rs),
        "max_drawdown_rs": max_drawdown(pnls),
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
    }


def build_card(m: dict) -> str:
    """Plain-English advisory card."""
    if m.get("verdict") == "abstain":
        return ("📏 **Track-record metrics** — abstaining: "
                f"{m.get('reason')}.")

    def fmt(x):
        return "n/a" if x is None else f"{x:+.2f}"

    return "\n".join([
        f"📏 **Track-record metrics** — {m['n']} resolved paper trades",
        f"_Win rate {m['win_rate']:g}% · avg {m['avg_r']:+.2f}R · "
        f"net Rs.{m['total_pnl']:,.0f}_",
        f"• **Sharpe** {fmt(m['sharpe'])} · **Sortino** {fmt(m['sortino'])} "
        "(per-trade, in R units — consistency of the edge per unit of risk)",
        f"• Max drawdown {m['max_drawdown_r']:.2f}R "
        f"(Rs.{m['max_drawdown_rs']:,.0f}) · best {m['best_r']:+g}R / "
        f"worst {m['worst_r']:+g}R",
        "_Real paper track record only (sim corpus excluded). Advisory._",
    ])


def run(entries: list = None, *, notify_fn=None, config: dict = None) -> dict:
    """Compute → optionally post a Discord card. Posts ONLY when there is a
    real verdict (an abstain read stays silent on Discord — no weeks of
    'not enough data' spam), printing either way. Fail-open."""
    try:
        m = compute(entries, config=config)
        card = build_card(m)
        if notify_fn and m.get("verdict") == "ok":
            try:
                notify_fn(card)
            except Exception:
                pass
        return {"metrics": m, "card": card}
    except Exception as e:
        print(f"  (performance read skipped — failing open: {e})")
        return {"metrics": {"verdict": "error"}, "card": None}


def main() -> None:
    """CLI / cron: `python3 -m src.performance`. `--quiet` = snapshot/print
    only, no Discord post."""
    import sys
    quiet = "--quiet" in sys.argv
    notify_fn = None
    if not quiet:
        try:
            from src import notifier

            def notify_fn(msg):
                notifier.fire_broadcast({"embeds": [{"description": msg,
                                                     "color": 0x3498DB}]})
        except Exception:
            notify_fn = None
    out = run(notify_fn=notify_fn)
    print(out["card"] or "performance: read failed")


if __name__ == "__main__":
    main()
