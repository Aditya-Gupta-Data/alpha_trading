# Self-Evolving Brain Map & Dynamic Confidence Engine — Design Spec (PLANNING ONLY)

> **Status: conceptual blueprint, locked 2026-07-09. NO code, NO
> migrations exist yet.** Build deferred until after the observation
> week. This document is the agreed architecture to build *from*.
>
> **Relationship to `skeptic_agent.py`:** this system already has a
> confidence-scoring engine — `RandomForestAuditor.predict_win_probability`,
> gated behind decision #44's rule that it must ABSTAIN rather than
> speak unless it clears a real statistical bar (`MIN_BALANCED_ACCURACY
> = 0.60`; it currently abstains — see decision #50, regime tags did not
> get it there). The architecture below is designed as the **next
> generation of that same engine**, not a second, parallel confidence
> system. Two numeric-confidence outputs that could disagree on the same
> live alert is a worse failure mode than one engine that abstains
> honestly. Wherever this doc says "confidence engine," read it as
> "skeptic_agent v3."

## 0. The shift in kind

Today's memory (`brain_map.db`) answers **discrete** questions: "how did
`iron_condor` do, historically?" (`query_similar_events`), "P(win) for
this feature vector?" (the skeptic, currently abstaining). This proposal
asks the map to answer **continuous** questions: "of all the times a
setup looked like *this exact shape*, where in that pattern's life cycle
are we **right now**, and what does the data say the optimal exit looks
like **from here**?"

That is a real jump — from classification (win/loss) to trajectory
matching + surface optimization. §4 is explicit about what has to be true
for that jump to be trustworthy rather than an elaborate overfitting
machine.

## 1. Real-Time Pattern Coordinate Mapping

### 1.1 Dynamic Time Warping & clustering
- Recent multi-day **price, volume, and volatility vectors** are
  continuously converted into normalized spatial patterns — the same
  underlying series `suggestions.analyze` / `simulator.analysis_from_closes`
  already compute (closes, VIX), extended with volume.
- **Dynamic Time Warping (DTW)** is the matching primitive: unlike a
  fixed-window Euclidean comparison, DTW aligns two series that move at
  different speeds — the right tool when "the same setup" can play out
  over 4 days once and 9 days another time. Clustering (e.g. DTW-distance
  + hierarchical or density clustering) groups historical windows into
  **pattern families**.
- This is new compute, not a new data source: it reads the same closes/
  VIX/volume the engine already fetches; it does not need a new feed.

### 1.2 "You Are Here" trajectory tracking
- Historical setups are **not** treated as static, resolved points (win
  or loss and done). Each is a **trajectory** — the full path the pattern
  took from formation to resolution.
- When a new setup forms, the engine finds its nearest match(es) in the
  historical cluster cloud and reports **where along that trajectory's
  typical life cycle** the current setup sits — e.g. "this looks like
  the early-formation third of the `iron_condor`/mid-VIX cluster's
  typical path," not just "this looks like that cluster."
- **This is the most underspecified piece in the request, and it has to
  be pinned down concretely before anything is built** (§4.1): "position
  within the cluster cloud" needs an actual geometric definition —
  candidates include (a) percentile position along DTW-aligned time
  within the matched trajectory, (b) a low-dimensional embedding (e.g.
  UMAP over the DTW distance matrix) with the current point's projected
  location, or (c) both, cross-checked against each other. Whichever is
  chosen must be described precisely enough that two engineers would
  build the identical thing from the description.

## 2. Self-Improving Confidence Engine (X%)

### 2.1 Error-driven Bayesian weight updates
- Every trade resolution (win / loss / rejected-would-have) feeds a
  **localized post-mortem back into that pattern's memory node** — same
  spirit as `tuner.py`'s existing archetype-weight learning loop, but at
  pattern-cluster granularity instead of BUY-archetype granularity, and
  Bayesian rather than a flat score: a **Beta-Bernoulli update**
  (win → increment α, loss → increment β) is the natural conjugate model
  for a pattern's win-rate belief — it has a clean closed form, and its
  posterior variance is itself a built-in "how much do we actually know
  about this pattern yet" signal (§2.2 uses this directly).
