"""GEX steering for the books the Architect names (decision #160, 2026-10-28) — and the one GEX read the
session playbook uses (#159). Only the CALIBRATED non_client profile (#154) counts; the naive sign never does.

`steer` answers one question per proposal: may a GEX-steered account copy this SMA-built structure? In a
short-gamma zone (#155 D4) only the D6 momentum spread in the session's direction may be copied; every other
zone, or no calibrated read, leaves the SMA routing untouched. Today only PAPER_2L reads it
(`config.GEX_STEERING_ACCOUNTS`): a shadow book settles on the primary's P&L, so steering can refuse a
structure for it but cannot hand it a different one."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
GEX_READ_MAX_AGE_DAYS = 4        # a Friday profile still reads on Monday; older is no read


def latest_gex(underlying: str, today: date = None, root=None) -> dict:
    """The newest GEX profile from BEFORE `today` for one underlying (data/lake/gex, VM cron #38):
    {zero_gamma, regime_at_spot, day} on the calibrated non_client sign, walking back to the newest
    calibrated day within GEX_READ_MAX_AGE_DAYS; {} when there is none."""
    from src import lake
    from src.ingestion.chain_archiver import UNDERLYINGS as SLUGS
    slug = SLUGS.get(underlying)
    if not slug:
        return {}
    today = today or datetime.now(IST).date()
    for day in sorted(lake.list_days(f"gex/{slug}", root), reverse=True):
        age = (today - date.fromisoformat(day)).days
        if age <= 0:
            continue
        if age > GEX_READ_MAX_AGE_DAYS:
            break
        row = (lake.read_day(f"gex/{slug}", day, root=root) or [None])[0] or {}
        cal = ((row.get("combined") or {}).get("calibrated") or {}).get("non_client") or {}
        if cal.get("regime_at_spot") in ("positive", "negative"):
            return {"zero_gamma": cal.get("zero_gamma"), "regime_at_spot": cal["regime_at_spot"], "day": day}
    return {}


def session_move_pct(analysis: dict):
    """Live price vs the previous close in % (the live state's closes end with the live price); None unread."""
    closes = (analysis or {}).get("closes") or []
    try:
        return round((float(closes[-1]) / float(closes[-2]) - 1) * 100, 3)
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return None


def steer(underlying: str, strategy: str, spot, analysis: dict = None, gex_fn=None) -> dict:
    """{allowed, zone, wanted, move_pct, gex, reason} for copying `strategy` into a GEX-steered book."""
    from src.trade_planner import MOMENTUM_MIN_PCT, gamma_zone
    gex = (gex_fn or latest_gex)(underlying) or {}
    zone = gamma_zone(spot, gex.get("zero_gamma"), gex.get("regime_at_spot"))
    move = session_move_pct(analysis)
    out = {"allowed": True, "zone": zone.get("zone"), "wanted": None, "move_pct": move,
           "gex": gex, "reason": None}
    if zone.get("zone") != "short_gamma":
        out["reason"] = f"GEX {zone.get('zone') or 'no calibrated read'}: SMA routing stands"
        return out
    if move is not None and move >= MOMENTUM_MIN_PCT:
        out["wanted"] = "bull_call_spread"
    elif move is not None and move <= -MOMENTUM_MIN_PCT:
        out["wanted"] = "bear_put_spread"
    else:
        out["wanted"] = "long_straddle"
    if strategy == out["wanted"]:
        out["reason"] = f"GEX short gamma ({gex.get('day')}): {strategy} agrees with D6"
        return out
    out["allowed"] = False
    move_txt = "no session move read" if move is None else f"session {move:+.2f}%"
    out["reason"] = (f"gex steering: short-gamma zone ({gex.get('day')}, {move_txt}) wants "
                     f"{out['wanted'].replace('_', ' ')}, the SMA built {str(strategy).replace('_', ' ')} (#160)")
    return out
