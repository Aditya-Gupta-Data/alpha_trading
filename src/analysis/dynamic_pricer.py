"""
src/analysis/dynamic_pricer.py — dynamic levels for the Darlings (Dept 8)
=========================================================================

Phase 2 of the Darling Pipeline (owner directives + amendments approved
2026-07-19): the fundamental pipeline decides WHAT deserves attention;
this module continuously recomputes WHERE it makes sense to act — and it
is ADVISORY-ONLY (Law #63 twice reaffirmed: levels annotate, the shadow
journal earns any future authority through Dept 5).

Runs on the MAC (boundary doctrine: heavy data stays local — it reads
the bhavcopy lake); only its OUTPUT artifact `data/darlings_levels.json`
is lightweight enough to ship to the VM.

Per darling, from daily bars (`bhavcopy_clerk.bars_for`):

  BUY ZONE   anchored VWAP (anchored at the highest-volume up-day of the
             last 60 sessions — where accumulation actually happened)
             widened to the nearest high-volume node band.
  STOP       buy-zone floor − 1.5 × ATR(14). Volatility doubles, the
             stop widens — no static percentage anywhere.
  TRIMS      the last confirmed swing-pivot highs above price (profit-
             booking ladder); the trailing floor ratchets to the latest
             confirmed pivot low.
  TREND      50-DMA / 200-DMA + the Law-3 overextension state:
             `overextended` = close more than 3 ATR above the 50-DMA
             AND >20% above the 200-DMA -> the equity checks layer
             blocks fresh delivery buys until a pullback.

NULL-HONESTY (non-negotiable): every measure needs its minimum history
— ATR(14) needs 15 bars, DMA200 needs 200, pivots need their flanks. A
darling with thin history gets None fields and an `insufficient_history`
row, never a guessed level. Nothing is interpolated.

Shadow journal (Dept 5's future evidence): every run appends one line
per symbol to `data/lake/pricer_journal.jsonl` — date, close, levels,
extension state, recalc_reason — so entry-timing efficiency is
measurable BEFORE any authority is ever granted.

CLI (Mac):  python3 -m src.analysis.dynamic_pricer [--dry-run]
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
LEVELS_PATH = ROOT / "data" / "darlings_levels.json"
JOURNAL_PATH = ROOT / "data" / "lake" / "pricer_journal.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))

ATR_N = 14
STOP_ATR_MULT = 1.5
ANCHOR_WINDOW = 60                 # sessions searched for the volume anchor
HVN_WINDOW = 120                   # sessions for high-volume nodes
HVN_BINS = 12
PIVOT_FLANK = 5                    # bars each side to confirm a swing pivot
MIN_SESSIONS = 60                  # below this: insufficient_history
# Law 3 overextension (v1 constants, documented not hidden):
EXT_ATR_ABOVE_50DMA = 3.0
EXT_PCT_ABOVE_200DMA = 0.20


def atr(bars: list, n: int = ATR_N):
    """Mean true range of the last n sessions. None below n+1 bars."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        prev_close = bars[i - 1]["close"]
        hi, lo = bars[i]["high"], bars[i]["low"]
        if None in (prev_close, hi, lo):
            return None
        trs.append(max(hi, prev_close) - min(lo, prev_close))
    return round(sum(trs) / n, 2)


def dma(bars: list, n: int):
    """Simple n-session moving average of closes. None below n bars."""
    if len(bars) < n:
        return None
    closes = [b["close"] for b in bars[-n:]]
    if any(c is None for c in closes):
        return None
    return round(sum(closes) / n, 2)


def find_anchor(bars: list, window: int = ANCHOR_WINDOW) -> int:
    """Index of the highest-volume UP-day in the last `window` sessions —
    the session accumulation most plausibly happened. Falls back to the
    window start when no up-day exists (never guesses outside data)."""
    lo = max(1, len(bars) - window)
    best, best_vol = lo, -1.0
    for i in range(lo, len(bars)):
        b = bars[i]
        if None in (b["close"], b["volume"], bars[i - 1]["close"]):
            continue
        if b["close"] > bars[i - 1]["close"] and b["volume"] > best_vol:
            best, best_vol = i, b["volume"]
    return best


def anchored_vwap(bars: list, anchor_idx: int):
    """Volume-weighted average close from the anchor to now."""
    num = den = 0.0
    for b in bars[anchor_idx:]:
        if None in (b["close"], b["volume"]):
            continue
        num += b["close"] * b["volume"]
        den += b["volume"]
    return round(num / den, 2) if den > 0 else None


def high_volume_nodes(bars: list, window: int = HVN_WINDOW,
                      bins: int = HVN_BINS, top: int = 3) -> list:
    """Price bands where the most volume traded (daily resolution — the
    honest stand-in for volume profile until intraday data exists).
    Returns up to `top` (low, high) bands, heaviest first."""
    sample = [b for b in bars[-window:]
              if b["close"] is not None and b["volume"]]
    if len(sample) < 20:
        return []
    lo = min(b["close"] for b in sample)
    hi = max(b["close"] for b in sample)
    if hi <= lo:
        return []
    width = (hi - lo) / bins
    buckets = [0.0] * bins
    for b in sample:
        idx = min(int((b["close"] - lo) / width), bins - 1)
        buckets[idx] += b["volume"]
    ranked = sorted(range(bins), key=lambda i: buckets[i], reverse=True)
    return [(round(lo + i * width, 2), round(lo + (i + 1) * width, 2))
            for i in ranked[:top]]


