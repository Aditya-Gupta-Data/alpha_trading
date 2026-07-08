# Phase 8: Semantic News Ingestion — Architecture Spec

**Status: SPEC ONLY. Nothing in this document is built.** No code exists for
any module named here, no database schema has been changed, and no tests
have been written. This file is a blueprint for a future implementation
session — treat every table/column/function signature below as a DRAFT,
not a contract, until it's actually built and reviewed.

**⚠️ Naming note — this is NOT the old Phase 8.** `VISION_PLAN.md`'s
original Phase 8 ("Advanced Data Ingestion — Block Deals & Options Prep")
was retired: data ingestion moved to the DhanHQ Data API instead
(`DECISIONS.md` decision #22), and that old Phase 8 is explicitly marked
superseded. The number is free, so this is a new, unrelated effort reusing
it. If a future session renumbers the roadmap, this doc's title should
move with it.

---

## 1. Objective

Feed the Knowledge Graph (Phase 6C/6D) a second source of causal evidence —
structured semantic triplets mined from **official NSE regulatory text**
(results, circulars) — distinct from the two sources that already exist:

| Source | Module | What it produces | Feeds |
|---|---|---|---|
| Reviewed trade outcomes | `sleep_phase.write_causal_links` (Phase 6D) | `(strategy) RESULTS_IN (win/loss)` triples, confidence 1.0 | `graph_edges` (decision #34: outcomes ONLY) |
| RSS headlines, per-ticker | `news_processor.py` (Phase 4D) | A single sentiment score per watchlist ticker | `data/news_sentiment.json` (deliberately isolated — see its own docstring) |
| **NSE results/circulars (Phase 8, this doc)** | `news_ingestion.py` + `local_parser.py` (new) | Time-neutral `(subject) PREDICATE (object)` semantic triplets, regime-tagged | `graph_edges` (proposed — **see the open question in §5**) |

**Phase 8 does not replace `news_processor.py`.** That module stays exactly
as-is (RSS + Gemini, per-ticker sentiment, feeds the forecast/dashboard).
Phase 8 is a separate, narrower pipeline: official regulatory/results text
only, structured causal extraction only, feeding the graph only.

---

## 2. Layer 1 — Sourcing (`src/news_ingestion.py`, new module)

- **Source**: [`nsepython`](https://pypi.org/project/nsepython/) (open-source,
  not yet a project dependency — add to `requirements.txt` when this is
  actually built). Two fetchers: `nse_results()` (corporate results
  filings) and `nse_circular()` (NSE/SEBI circulars).
- **Pre-LLM keyword filter — deterministic Python, no LLM.** Per decision
  #30 (an LLM must never be used for work that's cheap and deterministic
  in plain code), the first pass is a plain keyword match against a small
  list (`RBI`, `SEBI`, `Monetary Policy`, and whatever else proves useful
  once real circular text is seen) — only matching items go anywhere near
  the local LLM in Layer 2. Everything else is dropped before it costs a
  single token.
- **SHA-256 dedup against a new `raw_news` table**, checked *before*
  parsing — same shape as `sleep_phase.py`'s existing `ingest_log`
  (content-hash primary key, so re-running the fetch is idempotent and
  Ollama is never re-billed for text already seen). Draft schema:

  ```sql
  CREATE TABLE IF NOT EXISTS raw_news (
      content_hash   TEXT PRIMARY KEY,   -- sha256 of the raw fetched text
      source         TEXT NOT NULL,      -- 'nse_results' | 'nse_circular'
      headline       TEXT,
      published_at   TEXT,
      keyword_matched TEXT,              -- which filter term hit, for audit
      fetched_at     TEXT NOT NULL,
      parsed         INTEGER NOT NULL DEFAULT 0  -- has Layer 2 run on it yet
  );
  ```

  Owned by `news_ingestion.py`, additive to `brain_map.db` — same
  one-module-owns-its-tables convention as `sleep_phase.py`
  (`ingest_log`/`semantic_nodes`), `graph_engine.py` (`graph_edges`), and
  `simulator.py` (`simulated_trades`). Core `brain_map.py` tables stay
  untouched.

---

## 3. Layer 2 — Cognitive Extraction (`src/local_parser.py`)

- **Semantic chunking**, not naive truncation: NSE circulars/results text
  can exceed Llama 3 8B's usable context comfortably, so a future
  `extract_time_neutral_triples()` must split on paragraph/section
  boundaries (not mid-sentence) and process chunks independently, merging
  the resulting triplet lists — analogous to how `sleep_phase.py`'s
  consolidation step already batches events into one bounded prompt rather
  than sending unbounded input.
- **Predicates are TIME-NEUTRAL** — this is the core difference from Phase
  6D's causal triples. Phase 6D asks "did this strategy result in a win or
  loss" (an outcome, anchored to one resolved trade). Phase 8 asks "what
  general market mechanism does this regulatory text describe" (a
  standing relationship, not tied to one event) — hence `INCREASES` /
  `DECREASES` / `CAUSES` / `INDICATES` / `CONTRADICTS` instead of Phase
  6D's `RESULTS_IN` / `PRECEDES` / `INDICATES` / `CONTRADICTS`.
  **Open question**: should these two predicate vocabularies stay
  separate (each source keeps its own semantics), or converge into one
  shared enum? Not resolved here — flagging it so a future session makes
  the call deliberately rather than by accident.
- **The required prompt** (verbatim, to enforce on implementation):

  ```
  You are an expert financial semantic parser for an algorithmic trading system.
  Extract causal relationships regarding the Indian market (Nifty 50 / Bank Nifty) as a JSON list of Time-Neutral Subject-Predicate-Object (SPO) triplets.
  RULES:
  1. Predicates MUST be time-neutral: [INCREASES, DECREASES, CAUSES, INDICATES, CONTRADICTS].
  SCHEMA: [{"subject": "Entity", "predicate": "VERB", "object": "Target", "regime_impact": "Expansion/Contraction/Neutral"}]
  ```

