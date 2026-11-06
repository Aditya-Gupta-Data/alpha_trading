# MANUAL OFFLINE TOOL — option P&L backtest on REAL NSE option prices (branch feature/option-pnl-sim). No cron.
"""
The options desk replayed on historical NSE F&O closes (`scripts/backfill_fo_history.py`), today's rules:

    python -m src.research.option_pnl_sim --start 2024-01-01 --end 2025-12-31 [--modes live,skip_bearish,bull_only]

Per underlying, per session T while no position is open: the live view (`options_proposer.market_view` on closes up
to T), the live `build_proposal` (strategy, strikes, R:R gate, 2% fractional sizing on a ₹2L account, VIX gate) fed a
chain of T's REAL option closes — only strikes that traded that day — so the decision and the fill share T's close,
as a live order shares its own moment. Each later session the spread is marked on its legs' real closes (settlement
price when a leg did not trade), clamped to its defined-risk bounds, and exits by the live rules: the profit ratchet
for directional structures (#110, applied from day one, not from its 2026-09-24 effective date), 65% profit take for
range structures, the forced pre-expiry window. Costs: `plan_tracker._spread_exit_costs_quoted` (2026 frictions +
slippage ladder on both ends). Not modelled: bid/ask beyond the ladder, GEX/advisory/earnings vetoes that need data
the archive lacks, intraday exits, today's lot sizes are used for sizing (R-multiples are lot-size free).
Modes: live (as is), skip_bearish (bearish views not traded), bull_only (every directional view forced bullish).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FO_DIR = ROOT / "data" / "backtest" / "fo_history"
MACRO_DIR = ROOT / "data" / "lake" / "macro"
NAMES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE",
         "MIDCPNIFTY": "NIFTY MID SELECT", "RELIANCE": "RELIANCE.NS", "HDFCBANK": "HDFCBANK.NS",
         "ICICIBANK": "ICICIBANK.NS", "INFY": "INFY.NS", "TCS": "TCS.NS"}
INDEX_CSV = {"NIFTY": "NIFTY.csv", "BANKNIFTY": "NIFTY_BANK.csv", "FINNIFTY": "NIFTY_FIN_SERVICE.csv"}
EQUITY = 200_000.0
MODES = ("live", "skip_bearish", "bull_only")
# Closes of different strikes print at different moments, so a "spread" can look nearly free; no live book offers that.
MAX_PLAUSIBLE_REWARD_RISK = 9.0


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


class FoHistory:
    """Day files → {symbol: {"spot", "opts": {(expiry, strike, 'CE'|'PE'): (close, settle, volume)}}}."""

    def __init__(self, fo_dir=None):
        self.dir = Path(fo_dir or FO_DIR)
        self.days = sorted(p.name[:10] for p in self.dir.glob("????-??-??.csv.gz"))
        self._load = lru_cache(maxsize=64)(self._read)

    def _read(self, day: str) -> dict:
        out = {}
        with gzip.open(self.dir / f"{day}.csv.gz", "rt") as fh:
            for r in csv.DictReader(fh):
                sym = out.setdefault(r["TckrSymb"], {"spot": None, "opts": {}})
                if sym["spot"] is None:
                    sym["spot"] = _f(r["UndrlygPric"])
                if r["FinInstrmTp"] in ("IDO", "STO") and r["OptnTp"] in ("CE", "PE"):
                    sym["opts"][(r["XpryDt"], float(r["StrkPric"]), r["OptnTp"])] = (
                        _f(r["ClsPric"]), _f(r["SttlmPric"]), _f(r["TtlTradgVol"]) or 0.0)
        return out

    def day(self, d: str) -> dict:
        return self._load(d)


def closes_for(sym: str, fo: FoHistory, macro_dir=None) -> list:
    """[(date, close)] for the view: the index CSV (deep history), else the stock bhavcopy, else the F&O spot."""
    if sym in INDEX_CSV:
        p = Path(macro_dir or MACRO_DIR) / INDEX_CSV[sym]
        if p.exists():
            with p.open() as fh:
                return [(r["date"][:10], float(r["value"])) for r in csv.DictReader(fh) if r.get("value")]
    if not NAMES[sym].endswith(".NS"):
        return [(d, fo.day(d)[sym]["spot"]) for d in fo.days if sym in fo.day(d) and fo.day(d)[sym]["spot"]]
    from src.ingestion import bhavcopy_clerk
    rows = bhavcopy_clerk.bars_for_many([sym], days=1500).get(sym) or []
    return [(b["session"], float(b["close"])) for b in rows if b.get("close")]


def chain_for(day_sym: dict, expiry: str) -> dict:
    """A Dhan-shaped chain of every listed strike at that expiry; only a leg that TRADED that day carries a price
    (its close), so the live `_leg_fill` refuses an untraded leg exactly as it refuses a dead quote."""
    oc = {}
    for (exp, strike, kind), (close, _settle, vol) in day_sym["opts"].items():
        if exp == expiry:
            oc.setdefault(f"{strike:.6f}", {})[kind.lower()] = {"last_price": close if vol > 0 and close else 0}
    return {"last_price": day_sym["spot"], "oc": oc}


def leg_price(day_sym: dict, expiry: str, leg: dict):
    q = day_sym["opts"].get((expiry, float(leg["strike"]), leg["option_type"].upper()))
    if not q:
        return None
    close, settle, vol = q
    return close if vol > 0 and close else settle


def walk(spread: dict, entry_day: str, ticker: str, fo: FoHistory, sym: str):
    """(resolution, exit_day, exit_mark_ps, leg_exit_premiums) by the live exit rules on real marks, or None."""
    from src import plan_tracker as pt
    from src import profit_ratchet as pr
    expiry = spread["expiry"]
    lot = int(spread["lot_size"])
    m_entry = pt._spread_entry_mark(spread)
    max_profit_ps, max_loss_ps = float(spread["max_profit"]) / lot, float(spread["max_loss"]) / lot
    ratcheted = pr.applies(spread, max_profit_ps)
    peak = lock = None
    last = None
    for d in fo.days:
        if d <= entry_day:
            continue
        if d > expiry:
            break
        ds = fo.day(d).get(sym)
        prices = {(float(l["strike"]), l["option_type"].upper()): leg_price(ds, expiry, l) for l in spread["legs"]} \
            if ds else {}
        if not prices or any(v is None for v in prices.values()):
            continue
        m_now = sum((1.0 if l["side"].upper() == "BUY" else -1.0) * prices[(float(l["strike"]), l["option_type"].upper())]
                    for l in spread["legs"])
        profit_ps = max(-max_loss_ps, min(m_now - m_entry, max_profit_ps))
        last = (d, m_entry + profit_ps, prices)
        if ratcheted:
            capture = profit_ps / max_profit_ps * 100.0
            peak = capture if peak is None else max(peak, capture)
            lock = pr.state(peak, lock)["locked_pct"]
            if pr.ratchet_hit(capture, lock):
                return ("ratchet_hit",) + last
        elif max_profit_ps > 0 and profit_ps >= pt.OPTION_PROFIT_TAKE_FRACTION * max_profit_ps:
            return ("profit_take",) + last
        if pt.in_forced_exit_window(ticker, expiry, date.fromisoformat(d)):
            return ("pre_expiry_exit",) + last
    if last and fo.days and fo.days[-1] < expiry:
        return ("open_at_data_end",) + last
    return ("expiry_last_mark",) + last if last else None


def simulate(start: str, end: str, mode: str = "live", symbols=tuple(NAMES), fo: FoHistory = None,
             closes_by_sym: dict = None, vix_by_day: dict = None, equity: float = EQUITY) -> dict:
    from src import plan_tracker as pt
    from src.options_proposer import (_nearer_expiries, build_proposal, has_live_book, horizon_for, market_view,
                                      pick_expiry)
    from src.simulator import analysis_from_closes
    fo = fo or FoHistory()
    vix_by_day = vix_by_day if vix_by_day is not None else _vix()
    trades, refusals = [], defaultdict(int)
    for sym in symbols:
        ticker = NAMES[sym]
        series = (closes_by_sym or {}).get(sym) or closes_for(sym, fo)
        if closes_by_sym is not None:
            closes_by_sym[sym] = series
        idx = {d: i for i, (d, _) in enumerate(series)}
        closes = [c for _, c in series]
        busy_until = ""
        for d in fo.days:
            if not (start <= d <= end) or d <= busy_until or d not in idx:
                continue
            ds = fo.day(d).get(sym)
            a = analysis_from_closes(ticker, closes[:idx[d] + 1])
            if not ds or not ds["spot"] or a is None:
                continue
            view = market_view(a)
            if mode == "skip_bearish" and view == "bearish":
                refusals["skipped bearish view"] += 1
                continue
            override = "bullish" if mode == "bull_only" and view == "bearish" else None
            expiries = sorted({e for (e, _, _) in ds["opts"]})
            today, horizon = date.fromisoformat(d), horizon_for(a)
            expiry = pick_expiry(expiries, today=today, underlying=ticker, horizon=horizon)
            if expiry is None:
                refusals["no usable expiry"] += 1
                continue
            chain = chain_for(ds, expiry)
            if horizon == "long" and not has_live_book(chain, ds["spot"]):     # the live Issue 52 fallback
                for alt in _nearer_expiries(expiries, expiry, today, ticker):
                    alt_chain = chain_for(ds, alt)
                    if has_live_book(alt_chain, ds["spot"]):
                        expiry, chain = alt, alt_chain
                        break
            res = build_proposal(ticker, analysis=a, vix=vix_by_day.get(d), expiry=expiry,
                                 chain=chain, book={"cash": 1e12, "holdings": {}}, prices={},
                                 account_equity=equity, account_cash=equity, today=date.fromisoformat(d),
                                 view_override=override)
            p = res.get("proposal")
            if p is None:
                refusals[str(res.get("reason"))[:60]] += 1
                continue
            spread = p["spread"]
            ml, mp = float(spread["max_loss"]), float(spread["max_profit"])
            if ml <= 0 or mp / ml > MAX_PLAUSIBLE_REWARD_RISK:
                refusals["implausible close-price book (reward:risk > 9)"] += 1
                continue
            hit = walk(spread, d, ticker, fo, sym)
            if hit is None:
                refusals["no later marks"] += 1
                continue
            resolution, exit_day, m_exit, prices = hit
            if resolution == "open_at_data_end":
                refusals["still open when the data ends"] += 1
                busy_until = exit_day
                continue
            qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
            gross = (m_exit - pt._spread_entry_mark(spread)) * qty
            fr, sl = pt._spread_exit_costs_quoted(spread, prices)
            pnl = gross - fr - sl
            max_loss = float(spread["max_loss"]) * int(spread.get("lots", 1))
            trades.append({"underlying": sym, "entry": d, "exit": exit_day, "view": p.get("view"),
                           "strategy": spread["strategy"], "expiry": spread["expiry"], "lots": spread.get("lots"),
                           "resolution": resolution, "pnl_rs": round(pnl, 2), "costs_rs": round(fr + sl, 2),
                           "r": round(pnl / max_loss, 3) if max_loss > 0 else None})
            busy_until = exit_day
    return {"mode": mode, "start": start, "end": end, "trades": trades, "refusals": dict(refusals)}


def _vix(macro_dir=None) -> dict:
    p = Path(macro_dir or MACRO_DIR) / "INDIAVIX.csv"
    if not p.exists():
        return {}
    with p.open() as fh:
        return {r["date"][:10]: float(r["value"]) for r in csv.DictReader(fh) if r.get("value")}


def summarize(trades: list) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades if t["r"] is not None]
    pnl = [t["pnl_rs"] for t in sorted(trades, key=lambda t: t["exit"])]
    eq = peak = dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gains, losses = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    return {"n": n, "win_rate": sum(1 for x in pnl if x > 0) / n, "avg_r": sum(rs) / len(rs) if rs else None,
            "total_r": sum(rs), "total_rs": round(sum(pnl), 2), "costs_rs": round(sum(t["costs_rs"] for t in trades), 2),
            "profit_factor": round(gains / losses, 2) if losses else None, "max_drawdown_rs": round(dd, 2)}


def report(result: dict) -> dict:
    t = result["trades"]
    by = lambda key: {k: summarize([x for x in t if key(x) == k]) for k in sorted({key(x) for x in t})}
    return {"mode": result["mode"], "ALL": summarize(t), "by_strategy": by(lambda x: x["strategy"]),
            "by_underlying": by(lambda x: x["underlying"]), "by_year": by(lambda x: x["entry"][:4]),
            "by_resolution": by(lambda x: x["resolution"]), "refusals": result["refusals"]}


def lines(rep: dict) -> list:
    def row(name, s):
        if not s.get("n"):
            return f"  {name:22s} n=0"
        pf = "—" if s["profit_factor"] is None else s["profit_factor"]
        return (f"  {name:22s} n={s['n']:<4d} win={s['win_rate']:.0%} avgR={s['avg_r']:+.2f} totalR={s['total_r']:+.1f} "
                f"₹{s['total_rs']:+,.0f} PF={pf} maxDD=₹{s['max_drawdown_rs']:,.0f} costs=₹{s['costs_rs']:,.0f}")
    out = [f"\nMODE {rep['mode']} (BACKTEST on real NSE closes, ₹2L account, today's rules)", row("ALL", rep["ALL"])]
    for title in ("by_strategy", "by_year", "by_underlying", "by_resolution"):
        out.append(f" {title}:")
        out += [row(k, s) for k, s in rep[title].items()]
    top = sorted(rep["refusals"].items(), key=lambda kv: -kv[1])[:6]
    out.append(" refusals: " + "; ".join(f"{k} ×{v}" for k, v in top))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="option P&L backtest on real NSE option closes")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--json", help="write trades + reports here")
    a = ap.parse_args(argv)
    fo, vix, closes = FoHistory(), _vix(), {}
    out = {}
    for mode in a.modes.split(","):
        res = simulate(a.start, a.end, mode, fo=fo, vix_by_day=vix, closes_by_sym=closes)
        rep = report(res)
        out[mode] = {"report": rep, "trades": res["trades"]}
        print("\n".join(lines(rep)), flush=True)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
