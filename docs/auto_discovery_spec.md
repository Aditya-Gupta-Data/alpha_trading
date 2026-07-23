# Unsupervised Auto-Discovery — Architecture Blueprint (PLANNING ONLY)

> **Status: design spec, 2026-07-23 (owner directive: "stop relying on
> human-labeled episodes — find the patterns yourself"). NO code yet.
> Build begins only after the data spine is complete (the Gap Manifest).**

## 0. The thesis (and the trap)

The engine today matches TODAY against a HUMAN-curated catalog
(`macro_episodes.yaml`). The owner's critique is correct: a brain that
needs to be handed "El Niño" is a calculator. The moat is the engine
proposing its OWN episodes and regimes from 25 years of cross-asset
data, unlabeled.

**The trap this spec exists to avoid:** an unsupervised scan over a rich
lake will ALWAYS find "patterns" — most are noise. A system that reports
every motif it finds is a hallucination machine, which is worse than the
calculator. So §3 (the significance layer) is not a feature — it is the
whole product. Discovery is cheap; *proving a discovery isn't luck* is
the moat.

## 1. Part A — Unsupervised SHOCK detection (change-points)

Find the dates where the cross-asset SYSTEM broke, without being told.

- **Feature stream:** the existing `macro_features` daily vector (z20 of
  each global channel + the correlation state) — one code path, no new
  featurizer.
- **Break score, two complementary signals:**
  1. **Distributional break** — Mahalanobis distance of the trailing
     short window's distribution vs the trailing baseline; spikes = the
     system moved to a new state.
  2. **Correlation-structure break** — the cross-asset correlation
     MATRIX destabilizing (Frobenius distance of rolling corr matrices).
     Correlations breaking is the truest regime-shift signature — the
     Bridgewater insight: in a real regime change, what-moves-with-what
     inverts, not just levels.
- **Output:** ranked candidate shock anchors (date + break score),
  peak-detected above a null threshold (§3). Unnamed.

## 2. Part B — Unsupervised SLOW-BURN cycle discovery (motifs)

Find recurring 12-24 month multi-asset shapes that repeat across 25y —
the El Niño / rate-cycle class — without the labels.

- **Primitive:** self-similarity via the DTW we already built
  (`macro_fingerprints.dtw_distance`), computed on the SLOW channels
  (z60) over sliding ~18-month windows across the whole history.
- **Motif discovery:** the matrix-profile idea — for every window, its
  nearest non-overlapping neighbour; the lowest-distance pairs are
  recurring cycles. Cluster the low-distance windows (reuse
  `macro_fingerprints.cluster`) → **motif families = discovered
  slow-burn archetypes**, each with its member date-windows.
- **Output:** "there is a recurring ~18-month pattern peaking in 2009,
  2014, 2023" — and only THEN does a human look and say "those are the
  El Niño years." The discovery validates the label; the label was never
  an input. That is the exact inversion the owner asked for.

## 3. Part C — The significance layer (THE moat, not optional)

Every candidate from A or B must clear these before it is even a
"discovered episode," let alone advises anything:

1. **Out-of-sample recurrence.** Discover on the first ~60% of history;
   require the motif/regime to recur or predict in the held-out ~40%.
   In-sample-only = overfitting, discarded.
2. **Null-model significance.** Compare against **phase-randomized
   surrogates** and **block-bootstrap** of the same series (preserving
   autocorrelation). A real cycle recurs MORE than the null produces at
   a stated p. This is the abstention gate: "found nothing significant"
   is a first-class, common, correct output.
3. **Stability.** Robust to window-size and start-date perturbation;
   report a stability score. A regime that vanishes when the window
   shifts a month is not a regime.
4. **The candidate court.** Survivors are CANDIDATES routed through the
   existing `validation/registry` + `stat_gates` — the same lifecycle
   every human hypothesis faces. Discovery grants a hypothesis, never
   authority.

## 4. Part D — Integration (reuse, framework-free)

- Discovered episodes → `data/discovered_episodes.json`, tagged
  `source="auto"` (parallel to the human `macro_episodes.yaml`); the
  fingerprint / playbook / tracker machinery consumes both identically,
  provenance preserved.
- The nightly tracker declares against BOTH human and auto archetypes;
  when an auto-discovered regime and a human one AGREE, that is the
  strongest signal; when auto finds one the human catalog MISSED, that
  is a genuine discovery to surface (one Discord card).
- Runs as a periodic MINING job (Mac-side, weekly/monthly — heavy
  compute), a sibling of `evolution`/`discovery.nightly`, NOT the
  nightly cron. This is the macro instantiation of the already-locked
  `self_evolving_brain_map.md` DTW spec.

## 5. Honest limits (stated up front)

- **25 years holds maybe 15-20 slow-burn cycles, period.** Auto-discovery
  FINDS them; it cannot manufacture statistical power the data doesn't
  contain. n stays small; the significance layer keeps us honest about it.
- Motif discovery on ~6,500 daily points × ~6 channels is tractable on
  the Mac (no new infra, no paid compute — the ₹0 boundary holds).
- Auto-discovery will re-find most human episodes (validation) and
  propose a few genuinely new ones; it will NOT produce a flood of
  tradable regimes. If it does, the significance layer is miscalibrated
  and we halt — a flood is the failure signature, not success.

## 6. Build order (after the Gap Manifest lands)

1. AD-1: the break-score + motif self-similarity over the lake (compute
   only, writes a candidate list; no authority).
2. AD-2: the significance layer (surrogates, OOS, stability) — built
   BEFORE any candidate is admitted.
3. AD-3: candidate-court wiring + `discovered_episodes.json`.
4. AD-4: dual-catalog tracker + the "auto found one you didn't label"
   card.
