"""
Alpha Trading — Quantitative Execution Bridge (vol_bridge)
==========================================================

Stateless routing module: reads the active knowledge-graph edges from
brain_map.db, classifies the macro regime from the aggregate signed weight
of positive vs negative causal nodes, and translates that regime into iron
condor parameter overrides for build_proposal.

No writes, no cached module-level state, no side effects — the caller
merges the returned dict into its own build_proposal call.  An unavailable
DB, missing table, or any exception returns {} so the proposer runs with
its default parameters unchanged.

Public API
----------
    from src.vol_bridge import compute_regime_overrides
    overrides = compute_regime_overrides(conn=None, mode="scale_risk")
    # Always contains "regime".  Under Expansion also contains either:
    #   risk_pct            float  (scale_risk mode)
    #   short_strike_otm_pct float (widen_wings mode)

Regime classification
---------------------
Each active graph_edges row contributes (polarity × confidence_score) to
a running net_signal, where polarity is derived from the target node name:
  -1 for nodes containing a negative keyword (loss, bearish, crash, …)
  +1 for nodes containing a positive keyword (win, bullish, gain, …)
   0 neutral — source node polarity tried as a fallback structural signal;
               if still 0 the edge is skipped entirely

    net_signal < -EXPANSION_THRESHOLD   → "Expansion"  (negative dominates)
    net_signal >  CONTRACTION_THRESHOLD → "Contraction" (positive dominates)
    otherwise                           → "Neutral"

Under Expansion, two defensive modes are available (caller selects via mode):
  "scale_risk"  (default) — risk_pct = base × RISK_SCALE_FACTOR (0.70)
                            30 % fewer contracts → lower max loss per cycle.
  "widen_wings"           — short_strike_otm_pct = base × PUT_WING_SCALE_FACTOR (1.50)
                            short put moves further OTM, widening the buffer
                            between the sold strike and the protective wing
                            against sudden tail moves.

Decision #38 in DECISIONS.md.
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------------------
# Classification keywords  (matched as substrings, lowercase)
# ---------------------------------------------------------------------------

_NEGATIVE_KEYWORDS: frozenset[str] = frozenset({
    "loss", "bearish", "decline", "crash", "stress", "shock",
    "fail", "warning", "drawdown", "breakdown", "negative", "weak",
    "contraction_risk", "tail_risk",
})

_POSITIVE_KEYWORDS: frozenset[str] = frozenset({
    "win", "bullish", "gain", "recovery", "breakout", "profit",
    "strong", "positive", "growth", "rally", "expansion",
})

# ---------------------------------------------------------------------------
# Regime thresholds and adjustment constants (public for test inspection)
# ---------------------------------------------------------------------------

EXPANSION_THRESHOLD: float = 0.5     # net score < -this  → Expansion
CONTRACTION_THRESHOLD: float = 0.5   # net score > +this  → Contraction

RISK_SCALE_FACTOR: float = 0.70      # 30 % risk reduction (scale_risk mode)
PUT_WING_SCALE_FACTOR: float = 1.50  # 50 % wider put OTM  (widen_wings mode)

BASE_SHORT_STRIKE_OTM_PCT: float = 2.0   # mirrors options_proposer.SHORT_STRIKE_OTM_PCT
BASE_WING_STEPS: int = 4                  # mirrors options_proposer.WING_STEPS


# ---------------------------------------------------------------------------
# Internal helpers (public for direct unit-testing)
# ---------------------------------------------------------------------------

def _node_polarity(name: str) -> int:
    """Return -1 (negative), +1 (positive), or 0 (neutral) for a node name."""
    token = (name or "").lower()
    for kw in _NEGATIVE_KEYWORDS:
        if kw in token:
            return -1
    for kw in _POSITIVE_KEYWORDS:
        if kw in token:
            return 1
    return 0


def _load_active_edges(conn: sqlite3.Connection) -> list[dict]:
    """Return all active graph_edges rows (invalid_at IS NULL) as plain dicts.

    Returns [] when the graph_edges table does not exist yet (the knowledge
    graph has no edges until the Sleep Phase has run) or on any SQLite error.
    """
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "graph_edges" not in tables:
            return []
        rows = conn.execute(
            "SELECT source_node, relation, target_node, confidence_score "
            "FROM graph_edges WHERE invalid_at IS NULL"
        ).fetchall()
        return [
            {
                "source_node":    r["source_node"],
                "relation":       r["relation"],
                "target_node":    r["target_node"],
                "confidence_score": r["confidence_score"],
            }
            for r in rows
        ]
    except sqlite3.Error:
        return []


def _net_signal(edges: list[dict]) -> float:
    """Aggregate signed confidence over all edges.

    Each edge contributes (polarity × confidence_score).  Polarity comes
    from the target node first; if neutral the source node is tried as a
    structural fallback.  Edges with zero polarity on both nodes are skipped.

    Returns:
        value < 0  → negative nodes dominate  (Expansion / stress)
        value > 0  → positive nodes dominate  (Contraction / benign)
        0.0        → balanced or empty graph  (Neutral)
    """
    total = 0.0
    for edge in edges:
        polarity = _node_polarity(edge.get("target_node") or "")
        if polarity == 0:
            polarity = _node_polarity(edge.get("source_node") or "")
        if polarity == 0:
            continue
        weight = float(edge.get("confidence_score") or 1.0)
        total += polarity * weight
    return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_regime(edges: list[dict]) -> str:
    """Classify macro regime from pre-loaded active graph edges.

    Pure function — no I/O.
    Returns "Expansion", "Contraction", or "Neutral".
    """
    signal = _net_signal(edges)
    if signal < -EXPANSION_THRESHOLD:
        return "Expansion"
    if signal > CONTRACTION_THRESHOLD:
        return "Contraction"
    return "Neutral"


def compute_regime_overrides(
    conn: "sqlite3.Connection | None" = None,
    mode: str = "scale_risk",
    base_risk_pct: "float | None" = None,
    base_otm_pct: float = BASE_SHORT_STRIKE_OTM_PCT,
) -> dict:
    """Query the knowledge graph and return parameter overrides.

    Args:
        conn: open sqlite3 connection, or None to open data/brain_map.db.
              Caller owns the connection lifecycle; a temporary connection is
              opened and closed internally only when conn is None.
        mode: "scale_risk" (default) — cut risk_pct by 30 % under Expansion;
              "widen_wings" — widen short_strike_otm_pct instead.
        base_risk_pct: nominal OPTIONS_RISK_PER_TRADE_PCT to scale from.
              Defaults to config.OPTIONS_RISK_PER_TRADE_PCT at call time so
              tests can freely inject any float without touching config.
        base_otm_pct: SHORT_STRIKE_OTM_PCT to scale from (default 2.0).

    Returns:
        Dict always containing "regime" when a DB is reachable.  Under
        Expansion also contains "risk_pct" (scale_risk) or
        "short_strike_otm_pct" (widen_wings).  Returns {} on any error so
        the proposer runs with unchanged defaults (fail-safe).
    """
    if base_risk_pct is None:
        try:
            from src.config import OPTIONS_RISK_PER_TRADE_PCT
            base_risk_pct = OPTIONS_RISK_PER_TRADE_PCT
        except Exception:
            base_risk_pct = 10.0

    owns_conn = conn is None
    if owns_conn:
        try:
            from src import brain_map
            conn = brain_map.connect()
        except Exception:
            return {}

    try:
        edges = _load_active_edges(conn)
        regime = classify_regime(edges)

        if regime == "Expansion":
            if mode == "widen_wings":
                return {
                    "regime": regime,
                    "short_strike_otm_pct": base_otm_pct * PUT_WING_SCALE_FACTOR,
                }
            return {
                "regime": regime,
                "risk_pct": base_risk_pct * RISK_SCALE_FACTOR,
            }
        return {"regime": regime}

    except Exception:
        return {}
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
