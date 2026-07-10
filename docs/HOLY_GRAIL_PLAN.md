# HOLY_GRAIL_PLAN.md — The Unified Market Brain: Master Roadmap

**Status: PLAN ONLY (2026-07-10 night). Nothing below is built unless a phase
is explicitly marked shipped. This document is the synthesis of a 10-agent
design panel (5 architecture lenses × adversarial cross-examination) plus the
owner's directional answers, recorded 2026-07-10.**

Read `OVERVIEW.md` first. Everything here honors the non-negotiables:
paper-only, no broker execution code, file-based local state, no LLM in the
live price loop (#30), single Dhan token (#48/#56). "Full autonomy" below
always means *on paper, with the supervision contract of §6*.

---

## 0. The owner's direction (locked inputs to this design)

Captured via structured Q&A 2026-07-10 night:

1. **The edge, ranked:** smart-money footprints (deals/entities/flows) +
   macro-regime rotation + news catalysts. **Technicals are demoted to
   timing/confirmation** — they stop being the thesis and become the trigger.
2. **Horizon:** swing (days–weeks), decided EOD; **entries may be timed
   intraday** via the existing live loop. No full-intraday rebuild.
3. **Data moat (all free, all approved):** years of bulk/block archive
   backfill; FII/DII daily flows + delivery %; insider/SAST + shareholding
   patterns. Paid options-chain history: not now (we capture-forward instead).
4. **Autonomy ceiling: FULL AUTONOMY ON PAPER** — trades fire without
   per-trade approval; the human supervises via report cards and named vetoes.
5. **Smart-money confirmation rules (his instinct, now design):** a print is
   real when (a) price holds/rises the next days, (b) promoters/insiders are
   buying their own stock, (c) delivery % spiked with the deal.
6. **Macro→stock links to seed (validated later, never trusted blindly):**
   crude → OMCs/paints/aviation/tyres; USDINR → IT/pharma exporters;
   gold↑+VIX↑ = risk-off veto regime; US overnight/GIFT-Nifty gap playbook.
7. **News catalysts as triggers:** order wins/contracts; policy/budget/RBI;
   **red flags (fraud/auditor exits/pledge spikes/SEBI actions) as VETO
   events.** (Earnings-surprise triggers deprioritized by choice; the
   earnings *calendar* still ships as a feature — see §4.)
8. **Supervision contract:** daily digest + weekly deep-dive; veto by
   replying with a pattern/channel name.

## 1. The architecture in one paragraph

Every layer (technicals, macro matrix, news frames, entity-affinity, flows,
VIX regime, Brain-Map memory) becomes an **evidence emitter** in one shared
vocabulary, stamped onto every proposal at creation (*Evidence Snapshot*) and
frozen into a per-trade *decision receipt*. Composition follows one law:
**each trade names exactly ONE primary driver; every other layer votes only
CONFIRM / NEUTRAL / VETO — never blended points** (this is decisions #26/#33
promoted to the constitution of the whole brain; it prevents signal-mush and
double-counting, and keeps every trade explainable in one sentence). New
patterns are **discovered** by no-LLM statistical miners over the accumulated
history, but nothing discovered may influence anything until it survives the
**proving harness**: a registered hypothesis with a frozen definition,
walk-forward trial through the real simulator, multiplicity correction,
stability battery, and live shadow-monitoring with kill criteria. Validation
is a lease, not a diploma. The live loop stays dumb, fast arithmetic; the
brain thinks nightly in the sleep phase.

## 2. Why this shape (the panel's hardest-won conclusions)

- **The system has already proven it can fool itself twice** (#44: coin-flip
  skeptic; #50: regime features added nothing). With hundreds of resolved
  trades and dozens of deals/day, mining WILL find fake patterns. The harness
  is not bureaucracy — it is the product. A pattern that reaches the owner's
  eyes has, by construction, already paid rent out-of-sample.
- **Averaging kills.** Four weak correlated signals blended into one score
  yields permanent "mild bullish" and dilutes the only calibrated layer.
  Hence confirm/veto verdicts, one named primary driver, vetoed proposals
  STILL journaled (so the veto class itself accumulates scoreable outcomes —
  a veto changes presentation, never observability).
- **Sim and real evidence never pool silently.** ~366 simulated trades vs
  tens of real ones: any stat that mixes them is a stat about the synthetic
  world. Every scoreboard reports the strata separately; simulated evidence
  can support but never solely justify a promotion.
- **EOD honesty:** any signal sourced after close (deals ~19:00, macro, news)
  is only ever evaluated at **T+1 open with gap slippage**, and promotion
  requires the edge to survive T+1 (T+1≈T+3 means drift, not information).
- **Losses are permanent, and now so are their lessons** (loss-permanence
  invariant + λ=0 loss-derived edges, shipped 2026-07-10). The nightly graph
  audit may flag but never invalidate a λ=0 edge.

## 3. Phase 0 — Stop the data bleeding (hours; ship immediately)

Everything here has an **irreversibility clock** — each day not captured is
training data lost forever. Zero risk to the trading path.

1. **EOD option-chain archiver**: one post-close cron (~15:40 IST, after
   master_scheduler self-terminates; respects single-token #48/#56) snapshots
   full NIFTY/BANKNIFTY chains (all strikes/expiries: LTP, OI, IV, volume) +
   VIX + spot to `data/lake/chains/date=…/…json.gz` (~100MB/yr). Historical
   chains are unbuyable (#36's own note) — this is the only dataset money
   can't recover. Add to `ops_monitor.EXPECTED_JOBS` in the same commit.
2. **Archive the perishable JSONs daily**: `news_sentiment.json` + the macro
   matrix currently get overwritten — a ~20-line daily copy into the lake is
   the load-bearing prerequisite for every cross-layer join later.
3. **The Lake doctrine** (decision row amending #19): `data/lake/<dataset>/
   date=YYYY-MM-DD/` partitions, gz-JSONL (greppable, hand-openable — **no
   Parquet/DuckDB now**; revisit only when a named consumer outgrows linear
   scans, Mac-side first), atomic tmp-rename writes, **only ingestion
   writes**; `brain_map.db` remains the only writable memory.
4. **Deals-tape census + immutable raw snapshots**: per-day census row (row
   count, distinct clients/tickers, unmatched-group %, canonicalization-miss
   candidates) on the ops card; hash+archive each raw NSE payload so silent
   republication becomes a visible event. Every affinity advisory carries
   `n_deals` + window + coverage caveat inline. (The disclosure tape is
   censored — >0.5%-of-equity only — and name-mangled; make its lies legible
   before months of accumulation harden into false beliefs.)
5. **15-min candle persistence tap**: `live_bridge`'s `CandleAggregator`
   already computes candles and throws them away; append them to the lake
   with a mandatory `source=poll` fidelity field and explicit gap markers
   (poll-sampled candles are not true OHLC — label, never interpolate).
   **Websocket transport swap is deferred** until ≥2 clean weeks after the
   Monday go-live; capture-only, hard-gated from any intraday decision loop.
6. **VM telemetry, not VM upgrade**: mem/CPU/swap on the nightly health card.
   Resize e2-micro→e2-small (~₹1,200/mo) only when a trigger fires (sustained
   >80% mem, OOM kills, or the websocket consumer actually landing) — with a
   written post-restart checklist (#47's OAuth-scopes lesson).

## 4. Phase 1 — The backfill: seed the moat (days; Mac-side)

Converts the entity-affinity layer from "wait 6 months" to statistically
alive within days. Order matters:

1. **Client alias table FIRST** (`config/` hand-editable): without it,
   `canonicalize_client` fragments one fund across renamed spellings and
   concentration silently under-fires — the backfill would "work" and be
   quietly wrong. Near-duplicate candidates surface on the census card for
   human review; **nothing auto-merges** (false merges fake concentration).
2. **As-of edge projection**: `entity_affinity`'s graph projection gets an
   as-of `valid_from` so replayed 2023 links don't read as born-today; a
   2020-dead entity decays out on first sweep, a live-through-2026 entity
   ends fresh. (Verified by test.)
3. **3-year NSE bulk/block archive crawl** (not 5+ — pre-2021 is mostly
   delisted-entity noise against a survivorship-unaware group map): run from
   the **Mac** (residential IP; NSE blocks datacenter ranges), ~1 req/2s,
   raw CSVs cached to disk so it is one-time; parsed JSONL ships to the VM.
   Per-era CSV format handling.
4. **FII/DII daily flows** (first sibling stream; one row/day, trivial
   parser, backfillable years) — gives the macro layer the flows dimension it
   lacks: crude/USDINR say what SHOULD move indices; FII net says who IS.
   Logged as a #60 extension. Each further stream ships **serially**, gated
   on the previous running clean 2+ weeks in ops_monitor: **delivery %**
   (lake-only; enters brain_map only as derived discrete events like
   "high-delivery accumulation day"), then **insider/SAST** (CSV summary
   reports, watchlist tickers, discrete+rare+strong → real events rows), then
   **quarterly shareholding** (calibrator, not signal). A stream that can't
   run clean gets killed, not nursed.
5. **Earnings calendar** (deterministic, no-LLM): `days_to_results` attached
   at proposal time (#50 capture pattern) + a results-blackout *advisory*
   line. The announcements-LLM pipeline is **deferred** (strict category
   whitelist + cross-stream dedupe when it comes; frames land as events only,
   never edges, per #34).
6. **Gap-playbook data**: add US/global index + GIFT-Nifty metrics to
   `macro_tracker` via verified Dhan ids (#2 says Dhan covers these natively;
   ids verified against the scrip master, never guessed).

## 5. Phase 2 — The substrate: capture everything, judge nothing (days)

1. **Evidence Snapshot** (the panel's unanimous "everything is blocked on
   this"): one canonical Evidence record {layer, direction, strength,
   horizon, as_of, provenance} emitted by thin adapters over every existing
   layer, stamped by **one shared function** into every proposal from every
   path (headless, terminal, simulator) — coverage gaps would silently bias
   every downstream reliability stat. Layers with nothing to say record
   **explicit abstention, never a guessed neutral**. Persisted to an additive
   `evidence_snapshots` table keyed by journal_ref.
2. **daily_context Market-Frame table**: one row per trading day joining
   VIX band, trends, macro matrix, news sentiment, deals/affinity state,
   flows — the cross-layer join surface that makes motif mining possible at
   all. As-of backfilled, NULL-honest (#50), with event-explosion semantics
   documented in DATA_CONTRACT.
3. **Decision receipt** on every journal entry: each layer's exact inputs,
   output, and a changed-outcome flag; `python3 -m src.explain <short_id>`
   pretty-prints why a trade fired. Drift alarm replays the receipt's own
   **frozen inputs** through pure `build_proposal` (never re-fetches — the
   simulator's synthetic world would false-alarm constantly).
4. **Timelock leakage tests**: every discovery-facing function takes explicit
   `as_of`; a harness mutates all data after T and asserts identical output.
   One leaked feature voids the whole harness — this is the load-bearing
   wall, built first, enforced in the suite like #30's import guards.
   Retrofit `entity_affinity` now while it's a day old.
5. **T+1-open execution-timing contract** in the simulator for every
   EOD-sourced signal, with `signal_age_hours` on the receipt.
6. **Graph provenance + authority caps**: additive `source` column on
   `graph_edges` (outcome_derived | llm_mined | affinity_projected |
   simulated | miner); `vol_bridge`'s net_signal sums **outcome_derived
   only**; llm_mined caps at confidence 0.5 and renders visibly tagged; a
   nightly no-LLM audit re-checks outcome_derived edges against the outcomes
   table (evidence floors before stamping; **λ=0 loss edges are flagged for
   human review, never auto-invalidated** — the loss-permanence invariant).

## 6. Phase 3 — Composition law + the full-autonomy supervision contract (days)

1. **The composition law** (new DECISIONS row + enforcing test): one named
   `primary_driver` per proposal; macro/news/affinity/flows emit
   CONFIRM/NEUTRAL/VETO. Every layer starts **ANNOTATE-only**; veto power is
   *earned* per-cell via the registry (§7) and **granted by a human-approved
   candidate card** (evolution-style, #49) — never auto-armed. A vetoed
   proposal still journals (`advisory_flagged`) so the hypothetical tracker
   scores the veto class itself.
2. **Descriptive alignment line ships NOW** on every proposal card: macro
   bias for the underlying vs proposal direction + news sentiment sign,
   stated as fact with no statistical claim ("evidence, not gate") — answers
   the macro-confirm ask on day one while the earned version accumulates.
3. **Sector-level macro map**: extend INDEX_IMPACT_WEIGHTS with the owner's
   four links (crude→OMC/paints/aviation/tyres; USDINR→IT/pharma;
   gold+VIX risk-off; overnight-gap regime tag) as hand-authored **priors**
   in config, each carrying its own hypothesis entry so the harness scores
   them like everything else.
4. **Red-flag event classes** in the news parser (fraud/auditor exit/pledge
   spike/SEBI action) → VETO-class advisories (annotate-only until promoted).
5. **Smart-money confirmation rules** (the owner's three): affinity
   advisories gain confirmation fields — next-days price behavior, insider
   co-occurrence, delivery-% spike — surfaced inline, feeding the sequence
   miner as antecedents (§7).
6. **The supervision contract (full autonomy on paper, #53 extended —
   logged decision):**
   - Auto-approval routes through the same `decide_pending` path; nothing
     bypasses the margin gate or journaling.
   - **Engagement tripwire**: if the human takes zero manual actions in N
     trading days while auto-approve is ON, post "brain is unsupervised" and
     pause NEW auto-approvals until any human action (non-negotiable #2 is
     only real if the human is actually in the loop).
   - **Daily digest** (one card: what fired and why, per the receipts) +
     **weekly deep-dive** (pattern performance, validations/kills, equity
     curve, drought metrics).
   - **Veto-by-name**: replying with a pattern/channel name quarantines it
     (registry state flip; reversible; loudly listed on every health card).
   - **Advisory budget**: daily cap, lowest-confidence dropped first;
     below-floor channels get **proposed-mute** on the health card (30-obs
     floor, one-tap confirm, one-command reversal) — never auto-muted.

## 7. Phase 4 — The proving harness (days–week; the product's spine)

1. **`stat_gates.py`** — the single home of every noise-control primitive:
   Wilson lower bounds (never point estimates), Benjamini–Hochberg (q<0.10),
   circular-block permutation nulls, support floors (15 itemset / 8 sequence
   / episode-counted affinity), split-window stability (extracted from #55 so
   evolution imports it too), real-vs-sim separation policy, `shadow:`/`auto:`
   prefix exclusions. Ships with the **noise-injection suite**: feed the full
   pipeline pure-noise histories and assert the end-to-end false-promotion
   rate ≤ q ("this brain does not see faces in clouds", as a regression test;
   25-seed smoke in CI, 500-seed nightly).
2. **Pattern registry + lifecycle**: every mined hypothesis (frozen JSON
   predicate, minted_at, mining-run denominator) with states CANDIDATE →
   TRIAL → VALIDATED → LIVE_ADVISORY → QUARANTINED (+ **INSUFFICIENT_N**,
   distinct from tried-and-failed). `auto:*` tags are structurally excluded
   from miner inputs (no tautological rediscovery). Existing #26/#33 surfaces
   keep rendering, stamped with registry state inline — new discovery-brain
   outputs are gated, shipped functionality is not silenced.
3. **Walk-forward trial** through the real simulator: mined on window A,
   promoted only on disjoint window B (5-day embargo), `trial:<sha1>`
   idempotent refs; promotion = **superiority over the matched strategy
   family's base rate** (not >50% — the sim corpus wins 79% baseline) + Wilson
   LB + positive avg R; "new" evidence defined by **market date**, not run
   date. Validation window = all out-of-discovery data to date (grows
   weekly), so floors are reachable, with a 6-month zero-promotion governance
   tripwire (human review session, drought visible on the dashboard — never
   silent threshold erosion).
4. **Shadow tracking** of live firings in `brain_map` shadow tables resolved
   by plan_tracker's pure helpers — **never journal.jsonl** (a shadow entry
   would arm the cooldown and block real proposals; runtime-spy tested).
5. **Stability battery** (combined-window: stability is a veto, in-sample
   rejection can only make the harness stricter): split-halves,
   leave-one-slice-out (n≥10 per slice else "untestable"), concentration veto
   (>50% of R inside one 10-day window = one event wearing a pattern
   costume).
6. **Validation is a lease**: adaptive expiry (max(90d, time to K new
   matches)), CUSUM drift monitor + Wilson-crossing tripwire, quarantine →
   re-trial on monotonically growing evidence, twice-quarantined = DEAD
   (stored forever, lineage rule).
7. **Placebo patterns** in a parallel corrected batch (never inflating the
   real BH denominator): the harness's realized false-discovery rate becomes
   a monitored dashboard number with its own CI.
8. **Honest evidence dashboard** (`/dashboard/patterns` + weekly Discord
   digest): per-pattern card led by a plain-English verdict, the
   discovery→validation→live **degradation staircase as the default view**,
   Wilson CI bars, real/sim strata, drought metrics, placebo rate.

## 8. Phase 5 — The discovery brain (weeks; gated on substrate accumulation)

Sequenced last deliberately: miners without accumulated evidence snapshots
would sit provably idle or rediscover the entry gates (#50's exact failure).

1. **Layer reliability scoreboard** (real/sim strata never pooled silently;
   hierarchical fallback; n-floors) — standalone CLI trust report first.
2. **Log-odds combiner** (advisory): technical evidence from RAW pre-tuner
   weights; Brain-Map memory as prior, never a parallel layer (no
   triple-counting of the same outcomes); staged Discord card — evidence grid
   with n now, **no net P(win) headline and no auto-approve floor until the
   combiner passes its simulator promotion gate**; tuner's fate post-switch
   decided in the promotion decision.
3. **Macro gate** reusing resonance's horizon-blend (one implementation);
   soft CONFLICT→ hard veto only via human-approved candidate per cell.
4. **Co-occurrence miner** (Apriori over daily_context ⨝ outcomes;
   stratified base rates so it can't rediscover the VIX gate; real and sim
   corpora mined separately; `sim_only` flags; near-zero early survivors is
   correct output, reported honestly).
5. **Sequence miner** ("A then B within N days" — the owner's thesis is
   inherently sequential): permutation nulls; antecedents restricted to
   deterministic sources (deals advisories, macro tags — never
   news-sentiment tags, honoring #34); registry-only until validated; edges
   written under a distinct miner namespace only after promotion (logged #34
   amendment).
6. **Counterfactual structure pricing** in the simulator (price the adjacent
   structures the matrix refused, same synthetic world, `counterfactual`
   flag) — the data generator without which any strategy selector can only
   re-confirm its prior. Then the **pattern×strategy evidence view** (Wilson
   bounds, ≥5 real resolutions before rendering), and only after the first
   pattern validates, the **selector duel**: champion/challenger paired on
   disagreement days (≥30-day floor), drawdown regression gate, dethroning
   lands as a human-approved candidate (#49 ritual) — never auto-applied.
7. **Skeptic v3**: realized-vs-implied vol spread, distance-to-support,
   days_to_results (all as-of computable today; no confluence-log-odds
   circularity). Ablation study deferred until layers have ≥50% non-NULL
   coverage — running it now would "prove" the new layers earn zero, an
   artifact the owner would misread.

## 9. The kill list (locked refusals — one DECISIONS row each)

- **No embeddings / vector store / semantic similarity** over trading state:
  at hundreds of outcomes, nearest-neighbor in embedding space is a
  random-neighbor generator wearing math; patterns stay countable,
  inspectable predicates. Revisit only at tens of thousands of real trades.
- **No auto-reweighting meta-learner over the layers**: per-layer trust at
  this scale = sample sizes in the teens chasing the last regime; trust
  changes are rare, human-gated events via the evolution candidates path.
- **No intraday/streaming unified brain**: EOD sources update once a day —
  "live" consumption of them is theater; the dumb-fast live path / smart-slow
  nightly path boundary is the system's cleanest working seam. (Intraday
  *entry timing* for EOD-decided trades rides the existing live loop and is
  in scope.)
- **No immediate VM resize, no websocket swap this week, no Parquet/DuckDB
  now, no announcements-LLM yet** — each is trigger- or evidence-gated above.

## 10. Infra & spend (signal-per-rupee order)

| Item | Cost | Gate |
|---|---|---|
| Everything in Phases 0–4 | ₹0 (free data + engineering) | ship |
| Disk for the lake | single-digit GB | none |
| VM e2-small resize | ~₹1,200/mo | telemetry trigger (mem>80%/OOM/websocket lands) |
| Websocket transport | ₹0 | ≥2 clean weeks post go-live + resize landed |
| Mac mini M4 Pro 64GB (70B-class local LLM) | ~₹2–2.2L one-time | a *measured* quality gap (schema-gate kill rate, frame spot-check disagreement) — data before compute: an 8B model over 3 backfilled years beats a 70B model over a thin corpus |

## 11. What this delivers, in the owner's terms

- **"Find patterns"** → the miners (§8) over the joined substrate (§5),
  with the registry/harness (§7) guaranteeing every surfaced pattern already
  survived out-of-sample — and silence reported honestly as silence.
- **"Check which strategy"** → counterfactual pricing + evidence view +
  human-gated duel (§8.6), with the #42 matrix as the never-insane prior.
- **"Confirm with macro (crude, gold…)"** → descriptive alignment line now
  (§6.2), sector-seeded priors (§6.3), earned per-cell veto (§8.3), gap
  playbook via global indices (§4.6).
- **"News sentiment folded in"** → catalyst/red-flag event classes (§6.4)
  under the confirm/veto law, days_to_results everywhere (§4.5).
- **"Smart money"** → 3-year backfilled entity-affinity + flows + delivery +
  insider triangulation (§4), his three confirmation rules inline (§6.5),
  the sequence miner testing distribution→drawdown formally (§8.5).
- **"Full autonomy"** → §6.6's supervision contract: the brain trades paper
  by itself, the human stays sovereign through digests, tripwires, and
  veto-by-name.

## 12. Suggested build order for the sprint

Phase 0 tonight/tomorrow (hours, pure capture). Phase 1 through the weekend
(backfill is Mac-side and one-time). Phases 2–3 next (substrate + law +
supervision). Phase 4 immediately after — before any miner exists, the
harness must. Phase 5 lands as data accumulates; its miners are the LAST
course, not the third.

Each phase ships alone: tested offline, committed, deployable without the
later ones. Every "logged decision" noted above gets its DECISIONS.md row in
the same commit as the code it governs.
