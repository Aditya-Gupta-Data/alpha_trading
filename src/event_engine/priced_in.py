"""
Step 8, what the market already priced — our arithmetic on end-of-day prices from the bhavcopy lake
(the VM's own NSE files, mirrored on the node; never yfinance, which only works from a home IP).

  event_move_pct     close k sessions after the announcement vs the close before it
  justified_move_pct the material base effect × `move_per_revenue_pct` (default 1.0: a 1% revenue
                     effect justifies a 1% price move) — a stated, adjustable model, tagged computed
  verdict            in_line | overreaction | underreaction | opposite | unknown

Direction for a trade: overreaction → fade the move; underreaction or opposite → follow the
mechanics; in_line and unknown → no trade.
"""
from __future__ import annotations

from datetime import date

from src.event_engine import TAG_COMPUTED, settings, worst_tag

AFTER_SESSIONS = 3


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def event_move(bars: list, announced_on: str, after: int = AFTER_SESSIONS) -> dict:
    """bars = [{session: 'YYYY-MM-DD', close: float}, ...] oldest→newest."""
    try:
        d = date.fromisoformat(str(announced_on)[:10]).isoformat()
    except (TypeError, ValueError):
        return {"ok": False, "reason": "no announcement date"}
    rows = [b for b in bars or [] if b.get("session") and b.get("close")]
    before = [b for b in rows if b["session"] < d]
    on_after = [b for b in rows if b["session"] >= d]
    if not before or not on_after:
        return {"ok": False, "reason": "no prices around the announcement in the bhavcopy lake"}
    prev = float(before[-1]["close"])
    end = on_after[min(after, len(on_after) - 1)]
    move = round(100.0 * (float(end["close"]) / prev - 1.0), 3)
    return {"ok": True, "move_pct": move, "from": before[-1]["session"], "to": end["session"]}


def assess(ticker: str, material: dict, announced_on: str, bars_fn=None, cfg: dict = None) -> dict:
    cfg = cfg or settings()
    out = {"ticker": ticker, "verdict": "unknown", "event_move_pct": None, "justified_move_pct": None,
           "direction": None, "source": TAG_COMPUTED, "reason": None}
    if not material or not material.get("passes_gate"):
        out["reason"] = "not material"
        return out
    if bars_fn is None:
        from src.ingestion.bhavcopy_clerk import bars_for
        bars_fn = lambda sym: bars_for(sym, days=60)        # noqa: E731
    try:
        bars = bars_fn(ticker)
    except Exception as exc:
        bars = []
        out["reason"] = f"price read failed: {exc}"
    mv = event_move(bars, announced_on)
    if not mv["ok"]:
        out["reason"] = out["reason"] or mv["reason"]
        return out
    base = float(material["effect_pct"][1])
    justified = round(base * float(cfg.get("move_per_revenue_pct", 1.0)), 3)
    move, tol = mv["move_pct"], float(cfg.get("priced_in_tolerance_pct", 1.0))
    out.update(event_move_pct=move, justified_move_pct=justified, window=[mv["from"], mv["to"]],
               source=worst_tag(material.get("source"), TAG_COMPUTED))
    if abs(move - justified) <= tol:
        out["verdict"] = "in_line"
    elif _sign(move) and _sign(justified) and _sign(move) != _sign(justified):
        out["verdict"], out["direction"] = "opposite", ("bullish" if justified > 0 else "bearish")
    elif abs(move) > abs(justified):
        out["verdict"], out["direction"] = "overreaction", ("bearish" if move > 0 else "bullish")
    else:
        out["verdict"], out["direction"] = "underreaction", ("bullish" if justified > 0 else "bearish")
    return out
