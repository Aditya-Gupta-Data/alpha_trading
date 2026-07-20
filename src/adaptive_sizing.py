"""
src/adaptive_sizing.py — Dept 3: the autopsy-driven sizing feedback loop
========================================================================

Owner Directive 2 (2026-07-20, approved with pushbacks #2/#3): "make a
mistake, note it, remember it next time." Before either desk sizes a
trade, this layer reads the system's OWN resolved history — the equity
shadow ledger's autopsies, the options journal's outcomes — and returns
a sizing verdict: a penalty, a veto, a boost, or (most days, honestly)
neutral.

THE MATH (approved pushback #2 — never overrule it):
  * Beta prior CENTERED ON THE SETUP'S BREAK-EVEN win rate with strength
    `PRIOR_STRENGTH` pseudo-trades: at zero data the posterior edge is
    exactly 0 → multiplier exactly 1.0. No coin-flip learning (#44/#50).
  * Break-even p* = avg_loss_R / (avg_win_R + avg_loss_R) — empirical
    once the group has >= 3 weighted wins AND losses, else the family
    nominal (equity 0.40, options 0.50). A setup that wins small and
    loses big needs a higher win rate to deserve its size.
  * PENALTY (fast): >= MIN_PENALTY_N weighted resolutions, posterior
    edge < 0 and the one-sided Wilson LOWER bound below p* → multiplier
    1 + SLOPE*edge, floored at FLOOR (0.25x). Quick to shrink.
  * VETO (earned): >= MIN_VETO_N and the Wilson UPPER bound (the most
    charitable read of the win rate) still below p* → size 0. The
    telemetry row is STILL logged with the veto reason — the
    false-positive ledger never loses a line.
  * BOOST (slow): >= MIN_BOOST_N and the Wilson LOWER bound clears
    p* + BOOST_MARGIN → multiplier up to CAP (1.5x), always inside the
    desk's existing risk/notional caps. Slow to grow, by design.
  * GAP-SHOCK HALF-WEIGHT (approved pushback #3): a loss whose autopsy
    is a gap-down shock counts 0.5 toward the evidence — an exogenous
    overnight shock must not permanently taint a structural setup.
  * TICKER OVERLAY, veto-only: >= TICKER_VETO_N resolutions on one
    ticker with its Wilson UPPER bound under p* vetoes that ticker —
    a name that keeps burning us stops getting money, whatever the
    setup family says.

KEYS: equity — (setup, tier) from the entry's kyu_trigger
("darling_buy" x tier, "block_vwap_pullback"); options — the spread's
strategy archetype. Wilson math is imported from
`validation.stat_gates.wilson_lower_bound` (the repo's single source).

OPTIONS ASYMMETRY (disclosed): `size_lots` already returns the MAXIMUM
lots the risk budget affords, so the options side takes penalties
(floor 1 lot) and vetoes only — a boost there would breach
OPTIONS_RISK_PER_TRADE_PCT. Boosts act on the equity desk, where the
15% notional ceiling still binds above the raised risk budget.

Every non-neutral verdict → `logs/sizing_adjustments.jsonl`; a Discord
card fires only when a key's STATE CHANGES (neutral→penalty, →veto,
→boost, or back) — the ledger is the de-dup memory (Issue-8 pattern).
Kill switch `adaptive_sizing_enabled` (code default OFF); every seam
fails OPEN to neutral 1.0x — a broken feedback layer must never block
the pipeline.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import ADAPTIVE_SIZING_ENABLED
from src.validation.stat_gates import wilson_lower_bound

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
ADJUSTMENTS_PATH = ROOT / "logs" / "sizing_adjustments.jsonl"

PRIOR_STRENGTH = 10.0
SLOPE = 4.0
FLOOR = 0.25
CAP = 1.5
MIN_PENALTY_N = 4.0        # weighted resolutions before a penalty may act
MIN_VETO_N = 8.0
MIN_BOOST_N = 10.0
BOOST_MARGIN = 0.05
TICKER_VETO_N = 5.0
GAP_SHOCK_WEIGHT = 0.5
MIN_EMPIRICAL_SIDE = 3.0   # weighted wins AND losses before p* is empirical
NOMINAL_BREAK_EVEN = {"equity": 0.40, "options": 0.50}


def wilson_upper_bound(wins: float, n: float) -> float:
    """One-sided 95% Wilson UPPER bound: the most charitable win-rate
    reading. Mirror of the repo's lower bound (single z, single source)."""
    if n <= 0:
        return 1.0
    return 1.0 - wilson_lower_bound(n - wins, n)


# ------------------------------------------------------------- readers

