"""
Alpha Trading — Procedural Evolution: the error-driven rule mutator
====================================================================

The system studies its own LOSS CLUSTERS and proposes rule mutations —
never applying anything itself: every surviving candidate lands in
`candidates/evolution_<ts>.md` for the human gatekeeper (hard rule #3
of the spec, and decision #11's spirit: the human decides).

The pipeline, per cluster (all local, zero API spend):

  1. MINE       loss clusters from resolved simulated trades, grouped by
                (underlying, strategy, VIX band) — provenance kept as the
                exact journal_ref list.
  2. HINDSIGHT  each loss is deterministically re-read for partial
                success (the HER idea): was the TIMING fine but the RISK
                PARAMETERS wrong (rode to max loss), or the reverse
                (pre-expiry exit at a shallow loss = structure ok, clock
                wrong)? No LLM here — pure arithmetic buckets.
  3. COUNTER-   the same setup's WINS are pulled and contrasted (VIX,
     FACTUAL    hold time, capture) so the model must reason about the
                pivot that separated them, not free-associate.
  4. ANALYST    local Ollama proposes ONE mutation from the whitelisted
                EVOLVABLE_PARAMETERS registry — strict JSON, schema-gated,
                bounds-checked. A 3B model never writes code here: it
                picks a parameter and a value and argues why; the code
                diff in the output is generated deterministically by us.
  5. CRITIC     a second, adversarial pass hunts overfitting/loopholes;
                the analyst must answer; an unresolved BLOCK kills the
                candidate (the consensus gate).
  6. BACKTEST   the Phase 7 simulator replays history TWICE on throwaway
                in-memory DBs — baseline vs mutated parameters (patched
                in-process via override_parameters, restored in finally)
                — over the same cached bars. If the mutation repairs the
                target cluster but degrades global Sharpe/drawdown →
                RevertOnRegression: candidate discarded, lineage notes it.
  7. RECORD     survivors get the 4-section markdown (cluster, dialectic
                summary, simulator proof, unified diff) plus a lineage
                entry in data/evolution_lineage.json (v1 -> v2 per
                parameter, with the reasoning that drove each step).

Where it runs (decision #47/#48 realities the spec couldn't know):
  * The LLM work needs Ollama → the MAC. The VM's sleep phase calls Task
    E too, but it degrades to a silent skip there (no Ollama) — same
    pattern as ingestion/consolidation.
  * The Mac holds no live Dhan token (single-token rule, decision #48),
    so backtests run on a CACHED bars file (data/bars_cache.json),
    refreshed from the VM (which owns the token):
        python3 -m src.evolution --refresh-bars-cache
  * Nothing is scheduled anywhere yet — deliberately, until the
    observation week's triage clears it.

Run manually:  python3 -m src.evolution            (mine + propose)
               python3 -m src.evolution --refresh-bars-cache
"""

import argparse
import difflib
import hashlib
import json
import statistics
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_DIR = ROOT / "candidates"
LINEAGE_PATH = ROOT / "data" / "evolution_lineage.json"
BARS_CACHE_PATH = ROOT / "data" / "bars_cache.json"

MIN_CLUSTER_SIZE = 3          # fewer losses than this is noise, not a cluster
MAX_CANDIDATES_PER_RUN = 2    # keep nightly output reviewable by a human
SHARPE_TOLERANCE = 0.98       # mutated Sharpe must be >= 98% of baseline
DRAWDOWN_TOLERANCE = 1.10     # mutated max drawdown must be <= 110% of baseline

# Single source of band truth since Regime-Aware Memory: src/regime.py.
from src.regime import VIX_BANDS, vix_band  # noqa: F401 (re-exported)

