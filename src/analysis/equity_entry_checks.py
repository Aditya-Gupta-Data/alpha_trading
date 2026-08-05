"""
src/analysis/equity_entry_checks.py — the equity shadow path's halt stack
=========================================================================

The amended equity-options mandate (owner-approved 2026-07-19): Law #63
STANDS — equity/equity-option proposals are SHADOW-ONLY until Dept 5
grants authority, and these checks are the composed blocking laws that
gate even the shadow path. Same halt-stack rule as the index engine's
ENTRY_HALT_CHECKS: one ordered tuple, new law = new entry, never a new
call site. Deliberately a SEPARATE list — darling logic never pollutes
the index halt list (two departments, two stacks).

Checks (each: proposal dict -> (ok: bool, reason: str|None)):

  liquidity_filter      FAIL-CLOSED until the F&O bhavcopy (OI data)
                        exists — an equity-options proposal without
                        liquidity evidence is blocked, never waved in.
  expiry_week_halt      physical-settlement defense: no NEW equity-
                        option entries within the final week before the
                        monthly expiry (last Thursday) — delivery
                        obligations and margin spikes live there.
  overextension_halt    Law 3: no fresh delivery buys while the pricer
                        marks the darling `overextended`. An honest
                        abstain (None extension: thin history) does NOT
                        block a delivery buy — but see never_short.
  never_short_darling   Law 3, non-negotiable: bearish structures on a
                        queued Darling are FORBIDDEN, always, regardless
                        of extension state or anything else.

The 1R asymmetry gate is PARKED by owner order (2026-07-19) — it is not
implemented here and nothing R-related is touched.

Proposal shape (the future equity shadow proposer's contract):
  {"symbol", "direction": "long"|"short", "instrument":
   "delivery"|"option", "expiry": "YYYY-MM-DD"|None}
"""
import calendar
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
LEVELS_PATH = ROOT / "data" / "darlings_levels.json"

EXPIRY_BLOCK_DAYS = 7              # calendar days before monthly expiry


def _darlings(queue_path=None) -> set:
    try:
        path = Path(queue_path) if queue_path else QUEUE_PATH
        return set(json.loads(path.read_text()).get("tickers") or [])
    except (OSError, ValueError):
        return set()


def _extension(symbol: str, levels_path=None):
    try:
        path = Path(levels_path) if levels_path else LEVELS_PATH
        for row in json.loads(path.read_text()).get("levels") or []:
            if row.get("symbol") == symbol:
                return row.get("extension")
    except (OSError, ValueError):
        pass
    return None


def monthly_expiry(d: date) -> date:
    """The month's last Thursday (NSE monthly equity-derivatives expiry)."""
    last_day = date(d.year, d.month,
                    calendar.monthrange(d.year, d.month)[1])
    offset = (last_day.weekday() - 3) % 7          # 3 = Thursday
    return last_day - timedelta(days=offset)


LIQUIDITY_PATH = ROOT / "data" / "fo_liquidity.json"
LIQUIDITY_MAX_AGE_DAYS = 7      # a stale tier file is no evidence at all


def liquidity_filter(proposal: dict, liquidity_path=None,
                     today: date = None, **_) -> tuple:
    """Wired 2026-07-20 to the fo_bhavcopy clerk's tier file: an equity-
    OPTION proposal passes ONLY on a tier1 underlying (top-N by stock-
    options traded value, not in the exchange ban list) from a FRESH
    snapshot. Missing/stale/unknown -> FAIL-CLOSED, exactly as before —
    absence of evidence never waves an option through. Delivery (cash)
    proposals don't need options liquidity; the exchange BAN list blocks
    even those in F&O-ban names' options."""
    if proposal.get("instrument") != "option":
        return (True, None)
    try:
        path = Path(liquidity_path) if liquidity_path else LIQUIDITY_PATH
        snap = json.loads(path.read_text())
        as_of = date.fromisoformat(snap.get("as_of", ""))
        if ((today or date.today()) - as_of).days > LIQUIDITY_MAX_AGE_DAYS:
            return (False, f"liquidity snapshot stale ({snap.get('as_of')})"
                           " — fail-closed")
        row = (snap.get("symbols") or {}).get(proposal.get("symbol", ""))
        if row is None:
            return (False, "not an F&O underlying — no options liquidity")
        if row.get("tier") == "banned":
            return (False, "exchange F&O BAN list (MWPL) — blocked")
        if row.get("tier") != "tier1":
            return (False, f"liquidity {row.get('tier')} (rank "
                           f"{row.get('rank')}) — tier1 only")
        return (True, None)
    except (OSError, ValueError):
        return (False, "no F&O liquidity data — fail-closed")


