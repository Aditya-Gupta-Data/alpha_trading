# Adversarial audit — Chunk 1: Accounting & Ledger (2026-09-30)

Method: five finder lenses (concurrency, atomicity, invariants, orphans,
recon/append-only) read the files below whole; every finding was then put to
two independent refuters told to default to "refuted" when uncertain. 51
findings raised → 34 survived, 17 refuted. The 34 collapse into the 14
distinct defects below (most were found by 2–5 lenses independently). Real-data
evidence was checked read-only on the trading VM at 2026-09-30 ~19:30 IST.

**Fix status (2026-09-30 night):** D1–D8 and D13 are fixed in `4c5a838`
and the follow-ups through `a82d12f` (decisions #121, #122; the owner ruled
D6, D7, D8 and D13 the same day). They were deployed to the VM at 23:04 IST.
D5's data repair is a one-off tool, `scripts/repair_d5_brain_map_outcomes.py`.
It ran on the VM at 23:04:46 IST, verified, and is recorded in HANDOVER and
ledger Issue 39. D9–D12 and D14
(Batch D, minor) were fixed on 2026-10-01 (decision #123, `a91f532` → `0e66454`)
together with the two residuals of the fix-diff review. **Chunk 1 is closed**
(see "Batch D review" at the end). The same panel re-reviewed the fix diff; its
result is in the "Fix-diff review" section at the end.

The table below is the original audit and is left as it was written.

## Scope (files audited)

- `src/portfolio_manager.py` — treasury, margin locks, halts, breaker, shadow accounts, rotation bookkeeping
- `src/portfolio.py` — portfolio.json cash book, frictions, SPAN margin
- `src/journal.py` — journal.jsonl read / log / rewrite_all / update_entry
- `src/execution/recon_engine.py` — read-only broker reconciliation
- `src/firm_treasury.py` — equity-desk budget routing
- `src/equity_desk.py` — eqd: locks, fund_entry / settle_exit / sweep
- `src/wealth_lock.py` — advisory sweep ledger
- `src/brain_map.py` — connect() and schema (the shared sqlite file)
- `src/execution/live_pricer.py` — settlement parts only
- `src/plan_tracker.py` — settlement seams only
- `scripts/restore_issue31_trades.py`, `scripts/repair_issue34_2l_verdicts.py`

## Verified defects (deduplicated), worst first

| ID | Sev | Defect | Where | Real-data evidence (VM, 09-30) |
|----|-----|--------|-------|--------------------------------|
| D1 | **blocker** | `run_tracker` (api process, hourly) and `decide_pending` read the whole journal, do seconds of network/venue work, then `rewrite_all` the stale copy **without the journal lock**. A row appended or settled by the scheduler in that window is lost or reverted: a new trade's row vanishes while its locks stay held; an intraday square-off is reopened and settled a second time (portfolio.json credited twice); a raised ratchet lock is lowered. `decide_pending` also deterministically erases its own rotation-eviction stamp. | `plan_tracker.py:1368/1447/1609/1653`, `options_proposer.py:1257/1308`, `journal.py:73` | No damage found yet: every active lock has a journal row, every settled trade's lock P&L equals its journal P&L. Latent. |
| D2 | major | `journal.rewrite_all` truncates the file in place (no temp file, fsync, rename). A crash mid-write loses every row after the cut; a concurrent reader can see a half-written file. | `journal.py:73-78` | Not observed. |
| D3 | major | A failed primary lock release is swallowed (`release_entry` catches everything) and **both callers ignore the result**, then write the outcome and move portfolio.json cash. The lock stays active forever — nothing re-drives an options lock (only `eqd:` locks have `sweep_orphan_locks`) — and the shadow accounts' locks are skipped too. | `portfolio_manager.py:1360-1390`, `plan_tracker.py:1562-1570, 804-806` | None found yet. |
| D4 | major | `live_pricer._settle` is **not** one transaction, contrary to its comment and DECISIONS #120: `paper_release_margin` begins with `ensure_accounts_schema` → `executescript`, which implicitly COMMITs the pending row close. A failure after that leaves a closed row with a live lock. It self-heals (`_repair_late_locks` next market-hours tick), but the guarantee is false and `test_close_and_release_are_one_transaction` is vacuous (it monkeypatches the release away). Same mechanism makes `restore_issue31_trades.py`'s "one transaction" false. | `live_pricer.py:428-450`, `portfolio_manager.py:112-114, 778-781, 962-980` | Reproduced by two refuters. |
| D5 | **blocker** | `brain_map.record_outcome` is `INSERT … ON CONFLICT DO NOTHING`, not the upsert that `restore_issue31_trades.py` and DECISIONS #105 assume. The five Issue-31 voided stop-loss outcomes are therefore permanent in the Brain Map, and the real resolutions never overwrote them. Every consumer (strategy_stats, query_similar_events, the MCP tools, edge decay, cluster hit-rates) reads the wrong results. | `brain_map.py:170-187`, `scripts/restore_issue31_trades.py:22-26` | **Confirmed live.** 54365ef1: journal `profit_take` +₹11,136.68 → brain map `loss`, r −1.14. 2ff3443a: journal `pre_expiry_exit` +₹14,482.63 → `loss`, r −0.56. bd73554d / efe1681e: losses, but wrong r and date. f8356c9c: still OPEN in the journal, recorded as a closed loss. |
| D6 | major | Capital rotation (#115) runs its eviction inside `evaluate_shadow_accounts`, which fires at PROPOSAL time, before approval. PAPER_2L_ROT can close a live trade to fund a signal that is then rejected or never approved; and a trade the primary never entered (still `pending_approval`) is an eviction candidate, so ROT books P&L on a position the firm never held. | `portfolio_manager.py:1230-1252`, `plan_tracker.py:598-633` | Not fired yet (ROT's first fills were 09-29/09-30; no rotation events). |
| D7 | major (owner ruling) | Proposal-time locks on pending entries live until a resolver fires (days to weeks). Meanwhile they feed the treasury router's utilization tilt, the 2L/ROT refusals and ROT evictions. This is the Issue-33 frozen-margin shape, with no expiry. | `portfolio_manager.py:515-517, 946-949`, `firm_treasury.py:284-296` | Seen 09-23→09-25 (Issue 33). |
| D8 | major | A latched risk-of-ruin halt on PAPER_2L / ROT / LIVE has **no clear door**: `clear_halt` and `--reset-halt` act only on the primary. The only way out is hand-SQL with no audit row, which is what decision #92 forbids. | `portfolio_manager.py:855-879, 198-230` | No shadow halt latched yet. |
| D9 | minor | portfolio.json is an unlocked read-modify-write shared by three processes, written non-atomically; two settlements in the same instant lose one credit. | `portfolio.py:26-43`, `plan_tracker.py:414` | — |
| D10 | minor | The 15-minute mirror rsyncs `brain_map.db` as a plain file while writers commit (rollback-journal mode), so the dashboard can publish a torn snapshot. | `scripts/publish_dashboard_mirror.sh:17` | — |
| D11 | minor | Equity desk: the `eqd:` margin lock is committed before the ledger entry is appended; a lost append leaves a lock no sweep can settle (the Issue-30 shape). | `equity_desk.py:209-213`, `equity_shadow_proposer.py:552-563` | — |
| D12 | minor (explains a mystery) | The ₹14.49 "curve above equity" (HANDOVER 09-24) is **not** a path that moves realized P&L without a curve row. Two settlements landed in the same second; `equity_curve` has no ordering key, so `ORDER BY ts DESC` picked the earlier one. Every realized-P&L writer does append a curve point. | `portfolio_manager.py:86, 100-105` | Two eqd: settlements at 2026-09-24T09:21:59. |
| D13 | minor | Shadow P&L = primary × lots/primary_lots also scales the **flat** ₹20/leg brokerage, so PAPER_2L books fewer frictions than a real 1-lot trade would pay (≈₹63 per trade on a 3-lot primary). This quietly flatters the retail-scale proof. | `portfolio_manager.py:1333-1335` | — |
| D14 | minor | Recon's book side treats every FILLED ENTRY ticket without a FILLED EXIT as open, but the expiry backstop, an unfilled EOD exit and venue-off exits produce no exit ticket, so settled trades can linger as "open" paper rows. | `recon_engine.py:223-243` | 0 phantom rows at 09-30. |

## Refuted (17), in short

Refuted as deliberate design, unreachable in production, or harmless to money:

- The margin gate failing **open** on a DB error. This is the documented fail-open contract; the refuter noted it is a judgement call.
- `release_margin`'s check-then-update window. Neither concrete two-writer scenario reaches the same ref.
- `request_entry`'s cash check and lock insert not being one transaction. No real concurrent judges.
- `inject_capital`, `set_budget` and `rebase_budget` committing before the audit row. Owner-run and single-shot.
- The equity desk re-settling at the modelled exit instead of the venue fill. Same P&L to the paisa.
- Several `restore_issue31_trades.py` crash-safety gaps. It is a one-off tool, already run.
- `wealth_lock` rows outliving a voided settlement. No live path.
- Stranded live rows if the live arm is switched off, and a double-fault LIVE orphan. Both need two faults at once.

## Fix plan (before Chunk 2)

1. **Batch A — ledger integrity (code; no owner ruling needed).**
   - D1 + D2: make `rewrite_all` atomic (temp file, fsync, `os.replace`) under the journal flock. Give `journal.log` the same flock. Convert `run_tracker` and `decide_pending` to per-row `update_entry` writes with abort-if-already-resolved guards.
   - D3: callers check `released`; `release_entry` still settles the shadow locks when the primary fails; add an options orphan-lock reconciler at the top of `run_tracker`.
   - D4: no DDL inside a caller's transaction (a per-connection "schema ensured" memo or an `in_transaction` guard), plus a real-path test that fails the lock UPDATE.
   - Cross-cutting: `brain_map.connect` busy timeout of at least 30 s.
   - Then run the same adversarial panel over the Batch A diff.
2. **Batch B — Brain Map repair (D5; owner go-ahead needed, writes history).**
   - Back up first, then correct the five `outcomes` rows from the journal. Delete f8356c9c's row, since it is still open.
   - Add an explicit replace path for repairs only.
   - Append a DECISIONS row correcting #105's premise, rather than editing #105.
3. **Batch C — needs owner rulings.**
   - D6: execute ROT evictions only at approval, and only against approved primaries. This changes #115's behaviour.
   - D7: set a TTL or expiry for pending locks.
   - D8: add `paper_clear_halt` with the same audit rules as #92.
   - D13: settle shadow accounts on their own friction stack. This changes how PAPER_2L's P&L is computed.
4. **Batch D — minors.**
   - D9: portfolio.json lock plus atomic write.
   - D10: publish the mirror from a `sqlite3 .backup` snapshot.
   - D11: equity-desk entry ordering plus a no-entry orphan sweep.
   - D12: order the equity curve by `(ts, rowid)` and correct the 09-24 HANDOVER note.
   - D14: build recon's book side from active locks only.

Deploy each batch after 15:30 IST. Chunk 2 (OMS & Execution) starts after Batch A is deployed and re-reviewed.

## Fix-diff review (2026-09-30 night)

**Round 1, over `4c5a838`:** the same five lenses, then two refuters per
finding defaulting to "refuted". 19 findings were raised by the five lenses,
several of them the same defect, which leaves 10 distinct ones.

| # | Finding | Panel verdict | Fixed in |
|---|---------|---------------|----------|
| 1 | The `--review-pending` CLI approved an entry after its D7 expiry with no margin in any account (it never went through the gate) | CONFIRMED major (3 lenses) | `53e79c1`: the CLI decides through `decide_pending` |
| 2 | A proposal the closing cycle journals at 15:30:xx was pushed to the next day's close | CONFIRMED minor (3 lenses) | `53e79c1`: before 16:00 = that session's close |
| 3 | `--account` was silently ignored by `--inject` | CONFIRMED minor | `53e79c1`: refused |
| 4 | `decide_pending` held the journal lock through Dhan calls (live re-quote, rotation quotes) | CONFIRMED, rated minor by both refuters | `37a7708`: the live re-quote is prefetched before the lock. **Residual:** a margin-walled rotation eviction at approval still fetches its quotes under the lock |
| 5 | The EOD walk's ratchet on a still-open spread was no longer written | CONFIRMED minor (2 lenses) | `37a7708` |
| 6 | The restore-tool correction note overstated #122 | CONFIRMED minor | `37a7708` |
| 7 | The D5 tool deleted link rows without archiving them | PLAUSIBLE (1 of 2 refuters) | `53e79c1` |
| 8 | A pending expiry could release a live account lock that holds an open position | PLAUSIBLE | `37a7708` |
| 9 | The wealth sweep (quote + Discord) ran inside the settlement lock | REFUTED (both refuters: minor, not major) | fixed anyway, `37a7708` |
| 10 | An eviction at approval could be followed by the entry's refusal (stressed vs unstressed margin) | REFUTED (2 lenses) | fixed anyway, `53e79c1`, with a test |

Also refuted: "#121/#122 do not exist in DECISIONS.md" (true at review time
and added before commit) and "a crash between the side effects and the row
write re-settles" (not introduced by this diff; the window is narrower than
before). `f3d4458` adds a fix the panel suggested: a journal lock timeout
skips one tracker row instead of aborting the sweep.

**Round 2, over `4c5a838..f3d4458` (the follow-ups):** 13 findings, 4
distinct. The worst was a **blocker in my own round-2 change**: persisting the
EOD walk's ratchet let the next hourly walk judge EARLIER closes against the
saved lock and fire a backdated `ratchet_hit`. The same flaw had existed for
intraday rungs since #110. The VM history check found none backdated. The
other three: an eviction that closes at a loss can still leave the entry
unfunded; reconcile's deferred wealth sweeps could be lost if a later ref
raised; and a docs claim made before the repair had run. All four are fixed
in `f5761e5`.

**Round 3, over `f5761e5`:** 7 findings, 4 distinct.
- Three were CONFIRMED or PLAUSIBLE and are fixed in `47fd03f`:
  - A later intraday rung moved an earlier rung's date. Every rung is now kept with its own date.
  - A saved rung newer than every bar replaced the higher lock the closes had built. It is now merged instead.
  - The eviction funding estimate used ladder slippage, which can be below the venue's tier slippage. It now takes the worse of the two.
- One was refuted and is not fixed: an eviction whose own loss trips the daily breaker. This predates the audit fixes.

**Round 4, over `47fd03f`:** 6 findings, 2 distinct, both CONFIRMED or
PLAUSIBLE, both minor, both fixed in `4c19e6c`:
- The lock earned on closes was not durable, so one run on a partial Dhan bar series could lower it. A close-earned lock is now a dated rung.
- The eviction estimate dropped the entry-side slippage when the venue slip won. It now counts entry-side ladder + venue exit.

**Round 5, over `4c19e6c`:** 3 findings, 2 distinct, both addressed in `c7ea551`:
- CONFIRMED minor: a peak stored rounded half-up (59.996 → 60.00) re-armed the 60 → 30 rung on the next walk. Stored peaks are now floored.
- REFUTED, but done anyway: the entry-side estimate fix had no test. It has one now.

**Round 6, over `c7ea551`, plus a blocker/major-only sweep of the whole
series `4091dfa..c7ea551`:** the sweep found **no blocker or major**. The
rounding lens found 2 minor issues, both CONFIRMED and fixed in `a82d12f`:
- PAPER_2L_LIVE's own ratchet still stored its peak rounded half-up. It is now floored.
- The round-5 test's walk half tested a genuine arm, not the rounding. It now drives `note_ratchet` and the live arm, and both tests fail on the parent.

The review ends here. Every finding the panel upheld across the six rounds
is fixed, and each fix has a test that fails on the code before it. The
refuted findings not acted on are listed above.

**Known residual (not fixed):** when PAPER_2L_ROT is margin-walled at
approval, its eviction quotes are fetched while the journal lock is held.
This happens only in that case and lasts a few seconds.


## Batch D review (2026-10-01)

Batch D and the two leftovers shipped in `a91f532`. The same panel reviewed
it, then reviewed each fix it led to:

- **`a91f532`:** 20 findings, 7 distinct (11 CONFIRMED, 4 PLAUSIBLE, 5
  REFUTED). The two majors:
  - The orphan sweep would have released every desk lock at zero on an
    unreadable or empty ledger read.
  - Nothing read the `funding_revoked` correction.

  The minors:
  - `/api/decision` DISMISS returned a 500 after journaling (from the D9 rework).
  - Curve points could be written from a stale equity read.
  - The edge-miner and pull-script copy paths still copied the live files.
  - Recon ignored a per-account eviction.
  - One test proved nothing.

  All fixed in `fad4b72`.
- **`fad4b72`:** 14 findings across 4 lenses, all minor. 6 CONFIRMED, 3 PLAUSIBLE, 5 REFUTED. Every
  one was addressed in `0e66454`:
  - A curve rounding mismatch on exact-half values, and a read-back race.
  - A revocation shown as an EXIT, and one written without checking it persisted.
  - The sweep silently paused by one torn line.
  - A phantom-funded exit going through the OMS.
  - A D7 expiry hiding a position from recon.
  - DISMISS journaling before its read.
  - The export's margin column on revoked rows.
  - Weak reader tests, and stale module rows.
- **`0e66454`:** reviewed together with a blocker/major-only sweep of the whole
  Batch D series `ddaa1f7..0e66454`.
  - **The sweep found nothing.**
  - 10 findings, all minor: 5 CONFIRMED, 1 PLAUSIBLE, 4 REFUTED.
  - Two concerned code:
    - A lockless funded entry whose exit was already on the ledger was never corrected.
    - Recon's D7 check ran one query per lock.
  - Six were untested claims, among them the write lock across a curve point's read; that test was rewritten after a mutant showed it passing for the wrong reason.
  - All fixed in `95c053f`.
- **`95c053f`:** 2 findings, both CONFIRMED minor, both fixed in `7ebda95`:
  - The module row lacked the reverse sweep's era cutoff.
  - The primary half of recon's expiry fold was unpinned through a revival.

  A refuter checked the new fold against `pm._lock_expired_unapproved` on
  300 random event sequences and found 0 differences. No code changed after
  this round.

**Batch D outcome:**
- Every finding the panel upheld across the four rounds is fixed with a test.
- The one test shown passing for the wrong reason was rewritten and re-checked against a mutant.
- The whole-series sweep found no blocker or major.
