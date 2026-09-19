# HANDOVER.md — Cold-Start Brief

## 🤖 THE CONTINUOUS CONTEXT PROTOCOL — read this first, agent or human

*Established 2026-07-25, after a retroactive documentation cleanup found
`README.md` had been wrong for 19 days. Docs are maintained continuously now,
never in a big sweep. The binding copy of these rules lives in `CLAUDE.md`,
which loads automatically into every AI session; this is the human-readable
restatement.*

> **AI AGENT INSTRUCTION — NEW SESSION.** Whenever a new session starts, the
> very first action must be to read `HANDOVER.md` and `PROJECT_TIMELINE.md` to
> establish context, before writing any code.

> **AI AGENT INSTRUCTION — SESSION WRAP.** Whenever the user indicates the end
> of a session or completes a deployment, you MUST automatically update
> `HANDOVER.md` with the current exact state, open bugs, and next immediate
> steps BEFORE closing the session.

**For the owner — the one command at end of day:**

```bash
bash scripts/wrap_session.sh
```

It appends the day's commits to `PROJECT_TIMELINE.md`, runs the full suite as a
gate, and commits that one file. Add `--push` to push, `--dry-run` to preview,
`--skip-tests` on a docs-only day. It stages **only** the timeline — never your
in-flight work — and re-running it on the same day regenerates that day's entry
instead of duplicating it.

It deliberately does **not** write this file. Deciding what is genuinely broken
and what matters next needs judgment no script can extract from commit
subjects; it will remind you if `HANDOVER.md` looks stale, and updating it is
the agent's job under the Session Wrap rule above.

---

> **How to read the rest of this file.** It is REVERSE-CHRONOLOGICAL. The section
> immediately below is the current state; everything after it is the historical
> record, accurate as of its own date and left intact deliberately. If an older
> section contradicts a newer one, **the newer one wins.** For the narrative
> arc, see `PROJECT_TIMELINE.md`; for the reasoning, `DECISIONS.md`.

## 2026-09-19 (later) — The paper venue is live for entries; all 25 tier-1 names resolve (CODE, decision #101; deploy below)

**What changed.** (1) `darling_ids.json` rebuilt on the Mac with the tier-1
F&O names in the universe (`scrip_master._tier1_fo_symbols`): 148 ids, 0
unresolved, all 25 tier-1 covered; shipped to the VM. The archiver's
extension now resolves **23** names (25 minus the core RELIANCE/TCS), 0
skipped. (2) `src/execution/paper_venue.py`: sweeps PENDING legs, fills at
limit ± tier slippage via `oms.apply_fill`, rejects by name, stamps venue
fills onto the journal legs. (3) `decide_pending(approve=True)` → ticket +
venue sweep + stamp, before the rewrite, when `paper_venue_enabled`
(config, now **true**); `execution` record on every approved row; legacy
instant fill on flag-off or error. Tracker skips entry slippage on `venue`
fills. Tests: `tests/test_paper_venue.py` (15), OMS guard updated.