def pivots(bars: list, flank: int = PIVOT_FLANK) -> dict:
    """Confirmed swing pivots (a bar whose high/low is the extreme of its
    ±flank neighbourhood). Only CONFIRMED pivots — the last `flank` bars
    can't confirm and are never guessed."""
    highs, lows = [], []
    for i in range(flank, len(bars) - flank):
        window = bars[i - flank:i + flank + 1]
        if any(b["high"] is None or b["low"] is None for b in window):
            continue
        if bars[i]["high"] == max(b["high"] for b in window):
            highs.append((bars[i]["session"], bars[i]["high"]))
        if bars[i]["low"] == min(b["low"] for b in window):
            lows.append((bars[i]["session"], bars[i]["low"]))
    return {"highs": highs, "lows": lows}


def extension_state(close, dma50, dma200, atr_val):
    """Law 3: 'overextended' = far above BOTH DMAs. None (abstain) when
    any input is missing — an honest abstain never blocks anything."""
    if None in (close, dma50, dma200, atr_val) or atr_val == 0:
        return None
    over = ((close - dma50) / atr_val > EXT_ATR_ABOVE_50DMA
            and close > dma200 * (1 + EXT_PCT_ABOVE_200DMA))
    return "overextended" if over else "normal"


def levels_for(symbol: str, bars: list, recalc_reason: str = "daily") -> dict:
    """All levels for one darling. NULL-honest throughout."""
    if len(bars) < MIN_SESSIONS:
        return {"symbol": symbol, "status": "insufficient_history",
                "sessions": len(bars)}
    close = bars[-1]["close"]
    a = atr(bars)
    d50, d200 = dma(bars, 50), dma(bars, 200)
    vwap = anchored_vwap(bars, find_anchor(bars))
    nodes = high_volume_nodes(bars)
    pv = pivots(bars)

    # buy zone: anchored VWAP stretched to the nearest heavy node band
    buy_lo = buy_hi = vwap
    if vwap is not None and nodes:
        nearest = min(nodes, key=lambda band: min(
            abs(vwap - band[0]), abs(vwap - band[1])))
        buy_lo, buy_hi = min(vwap, nearest[0]), max(vwap, nearest[1])
    stop = (round(buy_lo - STOP_ATR_MULT * a, 2)
            if None not in (buy_lo, a) else None)
    trail = pv["lows"][-1][1] if pv["lows"] else None
    trims = [h for _, h in pv["highs"] if close is not None and h > close][-2:]

    return {"symbol": symbol, "status": "ok",
            "as_of": bars[-1]["session"], "close": close,
            "buy_zone": [buy_lo, buy_hi], "stop": stop,
            "trailing_floor": trail, "trim_levels": trims,
            "anchored_vwap": vwap, "hv_nodes": nodes,
            "atr14": a, "dma50": d50, "dma200": d200,
            "extension": extension_state(close, d50, d200, a),
            "sessions": len(bars), "recalc_reason": recalc_reason}


def run(queue_path=None, lake_dir=None, levels_path=None,
        journal_path=None, bars_fn=None, write: bool = True,
        recalc_reason: str = "daily") -> dict:
    """Every queued darling -> levels + one shadow-journal line."""
    qp = Path(queue_path) if queue_path else QUEUE_PATH
    try:
        tickers = json.loads(qp.read_text()).get("tickers") or []
    except (OSError, ValueError):
        tickers = []
    if bars_fn is None:
        # one pass over the day files for ALL symbols (never per-symbol
        # re-parsing — 91 darlings x 218 files would be quadratic)
        from src.ingestion.bhavcopy_clerk import bars_for_many
        store = bars_for_many(tickers, days=250, lake_dir=lake_dir)
        bars_fn = lambda sym: store.get(sym.split(".")[0].upper(), [])

    now = datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
    levels, thin = [], []
    for sym in tickers:
        row = levels_for(sym, bars_fn(sym), recalc_reason)
        (levels if row["status"] == "ok" else thin).append(row)

    out = {"as_of": now, "recalc_reason": recalc_reason,
           "levels": levels,
           "insufficient_history": [t["symbol"] for t in thin],
           "advisory_note": "ADVISORY-ONLY (Law #63): levels annotate; "
                            "nothing here creates, sizes, or routes a "
                            "trade."}
    if write:
        lp = Path(levels_path) if levels_path else LEVELS_PATH
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps(out, indent=1))
        out["levels_path"] = str(lp)
        jp = Path(journal_path) if journal_path else JOURNAL_PATH
        jp.parent.mkdir(parents=True, exist_ok=True)
        with jp.open("a") as fh:
            for row in levels:
                fh.write(json.dumps({"logged_at": now, **row}) + "\n")
    return out


if __name__ == "__main__":
    import sys

    o = run(write="--dry-run" not in sys.argv)
    print(f"levels for {len(o['levels'])} darlings "
          f"({len(o['insufficient_history'])} thin-history)")
    for r in o["levels"][:15]:
        ext = r["extension"] or "n/a"
        print(f"  {r['symbol']:14} close {r['close']:>9} "
              f"buy {r['buy_zone']} stop {r['stop']} ext {ext}")
