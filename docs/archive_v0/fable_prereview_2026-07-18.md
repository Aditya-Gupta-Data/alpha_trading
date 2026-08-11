# Fable Pre-Review Brief — Sprint of 2026-07-13 → 07-17

**For:** Fable, full-codebase review #2 of the ~2–3 budgeted (per the 2026-07-15
compartmentalization goal).
**Prepared:** 2026-07-18 (Saturday freeze).
**Repo state at hand-off:** working tree CLEAN; `HEAD` = `0cafdb6`; `origin/main`
and the VM = `6d89eb4`; **3 commits sit on the Mac only**. Suite: **1131 collected**.

## What this review must produce (owner's protocol)

1. A design check of the week's sprint against the lean 7-department structure.
2. **Refinement of `ARCHITECTURE.md` itself** — mandatory, not optional. Section 4
   below lists the specific holes found. The stated goal is that after ~2–3 full
   reviews, all future reviews go LOCAL to one department.

## Read the facts here, not the folklore — three corrections up front

The working notes going into this weekend contained three claims that the code
contradicts. Please review against the corrected version.

| Claim in circulation | What the repo actually shows |
|---|---|
| "The knowledge graph logger shipped in the `6d89eb4` rapid deploy." | **It did not.** `knowledge_graph_logger.py` + `equity_shadow_proposer.py` landed in `6507827`, which is **after** `6d89eb4` and **unpushed**. The Shadow Equity Engine is **not live and not on the VM.** |
| "Phases 3 & 4 are untracked drafts." | They are **committed** (`0cafdb6`) with a clean tree, and covered by 29 hermetic tests. They are unwired and undeployed — which is not the same as untracked. |
| "Origin / Mac / VM are identical." | True as of `6d89eb4`; **three commits landed after it.** See Section 5. |

---

# 1. The Live State — what is actually running on the VM (`6d89eb4`)

Deployed Friday 2026-07-17 ~13:35 IST by owner order ("rapid deployment"),
superseding the wait-for-Saturday hold. 20 files, +1995/−24. Suite 1088 green
after two deploy-blocking fixes.

### 1a. Dhan throttle (the DH-905 fix) — Department 1
`src/dhan_client.py` (+82): a proactive `_throttle()` — 1.1s minimum gap,
process-wide — placed in front of **all four** Dhan call sites. Retry-once is
kept underneath as the recovery layer, not the primary defense.
**Review question:** the throttle is process-wide state inside `dhan_client`,
while Department 1's declared manager is `dhan_guard.SafeDhanClient`. Is pacing
sitting one layer *below* the department manager the right seam, or does the
architecture want it at the guard?

### 1b. Regime filters — the only part of this sprint wired into a live decision
`src/analysis/regime_filters.py` (+100), composed in `market_loop.fetch_market_state`
and honored by `options_proposer.build_proposal` via an **additive `advisory=` kwarg**
(the vol_bridge pattern), fail-open throughout — absent data leaves the proposer
byte-for-byte unchanged, and all 56 existing options tests stayed green.
Two radars: (1) a smart-money/sector **VETO** on bullish index spreads when ≥2 of
the index's top-3 heavyweights show institutional distribution or the parent sector
isn't bullish; (2) **CRISIS regime** (VIX ≥ 25, abrupt spike, or a known `macro_shocks`
window) disables short-premium iron condors so only defined-risk debit spreads ride
the tail.
**This is the sprint's one live behavior change. It deserves the deepest scrutiny.**

### 1c. New analysis department (read-only, NOT wired)
`smart_money_trend` (+107), `sector_trend` (+134), `macro_shocks` (+120),
`institutional_alpha` (+55), `conviction` (+88).
**Flag:** `institutional_alpha` and `conviction` are documented in **neither
`MODULES.md` nor `ARCHITECTURE.md`**, and nothing imports them. `conviction`
describes itself as the score that "DRIVES dynamic position sizing (replacing
rigid risk tiers)" — a Department 3 capital-allocation claim with no manager,
no index row, and no consumer. See Section 4c.

### 1d. New ingestion clerks (capture-only, NOT wired)
`corporate_events` (+208), `flows_backfill` (+190), `fundamental_parser` (+246),
`intraday_tracker` (+133). Plus `config/sector_universe.json` (7 sectors → parent
index + ~70 constituents) and a 361-line watchlist expansion.

### 1e. P1 slippage fixes — Department 2/3
`options_proposer` (+38/−) and `plan_tracker` (+50/−24).