# --- the whitelisted mutation surface --------------------------------------
# The ONLY things evolution may touch. Each entry knows where the live
# constant lives (module attr, patched in-process for backtests only),
# its bounds, and which file a human-facing diff should show.
EVOLVABLE_PARAMETERS = {
    "vix_block_above": {
        "module": "src.strategy", "attr": "VIX_BLOCK_ABOVE",
        "bounds": (12.0, 24.0), "type": float, "file": "src/strategy.py",
        "doc": "India VIX level above which range-bound structures are blocked",
    },
    "options_risk_per_trade_pct": {
        "module": "src.options_proposer", "attr": "OPTIONS_RISK_PER_TRADE_PCT",
        "bounds": (2.0, 20.0), "type": float, "file": "config.json",
        "doc": "percent of the book a single spread's max loss may consume",
    },
    "short_strike_otm_pct": {
        "module": "src.options_proposer", "attr": "SHORT_STRIKE_OTM_PCT",
        "bounds": (1.0, 5.0), "type": float, "file": "src/options_proposer.py",
        "doc": "how far OTM (percent of spot) the condor's short strikes sit",
    },
    "profit_take_fraction": {
        "module": "src.plan_tracker", "attr": "OPTION_PROFIT_TAKE_FRACTION",
        "bounds": (0.40, 0.90), "type": float, "file": "src/plan_tracker.py",
        "doc": "fraction of max profit at which spreads auto-exit",
    },
    "pre_expiry_exit_days": {
        "module": "src.plan_tracker", "attr": "PRE_EXPIRY_EXIT_DAYS",
        "bounds": (1, 5), "type": int, "file": "src/plan_tracker.py",
        "doc": "days before expiry at which spreads are force-exited",
    },
}


def _module(name: str):
    import importlib
    return importlib.import_module(name)


def current_value(param: str):
    spec = EVOLVABLE_PARAMETERS[param]
    return getattr(_module(spec["module"]), spec["attr"])


@contextmanager
def override_parameters(overrides: dict):
    """Temporarily patch the live module constants (backtests ONLY —
    never persisted; restoration is unconditional). This is the seam
    that lets the Phase 7 simulator replay history under mutated rules
    without a single production file changing."""
    saved = {}
    try:
        for param, value in overrides.items():
            spec = EVOLVABLE_PARAMETERS[param]
            mod = _module(spec["module"])
            saved[param] = getattr(mod, spec["attr"])
            setattr(mod, spec["attr"], spec["type"](value))
        yield
    finally:
        for param, value in saved.items():
            spec = EVOLVABLE_PARAMETERS[param]
            setattr(_module(spec["module"]), spec["attr"], value)


# --- 1. loss-cluster mining -------------------------------------------------

def find_loss_clusters(conn, min_size: int = MIN_CLUSTER_SIZE) -> list:
    """Losing simulated trades grouped by (underlying, strategy, VIX
    band), largest total damage first. Each cluster carries its exact
    journal_refs — the provenance every candidate must cite."""
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT journal_ref, underlying, strategy, vix, pnl_net, "
            "r_multiple, capture_pct, resolution, proposed_on, exit_date "
            "FROM simulated_trades WHERE result = 'loss'")]
    except Exception:
        return []
    clusters: dict = {}
    for row in rows:
        key = (row["underlying"], row["strategy"], vix_band(row["vix"]))
        clusters.setdefault(key, []).append(row)
    out = []
    for (underlying, strategy, band), trades in clusters.items():
        if len(trades) < min_size:
            continue
        out.append({
            "underlying": underlying, "strategy": strategy,
            "vix_band": band, "trades": trades,
            "journal_refs": [t["journal_ref"] for t in trades],
            "total_loss": round(sum(t["pnl_net"] for t in trades), 2),
        })
    return sorted(out, key=lambda c: c["total_loss"])  # most negative first


# --- 2. hindsight (HER-style) reinterpretation -------------------------------

def hindsight_split(trade: dict) -> str:
    """Deterministic partial-success read of one loss:
      bad_risk_parameters — rode to (near) max loss: the structure/size
                            was wrong for the regime (r <= -0.9)
      bad_timing          — forced out near expiry at a shallow loss:
                            the structure was fine, the clock wasn't
      ambiguous           — everything else"""
    r = trade.get("r_multiple")
    if r is not None and float(r) <= -0.9:
        return "bad_risk_parameters"
    if (trade.get("resolution") == "pre_expiry_exit"
            and r is not None and float(r) > -0.5):
        return "bad_timing"
    return "ambiguous"


