"""
src/knowledge_graph/spreading_activation.py — 3D Spreading Activation over the Brain Map
========================================================================================

Pure function over a `GraphEngine` snapshot (or `networkx.DiGraph`). Reads active
edges only; optionally appends one immutable audit row to `graph_activation_log`.

Zero execution authority (ARCHITECTURE_ROADMAP.md §2.4–§2.5):
  * Output is context and ranking for event cards and Department-8 analysis.
  * Never alters position sizing, entry filtering, or risk gates.

Math summary (ARCHITECTURE_ROADMAP.md §2.4):
  1. Seeds:
       a_0(v) = s * m,  where s in {-1, +1} (sentiment) and m in (0, 1] (materiality)
  2. Edge transmission coefficient (u -> v):
       t(u -> v) = w(u -> v) * c(u -> v) * p(u -> v) * eps(u)
       eps(u)    = 1 / (1 + ln(1 + outdeg(u)))
  3. Synchronous path-simple propagation for hop k = 1 .. K (K = 3):
       Each simple path P = (v_0 -> v_1 -> ... -> v_k) contributes:
         contrib(P) = gamma^k * a_0(v_0) * prod_{i=1..k} t(v_{i-1} -> v_i)
       a_k(v) = sum_{P ending at v of length k} contrib(P)
       Pruning at each hop k:
         drop any v with |a_k(v)| < theta (0.02); cap frontier at top 200 by |a_k(v)|.
       Path-simple constraint:
         v_k not in {v_0, ..., v_{k-1}} (no cycle amplification A -> B -> A).
  4. Aggregation:
       E(v) = sum_{k=1..K} a_k(v)   (signed; conflicting paths partially cancel)
       Reports E(v), dominant_hop, and best path `via` (argmax single-path |contrib|).
  5. 3D Momentum M(v, t):
       M(v, t) = (lambda_m ** delta_days) * M(v, t_prev) + E_today(v)
       Ranked by (|M(v, t)|, |E(v)|) descending.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone

from src.knowledge_graph import graphrag

DEFAULT_PARAMS: dict = {
    "gamma": 0.6,
    "max_hops": 3,
    "theta": 0.02,
    "frontier_cap": 200,
    "lambda_m": 0.85,
    "top_k_log": 25,
    "epsilon_form": "1/(1+log(1+outdeg))",
}


def fanout_factor(out_degree: int) -> float:
    """Fan-out normalization eps(u) = 1 / (1 + ln(1 + max(0, outdeg(u) - 1))).
    A non-branching node (outdeg=1) retains 1.0 so 1st/2nd/3rd-order hops on a
    simple chain retain 0.6 / 0.36 / 0.216 of raw signal; branching hubs are
    logarithmically dampened."""
    excess = max(0, int(out_degree) - 1)
    return 1.0 / (1.0 + math.log(1.0 + excess))


def transmission_coeff(graph, u: str, v: str) -> float:
    """Directed edge transmission t(u -> v) = w * c * p * eps(u).
    Polarity 0 transmits nothing (0.0). Missing polarity defaults to +1 on
    legacy edges."""
    if not graph.has_edge(u, v):
        return 0.0
    data = graph[u][v]
    raw_p = data.get("polarity")
    polarity = 1 if raw_p is None else int(raw_p)
    if polarity == 0:
        return 0.0

    # Resolve w (weight) and c (confidence_score):
    # - On GraphRAG edges loaded via GraphEngine, `edge_weight` holds the SQLite
    #   `weight` column and `confidence_score` holds the decayed confidence.
    # - On hand-built networkx graphs in unit tests, `weight` and/or
    #   `confidence_score` may be supplied directly.
    has_conf = "confidence_score" in data and data["confidence_score"] is not None
    has_edge_w = "edge_weight" in data and data["edge_weight"] is not None
    has_w = "weight" in data and data["weight"] is not None

    if has_edge_w and has_conf:
        w = float(data["edge_weight"])
        c = float(data["confidence_score"])
    elif has_w and has_conf and "edge_weight" not in data:
        w = float(data["weight"])
        c = float(data["confidence_score"])
    elif has_conf:
        w = 1.0
        c = float(data["confidence_score"])
    elif has_w:
        w = float(data["weight"])
        c = 1.0
    else:
        w = 1.0
        c = 1.0

    eps = fanout_factor(graph.out_degree(u))
    return w * c * float(polarity) * eps


def graph_snapshot_hash(graph) -> str:
    """Deterministic SHA-256 fingerprint of active graph edges for reproducibility."""
    items = []
    for u, v, data in sorted(graph.edges(data=True), key=lambda e: (str(e[0]), str(e[1]))):
        items.append((
            str(u),
            str(v),
            data.get("relation"),
            round(float(data.get("weight") or 0.0), 6),
            round(float(data.get("confidence_score") or 0.0), 6)
            if data.get("confidence_score") is not None else None,
            data.get("polarity"),
        ))
    raw = json.dumps(items, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_day(iso_str: str | None) -> date | None:
    if not iso_str:
        return None
    try:
        return date.fromisoformat(str(iso_str)[:10])
    except ValueError:
        return None


def _prior_momentum(conn, as_of_day: date | None, lambda_m: float) -> dict[str, float]:
    """Fold prior `graph_activation_log` rows chronologically to recover
    each node's decayed momentum M(v, t_prev) entering `as_of_day`."""
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT run_at, result_json FROM graph_activation_log ORDER BY id ASC"
        ).fetchall()
    except Exception:
        return {}
    if not rows:
        return {}

    # Aggregate E_day(v) per calendar date first, then fold across dates with
    # exponential decay lambda_m ** delta_days.
    daily_energy: dict[date, dict[str, float]] = {}
    for r in rows:
        d = _parse_day(r["run_at"])
        if d is None or (as_of_day is not None and d > as_of_day):
            continue
        try:
            res = json.loads(r["result_json"] or "{}")
        except (ValueError, TypeError):
            continue
        bucket = daily_energy.setdefault(d, {})
        for node_id, info in res.items():
            if isinstance(info, dict) and info.get("energy") is not None:
                bucket[node_id] = bucket.get(node_id, 0.0) + float(info["energy"])

    if not daily_energy:
        return {}

    momentum: dict[str, float] = {}
    prev_day: date | None = None
    for d in sorted(daily_energy):
        if as_of_day is not None and d == as_of_day:
            # Same-day prior runs: decay from previous day first, then add same-day prior energy
            if prev_day is not None:
                delta = max(1, (d - prev_day).days)
                decay = lambda_m ** delta
                momentum = {k: v * decay for k, v in momentum.items()}
                prev_day = d
            for node_id, e_val in daily_energy[d].items():
                momentum[node_id] = momentum.get(node_id, 0.0) + e_val
            continue
        if prev_day is not None:
            delta = max(1, (d - prev_day).days)
            decay = lambda_m ** delta
            momentum = {k: v * decay for k, v in momentum.items()}
        for node_id, e_val in daily_energy[d].items():
            momentum[node_id] = momentum.get(node_id, 0.0) + e_val
        prev_day = d

    if as_of_day is not None and prev_day is not None and prev_day < as_of_day:
        delta = max(1, (as_of_day - prev_day).days)
        decay = lambda_m ** delta
        momentum = {k: v * decay for k, v in momentum.items()}

    return momentum