### 1f. The two deploy-blocking data catches (ledger Issues 14/15)
An "all tests passed" claim was wrong: the suite was 1087 green **/ 1 red**.
LTIM has no NSE listing in Dhan's scrip master; TATAMOTORS demerged (TMPV id 3456
= the old TM id, passenger/EV; TMCV id 759782 = commercial vehicles). Fixed → 1088.
**Systemic question for Fable:** the watchlist encodes broker-specific security IDs
that silently rot on delisting/demerger. Is there a department that should own
scrip-master reconciliation as a scheduled check, rather than discovering it at
deploy time?

---

# 2. The Staged State — `next_gen_engine/` Phases 1 & 2

**Status:** committed (`326315e`), **imported by nothing in `src/`**, unscheduled,
undeployed. Every module is pure/injectable; 29 hermetic tests
(`tests/test_next_gen_engine.py`) are collected by the main suite so the folder
can't silently rot.

**The anti-orphan rule is already enforced in-repo** — learned from the discarded
`pattern_registry.py`/`trial_runner.py` duplicate. Every module's header and the
folder README name a CANONICAL `src/` merge target. At deploy the logic moves INTO
that target; this folder must never become a second implementation.

### The Department 3 mapping — with one correction to the framing

The brief was requested as "Phase 1 & 2 → Department 3 (Risk & Capital)." **Three of
the four map to Department 3; `execution_algo` does not.** Please review it as a
Department 2 change.

