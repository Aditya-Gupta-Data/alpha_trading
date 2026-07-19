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


def liquidity_filter(proposal: dict, **_) -> tuple:
    """FAIL-CLOSED: until the F&O bhavcopy clerk supplies OI/contract
    data, no equity-OPTION proposal can prove liquidity -> blocked.
    Delivery (cash) proposals don't need options liquidity."""
    if proposal.get("instrument") == "option":
        return (False, "no F&O liquidity data yet (OI clerk pending) — "
                       "fail-closed")
    return (True, None)


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


# The composed halt stack — order matters, first block wins.
EQUITY_ENTRY_CHECKS = (never_short_darling, liquidity_filter,
                       expiry_week_halt, overextension_halt)


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
