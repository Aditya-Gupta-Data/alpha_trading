"""
Alpha Trading — Phase 7: the Time-Travel Simulator
==================================================

Replays history through the EXISTING proposal + resolution pipeline to
generate the resolved-trade experience the learning stack is starving
for: `graph_edges` (via the Sleep Phase's causal writer) and, later, the
Phase 11 skeptic's training set — without waiting months of real time.

For each trading day D in [start, end], per underlying:

  1. AS-OF ANALYSIS   the same SMA50/200 + RSI read suggestions.analyze()
                      makes today, computed over only the closes known by
                      D (never a byte of future data enters a proposal).
  2. PROPOSE          options_proposer.build_proposal() — the REAL Phase 5
                      logic (regime map, VIX gate, strike selection,
                      max-loss sizing) — fed a synthetic option chain
                      (premiums modeled from spot/VIX/time; historical
                      chains aren't available) and a SIMULATED portfolio.
  3. AUTO-APPROVE     the entry is shaped exactly like a journal entry but
                      NEVER touches data/journal.jsonl or the real paper
                      book — it lives only in brain_map.db.
  4. PEEK & RESOLVE   plan_tracker's own pure helpers (_resolve_spread,
                      _spread_exit_costs) scan the REAL daily bars after D
                      — so exits, the 65% profit take, the pre-expiry
                      gamma rule, and the FULL 2026 fiscal friction stack
                      (STT sell-side, stamp duty, brokerage, exchange/SEBI
                      charges, GST, liquidity-laddered slippage) are
                      byte-identical to live tracking. "Win" here means
                      what it means in production.
  5. RECORD           one row in the new `simulated_trades` table (owned
                      here, additive — core brain_map tables untouched)
                      keyed by a DETERMINISTIC `sim:<hash>` journal_ref,
                      plus the standard brain_map.record_resolved_entry()
                      write (outcomes + events + links) under the same
                      ref — which is exactly what the Sleep Phase's Task D
                      (write_causal_links, decision #34) reads to mint
                      graph edges. Everything is idempotent: re-running
                      the same range adds NOTHING.

SAFETY (hard rules):
  * No broker anywhere — dhan_client is data-only by project rule
    (decision #11), and this module places nothing, journals nothing,
    notifies nothing (no notifier/discord imports; guard-tested).
  * The real paper state is untouchable: portfolio.json is never loaded
    or saved (a plain dict book is injected), journal.jsonl never written.
  * Offline-first: every input (bars, VIX series, extractor, clock) is
    injectable; network fetches happen only in the CLI convenience path.

Run from the project folder (needs a valid Dhan token for the history):

    python3 -m src.simulator --start 2025-01-01 --end 2025-03-31
    python3 -m src.simulator --start 2025-01-01 --end 2025-03-31 --skip-causal
"""

import argparse
import hashlib
import json
from datetime import date, timedelta

from src import brain_map
from src import plan_tracker as pt
from src.config import MOVING_AVERAGE_FAST, MOVING_AVERAGE_SLOW
from src.indicators import rsi, sma
from src.options_proposer import LOT_SIZES, MIN_DAYS_TO_EXPIRY, build_proposal

RSI_PERIOD = 14                      # same as suggestions.py
SIM_BOOK_CASH = 1_000_000.0          # simulated portfolio — never the real one
STRIKE_STEPS = {"NIFTY 50": 50.0, "NIFTY BANK": 100.0}
CHAIN_SPAN_STEPS = 25                # strikes each side of spot
DEFAULT_SIGMA_PCT = 12.0             # premium model vol when VIX is unknown

# Owned by this module — additive to brain_map's tables, same .db file.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS simulated_trades (
    journal_ref   TEXT PRIMARY KEY,   -- deterministic "sim:<hash>" (idempotency)
    underlying    TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    view          TEXT,
    proposed_on   TEXT NOT NULL,
    expiry        TEXT NOT NULL,
    vix           REAL,
    net_credit    REAL,
    net_debit     REAL,
    spread_width  REAL,
    max_loss      REAL,               -- per lot
    max_profit    REAL,               -- per lot
    lots          INTEGER,
    lot_size      INTEGER,
    resolution    TEXT NOT NULL,
    exit_date     TEXT NOT NULL,
    pnl_net       REAL NOT NULL,      -- after the full 2026 friction stack
    frictions_rs  REAL NOT NULL,
    slippage_rs   REAL NOT NULL,
    capture_pct   REAL,
    r_multiple    REAL,
    result        TEXT NOT NULL CHECK (result IN ('win', 'loss', 'scratch')),
    verdict       TEXT
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def sim_ref(underlying: str, day: str, strategy: str, expiry: str) -> str:
    """The deterministic journal_ref for one simulated trade — same inputs,
    same ref, forever. This single key is what makes every write in the
    pipeline (simulated_trades PK, outcomes UNIQUE, event links) idempotent."""
    key = f"{underlying}|{day}|{strategy}|{expiry}"
    return "sim:" + hashlib.sha1(key.encode()).hexdigest()[:12]


