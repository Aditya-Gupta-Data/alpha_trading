# Opportunity Cost Tracking — Directive 1 design (2026-07-27)

**Owner directive:** when a gate blocks a trade, route it into the EXISTING
`shadow_trades` table via the existing machinery, mode-tagged so it can never
contaminate the pattern-learning corpus, and resolvable so we can see whether
the gates save or cost money. **NO NEW DATABASE OR ENGINE.**

## The architectural constraint that shapes everything: a shadow needs a host

Verified 07-27 by reading the resolver. `discovery.shadow_runner.resolve_from_outcomes`
is a single SQL join:

```sql
FROM shadow_trades s JOIN outcomes o ON o.journal_ref = s.host_ref
WHERE s.resolved = 0 AND s.host_ref IS NOT NULL
```

A shadow is resolved by **inheriting a real resolved trade's outcome**. There
is no independent price math anywhere in that path — deliberately ("one
resolver, no parallel price arithmetic", `trial.py:65`).

So the honest question for every gate is: **when this gate blocks, does a real
trade exist whose outcome answers the counterfactual?**

| Gate | Blocks because | Is there a host? |
|---|---|---|
| **Exposure gate** (#68) | a position on the SAME underlying+direction is already open | **YES** — the conflicting position. `gate_entry` already computes it (`conflicts` → `trade_id`), and `positions.trade_id` IS `outcomes.journal_ref` (both are the journal `short_id`). |
| Risk-of-ruin halt | account drawdown ≥10% | No — nothing traded |
| Daily 3% breaker | today's realized loss | No |
| Margin exhaustion | insufficient liquid cash | No |
| Adaptive-sizing veto (#81) | the setup's own losing record | No |
| Equity-desk funding | desk budget/ruin | No |

**This build wires the exposure gate — the one block type with a genuine
host — and deliberately does NOT fabricate outcomes for the rest.**

### Why the hostless gates are not "resolved" (and why that is the honest call)

To resolve a ruin-halt-blocked spread we would have to price a hypothetical
position forward with the synthetic-chain model. `HANDOVER.md` open item 5
records that this model is **inflated roughly 10x, in the known 62–79%
generosity band**. Feeding that into "did the gate cost us money?" would
systematically inflate the blocked trades' imagined profits and make every
risk gate look far more expensive than it is — an argument for switching the
safety machinery off, manufactured by a known-biased model.

Those blocks are already recorded where they belong: `logs/exposure_blocks.jsonl`,
`account_events` (halts/margin), `logs/sizing_adjustments.jsonl` (vetoes).
They are counted, not silently dropped — they are just never given a fake P&L.

## What gets built (all reuse)

1. **`mode` column on `shadow_trades`** — additive `ALTER TABLE` in
   `trial.ensure_schema`, exactly the in-place upgrade pattern `host_ref`
   already used (#25 discipline). `NULL` = legacy pattern shadow.
   `"BLOCKED_BY_RISK"` = an opportunity-cost row.
2. **`trial.record_block(...)`** — sits beside `record_shadow_fire`, writes
   the same table, sets `mode` + `host_ref` in one statement. Ref namespace
   **`blocked:`**, added to `stat_gates.EXCLUDED_REF_PREFIXES` (the ONE
   exclusion authority) so every existing corpus filter drops it for free.
3. **The seam** — `exposure_gate.gate_entry` records a block right where it
   already ledgers one. Fail-open and side-effect-free by the gate's own hard
   rule: a broken ledger write can never change the verdict.
4. **Resolution — ZERO new code.** Blocked rows carry `host_ref`, so the
   existing Sleep-Phase Task I sweep resolves them the night the blocking
   position resolves. This is the whole point of choosing the exposure gate.
5. **`opportunity_cost.report()`** — a read over resolved `BLOCKED_BY_RISK`
   rows, reusing the Forward-Scoreboard reporting shape.

## The isolation guarantee (three independent layers, all tested)

1. **Ref prefix** — `blocked:` in `EXCLUDED_REF_PREFIXES` ⇒
   `is_learnable_ref` is False ⇒ `learning_corpus_filter` drops it ⇒ no
   tuner/skeptic/miner can consume it.
2. **Namespaced `pattern_id`** (`blocked:exposure_gate`) ⇒ no real pattern's
   `shadow_evidence(pattern_id=…)` query can ever match a blocked row.
3. **Explicit `mode` filter** in `shadow_evidence` ⇒ even if a future caller
   reused a real `pattern_id` by mistake, the row is still excluded from
   pattern evidence. Belt, braces, and a third belt — because a contaminated
   corpus is silent and permanent.

## What the resulting number means (and does not)

The blocked duplicate inherits the **blocking position's** outcome. Same
underlying, same direction, overlapping window — strongly correlated, but a
different strike structure, so the inherited `r_multiple` is a **proxy, not
the blocked trade's true R**. The report says so on its face.

Read it as: *when we refused a duplicate, how did the exposure we already
held perform?* Hosts mostly winning ⇒ the gate is costing us second winners.
Hosts mostly losing ⇒ it is saving us second losers.

**What it does NOT measure:** decision #68's actual purpose was structural —
capping concentration so two correlated losers can't compound into one
drawdown. A gate can be "costing" in expectancy and still be correct on
variance. This number is one input to that judgement, never the verdict.