- **`regime_impact`** (`Expansion` / `Contraction` / `Neutral`) is new —
  neither Phase 6D's triples nor `local_parser`'s existing
  `extract_event_json`/`extract_causal_triples` carry a regime tag today.
  This is the field Layer 3's hard-regime-shift logic (§4) keys off of.
- Same guardrails as every existing `LocalExtractor` method: local Ollama
  only, fail-safe (unreachable/unusable output -> `[]`, never raises), zero
  market-data imports (decision #30's guard test would need a Phase-8
  counterpart).

---

## 4. Layer 3 — Graph Integration & Temporal Decay (`graph_engine.py` schema + `sleep_phase.py`)

`graph_edges` (Phase 6C: `source_node, relation, target_node,
confidence_score`; Phase 6D added `context`) needs **three more additive
columns** to support news-derived edges aging out over time — same
`ensure_schema()` + `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`
in-place-upgrade pattern already used for the `context` column:

```sql
ALTER TABLE graph_edges ADD COLUMN valid_from   TEXT;   -- ISO date the edge became true
ALTER TABLE graph_edges ADD COLUMN invalid_at   TEXT;   -- ISO date it's forced inactive, or NULL
ALTER TABLE graph_edges ADD COLUMN decay_lambda REAL;   -- per-edge exponential decay rate
```

Two distinct expiry mechanisms — deliberately not conflated:

1. **Soft decay** (`decay_lambda`): the same exponential-decay shape
   `sleep_phase.py`'s `semantic_nodes` already uses
   (`score_new = score * e^(-lambda * dt)`), applied per-edge instead of
   per-node. A gradually-staling market read (e.g. "IT sector strength" a
   few months old) fades smoothly rather than vanishing or staying at
   full confidence forever.
2. **Hard regime shift** (`invalid_at`): certain events aren't decay,
   they're a wall — e.g. the SEBI 2026 F&O rule changes structurally
   invalidate prior options-market behavioral edges overnight, regardless
   of how fresh they otherwise looked. `regime_impact`-tagged edges whose
   underlying rule changed get `invalid_at` set explicitly; once passed,
   the edge is force-excluded from `GraphEngine` traversal no matter what
   `decay_lambda` alone would say.

`GraphEngine.get_relevant_context()` (Phase 6C) would need an `as_of`
filter — `valid_from <= as_of AND (invalid_at IS NULL OR invalid_at > as_of)`
— before returning an edge. Not built; noted here as the reader-side
follow-up so Layer 3 isn't schema-only.

---

## 5. ⚠️ Open architectural question — must be resolved before implementation

**Decision #34 currently states**: *"Knowledge Graph edges must be
extracted ONLY from reviewed outcomes/post-mortems, never from raw,
unverified news sentiment."* That rule exists because a reviewed outcome
is ground truth (the trade actually won or lost), while news sentiment is
unverified model output — causal claims mined from it would be
speculation compounding speculation.

Phase 8, as specified above, proposes writing **news-derived** SPO
triplets into the **same** `graph_edges` table decision #34 governs. This
is a direct tension that this spec does **not** resolve — flagging it as
the one decision a future implementation session must make explicitly
before writing code, likely one of:

- **(a)** Give news-derived edges a distinct provenance marker (e.g. a
  `source` column value `'news_semantic'` vs Phase 6D's
  `'outcome_causal'`), so decision #34's outcome-only guarantee is
  preserved for that clearly-scoped subset, while Phase 8 edges are a
  separate, explicitly-labeled class under new rules (and `GraphEngine`
  reads/weighs them differently — e.g. never sorted above outcome-derived
  edges, or surfaced in a visibly distinct section of the Discord memory
  block).
- **(b)** Formally amend/supersede decision #34 to allow reviewed
  regulatory text as a second legitimate source, with its own justification
  banked in `DECISIONS.md`.

No default is chosen here on purpose.

---

## 6. Guardrails carried forward unconditionally

Regardless of how §5 resolves, any future implementation MUST keep:

- **Decision #30**: the keyword filter and any other deterministic logic
  in `news_ingestion.py` stays plain Python — no LLM for cheap,
  deterministic work; the LLM only ever sees pre-filtered, deduplicated
  text.
- **Decision #33**: graph reasoning remains READ-ONLY, advisory inference.
  Phase 8 edges — like every edge before them — inform the Discord
  proposal's rationale and nothing else. They never gate a VIX check,
  resize a position, or block a proposal.
- **Paper-only, no broker** (project-wide, decision #11): nothing in this
  pipeline places or influences a real order.
- **Additive-only schema changes**: every new table/column above uses
  `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN` guarded by
  `PRAGMA table_info`, matching every prior phase's convention. Nothing
  here should ever require a destructive migration.

---

## 7. Suggested build order (for whenever this is greenlit)

Same step-by-step, confirm-before-proceeding discipline used for Phases
6D/7/11: (1) resolve §5, bank it in `DECISIONS.md` with a number; (2)
`news_ingestion.py` — sourcing + keyword filter + `raw_news` dedup, fully
offline-testable with injected fetchers; (3) `local_parser.py`'s
`extract_time_neutral_triples()` + semantic chunking, offline-tested with
a fake extractor; (4) the `graph_edges` schema migration + `GraphEngine`'s
`as_of` filter; (5) wire Layer 1 -> Layer 2 -> Layer 3 end to end; (6)
tests at every step, matching the existing `test_causal_writer.py` /
`test_graph_engine.py` pattern.

---

*Spec authored 2026-07-07. No code, schema, or tests exist for any of the
above as of this writing.*