def equity_history(events=None, ledger_path=None) -> list:
    """Resolved shadow trades as evidence rows: {key, ticker, r, weight}.
    key = (setup, tier or None). Gap-shock losses carry half weight
    (pushback #3). Unresolved / r-less rows are excluded, never guessed."""
    from src import knowledge_graph_logger as kg
    events = kg.read_events(ledger_path) if events is None else events
    entries = {e.get("id"): e for e in events if e.get("event") == "entry"}
    rows = []
    for e in events:
        if e.get("event") != "exit":
            continue
        host = entries.get(e.get("id"))
        if not host:
            continue
        r = (e.get("kya_sikha_autopsy") or {}).get("r_multiple")
        if r is None:
            continue
        trig = host.get("kyu_trigger") or {}
        category = (e.get("kya_sikha_autopsy") or {}).get("category") or ""
        weight = (GAP_SHOCK_WEIGHT
                  if float(r) <= 0 and "Gap-down shock" in category else 1.0)
        rows.append({"key": (trig.get("setup"), trig.get("tier")),
                     "ticker": e.get("ticker"), "r": float(r),
                     "weight": weight})
    return rows


def options_history(entries=None) -> list:
    """Resolved REAL options trades keyed by strategy archetype. Same
    exclusions as performance.py (#72): hypothetical shadows out, rows
    missing an r_multiple out; the sim corpus never enters (it reads the
    journal, where sim trades don't live — #49/#65)."""
    from src import journal
    if entries is None:
        entries = journal.read_all()
    rows = []
    for e in entries:
        o = e.get("outcome")
        if not o or o.get("hypothetical"):
            continue
        r = o.get("r_multiple")
        if r is None:
            continue
        strategy = (e.get("spread") or {}).get("strategy")
        if not strategy:
            continue
        rows.append({"key": ("option", strategy), "ticker": e.get("ticker"),
                     "r": float(r), "weight": 1.0})
    return rows


# ---------------------------------------------------------------- math

def _stats(rows: list) -> dict:
    wins = sum(r["weight"] for r in rows if r["r"] > 0)
    losses = sum(r["weight"] for r in rows if r["r"] <= 0)
    win_rs = [r["r"] for r in rows if r["r"] > 0]
    loss_rs = [-r["r"] for r in rows if r["r"] <= 0]
    return {"wins": wins, "losses": losses, "n": wins + losses,
            "avg_win_r": sum(win_rs) / len(win_rs) if win_rs else None,
            "avg_loss_r": sum(loss_rs) / len(loss_rs) if loss_rs else None}


def break_even(stats: dict, family: str) -> float:
    """p* = L/(W+L). Empirical only once BOTH sides have real weight
    (>= MIN_EMPIRICAL_SIDE); before that, the family nominal — a payoff
    ratio invented from two data points is worse than a stated default."""
    if (stats["wins"] >= MIN_EMPIRICAL_SIDE
            and stats["losses"] >= MIN_EMPIRICAL_SIDE
            and stats["avg_win_r"] and stats["avg_loss_r"]
            and stats["avg_win_r"] > 0):
        return stats["avg_loss_r"] / (stats["avg_win_r"]
                                      + stats["avg_loss_r"])
    return NOMINAL_BREAK_EVEN.get(family, 0.5)


def evaluate(rows: list, family: str, ticker: str = None,
             ticker_rows: list = None) -> dict:
    """One sizing verdict for a key (plus the veto-only ticker overlay).
    Neutral by construction until the evidence bars are met."""
    s = _stats(rows)
    p_star = break_even(s, family)
    verdict = {"multiplier": 1.0, "action": "neutral", "n_eff": round(s["n"], 1),
               "break_even": round(p_star, 3), "detail": "insufficient data"}
    if s["n"] >= MIN_PENALTY_N:
        post_mean = ((p_star * PRIOR_STRENGTH + s["wins"])
                     / (PRIOR_STRENGTH + s["n"]))
        edge = post_mean - p_star
        lb = wilson_lower_bound(s["wins"], s["n"])
        ub = wilson_upper_bound(s["wins"], s["n"])
        verdict.update(win_lb=round(lb, 3), win_ub=round(ub, 3),
                       posterior=round(post_mean, 3))
        if s["n"] >= MIN_VETO_N and ub < p_star:
            verdict.update(multiplier=0.0, action="veto",
                           detail=(f"even the charitable win-rate read "
                                   f"({ub:.0%}) sits under break-even "
                                   f"({p_star:.0%}) on n={s['n']:.1f}"))
            return verdict
        if s["n"] >= MIN_BOOST_N and lb > p_star + BOOST_MARGIN:
            verdict.update(multiplier=round(min(1 + SLOPE * edge, CAP), 3),
                           action="boost",
                           detail=(f"win-rate lower bound {lb:.0%} clears "
                                   f"break-even {p_star:.0%} + margin"))
        elif edge < 0 and lb < p_star:
            verdict.update(multiplier=round(max(1 + SLOPE * edge, FLOOR), 3),
                           action="penalty",
                           detail=(f"posterior {post_mean:.0%} under "
                                   f"break-even {p_star:.0%}"))
        else:
            verdict["detail"] = "edge indistinguishable from break-even"
    # Ticker overlay — veto only, its own bar (a burning name stops
    # getting money regardless of how the setup family is doing).
    if ticker and ticker_rows:
        ts = _stats(ticker_rows)
        if (ts["n"] >= TICKER_VETO_N
                and wilson_upper_bound(ts["wins"], ts["n"]) < p_star):
            verdict.update(multiplier=0.0, action="veto",
                           detail=(f"{ticker}: {ts['losses']:.1f} weighted "
                                   f"losses vs {ts['wins']:.1f} wins — "
                                   f"ticker veto"))
    return verdict


