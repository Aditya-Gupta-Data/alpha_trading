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
  * **AltData edge-class court (bottom of file):** grades each alt-data
    relation on its dated activations; `promote_edge_class` flips a GRADUATED
    class to `outcome_derived` only when `scripts/run_proving_court.py
    --promote` is run by a person.
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


# ---------------------------------------------------------------------------
# AltData edge-class court: each relation written by src/analysis/altdata_graph
# (WON_CONTRACT, INPUT_COST_PRESSURE, VEHICLE_SALES_MOMENTUM) is graded as one
# class on its dated activations (`altdata_momentum_modifiers`).
# ---------------------------------------------------------------------------

ALTDATA_SOURCE = "altdata_telemetry"
ALTDATA_EXTRACTOR = "altdata_graph"
PROMOTED_SOURCE = "outcome_derived"
DEFAULT_PLACEBO_DRAWS = 1000
MIN_PLACEBO_POOL = 5

_PROMOTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS altdata_class_promotions (
    relation        TEXT PRIMARY KEY,
    promoted_at     TEXT NOT NULL,
    edges_promoted  INTEGER NOT NULL,
    verdict_json    TEXT NOT NULL
);
"""


def _bare_symbol(ticker) -> str:
    return str(ticker or "").upper().removesuffix(".NS").removesuffix(".BO").strip()


def _bar_day(bar: dict) -> str:
    return str(bar.get("session") or bar.get("date") or "")[:10]


def load_altdata_activations(conn) -> list[dict]:
    """Dated, signed alt-data activations: one per non-zero momentum modifier
    whose (ticker, relation) still has an alt-data edge in `graph_edges`."""
    try:
        rows = conn.execute(
            "SELECT m.date, m.ticker, m.signal_type, m.momentum_modifier "
            "FROM altdata_momentum_modifiers m "
            "WHERE EXISTS (SELECT 1 FROM graph_edges e "
            "  WHERE e.relation = m.signal_type AND e.extractor = ? "
            "  AND (e.source_node = m.ticker OR e.target_node = m.ticker)) "
            "ORDER BY m.date, m.ticker, m.signal_type",
            (ALTDATA_EXTRACTOR,),
        ).fetchall()
    except Exception:
        return []
    out = []
    for d, ticker, relation, modifier in rows:
        m = float(modifier or 0)
        sign = (m > 0) - (m < 0)
        if sign and _bare_symbol(ticker):
            out.append({"date": str(d)[:10], "ticker": _bare_symbol(ticker),
                        "relation": relation, "sign": sign})
    return out


def forward_entry(bars: list[dict], signal_day: str,
                  horizon_sessions: int = DEFAULT_HORIZON_SESSIONS):
    """(entry_index, return_pct) entering at the open of the first session
    AFTER `signal_day` (the data lands overnight, decision #65) and exiting at
    the close `horizon_sessions` sessions later counting the entry session.
    None when the window is not complete or the open is missing."""
    if not bars or horizon_sessions < 1:
        return None
    day = str(signal_day)[:10]
    idx = next((i for i, b in enumerate(bars) if _bar_day(b) > day), None)
    if idx is None or idx + horizon_sessions - 1 >= len(bars):
        return None
    entry = bars[idx].get("open")
    exit_ = bars[idx + horizon_sessions - 1].get("close")
    if not entry or exit_ is None or float(entry) <= 0:
        return None
    return idx, (float(exit_) - float(entry)) / float(entry) * 100.0


def _class_stats(signed_returns: list[float], horizon_sessions: int) -> dict:
    n = len(signed_returns)
    wins = sum(1 for r in signed_returns if r > 0)
    mean = sum(signed_returns) / n if n else None
    sharpe = None
    if n >= 2:
        var = sum((r - mean) ** 2 for r in signed_returns) / (n - 1)
        if var > 0:
            sharpe = mean / math.sqrt(var) * math.sqrt(252.0 / horizon_sessions)
    return {
        "n": n,
        "wins": wins,
        "hit_rate": wins / n if n else None,
        "wilson_lb": stat_gates.wilson_lower_bound(wins, n),
        "mean_signed_return_pct": mean,
        "sharpe_annualised": sharpe,
    }


def pair_altdata_activations_with_outcomes(
    activations: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
) -> dict[str, dict]:
    """Per relation: signed forward returns (sign × return) for activations
    whose windows do not overlap an earlier counted one on the same ticker,
    so one persistent signal is never counted once per day."""
    bars = {_bare_symbol(k): v for k, v in (bars_by_ticker or {}).items()}
    out: dict[str, dict] = {}
    next_free: dict[tuple, int] = {}
    for act in sorted(activations, key=lambda a: (a["date"], a["ticker"])):
        rel = act["relation"]
        cls = out.setdefault(rel, {"pairs": [], "skipped_overlap": 0,
                                   "skipped_no_bars": 0})
        ticker = _bare_symbol(act["ticker"])
        fwd = forward_entry(bars.get(ticker, []), act["date"], horizon_sessions)
        if fwd is None:
            cls["skipped_no_bars"] += 1
            continue
        idx, ret = fwd
        key = (rel, ticker)
        if idx < next_free.get(key, -1):
            cls["skipped_overlap"] += 1
            continue
        next_free[key] = idx + horizon_sessions
        cls["pairs"].append({"date": act["date"], "ticker": ticker,
                             "sign": act["sign"], "return_pct": ret,
                             "signed_return_pct": act["sign"] * ret})
    for cls in out.values():
        cls.update(_class_stats([p["signed_return_pct"] for p in cls["pairs"]],
                                horizon_sessions))
    return out


def altdata_ticker_placebo(
    pairs: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    pool: list[str],
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    n_shuffles: int = DEFAULT_PLACEBO_DRAWS,
    seed: int = 0,
) -> dict:
    """Shuffled-ticker placebo: keep every activation's date and sign, give it
    a random ticker from `pool`, and re-score. p = (1 + #placebo hit rate ≥
    real) / (1 + draws). Market drift on those dates is in both, so only the
    edge-to-ticker mapping is under test."""
    bars = {_bare_symbol(k): v for k, v in (bars_by_ticker or {}).items()}
    pool = sorted({_bare_symbol(t) for t in pool if bars.get(_bare_symbol(t))})
    real_n = len(pairs)
    real_hit = (sum(1 for p in pairs if p["signed_return_pct"] > 0) / real_n
                if real_n else None)
    if real_n == 0 or len(pool) < MIN_PLACEBO_POOL:
        return {"p_value": None, "pool_size": len(pool), "draws": 0,
                "real_hit_rate": real_hit, "placebo_mean_hit_rate": None,
                "reason": "empty_class" if real_n == 0 else "placebo_pool_too_small"}
    options = []
    for p in pairs:
        rets = [p["sign"] * fwd[1] for t in pool
                if (fwd := forward_entry(bars[t], p["date"], horizon_sessions))]
        options.append(rets)
    rng = random.Random(seed)
    at_least, hits = 0, []
    for _ in range(n_shuffles):
        drawn = [rng.choice(o) for o in options if o]
        h = sum(1 for r in drawn if r > 0) / len(drawn) if drawn else 0.0
        hits.append(h)
        if h >= real_hit:
            at_least += 1
    return {"p_value": (1 + at_least) / (1 + n_shuffles), "pool_size": len(pool),
            "draws": n_shuffles, "real_hit_rate": real_hit,
            "placebo_mean_hit_rate": sum(hits) / len(hits), "reason": None}


def evaluate_altdata_edge_classes(
    activations: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    min_activations: int = MIN_FORWARD_ACTIVATIONS,
    alpha: float = DEFAULT_ALPHA,
    n_shuffles: int = DEFAULT_PLACEBO_DRAWS,
    seed: int = 0,
) -> dict[str, dict]:
    """Verdict per relation: ACCUMULATING (n < min), GRADUATES (n ≥ min,
    Wilson LB > 0.50, mean > 0, placebo p < alpha / classes tested), or
    REJECTED. Alpha is Bonferroni-split across the classes that reached n."""
    classes = pair_altdata_activations_with_outcomes(
        activations, bars_by_ticker, horizon_sessions)
    pool = sorted({_bare_symbol(a["ticker"]) for a in activations})
    tested = [r for r, c in classes.items() if c["n"] >= min_activations]
    alpha_eff = alpha / len(tested) if tested else alpha
    verdicts = {}
    for rel, cls in sorted(classes.items()):
        v = {k: cls[k] for k in ("n", "wins", "hit_rate", "wilson_lb",
                                 "mean_signed_return_pct", "sharpe_annualised",
                                 "skipped_overlap", "skipped_no_bars")}
        v.update(relation=rel, horizon_sessions=horizon_sessions,
                 min_activations=min_activations, alpha=alpha,
                 alpha_bonferroni=alpha_eff, placebo=None)
        if cls["n"] < min_activations:
            v.update(status="ACCUMULATING", graduates=False,
                     reason=f"{cls['n']}/{min_activations} non-overlapping activations")
        else:
            plc = altdata_ticker_placebo(cls["pairs"], bars_by_ticker, pool,
                                         horizon_sessions, n_shuffles, seed)
            v["placebo"] = plc
            p = plc["p_value"]
            fails = []
            if cls["wilson_lb"] <= 0.50:
                fails.append(f"Wilson LB {cls['wilson_lb']:.3f} <= 0.50")
            if (cls["mean_signed_return_pct"] or 0) <= 0:
                fails.append("mean signed return <= 0")
            if p is None:
                fails.append(f"placebo not run ({plc['reason']})")
            elif p >= alpha_eff:
                fails.append(f"placebo p {p:.4f} >= {alpha_eff:.4f}")
            v.update(status="REJECTED" if fails else "GRADUATES",
                     graduates=not fails,
                     reason="; ".join(fails) or "all gates passed")
        verdicts[rel] = v
    return verdicts


def ensure_promotions_schema(conn) -> None:
    conn.executescript(_PROMOTIONS_SCHEMA)


def promoted_relations(conn) -> set[str]:
    """Relations the court has promoted (empty when the table is absent)."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT relation FROM altdata_class_promotions")}
    except Exception:
        return set()


