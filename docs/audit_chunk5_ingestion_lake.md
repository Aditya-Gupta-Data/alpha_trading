# Adversarial audit — Chunk 5: Ingestion & Data Lake (2026-10-09)

Method: two finder lenses (data quality & staleness of values; failure modes, tokens, rate limits, cron hygiene) read the files whole; the lead confirmed or refuted each against the code, defaulting to "refuted" when uncertain. Fixes are queued for the next usage window (the 85% guardrail).

## Scope
`src/dhan_client.py`, `src/dhan_guard.py`, `src/token_provider.py`, `src/renew_token.py`, `src/data_fetcher.py`, `src/lake.py`, the candle sink in `src/live_bridge.py`, `src/ingestion/*.py`, `src/news_processor.py`, `src/nse_calendar.py`, `src/staleness_guard.py`, `src/ops_monitor.py`, the quote parsing in `src/execution/live_pricer.py`, `scripts/setup_cron.sh` and the scripts it calls.

## Findings

### Lens Q — data quality & staleness (8 raised; lead reproduced Q1, Q2, Q4)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| Q1 | **blocker** | CONFIRMED, **FIXED** | `fo_bhavcopy.py:172` ranks liquidity tiers by stock-option traded value; NSE's fo zip has carried FUT rows only since 07-14, so `opt_val` is 0 for all 213 symbols and tier1 was FILE ORDER. `liquidity_filter` refused the most liquid names; `liquidity_slippage` charged ICICIBANK/INFY the illiquid tier. | `data/fo_liquidity.json`: 0 symbols with opt_val>0; ICICIBANK rank 184 'illiquid', HDFCBANK 63. Fix: rank on `fut_val` when no option rows, `rank_basis` recorded. The next 19:xx bhavcopy run rebuilds the tiers. |
| Q2 | major | CONFIRMED, **FIXED** | `parse_secban` keeps `isalnum()` symbols only: M&M, GVT&D, BAJAJ-AUTO, NAM-INDIA can never read as banned. | `parse_secban('1,M&M\n2,BAJAJ-AUTO\n3,SAIL')` → `['SAIL']`. |
| Q3 | major | plausible | `deals_tracker.py:637` stamps live deals with the RUN date; the 19:30 cron runs daily incl. weekends → Friday's snapshot appended Fri/Sat/Sun, tripling `smart_money_trend` volume feeding the (now live, W1) bullish veto. | Lens scratch run: n_deals 3 for one deal. Verify on the VM: `grep -c '"as_of": "2026-10-04"' data/deals_history.jsonl` (a Sunday) should be 0. |
| Q4 | medium | CONFIRMED | `options_proposer._leg_fill` (`:446`): a quote >50% off LTP is not refused — the leg fills at the stale LTP (always the favourable side) with `fill_basis: "ltp"`; `live_pricer.crossed_open_price` abstains in the same case. | `_leg_fill(bid 12, ltp 30, SELL)` → `(30.0, 'ltp')`. Fix = refuse (abstain) like the live arm — entry-path change, dry-run first. |
| Q5 | medium | plausible | The only freshness check (`SafeDhanClient.freshness_error`) is not on any trading path; chain payloads carry no timestamp so it could never fire on a chain. The holiday calendar is the only defence against a frozen chain (Issue 41). | `grep -rn SafeDhanClient src` → wealth_lock, portfolio_report, portfolio_greeks, macro_tracker, cross_asset only. |
| Q6 | medium | plausible | `news_processor._clean_entry` turns a non-numeric/missing score into 0 with `stale: False` → a false "narrative collapsed" reversal card and a fake 0 ingested. | `_clean_entry({"short_term_catalyst_score":"bullish"})`. |
| Q7 | low | plausible | `flows_tracker.run` defaults `as_of` to today when the payload has no parseable date — a permanent lake row under the wrong session. | |
| Q8 | low | plausible | `live_pricer._node` returns `{}` for an ABSENT strike and the long leg is valued 0.0 with `ok=True` (a data hole becomes a mark). | `crossed_mark` with a missing 24100PE → ok True, 24100PE 0.0. |

Refuted by the lens: today's close not double-counted in SMA/RSI; IST host enforced by cron; exact strike keys; expiries from Dhan's list; VIX None kept as unknown; lake writes atomic/deduped; equity bhavcopy header strip OK.
