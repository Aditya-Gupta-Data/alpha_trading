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
