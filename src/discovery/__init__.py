"""
src/discovery — the pattern miners (Phase 5 of docs/HOLY_GRAIL_PLAN.md).

Where the brain stops only checking hand-coded patterns and starts finding
its own. Miners ENUMERATE candidate patterns from accumulated history and
REGISTER them (src/validation/registry.py) — they never surface anything
themselves. Everything a miner mints is a CANDIDATE that must survive the
proving harness (trial -> validation -> drift monitoring) before any card
cites it.

  cooccurrence_miner  frequent tag-itemsets over outcomes x daily_context,
                      FDR-controlled, real and simulated corpora mined
                      SEPARATELY, stratified base rates so it can't
                      rediscover the pipeline's own gates.
  sequence_miner      the same core lifted onto a time axis: LAGGED
                      antecedent tags (market state k trading-days BEFORE a
                      trade's entry) — the "early tell precedes the move"
                      shape both owner theses (H1/H2) turn on. No look-ahead
                      by construction (timelock-tested).
  run_miners          the manual discovery-pass orchestrator: every miner
                      × both corpora, one honest combined report.
  strategy_evidence   the "check WHICH strategy" view — per-structure
                      Wilson-bounded win-rates for a pattern, real/sim kept
                      apart, a ≥5-real render floor, and a descriptive
                      PREFER/ABSTAIN decided on the honest lower bound. The
                      duel's read-only substrate, never the duel.

Honesty rails (the panel): near-zero survivors on thin data is CORRECT,
reported as such — never loosened to produce output. Miners consume only
learnable refs / minable tags (stat_gates exclusions) so the system never
mines its own hypotheses.
"""
