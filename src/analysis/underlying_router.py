"""
src/analysis/underlying_router.py — which underlying gets today's attention
===========================================================================

Level 1, 2026-08-05. The desk went from 2 underlyings to 9 (4 indices + 5
stock options). Proposing on all nine every cycle is not "more
opportunity" — it is nine chances to trip the ONE open-spread-per-
underlying+direction gate (#68) and burn the risk budget on whatever
happens to be scanned first, with no view about which of them is actually
moving. 645 exposure blocks against ~25 entries is what that looks like.

So this ORDERS the universe before the proposer walks it. It is a
PRIORITISER, not a gate:

  * It NEVER blocks anything. Every underlying stays in the list; only the
    order changes. A rank of 0.0 still gets proposed on, last. Blocking is
    Department 3's job (#63 — only Risk blocks), and this module has no
    business acquiring that power by accident.
  * It is ADVISORY-ONLY and fail-open: no score, no sector data, no
    brain_map, an exception anywhere — the original order is returned
    unchanged, which is exactly today's behaviour.

WHAT IT SCORES (all read-only, all already written by something else):

  momentum   `analysis/sector_trend.get_relative_strength` — is this
             index/stock outperforming its parent sector? The signal the
             brief asked for: "if midcaps are outperforming, prioritise
             MIDCPNIFTY".
  macro      the `long_term_macro_score` dimension that `news_processor`
             emits and `brain_map.events` now stores (the Level-1
             dual-horizon work). ABSOLUTE value: a strongly NEGATIVE macro
             read is just as actionable as a positive one — the desk trades
             both directions — and ranking by signed score would quietly
             bias the book long.

Both legs are optional. With neither available the ranking is flat and the
input order survives, so this can never make the desk worse than it was.
"""
from datetime import date

# Weights on the two legs. Momentum leads because it is measured off price
# TODAY; the macro score is a slower, noisier read and gets a supporting
# vote rather than a casting one.
MOMENTUM_WEIGHT = 1.0
MACRO_WEIGHT = 0.6

# Relative-strength spread (in %) that counts as a full-strength read.
# Beyond this the momentum leg saturates, so one violent day cannot
# monopolise the whole ranking.
RS_SATURATION_PCT = 5.0

# The macro dimension is -5..+5 (news_processor's contract).
MACRO_SCALE = 5.0

# Index -> the sector whose relative strength speaks for it. NIFTY 50 is
# the benchmark itself, so it has no "parent" to outperform and scores 0
# on the momentum leg by construction — deliberate, not an omission.
INDEX_SECTOR = {
    "NIFTY BANK": "FINANCIALS",
    "NIFTY FIN SERVICE": "FINANCIALS",
}

# NIFTY MID SELECT has NO parent in config/sector_universe.json (its seven
# sectors are IT/FINANCIALS/PHARMA/AUTO/FMCG/METAL/ENERGY), so on the live
# path `sector_trend` returns an honest error for it and the momentum leg
# reads 0.0 via the fail-open below. That is accurate today rather than
# hidden: the midcap read only becomes real when a midcap index series is
# added to the sector universe. Everything else about the router works for
# it in the meantime.
BENCHMARKS = {"NIFTY 50"}


# How many day-files of the bhavcopy lake to scan. `sector_trend`'s
# RS_LOOKBACK is 63 SESSIONS and it needs 64 closes, so this must cover
# comfortably more than that in CALENDAR days — ~21 sessions a month.
DAILY_BARS_DAYS = 130


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _muzzled() -> bool:
    """True inside the test suite (the same seam `equity_desk` uses for
    market calls, applied here to the on-disk lake)."""
    import os
    return (os.environ.get("IS_TEST_ENV", "").strip().lower()
            in ("1", "true", "yes")
            or bool(os.environ.get("PYTEST_CURRENT_TEST")))


def sector_for(underlying: str, universe=None) -> str | None:
    """The parent whose momentum this underlying is measured against.

    Indices come from INDEX_SECTOR. STOCK underlyings (the five equity-
    option names) were never mapped at all — `INDEX_SECTOR.get()` returned
    None for them, so `sector_trend` was handed an empty sector name and
    could not find index bars either. Their sector is already written down
    in `config/sector_universe.json`'s constituent lists, so it is read
    from there rather than duplicated here (a second copy of a mapping is
    a second thing to let rot)."""
    if underlying in INDEX_SECTOR:
        return INDEX_SECTOR[underlying]
    try:
        if universe is None:
            from src.analysis.sector_trend import load_universe
            universe = load_universe()
        for sector, meta in (universe or {}).items():
            if underlying in (meta.get("constituents") or []):
                return sector
    except Exception:
        pass
    return None


