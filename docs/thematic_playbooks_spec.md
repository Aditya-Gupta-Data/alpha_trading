# Thematic Playbooks — Design Spec (PLANNING ONLY)

> **Status: conceptual blueprint, locked 2026-07-09. NO code exists yet.**
> Build deferred until after the observation week. This document is the
> agreed architecture to build *from*, not a record of anything built.
> It deliberately reuses the existing engine's seams rather than adding a
> parallel stack — every "new" piece below names the current module it
> extends.

## 1. The Concept

Evolve the engine from **reactive daily trading** (each day, is there a
setup right now?) to **predictive multi-month thematic tracking** (which
macro cycle is turning, and how do we stage into it?).

Canonical example: the rotation from an **"AI Hype Capex"** narrative
into a **"Cost-Optimized Indian IT"** narrative — capturing the moment
the market stops paying for AI spend and starts rewarding IT-services
margin discipline. A *playbook* is the reusable object that watches for
one such hype→reality turn and runs a staged paper campaign when it
comes.

Non-negotiables inherited from the rest of the system:
- **Paper only.** No broker path exists anywhere; a playbook proposes,
  the human approves (decision #11). A campaign is a *sequence of
  proposals*, each still gated and approved individually.
- **Advisory, human-gated, reversible.** A playbook never auto-fires a
  trade; it arms, confirms, and *proposes* — exactly the pending-approval
  flow already in place.
- **The dual-machine split holds** (decisions #47/#48): fundamental
  narrative tracking is Mac-side (local Ollama, zero API spend);
  technical confirmation is VM-side (the live engine with the Dhan
  token). Neither half alone can open a campaign.

## 2. The Dual-Trigger "AND" Gate

A campaign arms only when **both** independent triggers agree. Either
alone is explicitly insufficient — this is the core discipline (a
narrative without price confirmation is a thesis, not a trade; price
without narrative is noise-chasing).

### Trigger A — Fundamental Primer (Mac-side, `sleep_phase.py`)
- Extends the existing off-market sleep-phase pass (Task A/B ingestion +
  consolidation already run local Ollama over text).
- Tracks a playbook's **thematic keyword sets** over a **60–90 day
  rolling window** from earnings-call transcripts and macro news — e.g.
  for AI→IT: a HYPE set ("AI capex", "GPU buildout", "hyperscaler
  demand") vs. a REALITY set ("AI ROI", "IT budget cuts", "vendor
  consolidation", "cost optimization", "deal ramp-downs").
- **Arms** when the reality narrative's rolling weight overtakes the hype
  narrative's (a crossover of two decaying keyword-frequency series —
  reuse the same exponential-decay math already in `decay_engine` /
  `sleep_phase` so recency is weighted consistently).
- Stores the armed state + its evidence (which transcripts/dates drove
  the crossover) as a new additive Brain Map object — provenance is
  mandatory, same discipline as Procedural Evolution's candidate refs.

### Trigger B — Technical Confirmation (VM-side, live engine)
- Extends `market_loop` / `live_bridge`: tracks the **relative strength**
  of the target sector vs. the benchmark (e.g. **CNXIT / NIFTY 50**
  ratio, using `dhan_client` daily closes).
- **Confirms** on evidence of structural institutional rotation — e.g.
  the RS line reclaiming a long-term moving average it had been below,
  or a multi-week RS breakout — not a single green day. Reuse
  `indicators.sma` / the existing trend-read primitives; no new TA stack.
- Confirmation is a *state*, not an instant: it must hold, mirroring the
  live bridge's existing de-dup discipline so one wobble doesn't fire.

### The gate
Campaign opens **only** when Trigger A is armed **AND** Trigger B is
confirmed, within a bounded window of each other (a narrative that armed
6 months before price confirmed is stale — the window is a playbook
parameter). Both states, and the AND evaluation, are logged with full
provenance for later review.

## 3. Staggered Execution

When the gate opens, the playbook emits a **coordinated sequence** of
proposals over the campaign's life, not a single trade — each one still
flowing through the existing margin gate (6G) and pending-approval
human-gate:

1. **Early / base-building (thesis young, trend unproven):**
   defined-risk, range-friendly structures — **Iron Condors** — that pay
   while the rotation is still consolidating. Capital-light per the 6G
   allocation layer.
2. **Confirmation / trend-riding (RS breakout holds):** shift to
   **directional debit structures** — **Bull Call Spreads** on the
   emerging leader (or Bear Call Spreads on the fading theme) — sized up
   as conviction and the equity curve allow.
3. **Maturity / exit discipline:** the campaign winds down when Trigger A
   decays back (narrative exhausts) or Trigger B breaks (RS rolls over) —
   reusing the tracker's existing exit rules per position; no new
   settlement path.

The sequence, its stage transitions, and each proposal's link back to
the campaign are one auditable object — think Procedural Evolution's
lineage tree, but for a campaign's trades instead of a rule's versions.

## 4. Generalization

The framework must be **theme-agnostic and reusable** — AI→IT is the
first instance, not a special case. A playbook is defined by data, not
code:

- **keyword sets** (hype vs. reality) for Trigger A,
- **sector / benchmark pair** and RS-confirmation rule for Trigger B,
- **the arming window** binding A and B,
- **the staged structure ladder** for execution.

New themes (e.g. "renewables capex → grid-reliability reality",
"EV-hype → charging-infra reality") are added as new playbook
definitions in a registry — same philosophy as `EVOLVABLE_PARAMETERS`
and the trade-planner's routing matrix: the *engine* is generic, the
*playbooks* are declarative config the human writes and reviews.

## Open questions to resolve at build time (not now)

- **Transcript source & offline compliance:** earnings transcripts are
  not in the current data layer. Where do they come from, and does
  ingestion stay within the zero-API-spend rule (local parsing) or does
  it need a licensed feed? This is the biggest unknown and likely gates
  the whole Trigger-A half.
- **CNXIT / sector index availability** in `dhan_client`'s
  `SECURITY_ID_MAP` — needs verification (and ties into the Issue-7
  SECURITY_ID_MAP audit already queued).
- **Backtestability:** can a playbook be replayed through the Phase 7
  simulator to validate a turn *would* have been caught, the same way
  every other strategy change is proven before it ships? If not,
  playbooks can't clear the same evidence bar as everything else.
- **Interaction with the daily engine:** does a campaign's margin coexist
  with day-to-day proposals under the single 6G ₹10L pool, or get its own
  sub-allocation? (Recommend: same pool, so total risk stays honestly
  capped.)

## Build sequencing (when the observation week clears)

This sits **behind** the already-queued post-observation priorities
(self-healing token refresh; dhan_client response-shape audit — see
[[project-alpha-trading-status]] and `docs/observation_week_ledger.md`).
Within the playbooks work itself, the natural order is: resolve the
transcript-source open question → build Trigger B (pure extension of
existing TA, low risk) → build Trigger A → the AND gate → staged
execution → the generalization/registry layer last.

---

## 5. Hypothesis-Driven Autonomous Simulation (advanced pipeline)

The playbook framework above assumes a human hand-writes each playbook's
config (keyword sets, sector pair, structure ladder). This pipeline is
the ambitious extension: the human supplies only a **raw macro thesis**
("Seed"), and the system autonomously structures it, validates it
against history, generalizes it across sectors, and installs permanent
sensors to catch the cycle early wherever it next appears.

This is the most speculative part of the whole design. It is documented
here as the target vision; every step below carries a hard reality-check
(§5.5) that must be answered before it can be trusted with even paper
capital. Nothing here weakens the non-negotiables: still paper-only,
still human-gated at the point of any actual proposal, still zero-API
LLM spend (local Ollama).

### 5.1 Seed Ingestion & Structuring
- The user inputs a raw, unstructured macro thesis — via a Discord
  command (extending the existing `chat_agent` / bot surface) or a text
  file dropped in a watched location.
- Local Ollama parses the natural language into a **structured economic
  cycle**: an ordered sequence of phases with entry/exit signatures,
  e.g. `CapEx Boom → Margin Compression → Value Rebound`.
- Output is strict-JSON, schema-gated exactly like Procedural Evolution's
  proposals (decision #49): a malformed parse is rejected, never
  half-interpreted. The structured cycle is a *hypothesis object*, not
  yet a playbook — it has earned nothing until §5.2.

### 5.2 Historical Validation via the Phase 7 Simulator
- The parsed cycle is routed to the **Time-Travel Simulator**
  (`src/simulator.py`) — the same real-pipeline, as-of-date replay engine
  every other strategy change already has to clear (decision #36).
- The simulator backtests the cycle's **structural footprint** against
  historical data to (a) confirm the phases actually occurred in the
  claimed order with tradeable separation, and (b) **parameterize** the
  cycle's empirical shape: typical **duration** per phase, **depth**
  (drawdown/rotation magnitude), and **volatility** regime at each turn.
- A Seed that cannot be reproduced in history — the phases never line up,
  or the "signal" is indistinguishable from noise — is **rejected here**,
  the same RevertOnRegression discipline that kills unprovable rule
  mutations. Validation is a gate, not a formality.

### 5.3 Autonomous Generalization
- A cycle validated on its origin sector is then replayed by the
  simulator against **other asset classes and indices** (Auto, Metals,
  Pharma, …) to discover whether any are **currently trapped in a
  parallel cycle** at an earlier phase — i.e. the same structural
  footprint, shifted in time.
- This is pattern-transfer, and it is where false positives breed: a
  footprint that "fits" three unrelated sectors is more likely
  overfitting than a universal law. Generalization results are therefore
  **ranked candidates for human review**, not auto-activated playbooks —
  each carries its own §5.2 validation score on the new sector, and a
  weak fit is surfaced as weak, not laundered into confidence.

### 5.4 Brain Map Anchoring
- A parameterized, generalized cycle that survives the above is written
  to a **new additive `cyclical_models` table** in `brain_map.db`
  (same additive-migration discipline as the regime columns and
  `simulated_trades` — core tables untouched, NULL-safe, idempotent).
- Each row anchors: the structured phase sequence, its empirical
  parameters (duration/depth/volatility per phase), the sectors it
  validated on with their scores, and full provenance back to the
  originating Seed text and the simulator runs that validated it.
- The system then installs **permanent sensor triggers**: standing
  Trigger-A (narrative) and Trigger-B (technical) watchers derived from
  the model, so that when **Phase 1** of this cycle's footprint begins to
  form in *any* tracked industry, the dual-gate arms and the staged
  execution playbook (§3) is proposed **early** — the whole point being
  to catch the turn as it starts, not after it's obvious.

### 5.5 Reality checks (must be answered before this is trusted)
- **Overfitting is the default failure mode, not the exception.** A cycle
  parameterized on one historical rotation and then "found" in three
  other sectors is exactly the shape of a spurious pattern. The pipeline
  needs an out-of-sample discipline (validate on one date range, confirm
  the sensor fires correctly on a *held-out* later range) or it will
  manufacture confident nonsense. This is the single biggest risk.
- **A 3B local model structuring macro theses is weak.** Ollama-parsing
  a nuanced thesis into a clean phase sequence will be the noisiest link;
  the schema gate catches malformed JSON but not *plausible-but-wrong*
  structuring. Human review of the parsed cycle (§5.1) before it consumes
  simulator time is likely mandatory, not optional.
- **"Structural footprint" needs a concrete definition.** Steps 5.2–5.3
  hinge on a computable footprint (which measurable series, over what
  window, matched by what metric). Undefined, this is hand-waving. This
  must be pinned down first — it is the technical crux of the whole
  pipeline.
- **Sensor drift.** Permanent triggers installed today are calibrated to
  today's regime; a cycle's footprint may itself evolve. Sensors need the
  same decay/re-validation the knowledge graph already applies to edges,
  or they slowly become stale false-alarm generators.
- **Human gate is preserved end-to-end.** Even a fully autonomous
  Seed→sensor pipeline still only ever *proposes* — every trade the
  staged execution emits remains pending-approval (decision #11). "Deploy
  early" means "propose early," never "trade autonomously."