def expiry_week_halt(proposal: dict, today: date = None, **_) -> tuple:
    """Physical-settlement defense: block new equity-option entries in
    the final week before monthly expiry."""
    if proposal.get("instrument") != "option":
        return (True, None)
    today = today or date.today()
    exp = monthly_expiry(today)
    if 0 <= (exp - today).days <= EXPIRY_BLOCK_DAYS:
        return (False, f"expiry week ({exp.isoformat()}) — physical "
                       "settlement defense")
    return (True, None)


def overextension_halt(proposal: dict, levels_path=None, **_) -> tuple:
    """Law 3: no fresh delivery buys while overextended. Abstained
    extension (thin history) does not block."""
    if proposal.get("direction") != "long" or \
            proposal.get("instrument") != "delivery":
        return (True, None)
    if _extension(proposal.get("symbol"), levels_path) == "overextended":
        return (False, "overextended above both DMAs — wait for the "
                       "pullback to the zone")
    return (True, None)


def never_short_darling(proposal: dict, queue_path=None, **_) -> tuple:
    """Law 3, non-negotiable: a queued Darling is long-bias only."""
    if proposal.get("direction") == "short" and \
            proposal.get("symbol") in _darlings(queue_path):
        return (False, "NEVER SHORT A DARLING — long-bias only, "
                       "non-negotiable")
    return (True, None)


# --------------------------------------------------- corporate risk (08-05)

RISK_FLAGS = ("STRUCTURAL_RISK", "LEGAL_RISK")
RISK_LOOKBACK_DAYS = 90        # how long a filed risk event keeps blocking


def _events_as_of(lake_root=None):
    """The newest partition day in the events lake, or None when there is
    no lake at all (None => the guard falls back to mtime, and a missing
    directory reads as `missing`, which the halt treats as not-fresh)."""
    try:
        from src import lake
        days = lake.list_days("events", root=lake_root)
        return days[-1] if days else None
    except Exception:
        return None


def recent_risk_events(symbol: str, today: date = None,
                       lookback_days: int = RISK_LOOKBACK_DAYS,
                       lake_root=None) -> list:
    """Every STRUCTURAL_RISK / LEGAL_RISK announcement filed for `symbol`
    inside the lookback window, newest first.

    Reads the `events` lake that `ingestion/corporate_events` writes —
    demerger / scheme of arrangement / slump sale (STRUCTURAL_RISK) and
    SEBI order / fraud / insolvency / NCLT / auditor resignation
    (LEGAL_RISK). Point-in-time: an announcement is only visible from its
    own `as_of` day forward, so a backtest can never see tomorrow's filing.
    Never raises — an unreadable lake returns []."""
    from src import lake
    today = today or date.today()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()
    sym = (symbol or "").upper().replace(".NS", "")
    if not sym:
        return []
    hits = []
    try:
        for _, row in lake.scan("events", start=start, end=end,
                                root=lake_root):
            if not isinstance(row, dict):
                continue
            if (row.get("symbol") or "").upper() != sym:
                continue
            if any(f in RISK_FLAGS for f in (row.get("flags") or [])):
                hits.append(row)
    except Exception:
        return []
    return sorted(hits, key=lambda r: str(r.get("as_of") or ""), reverse=True)


