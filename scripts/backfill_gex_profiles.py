"""
scripts/backfill_gex_profiles.py — GEX profiles from the archived EOD chains (decision #152).

READ-ONLY over data/lake/chains/<slug>/; writes only data/lake/gex/<slug>/date=D/part.jsonl.gz (one row per
day, replaced on re-run). No Dhan call, no journal, no database, no trade path. Analytics host (the Mini PC).

  python3 scripts/backfill_gex_profiles.py                    # every slug, every archived day
  python3 scripts/backfill_gex_profiles.py --slug nifty --since 2026-10-01 --dry-run
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


def day_row(slug: str, day: str, chain_rows: list) -> dict | None:
    """The day's GEX record for one underlying: a profile per expiry plus the combined <= 35 DTE profile."""
    chain_rows = [r for r in chain_rows if isinstance(r, dict) and isinstance(r.get("oc"), dict)]
    if not chain_rows:
        return None
    underlying = chain_rows[0].get("underlying")
    band = gp.DEFAULT_BAND_PCT["stock" if str(underlying).upper().endswith(".NS") else "index"]
    lot = _lot_size(underlying)
    expiries, near = [], []
    for r in sorted(chain_rows, key=lambda x: str(x.get("expiry"))):
        rows = gp.strike_gex(r["oc"], r.get("spot"))
        dte = _dte(r.get("expiry"), day)
        expiries.append({"expiry": r.get("expiry"), "dte": dte,
                         "oi_lot_multiple": gp.oi_lot_multiple(rows, lot),
                         "profile": gp.profile(rows, r.get("spot"), band)})
        if dte is not None and 0 <= dte <= COMBINED_MAX_DTE:
            near.append(rows)
    spot = chain_rows[0].get("spot")
    combined = gp.profile(gp.merge_rows(*near), spot, band) if near else dict(
        gp.profile([], spot, band), reason=f"no expiry within {COMBINED_MAX_DTE} days")
    return {"date": day, "slug": slug, "underlying": underlying, "spot": spot,
            "vix": chain_rows[0].get("vix"), "captured_at": chain_rows[0].get("captured_at"),
            "lot_size": lot, "sign_convention": "naive: calls +, puts - (D2, unverified for NSE)",
            "combined": {"max_dte": COMBINED_MAX_DTE, "expiries": len(near), "profile": combined},
            "expiries": expiries}


def slugs(root=None) -> list:
    base = Path(root) if root is not None else lake.LAKE_ROOT
    chains = base / "chains"
    return sorted(p.name for p in chains.iterdir() if p.is_dir()) if chains.is_dir() else []


def run(root=None, only=None, since=None, dry_run=False) -> dict:
    """Backfill every (slug, day). Returns {slug: [day rows]}; writes nothing on dry_run."""
    out = {}
    for slug in slugs(root):
        if only and slug != only:
            continue
        for day in lake.list_days(f"chains/{slug}", root):
            if since and day < since:
                continue
            row = day_row(slug, day, lake.read_day(f"chains/{slug}", day, root=root))
            if row is None:
                continue
            out.setdefault(slug, []).append(row)
            if not dry_run:
                lake.write_partition(f"gex/{slug}", day, [row], root=root)
    return out


def summary_lines(result: dict) -> list:
    lines = [f"{'slug':12} {'days':>4} {'pos':>4} {'neg':>4} {'abst':>4} {'med net_share':>13} "
             f"{'med conc':>8} {'oi=qty':>6}"]
    for slug, rows in sorted(result.items()):
        profs = [r["combined"]["profile"] for r in rows]
        ok = [p for p in profs if p.get("regime")]
        mult = [e["oi_lot_multiple"] for r in rows for e in r["expiries"] if e["oi_lot_multiple"] is not None]
        share = [p["net_share"] for p in ok if p.get("net_share") is not None]
        conc = [p["concentration"] for p in ok if p.get("concentration") is not None]
        lines.append(f"{slug:12} {len(rows):>4} {sum(p['regime'] == 'positive' for p in ok):>4} "
                     f"{sum(p['regime'] == 'negative' for p in ok):>4} {len(profs) - len(ok):>4} "
                     f"{statistics.median(share) if share else float('nan'):>13.3f} "
                     f"{statistics.median(conc) if conc else float('nan'):>8.3f} "
                     f"{statistics.median(mult) if mult else float('nan'):>6.2f}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", help="lake root (default data/lake)")
    ap.add_argument("--slug", help="one chains/<slug> only")
    ap.add_argument("--since", help="first day, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    args = ap.parse_args(argv)
    result = run(args.root, args.slug, args.since, args.dry_run)
    print("\n".join(summary_lines(result)))
    print(f"[gex backfill] {sum(len(v) for v in result.values())} day rows over {len(result)} underlyings"
          + (" (dry run, nothing written)" if args.dry_run
             else f" -> {Path(args.root) if args.root else lake.LAKE_ROOT}/gex/<slug>/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
