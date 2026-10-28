# ARCHITECTURE ROADMAP — the institutional leap (saved 2026-10-28)

*State-save written before a usage-limit reset. Owner (Lead Architect) fixed the
order below. Nothing in this file is built yet. It is a blueprint, not a
decision-log entry: when Phase 1 starts, add a numbered entry to `DECISIONS.md`
(append-only) and the module rows to `MODULES.md` in the same commit.*

## 1. Execution order (binding)

| Phase | Name | Vector | Status |
|---|---|---|---|
| **1 (FIRST)** | Qualitative 3D Associative Knowledge Graph (GraphRAG) | Vector 3 | Steps 1–4 built on branch `phase1-graphrag` (`#162`, 10 tests); Step 5 awaits §2.7 owner ruling |
| **2 (SECOND)** | Self-Evolving Brain Map + Unsupervised Auto-Discovery (Cycle Hunter) | Vector 4 | not started; builds on `docs/self_evolving_brain_map.md`, `docs/cycle_hunter_plan.md`, `docs/auto_discovery_spec.md` |
| **3+ (LATER)** | OMS pipeline, multi-bucket allocation, multi-seat topology | — | not started; see `docs/m1_multi_bucket_ledger_blueprint.md`, decisions #143/#144/#146 |

Standing constraints that apply to every phase (from `CLAUDE.md` / `HANDOVER.md`,
re-checked 2026-10-28):

