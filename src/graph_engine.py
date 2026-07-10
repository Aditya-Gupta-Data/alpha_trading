"""
Alpha Trading — Phase 6C: the Knowledge Graph reasoning layer
=============================================================

A READ-ONLY, memory-resident reasoning layer over the Brain Map. The
relational store (src/brain_map.py) records *what happened*; this layer
lets the AI Analyst ask *what is this connected to* — "when NIFTY 50 was in
this regime before, what themes and outcomes were linked to it?" — by
walking a directed graph of causal links.

Persistence stays exactly where the rest of the memory lives: one more
additive table in data/brain_map.db, `graph_edges`:

    source_node       TEXT   e.g. a ticker ("NIFTY 50"), a regime/theme tag
    relation          TEXT   the predicate ("led_to", "co_occurred_with", …)
    target_node       TEXT
    confidence_score  REAL   0..1 edge weight — decays over time
    context           TEXT   optional qualifying condition ("VIX > 20")
    valid_from        TEXT   ISO datetime this edge was created/last reinforced
    invalid_at        TEXT   ISO datetime decay expired this edge (NULL = active)
    decay_lambda      REAL   per-edge exponential decay rate (days⁻¹, default 0.05)

The edges are written by the off-market Sleep Phase (src/sleep_phase.py)
as it distils causal links out of the day's episodic events. src/decay_engine.py
applies the daily exponential decay sweep: w(t) = w₀·exp(−λ·t). Edges
below confidence 0.1 are marked via invalid_at and excluded from inference.

This module only READS active edges: at construction it loads every row
where invalid_at IS NULL into a networkx.DiGraph and answers queries purely
from memory — no DB access during inference, no writes anywhere, ever
(decision #33). networkx is the in-memory reasoning layer; SQLite remains
the only persistent store (no new database — the Phase 6C strict constraint).

STRICTLY ADDITIVE, same discipline as brain_map/sleep_phase: `brain_map.py`
is untouched, and an empty/missing `graph_edges` table degrades to an empty
graph so every caller — most importantly the live options proposer — keeps
working with no behavior change until edges exist.

    from src.graph_engine import GraphEngine
    eng = GraphEngine()                       # loads data/brain_map.db edges
    ctx = eng.get_relevant_context("NIFTY 50")
"""

from datetime import datetime, timezone

from src import brain_map

# Default decay rate matching sleep_phase.DEFAULT_DECAY_LAMBDA.
_DEFAULT_DECAY_LAMBDA = 0.05

# Owned by this module — additive to brain_map's core tables, same .db file.
# `context` (nullable) preserves a causal link's qualifying condition, e.g.
# "iron_condor RESULTS_IN loss" with context "VIX > 20" (Phase 6D). The
# UNIQUE index makes edge writes idempotent per (subject, predicate, object).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_edges (
    source_node       TEXT NOT NULL,
    relation          TEXT NOT NULL,
    target_node       TEXT NOT NULL,
    confidence_score  REAL,
    context           TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges (source_node);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_edges_triple
    ON graph_edges (source_node, relation, target_node);
