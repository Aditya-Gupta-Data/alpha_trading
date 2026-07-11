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

Honesty rails (the panel): near-zero survivors on thin data is CORRECT,
reported as such — never loosened to produce output. Miners consume only
learnable refs / minable tags (stat_gates exclusions) so the system never
mines its own hypotheses.
"""
