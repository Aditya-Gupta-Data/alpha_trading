"""
src/knowledge_graph/graphrag_eval.py — GraphRAG Forward Outcome Pairing & Placebo Gate
======================================================================================

Implements the promotion readiness harness defined in
`docs/ARCHITECTURE_ROADMAP.md` §2.5, §3, and §5:

  * **Corporate-Action Adjusted Outcome Pairing:** Pairs forward-logged
    `graph_activation_log` runs (`in_universe` nodes with `|E(v)| >= theta`)
    against realized `horizon_days`-session forward price returns computed on
    `src.ingestion.corporate_actions.adjusted` bars (so stock splits and
    bonuses never corrupt outcome pairing).
  * **Hard Sample-Size Gate (`MIN_FORWARD_ACTIVATIONS = 60`):** Refuses to
    render a promotion verdict below 60 paired forward activations
    (`status = "ACCUMULATING"`).
  * **Shuffled-Edge Placebo Comparison:** Permutes target endpoints and edge
    polarities across the active graph (`n_shuffles` draws, preserving degree
    sequence and edge weight/confidence marginals), replays the same historical
    seed activations through `spreading_activation.propagate`, and computes an
    add-one surrogate p-value (`p_shuffled`) plus a one-sided 95% Wilson lower
    bound (`stat_gates.wilson_lower_bound`).
  * **Zero Execution Authority:** Even when all three gates pass
    (`n >= 60`, `wilson_lb > 0.50`, `p_shuffled < alpha`), the harness emits
    `status = "READY_FOR_DEPT5_DECISION"` — never self-promoting edges into
    `vol_bridge` or altering sizing/entry rules.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date

import networkx as nx

from src.ingestion import corporate_actions
from src.knowledge_graph import spreading_activation
from src.validation import stat_gates

MIN_FORWARD_ACTIVATIONS = 60
DEFAULT_HORIZON_SESSIONS = 5
DEFAULT_ALPHA = 0.05
DEFAULT_SHUFFLES = 100


def _adjusted_bars_by_ticker(
    bars_by_ticker: dict[str, list[dict]],
    adjust_corporate_actions: bool = True,
    actions: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Return chronologically sorted, corporate-action-adjusted bars per ticker."""
    out: dict[str, list[dict]] = {}
    for ticker, raw_bars in (bars_by_ticker or {}).items():
        if not raw_bars:
            continue
        sorted_bars = sorted(
            (dict(b) for b in raw_bars if b.get("date") and b.get("close") is not None),
            key=lambda b: str(b["date"])[:10],
        )
        if not sorted_bars:
            continue
        clean_sym = str(ticker).upper().removesuffix(".NS").removesuffix(".BO").strip()
        if adjust_corporate_actions:
            out[clean_sym] = corporate_actions.adjusted(
                clean_sym, sorted_bars, actions=actions
            )
        else:
            out[clean_sym] = sorted_bars
    return out


def _forward_return_pct(
    bars: list[dict],
    run_day: str,
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
) -> float | None:
    """Compute realized percentage return from the first bar on/after `run_day`
    to `horizon_sessions` trading sessions later on corporate-action-adjusted closes."""
    if not bars or horizon_sessions < 1:
        return None
    day_prefix = str(run_day)[:10]
    start_idx = None
    for idx, b in enumerate(bars):
        if str(b.get("date", ""))[:10] >= day_prefix:
            start_idx = idx
            break
    if start_idx is None or start_idx + horizon_sessions >= len(bars):
        return None
    c0 = float(bars[start_idx]["close"])
    c1 = float(bars[start_idx + horizon_sessions]["close"])
    if c0 <= 0.0:
        return None
    return (c1 - c0) / c0 * 100.0


