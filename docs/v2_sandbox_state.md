# V2 SANDBOX — STATE DUMP

**Written 2026-08-16 at a context boundary, for the next session.** This is
the R&D sandbox only. V1 is FROZEN and untouched by everything below; the
sandbox has no cron line, no live importer, and tests enforce both.

> **Read this before touching anything here.** The sandbox's whole value is
> that it refuses to produce a finding it cannot support. Several results
> below LOOK like signals and are explicitly marked as not-signals. Do not
> promote them by re-running them with a looser gate.

---

## 0. The one-paragraph summary

Three ingestion lanes and two study engines, all isolated. The only
**out-of-sample-verified** result so far is that **insolvency and default
filings are followed by negative median returns** (see §5). Everything else
is either untested, under-powered, or explicitly labelled insufficient. The
binding constraint across the whole programme is **event count, not code**.

---

## 1. Where everything lives

| Path | What it is |
|---|---|
| `src/research/event_study_simulator.py` | Keyword + dated event studies |
| `src/research/geo_revenue_extractor.py` | Annual-report PDF → geography & plant states |
| `src/ingestion/sandbox/credit_monitor.py` | Credit stress from the events lake |
| `src/ingestion/sandbox/macro_shocks_v2.py` | ONI (El Niño) + election shock clocks |
| `src/ingestion/sandbox/deep_history.py` | yfinance deep prices + steel proxies (**MAC-ONLY**) |
| `src/ingestion/sandbox/election_calendar.py` | Wikipedia-API election scraper (**MAC-ONLY**) |
| `config/geo_revenue_exposure.json` | The exposure map (§2) |
| `config/india_election_calendar.json` | 32 sourced elections (§3) |
| `config/macro_securities.json` | Verified MCX ids incl. the metals (§6) |
| `tests/test_research_sandbox.py` | 44 tests, incl. the isolation guard |

**Isolation rule (a test enforces it):** nothing in `src/` outside
`src/research/` and `src/ingestion/sandbox/` may import any of the above,
and neither package may appear in `scripts/setup_cron.sh`.

---

## 2. `config/geo_revenue_exposure.json`

```json
{"exposures": {"<TICKER>.NS": {
   "exposures": [{"region": "...", "kind": "india_state|country|region_bloc",
                  "share_pct": null, "driver": "the MECHANISM",
                  "confidence": "high|medium|low",
                  "basis": "operational_presence",   // optional, see below
                  "source": "..."}],
   "shock_sensitivity": ["monsoon","election","currency","tariff",
                         "regulation","conflict"]}}}
```

**Two kinds of state row, and confusing them is a category error:**

* `basis: operational_presence` — **where the assets are**, extracted from
  the annual report's facilities pages. `share_pct` is ALWAYS null; a
  presence is not a revenue share. 15 such rows, 6 upgraded hand estimates.
* no `basis` — a hand-typed **revenue** estimate, `confidence` low/medium.

**The finding that forced this split: Ind AS 108 discloses revenue by
COUNTRY and never by Indian STATE.** State-level revenue cannot be
extracted from annual reports — ever. Operational presence is the available
substitute and is arguably the better variable for an election or riot: a
strike in Haryana stops MARUTI's line whatever fraction of revenue Haryana
buys.

`shock_sensitivity` is the **join key** — `macro_shocks_v2.tickers_for_shock("monsoon")`
returns the basket a study runs on.

---

## 3. `config/india_election_calendar.json`

**32 state assembly elections, 2019–2026**, sourced from the **Wikipedia
API** (one page per election). Row shape:

```json
{"date": "2024-11-20", "result_date": null, "state": "Maharashtra",
 "kind": "state_assembly", "source_page": "2024 Maharashtra Legislative
 Assembly election", "verified_against_eci": false}
```

* `date` is the **LAST poll date** of a multi-phase election — the market
  event is the resolution, not phase one.
* `result_date` is **null unless the page stated it.** Counting day is
  conventionally a few days later, and "conventionally" is not a date.
* **`verified_against_eci` is `false` on every row.** Wikipedia is
  machine-readable where ECI is scattered press-note PDFs, but it is a
  SECONDARY source. Confirm any date that a conclusion rests on.
* By-elections are **excluded** — they change no government and would pad
  n with non-events.
* ⚠️ The category endpoint is flaky under load (HTTP 429). Two years came
  in on a second pass and were **merged, not overwritten**. If a year looks
  missing, merge — do not re-run `--apply`, which replaces the file.

---

## 4. The two study engines

### `event_study_simulator.py`

```bash
# keyword over the events lake (2,612 partitions, 2019→2026)
python3 -m src.research.event_study_simulator --keyword Insolvency \
    --from 2019-10-01 --to 2026-06-30 --windows 5,10 [--sector FINANCIALS] [--json]
```