"""


def ensure_schema(conn) -> None:
    """Create the graph_edges table/indexes if absent (idempotent), and
    upgrade existing tables in place by adding any missing columns.
    Handles the pre-Phase-6D `context` column and the Phase 6E temporal
    decay columns (valid_from, invalid_at, decay_lambda)."""
    conn.executescript(_SCHEMA)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(graph_edges)")}
    if "context" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN context TEXT")
    if "valid_from" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN valid_from TEXT")
    if "invalid_at" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN invalid_at TEXT")
    if "decay_lambda" not in cols:
        conn.execute("ALTER TABLE graph_edges ADD COLUMN decay_lambda REAL")
    if "source" not in cols:
        # Provenance firewall (holy-grail plan §5.6): WHO wrote an edge
        # decides what it may DO — only outcome_derived edges may ever feed
        # vol_bridge's sizing signal; affinity projections and future miner
        # edges are context, never risk. Backfill is deterministic: the
        # only non-causal writer to date is entity_affinity's
        # concentrates_in relation; everything else came from the Task-D
        # causal writer / edge_miner (reviewed outcomes, decision #34).
        conn.execute("ALTER TABLE graph_edges ADD COLUMN source TEXT")
        conn.execute("UPDATE graph_edges SET source = 'affinity_projected' "
                     "WHERE relation = 'concentrates_in' AND source IS NULL")
        conn.execute("UPDATE graph_edges SET source = 'outcome_derived' "
                     "WHERE source IS NULL")
    conn.commit()


def add_edge(conn, source_node, relation, target_node,
             confidence_score=None, context=None, decay_lambda=None,
             valid_from=None, source: str = "outcome_derived") -> None:
    """Write (or reinforce) one causal link. Idempotent on the
    (source, relation, target) triple: re-writing the same edge UPDATES its
    confidence/context instead of duplicating, so repeated Sleep-Phase runs
    never grow the graph unbounded.

    On every write — new or reinforce — valid_from is reset (default: now)
    so the decay clock restarts (a re-observed pattern is fresh again), and
    invalid_at is cleared so a previously-expired edge reactivates.

    `decay_lambda` overrides the edge's decay rate when given: 0.0 makes
    the edge DECAY-EXEMPT (loss-permanence — a lesson paid for with a loss
    must not fade out of the active graph just because it isn't re-observed;
    winners re-reinforce themselves, losses don't). An explicit value wins
    over whatever the edge had; None keeps the existing rate (or the
    default on first write).

    `valid_from` (ISO date/datetime string) stamps the decay clock's anchor
    explicitly — the HISTORICAL-BACKFILL seam: a 2023 deal replayed today
    must age from 2023, not read as born-today (else decay_engine treats a
    long-dead affinity as maximally fresh). Forward/live writes leave it
    None and get now. Not called during inference — this is the writer
    seam."""
    ensure_schema(conn)
    now_str = (str(valid_from) if valid_from
               else datetime.now(timezone.utc).isoformat())
    if decay_lambda is None:
        # Preserve any existing per-edge rate; default only on first write.
        lambda_sql = "COALESCE(graph_edges.decay_lambda, excluded.decay_lambda)"
        lambda_val = _DEFAULT_DECAY_LAMBDA
    else:
        # Explicit rate wins — including 0.0 (decay-exempt). Once an edge
        # has been marked exempt, a later None-write keeps it exempt via
        # the COALESCE branch above.
        lambda_sql = "excluded.decay_lambda"
        lambda_val = float(decay_lambda)
    conn.execute(
        "INSERT INTO graph_edges (source_node, relation, target_node, "
        "confidence_score, context, valid_from, decay_lambda, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source_node, relation, target_node) DO UPDATE SET "
        "confidence_score = excluded.confidence_score, "
        "context = COALESCE(excluded.context, graph_edges.context), "
        "valid_from = excluded.valid_from, "
        f"decay_lambda = {lambda_sql}, "
        "source = COALESCE(excluded.source, graph_edges.source), "
        "invalid_at = NULL",
        (source_node, relation, target_node, confidence_score, context,
         now_str, lambda_val, source or "outcome_derived"),
    )
    conn.commit()


class GraphEngine:
    """Loads all `graph_edges` into a networkx.DiGraph once, then answers
    read-only traversal queries entirely from that in-memory graph."""

    def __init__(self, db_path=None, conn=None):
        """Build the in-memory graph from the Brain Map's edges. Pass an
        open `conn` (e.g. an in-:memory: test DB) to load from it directly;
        otherwise a short-lived connection to `db_path` (default the real
        data/brain_map.db) is opened, read, and closed."""
        import networkx as nx

        self.graph = nx.DiGraph()
        owns_conn = conn is None
        if conn is None:
            conn = brain_map.connect(db_path)
        try:
            ensure_schema(conn)
            for row in conn.execute(
                "SELECT source_node, relation, target_node, confidence_score, "
                "context FROM graph_edges WHERE invalid_at IS NULL"
            ):
                src, relation, tgt, score, context = (
                    row["source_node"], row["relation"], row["target_node"],
                    row["confidence_score"], row["context"],
                )
                # A DiGraph keeps one edge per (src, tgt); if the store holds
                # duplicates, the strongest link wins so traversal ranks it.
                weight = 0.0 if score is None else float(score)
                if self.graph.has_edge(src, tgt) and \
                        self.graph[src][tgt].get("weight", 0.0) >= weight:
                    continue
                self.graph.add_edge(src, tgt, relation=relation,
                                    confidence_score=score, weight=weight,
                                    context=context)
        finally:
            if owns_conn:
                conn.close()

    def get_relevant_context(self, current_node, max_hops: int = 2) -> list:
        """Breadth-first walk out from `current_node` to depth `max_hops`
        (default 2), returning every linked edge along the way as:

            {"source", "relation", "target", "confidence_score", "hops"}

        sorted by `confidence_score` (highest first; unknown/None weights
        sort last). Returns [] for an unknown node or an empty graph — the
        engine never raises during inference, so callers can wire it in
        without adding a failure mode."""
        g = self.graph
        if current_node not in g or max_hops < 1:
            return []

        results = []
        seen_edges = set()
        # Standard BFS over out-edges, tracking depth so we stop at max_hops.
        frontier = [current_node]
        visited_nodes = {current_node}
        for depth in range(1, max_hops + 1):
            next_frontier = []
            for node in frontier:
                for target in g.successors(node):
                    edge_key = (node, target)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        data = g[node][target]
                        results.append({
                            "source": node,
                            "relation": data.get("relation"),
                            "target": target,
                            "confidence_score": data.get("confidence_score"),
                            "context": data.get("context"),
                            "hops": depth,
                        })
                    if target not in visited_nodes:
                        visited_nodes.add(target)
                        next_frontier.append(target)
            frontier = next_frontier
            if not frontier:
                break

        results.sort(
            key=lambda e: (e["confidence_score"] is None,
                           -(e["confidence_score"] or 0.0)))
        return results

    def summarize_context(self, current_node, max_hops: int = 2,
                          limit: int = 5) -> str:
        """A compact, human/LLM-readable rendering of get_relevant_context,
        or "" when there's nothing linked — ready to drop straight into a
        trade rationale or an LLM prompt."""
        edges = self.get_relevant_context(current_node, max_hops=max_hops)
        if not edges:
            return ""
        lines = []
        for e in edges[:limit]:
            score = e["confidence_score"]
            conf = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
            rel = (e["relation"] or "linked_to").replace("_", " ")
            cond = f" when {e['context']}" if e.get("context") else ""
            lines.append(f"{e['source']} —{rel}→ {e['target']}{cond} "
                         f"(confidence {conf}, {e['hops']} hop)")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    node = sys.argv[1] if len(sys.argv) > 1 else "NIFTY 50"
    engine = GraphEngine()
    print(f"Knowledge graph: {engine.graph.number_of_nodes()} node(s), "
          f"{engine.graph.number_of_edges()} edge(s).")
    ctx = engine.get_relevant_context(node)
    if not ctx:
        print(f"No linked context for {node!r} "
              "(graph_edges is empty until the Sleep Phase populates it).")
    else:
        print(f"2-hop context for {node!r}:")
        for e in ctx:
            print(f"  {e}")