def analysis_from_closes(underlying: str, closes: list) -> dict | None:
    """suggestions.analyze()'s exact read, computed over an injected
    as-of-date closes slice instead of a live fetch. None when there isn't
    enough history (needs MOVING_AVERAGE_SLOW+1 closes), same as live."""
    if closes is None or len(closes) < MOVING_AVERAGE_SLOW + 1:
        return None
    uptrend_today = sma(closes, MOVING_AVERAGE_FAST) > sma(closes, MOVING_AVERAGE_SLOW)
    uptrend_yesterday = (sma(closes[:-1], MOVING_AVERAGE_FAST)
                         > sma(closes[:-1], MOVING_AVERAGE_SLOW))
    return {
        "ticker": underlying,
        "uptrend": uptrend_today,
        "fresh_cross": uptrend_today != uptrend_yesterday,
        "rsi": rsi(closes[-(RSI_PERIOD * 3):], RSI_PERIOD),
        "price": closes[-1],
    }


def next_expiry(day: date, min_days: int = MIN_DAYS_TO_EXPIRY) -> str:
    """The first Thursday (NSE index weekly expiry day) at least `min_days`
    out from `day` — the simulator's stand-in for get_expiry_list()."""
    candidate = day + timedelta(days=min_days)
    while candidate.weekday() != 3:  # Thursday
        candidate += timedelta(days=1)
    return candidate.isoformat()


def build_synthetic_chain(spot: float, vix: float, days_to_expiry: int,
                          step: float) -> dict:
    """A Dhan-shaped option chain modeled from spot/VIX/time — historical
    chains aren't retrievable, so entry premiums are modeled: intrinsic
    plus an ATM time value of ~0.4·S·σ·√T decaying with strike distance.
    Consistent with the tracker's own model-based leg marks
    (plan_tracker._leg_model_premium), so entry and exit live in the same
    modeled world and P&L is internally coherent."""
    sigma = ((vix if vix else DEFAULT_SIGMA_PCT) / 100.0)
    t_years = max(days_to_expiry, 1) / 365.0
    atm_tv = 0.4 * spot * sigma * (t_years ** 0.5)
    decay_scale = max(1.0, 2.5 * spot * sigma * (t_years ** 0.5))
    atm = round(spot / step) * step
    oc = {}
    for i in range(-CHAIN_SPAN_STEPS, CHAIN_SPAN_STEPS + 1):
        strike = atm + i * step
        dist = abs(strike - spot)
        tv = max(0.5, atm_tv * pow(2.718281828, -dist / decay_scale))
        ce = max(0.5, spot - strike) + tv if spot > strike else tv
        pe = max(0.5, strike - spot) + tv if strike > spot else tv
        oc[f"{strike:.6f}"] = {"ce": {"last_price": round(ce, 2)},
                               "pe": {"last_price": round(pe, 2)}}
    return {"last_price": spot, "oc": oc}


def _entry_for(proposal: dict, day: str, ref: str) -> dict:
    """A journal-entry-shaped dict for the resolution helpers and the Brain
    Map — auto-approved, clearly marked simulated, NEVER journaled."""
    spread = proposal["spread"]
    return {
        "short_id": ref,
        "date": day,
        "action": "SPREAD",
        "ticker": proposal["ticker"],
        "shares": proposal["shares"],
        "price": proposal["price"],
        "signal": f"sim {spread['strategy']} ({proposal['view']} view)",
        "decision": "approved",
        "why": "(simulated — Phase 7 time-travel replay)",
        "pattern_tags": [spread["strategy"], proposal["view"]],
        "spread": spread,
        "outcome": None,
    }


