"""
src/knowledge_graph — Phase 7 cross-referencing layer (scratchpad build).

Where structured macro/news signals (src/ingestion) meet the live paper
book: the resonance engine reads open positions from data/journal.jsonl
(pure file stream) and historical context from brain_map.db strictly in
SQLite read-only mode (mode=ro), so nothing in this package can ever
contend for a write lock with the main execution loop — and nothing here
places, modifies, or auto-exits a trade. Output is ADVISORY payloads
only; the human (or the existing decide_pending path) stays in charge.

  resonance        cross-refs incoming flow against open positions →
                   CONFLICT / RESONANCE / NEUTRAL advisories.
  entity_affinity  Phase 8: accumulates the raw bulk/block-deal history
                   into an entity↔promoter-group affinity graph (who
                   concentrates where, from public disclosures only), and
                   reads linked entities' recent net direction as
                   DISTRIBUTION / ACCUMULATION advisories. Writes its
                   affinity tables (in brain_map.db) + data/entity_affinity.json;
                   proposes no trades, not wired into scoring.
"""
