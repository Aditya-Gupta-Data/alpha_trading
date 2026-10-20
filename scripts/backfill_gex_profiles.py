"""
scripts/backfill_gex_profiles.py — GEX profiles from the archived EOD chains (decision #152).

READ-ONLY over data/lake/chains/<slug>/ (and participant_oi/ for the calibrated sign); writes only
data/lake/gex/<slug>/date=D/part.jsonl.gz (one row per day, replaced on re-run). No Dhan call, no journal,
no database, no trade path. Analytics host (the Mini PC). Seam 2 (#153) adds the re-priced zero-gamma level
(naive sign) and the participant-OI-calibrated profiles per dealer definition.

  python3 scripts/backfill_gex_profiles.py                    # every slug, every archived day
  python3 scripts/backfill_gex_profiles.py --slug nifty --since 2026-10-01 --dry-run
  python3 scripts/backfill_gex_profiles.py --root data/vm_lake_copy --participant-root data/lake
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import lake                                   # noqa: E402
from src.analysis import gamma_profile as gp          # noqa: E402
from src.ingestion import participant_oi as poi       # noqa: E402

COMBINED_MAX_DTE = 35                                  # D3: expiries summed into the day's combined profile


def _lot_size(underlying):
    try:
        from src.options_proposer import EQUITY_OPTION_UNDERLYINGS, LOT_SIZES
        return LOT_SIZES.get(underlying) or EQUITY_OPTION_UNDERLYINGS.get(str(underlying).upper())
    except Exception:
        return None


def _dte(expiry, day):
    try:
        return (date.fromisoformat(str(expiry)[:10]) - date.fromisoformat(day)).days
    except ValueError:
        return None


def _class(underlying) -> str:
    return "stock" if str(underlying).upper().endswith(".NS") else "index"


def day_row(slug: str, day: str, chain_rows: list, participant: dict = None) -> dict | None:
    """The day's GEX record for one underlying: per expiry, Dhan-gamma profile + the re-priced profile
    (naive sign) + the calibrated one (DEFAULT_DEALER); combined over expiries <= 35 DTE, the same plus
    every dealer definition. `participant` = that day's participant_oi row (None = no calibration)."""
    chain_rows = [r for r in chain_rows if isinstance(r, dict) and isinstance(r.get("oc"), dict)]
    if not chain_rows:
        return None
    underlying = chain_rows[0].get("underlying")
    cls = _class(underlying)
    band, grid = gp.DEFAULT_BAND_PCT[cls], gp.GRID_PCT[cls]
    lot = _lot_size(underlying)
    weights = {d: (w or {}).get(cls) for d, w in ((participant or {}).get("weights") or {}).items()}
    cal = weights.get(poi.DEFAULT_DEALER)
    spot = chain_rows[0].get("spot")
    as_of = chain_rows[0].get("captured_at") or f"{day}T15:40:00+05:30"
    expiries, near, near_chains = [], [], []
    for r in sorted(chain_rows, key=lambda x: str(x.get("expiry"))):
        rows = gp.strike_gex(r["oc"], r.get("spot"))
        dte = _dte(r.get("expiry"), day)
        one = [(r["oc"], r.get("expiry"))]
        expiries.append({"expiry": r.get("expiry"), "dte": dte,
                         "oi_lot_multiple": gp.oi_lot_multiple(rows, lot),
                         "profile": gp.profile(rows, r.get("spot"), band),
                         "repriced": gp.repriced_profile(one, r.get("spot"), as_of, band, None, grid),
                         "calibrated": (gp.repriced_profile(one, r.get("spot"), as_of, band, cal, grid)
                                        if cal else None)})
        if dte is not None and 0 <= dte <= COMBINED_MAX_DTE:
            near.append(rows)
            near_chains.append(one[0])
    if near:
        combined = {"max_dte": COMBINED_MAX_DTE, "expiries": len(near),
                    "profile": gp.profile(gp.merge_rows(*near), spot, band),
                    "repriced": gp.repriced_profile(near_chains, spot, as_of, band, None, grid),
                    "calibrated": {d: gp.repriced_profile(near_chains, spot, as_of, band, w, grid)
                                   for d, w in weights.items() if w}}
    else:
        combined = {"max_dte": COMBINED_MAX_DTE, "expiries": 0, "repriced": None, "calibrated": {},
                    "profile": dict(gp.profile([], spot, band),
                                    reason=f"no expiry within {COMBINED_MAX_DTE} days")}
    return {"date": day, "slug": slug, "underlying": underlying, "spot": spot,
            "vix": chain_rows[0].get("vix"), "captured_at": chain_rows[0].get("captured_at"),
            "lot_size": lot, "option_class": cls,
            "sign_convention": "profile/repriced: naive calls +, puts -; calibrated: participant OI (D2)",
            "participant_weights": weights or None,
            "combined": combined, "expiries": expiries}


