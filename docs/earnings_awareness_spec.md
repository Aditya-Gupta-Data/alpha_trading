# Earnings Awareness — architectural requirement (draft, 2026-10-16)

**Status:** S1–S3 **BUILT** 2026-10-16 (decision #150, branch `claude/earnings-s1-s3`, not deployed); S4–S6 spec only. Elsewhere, nothing below is built except where marked **EXISTS**.
**One rule added in the build (see #150):** a stock whose next results date is not yet announced is `clear` only when it cannot report before expiry (last quarter already reported, expiry before the next quarter end); otherwise `window` is null with the reason "not announced".
**Ruling it serves (Architect, 2026-10-16, decision #145 E):** no blind blackout
window. The system must know about results in advance, square off an open trade
when holding it through results is not justified, and eventually learn to trade
into or around results. For now, lay the groundwork; place no hard block.

**Why now (the 12–16 Oct diagnostic):** the HDFCBANK iron butterfly `8be5df84`
opened two days before HDFC Bank's results and the bear put `7da29e4f` five days
before. The TCS butterfly `24581540` opened in the middle of a results rally
(RSI 68, news +3/5). INFY and TCS bear puts were held through results that moved
the stocks 13–15%. The system recorded `days_to_results` on every entry and did
nothing with it.

**Binding constraints:** NO NEW ENGINES (07-27). Every seam below extends a module
that already exists. Paper only. Abstention over guessing: an unknown results
date is `None` with a reason, never "no results".

---

## 1. What exists today

| Piece | Where | What it does |
|---|---|---|
| Results calendar **EXISTS** | `src/ingestion/earnings_calendar.py`, cron 19:20 IST | NSE event calendar → `data/earnings_calendar.json` `{TICKER: next results date}`; daily copy in `data/lake/earnings/` |
| Entry stamp **EXISTS** | `src/confluence/evidence.py` | `days_to_results` on the evidence snapshot of every spread entry |
| Briefs **EXISTS** | `morning_brief.py`, `ceo_brief.py` | mention upcoming results |

**Gaps:**
1. Only the *next* date is kept. Once results pass, the ticker drops out, so
   "results were 1 day ago" can't be expressed (TCS on 10-15 read `None`).
2. Index underlyings (NIFTY BANK, FIN SERVICE, NIFTY 50) have no results date,
   but their heavyweights do. HDFCBANK and ICICIBANK results move NIFTY BANK.
3. Nothing asks "does this position's underlying report before its expiry?"
4. Outcomes, Court rows and `shadow_trades` do not carry the results window,
   so the learning stack cannot split win rate by "held through results" vs not.

## 2. The seams

### S1 — Calendar memory (extend `earnings_calendar.py`) — **BUILT (#150)**
- Keep `last` beside `next` per ticker, rebuilt from the lake's daily copies
  (the most recent date that has passed).
- `days_since_results(ticker, today)` beside the existing `days_to_results`.
- `index_constituent_results(index, start, end)` returns the heavyweights that
  report inside a window, from a hand-maintained map in `config/` (top 5 by
  weight per index; the map is data, not code).
- Postponements stay "latest fetch wins". A date that moves is visible in the lake.

### S2 — Entry stamp (extend `confluence/evidence.py` → journal) — **BUILT (#150)**
Every spread entry carries one `earnings` block. It is an annotation and gates nothing:

```json
"earnings": {
  "next_results": "2026-10-17", "days_to_results": 2,
  "last_results": "2026-07-18", "days_since_results": 89,
  "results_before_expiry": true,
  "window": "pre",
  "index_constituents_reporting": [],
  "source_as_of": "2026-10-14"
}
```
- `window` is one of `"pre"` (results within `EARNINGS_PRE_DAYS`, default 7, and
  before expiry), `"post"` (results within the last `EARNINGS_POST_DAYS`, default 3),
  `"through"` (results before expiry but outside the pre window), `"clear"`, or
  `null` with a reason when the calendar is stale or missing.

### S3 — Outcome label (extend `plan_tracker` settlement + `shadow_learner` Court rows) — **BUILT (#150)**
- Settlement copies the entry's `earnings.window` and `results_before_expiry`
  onto the outcome, the Court row and every `shadow_trades` row of that ref.
- `mcp strategy_stats` gains a `by_earnings_window` split. This is the dataset
  the learning step needs. Nothing is decided on it until each cell has n ≥ 20.

### S4 — Open-book watch (advisory; extend `live_bridge` / `morning_brief`)
- Each morning, list every open position whose underlying (or index heavyweight)
  reports before its expiry: structure, days to results, real-quote P&L, max loss.
- When a short-volatility structure (condor, butterfly) has results inside
  `EARNINGS_PRE_DAYS`, fire ONE Discord review card per ref (de-duplicated ledger,
  fail-open, standing owner rule: review flags go to Discord). The card states
  the facts and the square-off price on real quotes. A human decides.
- No automatic exit in this seam.

### S5 — Proactive square-off (exit rule; shadow first)
- Candidate rule: square off a short-volatility structure at the last session
  before results when holding it is unjustified, e.g. real-quote capture already
  above X% of max profit (bank it), or the body sits within Y% of spot.
- **Shadow first:** log `earnings_exit_shadow` rows ("would have squared off at
  ₹P on real quotes") and compare them with the actual settlement. Build only
  after S3 has data.
- **Going live is an exit-rule change.** It needs a Department 5 ruling on the
  shadow evidence (n ≥ 20), then an open-book dry-run before deploy (standing rule).

### S6 — Trading into / around results (future)
- Pre-results long-volatility structures (long straddle/strangle) and post-results
  volatility-crush structures enter only as Strategy Registry entries proven in
  the Court through the G-gates. Not before S3 holds at least two quarters of events.

## 3. What this will NOT do
- No blackout. No entry is blocked because results are near.
- No sizing change keyed on results.
- No new engine, no new data vendor. The NSE calendar stays the one source.

## 4. Decisions needed from the Architect before S4/S5
1. `EARNINGS_PRE_DAYS` (default proposal 7) and `EARNINGS_POST_DAYS` (3).
2. The index heavyweight map: which names and how many per index.
3. Which structures count as short-volatility for S4/S5: condor and butterfly
   only, or also credit verticals?
4. The S5 thresholds X and Y. Proposal: decide them from the S3/S5 shadow data.

## 5. Acceptance (per seam, hermetic tests)
- S1: lake fixtures with a moved date → `last`/`next` correct; a stale calendar
  (>3 days) → `None` + reason.
- S2: an entry two days before results → `window == "pre"`,
  `results_before_expiry`; a missing calendar → `window: null` + reason; the entry
  is never refused because of this block.
- S3: settlement copies the block onto outcome, Court and shadow rows.
- S4: one card per ref per day; a broken calendar sends no card and logs a named skip.
- S5: shadow rows only; no EXIT ticket exists on this path until its ruling.
