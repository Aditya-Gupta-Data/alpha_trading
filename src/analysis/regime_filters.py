"""
src/analysis/regime_filters.py — advisory regime filters for the options engine
================================================================================

Two ADVISORY radars layered onto the options proposer (composed in
market_loop.fetch_market_state, honored in options_proposer.build_proposal —
the vol_bridge pattern). Both FAIL-OPEN: if the underlying data (deals ledger,
sector bars, VIX) is missing or anything throws, they return "no veto" and the
proposer behaves exactly as before. Nothing here places or blocks a trade
directly — it only hands build_proposal a verdict.

  1. SMART-MONEY / SECTOR VETO (Task 1): before a BULLISH index spread, check
     the index's heavyweight constituents. If >=2 of the top-3 show net
     institutional DISTRIBUTION (smart_money_trend) OR the parent sector is not
     structurally bullish (sector_trend), veto the bullish structure.
     **The sector leg is guarded (2026-08-05):** `staleness_guard` checks
     `sector_index_bars` before `sector_trend` is allowed to vote, and drops
     its verdict when the bars are stale. That file currently has no producer
     on any schedule, so the sector half of this veto is OFF until one exists;
     the smart-money half is unaffected and still votes.

  2. WAR PLAYBOOK / CRISIS REGIME (Task 2): if VIX spikes abruptly, sits in
     panic, or the date falls in a known macro-shock window (macro_shocks),
     flag a crisis regime — the proposer then disables SHORT-premium structures
     (iron condors / credit spreads) and only allows defined-risk LONG-premium
     debit spreads to ride the fat tail.
"""
# Index -> its heavyweight constituents (the ones that move it) + parent sector
INDEX_CONSTITUENTS = {
    "NIFTY BANK": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS"],
    "NIFTY 50":   ["RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS"],
}
INDEX_SECTOR = {"NIFTY BANK": "FINANCIALS", "NIFTY 50": None}

VIX_SPIKE_PCT = 0.15     # abrupt day-over-day VIX jump -> crisis
VIX_PANIC = 25.0         # absolute panic level


def _distribution(underlying, deals_by_ticker, as_of=None):
    """>=2 of the index's top-3 constituents in net institutional distribution
    over the last 90d. Returns (bool, detail). Fail-open (None deals -> False).
    The default decision day comes from the shared IST clock, never the host's
    timezone — on a UTC box, date.today() lags IST until 05:30."""
    from src.analysis import smart_money_trend as SM
    tickers = INDEX_CONSTITUENTS.get(underlying, [])
    if not tickers or deals_by_ticker is None:
        return False, "no constituent/deals data"
    if as_of is None:
        from src.market_loop import ist_now
        as_of = ist_now().date().isoformat()
    distributing = []
    for t in tickers:
        niv = SM.net_institutional_volume(deals_by_ticker.get(t, []), as_of, 90)
        if niv["n_deals"] > 0 and niv["net_value_rs"] < 0:
            distributing.append(t)
    return (len(distributing) >= 2,
            f"{len(distributing)}/{len(tickers)} heavyweights distributing "
            f"({', '.join(distributing)})" if distributing else "no distribution")


def _sector_bearish(underlying, staleness_fn=None):
    """Parent sector NOT structurally bullish (below its 50/200 SMA). Fail-open.

    STALENESS GUARD (2026-08-05). `sector_trend` reads
    `data/sector_index_bars.json`, which has NO producer on any schedule — it
    was written once and has never been refreshed. A 50/200 SMA verdict off
    stale bars is wrong QUIETLY, which is worse than absent, so the guard
    self-disables this radar rather than let it vote. Dropping the verdict
    returns the proposer to exactly the baseline behaviour it already has
    whenever this function raises, so a disabled veto can never be worse than
    a broken one. `staleness_fn` is the test seam.
    """
    sector = INDEX_SECTOR.get(underlying)
    if not sector:
        return False, "no sector mapping"
    try:
        if staleness_fn is None:
            from src import staleness_guard
            staleness_fn = staleness_guard.check
        verdict = staleness_fn("sector_index_bars")
        if verdict.get("state") != "fresh":
            return False, (f"sector veto SELF-DISABLED — data "
                           f"{verdict.get('state')}: {verdict.get('reason')}")
    except Exception as exc:
        # The guard itself failing must not silently re-arm the veto it
        # exists to police: no verdict, and say why.
        return False, f"sector veto SELF-DISABLED — guard unavailable [{exc}]"
    try:
        from src.analysis import sector_trend
        v = sector_trend.is_sector_bullish(sector)
        if v.get("bullish") is False:
            return True, f"{sector} sector below its SMAs"
        return False, f"{sector} sector trend ok"
    except Exception:
        return False, "sector data unavailable"


def crisis_regime(vix, prev_vix=None, as_of=None):
    """VIX spike / panic / known macro shock -> crisis. Fail-open."""
    reasons = []
    try:
        if vix is not None and vix >= VIX_PANIC:
            reasons.append(f"VIX {vix:.1f} >= panic {VIX_PANIC}")
        if vix is not None and prev_vix and prev_vix > 0 and (vix / prev_vix - 1) >= VIX_SPIKE_PCT:
            reasons.append(f"VIX spiked {(vix/prev_vix-1)*100:.0f}% d/d")
        if as_of:
            from src.analysis import macro_shocks
            s = macro_shocks.active_shock(as_of)
            if s:
                reasons.append(f"in known shock window '{s}'")
    except Exception:
        pass
    return {"crisis": bool(reasons), "reason": "; ".join(reasons) or "calm"}


def advise(underlying, vix=None, prev_vix=None, as_of=None, deals_by_ticker=None) -> dict:
    """VIEW-INDEPENDENT verdict the proposer applies by view. Fail-open: any
    failure -> a permissive verdict (no block)."""
    try:
        dist, dist_why = _distribution(underlying, deals_by_ticker, as_of)
        secb, sec_why = _sector_bearish(underlying)
        crisis = crisis_regime(vix, prev_vix, as_of)
        return {
            "block_bullish": bool(dist or secb),
            "bullish_reason": f"smart-money/sector veto: {dist_why}; {sec_why}",
            "crisis": crisis["crisis"], "crisis_reason": crisis["reason"],
        }
    except Exception as exc:
        return {"block_bullish": False, "bullish_reason": f"advisory failed-open [{exc}]",
                "crisis": False, "crisis_reason": "advisory failed-open"}
