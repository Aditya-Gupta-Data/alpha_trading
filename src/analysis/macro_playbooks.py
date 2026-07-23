"""
src/analysis/macro_playbooks.py — M3: what the shapes actually pay
==================================================================

The Macro Regime Engine's tradable memory (spec §2 "Playbook table"):
per (archetype, phase, sector) — what each NIFTY sector index actually
did after episodes of that archetype, with hit-rates and n STATED.

Phases (v1, fixed offset windows — documented simplification; the
spec's changepoint segmentation is a later refinement, not skipped
silently). HORIZON-AWARE (owner directive 2026-07-23): a sudden shock
and a slow-burn cycle read on different clocks —
    shock:      P1 shock T+0..10 · P2 basing T+11..45 · P3 resolution T+46..120
    slow_burn:  S1 buildup T+0..120 · S2 peak effect T+121..300 · S3 unwind T+301..500
An El Niño playbook measured on a 30-day clock would be noise; a panic
measured on a 2-year clock would be mush. Each archetype's table uses
its own horizon's clock, and the two never mix.

Sector returns are measured absolute AND excess-vs-NIFTY (rotation is
an excess-return game); the aggregate rows carry n / median / min /
max / hit-rate of the EXCESS leg. Honesty rules:
  * Sector index history starts 2019-10 — episodes before that simply
    have no sector legs; a cell with n=0 does not exist in the table
    (never a fabricated aggregate), and every aggregate names its n.
  * A sector missing a usable close at a window edge contributes
    nothing to that cell (window edges snap to the nearest session
    INSIDE the window; an empty window is a named per-episode miss).
  * Everything is derived from data/macro_templates.json's STABLE
    archetype IDs (M2.1 core-channel layer) — playbooks never invent
    their own taxonomy.

Advisory value only after Dept-5 scoring (spec law 4); this module
writes an artifact, it advises nothing by itself.

CLI: python3 -m src.analysis.macro_playbooks [--dry-run]
"""
import json
from bisect import bisect_left, bisect_right
from datetime import datetime
from pathlib import Path

from src.analysis import macro_features as MF
from src.ingestion.indices_lake import INDEX_MAP

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_PATH = ROOT / "data" / "macro_templates.json"
PLAYBOOKS_PATH = ROOT / "data" / "macro_playbooks.json"

PHASES_BY_HORIZON = {
    "shock": (("P1_shock", 0, 10),
              ("P2_basing", 11, 45),
              ("P3_resolution", 46, 120)),
    "slow_burn": (("S1_buildup", 0, 120),
                  ("S2_peak_effect", 121, 300),
                  ("S3_unwind", 301, 500)),
}
PHASES = PHASES_BY_HORIZON["shock"]      # the default clock

SECTORS = tuple(k for k in INDEX_MAP
                if k not in ("NIFTY", "INDIAVIX"))
BENCHMARK = "NIFTY"


def _closes(key, lake_dir=None):
    """[(iso_date, close)] with holes dropped — return math needs real
    prices; a hole simply doesn't anchor a window edge."""
    return [(d, v) for d, v in MF.read_series(key, lake_dir)
            if v is not None]


def _range_return(rows, dates, lo_date, hi_date):
    """% return between the first usable close dated >= lo_date and the
    last usable close dated <= hi_date. None when fewer than two usable
    sessions fall inside [lo_date, hi_date] — the edges SNAP INWARD, a
    hole at a window edge never voids the whole window."""
    i = bisect_left(dates, lo_date)
    j = bisect_right(dates, hi_date) - 1
    if j <= i or i >= len(dates):
        return None
    a, b = rows[i][1], rows[j][1]
    if not a:
        return None
    return (b - a) / a