`run_dates(events, tickers=...)` is the **date-driven** twin for shocks
(elections, monsoons). It reuses the *same* forward-return machinery
deliberately — a tool with two definitions of "forward return" will
eventually disagree with itself.

### `deep_history.py` — **MAC-ONLY**

```bash
python3 -m src.ingestion.sandbox.deep_history --fetch          # 6 series, 20y
python3 -m src.ingestion.sandbox.deep_history --el-nino-study  # year-level test
```

Fetches `^CNXFMCG` (2011→), `ITC.NS`/`HINDUNILVR.NS` (2006→) and the steel
proxies. **Never run on the VM**: yfinance is a Mac-lane dependency, Yahoo
blocks datacentre IPs, and `src/` proper must stay free of a yfinance
import (the import is function-local for exactly this reason).

### Data the studies read

| Lake | Depth | Where it actually is |
|---|---|---|
| `data/lake/events/` | 2,612 partitions, 2019-01→2026-08 | VM (copied to Mac 08-16) |
| `data/lake/bhavcopy/` | 1,771 sessions, 2019-09-30→2026-08 | **Mac** (VM has ~101) |
| `data/lake/macro/ONI.csv` | 918 seasons, **1950→2026** | both |
| `data/lake/macro/*_DEEP.csv` | ~5,000 rows each, 2006→2026 | Mac |

⚠️ **The two big lakes live on different machines.** A full-history run
needs one copied first; `--lake-root` exists for that.

---

## 5. THE STATISTICAL GUARDRAILS — non-negotiable

1. **HYPOTHESIS FIRST.** State the expected direction before running. A
   result explained after the fact is a story, not a finding.
2. **THE MEAN IS A LIE ON THIS DATA.** Report **median + hit rate**.
   Measured: insolvency filings show a **+4.04% mean and a −3.22% median** —
   one shell running +1,021% flips the sign. `mean_median_diverge` flags
   every window where mean and median disagree; the render leads with the
   median for this reason.
3. **n-COUNT GATES, BOTH OF THEM.** `MIN_SAMPLE = 10` observations, **and**
   ≥3 distinct event dates for a dated study. 200 ticker-days off 2 event
   dates is one fortnight wearing a large n. For year-level tests the gate
   is ≥5 independent years.
4. **SURVIVORSHIP BIASES UPWARD, AND MATERIALLY.** 37 of 234 symbols in the
   insolvency study have **no bars at all** — they are the outright
   delistings (AMTEKAUTO, EDUCOMP). Even the median is the *survivors'*
   median. Not corrected; always stated.
5. **NO p-VALUES.** Event windows overlap and are autocorrelated; a t-test
   would look like significance without being it. (Auto-Discovery already
   paid for this lesson.)
6. **OUT-OF-SAMPLE.** Train 2019–2023, verify 2024–2026. A signal that does
   not survive the split is not a signal.

### The only result that has passed all six

| Keyword | Split | Median 5d / 10d | Hit |
|---|---|---|---|
| Insolvency | TRAIN 2019-23 | −2.50% / −3.03% | 39% / 40% |
| Insolvency | **VERIFY 2024-26** | **−4.94% / −5.12%** | 31% / 32% |
| Defaults on Payment | TRAIN | −0.67% / −1.52% | 44% / 43% |
| Defaults on Payment | **VERIFY** | **−3.36% / −4.51%** | 32% / 35% |

Sign survives and the effect strengthens out of sample. n=1,863 / 1,842.

### Results that are NOT findings — do not quote these

* **El Niño → FMCG.** HINDUNILVR El Niño years median **−1.90%, hit 0/3**
  vs +12.26% / 75% otherwise (gap −14.16%); ITC gap −2.71%; NIFTY FMCG
  contradicts at +2.49%. Direction is mechanistically coherent (most-rural
  worst) but **there are only THREE distinct El Niño monsoon years in
  2006–2026** (2009, 2015, 2023). 0-for-3 is three coin flips: 1-in-8 by
  chance. Verdict on all three: `insufficient_independent_events`.
* **Elections.** Calendar sourced; **study never run.**

---

## 6. Proxies and instruments being tracked

| Name | Instrument | Where | Note |
|---|---|---|---|
| CRUDE | MCX `560977` FUTCOM | `cross_asset` | monthly roll |
| GOLD_INDIA | MCX `483079` | `cross_asset` | bi-monthly roll |
| **COPPER** | MCX `568831` | `cross_asset` | **monthly roll** |
| **ALUMINIUM** | MCX `568830` | `cross_asset` | **monthly roll** |
| **ZINC** | MCX `568836` | `cross_asset` | **monthly roll** |
| **SLX / MT / XME** | Yahoo ETFs & equity | `deep_history` | **steel PROXY** |

