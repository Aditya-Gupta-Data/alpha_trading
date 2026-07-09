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

---

## 6. Continuous Learning & Execution Loop

§5 turns one human Seed into an installed cyclical model. This section
closes the loop: how the system **autonomously hunts** for cycle turns
over time — and generates entirely new Seeds — without constant manual
input. It maps directly onto the existing two-machine cadence (Mac
nightly / VM live / weekend downtime) so it adds rhythm, not a new
daemon.

Same non-negotiables carry through: paper-only, human-gated at every
actual proposal, zero-API LLM spend (local Ollama), and — critically for
an *autonomous* loop — nothing it discovers auto-activates; discoveries
become **ranked review items**, not live playbooks (see §6.4).

### 6.1 The Daily Heat-Map (light compute — Mac, `sleep_phase.py`)
- A new nightly task alongside the existing sleep-phase passes (A–E).
  Local Ollama scores that day's macro news + earnings text against
  **every known cycle** in the `cyclical_models` table (§5.4).
- Maintains a dynamic **Heat Map**: per (sector × known-cycle), a rolling
  score of how strongly today's evidence matches each cycle phase's
  entry signature — i.e. "which sectors are entering which phase, how
  warm." Stored as an additive table (`cycle_heatmap`, dated rows so the
  trajectory is auditable, decay-weighted like everything else).
- Light by construction: it *scores against existing models*, it does not
  simulate or fetch price data. This is what makes it safe to run every
  night on an 8GB Mac with a 3B model.
- Output states per cell: `cold` / `warming` / `hot`. `warming` is the
  handoff signal to §6.2.

### 6.2 The Weekend Simulator (heavy compute — weekend downtime)
- Markets are closed; the Phase 7 simulator can run long without
  competing with the live session. A new scheduled weekend pass takes
  every sector the week's Heat Map flagged **`warming`** and runs the
  Time-Travel Simulator on it to seek **technical price confirmation** of
  the suspected cycle (Trigger B's logic, applied retroactively + to
  recent bars).
- This is the deliberate coupling of the two triggers across time: the
  daily heat-map is the cheap fundamental primer (Trigger A at scale);
  the weekend sim is the expensive technical confirmation (Trigger B).
  A sector only escalates to a proposable campaign when BOTH agree — the
  §2 AND gate, now driven continuously instead of per-hand-written-
  playbook.
- Where it runs is an open question (§6.5): the 1GB VM cannot host heavy
  simulator sweeps, and the Mac holds no live token — most likely the Mac
  runs it against the VM-refreshed bars cache (the same pattern
  `evolution.py --refresh-bars-cache` already uses), on a weekend
  LaunchAgent.

### 6.3 Autonomous Self-Seeding (anomaly detection)
- The above hunts for *known* cycles. This finds *new* ones. The system
  monitors **unmapped** sectors for extreme fundamental↔technical
  **divergences** — the raw material of a hype→reality turn — e.g. peak
  hype sentiment (Trigger-A narrative maxed) co-occurring with breaking
  price momentum (Trigger-B rolling over): the market still talking the
  story while the tape stops paying for it.
- On such a divergence, the system **generates its own Seed thesis**
  (local Ollama, describing the divergence as a candidate cycle), and
  runs it through the entire §5 validation pipeline — structure →
  historical validation → generalization. If the math holds (survives
  §5.2's reproducibility gate and §5.5's out-of-sample discipline), a new
  `cyclical_models` row is proposed.
- This is the system writing its own homework — and therefore the
  highest-risk mechanic in the whole spec (§6.5).

### 6.4 The full loop
```
daily heat-map (Mac, nightly)            — score today vs known cycles
   │  sectors flagged "warming"
   ▼
weekend simulator (weekend downtime)     — technical confirmation
   │  AND-gate opens
   ▼
staged execution playbook (§3)           — PROPOSES campaign, human approves
   ╲
    ╲  in parallel, always-on:
     ▼
anomaly self-seeding (§6.3)  ──►  §5 validation  ──►  new cyclical_models row
                                                       (feeds back into the
                                                        daily heat-map)
```
Each turn of the loop enriches `cyclical_models`, which sharpens the next
day's heat-map — the compounding-knowledge property the Brain Map was
built for, now applied to macro cycles.