def episode_phase_returns(anchor, lake_dir=None, phases=PHASES):
    """One episode -> {phase: {sector: {"abs": r, "excess": r-nifty}}},
    plus the benchmark's own phase returns. Phase windows are defined on
    the BENCHMARK's session calendar (T+offsets), then every series —
    benchmark and sectors alike — measures over that DATE range with
    inward-snapping edges. An episode before index history returns
    ({}, {}) honestly."""
    bench = _closes(BENCHMARK, lake_dir)
    if not bench:
        return {}, {}
    bdates = [d for d, _ in bench]
    anchor_idx = bisect_right(bdates, anchor) - 1
    if anchor_idx < 0:
        return {}, {}
    sector_rows = {s: _closes(s, lake_dir) for s in SECTORS}
    out, bench_out = {}, {}
    for phase, lo, hi in phases:
        lo_i = anchor_idx + lo
        hi_i = min(anchor_idx + hi, len(bdates) - 1)
        if lo_i >= len(bdates) or hi_i <= lo_i:
            continue                     # no benchmark window, no phase
        lo_date, hi_date = bdates[lo_i], bdates[hi_i]
        b_ret = _range_return(bench, bdates, lo_date, hi_date)
        if b_ret is None:
            continue
        bench_out[phase] = round(b_ret, 6)
        cells = {}
        for s, rows in sector_rows.items():
            if not rows:
                continue
            r = _range_return(rows, [d for d, _ in rows],
                              lo_date, hi_date)
            if r is None:
                continue
            cells[s] = {"abs": round(r, 6),
                        "excess": round(r - b_ret, 6)}
        if cells:
            out[phase] = cells
    return out, bench_out


def _aggregate(cells):
    """[(episode, excess)] -> the honest aggregate row."""
    vals = sorted(x for _, x in cells)
    n = len(vals)
    mid = n // 2
    median = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {"n": n,
            "median_excess": round(median, 6),
            "min_excess": vals[0], "max_excess": vals[-1],
            "hit_rate": round(sum(1 for v in vals if v > 0) / n, 3),
            "episodes": {ep: x for ep, x in sorted(cells)}}


def build_playbooks(templates_path=None, lake_dir=None, out_path=None,
                    dry_run=False):
    """data/macro_templates.json + the macro lake -> the playbook table.
    Cells exist only where n >= 1; every cell names its episodes."""
    templates = json.loads(
        Path(templates_path or TEMPLATES_PATH).read_text())
    per_episode, no_sector_legs, table, members_out = {}, [], {}, {}
    for hz, block in templates["horizons"].items():
        phases = PHASES_BY_HORIZON[hz]
        anchors = {e["name"]: e["anchor"] for e in block["episodes"]}
        for name, anchor in anchors.items():
            ph, bench = episode_phase_returns(anchor, lake_dir,
                                              phases=phases)
            if not ph:
                no_sector_legs.append(name)
                continue
            per_episode[name] = {"anchor": anchor, "horizon": hz,
                                 "phases": ph, "benchmark": bench}
        for arch in block["archetypes"]:
            aid = arch["id"]
            members_out[aid] = arch["members"]
            for phase, _, _ in phases:
                for sector in SECTORS:
                    cells = []
                    for m in arch["members"]:
                        eph = (per_episode.get(m) or {}).get("phases", {})
                        cell = (eph.get(phase) or {}).get(sector)
                        if cell is not None:
                            cells.append((m, cell["excess"]))
                    if cells:
                        table.setdefault(aid, {}).setdefault(
                            phase, {})[sector] = _aggregate(cells)

    doc = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"phases_by_horizon":
                       {h: [list(p) for p in ps]
                        for h, ps in PHASES_BY_HORIZON.items()},
                   "benchmark": BENCHMARK,
                   "sectors": list(SECTORS),
                   "source_templates_built_at": templates["built_at"]},
        "archetype_members": members_out,
        "episodes_without_sector_legs": sorted(no_sector_legs),
        "per_episode": per_episode,
        "table": table,
        "advisory_note": ("descriptive history with n stated per cell; "
                          "advises nothing until Dept-5 scoring passes"),
    }
    if not dry_run:
        path = Path(out_path or PLAYBOOKS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(path)
    return doc


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    doc = build_playbooks(dry_run=args.dry_run)
    summary = {aid: {ph: len(sec) for ph, sec in phases.items()}
               for aid, phases in doc["table"].items()}
    print(json.dumps({
        "built_at": doc["built_at"],
        "episodes_with_sector_legs": sorted(doc["per_episode"]),
        "episodes_without": doc["episodes_without_sector_legs"],
        "cells_per_archetype_phase": summary,
    }, indent=2))
