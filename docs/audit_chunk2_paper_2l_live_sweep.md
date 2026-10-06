# Chunk 2 audit — PAPER_2L_LIVE adversarial sweep (2026-10-01 → 2026-10-05)

Read-only adversarial audit of the live-quote arm (decision #120) and the exit/settlement paths it shares with the primary book. Code audited at `e97f3b0` (VM runs `5cadb5f`; they differ only by the Issue 44 fix to `src/live_bridge.py`).

**Method:**
- 7 finder lenses: illiquidity, mid-exit failure, spread boundaries, lifecycle, concurrency, silent failures, time/calendar.
- One merge pass.
- 3 independent refuters per finding: a code-trace, a hermetic reproduction and a production-reachability check. A finding is confirmed when at least 2 of its 3 refuters do not refute it.
- A completeness critic, then one extra lens on the gap it named.
- About 70 hermetic repro tests are kept in the git-ignored `.claude/audit_state/paper_2l_live_sweep/repro/`.

**Result:**
- 25 findings verified: **24 confirmed** (1 high, 6 medium, 17 low) and 1 refuted as documented design. Plus 2 informational notes.
- No critical finding. Nothing found double-books money, orphans a lock or leaves a ghost position.
- The one high finding (L1 = ledger Issue 44) is fixed in `e97f3b0` but **not deployed**.

## Answers to the three questions the sweep was opened with

**1. Zero bid or illiquidity.**
- A long leg with no bid is valued at 0 (decision #120, by design), and the structure still marks.
- A short leg with no ask, a crossed book, or a quote more than 50% off its last price makes the tick abstain and keep the last mark. It never crashes.
- An exit that would cost more than max loss is held.
- The gaps are F01 (an impossible quote on the profit side is accepted) and F02 (abstentions and holds are invisible).

**2. Mid-exit failure.**
- The row is stamped `exiting` before the exit is sent. The next tick resumes from the ticket and never sends a second one.
- `_settle` is one transaction and is idempotent. No double settlement or orphan lock was found.
- The remaining edges are low: F05, F06, F07, F08 and F15.

**3. Spread boundaries.**
- Profit per share is clamped to the structure's max loss and max profit at every place money is computed: marks, settlement, the expiry backstop and the dashboard.
- Frictions can take net P&L below max loss. That is correct, and the primary does the same.
- The gaps are F01 (the clamp accepts an impossible mark instead of rejecting it) and F11 (bounds are taken from the sign of the entry).

**4. The revived square-off with PAPER_2L_LIVE open (critic gap, 8-test repro of `24f931bb`): no defect.**
- The primary, 2L and ROT settle exactly once.
- PAPER_2L_LIVE's lock, position and ratchet are untouched. It later exits and settles at its own P&L.
- The hourly reconcile never releases the LIVE lock while its position is open.

## Confirmed findings

| ID | Severity | Finding | Where | Votes (trace / repro / reach) |
|---|---|---|---|---|
| L1 | high | The real-quote door for the primary's intraday square-off and for capital-rotation evictions is dead code (swallowed ImportError since 2026-07-15), so every primary/2L/ROT exit is priced on the EOD model | `src/live_bridge.py:355-370 (import at :357, bare except at :369-370)` | trace:ok/high repro:ok/high reach:ok/high |
| F01 | medium | An impossible crossed mark (above the structure's upper bound) is clamped to 100% capture instead of being rejected: one bad snapshot latches the ratchet lock, after which a normal tick exits at a loss, or a condor or pre-expiry exit settles at full max profit | `src/execution/live_pricer.py:231, :241, :445, :572-575, :611, :698-709, :719-725, :813` | trace:ok/medium repro:ok/medium reach:ok/low |
| F02 | medium | Non-marking and non-exiting states of the live arm are invisible: abstentions, unconfirmed marks and held exits write no event and no log line, and live_cycle discards tick's summary. Meanwhile pre-expiry and ratchet exits cannot fire, the dashboard shows the … | `src/live_bridge.py:489-492` | trace:ok/medium repro:ok/medium reach:ok/low |
| F09 | medium | The one-per-underlying+direction gate (#68) cannot see a PAPER_2L_LIVE position that outlives its primary, so LIVE stacks same-direction trades on one thesis | `src/exposure_gate.py:83-91, :110` | trace:ok/medium repro:ok/medium reach:ok/low |
| F17 | medium | A stock option entered exactly 7 days before expiry is closed by the live arm's first tick: the entry floor and the forced-exit threshold are the same number, so every such trade is a guaranteed round-trip loss | `src/options_proposer.py:114 (EQUITY_MIN_DAYS_TO_EXPIRY = 7), :117 (EQUITY_FORCED_EXIT_DAYS` | trace:ok/medium repro:ok/medium reach:ok/low |
| F22 | medium | The expiry backstop settles at an earlier session's close when the expiry-day bar is not yet in the series: the first overnight Auto-Sync after expiry books the previous close, for both live_pricer.eod_sweep and the primary's _expiry_backstop | `src/execution/live_pricer.py:885, :893-896, :908` | trace:ok/medium repro:ok/medium reach:ok/low |
| F23 | medium | Expiries moved to Monday by a holiday leave no session inside the 2-calendar-day pre-expiry window: the primary cannot exit before the expiry-day bar arrives (so the backstop settles it at Friday's close), and the live arm's only exit session is expiry day … | `src/plan_tracker.py:128 (PRE_EXPIRY_EXIT_DAYS = 2 calendar days), :277, :1702-1709` | trace:ok/medium repro:ok/medium reach:ok/medium |
| F04 | low | A held exit re-fetches the option chain on every 60-s tick for as long as the hold lasts, ignoring LIVE_QUOTE_INTERVAL_SECONDS | `src/execution/live_pricer.py:573-575, :580, :813-822` | trace:ok/low repro:ok/low reach:ok/info |
| F05 | low | _resume_exiting reopens the full position on any non-FILLED read of the exit ticket and ignores the status cancel_ticket returns: a half-filled basket, or a ticket filled concurrently, is left behind and the next exit closes those legs again | `src/execution/live_pricer.py:555-560 (same pattern in _exit's unfilled branch :619-626)` | trace:ok/low repro:ok/low reach:ok/low |
| F06 | low | _exit never checks that its 'exiting' stamp matched a row, and the reopen UPDATEs ignore the row's current state: an actor holding a stale row (such as a second live loop) issues a second FILLED EXIT ticket or reopens a closed row as a ghost | `src/execution/live_pricer.py:593-598 (stamp rowcount ignored)` | trace:ok/low repro:ok/low reach:R/info |
| F07 | low | If the live_exit event write fails after the money commit, the audit event and the journal stamp are lost for good, and the tick logs the settled exit as 'tick failed' | `src/execution/live_pricer.py:475 (commit), :493-494 (paper_log_event and _stamp_journal, u` | trace:ok/low repro:ok/low reach:ok/info |
| F08 | low | The expiry backstop skips rows in 'exiting': a row left exiting when the live arm stops ticking is never settled, and its lock is held indefinitely | `src/execution/live_pricer.py:885 (eod_sweep selects state == STATE_OPEN only), :356-362 (h` | trace:ok/low repro:ok/low reach:ok/info |
| F10 | low | The dashboard's open-trades table shows PAPER_2L_LIVE with the primary's MTM and ratchet, and drops the position once the primary settles while the live arm still holds it | `src/dashboard/data.py:322-347 (open_trades` | trace:ok/low repro:ok/low reach:ok/low |
| F11 | low | Max loss and max profit come from the sign of the crossed entry, not from the structure: a re-quote that violates no-arbitrage is accepted with wrong bounds, and every clamp then enforces them | `src/execution/live_pricer.py:274-284 (structure_bounds), :320-326 (requote_entry acceptanc` | trace:ok/low repro:ok/low reach:ok/low |
| F12 | low | The live arm enters at the approval-time crossed price without re-applying the R:R floor or re-sizing, so its real risk and reward:risk can breach the desk's own entry rules | `src/options_proposer.py:631 (R:R gate only at proposal), :1512-1524 (_live_requote checks ` | trace:ok/low repro:ok/low reach:R/info |
| F13 | low | A ruin halt or daily breaker that trips between proposal and approval does not stop a held lock from opening a new LIVE position | `src/portfolio_manager.py:546-549, :1108-1112, :1439-1443 (held lock approved before any ha` | trace:ok/low repro:ok/low reach:ok/info |
| F14 | low | Turning the live arm off leaves an open LIVE position unmarked and unexited until expiry; with paper_venue_enabled off, LIVE opens nothing but keeps its proposal-time lock | `src/live_bridge.py:619-626 at HEAD (tick armed only when pm.live_account_enabled(), checke` | trace:ok/low repro:ok/low reach:R/info |
| F15 | low | A FILLED live entry whose position row was never written is released at Rs.0 as 'never opened' if the primary settles before the scheduler's repair tick | `src/portfolio_manager.py:1576-1601 (LIVE branch of release_shadow_locks)` | trace:ok/low repro:ok/low reach:ok/info |
| F16 | low | expire_pending_lock's 'one commit' is split by the PAPER_2L_LIVE guard: has_open_position -> ensure_schema -> executescript commits the caller's open transaction (the D4 class) | `src/portfolio_manager.py:627-668 ('One commit for all of it' at :631` | trace:ok/low repro:ok/low reach:ok/info |
| F18 | low | A late approval inside the forced-exit window opens the live position at crossed prices, and the next 60-s tick closes it at crossed prices: a guaranteed round trip, because nothing checks days-to-expiry at approval | `src/execution/live_pricer.py:297-326 (requote_entry checks market-open and quotes only, :3` | trace:ok/low repro:ok/low reach:ok/low |
| F19 | low | The ratchet can arm off quotes from before the position existed: a second LIVE position on the same (ticker, expiry) is first marked on the shared cached chain, and when the chain door is failing that chain can be hours old while last_mark_ts says 'now' | `src/execution/live_pricer.py:704-706, :719-725, :736-737 (failed fetch leaves old cache), ` | trace:ok/low repro:ok/low reach:ok/low |
| F20 | low | The #110 ratchet kill switch (ratchet_enabled / RATCHET_ENABLED) does not reach the live arm: with it off, the primary and shadows return to the 65% take, but PAPER_2L_LIVE keeps ratcheting and never takes 65% | `src/execution/live_pricer.py:701-710` | trace:ok/low repro:ok/low reach:ok/info |
| F21 | low | Expiry-backstop failures and rows waiting for bars are never logged: eod_sweep keeps them only in its return value, and run_tracker prints only a settled count | `src/execution/live_pricer.py:883-912 (errors at :887 and :911, waiting at :898` | trace:ok/low repro:ok/low reach:ok/low |
| F24 | low | Now that e97f3b0 (Issue 44) revives it, the intraday square-off gets only one look per (trade, signal) per session: the alert de-dup runs before it, so a decline is never retried that session | `src/live_bridge.py:509 (`if sig['signal'] == 'hold' or not registry.fresh(sig): continue` ` | trace:ok/low repro:ok/low reach:ok/low |

## Refuted

- **F03** (trace:R/info repro:R/info reach:R/info): A long leg with no bid, or a strike missing from a partial chain, is priced at 0 before the stale check: a false max-loss MTM, and a missing condor wing is sold at a made-up 0.
  - Refuted as documented design: decision #120 says a long leg with no bid is worth 0, and `tests/test_live_account.py` asserts this.

## Informational (not defects)

- **F25**: The live arm's pre-expiry exit always fires on the first tick of the window day (about 09:15, on the opening book), while its control, the primary, exits at that day's close
- **F26**: Re-report of Issue 44 (the dead _leg_quotes_for import, already fixed at e97f3b0 and not yet deployed); kept only for its deployment consequence: the dormant real-quote exit and eviction paths switch on for the open book

## Fix order proposed for Chunk 2 implementation (needs owner sign-off)

1. **Deploy `e97f3b0` (L1 / Issue 44) after a read-only open-book dry-run on the VM.** It switches on the intraday square-off and the ROT evictions.
   - **Owner question first:** should #69 exits keep last-traded pricing, as restored, or cross the bid/ask like #70 and #120?
2. **F01:** reject a crossed mark above the structure's upper bound, and re-verify a peak increase on a fresh chain before it can arm a lock.
3. **F02 + F21:** log the live tick's summary, abstentions, holds and expiry-backstop waits or errors.
4. **F22 + F23:** settle the expiry backstop only on the expiry day's own bar, and count the pre-expiry window in trading days via `nse_calendar`.
5. **F17 + F18:** keep a one-day gap between the equity entry floor and forced exit, and add a days-to-expiry gate at approval.
6. **F09:** the #68 exposure gate is evaluated per account or firm-wide (owner ruling).
7. **The remaining lows**, batched: F04–F08, F10–F16, F19, F20, F24.

## Close-out status (2026-10-06 ~18:30 IST)

**Fixed and DEPLOYED** (VM `bc6fbe0`, 2026-10-06 18:21 IST). Each fix had an implementer, 3 adversarial reviewers and mutation checks, and the full suite passed (2,724).
- **L1 = Issue 44:** `e97f3b0`, priced crossed per #126.
- **Ruling 1 (#126):** `224c07f`.
- **F09, ruling 2 (#127):** `1102c8b` + `53bcc0d`.
- **F01:** `e589411`.
- **F02 + F21:** `cf09e52` + `96feb2a`.
- **F22 + F23 (#129):** `d5295e7`.
- **F17 + F18 (#128/#129):** `1e3966c`.
- **#68 re-checked at approval (#128), Architect ruling 2026-10-06:** `47f807a`.
- **Margin-block refusal UX:** `bc6fbe0`. A margin-blocked approval now answers 409 and the Discord bot keeps the buttons. It used to answer 200 "journaled".

**Still OPEN — the fix order's step 7:** the remaining 15 lows, F04–F08, F10–F16, F19, F20 and F24. None is critical, and none was found to double-book money, orphan a lock or leave a ghost position.

**Also open:**
- The data-driven closed-market detector that #124 queued for this chunk is not built.
- The informational notes F25 and F26 need no fix.

Chunk 2 closes when the Architect either rules the lows into a fix batch or formally defers them.

Raw lens answers, every refuter's reasoning and the repro tests are in `.claude/audit_state/paper_2l_live_sweep/` (git-ignored; Mac only). Its files:
- `done_lenses.json` (lens answers)
- `find_result.json` (merged findings)
- `verdicts_done.json` (every refuter's reasoning)
- `critic.json` and `find2_result.json` (completeness round)
- `repro/` (the repro tests)