- A pattern that **underperforms its historical baseline** has its
  baseline confidence dynamically **penalized** — same decay philosophy
  `decay_engine.py` already applies to graph edges (`w(t) = w₀·exp(−λt)`,
  decision #37), generalized: a pattern's prior doesn't just decay with
  time, it decays *faster* when recent resolutions disagree with it.

### 2.2 Contextual variance weighting → one confidence percentage
- The engine outputs a single **X%** per candidate, composed from:
  - **structural tightness of the matching cluster** — how tightly the
    historical trajectories in the matched cluster actually agree (a
    tight cluster with 40 consistent examples says more than a loose
    cluster with 40 scattered ones — this is exactly what the Beta
    posterior's variance from §2.1 measures),
  - **current VIX stability** (regime context — reuses `src/regime.py`,
    §5 of the thematic playbooks spec, and the volatility-of-volatility
    read the engine already has access to),
  - **recent rolling accuracy of the specific strategy** (a live,
    short-window analog of what `train_skeptic`'s holdout score checks
    once per training run).
- **This is the single highest-stakes number in the whole document**
  (§4.2): a stated "73% confidence" that is not actually calibrated —
  i.e., of everything ever labeled ~73%, roughly 73% doesn't actually
  win — is worse than no number at all, because it manufactures false
  precision the human will reasonably trust. Decision #44's ship-gate
  discipline (abstain rather than mislead) must extend to this number:
  it needs a calibration check (reliability curve / Brier score) before
  it is ever shown on a live alert, and it must be allowed to say "not
  enough data to price this" instead of forcing out a number.

## 3. Apex Target Optimization

### 3.1 Expectancy surface mapping (MFE/MAE analysis)
- Replaces the current fixed R-multiple targets (`OPTION_PROFIT_TAKE_FRACTION
  = 0.65`, `PRE_EXPIRY_EXIT_DAYS = 2` in `plan_tracker.py`) with an
  **empirical distribution**: for every historical trade in the matched
  cluster, how far did it actually run in its favor before reversing
  (**Maximum Favorable Excursion**) and against (**Maximum Adverse
  Excursion**) — a standard, well-established technique in systematic
  trading research, not a speculative one.
- Buildable on data the engine already computes: `plan_tracker`'s
  resolution scan already walks the daily bars between entry and exit
  for every trade (`_resolve_spread` and friends) — MFE/MAE extraction is
  a byproduct of that same bar walk, not a new data-collection effort.

### 3.2 Apex Target — the mathematically optimized exit
- From the MFE/MAE distribution of the **matched cluster** (not the
  whole historical population — the whole point is conditioning on "setups
  that looked like this one"), the engine computes the take-profit and
  stop-loss levels that maximize **net expectancy** (after the existing
  full 2026 friction stack — STT, slippage, brokerage — the same honest
  P&L basis every other part of this engine already insists on).
- Output is advisory context on the proposal alert (a suggested Apex
  Target alongside the existing rule-based target), **never** a silent
  override of the trader's fixed rules — see §4.4.

## 4. Reality checks (must be answered before this runs on a live alert)

### 4.1 "You are here" needs a precise, falsifiable definition
As noted in §1.2: without a concrete geometric definition of "position
within the cluster cloud," this collapses into an impressive-sounding
restatement of "looks similar to some past trades." Pin the definition
down first; make it something that can be unit-tested against a known
synthetic trajectory.

### 4.2 Confidence percentages must be calibrated, or not shown at all
The single biggest risk in the whole document (§2.2). A number is a
promise. If §2's X% is not checked for calibration — and re-checked
periodically, the way `train_skeptic`'s ship gate re-evaluates on every
training run — it must default to abstaining, exactly like the current
skeptic. **This is not a nice-to-have; it is the same discipline decision
#44 already established, applied to a fancier number.**

### 4.3 Cluster sample sizes are almost certainly too thin today
The current corpus (≈1,000 simulated trades across a handful of
strategy/underlying/VIX-band combinations, decision #44/#50) split
further into DTW-matched **pattern clusters** will leave many clusters
with single-digit membership. A "confidence %" computed from 4 historical
examples is not a confidence percentage, it's noise wearing a lab coat.
The engine needs an explicit minimum-cluster-size floor (mirroring
`train_skeptic`'s existing `MIN_TRAINING_ROWS`/`MIN_LOSS_ROWS` refusal
logic) below which it reports "insufficient history" rather than a number.

### 4.4 The rule-based system stays the floor, not a fallback
Every existing hard rule — the VIX-16 regime gate, the 6G margin gate,
the fixed profit-take/pre-expiry exit — continues to apply regardless of
what this engine says. Apex Targets and confidence percentages are
**advisory context added to the alert**, the same way the knowledge
graph's memory context already rides along advisory-only (decision #26's
founding philosophy: memory informs, it never silently overrides a rule).
This system must not become the thing that quietly loosens the VIX gate
because a cluster "looked confident."

### 4.5 Look-ahead bias is a live risk in trajectory matching, not just in backtests
An MFE/MAE distribution or a "life-cycle position" computed from
historical trajectories must only ever use each historical trade's own
as-of-date information when it was itself live — the same non-negotiable
the Phase 7 simulator already enforces (decision #36). It is easy to
accidentally leak a historical trade's *known future* exit into "typical
life-cycle shape" statistics; the implementation must be as disciplined
about this as the simulator already is.

### 4.6 Compute placement follows the existing dual-node split
DTW clustering and cluster-wide MFE/MAE analysis are properly **heavy,
Mac-side** compute (decision #47/#48; §8 of the thematic playbooks
spec) — the 1GB VM cannot host this. The VM's role stays what it already
is: query precomputed, Mac-written coordinates/targets from
`brain_map.db` during its live evaluation cycles, the same async
additive-migration bridge pattern (§8.3 of the thematic playbooks spec)
already designed for `cyclical_models`. No new architecture is needed
here — this is the same bridge, carrying richer data.

### 4.7 Still, always, a proposal
Whatever this engine computes — a coordinate, a confidence percentage, an
Apex Target — it is advisory text on a proposal that still requires a
human tap on Approve (decision #11). This document does not change that
in any way; it only makes the advisory context more mathematically
grounded.

## 5. Relationship to other in-flight specs

This is the third planning document alongside `docs/thematic_playbooks_spec.md`.
Suggested build ordering when the observation week clears, layered onto
the already-queued priorities (self-healing token refresh; dhan_client
response-shape audit — see [[project-alpha-trading-status]]):
1. §4.2's calibration framework first — it is the safety mechanism every
   other piece depends on, and it can be prototyped against the skeptic's
   existing abstain/ship-gate logic with no new data pipeline.
2. §3 (MFE/MAE expectancy surface) next — buildable on data
   `plan_tracker` already collects, lowest new-infrastructure cost, and
   independently useful even without §1's DTW clustering.
3. §1 (DTW pattern coordinates) once §4.1's geometric definition is
   pinned down on paper.
4. §2's full Bayesian per-cluster confidence engine last, once §1 and
   §4.3's sample-size floor make its inputs trustworthy.