# {(symbol, session_date): bars} — the lake is re-read once per symbol per
# DAY, not once per 15-minute cycle. A 63-session return does not move
# intraday, and re-parsing 130 day-files nine times a cycle would be the
# most expensive thing in the loop by a wide margin.
_BARS_CACHE = {}


def daily_bars(underlying: str, days: int = DAILY_BARS_DAYS, today=None,
               lake_dir=None) -> list:
    """Daily bars for `underlying` in `sector_trend`'s (date, low, high,
    close) shape, oldest first — [] when we honestly have none.

    THE 2026-08-07 BUG. `momentum_score` called `get_relative_strength`
    without `stock_bars`, and that function's contract is that
    `stock_bars` MUST be supplied while the live price path is token-
    gated. So it returned an error dict on every call, every leg read
    0.0, the ranking was uniformly flat, and `prioritise` — a stable sort
    over equal keys — never reordered anything. The router had been
    shipping as a no-op since 2026-08-05.

    The source is the LOCAL bhavcopy lake: already on disk, already
    refreshed nightly by its own cron, no token, no API call and so no
    rate contention with the live loop. INDICES are absent from an equity
    bhavcopy by construction, so they return [] here and keep their
    honest 0.0 — an index cannot be priced off a file that only carries
    equities, and inventing one would be exactly the wrong fix."""
    symbol = str(underlying or "")
    if not symbol.endswith(".NS"):
        return []
    key = (symbol, str(today or date.today()))
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    if lake_dir is None and _muzzled():
        # Hermetic tests do not walk the real 467 MB lake: 130 file reads
        # per underlying is exactly the "slow test is a bug report" case.
        # A test that wants bars passes `lake_dir` (or primes the cache).
        return []
    bars = []
    try:
        from src.ingestion.bhavcopy_clerk import bars_for
        for b in bars_for(symbol, days=days, lake_dir=lake_dir):
            close = b.get("close")
            if close is None:
                continue           # a NULL-honest row is skipped, not zeroed
            bars.append((b.get("date") or b.get("session"),
                         b.get("low"), b.get("high"), float(close)))
    except Exception:
        bars = []
    _BARS_CACHE[key] = bars
    return bars


def prime_bars(underlyings, days: int = DAILY_BARS_DAYS, today=None,
               lake_dir=None) -> int:
    """Load the whole universe's bars in ONE pass over the day-files.

    `bars_for` re-parses every file per symbol, so five stocks cost five
    passes over 130 files — measured at ~10s on the Mac and slower on the
    1 GB e2-micro. `bars_for_many` parses each file exactly once. Same
    cache, same shape; purely how many times the disk is read. Returns
    how many symbols were primed; never raises."""
    if _muzzled() and lake_dir is None:
        return 0
    day = str(today or date.today())
    wanted = [u for u in (underlyings or [])
              if str(u).endswith(".NS") and (str(u), day) not in _BARS_CACHE]
    if not wanted:
        return 0
    try:
        from src.ingestion.bhavcopy_clerk import bars_for_many
        batch = bars_for_many(wanted, days=days, lake_dir=lake_dir)
    except Exception:
        return 0
    primed = 0
    for u in wanted:
        rows = batch.get(str(u).split(".")[0].strip().upper()) or []
        bars = [(b.get("date") or b.get("session"), b.get("low"),
                 b.get("high"), float(b["close"]))
                for b in rows if b.get("close") is not None]
        _BARS_CACHE[(str(u), day)] = bars
        primed += 1
    return primed


def momentum_score(underlying: str, rs_fn=None, universe=None,
                   bars_fn=None) -> float:
    """[-1, 1] from relative strength vs the parent sector, or 0.0 when
    there is no reading. Never raises."""
    if underlying in BENCHMARKS:
        # The benchmark cannot outperform itself. 0.0 here is a CORRECT
        # reading, not a missing one.
        return 0.0
    sector = sector_for(underlying, universe=universe)
    try:
        if rs_fn is None:
            from src.analysis.sector_trend import get_relative_strength
            rs_fn = get_relative_strength
        bars = (bars_fn or daily_bars)(underlying)
        verdict = rs_fn(underlying, sector or "", stock_bars=bars)
        spread = (verdict or {}).get("rs_spread_pct")
        if spread is None:
            return 0.0
        return _clamp(float(spread) / RS_SATURATION_PCT)
    except Exception:
        return 0.0


