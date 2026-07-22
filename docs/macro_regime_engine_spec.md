# Macro Regime & Pattern Engine — Architectural Blueprint (PLANNING ONLY)

> **Status: design spec, 2026-07-22 (owner directive: "build a real
> moat — macro phases and cross-asset correlations"). NO code exists.
> This is the pre-review artifact for the build; the owner's example
> target — "Dollar-vs-Crude clash, Type 3, Phase 3, historically 4
> months after a major geopolitical shock" — is the north star, with
> the honesty machinery that makes such a sentence trustworthy.**

## 0. Design laws (non-negotiable, inherited from the house)

1. **Abstention beats hallucination.** The engine declares a phase ONLY
   above a similarity floor with a stated analog count. "No confident
   regime match (best 0.58, floor 0.70)" is a first-class output — the
   skeptic_agent precedent (decision #44) applied to macro.
2. **Advisory-only, risk-REDUCING authority**, through the ONE existing
   seam (`analysis/regime_filters.advise` → `build_proposal(advisory=)`)
   until Dept 5's stat_gates grant more (Review #2 ruling). No second
   authority path. No new door.
3. **NULL-honest data**: a missing series day is a hole, never
   interpolated silently; every fingerprint records what it could NOT
   see.
4. **Shadow first**: the engine publicly declares phases daily for ≥60
   sessions and gets SCORED on them before its advisories carry any
   weight beyond cards.
5. **Zero/near-zero cost**: EOD cadence, free sources, kilobyte-scale
   storage. Latency target is "tonight", never "this tick" — a regime
   reader, not an intraday signal.

## 1. Data Layer — the Cross-Asset Lake (Dept 1 clerk, ₹0)

**The structural advantage:** survivorship bias — the reason our
2000-2021 stock archive is research-only — DOES NOT APPLY to macro
series. Brent, DXY, USD/INR, US10Y, index levels: none of them can be
delisted. So while stock-level work is capped at the bhavcopy floor
(~Oct 2019), the macro engine trains on **25+ years, including 2008,
2011, 2013 taper, 2016 demonetization, 2018 IL&FS, 2020 COVID, 2022
Ukraine/rates, 2023 banking, 2024 election + yen-carry.** The episode
base grows ~3x vs the Time Machine window alone. This is the single
biggest edge in the design.

- **Sources (all free, all EOD):**
  - FRED CSV endpoints (stable, decades deep, no auth for csv pulls):
    Brent (`DCOILBRENTEU`), broad-dollar DXY proxy (`DTWEXBGS`),
    USD/INR (`DEXINUS`), US 10Y (`DGS10`). One polite fetch per series
    per day.
  - India VIX: live already flows via Dhan into `daily_context`;
    history via NSE's indices archive (same fetch idiom as
    bhavcopy_clerk).
  - NIFTY + sector indices history: NSE indices archive (needed to map
    "what followed" onto tradable outcomes; `sector_index_bars.json`
    already captures the present).
- **Shape:** `src/ingestion/macro_lake.py` — a clerk in the exact
  bhavcopy_clerk mold: drop-folder friendly, idempotent, NULL-honest,
  throttled, `data/lake/macro/<series>.csv` (append-only, one row per
  day). Cron: nightly, off-hours, AFTER the market loop is done. Total
  storage for 25 years × ~8 series: **a few MB.** Cost: **₹0.**
  Latency: T+0 evening for Indian series, T+1 morning for US series
  (FRED publishes with a 1-day lag) — acceptable by Law 5 and stamped
  honestly in every output (`data_as_of` per series).

## 2. The Phase Engine — fingerprints from the Time Machine

**Vocabulary (fixed, so the whole firm speaks one language):**
- **Episode** — a dated macro shock anchor (COVID crash 2020-02-24,
  Ukraine 2022-02-24, taper tantrum 2013-05-22, …). Two admission
  routes: (a) a hand-curated seed catalog (~15 episodes, each with a
  one-line why — auditable, the macro_shocks/War-Playbook lineage);
  (b) mechanical detection (NIFTY drawdown >7%/10d, VIX z>2.5, or
  USD/INR move >2%/5d) proposing NEW anchors that land in the catalog
  only via the candidate court. Every episode row carries its source.
- **Fingerprint** — for each episode, the normalized multi-asset
  trajectory: z-scored 20/60-day changes of each series + the rolling
  60-day correlation state (e.g., the dollar-crude correlation SIGN —
  the owner's "clash" is precisely `corr(DXY, Brent) < -0.4 with both
  |z| > 1`, now a measurable, versioned definition) sampled at
  T-20 … T+120 around the anchor.
- **Archetype ("Type")** — episodes clustered on fingerprint distance
  (DTW — deliberately the same primitive as the locked
  `self_evolving_brain_map` spec §1.1, ONE pattern-matching engine in
  the firm, not two). With n≈15 episodes the honest cluster count is
  **3-4 archetypes, not more** — the spec hard-caps k so the taxonomy
  cannot outgrow the evidence.
- **Phase** — within an archetype, the composite post-anchor timeline
  segmented where the cross-asset state MEASURABLY shifts (changepoint
  on the composite trajectory, typical result: Phase 1 shock/vol-spike,
  Phase 2 basing/divergence, Phase 3 normalization-or-second-leg).
  Phases are properties OF an archetype, never free-floating.
- **Playbook table** — per (archetype, phase): what NIFTY sector
  indices did (median + spread + hit-rate, n stated), the tradable
  memory. This generalizes the hand-written War Playbook into a
  measured artifact.

**Where it lives:** episodes/fingerprints/playbooks are brain_map rows
(events with `event_type='macro_episode'`, `source='backfill'` tagging
per the Time Machine rule) + one artifact `data/macro_templates.json`.
Rebuilt only when the catalog changes — this is offline compute, Mac
lane.

## 3. The Current State Tracker — "what phase are we in, honestly"

Nightly (after macro_lake ingests), `analysis/macro_regime.py`:

1. Compute today's feature vector exactly as fingerprints were computed
   (same code path — one featurizer, no train/serve skew).
2. DTW-match the trailing 60-session trajectory against every
   archetype's phase segments.
3. Output `data/macro_regime.json`:
   - `best_match`: archetype + phase + similarity + **analog count** +
     time-since-nearest-detected-anchor;
   - `runner_up` (the second-best always shown — regime calls that hide
     their ambiguity are lies of omission);
   - `declared`: true ONLY if similarity ≥ floor (start 0.70) AND
     analogs ≥ 3 — else the honest `"no_confident_match"`;
   - the playbook slice for the declared cell (sector medians, n);
   - `data_as_of` per series + every hole that reduced confidence.
4. One line appended daily to a **declaration ledger**
   (`logs/macro_regime_declarations.jsonl`) — the immutable record the
   scorer reads. Family transitions (declare/undeclare/phase-change)
   fire ONE Discord card via the standard door; daily sameness is
   silent (the tier-engine card discipline).

**Scoring (Dept 5, the court):** after 20/60 sessions, each declaration
is graded — did the declared phase's playbook direction materialize
better than the unconditional base rate? `stat_gates` rules on the
ledger, not on backtests of itself. Until the gate passes, the tracker
is a public forecaster on the record, nothing more.

## 4. Execution Integration — how macro state touches money

All via EXISTING seams, all risk-reducing, all reversible:

- **Sector pause list** (equity desk): a declared phase whose playbook
  shows a sector's analog hit-rate ≤ threshold puts that sector on
  `macro_regime.json`'s `caution_sectors`; `regime_filters.advise`
  consumes it → entries in those sectors are BLOCKED-with-reason
  (risk-reducing = allowed today). Card carries the analog table.
- **Sizing damper** (both desks): declared Phase 1 (shock) →
  `adaptive_sizing` consult may only shrink multipliers (floor 0.5x),
  never boost. Undeclared regime = 1.0x, untouched.
- **Cash reserve tilt** (treasury): advisory line to the 19:50 treasury
  cron suggesting a lower deployable fraction during declared Phase 1/2
  — logged + card; the treasury's own rules decide (no new authority).
- **Options strategy filter**: declared high-vol phases veto NEW
  short-vol structures (condors) via the existing proposal advisory;
  directional spreads unaffected.
- **Graduation path**: each hook ships OFF, turns on only after the
  Dept 5 gate scores the tracker's ledger ≥ its bar; any hook's
  authority beyond risk-reduction needs a new owner ruling (Review #2
  law restated).
- **Product leg**: `macro_regime` + `macro_playbooks` become premium
  brain-MCP tools — the declaration ledger with graded outcomes IS the
  demo ("here's every call we made, timestamped, and how it scored").
  The moat and the product are the same artifact.

## 5. Build sequence & cost

| Step | What | Lane | Cost |
|---|---|---|---|
| M1 | macro_lake clerk + 25y backfill (FRED + NSE indices) | Dept 1, ~1 session | ₹0 |
| M2 | Episode catalog (seed 15) + fingerprint builder + archetype clustering (k≤4) | Dept 8, 1-2 sessions | ₹0 |
| M3 | Playbook tables + brain_map episode rows | Dept 8/4, 1 session | ₹0 |
| M4 | State tracker + declaration ledger + cards | Dept 8, 1 session | ₹0 |
| M5 | 60-session public shadow scoring (calendar time, not build time) | Dept 5 | ₹0 |
| M6 | Execution hooks, gated ON one by one | Dept 2/3 | ₹0 |

M1-M4 fit inside the Max window alongside the existing schedule if
prioritized over the landing page; M5 runs through August regardless of
subscription tier (it's cron + ledger). **Total new spend: ₹0** — the
moat rides the proof-gate plan without opening a single gate.

## 6. What this does NOT promise

- It will not name a phase every day. Most days are "no confident
  match" — that is correct behavior, and the Discord silence rule makes
  it cheap.
- It will not learn from 9 cells what 15 episodes cannot support — the
  k-cap and analog-count floors are load-bearing, not decoration.
- It does not move money on day one. It earns each hook through the
  same court everything else in this firm faces.
