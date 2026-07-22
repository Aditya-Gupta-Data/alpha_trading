# Phase 4 Plan (replanned 2026-07-04) — HISTORICAL

> **2026-07-22: this document is history, kept for the record.** The
> living master plan is `docs/cycle_hunter_plan.md` (replay-backward /
> validate-forward + the G1–G5 proof-gated budget + the Aug-8 schedule);
> the build queue index is `docs/planning_index.md`; the workflow rules
> are `docs/dev_workflow.md`. Much of what this file planned has since
> shipped in evolved form — see `ARCHITECTURE.md` for what exists now.

This replaces the step order in `aditrader-phase4-master-handoff-prd.md` (that
file's 4A/4B/4C only covered part of what we actually want). The replan is
built around a "Plan and scope" doc the user shared, which describes the full
end state: news + market data -> forecast -> trade plan -> rationale -> user
dialogue -> paper trade -> learning loop.

**Status: planned, not started.** Nothing below is built yet. We stopped here
on purpose, per the user's rule to confirm before starting 4A.

## Scope decisions locked in (2026-07-04)

- **Markets**: NSE/BSE equities AND options. Options support is real scope,
  not dropped, but pushed to Phase 5 (see below) since NSE options data is
  unreliable via yfinance and the real feed needs its own investigation.
- **Holding period**: swing (multi-day) first, matching the existing
  50/200-day SMA + 14-day RSI cadence. Intraday is an explicit later goal,
  not in v1 — it needs real-time data and a much faster loop.
- **News source**: free RSS / Google News, condensed by an LLM into a tiny
  sentiment JSON (score + 3-word driver). No paid news API for v1.
- **Dialogue loop**: not a dedicated in-tool chat feature for v1. The user
  already discusses theses with Claude directly in sessions with full journal
  context; structured levers (4A) capture the outcome of that conversation
  instead of rebuilding chat inside the app.

## Phase 4 steps, in build order

### 4A — Structured journal + risk levers  ✅ DONE 2026-07-04
Trade sessions capture pattern tags (e.g. `RSI_Oversold`, `Breakout`), a
stop-loss %, and a position size per trade (defaults from `config.json`,
adjustable). Old journal entries keep working untouched.
Files: `src/journal.py`, `src/trade.py`.
- `journal.new_entry()` now writes `risk_levers` {sl_pct, size} and
  `pattern_tags` on every record; the 3 new params are optional and fall
  back to config defaults, so old callers and old journal lines are fine.
- `src/trade.py` prompts for tags (always) and stop-loss % + position size
  (only when approved), with config defaults on Enter.
- **Carry-over into 4B:** the `size` lever is CAPTURED but NOT yet wired into
  execution — trades still use `prop["shares"]` from strategy.py. Risk-based
  position sizing is 4B's job. Same for `sl_pct`: stored, but no stop is
  enforced yet (that's 4C's automatic tracking).

### 4B — Full trade plans, not bare proposals  ✅ DONE 2026-07-04
`src/strategy.py` proposals grow into plans: entry rule, hard stop-loss,
take-profit target, invalidation criteria, risk:reward estimate, plain-English
rationale. Up to one primary + one alternative plan per signal.
New config levers: risk level (conservative/moderate/aggressive), max loss
per trade, max concurrent positions.
Files: `src/strategy.py`, `config.json`.
- Done: `config.json` + `src/config.py` gained `risk_level` (moderate) with
  a `risk_levels` map (0.5/1.0/2.0 % of portfolio risked per trade),
  `take_profit_rr` (2.0), `max_concurrent_positions` (4),
  `alt_entry_pullback_pct` (2.0).
- Done: `strategy.py` now builds full plan dicts (entry_rule, stop_loss,
  target, risk_reward, max_loss_rs, invalidation, rationale, variant) via
  `propose_plans()` → [primary, alternative] for buys, [exit plan] for
  sells. Position size is now RISK-BASED (risk budget ÷ stop distance,
  capped by default_investment_size and the Phase 3 rails) — a big change
  from "max affordable": positions are much smaller now, by design.
  `propose()` keeps the old contract (primary dict or None) so trade.py
  and journal.py work unchanged; plan fields ride along on the same dict.
- Done (second half, same day): `src/trade.py` now shows the full plan
  (entry rule, stop, target, R:R, max loss, invalidation) plus the
  alternative plan as context; the user's per-trade answers now DRIVE
  execution — position size sets the share count (clamped by the Phase 3
  rails), a custom stop-loss % moves the stop AND the target to keep the
  configured R:R. Levers are only asked on approved BUYs (sells are exits).
  `src/journal.py` now stores a `plan` block (stop/target/invalidation/…)
  on every entry — needed because new_entry() copies explicit keys, so plan
  fields would otherwise have been dropped; 4C's tracking depends on this.
- **Carry-over into 4C:** the alternative (limit) plan is display-only.
  Choosing/tracking conditional entries needs 4C's plan tracker.

### 4C — Automatic plan tracking  ✅ DONE 2026-07-04
Every generated plan (approved or skipped) gets a tracking record. A daily
check watches for stop/target hits and records real outcome metrics: P&L,
R-multiple, days in trade — replacing the current "did price move 7 days
later" scoring for plan-carrying entries.
Files: new `src/plan_tracker.py`, `src/review.py` (skip-guard added, not
rewritten), `src/trade.py` (sweep hook), `config.json`/`src/config.py`
(`plan_max_days: 30`).
- Runs LOCALLY, not on the VM (it must update `data/portfolio.json`, which
  deliberately lives only on the Mac): auto-sweeps at the start of every
  `python3 -m src.trade` session, or manually via `python3 -m src.plan_tracker`.
- Approving a BUY means approving its exits: when the stop or target later
  trades, the paper position is closed at that price automatically (bracket-
  order semantics). If the position was already closed another way, the
  outcome is recorded for scoring without touching the portfolio.
- Rejected plans resolve hypothetically (GOOD SKIP / MISSED GAIN), never
  touching the portfolio.
- Pessimistic tie-break: if one day's range covers both stop and target,
  the stop is assumed to have hit first. The entry day itself is never
  scanned. Plans older than `plan_max_days` (30) close at market ("time
  stop").
- Outcomes reuse review.py's verdict vocabulary (WIN/LOSS/GOOD SKIP/MISSED
  GAIN/flat) so the running scorecard totals count them with no changes.
  review.py now skips stop-carrying plan entries (tracker's job) but still
  blunt-scores old-style entries (e.g. the live ONGC buy) and sell decisions.
- **Carry-over into 4D-4F:** outcome now includes r_multiple, days_in_trade,
  pnl_rs, resolution — the exact metrics 4F's tuner needs. Alternative
  (limit) plans are still display-only: trade.py journals only the primary,
  so the tracker never sees them. Making alternatives selectable/trackable
  is still open.

### 4D — News & sentiment (isolated)  ✅ DONE 2026-07-04
Standalone `news_processor.py`: fetches free Google News RSS per watchlist
ticker, an LLM condenses headlines to a sentiment score (-5 to +5) and a
3-word driver, writes to `data/news_sentiment.json`. Core trading scripts
only ever read that JSON file, never raw articles.
Files: new `src/news_processor.py`, `data/news_sentiment.json` (git-ignored),
`.env.example` (+GEMINI_API_KEY), `requirements.txt` (+certifi).
- LLM decision: **Gemini 2.0 Flash (free tier)**, called via raw HTTPS with
  stdlib urllib (no new SDK to install). Key read from `.env` as
  `GEMINI_API_KEY` using the same self-contained `.env` reader as notifier.
- **Isolation verified**: imports NO core trading code (only json/os/ssl/
  urllib/xml/datetime/pathlib + certifi + yaml). Reads the watchlist YAML
  directly rather than importing src.suggest.
- Fallback (user's choice): if the key is missing, or the LLM call/parse
  fails, EVERY ticker is written as sentiment 0 / focus "no data" /
  stale=true, and top-level source="fallback". Downstream (4E) can trust the
  file always exists and can tell real reads from placeholders via `stale`.
- One batched Gemini call for all tickers (token guardrail), temperature 0,
  responseMimeType=application/json. Model output is coerced into schema
  (score clamped to [-5,5] and int-rounded, focus truncated to 3 words).
- SSL: macOS/VM Python urllib doesn't use the system CA store, so HTTPS was
  failing CERTIFICATE_VERIFY_FAILED; fixed by pointing an SSL context at
  certifi's bundle (now an explicit requirement). Live RSS fetch confirmed
  working (real ONGC/INFY headlines pulled).
- **RESOLVED 2026-07-05**: GEMINI_API_KEY is set and confirmed live —
  `source: "gemini"`, 10/10 real reads (e.g. TCS.NS scored -5 "sharp price
  crash", RELIANCE.NS +2 "Jio IPO"). Two things had to be fixed to get here:
  (1) the first key was created via AI Studio's "new project" option, which
  gets zero free-tier quota — fixed by creating the key against the
  existing billed `alpha-trading-app-2026` GCP project instead;
  (2) `gemini-2.0-flash` had been deprecated (404 "no longer available") —
  swapped to the `gemini-flash-lite-latest` ALIAS (not a pinned version),
  so future model deprecations won't silently break this again. Not yet
  scheduled on the VM (still not needed — see 4D note above).
- Nothing reads `news_sentiment.json` yet — that's 4E's forecast layer.

### 4E — Forecast layer  ✅ DONE 2026-07-05
Combines technicals (existing SMA/RSI) + `data/news_sentiment.json` into a
forecast: directional bias, confidence score, top 3-5 drivers, time horizon.
Rule-based weighted checklist in v1 — transparent, not a black box.
Files: new `src/forecast.py`.
- Weighted checklist, max +/-10 points: trend 50/200 SMA (+/-4), fresh
  Golden/Death Cross (+/-2, same direction as the new trend), RSI
  mean-reversion (+/-2: oversold=bullish, overbought=bearish), news
  sentiment (-5..+5 scaled to +/-2, weight 0 if the entry is
  `stale`/missing so a forecast never blocks on news_processor not having
  run). `confidence` = `|score| / 10 * 100`. Bias is bullish/bearish
  above/below a +/-2 threshold, else neutral. Time horizon fixed to
  "swing (multi-day to multi-week)" per the locked scope decision.
- `forecast(ticker)` returns None the same way `suggestions.analyze()`
  does when there's under 200 days of price history. Drivers returned
  sorted by magnitude, capped at 5 (checklist has 4 today; cap is
  future-proofing).
- Runnable standalone (`python3 -m src.forecast`), same pattern as
  `src.suggest`/`src.news_processor` — prints one line + driver list per
  watchlist ticker. Reads `news_sentiment.json` directly (own loader, no
  import of `news_processor` — keeps 4D's isolation boundary: forecast
  reads the JSON, never raw articles).
- Verified live end-to-end 2026-07-05: real technicals for all 10
  watchlist tickers combined with the real Gemini-scored sentiment file —
  e.g. TCS.NS came out BEARISH/60% (downtrend + "sharp price crash" news
  both pointing down), ONGC.NS BULLISH/32% (uptrend outweighing a
  negative earnings-miss headline). 8 new offline tests in
  `tests/test_forecast.py` (monkeypatches `suggestions.analyze`, no
  internet needed), 23/23 tests passing project-wide.
- **Carry-over into 4F:** `forecast()` is NOT wired into `strategy.py` or
  `trade.py` yet — it's a standalone read of technicals + news today.
  4F's tuner writes `data/brain_weights.json`; deciding how those learned
  weights adjust this checklist (and whether trade.py starts showing the
  forecast alongside a plan) is 4F's job, not yet done.

### 4F — Learning loop (auto-tuner)  ✅ DONE 2026-07-05
Reads scored outcomes from 4C, groups by plan archetype, and writes
adjustment weights to `data/brain_weights.json`, which `src/forecast.py`
(4E) consumes. Only tunes an archetype once it has a minimum sample size.
Files: new `src/tuner.py`, `src/forecast.py` (reads weights).
- **Design call vs. the original plan text above**: this was originally
  scoped as `src/strategy.py` reading the weights — written 2026-07-04,
  before `forecast.py` existed. Now that 4E's checklist exists and already
  scores by driver type (trend/cross/RSI/news), that's the natural place
  for learned weights to land, so the tuner feeds `forecast.py` instead.
  `strategy.py` is untouched.
- **Groups by plan archetype**, not by "confidence bucket" as the original
  text above suggested — no confidence value is journaled per trade today
  (forecast.py isn't wired into trade.py's proposal flow), so there was
  nothing to bucket by. Archetype is derived from strategy.py's own
  `signal` text, which only ever fires one of two BUY reasons: "fresh
  Golden Cross" or "uptrend with a dip (RSI ...)" — these map 1:1 onto
  forecast.py's bullish cross/RSI-oversold drivers, so the loop is closed:
  strategy proposes -> user decides -> 4C tracks the outcome -> 4F scores
  the archetype -> 4E leans on it next time.
  Pattern tags (the free-text labels from 4A, e.g. "Breakout") ARE broken
  out in a separate `pattern_tag_report`, printed and written to
  `brain_weights.json`, for the user's own insight — but not fed into any
  weight, since they're free-form text with no matching forecast.py driver.
- **Minimum sample size**: `tuner_min_samples` in `config.json` (default 5)
  resolved BUY-plan outcomes (from 4C's plan tracker — sells never carry a
  trackable stop, so they're naturally excluded) before an archetype's
  weight moves off neutral (1.0). Currently there's 1 real journal entry
  total (the ONGC buy, pre-4B, no plan) — 0 resolved plans, so the tuner
  correctly writes an empty/neutral `brain_weights.json` and won't move
  anything until real 4B+ plans exist and resolve.
- **Weight math**: `weight = 1.0 + avg_r_multiple * tuner_weight_sensitivity`
  (config, default 0.25), clamped to `tuner_weight_bounds` (config, default
  [0.5, 1.5]) — simple, transparent, and bounded so no archetype can swamp
  the checklist. `forecast.py` multiplies only the *bullish* cross/RSI
  point contributions by these weights; the bearish mirrors (Death Cross,
  RSI overbought) and the trend/news drivers stay fixed, since there's no
  journaled BUY archetype for the tuner to learn those from.
  `forecast()`/`run_once()` gained an optional `weights` param, defaulting
  to `load_weights()` (empty dict, i.e. all-neutral, if the tuner hasn't
  run yet or the file doesn't exist).
- Verified: 7 new offline tests in `tests/test_tuner.py` (fake journal
  entries — below/at/above min-sample-size, positive/negative average R,
  bound-capping, two archetypes tracked independently, pattern tags
  reported-not-weighted); ran live against the real journal (correctly
  0 resolved, empty weights, no crash) and live against `src/forecast.py`
  with the real (empty) `brain_weights.json` in place (neutral scores,
  unchanged from before 4F). 30/30 tests passing project-wide.
- **Phase 4 is now feature-complete end to end**: news + technicals ->
  forecast -> trade plan -> plan tracking -> learning loop, all in place.
  Nothing further is scoped for Phase 4; Phase 5 (options, intraday) is
  still deferred, see below.

## Deferred (real scope, later phases)

- **Phase 5 — Options support**: [✅ DONE 2026-07-06] - Migrated to the DhanHQ Data API, built defined-risk spreads, VIX-gating, SPAN margin offsets, dynamic bid-ask slippage, 2026 STT frictions, and atomic basket auto-exits.
- **Phase 5 — Intraday**: needs real-time/streaming price data and a faster
  fetch-decide loop; current daily-cron architecture can't do this as-is.

## Working rule for this phase

Same as the master PRD's guardrails: one file at a time unless told
otherwise, read configuration from `config.json` (never hardcode
multipliers), don't refactor `review.py` or `portfolio.json` math unless
explicitly asked.
