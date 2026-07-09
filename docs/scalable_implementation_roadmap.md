# Scalable Implementation Roadmap — Decoupled Architecture & the Skeptic Quality Gate (PLANNING ONLY)

> **Status: conceptual blueprint, locked 2026-07-09. NO code exists yet.**
> Build deferred until after the observation week. This is the fifth
> planning document in this series — it sits ON TOP of, and must stay
> consistent with, `docs/thematic_playbooks_spec.md`,
> `docs/self_evolving_brain_map.md`, and `docs/commercial_tip_verifier.md`.
> Where this doc's phases touch something already specified elsewhere,
> it cross-references rather than re-deciding it.

## 0. The Decoupled Scale methodology

Build every heavy component with a **clean interface boundary** between
"what it computes" and "where it runs" — so the *current* phase (Mac +
GCP VM, zero incremental cost) and a *later* scale phase (managed
compute: Spark/Redis, cloud GPU, higher-throughput queues) are two
deployments of the same abstraction, not two different systems. This is
standard, sound engineering practice independent of whether/when scaling
actually happens — the interfaces cost little to design well now and are
expensive to retrofit later.

## 1. K-Shape Brain Map (Compute Offloading)

**Current Phase:** the Mac's `sleep_phase.py` runs the heavy pattern-
clustering matrix calculations in an overnight batch process; the live
GCP VM performs only lightweight, low-latency lookups against the
pre-computed map during its evaluation cycles — the same Mac-heavy /
VM-light split already established (decisions #47/#48;
`self_evolving_brain_map.md` §4.6; `thematic_playbooks_spec.md` §8).

**Relationship to the existing brain-map spec:** `self_evolving_brain_map.md`
§1.1 already specifies "DTW + clustering" as the pattern-matching layer.
**K-Shape is a concrete algorithm choice for that same layer**, not a
competing new one — it's a shape-based clustering method (normalized
cross-correlation distance, clustering built into its own definition)
that is typically faster than DTW-distance + separate hierarchical
clustering at scale, at the cost of being less tolerant of the
time-axis warping DTW handles natively. Treat K-Shape as the leading
candidate implementation of §1.1's clustering step; the choice between
K-Shape / DTW+clustering / both (cross-checked against each other) is an
empirical question to settle when §1.1's build starts, not something to
lock in twice across two documents.