- **Paper money only; no broker path.** The VM is the only execution node (#146).
- **Zero execution authority for new intelligence layers.** Phase 1 output is
  *context and ranking*, never sizing or entry, until a passed statistical test
  and a Department-5 decision say otherwise (same rule as the Macro Regime Engine).
- **"NO NEW ENGINES" (07-27 roadmap directive):** Phase 1 must extend the existing
  `graph_edges` / `graph_engine` / `event_engine` / `local_parser` stack, not add a
  parallel one. **Owner has now ruled Phase 1 first; the first coding step must still
  re-confirm no code-freeze is active in `HANDOVER.md`.**
- **One door per concern:** LLM calls go through `src/local_parser.py`
  (FreeLLMAPI, `localhost:3001/v1`, `auto:fast`) and run **on the analytics node
  only** (#148/#149, `node_role.llm_jobs_refusal()`); Discord via
  `notifier.fire_broadcast`; abstention (`None` / named skip) over fabricated defaults.
- Tests hermetic: no network, no real `data/`/`logs/` writes (conftest guard).

---

## 2. PHASE 1 BLUEPRINT — GraphRAG / 3D Spreading Activation

### 2.0 What already exists (do NOT rebuild)

Verified in code 2026-10-28:

- `src/graph_engine.py` — table `graph_edges(source_node, relation, target_node,
  confidence_score, context, valid_from, invalid_at, decay_lambda, source)` with a
  UNIQUE index on `(source_node, relation, target_node)`; `add_edge` (idempotent,
  reinforcing), `GraphEngine` (loads ACTIVE edges into a `networkx.DiGraph`,
  read-only, memory-resident, decision #33).
- `src/decay_engine.py` — daily `w(t)=w0·exp(−λt)`, expiry below 0.1 via `invalid_at`.
- `src/sleep_phase.py` — `semantic_nodes`, `semantic_event_link`, `ingest_log`.
- `src/knowledge_graph/entity_affinity.py` — `entity_affinity` tables, `concentrates_in` edges.
- **Provenance firewall** (`graph_edges.source`): only `outcome_derived` edges may
  ever feed `vol_bridge` sizing. Affinity and miner edges are context only.
- `src/event_engine/{detect,llm,materiality,priced_in,store,…}` — RSS/inbox → LLM
  classify → card; `detect.in_universe` gates what reaches the LLM.
- `networkx` already in `requirements.txt`.

**Therefore Phase 1 = additive columns + one node table + one extraction module +
one propagation module.** No second graph store.

### 2.1 Library

`networkx` (already a dependency): `DiGraph` for the active-edge snapshot,
sparse matrix export via `nx.to_scipy_sparse_array` only if propagation outgrows
pure-Python loops (target graph ≤ ~5k nodes / 50k edges; pure Python is fine).
**Not** adding Neo4j, a vector DB or `sentence-transformers` in Phase 1.
SQLite remains the only persistent store.

### 2.2 SQLite schema additions (all in `data/brain_map.db`, additive, idempotent)

Created in a new `ensure_schema` section owned by the new module
(`src/knowledge_graph/graphrag.py`), same style as `graph_engine.ensure_schema`
(`CREATE … IF NOT EXISTS` + `PRAGMA table_info` guarded `ALTER`).

```sql
-- NODES: every entity / sector / commodity / theme / policy the graph can hold.
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id       TEXT PRIMARY KEY,          -- canonical key, e.g. 'TATAMOTORS', 'sector:AUTO', 'commodity:LITHIUM'
    node_type     TEXT NOT NULL,             -- 'company'|'sector'|'commodity'|'technology'|'theme'|'policy'|'macro'|'person_group'
    display_name  TEXT NOT NULL,
    aliases_json  TEXT,                      -- JSON list; reuses detect.STOCK_ALIASES + config/entity_groups.json
    in_universe   INTEGER NOT NULL DEFAULT 0,-- 1 = tradable name on the VM universe (seeds execution-relevant output)
    sector        TEXT,                      -- home sector for company nodes
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes (node_type);

-- EDGES: reuse graph_edges; add the columns below via guarded ALTER.
ALTER TABLE graph_edges ADD COLUMN edge_class   TEXT;   -- 'supply'|'demand'|'substitute'|'competes'|'regulates'|'owns'|'macro_sensitivity'|'causal'
ALTER TABLE graph_edges ADD COLUMN polarity     INTEGER;-- +1 reinforcing / −1 inhibiting / 0 neutral  (sign of influence)
ALTER TABLE graph_edges ADD COLUMN weight       REAL;   -- 0..1 propagation strength (distinct from confidence_score)
ALTER TABLE graph_edges ADD COLUMN evidence_count INTEGER DEFAULT 1;
ALTER TABLE graph_edges ADD COLUMN extractor    TEXT;   -- 'llm:<model>' | 'rule' | 'seed'
-- graph_edges.source gets a NEW value 'llm_extracted' (context-only under the firewall).

-- PROVENANCE: one row per (edge, supporting text) so every edge is auditable.
CREATE TABLE IF NOT EXISTS graph_edge_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node   TEXT NOT NULL,
    relation      TEXT NOT NULL,
    target_node   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,             -- sha256 of source text (dedupe, like ingest_log)
    source_ref    TEXT,                      -- event_engine store id / rss url / inbox file
    quote         TEXT,                      -- ≤240-char supporting snippet
    extracted_at  TEXT NOT NULL,
    UNIQUE (source_node, relation, target_node, content_hash)
);

-- ACTIVATION LOG: append-only record of every propagation run (forward-test record, never rewritten).
CREATE TABLE IF NOT EXISTS graph_activation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL,
    trigger_ref  TEXT,                       -- event id / card id
    seeds_json   TEXT NOT NULL,              -- {node: energy0}
    params_json  TEXT NOT NULL,              -- decay, hops, threshold, graph snapshot hash
    result_json  TEXT NOT NULL               -- {node: {energy, hop, via}}  top-K only
);
```

Notes: `graph_nodes` seeds from `STOCK_ALIASES`, the sector map used by the
proposer, and `config/entity_groups.json`. Existing `graph_edges` rows (ticker /
regime / theme strings) stay valid — a node need not pre-exist for an old edge;
`ensure_nodes_from_edges()` back-fills `graph_nodes` from the distinct endpoints.

### 2.3 LLM extraction pipeline (Event Engine → graph edges)

Module: `src/knowledge_graph/graphrag_extract.py`. Runs **on the analytics node
only**, from the same cron lane as the event emitter, after `event_engine.detect`
has passed an item (so the universe gate already bounded LLM volume).

1. **Input:** item text from `event_engine.store` (title + summary + body ≤ 3k chars).
2. **Dedupe:** `content_hash` against `graph_edge_evidence`; skip if seen.
3. **Prompt** (through `local_parser`, `auto:fast`, temperature 0, JSON only):
   - System: "Extract directed supply-chain / demand / regulatory relationships stated
     or directly implied by the text. Return ONLY JSON
     `{"edges":[{"subject":str,"subject_type":str,"predicate":str,"object":str,
     "object_type":str,"polarity":-1|0|1,"strength":0..1,"quote":str}]}`.
     `predicate` ∈ the closed vocabulary below. `quote` MUST be a verbatim substring
     of the input. No speculation; empty list if nothing is stated."
   - Closed predicate vocabulary (→ `edge_class`): `makes`/`sells`→supply,
     `needs`/`consumes`→demand, `substitutes`→substitute, `competes_with`→competes,
     `regulates`/`subsidises`/`taxes`→regulates, `owns`/`subsidiary_of`→owns,
     `sensitive_to`→macro_sensitivity, `drives`/`hurts`→causal.
4. **Worked example.** Text: "Tata Motors ramps EV output; cell makers warn of
   lithium shortage."
   LLM → `Tata Motors –makes→ EV`, `EV –needs→ Lithium`, `Lithium –shortage hurts→ EV`.
   Canonicalised → rows:
   `(TATAMOTORS, makes, technology:EV, supply, +1, 0.8)`,
   `(technology:EV, needs, commodity:LITHIUM, demand, +1, 0.8)`.
   Chain TATAMOTORS→EV→LITHIUM is now walkable.
5. **Validation (reject, never repair):** predicate in vocabulary; `quote` is a
   verbatim substring (kills hallucinated edges — the annual-report pipeline measured
   62% hallucination drop from exactly this check); subject ≠ object; both endpoints
   resolve via alias table or are accepted as new non-company nodes
   (`in_universe=0`); `strength` clamped 0..1. Anything failing → counted in a
   `rejected` stat, not written.
6. **Write:** `graph_nodes` upsert; `graph_edges` via `add_edge(..., source="llm_extracted")`
   with initial `confidence_score = 0.5·strength`, `weight = strength`,
   `decay_lambda = 0.02` (relationships are slower-moving than event edges, but
   not permanent); re-observation reinforces: `confidence ← 1 − (1−c)(1−0.5·strength)`,
   `evidence_count += 1`. Evidence row inserted.
7. **Fail-open:** LLM down / parse error / node refusal → skip item, log, event
   engine behaviour unchanged. Test both "exception swallowed" and "later stages ran".
8. **Review flag:** a *new* edge touching an `in_universe` node with `weight ≥ 0.7`
   fires one de-duped Discord card via `notifier.fire_broadcast` (owner rule: items
   needing review are real-time cards, not log lines).

### 2.4 Spreading Activation — math

Module: `src/knowledge_graph/spreading_activation.py`. Pure function over a
`GraphEngine` snapshot; reads only; writes one `graph_activation_log` row.

**Seeds.** An event card activates seed nodes with initial energy
`a₀(v) = s·m` where `s ∈ {−1,+1}` is the event's sentiment and `m ∈ (0,1]` is
the existing materiality score (`event_engine.materiality`).

**Edge transmission coefficient** (per directed edge `u→v`):

`t(u→v) = w(u→v) · c(u→v) · p(u→v) · ε(u→v)`

- `w` = `weight`, `c` = current decayed `confidence_score`, `p` = polarity (±1; 0 edges transmit nothing),
- `ε = 1/ (1 + log(1 + outdeg(u)))` — **fan-out normalisation**, so hub nodes (e.g. "crude oil") do not blast every neighbour at full strength.

**Propagation** (synchronous, for hop `k = 1 … K`, with `K = 3`, i.e. 2nd and 3rd-order nodes):

`aₖ(v) = γᵏ · Σ_{u→v} t(u→v) · aₖ₋₁(u)`

- `γ = 0.6` per-hop decay (1st-order retains 0.6, 2nd 0.36, 3rd 0.216 of raw signal),
- **Pruning:** drop any `|aₖ(v)| < θ`, `θ = 0.02`; cap frontier at the top 200 nodes by `|aₖ|`,
- **No re-injection:** a node's energy from hop k is not propagated back to nodes already on that path (path-simple walk) — prevents cycles (A→B→A) from amplifying.

**Aggregation.** Total energy `E(v) = Σₖ aₖ(v)` (signed, so conflicting
paths partially cancel). Report `E(v)`, the dominant hop, and the best path
(`argmax` of single-path contribution) for explainability.

**Momentum ("3D").** The third dimension is time/persistence. Maintain a per-node
momentum `M(v,t) = λ_m·M(v,t−1) + E_today(v)` with `λ_m = 0.85` and `M` decaying
the same way as edges (`exp(−λ·Δdays)`), stored in `graph_activation_log` rollups
(not a mutable table). Repeated same-sign activation across days builds momentum;
a one-off spike fades. Output ranks by `|M|` then `|E|`.

**Dimensions summarised:** (1) *structural* — which edges exist; (2) *strength* —
w·c·p·ε; (3) *temporal* — decay + momentum.

**Parameters** (`γ, K, θ, λ_m, ε-form`) live in one config block and are written
into every `params_json` row, so any result is reproducible. **They are priors,
not tuned values; do not fit them to PnL** (that is Phase 2's validated job).

### 2.5 Consumers (context only — zero execution authority)

- Event card gains a "second/third-order read" line: top-5 `E(v)` nodes, in-universe flagged.
- `resonance.py` / analysis (Dept 8) may *read* `E(v)` as context for an advisory.
- **Not allowed in Phase 1:** any use as sizing, entry filter or gate. Promotion
  needs: ≥60 forward-logged activations with outcome pairing, a significance test
  against shuffled-edge placebos (reuse `src/validation/placebo.py`), and a
  Department-5 decision.

### 2.6 Build steps (for the next session)

1. Read `HANDOVER.md` + `PROJECT_TIMELINE.md` (Rule 1); confirm no freeze.
2. `graphrag.py`: `ensure_schema` (nodes, ALTERs, evidence, activation log) + `seed_nodes()` + tests (idempotent, old edges untouched, provenance firewall: `llm_extracted` never reaches `vol_bridge`).
3. `spreading_activation.py` pure function + tests on hand-built graphs (3-hop chain, cycle, hub fan-out, sign cancellation, pruning, determinism).
4. `graphrag_extract.py` with the validator + fake-LLM tests (verbatim-quote rejection, closed vocabulary, fail-open).
5. Wire extraction after `event_engine.detect` on the analytics node block only; wire read-only card line. Update `MODULES.md`, `CRON_SETUP.md`, `DECISIONS.md` in the same commit.
6. Dry-run on a week of stored event-engine items on the Mini PC before enabling the cron line; ledger entry in `docs/observation_week_ledger.md`.

### 2.7 Open questions for the owner (answer before step 5)

- Seed ontology: is the closed predicate vocabulary above acceptable, or add more?
- Extraction volume cap per day (LLM quota on FreeLLMAPI) — proposed 150 items/day.

---

## 3. PHASE 2 (summary only)

Self-evolving Brain Map: edges and parameters proposed by an unsupervised scanner
(Cycle Hunter / Auto-Discovery AD-1→AD-4), admitted only through a significance
gate with a placebo comparison and the append-only forward record. Phase 1's
`graph_activation_log` is the dataset this phase mines. Existing constraint:
AD-2 has admitted zero discoveries so far (decision log, 08-01); the gate stays.

## 4. PHASE 3+ (summary only)

OMS pipeline hardening, multi-bucket allocation (`docs/m1_multi_bucket_ledger_blueprint.md`),
multi-seat topology. Constraint: the VM stays the single execution node (#146);
the Mini PC stays analytics-only (#143/#144).

---

## 5. FULL "PAPER-PROVE EVERYTHING" VISION — six vectors (owner text, condensed 2026-10-28)

*Condensed from the Architect's brief, not copied. Status column checked against
the repo today (file existence only — "exists" means a module is there, not that
it is proven). Lines in **'single quotes'** are the agent's own additions.*

**Thesis:** every institutional component (execution, reconciliation, multi-bucket
allocation, GraphRAG, cycle discovery) is built and statistically proven on paper
before any real rupee. Paper-only (CLAUDE.md Rule 7) stays binding.

| # | Vector | Owner's scope | Repo status 10-28 | Phase |
|---|---|---|---|---|
| 1 | Execution & OMS | limit-chasing multi-leg algo (`next_gen_engine/execution_algo.py`), wings-first leg order, order state machine (PENDING/PARTIAL/FILLED/REJECTED/CANCELLED, idempotent), read-only broker recon daemon, remote kill-switch | `src/oms.py`, `src/execution/{paper_venue,live_pricer,recon_engine}.py`, `next_gen_engine/execution_algo.py`, desk-halt latch (#125) all **exist**; recon is GET-only (#94). Remote mobile kill-switch: **not verified** | 3+ |
| 2 | Multi-bucket allocation (M4) | 5 ring-fenced buckets B1 options ≤25%, B2 swing ≤20%, B3 darlings ≤30%, B4 long-term + GOLDBEES wealth lock ≤25%, B5 cross-asset/macro ≤15%; `corporate_actions.py` split/bonus adjuster | blueprint `docs/m1_multi_bucket_ledger_blueprint.md` (design only); `wealth_lock.py`, `firm_treasury.py` exist; `corporate_actions.py` **absent** | 3+ |
| 3 | GraphRAG | weighted directed entity graph, 2nd/3rd-order signal discovery | §2 above | **1** |
| 4 | Self-evolving Brain Map / Cycle Hunter | 2016–2023 bhavcopy backfill, AD-1–AD-4 (Mahalanobis, Frobenius corr-shift), MFE/MAE-optimised exits | AD-1/AD-2 built (zero admissions), `calibration/mfe_mae_analyzer.py` exists | **2** |
| 5 | Playbooks & staged positions | Stage-B 60-session forward clock, H4 pyramid in shadow, `PAPER_EVENTS` ₹10L sandbox | Stage B clock running (#86, target ~Oct 13+, slack 0), H4 shadowed, `PAPER_EVENTS` executor ON (#157) | in flight |
| 6 | Topology / multi-seat (M6) | VM = execution, Mini PC = analytics/LLM; per-tenant SQLite isolation, role permissions | role split LIVE (#146/#147); tenancy **not started** | 3+ |

### Agent additions (not in the owner's brief)

- **'Kill-switch first.'** 'Of everything in Phase 3, the remote kill-switch is the cheapest and highest-value: reuse the existing desk-halt latch (#125) behind a signed Discord command or a tiny authenticated endpoint. Needs an owner ruling on the auth method before building.'
- **'Corporate actions can move up.'** 'Phase 1 and Phase 2 both read historical bars; a split-unadjusted bar corrupts GraphRAG outcome pairing and the 2016–2023 backfill. Build `corporate_actions.py` (golden-file tests on a known split and a known bonus) before the Phase 2 backfill, even though M4 itself is Phase 3+.'
- **'Bucket caps need an enforcement test.'** 'The 25/20/30/25/15 caps sum to 115%. Intentional ceilings, but the allocator must define precedence when all buckets want their ceiling, and a test must prove no bucket can borrow another’s halted capital.'
- **'Survivorship in the backfill.'** 'bhavcopy 2016–2023 only lists names alive today unless delisted symbols are included; the archive/ NIFTY-50 set is already known to be survivor-biased. Record this in the Phase 2 spec and treat backfilled results as hypotheses, never as forward evidence (append-only forward ledgers stay untouched, Rule 3).'
- **'MFE/MAE exits:'** 'the existing analyzer is the starting point; replacing static targets is a sizing/exit change, so it still needs the dry-run over the open book and a Court verdict before any deploy.'
- **'Proof standard for "proven on paper":'** 'one definition for all six vectors: forward (not backfilled) sample, pre-registered metric, placebo/shuffled comparison, n stated. Otherwise "proven" drifts.'
- **'Sequencing guard:'** 'Phase 1 and 2 are analytics-node work and cannot touch the execution path, so they can run without disturbing the live VM; Phase 3 items touch the VM and need the open-book dry-run protocol.'
