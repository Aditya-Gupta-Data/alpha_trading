"""
src/validation — the proving harness (Phase 2+ of docs/HOLY_GRAIL_PLAN.md).

The wall between the discovery brain and anything the human sees or the
engine acts on. Nothing in this package fetches market data, trades, or
writes trade state — it verifies.

  timelock   the as-of contract: any discovery-facing computation must be
             FUTURE-BLIND — mutate everything dated after its as_of and
             the output must not change. Enforced by tests the same way
             decision #30's import guards enforce no-LLM-in-price-loop.
"""