def cluster_summary(cluster: dict) -> dict:
    buckets: dict = {}
    for t in cluster["trades"]:
        buckets.setdefault(hindsight_split(t), []).append(t["journal_ref"])
    vixes = [t["vix"] for t in cluster["trades"] if t["vix"] is not None]
    return {
        "underlying": cluster["underlying"], "strategy": cluster["strategy"],
        "vix_band": cluster["vix_band"], "n_losses": len(cluster["trades"]),
        "total_loss": cluster["total_loss"],
        "hindsight_buckets": {k: len(v) for k, v in buckets.items()},
        "avg_vix": round(statistics.mean(vixes), 2) if vixes else None,
        "journal_refs": cluster["journal_refs"],
    }


# --- 3. counterfactual context ------------------------------------------------

def counterfactual_context(conn, cluster: dict) -> dict:
    """The same setup's WINS, statistically contrasted with the losses,
    so the analyst reasons about a concrete pivot instead of vibes."""
    try:
        wins = [dict(r) for r in conn.execute(
            "SELECT vix, pnl_net, r_multiple, proposed_on, exit_date "
            "FROM simulated_trades WHERE result = 'win' AND underlying = ? "
            "AND strategy = ?", (cluster["underlying"], cluster["strategy"]))]
    except Exception:
        wins = []
    win_vix = [w["vix"] for w in wins if w["vix"] is not None]
    loss_vix = [t["vix"] for t in cluster["trades"] if t["vix"] is not None]
    return {
        "n_wins": len(wins),
        "wins_avg_vix": round(statistics.mean(win_vix), 2) if win_vix else None,
        "losses_avg_vix": round(statistics.mean(loss_vix), 2) if loss_vix else None,
        "wins_avg_r": round(statistics.mean(
            [w["r_multiple"] for w in wins if w["r_multiple"] is not None]), 2)
            if wins else None,
    }


# --- 4/5. the dialectic: analyst proposes, critic attacks --------------------

def _llm_json(extractor, system: str, payload: str, required: dict,
              retries: int = 1):
    """One schema-gated LLM call: parse JSON, check every required key's
    type, or return None — bad JSON never propagates (spec guardrail #2).

    Small-model pragmatics: near-miss shapes are coerced (a lone string
    where a list was required becomes a one-item list — 3B models do this
    constantly), and a retry APPENDS the failure reason to the payload —
    at temperature 0 an identical re-ask would just reproduce the same
    broken reply."""
    ask = payload
    for _ in range(retries + 1):
        raw = extractor._chat(ask, system_prompt=system)
        if raw is None:
            return None
        try:
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start:end + 1])
            for key, typ in required.items():
                if key not in data:
                    raise ValueError(f"missing key {key!r}")
                if typ is list and isinstance(data[key], str):
                    data[key] = [data[key]]          # tolerated near-miss
                if not isinstance(data[key], typ):
                    raise ValueError(f"key {key!r} must be {typ.__name__}")
            return data
        except Exception as e:
            ask = (payload + "\n\nYour previous reply was rejected: "
                   f"{e}. Respond again with ONLY a JSON object containing "
                   f"exactly these keys: {list(required)}.")
            continue
    return None


def validate_proposal(proposal: dict) -> str | None:
    """The deterministic gate behind the analyst: whitelisted parameter,
    in-bounds value, actually different from current. Returns a rejection
    reason or None when clean."""
    param = proposal.get("parameter")
    if param not in EVOLVABLE_PARAMETERS:
        return f"parameter {param!r} is not in the evolvable whitelist"
    spec = EVOLVABLE_PARAMETERS[param]
    try:
        value = spec["type"](proposal.get("proposed_value"))
    except (TypeError, ValueError):
        return "proposed_value is not coercible to the parameter's type"
    lo, hi = spec["bounds"]
    if not (lo <= value <= hi):
        return f"proposed_value {value} outside bounds [{lo}, {hi}]"
    if value == current_value(param):
        return "proposed_value equals the current value — not a mutation"
    return None