def macro_score(underlying: str, conn=None, lookback_days: int = 30,
                today: date = None) -> float | None:
    """The freshest `long_term_macro_score` recorded for this underlying,
    or None when nothing was stored.

    Reads the dimensional column the Level-1 dual-horizon work added to
    `brain_map.events`. Returns the RAW -5..+5 score (the caller decides
    whether to use its sign or its magnitude); None is honest absence and
    is never coerced to 0, because "no macro read" and "neutral macro
    read" must stay distinguishable."""
    own = conn is None
    try:
        if conn is None:
            from src import brain_map
            conn = brain_map.connect()
        try:
            row = conn.execute(
                "SELECT long_term_macro_score FROM events "
                "WHERE event_type = 'news' AND ticker = ? "
                "AND long_term_macro_score IS NOT NULL "
                "ORDER BY date DESC, id DESC LIMIT 1",
                (underlying,)).fetchone()
        finally:
            if own:
                conn.close()
        if row is None:
            return None
        return float(row[0])
    except Exception:
        return None


def score_underlying(underlying: str, conn=None, rs_fn=None,
                     macro_fn=None, bars_fn=None) -> dict:
    """{underlying, momentum, macro, rank, rs_bars} — the full,
    inspectable read.

    `rank` combines the momentum leg with the ABSOLUTE macro leg: a
    strongly bearish macro read is as tradeable as a strongly bullish one,
    and ranking on the signed value would tilt the book long without
    anyone deciding to.

    `rs_bars` (2026-08-07) is how many daily bars the momentum leg
    actually had. It exists so the log can distinguish a MEASURED 0.00
    from an UNMEASURED one — the two are indistinguishable in the rank
    alone, and that is precisely how a dead router went unnoticed for two
    sessions."""
    mom = momentum_score(underlying, rs_fn=rs_fn, bars_fn=bars_fn)
    macro = (macro_fn(underlying) if macro_fn
             else macro_score(underlying, conn=conn))
    macro_leg = 0.0 if macro is None else _clamp(abs(float(macro)) / MACRO_SCALE, 0.0, 1.0)
    rank = MOMENTUM_WEIGHT * abs(mom) + MACRO_WEIGHT * macro_leg
    try:
        n_bars = len((bars_fn or daily_bars)(underlying))
    except Exception:
        n_bars = 0
    return {"underlying": underlying, "momentum": round(mom, 4),
            "macro": macro, "rank": round(rank, 4), "rs_bars": n_bars}


def rank_universe(underlyings, conn=None, rs_fn=None, macro_fn=None,
                  bars_fn=None) -> list:
    """The scored universe, best first. STABLE: equal ranks keep their
    input order, so a flat/absent signal reproduces today's behaviour
    exactly rather than shuffling the list."""
    try:
        if bars_fn is None:
            try:
                prime_bars(underlyings)     # one disk pass for the universe
            except Exception:
                pass
        scored = [score_underlying(u, conn=conn, rs_fn=rs_fn,
                                   macro_fn=macro_fn, bars_fn=bars_fn)
                  for u in (underlyings or [])]
    except Exception:
        return [{"underlying": u, "momentum": 0.0, "macro": None,
                 "rank": 0.0, "rs_bars": 0} for u in (underlyings or [])]
    return sorted(scored, key=lambda r: -r["rank"])


def prioritise(underlyings, conn=None, rs_fn=None, macro_fn=None) -> tuple:
    """The one call the market loop makes: the same universe, reordered.

    NOTHING IS EVER DROPPED — this is a prioritiser, not a filter. Only
    Risk may block (#63), and a router that silently shortened the
    universe would be acquiring that authority by accident. Fail-open to
    the input order."""
    try:
        ranked = rank_universe(underlyings, conn=conn, rs_fn=rs_fn,
                               macro_fn=macro_fn)
        out = tuple(r["underlying"] for r in ranked)
        return out if len(out) == len(tuple(underlyings)) else tuple(underlyings)
    except Exception:
        return tuple(underlyings or ())


def render_line(underlyings, conn=None, rs_fn=None, macro_fn=None,
                bars_fn=None) -> str:
    """One human line for a log — what got prioritised and why.

    An UNMEASURED momentum leg prints `rs —`, never `rs +0.00`: a
    fabricated zero reading next to a real one is the same class of lie
    the whole module is written to avoid."""
    ranked = rank_universe(underlyings, conn=conn, rs_fn=rs_fn,
                           macro_fn=macro_fn, bars_fn=bars_fn)
    if not ranked:
        return "underlying router: empty universe"
    parts = []
    for r in ranked:
        macro = "—" if r["macro"] is None else f"{r['macro']:+g}"
        rs = (f"{r['momentum']:+.2f}" if r.get("rs_bars")
              else "— (no bars)")
        parts.append(f"{r['underlying']} (rank {r['rank']:.2f}, "
                     f"rs {rs}, macro {macro})")
    return "underlying router: " + " > ".join(parts)