def pair_activations_with_outcomes(
    conn,
    bars_by_ticker: dict[str, list[dict]],
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    theta: float = 0.02,
    in_universe_only: bool = True,
    adjust_corporate_actions: bool = True,
    actions: list[dict] | None = None,
) -> dict:
    """Pair forward-logged `graph_activation_log` activations with realized
    corporate-action-adjusted forward returns.

    Returns:
      {
        "n_paired": int,
        "wins": int,
        "losses": int,
        "hit_rate": float | None,
        "wilson_lb": float,
        "mean_signed_return_pct": float | None,
        "pairs": list[dict],
      }
    """
    adj_bars = _adjusted_bars_by_ticker(
        bars_by_ticker,
        adjust_corporate_actions=adjust_corporate_actions,
        actions=actions,
    )
    try:
        rows = conn.execute(
            "SELECT id, run_at, trigger_ref, seeds_json, result_json "
            "FROM graph_activation_log ORDER BY id ASC"
        ).fetchall()
    except Exception:
        rows = []

    universe_nodes: set[str] = set()
    try:
        for r in conn.execute(
            "SELECT node_id FROM graph_nodes WHERE in_universe = 1"
        ).fetchall():
            universe_nodes.add(str(r["node_id"]).upper())
    except Exception:
        pass

    pairs: list[dict] = []
    for r in rows:
        run_day = str(r["run_at"] or "")[:10]
        if len(run_day) != 10:
            continue
        try:
            res = json.loads(r["result_json"] or "{}")
        except (ValueError, TypeError):
            continue
        for node_id, info in sorted(res.items()):
            if not isinstance(info, dict):
                continue
            energy = float(info.get("energy") or 0.0)
            if abs(energy) < theta:
                continue
            sym = str(node_id).upper().removesuffix(".NS").removesuffix(".BO")
            is_univ = bool(info.get("in_universe")) or (sym in universe_nodes)
            if in_universe_only and not is_univ:
                continue
            bars = adj_bars.get(sym)
            if not bars:
                continue
            fwd_ret = _forward_return_pct(bars, run_day, horizon_sessions=horizon_sessions)
            if fwd_ret is None or fwd_ret == 0.0:
                continue
            direction = 1 if energy > 0 else -1
            signed_ret = direction * fwd_ret
            hit = signed_ret > 0.0
            pairs.append({
                "log_id": r["id"],
                "run_day": run_day,
                "trigger_ref": r["trigger_ref"],
                "node_id": sym,
                "energy": round(energy, 6),
                "momentum": round(float(info.get("momentum") or energy), 6),
                "hop": int(info.get("hop") or info.get("dominant_hop") or 1),
                "forward_return_pct": round(fwd_ret, 4),
                "signed_return_pct": round(signed_ret, 4),
                "hit": hit,
            })

    n = len(pairs)
    wins = sum(1 for p in pairs if p["hit"])
    losses = n - wins
    hit_rate = round(wins / n, 4) if n > 0 else None
    wilson_lb = round(stat_gates.wilson_lower_bound(wins, n), 4) if n > 0 else 0.0
    mean_signed = (
        round(math.fsum(p["signed_return_pct"] for p in pairs) / n, 4)
        if n > 0 else None
    )
    return {
        "n_paired": n,
        "wins": wins,
        "losses": losses,
        "hit_rate": hit_rate,
        "wilson_lb": wilson_lb,
        "mean_signed_return_pct": mean_signed,
        "pairs": pairs,
    }


def build_shuffled_graph(graph: nx.DiGraph, rng: random.Random) -> nx.DiGraph:
    """Create a degree-preserving shuffled-edge placebo graph:
    for each active edge out of `u`, target nodes are randomly reassigned from
    the graph's candidate target nodes and edge polarity is randomly Rademacher
    sign-flipped (`+1` / `-1`) while preserving the magnitude distribution of
    `edge_weight` and `confidence_score`."""
    edges = list(graph.edges(data=True))
    if not edges:
        return nx.DiGraph()
    all_nodes = list(graph.nodes())
    attrs = [dict(d) for _, _, d in edges]
    rng.shuffle(attrs)
    shuffled = nx.DiGraph()
    for n, ndata in graph.nodes(data=True):
        shuffled.add_node(n, **dict(ndata))
    for (u, _, _), d in zip(edges, attrs):
        candidates = [v for v in all_nodes if v != u]
        if not candidates:
            continue
        v = rng.choice(candidates)
        d_copy = dict(d)
        d_copy["polarity"] = rng.choice((-1, 1))
        shuffled.add_edge(u, v, **d_copy)
    return shuffled