**Scale Phase:** abstract the data-ingestion interface (the boundary
between "here is a batch of price/volume/volatility vectors" and "here
is a cluster assignment") so the overnight-batch implementation can be
swapped for a real-time distributed Spark/Redis pipeline later without
touching anything downstream that consumes cluster assignments.

## 2. The Skeptic Engine Quality Gate — raised target, honest status

**The new baseline requirement: 0.70 balanced accuracy, up from the
existing 0.60 ship gate (`train_skeptic.MIN_BALANCED_ACCURACY`,
decision #44).** This section documents the proposed mechanism and is
explicit about what is proven versus what is a hypothesis to test.

### 2.1 Current Phase: Bayesian FBST epistemic e-values
- The GCP VM computes **Full Bayesian Significance Testing (FBST)**
  e-values dynamically during live evaluation — a Bayesian framework
  (Pereira & Stern) for quantifying how much evidence the data actually
  provides for a given estimate, as opposed to how large the estimate
  is. Used here to **penalize setups in low-data regimes**: a feature
  vector that resembles a cluster with thin historical support gets its
  epistemic e-value flagged, distinct from (and orthogonal to) the
  model's point-estimate P(win).
- This is a real generalization of machinery that already exists in this
  codebase — `train_skeptic.py` already refuses to ship a model below
  `MIN_TRAINING_ROWS`/`MIN_LOSS_ROWS`, a **hard, binary** version of
  exactly this idea (not enough data → don't trust the number). FBST
  e-values would make that a **smooth, per-prediction** epistemic
  measure instead of one global training-time cutoff.

### 2.2 ⚠️ Correction: "this is how we push the model past 0.7" is not yet established
This claim needs to be treated as a **hypothesis pending the same
empirical rigor decision #50 already applied tonight**, not as a solved
step in a roadmap. Two concrete reasons:

- **Tonight's actual result argues against assuming it works.**
  Decision #50 added an *orthogonal* new dimension (regime tags: trend +
  VIX band) to the exact same feature set and measured **no improvement**
  (5-fold balanced accuracy 0.578 vs. 0.594 pre-regime) — the diagnosis
  was that the existing features are already saturated with information
  the entry gates screen for, so a new dimension doesn't automatically
  buy new discriminative power. FBST epistemic e-values are also a new
  orthogonal dimension. There is no evidence yet that this one behaves
  differently from the last one — it needs the identical honest
  backfill-and-retrain experiment before being asserted as *the*
  mechanism, not assumed to succeed because the underlying math (FBST)
  is legitimate.
- **The mechanism, if it works, likely works by SELECTIVE ABSTENTION —
  which changes the denominator, not necessarily the model.** "Severely
  penalize low-data-regime setups" most naturally means: refuse to
  score (or heavily discount) predictions where epistemic uncertainty is
  high, i.e., predict on a *subset* of cases. Reporting balanced accuracy
  only on the subset the model chose to answer will almost always look
  better than accuracy on the full population — that's not new signal,
  it's declining to bet on the hardest cases. **This is a legitimate and
  useful technique (selective prediction / "reject option" classifiers
  are well studied)**, but the ship-gate evaluation must report it
  honestly: **both** the balanced accuracy on covered predictions **and**
  the coverage/abstention rate side by side (e.g. "0.71 balanced accuracy
  at 65% coverage, abstains on the other 35%") — never just the covered-
  subset number presented as "the model now hits 0.70," which would be
  the exact kind of quietly-restricted-population framing decision #44's
  whole ship-gate discipline exists to prevent.

**What this section commits to, honestly:** FBST epistemic e-values are
a well-motivated, worth-building technique, and selective abstention is
a legitimate path toward a higher *effective* quality bar. Whether it
actually clears **0.70 on covered predictions at a reasonable coverage
rate** is an empirical question, to be settled with the same
backfill/retrain/cross-validate discipline as decision #50 — not decided
in this document. When that experiment runs, it gets its own decision-
log entry (win or lose), exactly like #50 did.

### 2.3 Scale Phase
Design the FBST evaluation function so the e-value computation supports
**parallel Monte Carlo stress testing** — i.e., the per-prediction
epistemic estimate should be expressible as an embarrassingly-parallel
batch of simulations, ready to fan out across high-compute cloud
instances later without restructuring the estimator itself.

## 3. Commercial Tip Verifier (Asynchronous to Streaming)

**This section remains fully gated behind `docs/commercial_tip_verifier.md`
§1** (the SEBI Research Analyst / Investment Adviser regulatory review) —
nothing below changes or supersedes that gate. It documents the *queue
architecture* so it's ready to reference once (and only once) §1 clears,
exactly as that document specifies.

**Current Phase:** NLP tip verification runs as an **asynchronous batch
queue** on the Mac's local Ollama, specifically to avoid compute
throttling — batching keeps the 8GB Mac's single 3B-model inference slot
from being overwhelmed by concurrent requests (the same RAM-conscious
discipline as the `keep_alive: 0` / 3B-model-only rule already governing
every other local-LLM use in this repo).

**Scale Phase:** build the queue on a standard **Pub/Sub pattern**
(publisher enqueues a tip, worker(s) consume and process) from day one,
even while there's only one Mac-side worker — so scaling later means
pointing additional consumers (e.g. a dedicated cloud GPU worker) at the
same topic, not re-architecting the ingestion path.

## 4. The Deterministic Safety Envelope

**Most of this already exists — this section is consolidation and
naming, plus one genuinely new piece, not a greenfield build.**

**Already built, both current and scale phase (zero scaling overhead by
construction — plain arithmetic, no ML in the loop):**
- **Margin/order-size gating** — `src/portfolio_manager.py`'s Phase 6G
  layer: `request_entry` locks SPAN margin per proposed trade and
  silently rejects on exhaustion; `options_proposer.py`'s
  `size_lots(risk_pct=...)` already caps position size by the
  configured max-loss-per-trade percentage. This IS the order-size gate.
- **Cumulative-drawdown circuit breaker** — `portfolio_manager.py`'s
  `MAX_DRAWDOWN_PCT` (10%) halts ALL new entries once trailing drawdown
  from peak equity breaches it, hard-coded, checked before every entry.
- **Regime gate** — `strategy.py`'s `VIX_BLOCK_ABOVE` (16.0) strictly
  blocks range-bound structures above that VIX level, independent of
  anything the ML/skeptic layer says.
- All of the above already sit **directly above the execution client**
  (`gate_headless_entry` runs before a proposal is ever journaled) and
  are **already fully decoupled from the ML reasoning loop** — the
  skeptic is advisory-only (decision #44/§4.4 of
  `self_evolving_brain_map.md`); none of these hard gates consult it.

**Genuinely new:** a **per-day loss circuit breaker**, distinct from the
existing *cumulative-from-peak* drawdown halt — a gate that resets daily
and halts new entries for the remainder of the trading day once that
day's realized+marked losses cross a threshold, independent of where the
overall equity curve sits relative to its all-time peak. Same
implementation shape as `MAX_DRAWDOWN_PCT` (hard-coded constant,
checked pre-entry, zero ML involvement) — small, additive, no scaling
overhead either phase.

## 5. Cross-document consistency summary

| This roadmap's phase | Governed by / must stay consistent with |
|---|---|
| §1 K-Shape clustering | `self_evolving_brain_map.md` §1.1 (algorithm choice within that spec, not a new layer) |
| §2 FBST / 0.70 gate | `train_skeptic.py`'s existing 0.60 gate (decision #44); needs its own decision-log entry when tested, following decision #50's experimental discipline |
| §3 Tip verifier queue | `commercial_tip_verifier.md` §1 (regulatory gate — unresolved, this doc does not change that) |
| §4 Safety envelope | Mostly already shipped — `portfolio_manager.py` (6G), `strategy.py`'s VIX gate; only the per-day loss breaker is net-new |

**The one load-bearing open item in this entire document:** §2.2's FBST
hypothesis needs to be tested, not assumed, before "0.70" appears in any
future document as an achieved result rather than a target.
