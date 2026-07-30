"""H4 pyramid-continuation SHADOW — forward evidence, zero capital.

2026-07-30: the first real-data H4 experiment run (2022-01-01..2025-06-30,
NIFTY 50 — `logs/h4_run_2026-07-30.log` on the Mac, design doc
`docs/h4_simulator_experiment_design.md`) graduated exactly one variant:
pyramid adds gated on a fresh **10-day** extreme (Sortino 2.62 vs baseline
2.42, max drawdown 6.54R vs 6.69R; lookbacks 3 and 5 did NOT graduate —
3-day nearly doubled the drawdown, reproducing the #68 pileup in sim).
Owner ruling same day: **shadow it** (option 2 of record/shadow/graduate).

What this module does, nightly (sleep_phase Task J):
  for each OPEN journaled spread, if TODAY's completed bar shows
    (a) the position's modeled mark improved vs entry (plan_tracker's own
        linear-decay mark — the SAME pricing the live book resolves on), AND
    (b) a fresh 10-day close extreme in the trade's direction,
  then record ONE host-linked row in shadow_trades — the hypothetical add
  the pyramid policy would have made. The existing Sleep-Phase Task I sweep
  resolves it with the HOST's outcome when the host resolves; the forward
  question is whether the continuation condition SELECTS winners, so
  host-outcome inheritance is the honest proxy (same doctrine as the
  opportunity-cost rows, Directive 1).

Execution authority: NONE. This module reads the journal and writes
shadow_trades rows. It is not consulted by sizing, entry, or exits, and
must not be — graduation to `validation/registry.py` is a Department 5
decision gated on the forward record this module accumulates.

Every skip is named; missing data yields a skip, never a fabricated fire.
The DB seam carries the pytest muzzle from THIS first commit (the 07-27
opportunity-cost lesson: a seam that opens its own connection must be
muzzled before its first test exists, not after its first incident).
"""

import os
from datetime import date, datetime

from src.plan_tracker import (_spread_trackable, _spread_mark,
                              _spread_entry_mark)
from src.validation.h4_comparator import _fresh_extreme
from src.validation import trial

SIGNAL = "h4_pyramid_lb10"
LOOKBACK_DAYS = 10          # the ONLY lookback that graduated 2026-07-30
MAX_ADDS = 2                # H4_MAX_STACK=3 total = original + 2 adds
BARS_WINDOW = LOOKBACK_DAYS + 5   # padding for holidays/weekends


def _direction(entry: dict) -> str:
    """bullish/bearish from the structure name — bear_put_spread etc."""
    strategy = str((entry.get("spread") or {}).get("strategy", ""))
    return "bullish" if "bull" in strategy.lower() else "bearish"


def _default_bars_fn(ticker: str) -> list:
    from src.dhan_client import get_daily_ohlc
    return get_daily_ohlc(ticker, days=BARS_WINDOW)


def _mark_improved(entry: dict, close: float, today: date) -> bool:
    """Has the spread's modeled mark improved vs entry? Uses plan_tracker's
    own leg model (intrinsic + linearly-decaying entry time value) so the
    shadow judges improvement on the SAME pricing the live book resolves
    on — no second pricing model (one door per concern)."""
    spread = entry["spread"]
    expiry = date.fromisoformat(spread["expiry"])
    entry_day = date.fromisoformat(entry["date"])
    total_days = max(1, (expiry - entry_day).days)
    frac_left = max(0.0, (expiry - today).days / total_days)
    return (_spread_mark(spread, float(close), frac_left)
            - _spread_entry_mark(spread)) > 0.0


def run_shadow_pass(conn=None, *, entries=None, bars_fn=None,
                    today=None, record_fn=None) -> dict:
    """One nightly pass. Returns a named summary — {scanned, fired,
    skips: {reason: count}} — and never raises past its own boundary.

    Seams: `entries` (journal rows), `bars_fn(ticker) -> daily bars`,
    `today` (date), and either `conn` (sqlite, sleep_phase passes its own)
    or `record_fn(signal=, fire_date=, ticker=, direction=, host_ref=)`.
    With NO seam for the write, the pass opens the real brain_map — which
    is muzzled under pytest so the suite can never poison the live record.
    """
    summary = {"scanned": 0, "fired": 0, "skips": {}}

    def _skip(reason):
        summary["skips"][reason] = summary["skips"].get(reason, 0) + 1

    try:
        today = today or date.today()
        today_iso = today.isoformat()
        if entries is None:
            from src import journal
            entries = journal.read_all()
        bars_fn = bars_fn or _default_bars_fn

        open_spreads = [e for e in entries if _spread_trackable(e)]
        if not open_spreads:
            _skip("no_open_spreads")
            return summary

        own_conn = None
        if record_fn is None and conn is None:
            if os.environ.get("PYTEST_CURRENT_TEST"):
                # THE MUZZLE — same doctrine as exposure_gate's
                # opportunity-cost seam. Tests inject conn or record_fn.
                _skip("muzzled_under_pytest")
                return summary
            from src import brain_map
            own_conn = conn = brain_map.connect()
        if conn is not None:
            trial.ensure_schema(conn)   # the count query below needs the table

        try:
            for entry in open_spreads:
                summary["scanned"] += 1
                try:
                    host = entry.get("short_id")
                    ticker = entry.get("ticker")
                    if not host or not ticker:
                        _skip("entry_missing_id_or_ticker")
                        continue
                    bars = bars_fn(ticker) or []
                    if not bars:
                        _skip("no_bars")
                        continue
                    if bars[-1].get("date") != today_iso:
                        # Weekend/holiday/stale feed: no completed bar for
                        # today -> the signal state is yesterday's, already
                        # recorded. Never re-fire on a stale bar.
                        _skip("no_fresh_bar_today")
                        continue
                    closes = [float(b["close"]) for b in bars]
                    bullish = _direction(entry) == "bullish"
                    if not _fresh_extreme(closes, LOOKBACK_DAYS, bullish):
                        _skip("no_fresh_extreme")
                        continue
                    if not _mark_improved(entry, closes[-1], today):
                        _skip("mark_not_improved")
                        continue
                    direction = "bullish" if bullish else "bearish"
                    if record_fn is not None:
                        record_fn(signal=SIGNAL, fire_date=today_iso,
                                  ticker=ticker, direction=direction,
                                  host_ref=host)
                        summary["fired"] += 1
                        continue
                    prior_adds = conn.execute(
                        "SELECT COUNT(*) FROM shadow_trades "
                        "WHERE host_ref = ? AND pattern_id = ?",
                        (host, f"signal:{SIGNAL}")).fetchone()[0]
                    if prior_adds >= MAX_ADDS:
                        _skip("stack_cap_reached")
                        continue
                    out = trial.record_signal_fire(
                        conn, SIGNAL, today_iso, ticker,
                        direction=direction, host_ref=host)
                    if out["created"]:
                        summary["fired"] += 1
                    else:
                        _skip("already_recorded_today")
                except Exception as e:      # one bad entry never voids the pass
                    _skip(f"entry_error:{type(e).__name__}")
        finally:
            if own_conn is not None:
                own_conn.close()
    except Exception as e:                  # fail-open: shadow bookkeeping
        _skip(f"pass_error:{type(e).__name__}")   # never breaks the caller
    return summary
