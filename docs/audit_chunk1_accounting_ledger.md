# Adversarial audit — Chunk 1: Accounting & Ledger (2026-09-30)

Method: five finder lenses (concurrency, atomicity, invariants, orphans,
recon/append-only) read the files below whole; every finding was then put to
two independent refuters told to default to "refuted" when uncertain. 51
findings raised → 34 survived, 17 refuted. The 34 collapse into the 14
distinct defects below (most were found by 2–5 lenses independently). Real-data
evidence was checked read-only on the trading VM at 2026-09-30 ~19:30 IST.

**Nothing here is fixed yet.** This file is the work list for the fix pass.

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