ANALYST_SYSTEM = (
    "You are the ADiTrader evolution analyst. You study losing trade "
    "clusters and propose EXACTLY ONE parameter mutation from the given "
    "whitelist. Respond with ONLY a JSON object: {\"parameter\": str, "
    "\"proposed_value\": number, \"rationale\": str, \"expected_effect\": "
    "str}. Be quantitative. Never propose anything outside the whitelist.")

CRITIC_SYSTEM = (
    "You are the ADiTrader evolution critic — an adversarial validator. "
    "Attack the proposed mutation: overfitting to this cluster, edge "
    "cases, regime dependence, interaction with other rules. Respond "
    "with ONLY a JSON object: {\"objections\": [str, ...], \"verdict\": "
    "\"block\" or \"proceed_if_resolved\"}.")

RESOLUTION_SYSTEM = (
    "You are the ADiTrader evolution analyst answering your critic. "
    "Address each objection concretely, or withdraw. Respond with ONLY "
    "a JSON object: {\"resolutions\": [str, ...], \"withdraw\": true/false}.")


def run_dialectic(extractor, summary: dict, counterfactual: dict) -> dict | None:
    """Analyst -> Critic -> Analyst resolution -> consensus gate.
    Returns {"proposal", "critique", "resolution"} for a candidate that
    survived, else None (with the reason printed)."""
    registry = {p: {"current": current_value(p), "bounds": s["bounds"],
                    "doc": s["doc"]} for p, s in EVOLVABLE_PARAMETERS.items()}
    base_payload = json.dumps({"loss_cluster": summary,
                               "counterfactual_wins": counterfactual,
                               "evolvable_parameters": registry})

    proposal = _llm_json(extractor, ANALYST_SYSTEM, base_payload,
                         {"parameter": str, "rationale": str,
                          "expected_effect": str})
    if proposal is None:
        print("    (analyst: no valid JSON proposal — dropped)")
        return None
    rejection = validate_proposal(proposal)
    if rejection:
        print(f"    (proposal gate: {rejection} — dropped)")
        return None

    critique = _llm_json(extractor, CRITIC_SYSTEM,
                         json.dumps({"cluster": summary,
                                     "proposal": proposal}),
                         {"objections": list, "verdict": str})
    if critique is None:
        print("    (critic: no valid JSON — dropped, fail-closed)")
        return None
    if critique["verdict"] == "block":
        print("    (critic: BLOCK — candidate killed at the consensus gate)")
        return None

    resolution = _llm_json(extractor, RESOLUTION_SYSTEM,
                           json.dumps({"proposal": proposal,
                                       "objections": critique["objections"]}),
                           {"resolutions": list, "withdraw": bool})
    if resolution is None or resolution["withdraw"]:
        print("    (analyst withdrew under critique — dropped)")
        return None
    return {"proposal": proposal, "critique": critique,
            "resolution": resolution}


# --- 6. the revert-on-regression backtest ------------------------------------

def _portfolio_metrics(conn) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT pnl_net, result FROM simulated_trades ORDER BY exit_date")]
    if not rows:
        return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0}
    pnls = [r["pnl_net"] for r in rows]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    return {
        "trades": len(rows),
        "win_rate": round(sum(r["result"] == "win" for r in rows)
                          / len(rows) * 100, 2),
        "total_pnl": round(sum(pnls), 2),
        "sharpe": round(statistics.mean(pnls) / stdev, 4) if stdev else 0.0,
        "max_drawdown": round(max_dd, 2),
    }


def _cluster_losses(conn, cluster: dict) -> int:
    rows = conn.execute(
        "SELECT vix FROM simulated_trades WHERE result = 'loss' AND "
        "underlying = ? AND strategy = ?",
        (cluster["underlying"], cluster["strategy"])).fetchall()
    return sum(1 for r in rows if vix_band(r[0]) == cluster["vix_band"])


# --- anti-overfitting guardrails (Phase 5 scratchpad build) ------------------
#
# Two structural sanity checks against the classic genetic-optimizer trap:
# "perfect" parameters mined from a small or one-regime sample that break
# the moment conditions shift (decision #50 already demonstrated the
# failure mode empirically on regime tags).

# 1. Refuse to evolve at all on a thin corpus — below this many resolved
#    simulated trades, every cluster is more likely noise than signal.
MIN_TRADES_FOR_EVOLUTION = 30