def _resolve_and_score(entry: dict, bars: list) -> dict | None:
    """plan_tracker's spread resolution + the SAME outcome arithmetic its
    live sweep applies (gross P&L -> full 2026 frictions + slippage ->
    pnl_net -> capture % -> risk-based R) — minus anything that touches
    real state (_settle_spread_cash is deliberately never called). Returns
    the outcome dict, or None while the range ends before an exit fires."""
    hit = pt._resolve_spread(entry, bars)
    if hit is None:
        return None
    resolution, m_exit, frac_left, exit_day = hit
    spread = entry["spread"]
    qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
    gross_pnl = (m_exit - pt._spread_entry_mark(spread)) * qty

    exit_close = next(float(b[3]) for b in bars if b[0] == exit_day)
    frictions, slippage = pt._spread_exit_costs(spread, exit_close, frac_left)
    pnl_net = round(gross_pnl - frictions - slippage, 2)

    lots = int(spread.get("lots", 1))
    max_profit_total = float(spread["max_profit"]) * lots
    max_loss_total = float(spread["max_loss"]) * lots
    capture_pct = (gross_pnl / max_profit_total * 100) if max_profit_total > 0 else 0.0
    return {
        "checked": exit_day,
        "resolution": resolution,
        "price": round(m_exit, 2),
        "exit_date": exit_day,
        "pct": round(capture_pct, 2),
        "r_multiple": round(pnl_net / max_loss_total, 2) if max_loss_total > 0 else None,
        "days_in_trade": (date.fromisoformat(exit_day)
                          - date.fromisoformat(entry["date"])).days,
        "pnl_rs": pnl_net,
        "frictions_rs": round(frictions, 2),
        "slippage_rs": round(slippage, 2),
        "exit_style": "atomic_basket",
        "hypothetical": False,
        "position_closed": False,   # simulated book — nothing real to close
        "simulated": True,
        "verdict": pt._spread_verdict(entry, resolution, pnl_net, capture_pct),
    }


def _record(conn, entry: dict, vix) -> None:
    """One resolved simulated trade -> simulated_trades row + the standard
    Brain Map write (outcomes/events/links under the same sim: ref). Both
    idempotent; INSERT OR IGNORE means a re-run is a no-op."""
    s, o = entry["spread"], entry["outcome"]
    result = ("win" if o["pnl_rs"] > 0 else
              "loss" if o["pnl_rs"] < 0 else "scratch")
    conn.execute(
        "INSERT OR IGNORE INTO simulated_trades (journal_ref, underlying, "
        "strategy, view, proposed_on, expiry, vix, net_credit, net_debit, "
        "spread_width, max_loss, max_profit, lots, lot_size, resolution, "
        "exit_date, pnl_net, frictions_rs, slippage_rs, capture_pct, "
        "r_multiple, result, verdict) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry["short_id"], entry["ticker"], s["strategy"],
         entry["pattern_tags"][1] if len(entry["pattern_tags"]) > 1 else None,
         entry["date"], s["expiry"], vix, s.get("net_credit"),
         s.get("net_debit"), s.get("spread_width"), s.get("max_loss"),
         s.get("max_profit"), int(s.get("lots", 1)), int(s["lot_size"]),
         o["resolution"], o["exit_date"], o["pnl_rs"], o["frictions_rs"],
         o["slippage_rs"], o["pct"], o["r_multiple"], result, o["verdict"]))
    conn.commit()
    brain_map.record_resolved_entry(conn, entry)