def shuffled_edge_placebo(
    graph: nx.DiGraph,
    seed_runs: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    n_shuffles: int = DEFAULT_SHUFFLES,
    theta: float = 0.02,
    adjust_corporate_actions: bool = True,
    actions: list[dict] | None = None,
    rng: random.Random | None = None,
) -> dict:
    """Compare real spreading-activation mean signed return & hit-rate on
    `seed_runs` (`[{"run_at": "YYYY-MM-DD", "seeds": {node: val}}, ...]`)
    against `n_shuffles` shuffled-edge placebo graphs."""
    rng = rng or random.Random(0)
    adj_bars = _adjusted_bars_by_ticker(
        bars_by_ticker,
        adjust_corporate_actions=adjust_corporate_actions,
        actions=actions,
    )

    def _score_runs(per_run_shuffle: bool) -> tuple[int, int, float]:
        signed_rets: list[float] = []
        wins = 0
        for run in seed_runs or []:
            run_day = str(run.get("run_at") or "")[:10]
            seeds = run.get("seeds") or {}
            if not run_day or not seeds:
                continue
            g_run = build_shuffled_graph(graph, rng) if per_run_shuffle else graph
            res = spreading_activation.propagate(
                g_run, seeds, run_at=run_day, log_activation=False
            )
            for item in res.get("activated") or []:
                e_val = float(item.get("energy") or 0.0)
                if abs(e_val) < theta:
                    continue
                sym = str(item["node_id"]).upper().removesuffix(".NS").removesuffix(".BO")
                bars = adj_bars.get(sym)
                if not bars:
                    continue
                fwd = _forward_return_pct(bars, run_day, horizon_sessions=horizon_sessions)
                if fwd is None or fwd == 0.0:
                    continue
                s_ret = (1 if e_val > 0 else -1) * fwd
                signed_rets.append(s_ret)
                if s_ret > 0.0:
                    wins += 1
        n_obs = len(signed_rets)
        mean_s = (math.fsum(signed_rets) / n_obs) if n_obs > 0 else 0.0
        return n_obs, wins, mean_s

    obs_n, obs_wins, obs_mean = _score_runs(per_run_shuffle=False)
    null_means: list[float] = []
    null_hit_rates: list[float] = []
    for _ in range(max(1, int(n_shuffles))):
        sn, sw, sm = _score_runs(per_run_shuffle=True)
        null_means.append(sm)
        null_hit_rates.append((sw / sn) if sn > 0 else 0.5)

    beaten_or_equal = sum(1 for m in null_means if m >= obs_mean)
    p_shuffled = (beaten_or_equal + 1) / (len(null_means) + 1)
    obs_hit_rate = round(obs_wins / obs_n, 4) if obs_n > 0 else None
    return {
        "n_paired": obs_n,
        "wins": obs_wins,
        "observed_hit_rate": obs_hit_rate,
        "observed_mean_signed_return_pct": round(obs_mean, 4),
        "p_shuffled": round(p_shuffled, 4),
        "null_mean_signed_return_pct": round(
            math.fsum(null_means) / len(null_means), 4
        ),
        "n_shuffles": len(null_means),
    }


def evaluate_promotion_readiness(
    conn,
    graph: nx.DiGraph,
    bars_by_ticker: dict[str, list[dict]],
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    min_activations: int = MIN_FORWARD_ACTIVATIONS,
    alpha: float = DEFAULT_ALPHA,
    n_shuffles: int = DEFAULT_SHUFFLES,
    adjust_corporate_actions: bool = True,
    actions: list[dict] | None = None,
    rng: random.Random | None = None,
) -> dict:
    """Department-5 readiness check for Phase 1 -> Phase 2 GraphRAG promotion
    (`docs/ARCHITECTURE_ROADMAP.md` §2.5):
      1. `n_paired >= min_activations` (default 60 forward-logged activations),
      2. `wilson_lb > 0.50` (one-sided 95% Wilson lower bound beats coin flip),
      3. `p_shuffled < alpha` (beats shuffled-edge placebo null).

    Never self-promotes: returns `status="READY_FOR_DEPT5_DECISION"` when all
    gates pass, `"ACCUMULATING"` below `min_activations`, or
    `"REJECTED_BY_PLACEBO"` otherwise."""
    paired = pair_activations_with_outcomes(
        conn,
        bars_by_ticker,
        horizon_sessions=horizon_sessions,
        adjust_corporate_actions=adjust_corporate_actions,
        actions=actions,
    )
    seed_runs: list[dict] = []
    try:
        for r in conn.execute(
            "SELECT run_at, seeds_json FROM graph_activation_log ORDER BY id ASC"
        ).fetchall():
            seeds = json.loads(r["seeds_json"] or "{}")
            if seeds:
                seed_runs.append({"run_at": r["run_at"], "seeds": seeds})
    except Exception:
        pass

    placebo_res = shuffled_edge_placebo(
        graph,
        seed_runs,
        bars_by_ticker,
        horizon_sessions=horizon_sessions,
        n_shuffles=n_shuffles,
        adjust_corporate_actions=adjust_corporate_actions,
        actions=actions,
        rng=rng,
    )

    n_paired = paired["n_paired"]
    wilson_lb = paired["wilson_lb"]
    p_shuf = placebo_res["p_shuffled"]
    mean_ret = paired["mean_signed_return_pct"] or 0.0

    if n_paired < min_activations:
        status = "ACCUMULATING"
        ready = False
        reason = (
            f"{n_paired}/{min_activations} paired forward activations — "
            f"{min_activations - n_paired} more required before promotion review"
        )
    elif wilson_lb > 0.50 and p_shuf < alpha and mean_ret > 0.0:
        status = "READY_FOR_DEPT5_DECISION"
        ready = True
        reason = (
            f"Cleared all 3 gates (n={n_paired}, Wilson LB={wilson_lb:.2%}, "
            f"p_shuffled={p_shuf:.4f} < {alpha}); awaiting Department-5 ruling"
        )
    else:
        status = "REJECTED_BY_PLACEBO"
        ready = False
        reason = (
            f"Failed significance gate (n={n_paired}, Wilson LB={wilson_lb:.2%}, "
            f"p_shuffled={p_shuf:.4f} vs alpha={alpha})"
        )

    return {
        "status": status,
        "ready_for_dept5": ready,
        "reason": reason,
        "min_activations": min_activations,
        "alpha": alpha,
        "paired": paired,
        "placebo": placebo_res,
    }