def corpus_size(conn) -> int:
    """Resolved trades available to learn from (0 when the table is
    missing — a fresh DB must gate exactly like a thin one)."""
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM simulated_trades").fetchone()[0])
    except Exception:
        return 0


# 2. Split-window stability: a candidate that only wins in the full-window
#    average may be riding one regime. The replay window is cut into two
#    halves (earlier half vs later half — regimes shift over time, so the
#    halves are cheap regime proxies), and a would-be promotion must not
#    DEGRADE total P&L in either half. Mathematically lightweight (two
#    extra baseline/mutated replays), structurally the standard
#    out-of-sample sign-consistency test.

def window_halves(start: str, end: str) -> tuple:
    """((start, mid), (mid, end)) — the midpoint date of the replay
    window, shared as the boundary bar."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    mid = (d0 + (d1 - d0) / 2).isoformat()
    return (start, mid), (mid, end)


def backtest_candidate(cluster: dict, proposal: dict, bars_by_underlying: dict,
                       vix_by_date: dict, start: str, end: str) -> dict:
    """Baseline vs mutated replay over identical cached history. Verdict:
      promoted               — cluster repaired, no global regression, AND
                               stable across both window halves
      unstable_out_of_sample — full-window winner that degrades one half
                               (one-regime overfit; discarded)
      revert_on_regression   — cluster repaired but Sharpe/drawdown degraded
      no_repair              — the mutation didn't even fix its own cluster"""
    from src import brain_map
    from src.simulator import run_simulation
    underlyings = tuple(bars_by_underlying)

    def replay(win_start: str, win_end: str) -> tuple:
        conn = brain_map.connect(":memory:")
        run_simulation(win_start, win_end, underlyings, conn=conn,
                       bars_by_underlying=bars_by_underlying,
                       vix_by_date=vix_by_date)
        metrics = _portfolio_metrics(conn)
        losses = _cluster_losses(conn, cluster)
        conn.close()
        return metrics, losses

    overrides = {proposal["parameter"]: proposal["proposed_value"]}

    baseline, baseline_losses = replay(start, end)
    with override_parameters(overrides):
        mutated, mutated_losses = replay(start, end)

    repaired = mutated_losses < baseline_losses
    regressed = (mutated["sharpe"] < baseline["sharpe"] * SHARPE_TOLERANCE
                 or mutated["max_drawdown"] >
                 baseline["max_drawdown"] * DRAWDOWN_TOLERANCE)
    verdict = ("no_repair" if not repaired
               else "revert_on_regression" if regressed else "promoted")

    stability = None
    if verdict == "promoted":
        # Only would-be promotions pay for the four extra half-replays.
        halves = []
        for win_start, win_end in window_halves(start, end):
            half_baseline, _ = replay(win_start, win_end)
            with override_parameters(overrides):
                half_mutated, _ = replay(win_start, win_end)
            halves.append({
                "window": [win_start, win_end],
                "baseline_pnl": half_baseline["total_pnl"],
                "mutated_pnl": half_mutated["total_pnl"],
                "delta_pnl": round(half_mutated["total_pnl"]
                                   - half_baseline["total_pnl"], 2),
            })
        stable = all(h["delta_pnl"] >= 0 for h in halves)
        stability = {"stable": stable, "halves": halves}
        if not stable:
            verdict = "unstable_out_of_sample"

    return {"verdict": verdict, "baseline": baseline, "mutated": mutated,
            "cluster_losses_baseline": baseline_losses,
            "cluster_losses_mutated": mutated_losses,
            "stability": stability}


# --- 7. provenance, lineage, and the candidate file ---------------------------

def _load_lineage(path: Path = None) -> list:
    path = path or LINEAGE_PATH
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def append_lineage(entry: dict, path: Path = None) -> dict:
    """Version-tree bookkeeping: vN per parameter, parent = the previous
    entry for the same parameter (any verdict — failed attempts are part
    of the history so future agents know what was already tried)."""
    path = path or LINEAGE_PATH
    lineage = _load_lineage(path)
    prior = [e for e in lineage if e["parameter"] == entry["parameter"]]
    entry = dict(entry,
                 version=f"v{len(prior) + 1}",
                 parent=prior[-1]["candidate_id"] if prior else None)
    lineage.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lineage, indent=2))
    return entry