def run_simulation(start: str, end: str, underlyings=("NIFTY 50",), *,
                   conn=None, bars_by_underlying: dict = None,
                   vix_by_date: dict = None) -> dict:
    """The replay loop. `bars_by_underlying` maps underlying ->
    [(date_iso, low, high, close), ...] covering [warmup, end + buffer]
    (inject in tests; the CLI fetches via dhan_client). `vix_by_date` maps
    date_iso -> India VIX close (missing dates -> None: the VIX gate then
    blocks range-bound structures, exactly like a live outage).

    One position per underlying at a time: while a simulated spread is
    open, later days are skipped until its exit date — mirroring the live
    market loop's cool-down spirit at daily resolution."""
    owns_conn = conn is None
    if conn is None:
        conn = brain_map.connect()
    ensure_schema(conn)
    if bars_by_underlying is None:
        bars_by_underlying = {u: _fetch_bars(u, start) for u in underlyings}
    vix_by_date = vix_by_date or {}

    stats = {"days_scanned": 0, "proposed": 0, "resolved": 0,
             "duplicates_skipped": 0, "unresolved_at_range_end": 0,
             "results": {"win": 0, "loss": 0, "scratch": 0}}
    for underlying in underlyings:
        bars = bars_by_underlying.get(underlying) or []
        step = STRIKE_STEPS.get(underlying, 50.0)
        blocked_until = ""  # skip days while a simulated position is open
        for i, (day, _low, _high, _close) in enumerate(bars):
            if not (start <= day <= end) or day <= blocked_until:
                continue
            stats["days_scanned"] += 1
            closes = [float(b[3]) for b in bars[:i + 1]]
            analysis = analysis_from_closes(underlying, closes)
            if analysis is None:
                continue  # not enough history yet at this date
            vix = vix_by_date.get(day)
            expiry = next_expiry(date.fromisoformat(day))
            dte = (date.fromisoformat(expiry) - date.fromisoformat(day)).days
            chain = build_synthetic_chain(analysis["price"], vix, dte, step)
            result = build_proposal(
                underlying, analysis=analysis, vix=vix, expiry=expiry,
                chain=chain, book={"cash": SIM_BOOK_CASH, "holdings": {}},
                prices={})
            if result["proposal"] is None:
                continue
            p = result["proposal"]
            ref = sim_ref(underlying, day, p["spread"]["strategy"], expiry)
            stats["proposed"] += 1
            if conn.execute("SELECT 1 FROM simulated_trades WHERE journal_ref = ?",
                            (ref,)).fetchone():
                stats["duplicates_skipped"] += 1
                blocked_until = conn.execute(
                    "SELECT exit_date FROM simulated_trades WHERE journal_ref = ?",
                    (ref,)).fetchone()["exit_date"]
                continue
            entry = _entry_for(p, day, ref)
            outcome = _resolve_and_score(entry, bars[i:])
            if outcome is None:
                stats["unresolved_at_range_end"] += 1
                break  # bars ran out — later days can't resolve either
            entry["outcome"] = outcome
            _record(conn, entry, vix)
            stats["resolved"] += 1
            stats["results"]["win" if outcome["pnl_rs"] > 0 else
                             "loss" if outcome["pnl_rs"] < 0 else "scratch"] += 1
            blocked_until = outcome["exit_date"]

    if owns_conn:
        conn.close()
    return stats


def encode_causal_links(conn, start: str, extractor=None,
                        today: date = None) -> dict:
    """The feedback loop's last mile: run the Sleep Phase's Task D
    (decision #34 — outcomes only) over a window wide enough to cover the
    simulated range, so simulated post-mortems mint graph_edges exactly
    like real ones. Fail-safe: no Ollama just means no edges this run."""
    from src import sleep_phase
    today = today or date.today()
    window = (today - date.fromisoformat(start)).days + 1
    return sleep_phase.write_causal_links(conn, extractor=extractor,
                                          window_days=window, today=today)


def _fetch_bars(underlying: str, start: str) -> list:
    """CLI-only history fetch (READ-ONLY market data; dhan_client has no
    order capability by hard project rule). Pulls enough pre-start history
    for the 200-day SMA warmup. Imported lazily so the module — and every
    test — stays fully offline."""
    from src.dhan_client import get_ohlc_since
    warmup_start = (date.fromisoformat(start)
                    - timedelta(days=430)).isoformat()
    return [(b["date"], b["low"], b["high"], b["close"])
            for b in get_ohlc_since(underlying, warmup_start)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 7 time-travel simulator")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--underlying", action="append",
                        help="repeatable; default NIFTY 50")
    parser.add_argument("--skip-causal", action="store_true",
                        help="skip the Sleep-Phase causal encoding step")
    args = parser.parse_args()
    underlyings = tuple(args.underlying or ["NIFTY 50"])

    connection = brain_map.connect()
    print(f"Simulator: replaying {', '.join(underlyings)} "
          f"{args.start} -> {args.end} (paper/simulated only)")
    summary = run_simulation(args.start, args.end, underlyings, conn=connection)
    print(f"  {json.dumps(summary)}")
    if not args.skip_causal:
        causal = encode_causal_links(connection, args.start)
        print(f"  causal encoding: {causal}")
    connection.close()