def slugs(root=None) -> list:
    base = Path(root) if root is not None else lake.LAKE_ROOT
    chains = base / "chains"
    return sorted(p.name for p in chains.iterdir() if p.is_dir()) if chains.is_dir() else []


def run(root=None, only=None, since=None, dry_run=False, participant_root=None) -> dict:
    """Backfill every (slug, day). Returns {slug: [day rows]}; writes nothing on dry_run."""
    out, cache = {}, {}
    for slug in slugs(root):
        if only and slug != only:
            continue
        for day in lake.list_days(f"chains/{slug}", root):
            if since and day < since:
                continue
            if day not in cache:
                cache[day] = poi.load(day, participant_root)
            row = day_row(slug, day, lake.read_day(f"chains/{slug}", day, root=root), cache[day])
            if row is None:
                continue
            out.setdefault(slug, []).append(row)
            if not dry_run:
                lake.write_partition(f"gex/{slug}", day, [row], root=root)
    return out


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


def summary_lines(result: dict) -> list:
    """Per underlying: Dhan-gamma regime, re-priced (naive) regime at spot, calibrated regime at spot,
    abstentions, median model/Dhan gamma ratio and the OI-unit check."""
    lines = [f"{'slug':12} {'days':>4} {'dhan +/-':>9} {'model +/-':>9} {'calib +/-':>9} {'abst':>4} "
             f"{'g ratio':>7} {'oi=qty':>6}"]
    for slug, rows in sorted(result.items()):
        comb = [r["combined"] for r in rows]
        dh = [c["profile"].get("regime") for c in comb]
        mo = [(c.get("repriced") or {}).get("regime_at_spot") for c in comb]
        ca = [((c.get("calibrated") or {}).get(poi.DEFAULT_DEALER) or {}).get("regime_at_spot") for c in comb]
        ab = sum(1 for c in comb if not (c.get("repriced") or {}).get("regime"))
        ratio = _med((c.get("repriced") or {}).get("model_gamma_ratio") for c in comb)
        mult = _med(e["oi_lot_multiple"] for r in rows for e in r["expiries"])

        def pn(xs):
            return f"{xs.count('positive')}/{xs.count('negative')}"
        lines.append(f"{slug:12} {len(rows):>4} {pn(dh):>9} {pn(mo):>9} {pn(ca):>9} {ab:>4} "
                     f"{ratio:>7.2f} {mult:>6.2f}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="lake root (default data/lake)")
    ap.add_argument("--slug", help="one chains/<slug> only")
    ap.add_argument("--since", help="first day, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--participant-root", help="lake root holding participant_oi/ (default data/lake)")
    args = ap.parse_args(argv)
    result = run(args.root, args.slug, args.since, args.dry_run, args.participant_root)
    print("\n".join(summary_lines(result)))
    print(f"[gex backfill] {sum(len(v) for v in result.values())} day rows over {len(result)} underlyings"
          + (" (dry run, nothing written)" if args.dry_run
             else f" -> {Path(args.root) if args.root else lake.LAKE_ROOT}/gex/<slug>/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