def parameter_diff(param: str, new_value) -> str:
    """A REAL unified diff of the file a human would edit — generated
    deterministically from the registry, never by the LLM."""
    spec = EVOLVABLE_PARAMETERS[param]
    target = ROOT / spec["file"]
    try:
        old_text = target.read_text()
    except OSError:
        return f"(could not read {spec['file']})"
    old_lines = old_text.splitlines(keepends=True)
    needle = (f'"{param}"' if spec["file"].endswith(".json")
              else spec["attr"])
    new_lines = []
    for line in old_lines:
        if needle in line and str(current_value(param)) in line:
            new_lines.append(line.replace(str(current_value(param)),
                                          str(spec["type"](new_value)), 1))
        else:
            new_lines.append(line)
    diff = difflib.unified_diff(old_lines, new_lines,
                                fromfile=f"a/{spec['file']}",
                                tofile=f"b/{spec['file']}")
    return "".join(diff) or "(no textual match found — apply by hand)"


def write_candidate(cluster_sum: dict, dialectic: dict, backtest: dict,
                    lineage_entry: dict, out_dir: Path = None,
                    now: datetime = None) -> Path:
    out_dir = out_dir or CANDIDATES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    p, c, r = (dialectic["proposal"], dialectic["critique"],
               dialectic["resolution"])
    b, m = backtest["baseline"], backtest["mutated"]
    path = out_dir / f"evolution_{now:%Y%m%d_%H%M%S}.md"
    objections = "\n".join(f"  - CRITIC: {o}" for o in c["objections"])
    resolutions = "\n".join(f"  - ANALYST: {x}" for x in r["resolutions"])
    path.write_text(f"""# Evolution Candidate {lineage_entry['candidate_id']} \
({p['parameter']} {lineage_entry['version']})

> STATUS: awaiting human review — nothing is applied automatically.
> Parent: {lineage_entry['parent'] or '(first mutation of this parameter)'}

## 1. Target Error Cluster
- Setup: {cluster_sum['strategy']} on {cluster_sum['underlying']}, \
VIX band {cluster_sum['vix_band']} (avg {cluster_sum['avg_vix']})
- {cluster_sum['n_losses']} losses, Rs.{cluster_sum['total_loss']:,.2f} total
- Hindsight buckets: {json.dumps(cluster_sum['hindsight_buckets'])}
- Provenance (journal_refs): {', '.join(cluster_sum['journal_refs'])}

## 2. Dialectic Debate Summary
- ANALYST proposal: set `{p['parameter']}` \
{current_value(p['parameter'])} -> {p['proposed_value']}
  - Rationale: {p['rationale']}
  - Expected effect: {p['expected_effect']}
{objections}
{resolutions}
- Consensus gate: PASSED (critic verdict: {c['verdict']}, analyst did not \
withdraw)

## 3. Simulator Proof
| metric | baseline | mutated |
|---|---|---|
| trades | {b['trades']} | {m['trades']} |
| win rate | {b['win_rate']}% | {m['win_rate']}% |
| total P&L | Rs.{b['total_pnl']:,.2f} | Rs.{m['total_pnl']:,.2f} |
| per-trade Sharpe | {b['sharpe']} | {m['sharpe']} |
| max drawdown | Rs.{b['max_drawdown']:,.2f} | Rs.{m['max_drawdown']:,.2f} |
| target-cluster losses | {backtest['cluster_losses_baseline']} | \
{backtest['cluster_losses_mutated']} |

Verdict: **{backtest['verdict']}**

## 4. Code Diff
```diff
{parameter_diff(p['parameter'], p['proposed_value'])}
```
""")
    return path


# --- the orchestrator ---------------------------------------------------------

def load_bars_cache(path: Path = None) -> dict | None:
    path = path or BARS_CACHE_PATH
    try:
        cache = json.loads(path.read_text())
        assert cache.get("bars") and cache.get("vix")
        return cache
    except Exception:
        return None