| Module | Canonical target | Department | Notes for review |
|---|---|---|---|
| `portfolio_risk_manager.py` | `portfolio_manager.gate_headless_entry` | **3 (entry-side manager)** | Daily realized-loss circuit breaker, 3%, **entries only**, resets at the IST day boundary by construction. Sits **beside** the existing 10% trailing-drawdown halt — does not replace it. A boundary-rounding bug was caught by its own tests. **Review:** two independent halts now guard one gate. Does Dept 3 want one composed halt policy instead of a growing stack of them? |
| `wealth_flywheel.py` | `wealth_lock.sweep_on_settlement` | **3** | Turns the advisory sweep into a concrete PAPER order (whole GOLDBEES units from a live quote, honest cash residual). **Blocker:** the GOLDBEES security id must be scrip-master-verified before any live pricing — this is exactly the Issue-15 failure mode. |
| `trailing_stops.py` | `atr()` → `indicators.py`; loop → `live_bridge.py` | **splits 2 / 6** | Wilder-ATR chandelier ratchet; never widens; a data gap retains the previous stop. **Review:** this one module lands in two departments. Confirm that's the clean decomposition and not a seam smell. |
| `execution_algo.py` | `options_proposer._leg_fill` (decision #70) | **2 (DECISION), not 3** | Limit-chase paper fill (mid → walk to touch over 30s, tick-snapped; an honest MISS is never a phantom mid-fill) + protective-leg-first sequencing. **Refines the paper fill model only — there is no order routing anywhere.** The non-negotiable "no broker/order-placement import exists in `src/`" must survive this merge. |

---

# 3. The Danger Zones — Phases 3 & 4

These carry the architectural risk: **Phase 3 touches the LLM seam; Phase 4
introduces entirely new infrastructure.** I verified each module's claimed seam
against its code rather than trusting the README.

### 3a. `wisdom_extractor.py` — Phase 3 — **verified compliant**
Qualitative text (macro memo / transcript) → strict backtestable JSON frame
(`target_sector`, `direction`, `timeframe_days`, `volatility_regime`,
`fundamental_filters`, thesis, confidence). Unknown enums and out-of-range values
drop to **null** — never a value the backtester can't trust.

**Proposed manager: `src/text_intelligence.get_extractor()` (decision #74) —
already the actual implementation.** Verified at `wisdom_extractor.py:136-137`:
the import is function-local and lazy, the extractor is injectable, and there is
**no `anthropic` / `gemini` / `openai` import anywhere in the file.** The owner
constraint ("MUST route through text_intelligence, not a new LLM client") holds today.
**Deploy target:** `src/ingestion/wisdom_extractor.py`, beside `news_parser`,
writing frames to the lake — Department 1, as a permanent text_intelligence *client*.
**Review question:** what test-enforces that it never grows its own model call?
`equity_shadow_proposer` has a test-enforced import ban (it may not import
journal/portfolio_manager/options_proposer/notifier/brain_map). That pattern looks
like the right precedent here.

### 3b. `redis_pubsub.py` — Phase 4 — **the real decision, and it is the owner's**
`EventPublisher`/`EventSubscriber`, a versioned envelope, `alpha.<domain>.<event>`
channels, and an in-memory `FakeBroker` the logic is tested against. `redis` is a
lazy import at `redis_pubsub.py:57` and deliberately **not** in `requirements.txt`.

**Proposed manager: none — and that is the finding.** Every other department maps to
a single existing manager file. An event bus is **cross-cutting infrastructure**: the
README slots publish calls into `options_proposer` (proposal.created), `live_bridge`
(position.exited / quote.tick), and `portfolio_manager` (account.halted), with the
Discord bridge and lake writer as subscribers.

> **This is the sprint's biggest architectural risk and my main pushback.** Those
> publish calls would put a second outbound path inside three different department
> managers — and the non-negotiables currently guarantee **one Discord door**
> (`fire_broadcast`) and **one settlement path** (`plan_tracker`). A bus that lets any
> subscriber react to `position.exited` is, structurally, a way to grow a second
> settlement path without anyone editing `plan_tracker`. **Recommendation: do not adopt
> Phase 4 until the bus has its own department and manager, with a written rule that
> subscribers are annotate-only (#63) and may never close a trade or send a card.**
> The system is currently a single-box scheduler; the bus buys decoupling the owner
> has not yet needed to pay for. Fable: is EDA premature here?

### 3c. `dhan_websocket.py` — Phase 4 — **verified compliant, honestly a skeleton**
Binary header + ticker-packet decoders (`struct`, tested), `build_subscribe_frame`,
`KeepAlive` ping/pong math, `backoff_schedule`. `websockets` is lazy-imported on the
run path only (`:203`); the async loop is a documented `NotImplementedError`.
**Verified:** the token comes from `src.token_provider.get_token` (`:184`, function-local)
— it reuses the single token seam and does **not** create a second credential source.

**Proposed manager: `src/dhan_guard.py` (`SafeDhanClient`) — Department 1.**
The non-negotiable is that `dhan_guard` is *the one hardened door to all market data*.
A WebSocket feed landing at `src/ingestion/dhan_feed.py` as the README suggests would
create a **second market-data door** that bypasses the guard's failure classification
and stale-quote voiding. **Recommendation: the feed must sit behind `dhan_guard`, or
Department 1's non-negotiable must be explicitly rewritten to say there are two doors
(pull and push) and what each guarantees.** Fable: which is cleaner?

---

# 4. Architecture Gaps — `ARCHITECTURE.md` refinement targets

**`ARCHITECTURE.md` self-stamps: "Current as of `dbd531f` (2026-07-15), suite 1006
green." Reality: `0cafdb6`, 1131 collected.** The document is 3 commits and ~125 tests
stale, and it predates the entire sprint.

### 4a. Zero coverage of the sprint — measured, not estimated
I grepped `ARCHITECTURE.md` for all 12 modules added this week:

`regime_filters`, `smart_money_trend`, `sector_trend`, `macro_shocks`,
`institutional_alpha`, `conviction`, `corporate_events`, `flows_backfill`,
`fundamental_parser`, `intraday_tracker`, `knowledge_graph_logger`,
`equity_shadow_proposer`, and `next_gen_engine/` — **all zero hits. Every one.**
Department 1's clerk list stops at `rss_ingester`; Department 2 has no mention that
`build_proposal` now honors an `advisory=` verdict.

### 4b. The `src/analysis/` package has no department at all
Six modules, ~600 lines, one of them wired into live options decisions — and the
7-department map has no home for it. **The single largest structural hole.** Is it a
new department, or Department 2's evidence layer? This must be resolved for the
"future reviews go local" goal to mean anything.

### 4c. Two modules documented nowhere
`institutional_alpha` and `conviction` appear in **neither index**. This breaks the
standing rule that `MODULES.md`/`ARCHITECTURE.md` update in the *same commit* as a
module change. `conviction` is the more urgent of the two: it claims to drive position
sizing, which is Department 3's job, from `src/analysis/` with no manager.

### 4d. Three next_gen merge targets aren't named in the department map
`wealth_lock`, `live_bridge`, and `indicators` all exist in `src/` and all appear in
`MODULES.md` — but **none is mentioned in `ARCHITECTURE.md`**. Three of the four
Phase 1/2 modules are therefore aimed at targets the department map doesn't
acknowledge. Adding those to their departments is a prerequisite for reviewing the
merge, not a follow-up.

### 4e. Department 4 doesn't know about the second knowledge store
`knowledge_graph_logger` writes an append-only JSONL store
(`logs/equity_shadow_journal.jsonl`) **deliberately kept out of `brain_map`** so
zero-capital telemetry can't skew the options engine's `query_similar_events`.
That is a sound call — but Department 4's map still says the brain map is *the*
knowledge store. It now has two, with a documented one-way ingest path reserved for
later. **Fable: is a mode-tagged column in `brain_map` the better long-term answer
than a parallel store?**

### 4f. `next_gen_engine/` isn't on the map
The folder has a good README and a `MODULES.md` row, but the department map never
says a staging ground exists or what the rules for leaving it are.

---

# 5. Deploy state — read before recommending anything ship

```
0cafdb6  next_gen_engine Phases 1-4 (Risk, Execution, Wisdom, Scaling)   ← Mac HEAD
326315e  next_gen_engine staging — blueprint Phases 1-2 (NOT wired)
6507827  Shadow Equity Engine — PAPER_TELEMETRY knowledge frame
--------------------------------------------------------------------  ← origin/main + VM
6d89eb4  Master deploy: Smart Money radar, P1 slippage fixes, Regime filters
```

The three Mac-only commits are all **unwired staging + telemetry** — nothing in them
changes live trading behavior, which is why the gap is safe to hold. The VM is
hands-free (`PAPER_AUTO_APPROVE=1` + Discord review cards).

**The Saturday protocol's night deploy is therefore a genuine decision, not a
formality:** shipping these three commits would push ~1000 lines of unwired code to the
VM for no live benefit. My recommendation is to **hold them until Fable rules on
Sections 3b and 4b** — the redis bus and the missing `analysis/` department are both
questions where the answer could change what the code should look like before it lands.

# 5b. Built during the freeze — `ceo_brief.py` (Department 6)

Built 2026-07-18 at owner request, **staged locally, not scheduled, not
deployed**. Suite 1174 green (43 new tests). Files: `src/ceo_brief.py` (new),
`tests/test_ceo_brief.py` (new), `src/notifier.py` (+4 lines: colour, title,
one branch arm), `scripts/setup_cron.sh` (entry #19, Mon-Fri 16:30 IST),
`MODULES.md` (2 rows).

One card, four sections (operations / issues / deployments / risk), routed
strictly through `notifier.fire_broadcast`. The notifier edit is additive and
follows the existing `eod_summary`/`portfolio_report` precedent: the manager
owns rendering, the job supplies pre-built fields.

**Three findings surfaced while building it — all three are department
questions, not module questions:**

1. **Dhan throttles are unobservable.** `dhan_client._throttle()` paces calls
   by *sleeping* and prints nothing, so there is no log line to sweep. The
   owner explicitly asked for "were there Dhan API throttles today" and the
   honest answer is that nothing measures them. The card carries the caveat
   rather than showing a green tick that means "unmeasured". **Proposed
   Dept-1 follow-up: have `_throttle()` count its sleeps and expose a daily
   total.** Needs Fable's ruling on where that counter lives — this is the
   same seam question as §1a.
2. **`ops_monitor.EXPECTED_JOBS` knows *whether* a job is weekday-only but not
   *when* it runs.** So a 16:30 card would have reported the 8 evening jobs
   (18:50–20:30) as "did not run today" every single day. `ceo_brief` now
   filters to jobs whose slot has passed, using its own `JOB_DUE_HOUR` map —
   which **duplicates the schedule that `scripts/setup_cron.sh` actually
   owns.** A drift guard test fails if the two ever disagree, but the real fix
   is one schedule source. **Fable: should `EXPECTED_JOBS` carry the hour?**
3. **`eod_summary`'s journal readers are underscore-private** (`_read_journal`,
   `_open_approved_spreads`, `_open_approved_equities`). `ceo_brief` reuses
   them deliberately — two reports computing "today's P&L" separately is how
   two reports start disagreeing — but that reuse is currently a reach-in.
   **Proposed: promote a small public read API on `eod_summary`.**

**Also worth a ruling:** `eod_summary` is documented at 15:30 IST but is **not
in `setup_cron.sh` at all** — it appears to be unscheduled on the VM. Either
the doc or the cron is wrong. Not touched during the freeze.

# 6. Suggested review order

1. **§4b** — where does `src/analysis/` live? Everything else depends on the answer.
2. **§1b** — `regime_filters`, the one live behavior change this week.
3. **§3b** — adopt the redis bus, or defer it? (My pushback: defer.)
4. **§3c** — one market-data door or two?
5. **§2** — the four merge targets, once §4d names them on the map.
6. **§5b** — the three Dept 6 / Dept 1 questions from `ceo_brief`.
7. **§4a/4c/4f** — rewrite `ARCHITECTURE.md` to current HEAD / 1174 tests.

# 7. Queued for Sunday (buffer day, per the weekly rhythm)

**SPAN margin audit** — deferred from the deploy by owner order, queued for Sunday
after this review. Not in scope here.
