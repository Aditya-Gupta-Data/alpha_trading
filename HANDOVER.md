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

