# The Machine's First Opinion — Macro Shock Clustering Report

> **Morning briefing, built overnight 2026-07-23.** Source artifact:
> `data/macro_templates.json` (commit `b6932ed`); engine:
> `src/analysis/macro_fingerprints.py`; data: the live macro lake
> (US10Y→1962, USDINR→1973, BRENT→1987, DXY→2006). Every number below
> is read from the artifact, not remembered.

## What was asked

Take 17 curated historical shocks (2001–2024), fingerprint each as its
cross-asset trajectory (z-scored moves + the dollar-crude correlation
state, T−20 → T+120 around the anchor), measure every pair's similarity
with time-warp-tolerant DTW, and let the machine group them — **with no
labels, no dates, no names.** Hard cap: 4 archetypes, because 17
episodes cannot honestly support more.

**Result: all 17 episodes fingerprinted, all 136 pairs comparable,
average observation coverage 0.99.** Nothing was excluded; nothing was
faked.

## The four archetypes it found

### A1 — "Global financial risk-off" (8 members · exemplar: US downgrade 2011)

> lehman_gfc · covid_crash · eurozone_flash_2010 · us_downgrade_2011 ·
> em_selloff_2006 · yuan_deval_2015 · cpi_rate_shock_2022 · svb_banking_2023

Every global credit/liquidity/monetary panic in the catalog, grouped by
trajectory alone. The two closest pairs in the ENTIRE matrix live here:
**COVID ↔ the 2010 eurozone flash (DTW 0.291)** and **eurozone flash ↔
US downgrade (0.293)** — the machine is saying these crises *moved* the
same way regardless of their nominal causes. The 2022 CPI shock sits
0.387 from the 2011 downgrade: the dollar-wrecking-ball signature,
eleven years apart.

### A2 — "INR-transmission shocks" (6 members · exemplar: the Taper Tantrum)

> taper_tantrum · ukraine_invasion · yen_carry_unwind ·
> israel_gaza_2023 · demonetization_plus_trump · india_election_2024

**The headline you asked for: yes — Ukraine 2022 clustered with the
Taper Tantrum (DTW 0.606, full coverage).** We had labeled Ukraine
"geopolitical"; its actual cross-asset fingerprint — crude bid while
the rupee bleeds against a firm dollar — filed it with 2013's INR
crisis instead of with Lehman's family. The class hint was the prior;
the trajectory was the truth. Also here: the 2024 yen-carry unwind sits
just 0.552 from the taper tantrum — two "EM funding squeeze" episodes
that share almost nothing in their news coverage and almost everything
in their price action.

### A3 — The pre-2006 pair (2 members)

> nine_eleven · india_election_2004 (DTW 0.718 between them)

An honesty artifact, and deliberately so: the broad-dollar series only
exists from 2006, so these two episodes carry a dark DXY channel. The
engine clustered them on what it could genuinely see rather than
inventing the rest. Treat A3 as "old, thin-channel shocks," not as a
behavioral family — the indices backfill (India VIX/NIFTY channels)
will re-test them.

### A4 — IL&FS, alone (1 member)

> ilfs_nbfc_crisis

The machine **refused to force India's 2018 NBFC credit crisis into
any global family** — its nearest neighbour anywhere is the 2022 CPI
shock at 0.500, and average linkage kept it out of every cluster. That
matches financial reality: IL&FS was a domestic, slow-burn credit
freeze with crude near cycle highs — a double squeeze with no global
analog in our catalog. A cheaper system would have filed it somewhere.
This refusal is the credibility of every future match.

## Sanity checks worth knowing

- **Farthest pair in the matrix: the 2004 India election vs Lehman
  (1.185)** — a purely domestic political shock vs a global credit
  collapse. The metric's extremes are exactly where they should be.
- The two India election shocks (2004, 2024) sit at 0.799 — related,
  but twenty years and a very different global backdrop apart; the 2024
  one clustered A2 because the INR channel dominated.
- Determinism: rebuilding the artifact from the same lake + catalog
  reproduces it byte-for-byte (no randomness anywhere in the pipeline).

## Honest caveats (the fine print that keeps this trustworthy)

1. **Channels active in this build:** z-scored 20-day moves of BRENT /
   DXY / USDINR / US10Y plus the dollar-crude correlation state. India
   VIX and NIFTY channels are NOT yet in — their history lands with the
   indices backfill, after which the fingerprints rebuild and this
   report gains an addendum: *did the archetypes survive richer
   evidence?*
2. **17 episodes is a seed, not a sample.** The archetypes are
   descriptive structure, not tradeable signals. Nothing here advises
   anything until the playbook tables (M3) are built AND the state
   tracker (M4) survives its 60-session public scoring by Dept 5's
   stat_gates. That graduation path is unchanged.
3. DXY is dark before 2006 (named per-pair in the artifact's coverage
   figures); the pre-2006 archetype is data-thin by construction.
4. The k=4 cap is load-bearing. With this few episodes, more clusters
   would be numerology.

## What runs next (already in motion overnight)

1. Indices backfill 2019→today (India VIX, NIFTY, 12 sector indices) —
   then the fingerprint rebuild and the addendum below.
2. M3: playbook tables — per (archetype, phase), what each NIFTY sector
   actually did, hit-rates and n stated. That's where shapes start
   learning what they *pay*.
3. M4: the nightly state tracker — "which archetype does TODAY resemble,
   at what similarity, or honestly none" — logged to a public
   declaration ledger and scored after the fact.