def promote_edge_class(conn, verdict: dict, now_iso: str) -> int:
    """Flip every alt-data edge of a GRADUATED relation to `outcome_derived`
    and record the verdict. The `extractor` column keeps 'altdata_graph', so
    the edge's origin stays visible. Returns edges changed."""
    if not verdict.get("graduates"):
        raise ValueError(f"{verdict.get('relation')} has not graduated")
    ensure_promotions_schema(conn)
    rel = verdict["relation"]
    cur = conn.execute(
        "UPDATE graph_edges SET source = ? "
        "WHERE relation = ? AND extractor = ? AND source = ?",
        (PROMOTED_SOURCE, rel, ALTDATA_EXTRACTOR, ALTDATA_SOURCE))
    changed = cur.rowcount
    conn.execute(
        "INSERT INTO altdata_class_promotions (relation, promoted_at, "
        "edges_promoted, verdict_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (relation) DO UPDATE SET promoted_at = excluded.promoted_at, "
        "edges_promoted = excluded.edges_promoted, verdict_json = excluded.verdict_json",
        (rel, now_iso, changed, json.dumps(verdict, sort_keys=True, default=str)))
    conn.commit()
    return changed


def revoke_edge_class(conn, relation: str) -> int:
    """Undo a promotion: the relation's alt-data edges go back to
    `altdata_telemetry` and its promotion row is removed."""
    ensure_promotions_schema(conn)
    cur = conn.execute(
        "UPDATE graph_edges SET source = ? "
        "WHERE relation = ? AND extractor = ? AND source = ?",
        (ALTDATA_SOURCE, relation, ALTDATA_EXTRACTOR, PROMOTED_SOURCE))
    conn.execute("DELETE FROM altdata_class_promotions WHERE relation = ?",
                 (relation,))
    conn.commit()
    return cur.rowcount