def corporate_risk_halt(proposal: dict, today: date = None,
                        lake_root=None, staleness_fn=None, **_) -> tuple:
    """HARD ENTRY HALT on a filed structural or legal risk event.

    A demerger reprices the instrument underneath us (ledger Issue 15:
    TATAMOTORS demerged into TMPV/TMCV and we were still quoting the old
    id). A SEBI order or an auditor resignation is the market telling us
    the fundamentals we screened on may not be real. Neither is something
    a price-based entry model can see, so this blocks on the FILING, not
    on the chart.

    FRESHNESS IS PART OF THE VERDICT. `corporate_events` has no producer
    on any schedule — the lake currently ends 2026-07-16 — and a halt that
    reads a dead feed returns "no risk found" for every ticker forever.
    That is not a safety feature, it is theatre, and it is exactly the
    failure `staleness_guard` exists to stop. So:

      fresh feed + risk event   -> BLOCK (fail-closed, as directed)
      fresh feed + no event     -> allow
      STALE/absent feed         -> BLOCK, and say the feed is stale

    The stale branch is fail-CLOSED on purpose and it is the one debatable
    call in this module. The alternative — allowing entries while claiming
    a risk check ran — is how `sector_index_bars` fed a live veto for 20
    days. A halt whose evidence is missing must not pretend it cleared
    anything. If this proves too blunt in practice it is an owner ruling
    to soften, not a quiet edit.
    """
    if proposal.get("direction") == "short":
        return (True, None)               # this halt guards LONG entries only
    symbol = proposal.get("symbol")
    if not symbol:
        return (True, None)

    # PYTEST MUZZLE (the desk_tickers / sleep_phase Task-K precedent). With
    # neither seam injected this default reads the REAL events lake and the
    # REAL staleness registry — the "a new default that reaches live state"
    # defect family, five instances already. Under pytest an uninjected call
    # ABSTAINS so the suite stays hermetic; tests that mean to exercise this
    # halt pass lake_root= and staleness_fn= explicitly, and every one of
    # them does. PYTEST_CURRENT_TEST is set by pytest alone, so production
    # can never take this branch.
    import os
    if os.environ.get("PYTEST_CURRENT_TEST") and \
            lake_root is None and staleness_fn is None:
        return (True, None)

    try:
        if staleness_fn is None:
            from src import staleness_guard
            staleness_fn = staleness_guard.check
        # The lake's newest PARTITION DAY is the honest freshness signal for
        # a date-partitioned dataset — a directory's mtime only says when a
        # folder was last touched, not what day the data covers.
        verdict = staleness_fn("corporate_events",
                               as_of=_events_as_of(lake_root))
        if verdict.get("state") != "fresh":
            return (False, "corporate-risk feed "
                           f"{verdict.get('state')} — cannot clear "
                           f"{symbol} of filed risk events "
                           f"({verdict.get('reason')})")
    except Exception as exc:
        return (False, f"corporate-risk feed unreadable [{exc}] — "
                       f"cannot clear {symbol}")

    hits = recent_risk_events(symbol, today=today, lake_root=lake_root)
    if hits:
        top = hits[0]
        flags = "/".join(f for f in (top.get("flags") or [])
                         if f in RISK_FLAGS)
        return (False, f"{flags} filed {top.get('as_of')}: "
                       f"{str(top.get('subject'))[:120]}")
    return (True, None)


# The composed halt stack — order matters, first block wins.
EQUITY_ENTRY_CHECKS = (never_short_darling, corporate_risk_halt,
                       liquidity_filter, expiry_week_halt,
                       overextension_halt)


def check_entry(proposal: dict, **kwargs) -> dict:
    """Walk the stack. -> {"allowed": bool, "blocked_by": str|None,
    "reason": str|None}. Pure and injectable (kwargs pass queue/levels
    paths + today for tests)."""
    for check in EQUITY_ENTRY_CHECKS:
        ok, reason = check(proposal, **kwargs)
        if not ok:
            return {"allowed": False, "blocked_by": check.__name__,
                    "reason": reason}
    return {"allowed": True, "blocked_by": None, "reason": None}