* All MCX ids **verified row-by-row** against `api-scrip-master-detailed.csv`
  (210,446 rows). Next ladder rungs for each metal are recorded in
  `config/macro_securities.json` so a roll is a lookup, not a re-derivation.
* ⚠️ **Three monthly-roll metals means `CA-410` (expired contract) now fires
  up to 4× more often.** `stale_instruments()` names it; it is not a crash.
* **STEEL is deliberately absent from `cross_asset`.** MCX lists only
  `STEELREBAR` (construction rebar), which does not track the flat/HRC
  steel driving TATASTEEL or JSWSTEEL. A wrong proxy is worse than an
  absent one (#78). SLX/MT/XME are the substitute and are **equities that
  co-move with steel, not steel prices** — never read an ETF close as an
  HRC quote.
* Steel proxies live in `deep_history`, NOT `cross_asset`, because
  `cross_asset` is the **Dhan door** and one market-data source per door is
  a house rule.

---

## 7. Known gaps, in the order I would attack them

1. **Event count is the universal blocker.** Not code, not compute. El Niño
   has 3 usable years; elections have 32 dates but the study is unrun.
2. **Elections study never executed** — the obvious next run, using
   `run_dates()` with `tickers_for_region(state)` as the basket.
3. **Consensus projections: no free source.** Compare our projections to
   **realized** results from `data/lake/financial_results/` instead — a
   stronger test and fully available today.
4. **Bond/credit is data-starved**: only 14 `RATING_ACTION` and 22
   `ISSUANCE` filings in 2 years. Agency pages would be a new crawler.
5. **Geo revenue extractor yield is ~40%** — 4 of 10 tickers surfaced a
   geography page, only quantified Ind AS lines promoted. The limiter is
   marker vocabulary across filer styles, **not** the page cap (verified:
   COALINDIA's full 371 pages were scanned).
6. **RELIANCE and TATAMOTORS have no annual report held** — both extractors
   skip them.
7. The Mac's copy of `data/lake/events/` is **untracked local data**, not
   synced by anything. Decide whether to wire it or drop it.

---

## 8. If you change one thing, do not change these

* The isolation guard and the cron-absence test.
* Median-first reporting and the two n-gates.
* `share_pct: null` meaning "material but unquantified" — **never 0**.
* `verified_against_eci: false` until someone actually checks ECI.
* The `basis: operational_presence` marker separating asset location from
  revenue share.

---

## 9. Studies run 2026-08-16 (all three: NOT findings)

Read §5 first. All three studies below were run with hypothesis-first,
median + hit rate, both n-gates, survivorship stated. None passed OOS.

* **Elections × operational presence** (`run_dates`-style, scratch): 17 of
  32 elections touch a `basis: operational_presence` name (only 5 names
  carry state rows), n=27 ticker-days / 14 dates. Hypothesis (vol premium
  for exposed names) REFUTED: treated median |r| 1.56%/2.86% vs control
  1.84%/3.35%. Directional TRAIN 10d +2.40% hit 67% → VERIFY +0.35% hit 50%.
  Calendar issues seen: `2021-09-30 West Bengal` looks like a by-poll date;
  Karnataka-2023, Gujarat/UP/Punjab-2022, Kerala/TN-2021, Bihar-2020 absent.
* **Steel-proxy shocks** (SLX 5-session ≥|8%|, de-clustered, 2011→2026):
  up-spikes 51 dates — NIFTY METAL TRAIN −0.74%/−0.51% → VERIFY +2.29%/+1.11%
  (sign flips, fails); METAL−AUTO spread +0.85/+0.91 → +0.99/+1.14 hit ~60%
  (sign survives, effect ⅓ of window noise: WATCH, not finding). Down-spikes
  44 dates — AUTO 5d −1.40 → −1.04 weakly survives; METAL −0.03 → −3.02 (no
  train support). Autos move WITH producers in single names → beta, not a
  steel-cost transfer. **MCX COPPER/ZINC hold zero history** (ids added
  today) — unusable as a shock series until ≥3y accumulate.
* **Earnings reaction** (`src/research/earnings_reaction.py`): see the
  MODULES.md row. Lake depth is 5 quarters, so all 201 YoY events are in
  2026 — **no train/verify split possible**. Bottom quartile −3.42%/−5.24%
  hit 28%/17% (excess vs NIFTY −2.17/−5.23); top quartile −0.72%/+0.87%
  (no positive edge; Q3 was as negative as Q4 in season A). Season B 5d
  bottom quartile flipped to +2.67% (n=12), July 10d windows truncated at
  the 2026-08-05 bhavcopy end. Re-run when the lake holds Q4-FY27 filings
  (Apr-2027) — that is the first genuine second year.
