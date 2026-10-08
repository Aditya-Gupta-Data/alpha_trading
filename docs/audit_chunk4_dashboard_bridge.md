# Adversarial audit — Chunk 4: Dashboard & Bridge (2026-10-09)

Method: three finder lenses (data truth on the dashboard, the API server + Discord bot, reporting honesty of the Discord cards) read the files whole; the lead confirmed or refuted each against code and the VM mirror snapshot (10-09 00:52, read-only), defaulting to "refuted" when uncertain.

## Scope
`src/dashboard/{app,data,benchmarks,api_bridge,mirror_snapshot}.py`, `src/equity_history.py`, `scripts/publish_dashboard_mirror.sh`, `src/portfolio_report.py`, `src/market_snapshot.py`, `src/firm_mtm.py`, `src/positions.py`, `src/api.py`, `src/api_server.py`, `src/discord_bot.py`, `src/discord_client.py`, `src/notifier.py` (budget/spool/card builders), `src/human_pulse.py`, `src/ceo_brief.py`, `src/ceo_language.py`, `src/morning_brief.py`, `src/eod_summary.py`, `src/ops_monitor.py`, `src/validation/digest.py`. The React desk (`frontend/`) was being edited by a parallel session and is out of scope.

**Status (2026-10-09 ~05:00 IST):** Batches A–D FIXED and pushed (`dbad814`, `3dfb8d7`, `275d3c1`; decision #137). NOT deployed. Rulings under 'Fix plan' remain open.

## Findings

### Lens B — API server & Discord bot (6 raised; lead verified B1, B2, B4 on the code)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| B1 | **major** | CONFIRMED | `_decide_pending_locked` (`options_proposer.py:1569-1668`) has no age check and stamps no `decided_at`; a pending row approved days later fills at proposal-day premiums and the tracker replays bars from the proposal `date`. The 15:30 sweep frees margin but leaves the row approvable. | No `decided_at`/age anywhere in the approval path (grep). HANDOVER 09-25: "owner approved the 14 stale pendings"; the unsupervised card says "approve or reject anything" to re-arm — approving is hindsight into the real paper record. |
| B2 | medium | CONFIRMED | `discord_bot._fetch_pnl` (`:494-500`) `json.loads` an already-parsed dict and slices a dict on error → `/pnl` always fails with "Can't reach the gateway". | `_bridge_call` docstring: returns parsed JSON. Broken since 07-14. |
| B3 | medium | plausible | Plain-text `send_discord_message` skips the budget gate: the hourly "📕 Trade Episode" text duplicates the spooled `closed` card; proposal cards, pending follow-ups, watchlist alerts and the 09:10 link card are uncounted. | `api.py:221,244`, `options_proposer.py:944`. Some are intended real-time (proposal needing approval); the Episode duplicate contradicts #84. Owner ruling on which text paths stay real-time. |
| B4 | medium | CONFIRMED | `drain_digest_queue` empties the spool when the card is BUILT (`ceo_brief.py:1074`, `eod_summary.py:457`): `--dry-run` eats the real queue; a failed or budget-spooled EOD/CEO post loses the Batched field (spool keeps only `description[:400]`); the drain truncates without a lock. | `notifier.py:494-501`. |
| B5 | low | plausible | Bot timeout 15 s; the server-side approval (Dhan quotes + up to 120 s lock wait) continues → owner sees "Gateway unreachable" for a trade that opened. No double-act. | `discord_bot.py:201,371`. |
| B6 | low (conditional) | plausible | Buttons never check `interaction.user`; any channel member can decide entries and re-arm auto-approve. | Only matters if the server ever has a second member. |

Refuted by the lens: key middleware wraps every mutating route; buttons cannot double-act or reverse; `margin_blocked` → 409 with buttons kept; D1 (journal lock) not regressed; chat endpoints read-only; CORS/binding localhost-only; the keyless Mac backend LaunchAgent is retired.

### Lens T — data truth on the dashboard (10 raised; lead reproduced T1, T2, T4 on the mirror)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| T1 | **major** | CONFIRMED | `data.py:538-567`: the PAPER_10L % line is a simple return on the base current at each point, not chained across capital moves — the opposite of its docstring. | 08-04 +19.71% → 08-07 (₹injection) +3.94%, no trade behind it; the 07-21 clean sheet (true 0%) is never plotted. |
| T2 | **major** | CONFIRMED | `data.py:271-274, 329-334`: net equity is set as soon as ANY position is priced; `equity_history.record` therefore never skips a partial mark. Equity-desk darlings are never priced (`mtm_rs: None`). | PAPER_10L 15/16 priced (SUPREMEIND ₹93k margin unpriced) yet ₹11,42,863.90 headlines as "realized + open positions" and is stored as history. |
| T3 | medium | plausible | `equity_history.record` stamps the recording time whatever the mark age; stale LIVE marks become flat "measurements". | Mirror row ts 00:52 priced on 15:29 marks. |
| T4 | medium | CONFIRMED | `data.py:81-90, 362-405`: CAGR mixes windows (₹2L-era P&L in the numerator, days from 08-07) and annualises shadows from day 1 against `firm_mtm.CAGR_MIN_DAYS=30`. | PAPER_10L 122.28% shown vs ~77% single-window; PAPER_2L 272.77% after 17.6 days. |
| T5 | medium (live once #135 deploys) | CONFIRMED on inspection | `data.py:759` shows the MODELED capture beside a peak/lock that #135 moves on CROSSED capture; `real_capture_pct` is in the snapshot but unused and the basis unlabelled. | 70% modeled shown beside lock 30 while crossed is 29%. |
| T6 | low-med | plausible | Issue-31 restatement (+₹36,545 at 09-23 20:31) is an unlabelled jump; `CAPITAL_EVENT_TYPES` has only clean_sheet/injection. | `account_events` issue31_restore. |
| T7 | low | plausible | DB backup and journal copy are not one snapshot; a settlement between them drops a trade's P&L from both realized and unrealized for one mirror cycle; on the VM `record()` can double-count permanently. | `mirror_snapshot.py:61-62`. |
| T8 | low | plausible | `api_bridge.py:85-103` adds +05:30 to curve timestamps but not to LIVE `marks_as_of`/`last_mark_ts`; a non-IST browser shows 20:59 for 15:29. | |
| T9 | low | plausible | Staleness flagged only for LIVE; snapshot marks of 10L/2L/ROT carry no age; React says FRESH outside market hours however old the mirror; realized lines extend flat to "now". | |
| T10 | low | plausible | A flat account shows "—" for net equity on Streamlit while the recorder stores realized = net. | `_with_mtm(unrealized_pnl=None, open_positions=0)`. |

Refuted by the lens: voided 7f4a4897 excluded everywhere; the box's sqlite copy is an online backup + atomic replace; shadow MTM scaling matches by hand; LIVE rows from their own book (#132 F10) correct; deep-ITM condor clamp genuine; benchmark rebasing immaterial; Issue-31 trades shown as the ledger has them.

### Lens R — reporting honesty (10 raised; lead reproduced R1, R2, R4, R5)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| R1 | **major** | CONFIRMED | `_spool` keeps only `description[:400]`; trade cards (`opened`/`closed`/`stop_loss`) and `recon_mismatch` carry their content in payload keys/fields → the 📦 Batched line is `HH:MM · closed: ` with no ticker/P&L. The "full text in the drained ledger" tail is false. | Reproduced: `budget_gate(closed…)` → spool → drain renders `01:53 · closed: `. |
| R2 | **major** | CONFIRMED | `morning_brief` (07-27) was never added to `BUDGET_SCHEDULED`; with the budget on it is spooled every weekday — the owner never gets the pre-open card. | `budget_gate({"event":"morning_brief"})` → spool; `"morning_brief" in BUDGET_SCHEDULED` → False. |
| R3 | medium-high | CONFIRMED on inspection | ALWAYS pages count toward the 5/day budget, so after 4–5 `live_exit_needs_review` pages the CEO/EOD cards spool; the queue is drained at BUILD so a spooled/failed EOD loses the Batched field. `live_entry_needs_review` still spools (#133 covered exits only). | `notifier.py:472-480, 599`; `eod_summary.py:457`; `ceo_brief.py:1074`. (= B4.) |
| R4 | medium | CONFIRMED | "Brain Map W/L" (`eod_summary.py:104-121`) counts every `outcomes` row by date incl. rejected/pending shadows (`hypothetical`). | Mirror: 4 rejected refs have `outcomes` rows; 08-20 card "Resolved Today: None" beside "3W/0L". |
| R5 | medium | CONFIRMED | `ceo_brief.py:999`: "✅ Clean day" needs only `ops.ok and not issues.total`; a crashed log sweep returns `total 0, available False`. | Code. |
| R6 | medium | plausible | "Today's MTM P&L" is the primary's realized cash settled today (usually yesterday's exits); "flat today" when nothing settled. | `eod_summary.py:331-337`, `ceo_language.py:129-134`. Wording/basis ruling. |
| R7 | low | reproduced by lens | `batched[:1024]` blind cut before `_fit_embed`. | 16 rows → 1049 chars, tail cut. |
| R8 | low | plausible | `firm_mtm` returns `open=0` on any exception → "unrealized MTM +0 (no open positions)" while 15 spreads are open. | `firm_mtm.py:77-78`. |
| R9 | low | reproduced by lens | Ops card `content[:2000]` cuts the SELF-DISABLED block. | 15 problems → 2085 chars. |
| R10 | low (latent) | plausible | All-FORWARD_CONFIRMED macro sentence drops the "advisory only" disclaimer. | `ceo_language.py:110-118`. |

Refuted by the lens: IST date keys (cron refuses non-+0530 hosts); voided/Issue-31 rows correct; crashed `blocked_line` abstains; CEO risk field split not cut; ops card uses the text path (always sends); halt banner independent of the spool; net-delta sign correct.

## Fix plan (lead, 2026-10-09)

- **Batch A — the notifier (R1, R2, R3/B4, R7, B2):** spool the WHOLE payload (render spooled cards as their embed's title + fields); add `morning_brief` to SCHEDULED; ALWAYS pages do not count toward the budget; drain the queue only when the carrying card was actually delivered (re-spool on failure/spool; `--dry-run` never drains); `_fit_embed` instead of the blind cut; fix `/pnl`.
- **Batch B — honest lines (R4, R5, R8):** W/L over approved, non-hypothetical outcomes only; "Clean day" requires the sweep and the journal to be available; `firm_mtm` abstains ("marks unavailable") instead of `open=0`.
- **Batch C — dashboard truth (T1, T2, T4, T5, T10):** chain the PAPER_10L % line across capital moves (clean sheet = visible break to 0, injection = continuous); net equity only when EVERY open position is priced (else `None` + "N of M priced"); CAGR on one window and only after 30 days (firm_mtm's rule); show the crossed capture with its basis on primary rows; a flat account's net = realized.
- **Batch D — stale approvals (B1):** stamp `decided_at`; a pending proposal from an EARLIER session is refused (`stale_proposal`, margin released) — policy default, owner may revise.
- **Rulings (unchanged):** B3 which text-path cards stay real-time; B5/B6; R6 "MTM P&L" basis; R9/R10 wording; T3/T6/T7/T8/T9.