def run_evolution(conn, extractor, bars_cache: dict,
                  max_candidates: int = MAX_CANDIDATES_PER_RUN,
                  out_dir: Path = None, lineage_path: Path = None,
                  now: datetime = None) -> dict:
    """The full mine -> reason -> verify -> record pass. Returns a stats
    dict; every dropped candidate says why. No production file changes,
    ever — output goes only to candidates/ and the lineage ledger."""
    bars = {u: [tuple(b) for b in blist]
            for u, blist in bars_cache["bars"].items()}
    vix = bars_cache["vix"]
    start, end = bars_cache["start"], bars_cache["end"]

    # Anti-overfitting guard 1: no evolution on a thin corpus — clusters
    # mined from a handful of trades are noise wearing a pattern costume.
    n_trades = corpus_size(conn)
    if n_trades < MIN_TRADES_FOR_EVOLUTION:
        reason = (f"corpus too small: {n_trades} resolved trades < the "
                  f"{MIN_TRADES_FOR_EVOLUTION}-trade floor — refusing to "
                  "optimize against noise (anti-overfitting guard)")
        print(f"  {reason}")
        return {"skipped": reason, "clusters_found": 0, "examined": 0,
                "written": 0, "reverted": 0, "no_repair": 0, "dropped": 0,
                "unstable": 0, "candidates": []}

    clusters = find_loss_clusters(conn)
    stats = {"clusters_found": len(clusters), "examined": 0,
             "written": 0, "reverted": 0, "no_repair": 0, "dropped": 0,
             "unstable": 0, "candidates": []}
    for cluster in clusters[:max_candidates]:
        stats["examined"] += 1
        summary = cluster_summary(cluster)
        print(f"  cluster: {summary['strategy']}/{summary['underlying']}"
              f"/{summary['vix_band']} — {summary['n_losses']} losses "
              f"Rs.{summary['total_loss']:,.0f}")
        dialectic = run_dialectic(extractor, summary,
                                  counterfactual_context(conn, cluster))
        if dialectic is None:
            stats["dropped"] += 1
            continue
        result = backtest_candidate(cluster, dialectic["proposal"],
                                    bars, vix, start, end)
        entry = append_lineage({
            "candidate_id": hashlib.sha1(
                json.dumps([summary["journal_refs"],
                            dialectic["proposal"]["parameter"],
                            str(dialectic["proposal"]["proposed_value"])]
                           ).encode()).hexdigest()[:12],
            "parameter": dialectic["proposal"]["parameter"],
            "from_value": current_value(dialectic["proposal"]["parameter"]),
            "to_value": dialectic["proposal"]["proposed_value"],
            "verdict": result["verdict"],
            "cluster_refs": summary["journal_refs"],
            "reasoning": {
                "rationale": dialectic["proposal"]["rationale"],
                "objections": dialectic["critique"]["objections"],
                "resolutions": dialectic["resolution"]["resolutions"]},
            "at": (now or datetime.now()).isoformat(timespec="seconds"),
        }, path=lineage_path)
        if result["verdict"] == "promoted":
            path = write_candidate(summary, dialectic, result, entry,
                                   out_dir=out_dir, now=now)
            stats["written"] += 1
            stats["candidates"].append(str(path))
            print(f"    PROMOTED -> {path.name}")
        elif result["verdict"] == "revert_on_regression":
            stats["reverted"] += 1
            print("    RevertOnRegression — repaired the cluster but "
                  "degraded global Sharpe/drawdown; discarded (lineage "
                  "remembers the attempt).")
        elif result["verdict"] == "unstable_out_of_sample":
            stats["unstable"] += 1
            print("    unstable_out_of_sample — wins the full window but "
                  "degrades one half of it (one-regime overfit); discarded "
                  "(lineage remembers the attempt).")
        else:
            stats["no_repair"] += 1
            print("    no_repair — mutation didn't fix its own cluster; "
                  "discarded.")
    return stats


