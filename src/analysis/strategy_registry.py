"""
src/analysis/strategy_registry.py — the Strategy Registry (regime-conditioned playbooks)
========================================================================================

Spec: docs/strategy_registry_spec.md.

Generalizes macro_playbooks.py from ONE hardcoded strategy ("hold a sector
through the phase") to a REGISTRY of declared, index-level trade recipes —
scored across the same historical episodes with the same NULL-honest return
math (macro_playbooks.episode_phase_returns), ranked on the Wilson lower bound,
and gated for significance by a SOURCE-SPLIT rule (owner ruling 2026-07-23):
a-priori theses use a STANDARD INDEPENDENT threshold (p<=0.05, directional) —
they are pre-registered, not mined, so the harsh Benjamini-Hochberg multiple-
testing penalty would punish a data-snooping crime they never committed; BH
stays PARKED for Stage-C auto-discovery, where snooping is the real risk. A
placebo control set runs under the SAME gate as the false-discovery meter.

A RegimeStrategy is DECLARATIVE data + a pure evaluator — NEVER arbitrary
code in a cron (safety + look-ahead safety). Four index-level `kind`s in v1
(options deferred — spec §11 Stage D — so the inflated options simulator is
NOWHERE in this path, spec §6.1):

    long_sector      {sector}             -> excess vs NIFTY
    long_short_pair  {long, short}        -> r(long) - r(short)          (neutral spread)
    basket_rotation  {longs[], shorts[]}  -> mean(longs) - mean(shorts); long-only = mean(excess)
    benchmark_tilt   {sector, weight}     -> weight * excess(sector)

Every kind is measured DRIFT-REMOVED (excess vs NIFTY, or a market-neutral
spread), so the 50% coin is the honest structural null — the market's own
upward drift is never credited as strategy edge (the circularity fix).

Identity: strategy_id = sha256 of the canonical spec (frozen; re-registration
is a no-op — the pattern-registry discipline, #49). name/thesis/source are
metadata and do not change identity.

Authority: NONE. This module writes data/macro_strategies.json and advises
nothing until Dept-5 scoring passes its ledger (spec Law 4). declare() reads
the artifact via top_strategies() (spec §7); a missing/unreadable artifact is
an honest 'unavailable', never a recompute, never a crash.

CLI: python3 -m src.analysis.strategy_registry [--dry-run]
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from src.analysis import macro_playbooks as PB
from src.validation import stat_gates as sg

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_PATH = PB.TEMPLATES_PATH
STRATEGIES_PATH = ROOT / "data" / "macro_strategies.json"

SECTORS = set(PB.SECTORS)
VALID_KINDS = ("long_sector", "long_short_pair", "basket_rotation",
               "benchmark_tilt")

# Owner ruling 2026-07-23: a cell renders a strategy only with >= this many
# REAL analog legs (matches strategy_evidence's MIN_VIEW_RESOLUTIONS). A
# 4-analog "win-rate" is noise wearing a percentage. Consequence (spec §6.2):
# with sector history starting only 2019-10, most cells' sector legs fall
# short until the pre-2019 sector CSVs land — the sector rotations ABSTAIN,
# the NIFTY-level strategies still render.
MIN_EPISODE_LEGS = 5
STRUCTURAL_NULL = 0.5      # drift-removed: does the recipe beat a coin?
INDEPENDENT_ALPHA = 0.05  # a-priori theses: standard threshold, NO BH penalty
FDR_Q = sg.FDR_Q          # Benjamini-Hochberg ceiling — PARKED for Stage-C only
TOP_K = 5                 # ranked strategies surfaced per cell in the artifact


# --------------------------------------------------------------- identity

def _norm_params(kind, params):
    """Canonical param form so equivalent specs hash identically: basket
    long/short membership is a SET (order-blind); pair roles are POSITIONAL
    (long A / short B is a different bet from long B / short A)."""
    p = dict(params)
    if kind == "basket_rotation":
        p["longs"] = sorted(p.get("longs") or [])
        p["shorts"] = sorted(p.get("shorts") or [])
    return p


def validate_spec(spec):
    """Raise ValueError on a malformed strategy; return it untouched. Every
    sector name is checked against the real INDEX_MAP universe so a typo can
    never silently price nothing forever."""
    kind = spec.get("kind")
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown kind {kind!r}; valid: {VALID_KINDS}")
    if spec.get("horizon") not in PB.PHASES_BY_HORIZON:
        raise ValueError(f"unknown horizon {spec.get('horizon')!r}")
    p = spec.get("params") or {}

    def _sector(s):
        if s not in SECTORS:
            raise ValueError(f"{s!r} is not a known sector index")

    if kind in ("long_sector", "benchmark_tilt"):
        _sector(p["sector"])
        if kind == "benchmark_tilt" and not 0 < float(p.get("weight", 1.0)) <= 1.0:
            raise ValueError("benchmark_tilt weight must be in (0, 1]")
    elif kind == "long_short_pair":
        _sector(p["long"]); _sector(p["short"])
        if p["long"] == p["short"]:
            raise ValueError("long_short_pair legs must differ")
    elif kind == "basket_rotation":
        longs, shorts = p.get("longs") or [], p.get("shorts") or []
        if not longs:
            raise ValueError("basket_rotation needs >= 1 long")
        for s in (*longs, *shorts):
            _sector(s)
        if set(longs) & set(shorts):
            raise ValueError("a sector cannot be both long and short")
    return spec


def canonical(spec):
    """The frozen semantic JSON (kind + normalized params + horizon)."""
    body = {"kind": spec["kind"], "horizon": spec["horizon"],
            "params": _norm_params(spec["kind"], spec.get("params") or {})}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def strategy_id(spec):
    return hashlib.sha256(canonical(spec).encode()).hexdigest()[:16]


# --------------------------------------------------------------- evaluator

def evaluate_leg(spec, phases_for_episode, phase):
    """One (strategy, episode, phase) -> signed drift-removed return | None.
    A pure combinator over macro_playbooks.episode_phase_returns output.
    NULL-honest: if ANY required sector leg is unpriceable in this phase
    window, the whole strategy leg is None and contributes nothing (a partial
    basket is a DIFFERENT strategy, never fabricated). Reads only the phase
    cell it is given -> future-blind by construction (spec §5.1)."""
    cell = (phases_for_episode or {}).get(phase)
    if not cell:
        return None
    kind, p = spec["kind"], spec["params"]

    def absr(s):
        c = cell.get(s)
        return c["abs"] if c else None

    def exc(s):
        c = cell.get(s)
        return c["excess"] if c else None

    if kind == "long_sector":
        return exc(p["sector"])
    if kind == "benchmark_tilt":
        e = exc(p["sector"])
        return None if e is None else float(p.get("weight", 1.0)) * e
    if kind == "long_short_pair":
        a, b = absr(p["long"]), absr(p["short"])
        return None if a is None or b is None else a - b
    if kind == "basket_rotation":
        longs, shorts = p["longs"], p.get("shorts") or []
        la = [absr(s) for s in longs]
        if any(x is None for x in la):
            return None
        if shorts:
            sa = [absr(s) for s in shorts]
            if any(x is None for x in sa):
                return None
            return sum(la) / len(la) - sum(sa) / len(sa)
        le = [exc(s) for s in longs]          # long-only basket vs benchmark
        if any(x is None for x in le):
            return None
        return sum(le) / len(le)
    return None


# --------------------------------------------------------------- catalog

# A-PRIORI seed strategies (owner ruling 2026-07-23: no hand-picking winners —
# these are economic theses frozen BEFORE the backtest reveals a score). The
# crisis theses follow the War Playbook doctrine (macro_shocks.py: favour
# Energy/Pharma/FMCG/Metal, avoid rate-sensitives/high-beta in shocks).
SEED_STRATEGIES = [
    # single-sector baselines (defensive / beneficiary / cyclical tells)
    {"name": "long_fmcg", "kind": "long_sector", "horizon": "shock",
     "params": {"sector": "NIFTY_FMCG"}, "thesis": "classic defensive"},
    {"name": "long_pharma", "kind": "long_sector", "horizon": "shock",
     "params": {"sector": "NIFTY_PHARMA"}, "thesis": "defensive haven"},
    {"name": "long_it_inr_haven", "kind": "long_sector", "horizon": "shock",
     "params": {"sector": "NIFTY_IT"}, "thesis": "INR-weakness exporter haven"},
    {"name": "long_energy_oil", "kind": "long_sector", "horizon": "shock",
     "params": {"sector": "NIFTY_ENERGY"}, "thesis": "oil/energy crisis beneficiary"},
    {"name": "long_metal_reflation", "kind": "long_sector", "horizon": "shock",
     "params": {"sector": "NIFTY_METAL"}, "thesis": "commodity/reflation leg"},
    # War-Playbook rotations (defensives/beneficiaries vs vulnerables)
    {"name": "crisis_favoured_vs_vulnerable", "kind": "basket_rotation",
     "horizon": "shock",
     "params": {"longs": ["NIFTY_ENERGY", "NIFTY_PHARMA", "NIFTY_FMCG",
                          "NIFTY_METAL"],
                "shorts": ["NIFTY_REALTY", "NIFTY_PSU_BANK"]},
     "thesis": "War Playbook: favour Energy/Pharma/FMCG/Metal, avoid rate-sensitives"},
    {"name": "defensives_vs_high_beta", "kind": "basket_rotation",
     "horizon": "shock",
     "params": {"longs": ["NIFTY_FMCG", "NIFTY_PHARMA"],
                "shorts": ["NIFTY_REALTY", "NIFTY_MEDIA"]},
     "thesis": "defensives outperform high-beta in risk-off"},
    {"name": "defensive_haven_basket", "kind": "basket_rotation",
     "horizon": "shock",
     "params": {"longs": ["NIFTY_FMCG", "NIFTY_PHARMA", "NIFTY_IT"]},
     "thesis": "long-only defensive haven vs NIFTY"},
    # long/short pairs (market-neutral crisis spreads)
    {"name": "flight_to_quality_fmcg_over_psu", "kind": "long_short_pair",
     "horizon": "shock",
     "params": {"long": "NIFTY_FMCG", "short": "NIFTY_PSU_BANK"},
     "thesis": "flight to quality: defensives over fragile PSU banks"},
    {"name": "pharma_over_realty", "kind": "long_short_pair", "horizon": "shock",
     "params": {"long": "NIFTY_PHARMA", "short": "NIFTY_REALTY"},
     "thesis": "defensive over rate-sensitive"},
    {"name": "it_over_banks_inr", "kind": "long_short_pair", "horizon": "shock",
     "params": {"long": "NIFTY_IT", "short": "NIFTY_BANK"},
     "thesis": "INR weakness: exporters over domestic financials"},
    {"name": "energy_over_auto_oilshock", "kind": "long_short_pair",
     "horizon": "shock",
     "params": {"long": "NIFTY_ENERGY", "short": "NIFTY_AUTO"},
     "thesis": "oil shock helps energy, hurts autos (input cost)"},
    {"name": "half_tilt_pharma", "kind": "benchmark_tilt", "horizon": "shock",
     "params": {"sector": "NIFTY_PHARMA", "weight": 0.5},
     "thesis": "gentle defensive overlay on the index"},
    # slow-burn seeds (El Niño / rate cycles — mostly abstain until pre-2019
    # sector CSVs land, registered so they light up when data deepens)
    {"name": "el_nino_it_over_fmcg", "kind": "long_short_pair",
     "horizon": "slow_burn",
     "params": {"long": "NIFTY_IT", "short": "NIFTY_FMCG"},
     "thesis": "poor monsoon: rural-demand FMCG lags monsoon-agnostic IT"},
    {"name": "rate_cycle_bank_over_realty", "kind": "long_short_pair",
     "horizon": "slow_burn",
     "params": {"long": "NIFTY_BANK", "short": "NIFTY_REALTY"},
     "thesis": "hiking cycle: bank NIMs over rate-sensitive realty"},
]

# PLACEBO strategies (spec §4 step 5): information-free recipes with no
# a-priori thesis — arbitrary same-character pairings (cyclical-vs-cyclical,
# commodity-vs-commodity) where we hold NO directional prior. They MUST use
# deep-history sectors (BANK/IT/AUTO/PHARMA/ENERGY/METAL) so they clear the
# floor and actually render — a placebo that never renders measures nothing.
# They flow through the IDENTICAL pipeline; their realized survival rate is
# the false-discovery thermometer. If placebos rank as high as the seeds, the
# Registry is measuring noise and the kill criterion fires.
PLACEBO_STRATEGIES = [
    {"name": "placebo_auto_over_bank", "kind": "long_short_pair",
     "horizon": "shock",
     "params": {"long": "NIFTY_AUTO", "short": "NIFTY_BANK"},
     "thesis": "information-free control (cyclical vs cyclical)",
     "source": "placebo"},
    {"name": "placebo_it_over_pharma", "kind": "long_short_pair",
     "horizon": "shock",
     "params": {"long": "NIFTY_IT", "short": "NIFTY_PHARMA"},
     "thesis": "information-free control (defensive vs defensive)",
     "source": "placebo"},
    {"name": "placebo_metal_over_energy", "kind": "long_short_pair",
     "horizon": "shock",
     "params": {"long": "NIFTY_METAL", "short": "NIFTY_ENERGY"},
     "thesis": "information-free control (commodity vs commodity)",
     "source": "placebo"},
    {"name": "placebo_arbitrary_basket", "kind": "basket_rotation",
     "horizon": "shock",
     "params": {"longs": ["NIFTY_AUTO", "NIFTY_METAL"],
                "shorts": ["NIFTY_BANK", "NIFTY_ENERGY"]},
     "thesis": "information-free control (arbitrary same-character basket)",
     "source": "placebo"},
    {"name": "placebo_bank_over_it_slow", "kind": "long_short_pair",
     "horizon": "slow_burn",
     "params": {"long": "NIFTY_BANK", "short": "NIFTY_IT"},
     "thesis": "information-free control", "source": "placebo"},
]


# --------------------------------------------------------------- aggregation

def _aggregate_returns(legs):
    """[(episode, return)] -> the honest aggregate row. EV = mean return;
    win-rate carries its one-sided Wilson LOWER bound (the honest number);
    the two-sided binomial p (vs the drift-removed 50% null) rides along for
    the batch Benjamini-Hochberg."""
    rets = [r for _, r in legs]
    n = len(rets)
    wins = sum(1 for r in rets if r > 0)
    vals = sorted(rets)
    mid = n // 2
    median = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {
        "n": n, "wins": wins,
        "ev": round(sum(rets) / n, 6),                 # mean return — the EV
        "median": round(median, 6),
        "min": round(vals[0], 6), "max": round(vals[-1], 6),
        "hit_rate": round(wins / n, 3),
        "wilson_lb": round(sg.wilson_lower_bound(wins, n), 3),
        "p_two_sided": sg.binomial_p_two_sided(wins, n, STRUCTURAL_NULL),
        "episodes": {ep: round(r, 6) for ep, r in sorted(legs)},
    }


_PUBLIC_KEYS = ("strategy_id", "name", "kind", "params", "source", "thesis",
                "n", "ev", "median", "min", "max", "hit_rate", "wilson_lb",
                "significant", "episodes")


def _public_row(row):
    return {k: row[k] for k in _PUBLIC_KEYS if k in row}


def _is_significant_independent(row):
    """A-priori significance: a directional INDEPENDENT threshold, NO multiple-
    testing penalty (owner ruling 2026-07-23 — pre-registered theses are not
    mined). A WINNER only: significantly better than the drift-removed coin,
    never a significant LOSER dressed up as an edge."""
    return (row["p_two_sided"] <= INDEPENDENT_ALPHA
            and row["hit_rate"] > STRUCTURAL_NULL)


def _verdict(real_rows):
    """PREFER / SHOW / ABSTAIN on a cell's REAL rows. A regime PREFERs when it
    holds AT LEAST ONE significant winning recipe — the owner's vision that a
    pattern can carry MULTIPLE viable strategies at once (each row's
    `significant` flag names which). SHOW = recipes render but none clear the
    gate. ABSTAIN = nothing above the support floor."""
    if not real_rows:
        return "ABSTAIN"
    return "PREFER" if any(r.get("significant") for r in real_rows) else "SHOW"


# --------------------------------------------------------------- builder

def build_strategies(templates_path=None, lake_dir=None, out_path=None,
                     strategies=None, dry_run=False):
    """data/macro_templates.json + the macro lake -> data/macro_strategies.json.
    Generalizes macro_playbooks.build_playbooks: per (archetype, phase), score
    EVERY registered strategy over the archetype's member episodes, apply the
    support floor, correct for multiple testing across the WHOLE build with
    Benjamini-Hochberg, then rank each cell on the Wilson lower bound. Placebo
    strategies ride through identically and their survival is reported."""
    templates = json.loads(
        Path(templates_path or TEMPLATES_PATH).read_text())
    catalog = (strategies if strategies is not None
               else SEED_STRATEGIES + PLACEBO_STRATEGIES)
    specs = []
    for s in catalog:
        validate_spec(s)
        specs.append({**s, "strategy_id": strategy_id(s)})

    # per-episode phase returns, computed ONCE per episode and reused across
    # every strategy (the expensive lake read happens |episodes| times, not
    # |episodes|x|strategies| times)
    per_episode_phases, episode_horizon = {}, {}
    for hz, block in templates["horizons"].items():
        phases = PB.PHASES_BY_HORIZON[hz]
        for e in block["episodes"]:
            if not e.get("included", True):
                continue
            ph, _bench = PB.episode_phase_returns(
                e["anchor"], lake_dir, phases=phases)
            per_episode_phases[e["name"]] = ph
            episode_horizon[e["name"]] = hz

    members_by_arch = {}     # (horizon, archetype_id) -> [episode names]
    for hz, block in templates["horizons"].items():
        for arch in block["archetypes"]:
            members_by_arch[(hz, arch["id"])] = arch["members"]

    # score every (strategy, cell) above the floor, then apply the
    # SOURCE-SPLIT significance gate (owner ruling 2026-07-23):
    #   * a-priori theses (seed) + their placebo controls -> a STANDARD
    #     INDEPENDENT threshold (directional p <= INDEPENDENT_ALPHA). These are
    #     PRE-REGISTERED hypotheses, not mined, so the harsh multiple-testing
    #     penalty would punish a data-snooping crime they never committed.
    #   * discovered theses (Stage-C auto-discovery) -> Benjamini-Hochberg
    #     stays PARKED here, where snooping across machine-generated
    #     candidates is the real risk.
    raw_cells = {}
    discovered = {"p": [], "idx": []}
    for spec in specs:
        hz = spec["horizon"]
        phases = PB.PHASES_BY_HORIZON[hz]
        for (mhz, aid), members in members_by_arch.items():
            if mhz != hz:
                continue
            for phase, _lo, _hi in phases:
                legs = [(m, r) for m in members
                        if (r := evaluate_leg(
                            spec, per_episode_phases.get(m), phase)) is not None]
                if len(legs) < MIN_EPISODE_LEGS:
                    continue
                row = {"strategy_id": spec["strategy_id"], "name": spec["name"],
                       "kind": spec["kind"], "params": spec["params"],
                       "source": spec.get("source", "seed"),
                       "thesis": spec.get("thesis"), **_aggregate_returns(legs)}
                cell = raw_cells.setdefault((aid, phase), [])
                cell.append(row)
                if row["source"] == "discovered":
                    row["significant"] = False           # decided by the BH pass
                    discovered["p"].append(row["p_two_sided"])
                    discovered["idx"].append((aid, phase, len(cell) - 1))
                else:
                    row["significant"] = _is_significant_independent(row)

    # BH stays PARKED for Stage-C 'discovered' theses only (data-snooping risk)
    for (aid, phase, i), ok in zip(
            discovered["idx"], sg.benjamini_hochberg(discovered["p"], q=FDR_Q)):
        r = raw_cells[(aid, phase)][i]
        r["significant"] = bool(ok) and r["hit_rate"] > STRUCTURAL_NULL

    # rank each cell on the honest lower bound; split real vs placebo. The
    # placebo controls face the SAME a-priori gate, so their survival rate is
    # the false-discovery thermometer FOR THAT GATE.
    table, placebo_total, placebo_survivors = {}, 0, 0
    for (aid, phase), rows in raw_cells.items():
        rows.sort(key=lambda r: (-r["wilson_lb"], -r["ev"]))
        real = [r for r in rows if r["source"] != "placebo"]
        placebo = [r for r in rows if r["source"] == "placebo"]
        placebo_total += len(placebo)
        placebo_survivors += sum(1 for r in placebo if r["significant"])
        table.setdefault(aid, {})[phase] = {
            "verdict": _verdict(real),
            "strategies": [_public_row(r) for r in real[:TOP_K]],
            "placebo_here": [_public_row(r) for r in placebo],
        }

    doc = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"min_episode_legs": MIN_EPISODE_LEGS,
                   "structural_null": STRUCTURAL_NULL,
                   "significance": {
                       "a_priori": f"independent two-sided p<={INDEPENDENT_ALPHA}"
                                   " (directional); NO multiple-testing penalty",
                       "discovered": f"benjamini_hochberg q={FDR_Q}"
                                     " (parked for Stage-C auto-discovery)"},
                   "top_k": TOP_K,
                   "source_templates_built_at": templates["built_at"]},
        "strategies": [{"strategy_id": s["strategy_id"], "name": s["name"],
                        "kind": s["kind"], "horizon": s["horizon"],
                        "params": s["params"], "source": s.get("source", "seed"),
                        "thesis": s.get("thesis")} for s in specs],
        "table": table,
        "placebo_report": {
            "total": placebo_total, "survivors": placebo_survivors,
            "note": (f"information-free controls under the SAME a-priori gate "
                     f"(p<={INDEPENDENT_ALPHA}); ~{int(INDEPENDENT_ALPHA * 100)}%"
                     " may pass by chance — a survivor RATE far above that means"
                     " the gate is too loose (the kill criterion).")},
        "advisory_note": ("descriptive history with n stated per cell, ranked "
                          "on the Wilson lower bound; NOT a forecast of return "
                          "and advises nothing until Dept-5 scoring passes."),
    }
    if not dry_run:
        path = Path(out_path or STRATEGIES_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(path)
    return doc


# --------------------------------------------------------------- query seam

def top_strategies(archetype, phase, k=TOP_K, registry_path=None,
                   require_cache=False):
    """THE declare() query seam (spec §7): a cheap dict lookup into the
    pre-built artifact — no featurizer pass, e2-micro-safe. A missing or
    unreadable artifact returns an honest 'unavailable' status (never a
    recompute, never a crash) so the VM executor's silence-ban discipline
    holds. `require_cache` is accepted for signature-parity with the
    fingerprint-cache path; there is no recompute fallback to forbid here."""
    p = Path(registry_path or STRATEGIES_PATH)
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable", "archetype": archetype,
                "phase": phase, "strategies": []}
    cell = ((doc.get("table") or {}).get(archetype) or {}).get(phase)
    if not cell:
        return {"status": "no_cell", "archetype": archetype, "phase": phase,
                "strategies": []}
    return {"status": "ok", "verdict": cell.get("verdict"),
            "archetype": archetype, "phase": phase,
            "strategies": (cell.get("strategies") or [])[:k]}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    doc = build_strategies(dry_run=args.dry_run)
    summary = {aid: {ph: {"verdict": c["verdict"],
                          "n_strategies": len(c["strategies"])}
                     for ph, c in phases.items()}
               for aid, phases in doc["table"].items()}
    print(json.dumps({
        "built_at": doc["built_at"],
        "registered": len(doc["strategies"]),
        "placebo_report": doc["placebo_report"],
        "cells": summary,
    }, indent=2, default=str))