**DEPLOYED.** VM at `14595a6`, `alpha-trading` restarted; 122/122 scoped tests
green on the venv; `PAPER_VENUE_ENABLED` reads True on the VM; the extension
resolves 23 names / 0 skipped on the VM's own files; OMS tables created,
0 tickets (first one lands with Monday's first approval).

**Suite.** 2,248 passed, 1 failed (the same pre-existing `test_darling_shadow`).

**What the next person should do first.**
1. Monday: the first approved entry should carry `execution.mode =
   paper_venue` and `spread.ticket_id` in the journal; `trade_tickets` /
   `trade_legs` on the VM should hold it FILLED with the 15:40 archived
   chain untouched. If `execution.error` is set, the legacy fill applied —
   read the error, nothing was lost.
2. Monday 15:40: `ls data/lake/chains/` should show ~23 new slugs; the
   21:00 court should report its first fires.
3. M2 next steps (each a decision): partial fills from the 15-min tape
   instead of a whole fill at the close; `margin_locks.parent_ref`; exits
   through the OMS; the #94 broker-book sync as reconciliation.

## 2026-09-19 — Court unblocked (tier-1 chains) + Phase M2 OMS schema and one strategy router (CODE, decision #100; deploy below)

**What changed.** (1) `chain_archiver` now captures every tier-1 F&O name
after the core nine, by scrip-master id from `darling_ids.json`
(`get_expiry_list_by_id` / `get_option_chain_by_id` added to dhan_client),
2 expiries each, under `extension_slug(symbol)`; today 12 of 25 tier-1
names resolve (the other 13 are not darlings → no id → skipped BY NAME:
AMBER, ANGELONE, BOSCHLTD, GAIL, GLENMARK, …). The court's `chain_from_lake`
reads extension slugs and takes lot size from the chain nodes. Extension
holes are notes, never a CA-EMPTY/BLACKOUT on the core. (2) `src/oms.py`:
`trade_tickets` / `trade_legs` / `leg_events` (additive in brain_map.db),
states PENDING → PARTIAL → FILLED, PENDING → REJECTED, PENDING/PARTIAL →
CANCELLED, fills never reduced, VWAP average, ≤30-char deterministic
`correlation_id`. (3) `src/strategy_router.py`: ONE routing table
(exposure_gate + glassbreaking derive from it), `build_ticket` (pure,
longs-first) and `issue`. **Nothing on the live path calls `issue`; no
venue exists; Rule 7 unchanged.** Tests: `tests/test_oms.py` (12), +5 in
`test_chain_archiver.py`.

**DEPLOYED.** VM at `07cca58` (~10:xx IST 09-19), `alpha-trading` restarted;
100/100 scoped tests green on the venv; on the VM's own files the extension
resolves 12 names (abb, bajaj_auto, bse, dixon, godrejcp, godrejprop, hal,
heromotoco, kei, lauruslabs, solarinds, trent), 11 skipped by name. First
by-id capture is Monday 15:40 (today is Saturday).

**Suite.** 2,233 passed, 1 failed (the same pre-existing `test_darling_shadow`
calendar failure), 60 s.

**What the next person should do first.**
1. After Monday's 15:40 archiver run: `ls data/lake/chains/` on the VM should
   show ~12 new slugs (abb, bajaj_auto, …); the 21:00 court should then
   report fires > 0 for the first time. If the by-id chain call answers
   nothing, the log names it under "tier-1 extension".
2. Widen `darling_ids.json` to the whole tier-1 list (a `scrip_master`
   `--symbols` run on the home node) so the other 13 names resolve.
3. M2 sequence, each a decision: `margin_locks.parent_ref` + exposure gate on
   parent; a PAPER venue that drives `oms.apply_fill` from the 15-min tape
   and archived chain (partials, rejects, real-quote slippage); the read-only
   broker-book sync (#94) as the reconciliation source; only then the Dhan
   Trading API, by a numbered decision lifting Rule 7.

## 2026-09-17 — Session close (docs only): Mini PC not yet live, may be auto-suspending

**State.** VM at `41fdbb4` (+ docs `f7409b7`), all services active, suite 57 s /
2,216 green. The Mac STILL owns the home lane. The Mini PC has Claude Code
installed and three messages queued in its session ("New mini PC" via
ListAgents, one-way): (1) bootstrap + report on branch `mininode/bootstrap`,
(2) cron install + one forced sync, (3) stop auto-suspend (owner reports the
box switches itself off). None has reported back yet — the owner must approve
them in that session. Owner-only steps outstanding on the node: copy `.env`
by USB/scp, `gcloud auth login` + project, BIOS "Restore on AC Power Loss =
Power On".

**What the next person should do first.**
1. `git fetch && git log origin/mininode/bootstrap` — has the node reported?
2. If the Mac lane is still on the Mac, keep the lid open at 07:30/12:30/19:20
   and Saturday 09:30–11:00 until the node ships 7/7; then retire the Mac
   agents (CRON_SETUP.md).
3. Watch tonight's 21:00 court sitting and tomorrow's ⚖️ CEO field; the
   16:30 card should now show `REJECTED_POOR_RR` counts if any fired.

## 2026-09-16 (evening) — Home node: the Mac lane ported to the Linux Mini PC (CODE, decision #99)

**What changed.** `scripts/node_env.sh` (one interpreter resolver, Mac or
Linux), `scripts/setup_mininode_cron.sh` (the node's crontab: 3 sync slots,
edge miner, evolution, Saturday scrip + recalibration; refuses macOS / the
VM / a non-IST clock; never touches the token), `mac_auto_sync.sh` /
`mine_edges.sh` / `run_evolution.sh` now source the resolver,
`ollama_session.sh` finds the binary, `config.GCLOUD_PATH` falls back to
PATH. CRON_SETUP.md has the node section + the Mac-retirement command.
Tests: `tests/test_mininode.py` (7).

**Where the migration stands.** The Mini PC's own Claude Code session
("New mini PC") was sent a bootstrap task (clone, env report, venv + suite,
`.env` presence by key name, gcloud auth state) and asked to push
`docs/mininode_bootstrap_report.md` on branch `mininode/bootstrap`; that
route is one-way, so check the branch. Not yet done, in order: the owner
copies `.env` by USB/scp and runs `gcloud auth login` on the node; the node
runs `bash scripts/setup_mininode_cron.sh`; first hand `mac_auto_sync.sh
--force` from the node ships 7/7; THEN the Mac's LaunchAgents/crontab are
retired (command in CRON_SETUP.md). Until then the Mac still owns the lane.

**Also this session — RULE 6, ledger Issue 29.** The suite had crept to 24
minutes: five tests were dialling Dhan (via `h4_shadow`'s default bars door
inside sleep_phase) and Gemini (the post-mortem analyst) for real, hidden
while both answered fast. Muzzled at the doors (`dhan_client._get_client`,
`analyst.generate_post_mortem`, `h4_shadow`). **Suite is now 57 s, 2,216
passed**, 1 pre-existing failure. CLAUDE.md RULE 6 numbers updated.

**What the next person should do first.**
1. `git fetch && git log origin/mininode/bootstrap` — read the node's report.
2. Walk the owner through `.env` copy + `gcloud auth login` on the node.
3. Install the node cron, verify one 7/7 ship, retire the Mac's agents.

## 2026-09-16 — Reward-to-risk guardrail (CODE, decision #98; DEPLOYED)

**What changed.** `strategy.reward_risk_gate`: max_profit / max_loss on the
BUILT structure must clear **1.5** (bull call / bear put) or **0.35** (iron
condor / butterfly — structurally inverted; six of the ledger's nine sit
0.32–0.67) or the trade is refused `REJECTED_POOR_RR: R:R below <floor>
threshold — …` before sizing, margin or any card. Wired into
`options_proposer.build_proposal` (after fill basis, before `size_lots`;
`rejected_spread` still goes to the ghost tracker), both shadow strategies,
and `proposal_ledger` (new fate). Every built spread carries `reward_risk`.
Tests: `tests/test_reward_risk.py` (16).

**Deployed.** VM at `5496220` + this docs commit, `alpha-trading` restarted
(the proposer runs inside it); smoke test on the box: the 3k-vs-9k example
returns `REJECTED_POOR_RR … (R:R 0.33)`.

**Finding for Dept 8 (not acted on).** The Phase-7 synthetic chain prices a
2%-OTM condor with 4-step wings at R:R 0.26–0.33 at every expiry the replay
uses — below the floor — while real condors mostly clear it: the model's
OTM premium is too thin at the shorts. `test_simulator.py` /
`test_execution_timing.py` now carry a richer fixture chain so the replay
mechanics stay testable; the shipped model is unchanged.
`test_options_proposer.make_chain` base premium 100→160 for the same reason.

**RULE 6 leak fixed.** `test_an_unknown_vix_refuses_both_range_structures_fail_safe`
called the LIVE `get_india_vix()` (vix=None path) and failed on the VM with
a valid token — pre-existing (fails at `3f18f25` on the box, passes with the
token blanked). Now pins the VIX read.

**Suite.** Mac: 2,204 passed, 1 failed — the pre-existing `test_darling_shadow`
calendar failure. The `test_options_spreads` → `test_intraday_exit` ordering
leak also still stands.

**What the next person should do first.**
1. Watch the proposal ledger for `REJECTED_POOR_RR` over the first sessions
   — how many condor and bear-put proposals the floor removes.
2. Dept 8: decide whether to recalibrate `simulator.build_synthetic_chain`
   so backtests price condors the desk can actually trade.

## 2026-09-15 — Loss post-mortem + THE PROVING COURT sits nightly (CODE, decision #97; deploy below)

**Post-mortem (ledger Observation 09-15).** The ₹6,213 realized drop is ONE
trade: `eqd:b8de8cfa` SUPREMEIND.NS, weak_buy darling entered 09-03 at
₹3,546.40 ×28, stopped out **09-15 11:20 IST** at ₹3,350 (₹16.76 through the
₹3,366.76 stop), R −1.09, pnl −₹6,212.94. Not 09-12→14. Position was
unmarked on most sweeps (50/52 quote failures); whether the stop crossed
earlier is unverified.

**GAP 1 closed (code).** `src/validation/run_proving_court.py` — ages
CANDIDATEs → TRIAL (7 d), enrols the Glassbreaking primitives, seeds 10
placebos per ISO week, feeds bhavcopy bars for the 5 archived option
underlyings + tier1 F&O names, prices only off the 15:40 archived chain,
fires/grades shadows through `trial`, scorecards every hypothesis (n / wins
/ Wilson LB vs null), `evaluate_trial` past the floor, audits placebo FDR,
writes `data/proving_court.json`. `ceo_brief` gained the ⚖️ Proving Court
field (STALE after 2 days = liveness). Cron 21:00 (`setup_cron.sh` #31);
deliberately not a 20:30 heartbeat job. Tests: `tests/test_proving_court.py`
(15) + 3 in `test_ceo_brief.py`.

**DEPLOYED + FIRST SITTING.** VM at `f03d4b3` (~20:1x IST 09-15), cron
line #31 installed (`crontab -l` shows it), 101/101 scoped tests green on
the venv. The court sat once by hand: **promoted 9 → TRIAL, enrolled 2
primitives (TRIAL 11 total), seeded 10 placebos (batch court-w202638), feed
28 names / 7 signals / 0 fires** (6 `no_chain_for_underlying`, 1
`no_listed_options`; `with_today_bar: 0` — today's bhavcopy had not landed
by the run), graded 0, FDR insufficient placebo n (10). `data/proving_court.json`
written; the ⚖️ field renders. No service restart needed (cron-only code).

**Suite.** 2,191 passed, 1 failed — the same pre-existing `test_darling_shadow`
calendar failure.

**What the next person should do first.**
1. Read tomorrow's 16:30 CEO card: the ⚖️ field's first real numbers
   (expect n=0 everywhere, placebos seeded 10, TRIAL count ≥ 11).
2. Coverage: tier1 names fire as `no_chain_for_underlying`; widening the
   chain archiver's universe is the lever if the court needs more cases.
3. Dept-3: an unmarkable desk position should be a card (post-mortem follow-up).

## 2026-09-12 — System blueprint + gap analysis (DOCS ONLY, no code change)

**What happened.** Architect paused feature work for a structural review.
Wrote `docs/SYSTEM_BLUEPRINT.md`: ingestion → storage → memory → routing
(live vs shadow) → risk/execution → telemetry, with the VM's real counts as
of 09-11, and a three-gap analysis: **GAP 1** the Proving Court has never
heard a case (9 candidates all CANDIDATE, 0 organic shadow fires, 0
placebos, 0 graded declarations, new M4A hypotheses with no feed); **GAP 2**
single token / single box / sleeping laptop / model marks — the 09-07→09-10
blindness is a structure, the 09-11 alarms only detect it; **GAP 3** no order
lifecycle or per-leg schema (one r_multiple per ref, one lock per ref),
routing duplicated between `options_proposer` and the unwired
`trade_planner` matrix. Smallest fixes named per gap. Indexed from README
and ROADMAP. Doc drift recorded (treasury 19:50 vs 19:56, "31/31" vs 30
lines, "4 services" vs 3 named, XRAY §9 fixes 1–4 already done).

**What is live now.** Unchanged: VM at `1deb165`, services active, no deploy.

**What the next person should do first.** Read the blueprint §7 before
proposing any new pipeline; GAP 1's nightly Dept-5 job is the recommended
first build after the freeze lifts (shadow only, one Discord slot).

## 2026-09-11 (night) — Phase 4 "Glassbreaking Profits" (M4A) shipped as SHADOW (CODE; deploy below)

**What changed (architect authorisation, decision #96).** New
`src/strategies/glassbreaking.py`: `falling_knife` (RSI ≤ 30 or anchored-VWAP
support after a ≥ 8% drop) and `early_breakout` (gap-up ≥ 2% + volume ≥ 2×
avg or a bulk/block print; SMA 50/200 bypassed by design), each routed
ONLY to a bull call spread with a post-build defined-risk assertion, sized
at 0.5% of pool, carrying a +1R/+2R tranche ladder (max 3× base) and an ATR
trail on the underlying. Writes `logs/glassbreaking_shadow.jsonl`; enrols
each primitive as a TRIAL hypothesis in the Proving Court (registry +
trial); `court_scorecard` gives n / wins / Wilson LB against the court's
own bar. **On no schedule.** `plan_tracker`: `atr_trailing_stop` (one
ratchet), tranche OBSERVATION on any plan with `plan.tranches`
(`outcome.tranches.pyramid_pnl_rs`, never booked), `_resolve_spread_trailed`
for the shadow grader only (live `_resolve_spread` unchanged — lock holds),
`trail_hit` digest label, time-stop on the `_today` seam. `journal._PLAN_KEYS`
now keeps `trailing` / `tranches` (they were silently dropped before — the
08-05 equity trail could never have fired on a real row).

**What is NOT done, on purpose.** No cron/sleep-phase task runs the shadow
yet (needs a candidate feed: bhavcopy bars via `bhavcopy_clerk.bars_for_many`
+ a chain read through `dhan_guard`; the module owns rules, not data doors).
Add-ons are not booked (M4A gaps 1–4 in PROP_ROADMAP). No pre-market feed
exists: the breakout gap leg is T+1 open vs prev close, or a live quote at
the open. The court verdict needs `harness_min_resolutions` (7) real
resolutions per primitive before it says anything.

**DEPLOYED.** VM at `f4aff47` (~19:1x IST 09-11), `alpha-trading` restarted,
`alpha-discord-bot` active; 95/95 scoped tests green on the VM venv; the
shadow module is on no cron line (verified: 0 matches in `setup_cron.sh`).

**Suite.** Full run: **2,173 passed, 1 failed** — the same pre-existing
calendar-dependent `test_darling_shadow` failure as the evening block.
New: `tests/test_glassbreaking.py` (17), `tests/test_tranches_trailing.py` (13).

**What the next person should do first.**
1. Decide the candidate feed for the shadow (Mac-side bhavcopy scan +
   chain read) and where it runs; then a nightly `run` + `grade` pass.
2. Watch tonight's 20:30 ops card (first live run of the RED alarms).
3. The two pre-existing test hygiene items (evening block) still stand.

## 2026-09-11 (evening) — Phase 1 hotfixes: expiry backstop + RED health alarms (CODE, deploy below)

**What changed (architect directive, hotfix under the V1 freeze, decision
#95).** `src/plan_tracker.py` gained a wall-clock **expiry backstop**: a
spread past its expiry with no on-or-before-expiry bar exit is force-settled
— at intrinsic on the last close ≤ expiry, or with no data at all at the
defined max loss after a 3-day grace — margin released, `settlement_basis`
stamped. `src/ops_monitor.py` gained three **RED alarms** that forbid the ✅
card: DH-901/902/903/906 in any log, ≥2 consecutive zero-capture sessions in
`intraday_15m.log`, MemAvailable < 100 MB. `src/ceo_brief.py` relabels DH-902
as the Data API subscription. `scripts/daily_health_and_queue.sh` runs
`src.ops_monitor --verdict`. Tests: `tests/test_expiry_backstop.py` (11) +
10 new in `tests/test_ops_monitor.py`.

**Stuck trades (Issue 28): already cleared.** The tracker settled all three
FIN SERVICE spreads itself on 09-10 12:03–12:04 IST when bars returned;
locks released, exits dated 08-18/21/24. Verified from the VM's live ledger
copy. Ledger entry appended.

**Suite.** Full run on the Mac 09-11: **2,146 passed, 1 failed** —
`test_darling_shadow.py::test_run_darling_cycle_resolves_forces_then_proposes_offline`,
which fails on HEAD *before* this change (calendar-dependent). Also
pre-existing: `test_options_spreads.py` leaks seams into
`test_intraday_exit.py` in that order. Neither touched. Deploy state is in
the line below this block once done.

**DEPLOYED.** VM at `69b1a87` (pulled ~18:30 IST 09-11), `alpha-trading`
restarted so the running tracker carries the backstop, `alpha-discord-bot`
active. On the VM (venv): 123/123 scoped tests green; `src.ops_monitor
--verdict` reads ✅ (25/25 slots captured today, 467 MB free); no open spread
is past expiry; 13 open locks, all live. First `--verdict` had read a false
RED from outage-era tail lines — fixed in `69b1a87` before this line was
written. `wrap_session.sh --skip-tests` used because the gate would trip on
the pre-existing darling failure; the full suite was run by hand (2,146/1).

**What the next person should do first.**
1. Read tonight's 20:30 ops card: it is the first live run of the alarms.
   A ✅ card tonight is the expected result (plan valid, 480 MB free).
2. Fix the two pre-existing test issues above (hygiene, freeze-compatible).
3. Everything in the 09-10 block's list still stands (DH-902 mislabel is now
   done; the FIN SERVICE item is closed).

## 2026-09-11 — Dhan Orders API scoped: read-only broker-book sync, PLAN ONLY (docs only, no code change)

**What is live now.** Unchanged from 09-10: VM at `b743cd5`, 4 services, 31/31
cron lines, Data API plan valid to 2026-10-10. No deploy today. No code
touched. Suite not run (docs-only day).

**What happened.** Planning session on integrating Dhan's Orders API.
Requirements were agreed with the owner and written to
`docs/dhan_broker_book_sync_plan.md` (v1.0); scoping recorded as
**decision #94**. Details were confirmed live against the official Dhan MCP
connector: today's order book, super-order book, trade book and positions
are all empty, holdings returns `DH-1111` (an empty state, not an error),
funds ₹1,411.18. Dhan's docs confirm static-IP whitelisting binds ONLY
placement (reads need none), no idempotency guarantee exists for orders,
and one-token-per-client-id (#48) still holds.

**Owner decisions (09-11).** First release = READ-ONLY sync (order/trade/super
books, positions, funds). Rule 7 NOT lifted — plan only, **build not
authorised.** Both desks reconciled from day one. Runs on the Mac with the
token COPIED FROM THE VM (never minted on the Mac). 15-min cadence in market
hours + EOD backfill. **No trading — infra readiness only**; the ₹1,411 Dhan
account is unfunded infra.

**What is broken / unwired.** Nothing new. The 09-10 list below still stands
(FIN SERVICE spreads past expiry, DH-902 mislabel, zero-capture card
missing, Issue 24 hand-add).

**What the next person should do first.**
1. Do NOT build the broker-book sync unless the owner says go. If they do:
   first commit = a new DECISIONS.md entry permitting a read-only reader +
   Rule 7 reworded to "no order *placement* path"; second = the NSE_FNO
   contract-id resolver (the one real prerequisite); then the reader per
   the spec. Never import `place_*`/`modify_*`/`cancel_*` from `dhanhq`.
2. Everything from the 09-10 block's "do first" list is still open.

## 2026-09-10 — Data-plan lapse, VM hang + reset, swap added (OPS ONLY, no code change)

**What is live now.** VM still at `b743cd5` (no deploy since 08-19), all 4
services active, 31/31 cron lines, **1 GB swap now on** (`/swapfile`, in
fstab), disk 81%. Dhan **Data API plan re-subscribed 09-10 11:43 IST, valid
to 2026-10-10** — the 11:45 sweep captured 88/88. The desk was BLIND
2026-09-07 → 09-10 11:43 (ledger Issue 26); the VM HUNG on 09-09 ~09:55 →
15:10 and was hard-reset (Issue 27).

**What is broken / unwired.**
- Three NIFTY FIN SERVICE spreads 16 days past their 08-25 expiry, still
  open, ₹73,845 locked, blocking every FIN SERVICE proposal — the tracker has
  no expiry backstop (Issue 28). **Not fixed.** Watch whether bars returning
  lets it settle them itself (exit would be dated 08-23).
- `ceo_brief` labels DH-902 "authentication not valid" — wrong; it is the
  data plan. No red card exists for consecutive zero-capture sessions;
  `daily_health_and_queue.sh` said `all_ok=True` through the whole outage.
- Stage B will have ~9 graded calls at the Oct-13 target; slow_burn has
  carried 0 strategies in 52 nights (ledger Observation 09-09). Decision
  needed on what the Oct-13 read means.
- 13 positions were unmarked through the outage; today's brief is the first
  honest MTM since 09-04. Issue 24's ₹26,982.14 hand-add still applies.

**What the next person should do first.**
1. Confirm tonight's CEO brief shows marks again and check whether the three
   FIN SERVICE spreads settled; if not, take Issue 28 to Dept 3.
2. Hotfix-size: relabel DH-902 in `ceo_brief`/ops sweep; add a
   "N sessions zero capture" card; add a `mem_available < 100 MB` card.
3. **Diarise 2026-10-10** — the data plan renews nothing on its own.
4. Human pulse: last human action 08-11; the 30-trading-day tripwire fires
   ~09-22 unless the owner touches `decide_pending`.

**Critic's readiness verdict (09-09, owner asked):** not ready for real
capital — 29 resolved trades, per-trade Sharpe 0.23, spread marks from a
linear model, and this week's outage went unpaged. Gate table in the
session transcript; nothing of it is in code.

## 2026-08-17 — Sequences 5 + 6: capital flow and the latching halt (DEPLOYED)

**What is live now.** VM at `20883b9`, pulled and verified 2026-08-17 ~11:15
IST. Two Dept-3 rulings shipped in one session, both from the architect,
both triggered by an audit rather than a bug report.

1. **Capital moves no longer launder the trading record** (`d3f3303`,
   decision #92a). `inject_capital` TRANSLATES `peak_equity` by the exact
   rupees moved instead of ratcheting it to the new equity, so the absolute
   distance `peak − equity` survives any deposit or withdrawal and drawdown
   measures TRADING only. The 2026-08-07 ₹8L injection had printed a 0.00%
   drawdown and a fresh high-water mark to a book that was 1.96% down.
2. **The risk-of-ruin halt LATCHES** (`20883b9`, decision #92b). Breaching
   the threshold writes `ruin_halt_latched` to the append-only
   `account_events` trail, and `trading_halted` reads that latch, not the
   live percentage — closing the dilution trap that (1) exposed, where a
   large enough deposit would drop the drawdown under 10% and disarm an
   armed halt. Nothing automatic clears it: not capital, not a recovering
   drawdown, not a new day, not a restart. **The only door out is**

   ```
   python3 -m src.portfolio_manager --reset-halt --why "..." --yes
   ```

   which refuses without a stated reason and re-arms instantly if the breach
   is still real. Supersedes the 07-27 "no override door" ruling in mechanism
   only — resume is still a deliberate human act, now a recorded one.

**Account state at deploy** (unchanged by the deploy): equity ₹10,18,628.04,
peak ₹10,39,423.99, drawdown 2.00%, **not halted, latch not armed, zero latch
rows**. Verified before pulling that the VM's `account_events` has never
contained a `risk_of_ruin_halt` row, so nothing latched retroactively.

**Also this session:** the P&L-gap audit that started it, written up as
**ledger Issue 24**. Reported realized (₹18,628.04) is ₹26,982.14 short of
settled P&L (₹45,610.18); the cause is the 2026-07-23 clean sheet reseeding
`account_state` while `margin_locks` kept history, plus the duplicated,
never-settled 07-06 TCS rows. Every trade reconciles exactly — no engine bug.
**Those historical rows were deliberately NOT rewritten** (RULE 3).

### What the next person should do first

1. **One transient test failure on the VM, unreproduced.** During the deploy
   smoke run at ~11:0x IST (market open, live loop writing),
   `tests/test_portfolio.py::test_pm_run_headless_silently_rejects_when_the_gate_says_no`
   failed at line 376 (`len(logged) == 1 and len(notified) == 1`) — an
   assertion about journal/notify counts, unrelated to the halt work. It then
   passed 5/5 in a row at the same commit, isolated and paired. Most likely a
   hermeticity leak (the test seeing production state mid-session), which
   RULE 6 forbids. **Re-run it during market hours to catch it again** before
   deciding it is benign.
2. **Any lifetime-performance figure needs ₹26,982.14 added by hand** to what
   `account_state` reports. Ledger Issue 24 follow-up (b), still open.
3. Suite: **2,118 green on the Mac** (~235s), 70/70 green on the VM for the
   two touched files.

## 🔴 PENDING ISSUES / BACKLOG — carried forward, not archived

*Created 2026-08-11 when this file was split. Every item below was an OPEN
issue somewhere in the historical blocks that now live in
`docs/handover_archive.md`. They are here so that archiving history cannot
silently archive a problem with it. Each line names where the detail is.*

**Resolved-on-review and therefore NOT carried:** `decay_engine.apply_decay_sweep`
appears as "UNWIRED, latent bug, decision pending" in the 07-25 block — it was
WIRED on 07-30 (`426fc34`, sleep-phase Task K). The Auto-Discovery statistical
foundation (circular-shift bootstrap, ragged missingness) reads as "next
session" in one 08-01 passage and as DONE earlier in the same block. **Newer
wins** — that is the file's own rule, and it is why date-blind archiving would
have been wrong in both directions.

### Deliberately deferred — not forgotten, not bugs

| Item | Status as recorded |
|---|---|
| **AD-2 motif gate** | **DELIBERATELY UNBUILT.** Any non-shock candidate returns `motif_gate_pending` and can never be admitted; the DTW-statistic surrogate test is the missing slice. Deferred on purpose. |
| **AD-3 Dept-5 registry enrolment** | `route_to_court` is built; full enrolment is not. Nothing to route yet, so no urgency. |
| **`2026-03-27`, stress 3.65** | A **forward-data WATCH item, NOT a discovery.** Large, recent, unlabelled; survived the statistic redefinition. Re-run the scan periodically and watch for a cluster. **Do NOT wire it to anything.** |
| **`equity_desk_snapshot.json`** | PARKED as `.parked` on both machines. Superseded by the one-database design (#83); kept, not deleted. |
| **Stage-B forward scoring** | Declarations accumulating, **0 graded** — the gate is CALENDAR TIME, not code. Nothing to do but wait. |
| **`claude/hello-d9m45n` (PR #14)** | Parked branch, still holds unmerged doc work. Owner-gated; do not touch it in passing. |

### Open and actionable

| Item | Where it stands |
|---|---|
| **Bug ledger: 2 active rows** | 74 triaged on 08-05, 72 retired to already-fixed causes, **2 remain active**. `python3 -m src.bug_ledger --report`. |
| **`suggest.py` DH-905 on historical calls** | `Input_Exception` ("Missing required fields, bad values"), e.g. `id=1333 NSE_EQ/EQUITY 2024-05-17->2026-08-05`. **Not a rate limit.** Pre-existing, fires every morning at 08:03, untriaged. |
| **GOLD_INDIA contract id expired** | `CA-410` since 2026-08-05; the cross-asset tap captures CRUDE only. Tracked as ROADMAP V1.3 step 1. |
| **Global index ids unverified** | `config/global_indices.json` ships EMPTY BY DESIGN — no id has been scrip-master-verified. ROADMAP V1.3 step 2. |
| **NIFTY MID SELECT: no tradeable quotes** | Every cycle, both sessions. Worth one look before assuming it is a liquidity fact. |
| **Midcap momentum leg reads 0** | NIFTY MID SELECT has no parent in `config/sector_universe.json`. One-file analysis-side addition, freeze-compatible. |
| **`report_downloader`'s dead crawl** | An orphan decision from the 08-05 queue: keep, fix or retire. Never resolved. |
| **Reporting gaps, `SYSTEM_XRAY.md` §9 fixes 1–5** | Named as "the recommended next item" on 08-05 and not started since. |
| **Dashboard surfacing** | Recorded as "still open" in the 07-27 block; no owner directive since. |

### Standing constraints that outlive any block

- **V1 CODE FREEZE** (owner, 2026-08-05) — no new execution features. Hotfix
  only on real breakage. Observability and hygiene are permitted.
- **The Macro Regime Engine has ZERO execution authority.** Wiring any of its
  output to sizing or entry is a Department 5 decision gated on a passed
  statistical test — never a code change taken on initiative.
- **Paper money only.** No broker/order path exists in `src/`; do not add one.
- **`research_archive/` is on NO execution path.** Never import it from `src/`.

---

## 📍 CURRENT STATE — 2026-08-16 (Sunday): miner gate 60→50, Next-Version staging, weekly cadence ahead

Suite **2,071 green** (before this block's change; re-run recorded in the
handoff). V1 `src/` execution logic untouched all day; three things moved:

1. **Miner depth gate lowered 60 → 50 → 40 frames** — `src/discovery/nightly.py`
   `MIN_CONTEXT_FRAMES = 40` (owner executive override, **decisions #87 + #88**,
   both 08-16: the second cut was made to force the first pass THIS week).
   The other two gates and CANDIDATE-only mining are unchanged. VM
   `daily_context` = **38 frames on 08-16** (verified on the box, ~1/day) →
   40 on the 08-18 frame → **first miner pass 20:20 IST Tue 2026-08-18**
   (08-19 if the frame lands late or heartbeats/ingestion block). Watch
   `logs/discovery_nightly.log` and the CEO card's `consecutive_skips` line;
   a run with 0 candidates is a valid outcome, not a bug. Deployed to the VM
   at `4941697` (gate 50); the 40 cut needs one more `git pull`.
2. **Next Version update STAGED, in prep.** The V1 CODE FREEZE (08-05) is
   nearing its end. **Post-update the engineering protocol becomes a strict
   WEEKLY RELEASE / UPDATE CADENCE** — one gated release per week (suite
   green, `wrap_session.sh`, HANDOVER block), iterative edge deployment.
   Until the update actually lands, freeze rules still bind.
3. **V2 R&D sandbox FROZEN** after three studies (elections, steel-proxy
   shocks, earnings reaction) all returned not-a-finding; Insolvency /
   Defaults remains the only OOS-verified edge. State in
   `docs/v2_sandbox_state.md` §9; `src/research/earnings_reaction.py` added.
   Also fixed: `tests/test_portfolio.py` headless margin-gate test read the
   REAL journal (RULE 6) — stubbed (`0054289`).

4. **Week-1 release: `src/strategies/insolvency_short.py` — SHADOW ONLY
   (decision #89).** Bear put spread only, F&O+tier1 gate (fail-closed),
   0.5% risk cap, day-5 time exit, `logs/insolvency_short_shadow.jsonl`, no
   cron. **Do not wire it to capital:** the F&O subset of the insolvency
   study is 19 filings / 3 names with a POSITIVE median and tier1 has ZERO
   events in 7 years — the edge is in non-F&O pennies. Jul-Aug 2026 dry
   run: 30 triggers, all `not_in_fo`. Next step if the owner wants it live:
   a Dept-5 F&O-subset study, which cannot pass today.
5. **M1 reporting honesty (Sequence 1):** Wilson line, drawdown line and
   blocked line verified live on EOD + CEO cards; `blocked_line` now also
   counts proposal-ledger risk refusals (margin / risk cap / budget /
   exposure) — `eod_summary.risk_refusals_today`. Telemetry only.

6. **Corporate-action adjuster (Sequence 2, decision #90):**
   `src/ingestion/corporate_actions.py` + `config/corporate_actions.json`
   (49 rows, all `verified_against_nse_circular: false`). `bars_for` /
   `bars_for_many` now return split-adjusted HISTORY by default; the latest
   bar is always raw. Verified: BAJAJFINSV 2022-09 reads 1,720→1,784, not
   17,205→1,784; latest bar factor 1.0. **The miners do not read prices** —
   this protects the analysis layer (tiers, pricer, router, valuation).
   Owner task when convenient: confirm rows against NSE circulars
   (`--scan SYMBOL` lists candidates; `--yf-check` is Mac-only).

7. **Sequence 3 (08-17, decision #91):** `🤖 Pattern Miner` field on the
   16:30 CEO brief (`ceo_brief.collect_miner`, read-only) — tomorrow's
   card will say whether the 20:20 pass ran and what it registered.
   `src/liquidity_slippage.py`: paper fills now pay tier slippage (0.10 /
   0.25 / 0.50% per side by `fo_liquidity` tier); STOCK was 0.0% and the
   equity desk settled frictionless. Expect equity-desk `pnl_net` to read
   ~0.2–1.0% lower per round trip from here on — that is the fix, not a bug.

Open items carried unchanged from the backlog above (DH-905 at 08:03, NIFTY
MID SELECT quotes, `report_downloader` crawl, 2 VM bug-ledger rows).

---

## 📍 CURRENT STATE — 2026-08-11 (Tuesday, pre-open, later): the Mac's dependencies now push themselves

Suite **2,000 green**. Ran for real before the open — **every artifact the
VM consumes is `fresh` right now.**

### The router's sector leg is live again

`sector_index_bars` was **producerless from 07-16 to 08-05** and unscheduled
after that, while still feeding a LIVE bullish veto — the bug this whole
staleness module was built for. It is now **0.0d old on the VM**, and the
router's ranking moved as a result (INFY 0.77 → 0.40, RELIANCE 0.33 → 0.36,
TCS 0.06 → 0.12): the momentum leg had been comparing today's stock closes
against **08-05** sector closes.

`bars_cache` (31 days) is refreshed too — 1,185 bars each for NIFTY 50 and
NIFTY BANK, 892 VIX sessions. It always had a refresher
(`evolution.refresh_bars_cache`); what it never had was a caller.

### Why these stayed Mac-native — it was checked, not assumed

`scripts/fetch_sector_bars.py` says **MAC-ONLY, NEVER RUN ON THE VM** and
means it: Yahoo rate-limits/blocks datacentre IPs, and `src/` must stay free
of a yfinance import. Valuation is the same story from the other direction —
the corpus is Mac-side. So the VM *cannot* build either; it can only be
shipped them. **The split is now explicit**: the VM builds what its own
bhavcopy supports (pricer + tiers, 19:18/19:22), the Mac pushes only what
the VM genuinely cannot compute.

### `scripts/mac_auto_sync.sh` + `com.aditrader.sync.plist`

Runs the Mac-only producers and ships **6 artifacts** through
`firm_treasury.vm_push_file` (the one Mac→VM lane). It **deliberately does
not ship `darling_tiers.json` / `darlings_levels.json`** — those went
VM-native yesterday, and a Mac copy over them would reintroduce exactly the
staleness this ends.

launchd, not cron, and for a measured reason: macOS cron does not fire while
asleep and never catches up (audited 08-04 — the Mac missed 4 of 11 weekdays
with **no log line at all**). `RunAtLoad` + hourly `StartInterval` coalesces
on wake, so the sync happens within an hour of the lid opening. The script
throttles itself to one real run per 3h; the agent ticking and doing nothing
is the intended shape.

**⚠️ THE OWNER MUST RUN THREE COMMANDS ONCE** to activate it — until then
this is a manual script and nothing runs it. They are in the plist's own
header comment and in the developer handoff.

**⚠️ `/bin/bash` needs Full Disk Access** or launchd cannot read a repo under
`~/Documents` (the 07-09 edge-miner TCC lesson). It is already granted on
this Mac; a macOS upgrade can revoke it silently, so if the agent goes quiet
check that first.

### Two staleness records corrected

`sector_index_bars` and `bars_cache` both carried `producer=None` in
`staleness_guard`. Both now name the sync agent, and the headline test moved
from asserting "NO PRODUCER" to asserting the producer is **named** — a
stale artifact whose refresher is unnamed is the harder incident.

---

## 📍 CURRENT STATE — 2026-08-11 (Tuesday, pre-open): the three Monday blockers are cleared

Suite **2,000 green**. VM at the latest commit, **31 cron lines**, no
duplicate schedules. No entry/exit or strategy logic touched.

### 1. Auto-approve is armed again — the tripwire was working, not broken

`PAPER_AUTO_APPROVE=1` was set the whole time. The **human-pulse
tripwire** had paused auto-approval because the last human decision was
**2026-08-04** and the threshold is 3 trading days. That is the
supervision contract doing its job while the owner was away.

Monday's three NIFTY FIN SERVICE condors were **rejected, not approved**:
their premiums are Monday's, so approving on Tuesday would journal a fill
at a price that never traded, and the #68 exposure gate would have blocked
the 2nd and 3rd as duplicates anyway. Decision #31 keeps them tracked
hypothetically, so nothing is lost. Those decisions ARE the pulse — the
tripwire is re-armed (`tripped: False`) and the pending queue is empty.

**This will recur** every time the owner goes 3 trading days without
touching `/pending`. It is by design; the card that fires says so.

### 2. Archiver depth 2 → 3, and the ghost book nearly doubled

The 08-10 ghosts wanted the **third monthly (2026-10-27)** — `horizon_for`
lets the proposer reach 90+ days — and the archive stopped at the second.
**The archive has to reach as far as the proposer does.** Live run
confirms `['2026-08-25', '2026-09-29', '2026-10-27']` for every
monthly-only name, zero DH-905, disk unchanged at 2.3 GB free.

Re-marking Monday with the deeper archive: **5 priced → 12 of 19**, and
the hypothetical total moved **+₹3,296 → +₹19,844**. ⚠️ Treat that number
as indicative only: it is 1-lot-assumed on trades the sizer had refused to
zero, and EOD `last_price` on an illiquid strike can be a stale mark.

### 3. The `expiry: None` bug — root cause was the stage, not the underlying

A **gate-stage** refusal (exposure #68, margin) hands back the BUILT
proposal, whose expiry lives at `proposal["spread"]["expiry"]` — there is
no top-level `expiry` key. So those rows wrote `expiry: None` and their
ghosts could never find a chain. Build-stage refusals were never affected
because `build_proposal` returns its own `expiry`, which is exactly why
the hole looked NIFTY-BANK-specific. `spot` had the same shape of bug and
now reads `entry_spot` off the spread. **Monday's 5 orphaned rows stay
unpriceable** — the fix applies to rows written from today forward.

### 4. The VM now builds the equity desk's eyes itself

`dynamic_pricer` **19:18** and `darling_tiers` **19:22** (Mon-Fri), after
this box's own 19:15 bhavcopy. The Mac had not run since 08-05, the
artifacts were 5.1 days stale, and the desk **fails closed** on a stale
tier table — so every further day of Mac sleep was a day it could not
enter a darling. Both now read **fresh (0.0d)**; first VM-native grading
produced 14 sane family transitions.

**⚠️ `valuation_scorer` is deliberately NOT scheduled on the VM, and this
is the trap of the day.** Measured here: it scores **0 of 109** darlings
because the fundamentals/deep-read corpus is Mac-only, and it **overwrites
`darlings_valuation.json` with an empty result** — which drives EVERY
darling to `ungraded` on the next grading pass (100+ transitions, and the
desk sees no tradeable darlings at all). I hit this live; a `--dry-run`
caught it and the Mac's copy was re-shipped to repair it. `darlings_queue`
/ `darlings_valuation` / `darling_pins` remain **Mac-shipped inputs** on a
weekly screen cadence.

**Why 19:18 and not the 16:00 that was asked for:** NSE publishes the full
bhavcopy after ~18:00, so a 16:00 run would grade on yesterday's close
while making the file *look* fresh — worse than being honestly stale.

### ⏭️ Watch today

1. **09:15** — auto-approval should resume; a fill journals as `approved`,
   not `pending_approval`.
2. **19:18 / 19:22** — first scheduled VM-native pricer + tier run.
3. `python3 -m src.ghost_tracker --date 2026-08-11` after the close — new
   rows should carry a real expiry on gate-stage refusals.
4. Still open: `bars_cache` is 31 days old with **no producer on any
   schedule**, and `sector_index_bars` is Mac-produced and stale — the
   router's sector leg reads off it.

---

## 📍 CURRENT STATE — 2026-08-07 (Friday, SESSION CLOSE): post-session health check — clean session, but read what the router did and did NOT do

**This is the session-wrap block. Start here Monday.**

Suite **1,997 green**, working tree clean, VM at `cb4098b`.

### The Friday session itself: clean

`09:15:00 → 15:30:01`, all nine underlyings armed, **zero tracebacks,
zero crashes, zero fatal exceptions** in `master_scheduler.log`. Entered
nothing — every candidate refused on the ₹10k cap, margin, exposure or
missing quotes, which is the same picture as Thursday and the reason the
₹10L injection and the proposal ledger exist.

### ⚠️ THE THING NOT TO MISBELIEVE: the router did NOT run on Friday

**Zero `underlying router:` lines in Friday's log.** The fix landed at
~15:1x and the scheduler process had been running since 09:10, so the
session executed the OLD code end to end. The same is true of the
proposal ledger — `data/proposal_ledger.jsonl` **does not exist yet** on
the VM, and `ghost_tracker` correctly reports "no refused trades" for
both 08-06 and 08-07. Nothing failed; the code simply was not in the
running process.

**Everything shipped today is first exercised at Monday 09:10.** Do not
read Friday's clean log as evidence that the router, the ledger or the
ghost book work in production — they have only been proven by direct
invocation, which is a weaker claim.

### The corrected router reading (complete lake)

The bhavcopy backfill finished 16:07 (**100 day-files, 2026-03-23 →
2026-08-07**). Against the full lake:

```
HDFCBANK.NS 1.00 (rs −1.00) > ICICIBANK.NS 1.00 (+1.00) > INFY.NS 0.77
(−0.77) > RELIANCE.NS 0.33 (−0.33) > TCS.NS 0.06 (−0.06) > the four
indices 0.00 (rs — (no bars))
```

63-session spreads: ICICIBANK **+11.78**, HDFCBANK −5.38, INFY −3.86,
RELIANCE −1.63, TCS −0.28. **`RS_SATURATION_PCT = 5.0` needs no change** —
it clips the two genuine outliers and leaves the middle ordered. An
earlier note in this file said all five saturated; that sample was taken
mid-backfill against a partial lake and must not be quoted.

*(A caution about the polling that produced it: `pgrep -f bhavcopy_clerk`
matches the SSH shell whose own command line contains that string, so it
reported the job alive long after it finished. Use
`ps -eo cmd | grep -E 'python -m src[.]ingestion[.]bhavcopy'`.)*

### Archiver: 9/9 captured, zero errors — with one honest correction

The **15:40 cron ran the OLD 2-underlying code** (the expansion deployed
at ~16:5x). The nine partitions on disk for 08-07 come from the manual
run at 16:53–16:55. **`chain_archiver.log` contains zero errors of any
kind, and zero DH-905 from the archiver.**

**A mistake I made and then repaired:** the manual run rewrote
`chains/banknifty/date=2026-08-07` with 2 expiries over the cron's 4,
dropping two far-dated snapshots that decision #36 says are not
retrievable. Because the market was closed the same closes were still
available, so BANKNIFTY was re-captured at depth 4 and the partition now
holds **4 rows (08-25, 09-29, 10-27, 12-29)**. From Monday the cron
captures BANKNIFTY at 2 by the new policy — deliberate, and the reason is
in `chain_archiver`'s own comment.

### DH-905: present, but not what the name suggests here

`suggest.log` (08:03), `ops_monitor.log`, `ceo_brief.log` carry DH-905
rows — all `Input_Exception` ("Missing required fields, bad values for
parameters") on **historical** calls, e.g. `id=1333 NSE_EQ/EQUITY
2024-05-17->2026-08-05`. That is a **bad-parameter** failure, not a rate
limit, and it is pre-existing and unrelated to the archiver expansion.
Worth triaging on its own; do not read it as rate-limit pressure from
today's changes.

### ⏭️ Monday, in order

1. **09:10** — first session on the new code. Watch for the
   `underlying router:` line (stocks with real `rs`, indices `— (no
   bars)`) and for `data/proposal_ledger.jsonl` appearing.
2. **15:40** — first *scheduled* nine-way archiver sweep.
3. **After the close** — `python3 -m src.ghost_tracker` for the first
   real ghost read; all nine underlyings are priceable from 08-07 on.
4. Triage the `suggest.py` DH-905 bad-parameter calls (id 1333).

---

## 📍 CURRENT STATE — 2026-08-07 (Friday, pre-weekend sync): all nine chains are captured, the desk budget matches the pool

**Deployed and verified live.** Suite **1,997 green**. No strategy, gate,
entry or exit parameter was touched — this was data capture and budget
limits only.

### 1. Chain archiver 2 → 9 — **first live run captured all nine**

```
NIFTY 50 4 | NIFTY BANK 2 | NIFTY FIN SERVICE 2 | NIFTY MID SELECT 2
RELIANCE.NS 2 | HDFCBANK.NS 2 | ICICIBANK.NS 2 | INFY.NS 2 | TCS.NS 2
```

Expiry depth is **per underlying** now: NIFTY keeps 4 because it is the
only index still carrying weeklies; everything else takes 2, since
FINNIFTY/MIDCPNIFTY and the five stocks are monthly-only and a 4-deep
sweep reaches contracts nobody trades. Slugs `nifty`/`banknifty` are
permanent — renaming them would orphan the history already on disk.

**Rate limits — this is the third protection, not the only one.** The
host-wide `_throttle()` already spaces every Dhan call ≥1.1s across
processes (the DH-905 fix), and 15:40 is *after* the scheduler
self-terminates at 15:30, so the sweep never races the live loop. Added
`UNDERLYING_PAUSE_SECONDS = 5.0` so nine names drip rather than burst.
Measured live: **~28 chain calls, well under 2 minutes, zero DH-905**.

**Storage is a non-issue**: the whole chain lake is 2.8 MB; today's nine
partitions cost ~150 KB. At that rate a year is ~35 MB against 1.8 GB
free.

**One bug this immediately exposed, fixed the same hour:** `ghost_tracker`
carried its OWN copy of the slug map, so it kept calling seven
underlyings unpriceable while their fresh chains sat on disk — silently,
with no error. It now imports the map from the archiver, pinned by a
test. **Verified end to end: a TCS.NS ghost prices**
(`status PRICED, price_source archive:2026-08-07, pnl −11,452.50`).

### 2. Treasury: equity desk ₹60,000 → **₹3,00,000**

`config.json` scaled 5× with the pool — deadband ₹10k → **₹50k**, max
step ₹25k → **₹1L**, rounding ₹5k → **₹25k**. These restore the 10L-era
values the treasury tests have always pinned; the ₹2L numbers were #84's
clean-sheet config.

**Config alone would have moved nothing**, which is the part worth
remembering: `get_budget` seeds from `equity_desk_capital_rs` ONCE and
thereafter only rotations move the row, and rotations are deadband- and
step-capped by design — the live budget would have crawled from ₹60k over
three sessions. `firm_treasury.rebase_budget()` is the deliberate
pool-scale door (the treasury's `inject_capital`), and **nothing calls it
automatically**: the step cap exists so the router cannot lurch the book
on a noisy signal.

Live now: **equity ₹3,00,000 | options ₹7,00,000 | pool ₹10,00,000**,
rebase logged to the treasury ledger at 16:53 IST.

### ⏭️ Monday

1. The 15:40 archiver run is the one to watch — first *scheduled* nine-way
   sweep. Its log is heartbeat-monitored, so a silent failure surfaces on
   the 20:30 ops card.
2. First real ghost read after the close; equity-option ghosts are
   priceable from today, index ghosts from 08-03.
3. The 19:56 treasury rotation now works off a ₹3L base — expect "hold
   within deadband" unless the router's tilts move it more than ₹50k.

---

## 📍 CURRENT STATE — 2026-08-07 (Friday, evening): the pool is ₹10L and the refused trades now have a ghost book

**Deployed.** Suite **1,987 green** (+24). No gate, cap, strategy or exit
parameter was changed.

### 1. Capital: ₹2,00,000 → ₹10,00,000, executed on the VM

`portfolio_manager.STARTING_CAPITAL` was **already** 10L — the live
`account_state` row was decision #84's ₹2L clean sheet, so this was a
state change, not a code change, and it needed a door with an audit
trail. Live before → after:

| | before | after |
|---|---|---|
| starting_capital | 200,000.00 | **1,000,000.00** |
| realized_pnl | 39,423.99 | 39,423.99 *(untouched)* |
| equity | 239,423.99 | 1,039,423.99 |
| locked_margin | 236,466.30 | 236,466.30 *(11 locks intact)* |
| **available_cash** | **2,957.69** | **802,957.69** |
| peak_equity | 244,215.34 | 1,039,423.99 *(ratcheted)* |
| drawdown_pct | 1.96 | 0.00 |

DB backed up first to `~/brain_map.db.bak-preinjection-20260807`. The
injection is in the append-only `account_events` trail (`2026-08-07
16:41:19`). Run it again with
`python3 -m src.portfolio_manager --inject <rupees> --why "…" --yes`.

**Three consequences the architect should know:**

1. **The halts rebased with the pool.** Daily 3% breaker: ₹6k → **₹30k**.
   Ruin halt 10% trailing off a ₹1,039,424 peak: ₹20k → **~₹104k**. That
   is decision #84's stated aggression relaxing by 5×, automatically.
2. **`firm_treasury.firm_pool` derives from `starting_capital`**, so the
   firm pool now reads ₹10,00,000 — but the **equity desk budget is still
   ₹60,000** and rotation is deadband-₹10k / step-capped-₹25k per day. It
   will take **many sessions** to walk up to the 30% base of the new pool.
   If the architect wants the equity desk funded at the new scale now,
   that is a separate, deliberate treasury decision.
3. Monday's refusals should change character: NIFTY 50 missed the cap by
   ₹166–257/lot, which is a **cap** problem, not a cash problem — those
   will still refuse. The margin-exhaustion refusals will not.

### 2. The ghost portfolio — `src/ghost_tracker.py`

Answers "what would have happened if we took them?" off the proposal
ledger. **On no execution path and a test enforces it**: no margin, no
journal, no `brain_map` row, no capital, no cron line. Deleting it changes
nothing about how the desk trades.

```bash
python3 -m src.ghost_tracker --date 2026-08-10
```

It needed one thing the ledger did not have — the refused **structure**.
`options_proposer` now returns a `rejected` payload (legs, lot_size,
expiry, net premium, max_loss) at the five refusal sites; additive keys
only, and nothing on the trading path reads them. Without them a refusal
is a sentence with no strikes in it.

⚠️ **Where it is blind, and it says so rather than modelling:** EOD
chains are archived for **NIFTY 50 and NIFTY BANK only**. FINNIFTY,
MIDCPNIFTY and all five equity options have **no EOD option chain
anywhere in this system**, so their ghosts report `NO_CHAIN_ARCHIVE` and
stay OUT of the total. Since Friday's refusals were heavily equity-option
and FINNIFTY, **expect the first ghost reports to be mostly unpriceable**.
Widening `chain_archiver.UNDERLYINGS` is the fix, and it is a real
decision: 4 expiries × N underlyings against a rate-limited chain
endpoint on a 1 GB box.

Also: a size-refused trade was sized to **zero** lots, so the ghost is
priced at one lot with `lots_assumed: true`. Reading it as "we'd have
taken one lot" is the reader's call, made in the open.

### ⏭️ Next session

1. First real ghost read after Monday's close — and check how much of it
   is `NO_CHAIN_ARCHIVE` before drawing any conclusion from the total.
2. Decide the equity-desk budget question in (1.2) above.
3. Everything in the two 08-07 blocks below still stands.

---

## 📍 CURRENT STATE — 2026-08-07 (Friday, later): the router actually routes, every refusal is on record, and V1.x is written down

**Deployed `9d4db66`.** Suite **1,963 green** (+25). No gate, cap, strategy
or exit parameter was touched — the freeze holds. `ROADMAP.md` is new.

### 1. The inert router (found earlier today) is FIXED

Two halves were missing, not one:

* **The bars.** `momentum_score` never passed `stock_bars`, which
  `get_relative_strength` requires while the live price path is
  token-gated. Source is now the **local bhavcopy lake** — on disk,
  refreshed by its own cron, no token, no API call, no rate contention
  with the live loop. Cached per symbol per DAY (a 63-session return does
  not move intraday) and primed in ONE pass over the day-files for the
  whole universe (five separate walks ≈ 10s; primed ≈ 2s).
* **The sector.** The five equity underlyings were mapped NOWHERE —
  `INDEX_SECTOR` covers only indices — so `sector_trend` was also being
  handed an empty sector name. `sector_for()` reads them out of
  `config/sector_universe.json`'s constituent lists.

Measured live: **RELIANCE −12.0, HDFCBANK −8.5, TCS −7.7, INFY −6.4,
ICICIBANK +11.3** vs their sectors.

**CORRECTED — the earlier saturation note was measured mid-backfill.**
The VM's bhavcopy backfill finished at 16:07 IST (100 day-files,
2026-03-23 → 2026-08-07). Against the COMPLETE lake the ranking
discriminates properly and only two names saturate:

```
underlying router: HDFCBANK.NS (rank 1.00, rs -1.00) > ICICIBANK.NS (1.00,
+1.00) > INFY.NS (0.77, -0.77) > RELIANCE.NS (0.33, -0.33) > TCS.NS (0.06,
-0.06) > NIFTY 50 (0.00, rs — (no bars)) > … the three other indices
```

The 63-session spreads behind it: ICICIBANK **+11.78**, HDFCBANK −5.38,
INFY −3.86, RELIANCE −1.63, TCS −0.28. So `RS_SATURATION_PCT = 5.0` is
NOT the blunt instrument the mid-backfill reading suggested — it clips
the two genuine outliers and leaves the middle of the field ordered. **No
change to that constant is needed.** Readings taken while the lake was
still filling are not comparable to these; the earlier numbers in this
file are left as written but should not be quoted.

The ranking is only as good as the lake behind it — that part stands.

Indices honestly return **no bars** (an equity bhavcopy does not carry
them) and print `rs — (no bars)` rather than a fabricated `+0.00`. That
distinction is the whole reason the dead router went unseen for two days.

**VM data prerequisite, done today:** the VM's bhavcopy lake held only 5
day-files (the daily cron's `--backfill 5`). A one-off `--backfill 140`
was run to give the 63-session lookback its 64 closes. **If the lake is
ever wiped, the momentum leg silently returns to 0.0** — it fails open by
design, so the `rs — (no bars)` marker in the log is the thing to watch.

### 2. The rejection ledger — `data/proposal_ledger.jsonl`

One row per evaluation, written after the proposer returns. **It decides
nothing** (no branch reads it back) and never raises. An unrecognised
refusal is `REJECTED_OTHER` **with its raw text kept** — a reworded gate
must show up as unmapped, not be absorbed by the nearest bucket. Read it:

```bash
python3 -m src.proposal_ledger --json
```

It starts filling at Monday's 09:10 session. Today's refusals are NOT in
it — the file starts empty, and back-filling it from log text would be
fabricating a record.

### 3. `ROADMAP.md`

V1.1 dynamic sizing (the ledger is its evidence base — ≥2 weeks of rows
before deciding), V1.2 opting in the ALREADY-BUILT ATR trail (nothing to
write; it needs a chosen `atr_mult` and out-of-sample evidence), V1.3
cross-asset. Nothing in it is approved; each needs an explicit unfreeze.

### ⏭️ Next session

1. **Watch Monday's first `underlying router:` line.** Stocks should
   carry real `rs` figures and indices should read `— (no bars)`. Any
   stock reading `— (no bars)` means the VM lake regressed.
2. `python3 -m src.proposal_ledger --date $(date +%F)` after the close —
   the first real read of what the desk wanted and could not have.
3. Everything in the 08-07 (earlier) block below still stands: capital,
   not strategy, is the binding constraint; GOLD_INDIA's contract id is
   still expired; NIFTY MID SELECT still finds no tradeable quotes.

---

## 📍 CURRENT STATE — 2026-08-07 (Friday): two live sessions observed, three ops holes closed, and ONE FINDING THE ARCHITECT MUST SEE

**The freeze held.** Nothing in this session touched a gate, a size, a
strategy or a risk cap. Deployed to the VM: `573def8` + a rebuilt
`data/darling_ids.json` + `setup_cron.sh` reinstalled (**29 cron lines**,
backup at `~/user_crontab.bak-20260807-151420`). Suite **1,938 green**.

### What the market actually did with the new desk (Thu 08-06 / Fri 08-07)

**G3 works — the bear-put monoculture is broken.** Thursday journalled three
spreads: one **iron condor** (NIFTY BANK 56600P/58900C, `ac895ae4`) and two
**bull call spreads** (NIFTY FIN SERVICE `3af8c6ce`, NIFTY BANK `a4303d95`).
Zero bear puts in two days; no butterfly yet. Friday journalled nothing —
and the reason is **capital, not signal**: ₹236,466 locked, ₹2,957.69 liquid.
NIFTY 50 missed the ₹10,000 per-trade cap by ₹166–257/lot on every cycle.

The **physical-settlement gate never engaged**, correctly — the five equity
names were evaluated every cycle and refused downstream on max-loss
(₹12,240–₹22,925/lot), never on settlement, and no stock entry landed inside
7 days of expiry. **NIFTY MID SELECT never trades**: "no tradeable quotes at
the chosen strikes", both days, every cycle.

### ⚠️ THE FINDING: the macro router is INERT, not merely macro-blind

`HANDOVER` 08-05 recorded that the router's macro leg is empty. It is worse
than that, and the new log line is what exposed it. Measured live on the VM:

```
underlying router: NIFTY 50 (rank 0.00, rs +0.00, macro —) > NIFTY BANK
(rank 0.00, rs +0.00, macro —) > … all nine at rank 0.00
```

**Both legs read zero.** The macro leg is absent as documented. The momentum
leg is zero for a different reason: `momentum_score` calls
`sector_trend.get_relative_strength(underlying, sector)` **without
`stock_bars`**, and that function's own contract is "`stock_bars` MUST be
supplied while the live price path is token-gated" — so it returns an error
dict, `rs_spread_pct` is None, and `momentum_score` fail-opens to 0.0 for
every name. Ranking is therefore uniformly flat and `prioritise` is a stable
sort over equal keys: **the universe is never reordered.** The router has
been shipping as a no-op since 08-05. Nothing is broken or unsafe — it
fails open exactly as designed — but the 3-D desk's routing leg is not
actually running, and wiring bars into it is a Dept-2 decision, not a
hygiene fix. **Left for the architect.**

### What was fixed (hygiene only)

1. **Seven unquotable names, five of them OPEN POSITIONS.** The id map is
   built on the Mac off the tier table; the tier table is a screen and it
   churns, so a held name dropped from the screen was deleted from the map
   on the next weekly rebuild while its position stayed open. That is an
   unmarked holding, not log spam. The id universe is now screen ∪ held ∪
   previously-resolved (carry-forward re-verifies every symbol against each
   run's master, so a delisted name still falls out into `unresolved`).
   **105 → 120 ids, 0 unresolved**; every open position on the VM now
   prices. BHARTIARTL 10604, RELIANCE 2885, TMCV 759782, BAJAJ-AUTO 16669,
   JSWSTEEL 11723, GRAVITA 20534, VOLTAMP 13577 (all NSE_EQ / EQ).
2. **The router logs its ranking** once per open cycle (`render_line`,
   which existed, was tested, and was called by nothing). Own try/except:
   a logging fault must never print as a routing failure.
3. **`cross_asset` is on cron** — daily 19:40 IST. Built 08-05, never
   scheduled. Dry-run from the VM: **CRUDE returns 3 bars** (last 08-06),
   so the earlier CA-404 was a stale window, not a dead feed. GOLD_INDIA
   reports **CA-410 — contract expired 2026-08-05**; rolling that id is a
   deliberate config change, not done here.

### ⏭️ Next session

1. **Decide the router** (above) — it is inert until `momentum_score` is
   given bars, and that is a freeze-breaking change by definition.
2. **Capital, not strategy, is the binding constraint on entries.** The
   architect's V1.1 dynamic sizing engine is aimed at exactly the refusals
   Friday's log is full of; Friday is the evidence for it.
3. Roll GOLD_INDIA's contract id in `config/macro_securities.json`, then
   consider adding `cross_asset.log` to `ops_monitor.EXPECTED_JOBS` — it is
   deliberately absent now because that heartbeat feeds `health_gate`, and a
   capture-only tap must not be able to block the discovery miner.
4. NIFTY MID SELECT's strike selection finds no tradeable quotes at all —
   worth one look before it is assumed to be a liquidity fact.
5. The market_loop change reaches production at Monday's 09:10
   `master_scheduler`; Friday's process is still running the old code.