def run_from_sleep_phase(conn, extractor, today: date = None) -> dict:
    """Task E seam for sleep_phase: silently skips wherever the pieces
    are missing (the VM has no Ollama; a machine without a bars cache
    can't backtest) — same graceful-degradation contract as Tasks A-D."""
    if not extractor.is_reachable():
        return {"skipped": "Ollama not reachable"}
    cache = load_bars_cache()
    if cache is None:
        return {"skipped": "no bars cache (python3 -m src.evolution "
                           "--refresh-bars-cache)"}
    return run_evolution(conn, extractor, cache)


# --- bars-cache refresh (Mac-side; the VM owns the only live token) -----------

_REMOTE_DUMP = r"""
import json, sys
sys.path.insert(0, ".")
from src.simulator import _fetch_bars, _fetch_vix_series
start = sys.argv[1]
out = {"start": start, "end": sys.argv[2],
       "bars": {u: _fetch_bars(u, start)
                for u in ("NIFTY 50", "NIFTY BANK")},
       "vix": _fetch_vix_series(start)}
print(json.dumps(out))
"""


def refresh_bars_cache(start: str = None, end: str = None,
                       path: Path = None) -> bool:
    """Pull fresh bars+VIX through the VM (its token) into the local
    cache. The dump script travels as a FILE via scp — multi-line python
    shipped through ssh --command gets its newlines mangled by the remote
    shell (bug found live 2026-07-09)."""
    from src.edge_miner import _gcloud, GCP_PROJECT, GCP_ZONE, VM, VM_REPO
    gcloud = _gcloud()
    if gcloud is None:
        print("refresh_bars_cache: gcloud CLI not found")
        return False
    start = start or "2023-01-01"
    end = end or date.today().isoformat()
    path = path or BARS_CACHE_PATH
    with tempfile.NamedTemporaryFile("w", suffix=".py",
                                     delete=False) as f:
        f.write(_REMOTE_DUMP)
        script = f.name
    res = subprocess.run(
        [gcloud, "compute", "scp", script, f"{VM}:/tmp/bars_dump.py",
         f"--project={GCP_PROJECT}", f"--zone={GCP_ZONE}", "--quiet"],
        capture_output=True, text=True, timeout=120)
    Path(script).unlink(missing_ok=True)
    if res.returncode != 0:
        print(f"refresh_bars_cache: script upload failed: {res.stderr[-300:]}")
        return False
    res = subprocess.run(
        [gcloud, "compute", "ssh", VM, f"--project={GCP_PROJECT}",
         f"--zone={GCP_ZONE}", "--quiet", "--command",
         f"cd {VM_REPO} && venv/bin/python3 /tmp/bars_dump.py "
         f"{start} {end}; rm -f /tmp/bars_dump.py"],
        capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        print(f"refresh_bars_cache: remote dump failed: {res.stderr[-300:]}")
        return False
    try:
        cache = json.loads(res.stdout[res.stdout.find("{"):])
        assert cache["bars"] and cache["vix"]
    except Exception as e:
        print(f"refresh_bars_cache: bad payload ({e})")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache))
    n = {u: len(b) for u, b in cache["bars"].items()}
    print(f"bars cache refreshed: {n}, vix sessions: {len(cache['vix'])}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Procedural Evolution: mine loss clusters, propose "
                    "gated rule mutations to candidates/")
    parser.add_argument("--refresh-bars-cache", action="store_true")
    parser.add_argument("--start", default=None,
                        help="bars-cache start date (YYYY-MM-DD)")
    parser.add_argument("--max-candidates", type=int,
                        default=MAX_CANDIDATES_PER_RUN)
    args = parser.parse_args()

    if args.refresh_bars_cache:
        raise SystemExit(0 if refresh_bars_cache(start=args.start) else 1)

    from src import brain_map
    from src.local_parser import LocalExtractor
    extractor = LocalExtractor()
    if not extractor.is_reachable():
        raise SystemExit("Ollama is not running — start it and retry.")
    cache = load_bars_cache()
    if cache is None:
        raise SystemExit("No bars cache — run: python3 -m src.evolution "
                         "--refresh-bars-cache")
    connection = brain_map.connect()
    summary = run_evolution(connection, extractor, cache,
                            max_candidates=args.max_candidates)
    connection.close()
    print(json.dumps(summary, indent=2))