def _universe_flags(conn, graph, node_ids) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for nid in node_ids:
        if nid in graph.nodes and "in_universe" in graph.nodes[nid]:
            flags[nid] = bool(graph.nodes[nid]["in_universe"])
    if conn is not None:
        missing = [nid for nid in node_ids if nid not in flags]
        if missing:
            try:
                placeholders = ",".join("?" for _ in missing)
                for r in conn.execute(
                    f"SELECT node_id, in_universe FROM graph_nodes WHERE node_id IN ({placeholders})",
                    tuple(missing),
                ).fetchall():
                    flags[r["node_id"]] = bool(r["in_universe"])
            except Exception:
                pass
    return flags


def propagate(
    engine_or_graph,
    seeds: dict[str, float],
    conn=None,
    trigger_ref: str | None = None,
    run_at: str | None = None,
    params: dict | None = None,
    log_activation: bool = True,
) -> dict:
    """Run 3D spreading activation from `seeds` ({node_id: a_0}) over
    `engine_or_graph` (`GraphEngine` or `networkx.DiGraph`).

    Returns:
      {
        "run_at": str,
        "trigger_ref": str | None,
        "seeds": {node_id: float},
        "params": dict (including snapshot_hash),
        "activated": [
          {
            "node_id": str,
            "energy": float,
            "momentum": float,
            "dominant_hop": int,
            "via": list[str],
            "hop_energies": dict[int, float],
            "in_universe": bool,
          }, ...
        ],
        "by_node": {node_id: {...}},
      }
    """
    graph = engine_or_graph if hasattr(engine_or_graph, "edges") else engine_or_graph.graph
    cfg = dict(DEFAULT_PARAMS)
    if params:
        cfg.update(params)

    gamma = float(cfg["gamma"])
    max_hops = int(cfg["max_hops"])
    theta = float(cfg["theta"])
    frontier_cap = int(cfg["frontier_cap"])
    lambda_m = float(cfg["lambda_m"])
    top_k_log = int(cfg["top_k_log"])

    ts = str(run_at) if run_at else datetime.now(timezone.utc).isoformat()
    snap_hash = graph_snapshot_hash(graph)
    full_params = {
        "gamma": gamma,
        "max_hops": max_hops,
        "theta": theta,
        "frontier_cap": frontier_cap,
        "lambda_m": lambda_m,
        "epsilon_form": cfg["epsilon_form"],
        "snapshot_hash": snap_hash,
    }

    clean_seeds = {
        str(k): float(v)
        for k, v in (seeds or {}).items()
        if v is not None and float(v) != 0.0 and str(k) in graph
    }

    # Frontier holds active simple paths from hop k-1:
    # list of (path_tuple, raw_unscaled_product_along_path)
    # where raw_unscaled_product = a_0(v_0) * prod_{i=1..k} t(v_{i-1} -> v_i),
    # and hop-k contribution of a path is (gamma ** k) * raw_unscaled_product.
    frontier: list[tuple[tuple[str, ...], float]] = [
        ((seed_node,), energy0) for seed_node, energy0 in sorted(clean_seeds.items())
    ]

    hop_energies: dict[str, dict[int, float]] = {}
    best_path_for: dict[str, tuple[float, list[str]]] = {}

    for k in range(1, max_hops + 1):
        if not frontier:
            break
        hop_scale = gamma ** k
        candidate_paths_by_target: dict[str, list[tuple[tuple[str, ...], float, float]]] = {}

        for path, raw_prod in frontier:
            u = path[-1]
            if u not in graph:
                continue
            for v in sorted(graph.successors(u), key=str):
                # Path-simple walk: no re-injection into any node already on this path
                if v in path:
                    continue
                t_uv = transmission_coeff(graph, u, v)
                if t_uv == 0.0:
                    continue
                next_raw = raw_prod * t_uv
                contrib = hop_scale * next_raw
                new_path = path + (v,)
                candidate_paths_by_target.setdefault(v, []).append((new_path, next_raw, contrib))

        # Sum a_k(v) across all simple paths reaching v at hop k, then prune (< theta)
        # and cap frontier at top `frontier_cap` nodes by |a_k(v)|.
        hop_totals: list[tuple[str, float]] = []
        for v, plist in candidate_paths_by_target.items():
            ak_v = sum(c for _, _, c in plist)
            if abs(ak_v) < theta:
                continue
            hop_totals.append((v, ak_v))

        # Deterministic rank at hop k: descending |a_k(v)|, tie-break by node_id ascending
        hop_totals.sort(key=lambda item: (-abs(item[1]), str(item[0])))
        surviving_Targets = {v for v, _ in hop_totals[:frontier_cap]}

        next_frontier: list[tuple[tuple[str, ...], float]] = []
        for v, ak_v in hop_totals[:frontier_cap]:
            if v not in surviving_Targets:
                continue
            hop_energies.setdefault(v, {})[k] = ak_v
            for new_path, next_raw, contrib in candidate_paths_by_target[v]:
                next_frontier.append((new_path, next_raw))
                prev_best = best_path_for.get(v)
                if prev_best is None or abs(contrib) > prev_best[0]:
                    best_path_for[v] = (abs(contrib), list(new_path))

        frontier = next_frontier

    # Aggregate E(v) = sum_k a_k(v) across hops 1..K
    as_of_day = _parse_day(ts)
    prior_mom = _prior_momentum(conn, as_of_day, lambda_m)
    univ_map = _universe_flags(conn, graph, list(hop_energies.keys()))

    activated: list[dict] = []
    for v, per_hop in hop_energies.items():
        total_e = sum(per_hop.values())
        if abs(total_e) < 1e-12:
            continue
        dom_hop = max(sorted(per_hop.keys()), key=lambda h: abs(per_hop[h]))
        via_path = best_path_for.get(v, (0.0, [v]))[1]
        mom = prior_mom.get(v, 0.0) + total_e
        activated.append({
            "node_id": v,
            "energy": round(total_e, 6),
            "momentum": round(mom, 6),
            "dominant_hop": dom_hop,
            "via": via_path,
            "hop_energies": {h: round(val, 6) for h, val in sorted(per_hop.items())},
            "in_universe": bool(univ_map.get(v, False)),
        })

    # Rank by |M| then |E|, tie-break by node_id ascending for strict determinism
    activated.sort(key=lambda r: (-abs(r["momentum"]), -abs(r["energy"]), str(r["node_id"])))
    by_node = {r["node_id"]: r for r in activated}

    if conn is not None and log_activation:
        graphrag.ensure_schema(conn)
        top_k_dict = {
            r["node_id"]: {
                "energy": r["energy"],
                "momentum": r["momentum"],
                "hop": r["dominant_hop"],
                "via": r["via"],
                "in_universe": r["in_universe"],
            }
            for r in activated[:top_k_log]
        }
        conn.execute(
            "INSERT INTO graph_activation_log (run_at, trigger_ref, seeds_json, params_json, result_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ts,
                trigger_ref,
                json.dumps(clean_seeds, sort_keys=True),
                json.dumps(full_params, sort_keys=True),
                json.dumps(top_k_dict, sort_keys=True),
            ),
        )
        conn.commit()

    return {
        "run_at": ts,
        "trigger_ref": trigger_ref,
        "seeds": clean_seeds,
        "params": full_params,
        "activated": activated,
        "by_node": by_node,
    }


