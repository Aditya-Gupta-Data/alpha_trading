"""
scripts/gex_planner_dry_run.py — one trade_planner plan from a stored GEX day row (decision #155). Read-only.

Reads data/lake/gex/<slug>/ (written by backfill_gex_profiles.py), takes S* and the regime at spot from the
combined calibrated profile (the ruled non_client dealer, #154; the naive re-priced profile when that day has
no participant OI, labelled), the session move from the previous stored day, and prints the planner's plan.
Places nothing, writes nothing, calls no market data.

  python3 scripts/gex_planner_dry_run.py --slug nifty --root data/vm_lake_copy
  python3 scripts/gex_planner_dry_run.py --slug nifty --root data/vm_lake_copy --day 2026-10-16 --trend neutral
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import lake                                   # noqa: E402
from src import trade_planner as tp                    # noqa: E402
from src.ingestion import participant_oi as poi        # noqa: E402


def state_for(slug: str, day: str = None, root=None, trend: str = None, dealer: str = poi.DEFAULT_DEALER) -> dict:
    """The planner's technical_state from the stored GEX rows, or {} when the day is missing."""
    days = lake.list_days(f"gex/{slug}", root)
    if day is None and days:
        day = days[-1]
    if day not in days:
        return {}
    row = lake.read_day(f"gex/{slug}", day, root=root)[0]
    prev_days = [d for d in days if d < day]
    prev = lake.read_day(f"gex/{slug}", prev_days[-1], root=root)[0] if prev_days else None
    comb = row.get("combined") or {}
    read = (comb.get("calibrated") or {}).get(dealer)
    basis = f"calibrated ({dealer})"
    if not read:
        read, basis = comb.get("repriced") or {}, "naive (no participant OI that day)"
    spot = row.get("spot")
    momentum = (round((spot / prev["spot"] - 1) * 100, 3)
                if prev and prev.get("spot") and spot else None)
    return {"spot": spot, "underlying": row.get("underlying"), "vix": row.get("vix"),
            "trend": trend or "neutral", "momentum_pct": momentum,
            "gex": {"zero_gamma": read.get("zero_gamma"), "regime_at_spot": read.get("regime_at_spot")},
            "_meta": {"day": day, "previous_day": prev_days[-1] if prev_days else None, "gex_basis": basis,
                      "net_gex_cr": read.get("net_gex_cr")}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug", default="nifty")
    ap.add_argument("--root", help="lake root holding gex/<slug>/ (default data/lake)")
    ap.add_argument("--day", help="YYYY-MM-DD (default: the latest stored day)")
    ap.add_argument("--trend", help="the SMA trend the matrix would use (default neutral; short gamma ignores it)")
    args = ap.parse_args(argv)
    state = state_for(args.slug, args.day, args.root, args.trend)
    if not state:
        print(f"no gex/{args.slug} row for {args.day or 'any day'} under {args.root or lake.LAKE_ROOT}")
        return 1
    meta = state.pop("_meta")
    plan = tp.map_technical_to_strategy(state)
    keep = ("strategy", "view", "tradeable", "gamma_zone", "net_debit", "net_credit", "max_profit",
            "max_loss", "rationale")
    print(json.dumps({"input": dict(state, **meta),
                      "plan": {k: plan.get(k) for k in keep},
                      "legs": [{k: l.get(k) for k in ("side", "option_type", "strike")} for l in plan.get("legs", [])]},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
