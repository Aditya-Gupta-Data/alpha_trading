# EWS × Pattern Engine — Phase 2 Vision (LOGGED, DO NOT BUILD)

> **Status: logged 2026-07-23 at owner direction. This is a VISION record,
> not a build ticket. The current focus is strictly the Strategy Registry
> (SR-1 / SR-2 / SR-3, `docs/strategy_registry_spec.md`). Nothing here is
> built until the owner green-lights Phase 2 — with the sole exception of
> the small forward-logging bolt in §3, which is itself deferred until SR
> ships.**
>
> Related: `PHASE_8_NEWS_INGESTION_SPEC.md` (the EWS/news pipeline as built),
> `docs/macro_regime_engine_spec.md` (the pattern engine), and
> `docs/strategy_registry_spec.md` (the recipes each pattern carries).

"EWS" = the Early Warning System = `news_processor.py`'s dual-horizon read:
`short_term_catalyst_score` (days/weeks) + `long_term_macro_score`
(months/structural), each −5…+5.

## 1. The destination — News-Triggered Pattern Prediction

Today the flow is **price → pattern**: the macro engine DTW-matches the
trailing price trajectory and declares "this resembles regime X." News
plays no part in the trigger.

Phase 2 adds the inverse flow — **news → pattern**:

1. The EWS parses a specific sentiment/event today (e.g. "sharp hawkish
   central-bank surprise," "oil supply shock," "banking-stress headline").
2. That event is canonicalized to an **event type** (the `news_parser`
   entity/event vocabulary already exists).
3. The pattern engine looks up **historical analogs of that exact event
   type** — the episodes whose anchors were the same kind of news — and
   **predicts the resulting market structure**: which archetype/phase
   historically FOLLOWED that news, and — via the Strategy Registry —
   which recipes paid off through it.

**News becomes the trigger; the pattern engine becomes the predictor.**
This can fire BEFORE the price trajectory fully confirms a regime — an
early-warning read, not a lagging DTW match. The two flows then
cross-check each other (news says "entering X," price DTW agrees or
doesn't — the disagreement is itself signal).

## 2. The prerequisite — simulate/backfill historical news

"Historical analogs of that exact news event" requires a **backfilled news
corpus mapped to dates** — the Time Machine applied to news, so that each
past episode anchor carries the news frame that actually preceded it.
Intention logged; when built it inherits the backfill laws already in
force: as-of dating with zero look-ahead (`timelock`), `source='backfill'`
tagging so replayed news is never confused with lived signal, and NULL-
honesty on unparseable/absent history. This is the heavy leg of Phase 2
and gates the trigger loop above.

## 3. The near-term bolt (minimal, forward-only) — deferred until SR ships

For the current 60-session shadow run we do the *smallest* honest thing,
and **not yet**: bolt the EXISTING EWS onto `macro_nightly` so that
**whenever `declare()` declares a regime, the current dual-horizon
sentiment is logged right beside it** on the declaration ledger. No
backtest, no analog lookup, no trigger — pure forward data collection.

- One additive field on each ledger line:
  `ews: {short_term_catalyst_score, long_term_macro_score, as_of}`, read
  fail-open from `news_processor` (a stale/absent read logs `null`, never
  blocks the tick — the silence-ban discipline).
- Why now-ish: it costs ~10 lines and starts accumulating the paired
  (regime declared, sentiment at declaration) series that Phase 2's
  correlation study will need — "did the sentiment at declaration predict
  how the regime scored?" We collect the data forward from today so the
  study is possible later, even though we don't run it yet.
- **Explicitly deferred**: this bolt is built only AFTER SR-1/SR-2/SR-3
  are done and the owner says go. It is logged here so it is not lost, and
  kept out of the current build so SR stays the sole focus.

## 4. Laws (unchanged)

Advisory-only, risk-reducing, one door; scored by Dept 5 before it advises
anything; abstention over hallucination. A news-triggered prediction is a
forecast on the record first, exactly like the regime call and the
strategy call — it earns authority through the same court, never by
assertion.