# ------------------------------------------------- ledger + state cards

def _last_action(key_name: str, path=None) -> str:
    p = Path(path) if path else ADJUSTMENTS_PATH
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return "neutral"
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("key") == key_name:
            return row.get("action", "neutral")
    return "neutral"


def record(key_name: str, verdict: dict, context: str,
           path=None, broadcast_fn=None) -> None:
    """Ledger every non-neutral verdict; card only on a STATE CHANGE for
    that key (the ledger is the de-dup memory)."""
    prev = _last_action(key_name, path)
    if verdict["action"] == "neutral" and prev == "neutral":
        return
    p = Path(path) if path else ADJUSTMENTS_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(IST).isoformat(timespec="seconds"),
                "key": key_name, "context": context, **verdict}) + "\n")
    except OSError:
        pass
    if verdict["action"] != prev:
        try:
            if broadcast_fn is None:
                from src.notifier import fire_broadcast
                broadcast_fn = fire_broadcast
            broadcast_fn({
                "event": "adaptive_sizing", "ticker": "SIZING",
                "date": datetime.now(IST).date().isoformat(),
                "description": (f"🧠 Adaptive sizing: `{key_name}` "
                                f"{prev} → {verdict['action']} "
                                f"(x{verdict['multiplier']}).\n"
                                f"{verdict['detail']}\n"
                                f"n_eff={verdict['n_eff']}, "
                                f"break-even={verdict['break_even']:.0%}")})
        except Exception as exc:
            print(f"  (adaptive sizing card failed: {exc})")


# ------------------------------------------------------- desk seams

def equity_verdict(entry: dict, ledger_path=None,
                   adjustments_path=None, broadcast_fn=None) -> dict:
    """The equity desk's pre-sizing consult. Fail-OPEN to neutral: a
    broken feedback layer must never block a funding decision."""
    neutral = {"multiplier": 1.0, "action": "neutral",
               "detail": "adaptive sizing disabled"}
    if not ADAPTIVE_SIZING_ENABLED:
        return neutral
    try:
        trig = entry.get("kyu_trigger") or {}
        key = (trig.get("setup"), trig.get("tier"))
        ticker = entry.get("ticker")
        rows = equity_history(ledger_path=ledger_path)
        verdict = evaluate([r for r in rows if r["key"] == key], "equity",
                           ticker=ticker,
                           ticker_rows=[r for r in rows
                                        if r["ticker"] == ticker])
        record(f"{key[0]}/{key[1]}", verdict, f"entry {ticker}",
               path=adjustments_path, broadcast_fn=broadcast_fn)
        return verdict
    except Exception as exc:
        return dict(neutral, detail=f"feedback layer unavailable ({exc})")


def adjust_option_lots(strategy: str, lots: int, entries=None,
                       adjustments_path=None, broadcast_fn=None) -> tuple:
    """The options proposer's pre-proposal consult: (adjusted_lots,
    verdict). Penalties floor at 1 lot; only an earned veto zeroes.
    Boosts never fire here (size_lots already returns the risk-budget
    maximum — see the module docstring). Fail-OPEN to unchanged lots."""
    neutral = {"multiplier": 1.0, "action": "neutral",
               "detail": "adaptive sizing disabled"}
    if not ADAPTIVE_SIZING_ENABLED:
        return lots, neutral
    try:
        rows = [r for r in options_history(entries=entries)
                if r["key"] == ("option", strategy)]
        verdict = evaluate(rows, "options")
        record(f"option/{strategy}", verdict, f"proposal x{lots} lots",
               path=adjustments_path, broadcast_fn=broadcast_fn)
        if verdict["action"] == "veto":
            return 0, verdict
        if verdict["action"] == "penalty":
            return max(1, int(lots * verdict["multiplier"])), verdict
        return lots, verdict
    except Exception as exc:
        return lots, dict(neutral,
                          detail=f"feedback layer unavailable ({exc})")


if __name__ == "__main__":
    # Read-only report: what the feedback layer currently thinks.
    groups = {}
    for row in equity_history() + options_history():
        groups.setdefault(row["key"], []).append(row)
    if not groups:
        print("adaptive sizing — no resolved history yet (all neutral)")
    for key, rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        fam = "options" if key[0] == "option" else "equity"
        v = evaluate(rows, fam)
        print(f"{key}: {v['action']} x{v['multiplier']} "
              f"(n_eff={v['n_eff']}, p*={v['break_even']:.0%}) — "
              f"{v['detail']}")