def seeds_from_event(exposures: list[dict], materials: list[dict], conn=None) -> dict[str, float]:
    """Build seed energies a_0(v) = s * m from `event_engine` exposures and
    materiality assessments.
      * s in {-1, +1} from exposure direction ('bullish'/'positive' -> +1,
        'bearish'/'negative' -> -1)
      * m in (0, 1] scaled from materiality effect_pct (clamped to [0.1, 1.0],
        with 10% revenue effect saturating at 1.0).
    """
    by_ticker = {m.get("ticker"): m for m in (materials or []) if isinstance(m, dict)}
    seeds: dict[str, float] = {}
    for e in exposures or []:
        if not isinstance(e, dict):
            continue
        raw_t = str(e.get("ticker") or "").strip()
        if not raw_t:
            continue
        direction = str(e.get("direction") or "").strip().lower()
        if direction in ("bullish", "positive", "long", "+1", "1"):
            s = 1.0
        elif direction in ("bearish", "negative", "short", "-1"):
            s = -1.0
        else:
            continue
        mat = by_ticker.get(raw_t) or {}
        eff = mat.get("effect_pct")
        try:
            m = max(0.1, min(1.0, abs(float(eff)) / 10.0)) if eff is not None else 0.5
        except (TypeError, ValueError):
            m = 0.5
        node_id = raw_t.upper().removesuffix(".NS")
        if conn is not None:
            resolved = graphrag.resolve_node(conn, raw_t, declared_type="company")
            if resolved:
                node_id = resolved["node_id"]
        seeds[node_id] = round(s * m, 4)
    return seeds


def format_second_order_line(activation_result: dict, top_n: int = 5) -> str:
    """Format a compact read-only 'second/third-order read' line for an event card:
    top-N activated nodes with signed energy, dominant hop, and `[UNIVERSE]` tag."""
    rows = (activation_result or {}).get("activated") or []
    if not rows:
        return ""
    parts = []
    for r in rows[:top_n]:
        tag = " [UNIVERSE]" if r.get("in_universe") else ""
        parts.append(f"{r['node_id']}{tag} ({r['energy']:+.2f}, hop {r['dominant_hop']})")
    return "2nd/3rd-order read: " + ", ".join(parts)
