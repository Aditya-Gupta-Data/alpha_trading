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


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def momentum_score(underlying: str, rs_fn=None, universe=None) -> float:
    """[-1, 1] from relative strength vs the parent sector, or 0.0 when
    there is no reading. Never raises."""
    if underlying in BENCHMARKS:
        # The benchmark cannot outperform itself. 0.0 here is a CORRECT
        # reading, not a missing one.
        return 0.0
    sector = INDEX_SECTOR.get(underlying)
    try:
        if rs_fn is None:
            from src.analysis.sector_trend import get_relative_strength
            rs_fn = get_relative_strength
        verdict = rs_fn(underlying, sector or "")
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
                     macro_fn=None) -> dict:
    """{underlying, momentum, macro, rank} — the full, inspectable read.

    `rank` combines the momentum leg with the ABSOLUTE macro leg: a
    strongly bearish macro read is as tradeable as a strongly bullish one,
    and ranking on the signed value would tilt the book long without
    anyone deciding to."""
    mom = momentum_score(underlying, rs_fn=rs_fn)
    macro = (macro_fn(underlying) if macro_fn
             else macro_score(underlying, conn=conn))
    macro_leg = 0.0 if macro is None else _clamp(abs(float(macro)) / MACRO_SCALE, 0.0, 1.0)
    rank = MOMENTUM_WEIGHT * abs(mom) + MACRO_WEIGHT * macro_leg
    return {"underlying": underlying, "momentum": round(mom, 4),
            "macro": macro, "rank": round(rank, 4)}


def rank_universe(underlyings, conn=None, rs_fn=None, macro_fn=None) -> list:
    """The scored universe, best first. STABLE: equal ranks keep their
    input order, so a flat/absent signal reproduces today's behaviour
    exactly rather than shuffling the list."""
    try:
        scored = [score_underlying(u, conn=conn, rs_fn=rs_fn,
                                   macro_fn=macro_fn)
                  for u in (underlyings or [])]
    except Exception:
        return [{"underlying": u, "momentum": 0.0, "macro": None,
                 "rank": 0.0} for u in (underlyings or [])]
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


def render_line(underlyings, conn=None, rs_fn=None, macro_fn=None) -> str:
    """One human line for a log — what got prioritised and why."""
    ranked = rank_universe(underlyings, conn=conn, rs_fn=rs_fn,
                           macro_fn=macro_fn)
    if not ranked:
        return "underlying router: empty universe"
    parts = []
    for r in ranked:
        macro = "—" if r["macro"] is None else f"{r['macro']:+g}"
        parts.append(f"{r['underlying']} (rank {r['rank']:.2f}, "
                     f"rs {r['momentum']:+.2f}, macro {macro})")
    return "underlying router: " + " > ".join(parts)