### 6.5 Reality checks (must be answered before this runs unattended)
- **Autonomous self-seeding is the sharpest double-edge in the system.**
  A machine that invents its own theses, validates them against its own
  simulator, and installs its own sensors can compound a single flawed
  assumption into a self-reinforcing delusion. The §5.5 out-of-sample
  discipline is not optional here — it is the only thing standing between
  "discovery" and "confident garbage." Self-seeded models should arguably
  require explicit human promotion before they ever arm a live sensor,
  even though everything downstream is already paper + human-gated.
- **Multiple-comparisons / data-dredging.** Scanning every sector every
  night against every known cycle, plus hunting anomalies, is thousands
  of implicit hypotheses tested continuously. Some WILL cross any fixed
  threshold by chance. The heat-map needs a false-discovery-rate
  correction or it becomes a permanent low-grade false-alarm generator.
- **Compute honesty on the Mac.** "Light" nightly scoring is only light
  if it stays scoring-against-existing-models; if it drifts into
  re-embedding or re-simulating nightly it will crush the 8GB machine
  (the constraint that already forced the 3B model + keep_alive=0). The
  light/heavy split (§6.1 vs §6.2) is a hard boundary, not a guideline.
- **Weekend-run placement + token cost.** The weekend sweep needs a fresh
  bars cache (VM-side token) but heavy CPU (Mac-side) — the split must be
  designed so it never re-introduces the single-token race (decision #48)
  or blocks the Monday session.
- **The loop must be interruptible and observable.** An always-on
  autonomous discovery loop needs the same ops-monitor heartbeat +
  problem-ledger treatment as every scheduled job, plus a kill switch —
  an autonomous system you cannot cheaply stop is a liability regardless
  of how good its ideas are.
- **Human gate, restated because autonomy invites forgetting it.** Every
  escalation in this loop terminates in a *proposal*, never a trade. The
  loop can discover, validate, generalize, and arm sensors entirely on
  its own — and still, the only thing that opens a position is the human
  tapping Approve (decision #11).

---

## 7. Wisdom Extraction & Optimization Pipeline

Where §5–§6 discover cycles from the system's own data, this pipeline
imports **external investing wisdom** — the qualitative cycle theories of
legendary investors (Buffett, Dalio, Marks, …) — and turns them into
tested, optimized, quantitative playbooks. It is a reinforcement-style
loop: read a theory, translate it to parameters, backtest it, optimize
it, store the refined version.

The key design choice (revised from an earlier manual-upload draft):
literature is **autonomously retrieved**, with human escalation only when
a source is genuinely unreachable. That autonomy makes the legal/ethical
guardrails in §7.5 load-bearing, not optional — read them as part of the
architecture, not a footnote.

### 7.1 Autonomous Knowledge Hunting
- Triggered when the system identifies a cyclical anomaly (§6.3) but has
  no optimized playbook for it — a *known gap*, not a fishing expedition.
- The local LLM generates targeted **search queries** for relevant
  literature on that specific macro theory, and the retrieval layer
  fetches **genuinely public documents** — shareholder letters, published
  investor memos (e.g. Oaktree/Marks memos, Berkshire letters), public
  transcripts — parsing PDFs/HTML/text into plain text.
- The LLM then extracts the qualitative cycle definition and **translates
  it into quantitative, testable parameters**: specific VIX thresholds,
  moving-average deviation bands, sentiment extremes, phase-duration
  priors — the same structured-cycle object shape as a §5 Seed, now
  sourced from literature instead of a human thesis.
- **Hard constraint (see §7.5):** the retrieval layer respects
  robots.txt, site ToS, rate limits, and access controls. It fetches only
  what is freely and lawfully accessible; it never attempts to defeat a
  paywall or anti-bot measure. When it hits one, that is not a failure to
  route around — it is the trigger for §7.2.

### 7.2 Human-in-the-Loop Escalation
- The system halts a given hunt **only** on an unpassable, lawful barrier:
  a strict paywall, an anti-bot block, or simply no reliable public
  source it can identify for a specific theory.
- On such a barrier it sends a **targeted Discord alert** requesting
  human help — e.g. *"Unable to source literature on the 1980s inflation
  cycle. Please provide a URL or a document path."* The human drops a
  link or a file; the pipeline resumes at §7.1's parse step.
- This reframes the paywall/anti-bot case correctly: the system does not
  try to get past the barrier — it **asks the human to supply the
  document they have lawful access to**. Escalation *is* the
  circumvention-avoidance mechanism, not a fallback after failed
  circumvention.

### 7.3 Baseline Simulation & Optimization Loop
- The extracted **"Version 1"** trading plan is run through the Phase 7
  Time-Travel Simulator against historical cycles to **baseline** its
  performance (win rate, per-trade Sharpe, max drawdown — the same
  metrics `train_skeptic` / `evolution` already compute).
- An automated **optimization loop** then perturbs entry/exit variables —
  delaying entries until volatility crests, staggering position sizing,
  shifting profit-take/stop parameters (drawing on the same
  `EVOLVABLE_PARAMETERS` whitelist discipline as Procedural Evolution,
  so the search space is bounded and every knob is one the engine can
  actually act on) — searching for a mathematically superior execution
  path: comparable or better return at **lower drawdown**.
- **This optimizer is a curve-fitting engine by nature** (§7.5): left
  unchecked it will always "find" a superior historical path, because it
  is fitting to history. The RevertOnRegression + out-of-sample
  discipline from §5.5 applies with full force here — the refined plan
  must beat V1 on a **held-out** date range, not just the one it was
  tuned on, or it is overfit and discarded.

### 7.4 Brain Map Integration
- A refined plan that survives out-of-sample validation is stored in the
  `cyclical_models` table (§5.4) as the optimized playbook for that
  cycle — ready for the §6 continuous loop to arm automatically when live
  conditions match, still proposing (never auto-trading) per the human
  gate.
- Provenance is mandatory and specific: the row records the source
  document(s), the extracted V1 parameters, the optimization delta, and
  the out-of-sample scores — so a human reviewing a proposal can trace it
  back to "Marks' memo on X, translated to these params, optimized this
  way, validated on this held-out range."

### 7.5 Reality checks (must be answered before this runs unattended)
- **Lawful retrieval is a hard architectural boundary, not a preference.**
  The retrieval layer must respect robots.txt, site Terms of Service,
  rate limits, and all access controls. It fetches only freely and
  lawfully accessible material. It must never be built to bypass
  paywalls, anti-bot systems, or authentication — the moment it hits any
  such barrier it escalates to the human (§7.2). This is what keeps an
  "autonomous scraper" on the right side of the line; without it, the
  feature should not be built at all.
- **Copyright: store derived parameters, not republished prose.** The
  value extracted is the *quantitative translation* (VIX levels, MA
  bands, phase priors) — numbers and rules, not the author's copyrighted
  text. The pipeline stores the derived parameters + a citation/provenance
  pointer to the source, not wholesale copies of memos or transcripts.
- **The optimizer overfits by construction — this is the central risk.**
  An automated loop tweaking entry/exit variables against historical
  cycles is, definitionally, curve-fitting; it will report a "superior"
  path that is often just memorized history. Out-of-sample validation on
  a held-out range is the only thing that separates genuine improvement
  from overfitting, and it must gate every promotion to §7.4.
- **Garbage-in from weak translation.** A 3B local model translating
  nuanced investing prose into precise parameters is a lossy, error-prone
  step; a confidently-wrong translation produces a confidently-wrong
  playbook. Human review of the extracted V1 parameters — before they
  consume optimizer/simulator time — is likely required, mirroring §5.5.
- **Attribution honesty.** A playbook derived from an investor's public
  writing should carry that attribution in its provenance, both for the
  human's benefit and to avoid presenting borrowed reasoning as the
  system's own discovery.
- **Human gate, once more.** Even a fully autonomous read→translate→
  optimize→store pipeline still only ever produces a *stored model that
  proposes when triggered*. Nothing here trades; the human still approves
  every position (decision #11).
