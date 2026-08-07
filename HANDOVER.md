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

⚠️ **Most of them exceed the ±5% saturation band**, so they tie at rank
1.00 and sort ahead of the four bar-less indices *without discriminating
among themselves*. `RS_SATURATION_PCT` is a strategy parameter and was
left alone. If the architect wants the router to rank stocks against each
other, that constant is the lever — a freeze-breaking change.

Verified on the VM after the backfill (4.1s, and it **does reorder** —
RELIANCE fell below TCS, which the flat ranking could never do):

```
underlying router: HDFCBANK.NS (rank 1.00, rs -1.00) > ICICIBANK.NS (1.00,
+1.00) > INFY.NS (1.00, -1.00) > TCS.NS (1.00, -1.00) > RELIANCE.NS (0.81,
-0.81) > NIFTY 50 (0.00, rs — (no bars)) > … the three other indices
```

The VM and Mac readings differ slightly for RELIANCE (−0.81 vs saturated)
because the two lakes are different depths, so the 63-session window
starts on a different date. Expected, not a defect — but it does mean the
ranking is only as reproducible as the lake behind it.

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

---

## 📍 CURRENT STATE — 2026-08-05 (final): V1 OPTIONS DESK COMPLETE — Sunday code freeze is in effect

**The last block of a very long day.** After the morning's infra work (below),
five more commits landed and are LIVE on the VM (`89d3dad`):

| Commit | What |
|---|---|
| `44e64cc` | Sequence 1 reporting honesty (Wilson bounds, drawdown, blocked-count, position age, performance countdown) |
| `f6f7f38` | Bug-ledger aging — 74 rows triaged, ALL traced to already-fixed causes; 72 retired, 2 active |
| `edd1290` | **G3 unblocked** — range-before-direction `market_view`, graded `classify_trend` wired, iron butterfly routed (90-session replay: 77/12/1 → 40/24/26 bearish/neutral/bullish) |
| `a22bef8` | Equity options behind a **physical-settlement gate** (7d entry floor + 7d forced exit for stocks; indices keep cash-settled rules) + the test-isolation leak fixed at root |
| `89d3dad` | **The 3-D desk**: universe 2→9 (FINNIFTY id 27 / MIDCPNIFTY id 442 verified), macro-trend `underlying_router` (reorders, never filters), time horizons (`horizon_for` + LEAPS-capable `pick_expiry`), **lot sizes verified against the scrip master — HDFCBANK 550→650 and TCS 175→225 were WRONG and are now corrected + test-pinned** |

Suite **1,931 green**. Token renewed by the owner and live-verified (VIX 12.06).

### ⛔ THE SUNDAY V1 CODE FREEZE (owner directive, this session's close)

**No new execution features.** Thursday/Friday are observation sessions:
watch the new equity-option and multi-index routing breathe in a live market.
Hotfix only on real breakage — the 07-20 freeze doctrine applies.

### Facts the next person must not re-learn

1. **FINNIFTY/MIDCPNIFTY are MONTHLY-only.** NIFTY is the only NSE index still
   carrying weeklies. The expansion bought breadth, not expiry frequency.
2. **LEAPS (90–200d) fully satisfiable only on NIFTY** (chain reaches 2031);
   BANKNIFTY ~11 months; everything else ~3 months by exchange listing.
3. **The G3 routing change is live and NOT shadow-fired** (#63 flag raised
   twice, owner deployed). The first sessions under it are the ones to watch.
4. `market_view` falls back byte-identically for any analysis dict without the
   SMA distances — the 19 journalled trades are untouched.
5. NIFTY MID SELECT's router momentum leg honestly reads 0 until a midcap
   series is added to `config/sector_universe.json` (owner watchlist item).
6. The router's macro leg is empty until news rows carry
   `long_term_macro_score` for these exact tickers; it degrades to pure
   momentum ordering today.

### ⏭️ Next session (Thursday, observation mode)

1. Watch 09:10 `master_scheduler` — first live session with 9 underlyings,
   the router, and the new `market_view`. Expect condor/butterfly proposals
   if VIX sits in band and the flat read holds.
2. Watch the first equity-option proposal: settlement gate reasons should
   appear in refusals, never a stock entry inside 7d of expiry.
3. `bash scripts/daily_health_and_queue.sh` as usual; the ops card now
   carries the staleness scan.
4. Midcap sector-universe addition is APPROVED work (analysis-side, not an
   execution feature — freeze-compatible).

---

## 📍 CURRENT STATE — 2026-08-05 (night, later): edge_miner's SSH transport is hardened; the Mac's brain_map.db is FRESH again

**Deployed to the VM this session** (`55e7a17`, `git pull` + `setup_cron.sh`,
27 cron lines, 3 services active) and the Mac cron line for
`fetch_sector_bars.py` is **installed**. Suite **1,783 green** (+10).

### The diagnosis — one problem wearing three faces

The pull died on three consecutive nights, and the three log lines look like
three different bugs. They are one: a fragile SSH hop from a home connection
to a `us-central1` box.

```
08-02  client_loop: send disconnect: Broken pipe        (stalled mid-copy)
08-02  subprocess.TimeoutExpired after 120 seconds      (escaped as a raw
                                                         TRACEBACK, not a result)
08-03  kex_exchange_identification: read: Operation timed out
       banner exchange: Connection to 35.239.254.99 port 22: Operation timed out
```

**This was never bandwidth.** `brain_map.db` is 3.6 MB and a healthy pull
measures ~10s (timed). It is handshake fragility, and any one transient event
killed the entire nightly cycle because there was no retry, no keep-alive, and
one uncaught exception path.

### The fix

| Defect | Fix |
|---|---|
| No keep-alive → stalls became `Broken pipe` | `SSH_TUNING`: `ServerAliveInterval=60`, `ServerAliveCountMax=3`, `ConnectTimeout=30`, `ConnectionAttempts=3` |
| No retry → one bad handshake killed the run | `run_resilient()` — 3 attempts, exponential backoff (5s, 10s), each failure NAMED on stdout |
| `TimeoutExpired` escaped as a traceback | Caught and converted to an ordinary failed result. **Transient BY CONSTRUCTION, not by string match** — its message carries none of the network vocabulary the classifier looks for, which is exactly how it would have slipped through a naive fix |
| 120s per attempt expired on 08-02 | Raised to 180s |
| gcloud interpreter unpinned | `_run` now uses `config.gcloud_env()` — the standing rule from the ship fix |

**A live finding worth writing down: `gcloud compute scp` does NOT accept the
`-- -o …` passthrough that `gcloud compute ssh` does.** It reads the flags as
extra source paths and errors out. Verified before building on it. Each tool
has its own flag (`--scp-flag=` / `--ssh-flag=`) and they are not
interchangeable — a plausible-looking single-flag fix would have silently
disabled the keep-alives on the very hop that was failing.

**Retry safety is stated at each of the four call sites.** The two pulls are
read-only; the `/tmp` push overwrites; the remote apply replays
`graph_engine.add_edge`, which is documented idempotent (reinforce, never
duplicate). **Retrying a remote WRITE is only acceptable because of that
property** — do not extend `run_resilient` to a writer that lacks it.

### Also closed: a silent-failure sibling in the same file

Step 5 (refresh the Mac's `data/` copies) **discarded its return value
entirely.** `brain_map.db` could stay stale while the run reported
`"status": "ok"` — the same disease as the ship bug and `live_quote`. Now
checked, named on failure, and surfaced as `local_copies_refreshed` in the
summary. It is still non-fatal (the mining already landed on the VM), but it
can no longer be invisible.

### Verified by a real forced run, not a mock

```
{"status": "ok", "outcomes_considered": 20, "triples_written_locally": 55,
 "new_edges_applied_to_vm": 24, "local_copies_refreshed": true}
```

| | before | after |
|---|---|---|
| `data/brain_map.db` mtime | **2026-07-30 21:02** | **2026-08-05 15:46** |
| `graph_edges` | 89 | **115** |
| `daily_context` | 20 (max 07-30) | **26 (max 08-04)** — matches the VM |
| `outcomes` | 384 | 385 |

**24 new causal edges were applied to the VM** — a real production write, via
the designed idempotent path. Ollama started on demand and stopped cleanly
(`pgrep -lf ollama` empty afterwards), so the 08-04 standing constraint holds.

**The two fixes verified each other.** An hour earlier the new Dept-5 stall
detector had correctly called the Mac's copy `NOT ACCRUING, newest frame
2026-07-30`. After this sync the same detector reads
`26/60 frames — accruing 1/day, ~34 more nights (≈2026-09-07)`. The detector
found a real stall, and the transport fix cleared it.

### Files changed

| File | Change |
|---|---|
| `src/edge_miner.py` | `SSH_TUNING`/`SCP_FLAGS`/`SSH_FLAGS`, `_transient()`, `run_resilient()`, all four transfers wrapped, step-5 return value checked, `gcloud_env()` |
| `tests/test_edge_miner.py` | +10 transport tests (clock injected — nothing sleeps) |
| `MODULES.md` | `edge_miner` + its test row |
| Mac crontab | `fetch_sector_bars.py` at 19:10 Mon-Fri **installed** |
| VM | pulled to `55e7a17`; 24 new `graph_edges` |

### ⏭️ Next immediate steps

1. **Watch tonight's 21:00 LaunchAgent run** — the first unattended exercise
   of the retry path. Today's proof was a forced run.
2. **The 19:10 sector-bars cron fires tonight for the first time.** Check
   `logs/sector_bars.log`.
3. Queue is otherwise as reported: reporting gaps (SYSTEM_XRAY §9 fixes 1–5)
   is the recommended next item; then the 74-item bug ledger; then the orphan
   decisions (RSS on/off, `report_downloader`'s dead crawl, corporate-events →
   `equity_entry_checks` halt).

---

## 📍 CURRENT STATE — 2026-08-05 (night): the pattern-miner is NOT broken. It is waiting, and now it says so out loud.

**Queue item #2. The headline is a negative finding, and it is the honest one:
there is no cron failure, no silent crash, no path/environment fault and no
swallowed exit code.** Suite **1,773 green** (+8). Nothing was "repaired",
because nothing was broken.

### What the diagnosis actually found

| Hypothesis in the brief | Verdict |
|---|---|
| Silent crash | **No.** `logs/discovery_nightly.log` holds 16 clean SKIPPED records; the job exits 0 and prints its reason every night. |
| Path/environment issue (like the gcloud bug) | **No.** Cron line #18 is installed and firing; `.discovery_nightly_state.json` updates every night (`last_skip: 2026-08-04T20:20:01`). |
| Bad exit code being swallowed | **No.** A skip is *designed* to be exit 0 — cron must stay quiet — and the every-7th Discord note is the escalation path. |

**The real state:** the health gate passes (`silent_jobs: []`,
`ingestion_problems: 0`) and the **depth gate correctly refuses** at 25/60
`daily_context` frames. It is doing precisely what decision #76 built it to do.

### Frame starvation — backfill is EXHAUSTED, and that is a fact about the lake

Read-only survey of the VM before touching anything:

```
FRAMES        25   2026-07-11 -> 2026-08-04   contiguous, ZERO gaps
macro_daily   25   2026-07-11 -> 2026-08-04
deals_census  25   2026-07-11 -> 2026-08-04
news_daily    17   2026-07-16 -> 2026-08-04
flows         17   2026-07-10 -> 2026-08-03
chains/*      17   2026-07-13 -> 2026-08-04
UNION of lake days: 26 (2026-07-10 -> 2026-08-04)
lake days with NO frame: 1  ['2026-07-10']
```

`fold_lake` was run on the VM (idempotent upsert; `daily_context` is a derived
refreshable table, not an append-only ledger). **Result: 25 → 26 frames.**
That is the entire available backfill.

**Frames cannot predate the data they are built from.** The Phase-0 lake begins
2026-07-10. Anything beyond that would be fabrication (RULE 3), so it was not
done and must not be done later.

Frame **quality** is good, checked field by field: trading days carry 16/16
populated columns, weekends 12/16 (no VIX/chains/flows — market shut, correctly
NULL). The collection mechanism is completely unblocked — sleep-phase Task G
records one frame per calendar day with no gaps.

**60 frames arrives organically at the observed 1.0/day: ~34 more nights,
≈2026-09-07.** No engineering shortens that. Only lowering `MIN_CONTEXT_FRAMES`
would, and that is an owner ruling against decision #76's panel rule, not a
change an agent makes on its own initiative.

### So what WAS fixed — the thing that actually deserved fixing

**The starvation was unmeasurable from outside, and a dead corpus was
indistinguishable from a healthy countdown.** Both printed
`daily_context 25/60 frames`. If sleep-phase Task G ever died, frames would
freeze and the skip line would not change by one character. That is the same
silent-failure disease as the last two sessions, one layer up.

`depth_gate` now MEASURES instead of only counting:

| New field | Meaning |
|---|---|
| `first_frame` / `last_frame` | the real span in the table |
| `accrual_per_day` | **observed** from that span — never assumed |
| `days_to_go` / `projected_ready` | when the floor is genuinely reached |
| `accruing` | False when the newest frame is older than `STALE_FRAME_DAYS` (3) — frames have STOPPED arriving |

Two deliberately different sentences, because a corpse and a countdown must
never read alike:

```
growing:  daily_context 26/60 frames — accruing 1/day, ~34 more nights (≈2026-09-07)
stalled:  daily_context 20/60 frames — ⚠️ NOT ACCRUING, newest frame 2026-07-30
                                        (sleep-phase Task G is not recording)
```

The every-7th Discord note escalates the same way: 🔴 *"the context corpus has
STOPPED GROWING … check Task G, not the miner"* versus ⏳ *"this is a countdown,
not a fault."*

**It proved itself on first run.** Executed against the Mac's own brain_map
copy, it correctly reported `accruing: false, last_frame 2026-07-30` — because
that copy really has stopped updating (`edge_miner`'s VM pull is broken on SSH
timeouts, logged 08-02/08-03). The detector's first live catch was a real
stall, not a synthetic one.

### Files changed

| File | Change |
|---|---|
| `src/discovery/nightly.py` | `depth_gate` measures accrual/ETA/staleness; new `depth_reason()`; the every-7th note distinguishes incident from countdown; `STALE_FRAME_DAYS = 3` |
| `tests/test_discovery_nightly.py` | 8 new tests (countdown vs corpse, the staleness boundary, slower-accrual ETA, empty table, single frame, the Discord wording split); 1 existing test relaxed from an exact-dict assert |
| `MODULES.md` | `nightly.py` row updated |
| VM `data/brain_map.db` | `fold_lake` → `daily_context` 25 → 26 rows |

### ⏭️ Next immediate steps

1. **Deploy** — `git pull` on the VM. Tonight's 20:20 pass then prints the
   countdown line instead of the bare fraction. (Still unpushed; see below.)
2. **Nothing else to do on Department 5 until ~2026-09-07.** The correct
   action is to leave it alone. If the owner wants patterns sooner, the only
   lever is `MIN_CONTEXT_FRAMES`, and lowering it means mining on a corpus too
   thin to clear the support floors — every run finds nothing and the surface
   trains you to ignore it. That is the trade #76 already weighed.
3. **Watch for `NOT ACCRUING`.** If it ever appears on the VM, the fault is
   sleep-phase Task G, not the miner.

---

## 📍 CURRENT STATE — 2026-08-05 (late): both loose threads CLOSED — the Mac→VM ship is alive, the sector veto RE-ARMED ITSELF

**No trading logic changed. No strategy, sizing, treasury or ledger touched.**
Suite **1,765 green** (was 1,729; +36). Two root causes found, both reproduced
deterministically before anything was written.

### 1. The Mac→VM ship — it did not "die 15 days ago". It NEVER ran.

The ship is `firm_treasury.vm_push_file`, called at the end of
`patience_basket.eod_chain`. `logs/patience_eod.log` shows
**`artifacts_shipped: []` on every single run from 2026-07-22 onward** — the
day after the feature landed (#83). There is no successful ship in the log,
ever. It shipped broken and stayed broken.

**Root cause, reproduced under a cron-like environment:**

```
ERROR: gcloud failed to load. You are running gcloud with Python 3.9,
which is no longer supported by gcloud. ... set the CLOUDSDK_PYTHON
environment variable to point to it.
```

`gcloud` is a `/bin/sh` wrapper that then goes looking for a Python **on
PATH**. Under cron's minimal PATH it finds macOS's own `/usr/bin/python3` —
**3.9.6**, a version gcloud dropped. Interactively it works, because
Homebrew/Framework pythons sit earlier on PATH. So it failed **only
unattended**, which is precisely why nobody saw it.

`config.py` already carried the comment *"Absolute path because the 19:15
cron's PATH is minimal"* — the team pinned **gcloud's own path** and stopped
exactly one layer short of pinning **the interpreter gcloud itself picks up.**

**Why it was invisible for 15 days** — a second, independent defect. The old
body was `subprocess.run(cmd, capture_output=True, timeout=90)` plus a bare
`except Exception: return False`. gcloud stated the cause in plain English
every night and the function **captured it and threw it away.** Same disease
as `live_quote` before 08-04.

**Fixed:**

| Fix | Where |
|---|---|
| `CLOUDSDK_PYTHON` pinned to `sys.executable` for every gcloud subprocess `src/` starts; an explicitly-set value is respected, never overwritten | `config.gcloud_env()` (new, beside `GCLOUD_PATH`) |
| The push NAMES its failure (`vm ship FAILED [file] rc=N: <gcloud's own stderr>`); still fails open, never raises; timeout 90s → 120s (the edge miner has recorded real 120s SSH stalls to this VM) | `firm_treasury.vm_push_file` |
| `artifacts_not_shipped` + a printed `VM SHIP INCOMPLETE — n/5 delivered; missing: …` | `patience_basket.eod_chain` |

**Verified end to end under `env -i` with cron's PATH and no TTY: 5/5
delivered.** On the VM afterwards:

```
darling_tiers.json      2026-08-05 15:00   as_of 2026-08-04T19:15:26
darlings_levels.json    2026-08-05 15:00   as_of 2026-08-04T19:15:22
darling_ids.json        2026-08-05 15:00
fo_liquidity.json       2026-08-05 15:00   as_of 2026-08-04     ← FIRST TIME EVER
sector_index_bars.json  2026-08-05 15:01                        ← FIRST TIME EVER
```

**`equity_desk`'s tier gate now reads `True` on the VM.** The desk has been
refusing every new equity entry since ~07-24 against a 2026-07-20 tier table;
it can trade again. Its own guard was working perfectly the whole time —
nothing reported it.

### The manifest grew 3 → 5 (`patience_basket.SHIP_MANIFEST`)

- **`fo_liquidity.json`** — `equity_entry_checks.liquidity_filter` is
  FAIL-CLOSED without it and it was **ABSENT on the VM entirely**, never
  shipped once since the filter was wired on 07-20.
- **`sector_index_bars.json`** — the new producer's output.

### 2. `data/sector_index_bars.json` now has a producer — and the veto re-armed itself

`scripts/fetch_sector_bars.py` (**MAC-ONLY**, never the VM: yfinance is a
Mac-lane dep absent from `requirements.txt`, Yahoo blocks datacentre IPs, and
`src/` must stay yfinance-free — the `fetch_pre2019_sectors.py` precedent).

- Writes the **exact** shape `sector_trend` already reads. Close at index 3
  and date at index 0 are load-bearing; an end-to-end test drives the real
  `is_sector_bullish` so a tuple-order drift screams.
- Indices and labels come from `config/sector_universe.json`, never a
  hardcoded list; a shared index (BATTERY_EV/AUTO → `^CNXAUTO`) is fetched once.
- **MERGE, not overwrite** (the `index_history` doctrine): union by date,
  **stored wins on overlap**, atomic tmp+rename. A bad fetch can neither
  shorten nor rewrite the 4,600-bar history.
- **NULL-honest:** any missing/NaN/inf OHLC ⇒ the bar is DROPPED, never
  zero-filled, never forward-filled.
- Per-index fail-open, SB-404/SB-500 → `logs/sector_bars.jsonl`, and **a run
  that refreshed nothing exits 1** (the `scrip_master` doctrine).

**Live result: 7/7 indices refreshed to 2026-08-05**, and the payoff of last
session's design landed with zero code change:

```
before:  sector veto SELF-DISABLED — data stale: … 19.7 days old
after :  FINANCIALS sector trend ok
```

### ⚠️ Honest limitation of the data source

**Yahoo's NSE sector coverage is genuinely sparse.** Over the same month
`^CNXIT` returned 23 sessions but `^CNXAUTO` only 11 — and `^CNXFMCG`,
`^CNXMETAL`, `^CNXENERGY` behave like AUTO (+5 sessions vs +14). The producer
is doing the right thing by dropping incomplete bars; the holes are upstream.

**This does not affect the live veto today:** `regime_filters.INDEX_SECTOR`
maps only `NIFTY BANK → FINANCIALS` (`NIFTY 50 → None`), and **`^NSEBANK` is
gap-free** — all 15 trading days from 07-16 to 08-05 present. If a future
sector is ever wired into a decision, re-check its density first.

### ⛏️ Owner action required — the new cron line (Mac cron install is TCC-blocked for Claude)

```bash
( crontab -l 2>/dev/null | grep -v 'fetch_sector_bars'; echo '10 19 * * 1-5 cd /Users/adityagupta/Documents/Claude/alpha_trading && /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 scripts/fetch_sector_bars.py >> logs/sector_bars.log 2>&1' ) | crontab -
```

19:10, five minutes ahead of the 19:15 EOD chain, so the chain ships a
same-day file. `CRON_SETUP.md` carries the row and the full re-install line.

### 🚩 The standing rule this session created

**Any new code that shells out to `gcloud` must use `config.gcloud_env()`.**
A bare `subprocess.run([GCLOUD_PATH, ...])` from cron is broken and will not
say so. Written into `CRON_SETUP.md` beside the Mac crontab.

### Files changed

| File | Change |
|---|---|
| `src/config.py` | **NEW** `gcloud_env()` — pins `CLOUDSDK_PYTHON` |
| `src/firm_treasury.py` | `vm_push_file` uses it, names failures, 120s timeout, `run_fn`/`env_fn` seams |
| `src/analysis/patience_basket.py` | `SHIP_MANIFEST` constant (3→5), `artifacts_not_shipped`, loud incomplete-ship line |
| `scripts/fetch_sector_bars.py` | **NEW** — the producer |
| `tests/test_vm_ship.py` | **NEW** — 14 tests (the ship had ZERO) |
| `tests/test_sector_bars_producer.py` | **NEW** — 22 tests |
| `CRON_SETUP.md` | the 19:10 row, the re-install line, the gcloud-env rule |
| `MODULES.md` | rows for the new script + both test files; `firm_treasury` and `patience_basket` rows updated |

### ⏭️ Next immediate steps

1. **Paste the cron line above.** Until then the sector file only refreshes
   when someone runs the script by hand, and the guard will disarm the veto
   again after 3 days.
2. **Deploy to the VM** — `git pull` on `main`. The VM does not yet have
   `staleness_guard`, so its 20:30 ops card is not scanning freshness yet.
   The ship fix is Mac-side and is live from tonight's 19:15 run regardless.
3. **Watch tonight's 19:15 chain** for `artifacts_shipped` with five entries
   (or a loud `VM SHIP INCOMPLETE`). This is the first unattended exercise of
   the fix; today's proof was a hand-run cron simulation.
4. **Separate, untouched:** `edge_miner`'s VM pull is failing on real
   **SSH/network timeouts** (`kex_exchange_identification: read: Operation
   timed out`, `Broken pipe`) on 08-02 and 08-03 — a different fault from
   this one, and out of scope here. It is why the Mac's `brain_map.db` copy
   is stale, and it deserves its own look.
5. `data/darling_ids.json` still carries **no `as_of`**; the guard falls back
   to mtime for it.

---

## 📍 CURRENT STATE — 2026-08-05: the STALENESS GUARD is live; the sector veto is DISABLED on purpose; and it immediately found a SECOND stale-data incident

**No trading logic changed. No strategy added. No sizing, treasury or ledger
touched.** One new module, one veto disarmed, one report section added.
Suite **1,729 green** (was 1,700; +29 tests).

### Why this session happened

An X-ray of the whole data flow (`SYSTEM_XRAY.md`, written the same day) found
that **`data/sector_index_bars.json` was 20 days stale and still feeding a LIVE
bullish veto** — `analysis/sector_trend.is_sector_bullish` →
`analysis/regime_filters._sector_bearish` → `advise()` →
`options_proposer.build_proposal`. Nothing crashed, nothing logged, and every
nightly ops card said ✅. A 50/200 SMA verdict computed on three-week-old bars
is wrong QUIETLY, which is the worst failure mode this system has.

### The immediate fix — the veto is OFF, deliberately, and it will re-arm itself

"Fix the cron that refreshes it" was **not available: there is no such cron and
never was.** Verified by grep over `src/`, `scripts/`, `archive/` and
`research_archive/` — **`data/sector_index_bars.json` has no producer anywhere
in the repository.** It was written once (yfinance, 2026-07-16) and has never
been refreshed. `scripts/fetch_pre2019_sectors.py` writes NSE-format CSVs into
`drop/` for `index_history`; it does not write this file.

So the veto self-disables through the guard rather than through a hardcoded
switch — **the moment a producer exists and the file goes fresh, the veto
re-arms with no code change.** Today `advise()` returns:

```
block_bullish: False
bullish_reason: smart-money/sector veto: no distribution;
                sector veto SELF-DISABLED — data stale:
                data/sector_index_bars.json is 19.7 days old
                (limit 3.0 days = 3× its 24h cadence);
                producer: NO PRODUCER on any schedule
```

**The smart-money half of the veto is untouched and still votes.** Only the
sector leg is off. On the VM the file is ABSENT entirely, so the outcome there
is identical (missing ⇒ stale) — the fix behaves the same on both boxes.

### The new module — `src/staleness_guard.py`

It does **not** invent a doctrine. The house already had one, correct, in four
places (`equity_desk.TIERS_MAX_AGE_DAYS=3`, `IDS_MAX_AGE_DAYS=14`,
`equity_entry_checks.LIQUIDITY_MAX_AGE_DAYS=7` fail-closed,
`market_snapshot.read(max_age_seconds=)`). What was missing was a shared
implementation and a REGISTRY, so that an artifact with **no** freshness check
is visible as an omission instead of invisible.

- **Stale ⇔ `age > tolerance × refresh_interval_hours`.** Tolerance forgives
  missed runs, so weekends/holidays/one flaky night never cry wolf.
- **Signal:** a caller-supplied content `as_of` when available (the more honest
  one, and what the four existing checks use), else file mtime (O(1), no read).
- **Two policies, and the DIRECTION is the whole point.** `IGNORE` = the
  dependent component drops its opinion — used ONLY by `sector_index_bars`,
  because a risk-reducing advisory's absence returns the system to its
  documented baseline. `MONITOR` = alert only, never override — used wherever
  the consumer already has its own correct check, because **self-disabling a
  fail-CLOSED risk gate would make it fail OPEN, i.e. riskier.** There is
  deliberately no policy that bypasses a fail-closed gate on staleness; if one
  is ever wanted it is an owner ruling, not a flag. An anti-drift test locks
  the `IGNORE` list to exactly one entry.
- **The guard itself FAILS SAFE**, unlike almost everything else here: missing
  file, unregistered name or a broken clock all yield `stale`. Precedent:
  `sleep_phase._targets_the_real_brain_map` — a muzzle that fails open is not
  a muzzle. Worst case the sector veto switches off, which is exactly where any
  exception in `_sector_bearish` already landed.
- **`producer=None` is a FINDING, not a blank.** A test refuses a registry row
  with no producer unless it carries a note saying so.

### The report — the ops card now screams

`ops_monitor.run_sweep` scans the registry nightly and appends a 🚨 block naming
every stale artifact and every component the guard switched off. A clean scan
adds **nothing** — the card stays byte-identical to its pre-guard form. A stale
FILE is never folded into the problem-LINE count: a log that says "failed" and
a file that quietly froze are different diseases.

### 🔴 WHAT THE GUARD FOUND ON DAY ONE — a second, unreported incident

Running the registry against the live VM:

| Artifact (VM) | Age | Consequence |
|---|---|---|
| `data/darling_tiers.json` | **`as_of` 2026-07-20 — 16 days** | `equity_desk` (`TIERS_MAX_AGE_DAYS=3`) has been refusing **all NEW equity entries since ~07-24**, silently |
| `data/darlings_levels.json` | `as_of` 2026-07-20 | same vintage |
| `data/darling_ids.json` | mtime 07-21, `as_of` **None** | ids file carries no build stamp |
| `data/fo_liquidity.json` | **ABSENT on the VM** | `liquidity_filter` fail-closed (correct, but total) |
| `data/sector_index_bars.json` | **ABSENT on the VM** | the veto this session disarmed |

**The Mac → VM artifact ship has been dead for ~15 days.** The Mac's own copies
are current (`darling_tiers.json` 08-04) — they are simply not arriving. That
is why the 3 equity-desk positions opened 2026-07-22 are still the last equity
entries the desk ever made: `equity_desk`'s own guard was working perfectly and
**nothing reported it**, which is the identical disease this session was called
in to fix.

**NOT fixed here — out of scope, and it is infra, not the sector veto.** It is
the single most valuable next action available.

### Files changed

| File | Change |
|---|---|
| `src/staleness_guard.py` | **NEW** — the guard + registry + alert payload + CLI |
| `src/analysis/regime_filters.py` | `_sector_bearish(underlying, staleness_fn=)` consults the guard before letting `sector_trend` vote; docstring states the sector leg is off |
| `src/ops_monitor.py` | `build_card(..., stale=)` additive section; `run_sweep(..., staleness_root=)` injectable seam; summary carries `stale_artifacts` / `disabled_components` / `stale_names` |
| `tests/test_staleness_guard.py` | **NEW** — 21 tests |
| `tests/test_regime_filters.py` | 5 new staleness tests; the 3 existing sector tests now inject a fresh verdict (their contract changed deliberately) |
| `tests/test_ops_monitor.py` | 3 new tests; the 4 `run_sweep` call sites now pass `staleness_root=tmp` |
| `MODULES.md` | rows for the new module + both test files; `regime_filters` row updated |
| `SYSTEM_XRAY.md`, `PROP_ROADMAP.md` | **NEW** (earlier the same day) — the audit this fix came out of |

### A regression this session deliberately avoided

Wiring the scan into `run_sweep` made `ops_monitor`'s tests read the mtimes of
real files in `data/` from inside pytest — **the fifth instance of the defect
family HANDOVER has now recorded four times** (07-22, 07-23, 07-27, 08-04: a
new default that reaches live state). Caught before it landed; the fix is the
`staleness_root=` seam, and all four test call sites pass a tempdir.

### ⚠️ Expected noise, so it is not a surprise

Until a producer exists, the VM's nightly ops card will carry the 🚨 STALE DATA
block **every night**. That is the correct cost of running with a radar down —
it disappears the day the artifact is fixed or the registry row is removed with
a reason. Do not silence it by loosening the tolerance.

### ⏭️ Next immediate steps

1. **Fix the Mac → VM artifact ship** (tiers/levels/ids/fo_liquidity, dead ~15
   days). The equity desk cannot take a new entry until it lands.
2. **Decide `sector_index_bars.json`'s fate** — either give it a producer (a
   Mac-side yfinance refresher, since yfinance is a Mac-only dep and NSE
   bot-blocks the VM) and the veto re-arms itself, or delete the artifact and
   the sector leg of the veto together. Leaving it as-is is also a valid
   choice, now that it is loud instead of silent.
3. `data/darling_ids.json` carries **no `as_of`** — the guard falls back to
   mtime for it. Adding a build stamp would make its check content-honest like
   the others.

---

## 📍 CURRENT STATE — 2026-08-04 (VM lane): equity desk is now PRICE-CAPTURED; bhavcopy migrated off the Mac

**Two sessions ran today in parallel.** This block is the VM/ingestion lane
(`d7d888f`, `ca6558f`). The Mac-lane Ollama block below (`dcf604d`) is a
DIFFERENT session's work — both are real, neither supersedes the other.

**Trigger:** the 15:45 EOD card printed `(3 unmarked)` with `—` for LAST and
P&L on all three equity positions. Reported as a broken price fetcher. **It
was not.** A step-by-step probe on the VM passed every gate
(`security_id_for(FINEORG)='3744'`, `get_live_price_by_id → 4929.5`) and the
card re-rendered fully populated 12 minutes later. The failure was real but
TRANSIENT — and undiagnosable, because `live_quote` swallowed every exception.

### What changed

1. **`live_quote` names its failures** (`no_security_id` / provider-returned-
   nothing / `<ExcType>: msg`) on stderr, still failing open. **The next
   occurrence will say WHY** — the 15:45 cause remains an open unknown and
   this is how we catch it.
2. **The 15-min sweep now covers the equity desk's open funded book.**
   `intraday_tracker.capture()` sweeps TWO universes through different doors:
   the watchlist by symbol, the desk by scrip id (desk names are NOT in
   `watchlist.yaml`). Desk rows carry `src="dhan_live_15m_desk"`. Exit logic
   finally has a durable price series instead of an on-demand fetch.
3. **NEW daily all-darlings tap** — cron #26, Mon-Fri 15:50 IST →
   `data/lake/darlings_daily.jsonl`. One close for ALL ~105 darlings
   **including the ones we do not hold**, so ENTRY zones are visible.
   Verified live on the VM: `captured: 105, failed: 0`.
4. **Bhavcopy MIGRATED to the VM** — cron #27, daily 19:15,
   `--backfill 5` → `data/lake/bhavcopy/`. See below.
5. **`firm_treasury` moved 19:50 → 19:56.** It was the ONLY pair on the box
   sharing an exact minute (with `macro_nightly`), and the hazard was MEMORY,
   not API: `macro_nightly` is the documented OOM risk on the 1 GB e2-micro.
   **All 28 VM jobs now own their own minute.**
6. **`equity_desk_snapshot.json` PARKED** (`.parked`, both machines) — it was
   decision #82's Mac→VM transport, superseded hours later by #83; its
   reader/writer functions are already gone from `src/`. NOT deleted. Delete
   only after a clean observation period.

### Bhavcopy: why it moved, and what was verified first

The Mac captured only **7 of 11 recent weekdays**, and on the missed days
`patience_eod.log` had **no entry at all** — the job never ran and never
failed. That is macOS cron not firing while the machine sleeps, not a
bhavcopy bug. Before wiring it, the real risk was tested: **NSE bot-blocks
scripted access, so a GCP IP could have been refused.** It is not — the first
VM run returned `captured: 2` and **recovered 2026-07-31, a day the Mac
missed**. `--backfill 5` is deliberate: `fetch_day` is idempotent, so each
run self-heals up to five days of holes.

**The Mac still fetches its own copy** — `patience_basket --eod` calls
`fetch_day` inline for tier grading, so removing it would break grading on
the days the Mac IS awake. Two per-machine stores of immutable date-keyed NSE
files; they cannot diverge. The VM is now the complete record.

### ⚠️ Open / for the next person

- **The 15:45 failure has NO confirmed cause.** The contention theory I first
  offered was DISPROVED: `intraday_tracker` at 15:45 self-gates and makes zero
  Dhan calls, `chain_archiver` finishes 15:40:42, `main` 15:39:37 —
  `eod_summary` is the only Dhan caller in that window. Do not "fix" it again
  without evidence; wait for the new logging to name it.
- **Regression worth remembering:** the new `desk_tickers()` default read the
  LIVE open book from inside pytest — six existing tests instantly grew 5
  phantom failures. Fixed with the standing muzzle. **Fourth instance of that
  family** (07-22, 07-23, 07-27): a new default that reaches live state is the
  single most repeated defect in this codebase.
- Suite **1,700 green**. 8 new tests.

---

## 📍 CURRENT STATE — 2026-08-04: Ollama is now a strictly ON-DEMAND service; the desktop app's background agent MUST stay disabled

**Nothing in the trading path changed today. No VM change, no sizing/treasury
change, no data file written.** This was a Mac-lane operational fix only. The
equity-desk / intraday-tracker work in `d7d888f` and `ca6558f` is a PARALLEL VM
session's, not this one's — see the commit note below.

### The architectural change, in one line

**Ollama no longer runs in the background at all. It is started by the job that
needs it, and killed the moment that job ends.**

Before today it had served continuously since **7 Jul** (28 days) on Ollama's
default **5-minute keep-alive**, holding ~2GB of model weights resident on an
8GB Mac after every scheduled call.

### The three layers, all live and verified

| Layer | What it does |
|---|---|
| **`scripts/ollama_session.sh`** (new) | The ONE door for server lifetime. Sourced by both LLM-using LaunchAgent wrappers. Starts the server, waits until it genuinely **answers** `/api/tags`, and on exit kills it plus its `ollama runner` children. |
| **`scripts/mine_edges.sh`, `scripts/run_evolution.sh`** | Source the switch, `trap ollama_session_stop EXIT INT TERM`, propagate the python exit code. |
| **`src/local_parser.py:_chat()`** | Sends `"keep_alive": 0` in the request body — belt and braces against the server-env policy drifting again. |

### ⚠️ THE STANDING CONSTRAINT — do not undo these three things

1. **The Ollama desktop app's background agent must remain DISABLED.**
   System Settings → General → Login Items & Extensions → **Allow in the
   Background** → Ollama **OFF**. Owner performed this 2026-08-04; verified
   the same day (`launchctl list | grep -i ollama` returns nothing, port
   11434 closed, zero processes). **If it is ever re-enabled, the whole fix
   is void** — the Electron app is a *supervisor*: it respawns `ollama serve`
   within seconds of any kill (reproduced: killing the server produced a new
   one with ppid = the app). Re-enabling it restores a 24/7 server.
2. **Never put `exec` back in front of the python line in the two wrapper
   scripts.** `exec` replaces the shell and silently discards the `trap`,
   which orphans the exact server this design exists to kill. Both files
   carry a comment saying so.
3. **Never replace the targeted kill with `pkill ollama`.** The stop function
   reaps only PIDs it recorded via `pgrep -P` before killing the parent. A
   blanket `pkill` would kill the owner's own GUI session if they opened one
   mid-run.

### Terminology, stated precisely because it matters to the next agent

The **service** is fail-closed / default-deny: absent unless a job explicitly
starts it, so a stray API call from any other app gets connection-refused
rather than waking anything. The **consumers** remain fail-OPEN: a failed
start degrades to "skip the LLM extraction", never to a failed job — the same
posture the VM has permanently (it runs no Ollama by design, decision #47).
`ollama_session_start` is therefore called as `|| true` in both wrappers, and
that is deliberate, not sloppiness.

Adoption rule: if a server is **already** reachable when a job starts, that is
the owner's own GUI session — the job uses it and does **not** kill it on exit.

### Verified 2026-08-04 (run, not asserted)

Owned path: start → `owned=1`, port answering, real `/v1/chat/completions`
**HTTP 200 in 4.4s**, `/api/ps` → **`{"models":[]}`** two seconds later
(weights unloaded immediately), stop → **zero processes, port closed**.
Adopt path: pre-existing server correctly left running. **Full suite: 1,700
passed** (note: `CLAUDE.md` still says 1,589 — that count is stale, not a
regression).

### Honest scope limit

**Ollama was never the machine's actual swap consumer.** At triage it held
4.7MB with zero models loaded; swap was 4,520MB and is *higher now* (4,836MB)
with Ollama completely dead. That load is Chrome (~12 helpers) and Claude.app.
This fix removes a real ~2GB **periodic** spike at 21:00 and Saturday 02:00 —
it does not and will not reclaim that 4.5GB. If sustained swap is the real
complaint, Chrome is the next thing to examine.

### ⚠️ Commit-provenance warning for the next agent

**`git log --grep=ollama` finds nothing for this change.** All six files landed
in **`ca6558f`**, whose subject is `chore(cron): migrate bhavcopy to the VM…`.
A parallel VM session staged the entire working tree at 16:46 IST and swept
this work into its own commit. Content was verified intact afterwards. History
was **not** rewritten to separate it — `ca6558f` was already pushed to
`origin/main` with a second session live in the same repo, making a rewrite
both destructive and unsafe. Full detail in ledger **Issue 23**.

### Next immediate steps

1. **Watch the first real on-demand run** — `com.adityagupta.alpha-edge-miner`
   at 21:00 tonight. Check `logs/ollama_session.log` for
   `started … / ready after Ns / stopped cleanly, port released`, then confirm
   `pgrep -lf ollama` is empty afterwards. Tonight is the first unattended
   exercise of the trap; today's proof was manual.
2. **Saturday 02:00** — `com.alphatrading.evolution`, same check. This is the
   run that previously left a model resident overnight.
3. **Open decision, not acted on:** `com.adityagupta.alpha-edge-miner` still
   has `RunAtLoad = true`, so the miner fires on **every login**, not just at
   21:00. Now bounded by the on-demand server, but whether a login-time run is
   wanted at all is unresolved.

---

## 📍 CURRENT STATE — 2026-08-01 (evening): Auto-Discovery is a WORKING INSTRUMENT that admits nothing

**Nothing in the trading path changed today, and nothing touched the VM.**
This was a Mac-lane research arc start to finish. No data file was written —
`discovered_episodes.json` still does not exist, because nothing was admitted
and `route_to_court` correctly has nothing to route. Stage-B's clock is
untouched (AD-* has zero execution authority and does not feed it).

### Final state of AD-1 → AD-4

| Stage | State |
|---|---|
| **AD-1** | **VALIDATED and accurate.** Unsupervised over 4 cross-asset channels, ZERO human labels — it independently finds COVID (2020-03-18), 2011 US-downgrade, GFC, 2014 oil collapse, 2010 Greek crisis, taper tantrum, demonetisation. Anchors land on the historically correct DAYS. |
| **AD-2 (shock)** | **A FULLY WORKING INSTRUMENT with a brutally high bar — ZERO admissions.** Graded, beatable, honest p-values. Not broken: refusing. |
| **AD-2 (motif)** | **DELIBERATELY UNBUILT.** Any non-shock candidate returns `motif_gate_pending` and can never be admitted. The DTW-statistic surrogate test is the missing slice — deferred on purpose, not forgotten. |
| **AD-3** | Built (`route_to_court`); full Dept-5 registry enrolment still unbuilt. Nothing to route. |
| **AD-4** | Built (`merged_catalog`, human ∪ auto with `discovery=True` flagging). |

### The definitive verdict (500 surrogates, full lake, seed pinned)

```
null:  circular-shift  median 3.66  p95 5.32      phase-randomized  median 3.39  p95 5.46
2020-03-18 COVID  co-stress 5.37   p_circ 0.0419 ✓   p_phase 0.0579 ✗   -> REFUSED
ADMITTED: 0
```

**COVID split the two nulls by 0.008** — cleared circular-shift (20/500 beat
it), failed phase-randomized (28/500). The AND-gate refused. That is "one null
is not enough" doing its job, and the bar was fixed BEFORE the result was
seen. The defensible statement: *over 38 years, even COVID's cross-asset
co-movement sits at ~the 95th percentile of chance alignment — strong, not
conclusive.* Verdict shipped as-is per the kill-criterion doctrine.

### 👁️ OFFICIAL FORWARD-DATA WATCH ITEM — `2026-03-27`

**Status: WATCH, not a regime.** Co-stress **3.59 — the #3 cross-asset
co-movement in 38 years** (behind only COVID 5.37 and the 2011 US-downgrade
3.78), with **no obvious human label**. AD-2 verdict: `p_circ 0.5768 /
p_phase 0.3613` — comfortably INSIDE what chance alignment produces, so it is
**not** an admitted regime and must not be described as a discovery.

What makes it worth watching anyway: it is large, recent, unlabelled, and it
survived the statistic redefinition (it ranked #10 under the old broken RMS
and rose to #3 under the valid one — it is not an artifact of either). The
honest read is "real event, not statistically exceptional versus chance." If
it is the start of something, FORWARD data will say so — re-run the scan
periodically and watch whether a cluster forms around it. Do NOT wire it to
anything.

### How today got here (three layers, each exposed by fixing the one above)

1. Ragged missingness → AD-2 had never been runnable on the real lake.
2. Block-bootstrap splices → fixed with `circular_shift`; the null got WORSE,
   which is what proved the surrogates were never the real problem.
3. **The statistic itself** — `system_stress` (RMS over channels PRESENT) has
   a variable divisor, so it mis-dated crises, promoted 1960s single-channel
   ghosts into AD-1's ranking, and made every null unbeatable. Replaced by
   **`co_stress` = second-largest |z|, ≥3-channel floor** (owner ruling,
   validated by a 50-surrogate preview before any production code). Scannable
   window is now 1988-06+; `system_stress` kept as a superseded reference,
   called by nothing.

Also shipped: `_z_series` O(n²)→O(n·baseline), **bit-identical** to `zdelta`
(test-pinned) — 33s→1.1s per pass, and a full-lake 200-surrogate null from a
projected 3.7 h down to 8 min. `build_null` is candidate-independent and
computed once; use `gate_many` for scans. 22 AD tests, suite **1,691**.

### ⏭️ Next session options (none urgent, none blocking)

- Motif significance (the DTW surrogate test) — the last unbuilt AD slice.
- AD-3 registry enrolment — only worth it once something admits.
- Re-run the scan later and check whether `2026-03-27` gains company.

---

## 📍 SUPERSEDED (same day, morning) — 2026-08-01, AD-2 run in the wild: the gate is broken

> Kept for the record; the evening block above is the finished state. The
> "two defects" below were both fixed hours later, and fixing them exposed
> the third and real one (the statistic).

### The headline: AD-1 is validated, AD-2 cannot admit anything

Owner ruled Option 1 — run AD-1+AD-2 on the real 25-year lake BEFORE building
the motif gate or registry enrolment. That call paid for itself immediately.

**AD-1 passed with distinction.** Unsupervised over four cross-asset channels
with ZERO human labels, it independently found **2013-05-29 taper tantrum**
(the medoid of our own human A2 archetype), the GFC as a coherent multi-phase
cluster (2008-01-23/05-15/10-22, 2009-01-26), COVID, Ukraine 2022-03-08,
demonetisation 2016-11-23, the 2007 quant quake, and — on the full lake —
the **1991-07-03 India BoP crisis** at the highest stress in 64 years.

**AD-2 returned 0 admitted of 25, and the null proves it is misspecified
rather than strict:**
- block-bootstrap max-stress **median 4.66** vs COVID (the most violent event
  in the window) at **5.27**. The typical RANDOM surrogate nearly beats the
  worst real crisis. From candidate #6 down, `p_block` is a flat **1.0000**.
- Cause: `block_bootstrap` concatenates random 20-day blocks, so every
  junction is an artificial discontinuity — and `system_stress` is built to
  detect exactly that. ~268 splices per surrogate, and the statistic takes
  the MAX. "Worst splice artifact in 268 tries" vs "worst real event in 20
  years" is a rigged contest.
- The tell: a too-strict-but-VALID gate clusters p-values near the 1/201
  resolution floor. These saturate at the ceiling.
- A second, independent defect: **AD-2 had never been runnable on the real
  lake at all** — ragged channel coverage makes each surrogate a different
  length and `_max_stress_of` overruns (`IndexError`). Worked around today by
  restricting to the post-2006 full-observation window; NOT fixed.

**Unresolved, and the most interesting thread: `2026-03-27`, stress 3.65 —
second-highest in twenty years, no obvious human label.** The instrument
cannot currently measure it. Re-test against a corrected null before drawing
any conclusion.

### ⏭️ NEXT SESSION — owner ruling, fix the statistical foundation

1. Replace the block bootstrap with a **circular/stationary bootstrap** (one
   wrap-point instead of ~268 splices).
2. Implement the **ragged-missingness fix** (surrogates keep the input's
   length and hole pattern) so the full 25-year history returns to scope.

They interact: with missingness fixed, the 1991 BoP crisis re-enters and is a
far stronger test of any corrected null than anything post-2006. Note the
`_z_series` O(n²) cost — a 200-surrogate null over 5,363 sessions took 31 min;
the null does NOT depend on the candidate, so build it once and share it.

### Also verified today (both were pending in the ledger)

- **Task K:** 07-31 sweep `{'swept': 55, 'expired': 0}` — the 45 expiries on
  07-30 were backlog exactly as predicted. Structural amnesia closed.
- **suggest DH-905 retry:** first post-deploy run clean (0 DH-905, HDFCBANK
  read normally) — but the window did not fire, so the retry path is
  **deployed, not proven**. Still awaiting its first real failure.
- **NEW open item:** the Stage-B tracker does not model NSE holidays and slack
  is pinned at **0**. Ganesh Chaturthi / Gandhi Jayanti / Diwali all fall
  inside the window, so the 60th session realistically lands past Oct 13 with
  zero failures. Either model the holiday calendar or accept Oct 13 as
  approximate — owner's call, not acted on.
- Untouched and still open from 07-31: `logs/autonomous_bug_report.jsonl` on
  the VM is at **72 items**, untriaged since the 07-21 autonomous run.

---

## 📍 CURRENT STATE — 2026-07-31, first real close-down run, one bug found

**The 2026-07-30 close-down ran for real and HALF worked — now fully fixed.**
Proven working: the note was captured (21:57) and `pmset -g log` shows
`Software Sleep pid=24128` at **21:57:13**, i.e. the script's own
`pmset sleepnow`. The 07-21 "never slept the Mac" defect is genuinely
closed. Proven broken: Chrome and Ollama were the same PIDs the next
afternoon — `app_running()` used `ps … | grep -q` and, under the script's
`set -o pipefail`, `grep -q`'s early exit gave `ps` a SIGPIPE and the
pipeline returned 141, so **a running app reported as not running**. It is
position-dependent (a race), which is why testing missed it. Fixed with a
pipe-free `case "$(ps -axo comm=)"`. Full reasoning, plus the two
hypotheses tested and rejected first (TCC automation; Chrome's
`exit_type`), is in `docs/observation_week_ledger.md`.

**The lesson worth carrying:** the close-down tool ends the session by
sleeping the Mac, so a silent failure is unobservable by construction. It
now tees to `logs/office_close.log`, surfaces `osascript` stderr instead
of discarding it, and **verifies each quit by polling up to 10s**,
reporting `!! <app> STILL RUNNING` plus a notification. Read that log
after the next close before trusting it.

**Still unverified:** the EOD-chain SLOW path has never executed in this
script (the tier table has been fresh on every run so far). Everything
else has now run for real at least once.

**Unchanged:** Stage-B calendar time is still the only open gate
(decision #86, target ~2026-10-13, Oct 1 preliminary). VM healthy,
`service: active`, firm treasury ₹2,00,000 pool, equity desk 3 open.
Noted in passing, NOT acted on: `logs/autonomous_bug_report.jsonl` on the
VM is at **72 collected items** — nobody has triaged it since the 07-21
autonomous run.

---

## 📍 CURRENT STATE — 2026-07-30 late night, Mac OS-workflow merge

**What changed:** `scripts/office_close.command` was merged with the
Mac-local `Office Close.app` into ONE ordered end-of-day pass, and three
defects were fixed. The headline one: **the script had never actually slept
the Mac since it was written on 2026-07-21.** It backgrounded
`( sleep 3; pmset sleepnow ) &` and then quit Terminal, which made Terminal
raise "terminate running processes?" — and that dialog's DEFAULT button
kills the pending `pmset`. Full reasoning and the two other fixes (quit
guard, anchored orphan-sweep regex) are in
`docs/observation_week_ledger.md`; the module rows are in `MODULES.md`.

**Nothing in the trading path was touched.** This is OS workflow only — no
sizing, treasury, strategy or ledger code was modified, and the suite is
unchanged by it.

**What the next person should know before trusting it:**
1. **The full run is UNVERIFIED end-to-end.** Dialog, tracker append,
   phases 1-4 and the guardrailed sweep were each exercised (dry-run +
   stubbed dialog), but no real `pmset sleepnow` was issued and the
   EOD-chain SLOW path never executed — the tier table was already fresh
   (`as_of 2026-07-30T19:15:48`). **First real slow-path run is still
   unobserved.** Use `--dry-run` first if in doubt.
2. **"Shut the lid and let it finish" does NOT work and cannot be made to
   work safely.** Closing the lid sleeps the Mac; in-flight work freezes
   and resumes on wake. `caffeinate -i -s` now wraps the EOD chain (idle
   sleep only; `-s` is AC-only), but no assertion survives a clamshell
   close — only `sudo pmset -a disablesleep 1` would, and that was
   deliberately not set. On the fast path the whole pass is ~10s and the
   script sleeps the Mac itself, so this only bites on the slow path,
   where a loud KEEP THE LID OPEN warning now prints.
3. **Ollama auto-start was disabled by the owner** via Login Items &
   Extensions / `launchctl disable gui/501/com.ollama.ollama`. It
   registers through SMAppService/BTM (`com.ollama.ollama`), NOT a
   LaunchAgent plist — there is no plist to hunt for. Relaunching the app
   manually can re-register it; re-check with
   `launchctl print-disabled gui/$UID | grep ollama`.
4. Supporting Mac-local files live OUTSIDE the repo in `~/Scripts/`
   (`jarvis_parse.py`, `vm_queue_sync.sh`, the two `.applescript`
   sources) and are NOT version-controlled here. If the repo is cloned to
   a new machine, `office_close.command` still runs — the Jarvis step is
   fail-open and appends the raw note when the parser is absent.

**Unchanged and still the open gate:** Stage-B calendar time (decision
#86, target ~2026-10-13, Oct 1 preliminary). See the block below.

---

## 📍 CURRENT STATE — 2026-07-30, status check + infrastructure cleanup

**Push status (corrects the 07-27 block below):** local `main` and
`origin/main` are **identical at `609dd80`** — the two H4 commits
(`9d8c13d`, `7e0d635`) are pushed. The 07-27 "UNPUSHED / decide whether to
push" item is closed; nothing on this Mac is ahead of the remote.

**Where the system is:** the production VM (`alpha-trading-vm`, project
`project-37632031-10d0-47dd-b6f`) is healthy and **deployed to `8547473`**
(evening pull, ff-only, clean tree; services untouched-and-active,
`/api/health` ok, cron unchanged — the suggest fix is a cron-read module,
no restart needed). `main` == `origin/main` == VM. Book per the 07-30 CEO
brief: firm MTM ₹2,39,266 on a ₹2L base (realized +₹44,215), 2 spreads +
3 equity positions open, day 9.

**Done this session (2026-07-30):**
1. **Zombie VM deleted.** The abandoned original cron box (DECISIONS
   #18/#24 — `alpha-trading-vm` in project `alpha-trading-app-2026`,
   IP 35.202.72.49, no git, yfinance-era 14-file `src/`) was still RUNNING
   with two daily cron jobs firing into its own logs. Identity was
   positively confirmed against production (different project, different
   external IP) before `gcloud compute instances delete`; project now
   lists 0 instances; production untouched and verified RUNNING after.
2. **07-27 intraday-capture mystery resolved as rate-limiting** — see the
   observation ledger entry dated 2026-07-30. 84/84 every slot on the
   throttled client.
3. **08:00 suggest DH-905 skips root-caused and fixed (`8547473`,
   deployed).** NOT an HDFCBANK/security-id bug — a transient Dhan-side
   window at run start hitting whichever early watchlist names fall in it.
   Fix: end-of-run retry pass + un-truncated error print (full ledger
   entry, 3 new tests, suite 1,630).
4. **H4 got its first real-data verdict AND its shadow (evening).** The
   comparator ran on real NIFTY 50 history 2022-2025: only lookback 10
   graduated (Sortino 2.62 vs 2.42, drawdown 6.54R vs 6.69R; lb-3
   reproduced the #68 pileup with 11.55R). Owner ruling: **shadow it** —
   built `src/validation/h4_shadow.py` (sleep_phase Task J,
   `trial.record_signal_fire` mode `SIGNAL_SHADOW`), host-linked rows
   resolved by the existing Task I sweep, ZERO execution authority.
   Graduation to `validation/registry.py`/sizing stays a Department 5
   decision gated on the forward record this accumulates. Suite 1,640.

5. **`decay_engine` WIRED — the 07-25 open item is CLOSED** (`426fc34`,
   owner-approved). Now sleep_phase **Task K**; `decay_engine.py` itself
   untouched. Path-aware DB muzzle (`PRAGMA database_list`, fails SAFE);
   9 tests. **Verified live in tonight's 20:00 run.**

### Session summary — the three things that changed today

1. **H4 shadow: the wall-clock fail-quiet trap, caught before it fired.**
   `h4_shadow` shipped requiring `bars[-1].date == today`, but Dhan serves
   only COMPLETED sessions — at 20:00 the newest bar is T-1. The task
   would have logged a plausible `no_fresh_bar_today` skip **every night
   forever** while the forward ledger stayed empty, and "selective signal"
   would have been indistinguishable from "structurally dead". Fires are
   now dated by their BAR (idempotent per signal+host+bar, mark computed
   as-of that bar) plus real `stale_feed` / `bar_not_after_entry` guards.
   Verified live tonight: `{'scanned': 2, 'fired': 0, 'skips':
   {'no_fresh_extreme': 2}}` — the real condition, honestly evaluated.
2. **Task K + structural amnesia fixed.** Edge decay was wired
   (`426fc34`), and its first sweep expired 45 of 45 `concentrates_in`
   edges while every causal edge lived — a RATE mismatch, not a sweep bug:
   λ=0.05 (~14-day half-life) applied to an all-time statistic that is
   only re-observed when a fresh deal lands, so the affinity layer sat
   permanently invalid between deals. Owner ruling: λ=0.002 (~347-day
   half-life). `scripts/resurrect_affinity.py` repaired it by REBUILDING
   each edge from source of truth — the naive `SET decay_lambda,
   invalid_at=NULL` was rejected as a self-reverting no-op (the sweep had
   already crushed `confidence_score` and overwritten `valid_from`).
   Applied on the VM: **11 of 45 restored** (32 genuinely stale >3y, 2 no
   longer true), and **11/11 verified to survive the next sweep**.
3. **Stage A verified already unlocked — natively, with zero VM disk
   used.** All 132 mapped sector CSVs were ingested back on 07-24; tonight's
   re-run added exactly **1** new date. No CSVs were copied to the VM (it
   reads built artifacts; rebuilding on the e2-micro is the OOM risk), and
   per owner ruling the roster was left untouched and artifacts were NOT
   rebuilt. **Verification: 8 of 9 registry cells render with 5-10 legs at
   `MIN_EPISODE_LEGS=5` — sector rotations are no longer abstaining.**

4. **Edge-to-Cloud asynchronous architecture formalised.** Standing rules:
   the e2-micro never runs heavy work, and the VM pipeline never depends on
   the Mac being online. The VM already abstained correctly
   (`require_cache=True`), but the reason scrolled away in a log.
   `src/mac_queue.py` is the missing outbox — on the EXISTING cache-miss
   branch the VM appends to `data/mac_pending_tasks.jsonl` and carries on.
   Append-only, idempotent per (task, day), fail-open, pytest-muzzled.
   **Not a new detector** (no new failure mode), and the non-dependency is
   regression-tested: a test explodes the queue and proves the nightly run
   still finishes. Daily read:

```bash
bash scripts/daily_health_and_queue.sh
```

### ✅ Stage B timeline — RULED (decision #86), no longer an open question

**The standard does not slip, the calendar does.** The 60-session bar for a
mature Stage-B verdict is HELD; the official completion target is now
**~2026-10-13**. **Oct 1 remains a PRELIMINARY, NON-BINDING read** — if
early forward windows show significance by then, good; otherwise wait for
the 13th. **Zero graded calls is expected, not a concern** — forward
windows need calendar time to mature. `stage_b_tracker.py` encodes both
dates, so the tool now measures against the ruling. Full reasoning in
DECISIONS.md #86.

The arithmetic that drove it is below, kept because it is the evidence:

### ⚠️ Why the date moved — 60 sessions was unreachable by Oct 1

The new `scripts/stage_b_tracker.py` (read-only by construction) surfaced
this on its first run. The clock is at **7 DISTINCT sessions, not the 12
raw rows** — the 07-22/24 build era wrote 2-4 rows per session; since
07-27 the cron is exactly 1/weekday and clean. **53 sessions were still
needed with only 45 weekdays left before Oct 1**, so perfect uptime landed
at ~52 — uptime alone could never have closed it, which is why the date
moved rather than the bar. Sessions are also not the same thing as
evidence: a verdict needs matured forward windows (`MIN_FWD_CALLS`=7).

Check any time:

```bash
python3 scripts/stage_b_tracker.py
```

### Tonight's 20:00 run — both new tasks verified live

```
J. h4 shadow: {'scanned': 2, 'fired': 0, 'skips': {'no_fresh_extreme': 2}}
K. edge decay:    {'swept': 86, 'decayed': 86, 'expired': 45}
```

- **Task J** evaluated the real condition (correct: both open spreads are
  bearish, NIFTY 50 closed UP on 07-29, so no fresh 10-day low). 0 rows
  written — an honest quiet night, not a stalled task.
- **Task K's 45 expirations are BACKLOG, not breakage** — all 45 are
  `concentrates_in` deal-affinity edges carrying historical `valid_from`
  stamps (years of arrears cleared in one step, by design); all 44
  causal-reasoning edges survived, as did all 4 decay-exempt (λ=0)
  loss-permanence edges. Nothing deleted. **Expect `expired` ≈ 0 from
  tomorrow — do NOT read tonight's number as an incident.**
- **One OPEN QUESTION logged for the owner** (ledger, not acted on): is a
  ~14-day half-life (λ=0.05) right for `concentrates_in` edges, which are
  re-observed only when a new deal lands? One-line fix at the
  `entity_affinity` write site if the owner wants it; current behaviour
  stands until ruled on.

**⚠️ VERIFY NEXT SESSION:** the first post-deploy 08:00 run (2026-07-31).
Expect `recovered` lines in `suggest.log` instead of lost names; if the
window fires, the widened error print will finally show WHICH parameter
Dhan objects to — read it before theorizing further. Also confirm Task K's
`expired` count drops to ~0. Remaining 07-27 open items (real H4 run —
**done tonight**, spread-tuner step 2 scoped-not-built) as noted.

**No Strategy Registry rulings are pending** — a stale memory index said
three were stalled since 07-23; verified false. All three were RECEIVED
07-23 and locked into `docs/strategy_registry_spec.md` §9; SR1-3 shipped
same day (`49ff347`), Stage A+B built 07-24, SR-4 cancelled by the owner.
The only open gate is Stage B calendar time: **12 declarations, 24
pending, 0 graded** (verified 07-30 — nothing has matured yet; the
missing `macro_strategy_scores.jsonl` is correct, not a bug).

---

## 📍 CURRENT STATE — 2026-07-27 night, session close (H4 harness + spread-tuner design)

**Where the system is:** the VM is UNCHANGED — still on `b4e0437`. Everything
this session produced is **local, committed to `main`, and UNPUSHED**
*[superseded 2026-07-30: since pushed — `main` == `origin/main` at
`609dd80`]*. Two commits are ahead of `origin/main`:

| Commit | What |
|---|---|
| `9d8c13d` | `feat(validation)` — the H4 simulator experiment harness |
| `7e0d635` | `fix(validation)` — H4 loud-abort + spread-tuner design + floor 5→10 |

**⚠️ FIRST THING NEXT SESSION:** decide whether to push *[resolved
2026-07-30: pushed]*. Nothing here touches
the live execution path (the H4 harness is off-cron, `forecast.py` is
deliberately not wired), so there is no urgency and no risk in the delay —
but the work is only on this Mac until it is pushed.

### What was built

1. **H4 experiment harness** (`src/validation/h4_comparator.py`, design doc
   `docs/h4_simulator_experiment_design.md`). Runs `baseline` (today's
   one-and-done #68 gate) vs `pyramid` (staged adds/trims, stack capped at
   3) through the SAME simulator/plan_tracker machinery over identical
   bars. Continuation requires mark improvement **AND** a fresh N-day
   extreme (grid 3/5/10) — signal repetition alone does nothing, the direct
   guard against reproducing the #68 pileup. Adverse is staged: ≥25% of
   max_loss trims 50% of remaining lots once, ≥35% closes the rest.
2. **Loud-abort fix** (same module). See the ledger — a DH-901 expired token
   made the first real run print a tidy `insufficient_data` report having
   simulated nothing. Now `H4DataError` aborts before any policy walks a
   day, names the likely cause, and exits 1.
3. **`docs/spread_aware_tuner_design.md`** — scopes learning from spread
   outcomes. Not built, design only.
4. **`scripts/export_trade_book.py`** — a read-only journal→CSV audit export
   (NOT written by this session's agent; found untracked and committed after
   a full read + safety scan). Its output `trade_book_audit.csv` is now
   git-ignored.

### The three findings that matter most

- **The live book is degenerate along every structural axis.** All 15
  resolved spreads are `bear_put_spread`; all 7 regime-stamped ones are
  `('bearish','mid')`. One structure, one regime, one direction. Any
  archetype partition over today's book collapses to a single bucket, so
  the spread-aware tuner design carries a **≥2-populated-bucket guard** —
  without it the tuner would "learn" a single weight applied to every
  trade, a uniform rescale that changes nothing while looking like
  learning. **Expect the first tuner run to emit nothing but neutral
  weights. That is a pass, not a failure.**
- **`tuner.py` is deliberately off-cron, not broken.** Line 1 carries the
  `# MANUAL OFFLINE TOOL` marker (Rule 5, Phase-1 audit 07-25). It is also
  structurally blind to the options book (`_resolved_buy_outcomes` filters
  `action != "BUY"`), so cronning it would rewrite `brain_weights.json`
  with the same neutral values it has held since 2026-07-05. **No cron was
  installed.** `tuner_min_samples` raised 5 → 10 (owner ruling); global
  value, equity path has 3 trades so no behaviour change today.
- **Two directives this session were built on false premises and were
  refused/reported rather than executed** — a delete order citing a
  non-existent forensic report (would have removed `decay_engine.py` and
  `resonance.py`, both live with 4 importers and a green 27-test suite),
  and a journaling fix for a `regime: None` leak that does not exist (my
  own sampling error — I inspected the oldest entry and generalized). Both
  are written up in the ledger, including my error, not silently dropped.

### Open items unchanged by this session

Items 1–8 from the 07-27 evening block below are all untouched. Note
specifically that **`decay_engine` remains UNWIRED** (open item 1) — this
session verified it is live-imported and refused to delete it, but did NOT
wire it. That decision is still the owner's.

### Next steps

- **Push the two commits** (or decide not to).
- **Regenerate the Dhan token**, then run the real H4 experiment:
  `python3 -m src.validation.h4_comparator --start 2022-01-01 --end 2025-06-30 --underlying "NIFTY 50"`
  — owner planned this for the weekend on the local Mac. It will now abort
  loudly if the token is still dead, instead of faking a verdict.
- **Spread-aware tuner step 2** is scoped and ready to build when the owner
  wants it — advisory/shadow only; live wiring (`forecast.py` → proposer)
  is **descoped by owner ruling** until the registry promotes it.

---

## 📍 CURRENT STATE — 2026-07-27 late evening, H4 simulator experiment harness built

**Where the system is:** design + code only — nothing deployed to the VM,
nothing registered, no live-path change. The owner pivoted from the
Intelligence & Autonomy trio (below) back to H4 (`docs/hypotheses.md` §H4:
asymmetric position management — add on price-confirmed continuation, trim
on adverse). This session did NOT touch H1/H2/H3, and confirmed by grep
that `config/trade_hypotheses.json` still has no code reading it — that
stays a pure human capture surface, owner's explicit call ("keeping
administrative bloat to a zero").

**Built, not yet run for real:**
1. `docs/h4_simulator_experiment_design.md` — the design doc, owner-approved.
2. `src/validation/h4_comparator.py` — the experiment harness. Runs
   `baseline` (today's one-and-done #68 gate) vs `pyramid` (stacked adds,
   capped at `H4_MAX_STACK`=3) through the SAME simulator machinery
   (`src/simulator.py` + `src/plan_tracker.py`'s pricing/resolution
   helpers) over identical bars. Continuation = mark improvement AND a
   fresh N-day extreme (grid `[3, 5, 10]`, owner-specified) — signal
   repetition alone changes nothing, the direct guard against the #68
   pileup. Adverse is staged (owner-specified): ≥25% of max_loss trims 50%
   of remaining lots once; ≥35% closes the rest. Full MODULES.md entry has
   the complete mechanism.
3. Verified end-to-end on synthetic in-memory bars (not real Dhan history —
   no token in this session): the loop runs, both policies propose/resolve/
   trim, `compute_report()` correctly returned a **"does_not_graduate"**
   verdict on the toy data (pyramid's Sortino was lower, not higher, than
   baseline's) — i.e. the harness does not rig itself to always pass.
   Confirmed isolation: `data/journal.jsonl` untouched (`git status` clean
   apart from the two new files), rows land under the `sim:h4:<hash>`
   namespace so they can never collide with `src/simulator.py`'s own
   production `sim:<hash>` rows, and `src/performance.py` still reads only
   the real journal.
4. Full suite green: 1,627 passed, ~93s (`h4_comparator.py` adds no new
   tests yet — it's a research harness, not on any execution path, same
   status as `simulator.py` itself).

**Known, documented gap — not a bug, a scope cut:** no Vega/Delta ceiling
(#71) on stack adds. `build_synthetic_chain()` (the simulator's modeled
option chain) carries no per-strike Greeks, and `portfolio_greeks.aggregate()`
needs real Dhan-shaped `greeks.{delta,...}` to price anything — there was no
honest number to gate on without fabricating one. Only the count-based
`H4_MAX_STACK` cap guards concentration in this first cut. A real Greeks
ceiling needs the synthetic chain to grow modeled Greeks first, called out
explicitly in the module docstring so it isn't silently forgotten.

**Next steps (owner's call, not started):**
- Run the harness against REAL Dhan daily history (needs a live token) over
  a real multi-year range, not the synthetic toy series used to verify the
  code path.
- Once a real run produces a verdict, decide whether any lookback graduates
  before touching `validation/registry.py` or live sizing — per the
  owner's own H4 note, nothing here trades until it earns that.

---

## 📍 CURRENT STATE — 2026-07-27 evening, after the Intelligence & Autonomy trio

**Where the system is:** the VM runs `b4e0437`. All three "Intelligence &
Autonomy" directives from this morning's backlog (see the prior 07-27 section
below) are designed, built, tested, deployed, and verified live — same day.
The **NO NEW ENGINES** constraint held throughout: every directive reused an
existing table, door, or resolver; nothing new was stood up.

### Directive 3 — The Walkaway Protocol (`f15182e`)

The risk-of-ruin halt was previously silent (a DB row, no card). Now:
`portfolio_manager._ruin_halt_card` fires ONE 🔴 SYSTEM PAUSED Discord card
per IST day (de-dup via `account_events`), from both transition sites, and
`halt_banner_lines()` re-fires it daily through the existing Mon-Fri digest
reads (owner ruling: daily reminder, no separate cron). **Owner ruling: NO
override door** — resume is a clean-sheet decision only, never automated.
Exits-during-halt is now regression-tested (`release_margin` deliberately has
no halt check). Design: `docs/walkaway_protocol_design.md`.

### Directive 2 — The CEO-View Discord (`66c60a3`)

New `src/ceo_language.py` is the ONE place allowed to turn macro/strategy
data into a sentence, enforcing two honesty gates: (1) an analog is named
only when `macro_regime.declare()` itself declared a match, (2) a strategy
is called more than an in-sample hunch only when its Stage-B scoreboard cell
graduated to `FORWARD_CONFIRMED` (0 cells as of today — everything reads
honestly as "still accumulating"). **Never says "executing"** — a deliberate
correction to the owner's own example phrasing, because the Macro Regime
Engine has zero execution authority (Rule 5); flagged in
`docs/ceo_view_discord_design.md` rather than silently reworded. New
`src/morning_brief.py` card at 08:05 Mon-Fri (cron #25, installed and
verified on the VM); EOD + CEO brief got plain-English lead lines over
numbers they already compute.

### Directive 1 — Opportunity Cost Tracking (`b4e0437`)

Gate-blocked trades now route into the EXISTING `shadow_trades` table
(`trial.record_block`, additive `mode` column — the `host_ref` upgrade
pattern) instead of being silently discarded. **Only the exposure gate is
wired** — it is the one block type with a genuine host (the conflicting
position, whose `trade_id` IS `outcomes.journal_ref`), so the untouched
Sleep-Phase sweep resolves it with zero new resolver code. Halt/margin/
sizing-veto blocks are deliberately NOT given a fabricated outcome — pricing
those hypotheticals would need the synthetic-chain model, whose known ~10x
generosity (item 5 below) would make every risk gate look artificially
expensive. Corpus isolation is three independent layers (ref-prefix
exclusion, namespaced pattern_id, explicit mode filter), all tested. Surfaces
in the weekly harness digest, silent until something is actually blocked.
Design: `docs/opportunity_cost_design.md`.

**⚠️ Self-caught bug, same session:** the exposure-gate seam initially opened
the REAL `brain_map.db` when untested, and a full suite run wrote 4 fixture
rows into production — `python3 -m src.opportunity_cost` then reported a
fabricated "4 duplicate trades refused." Found by checking the live DB after
green tests rather than trusting the count. Fixed same hour (muzzled under
`PYTEST_CURRENT_TEST`, same doctrine as the notifier's webhook muzzle), rows
purged after verification, regression test added. Logged in the observation
ledger as the **third** instance of this family (07-22 journal drift, 07-23
digest-queue leak) — the standing lesson: a new seam that opens its own DB
connection needs the pytest muzzle in its first commit, not a later fix.

### What this means for the open items below

Items 1, 2, 4, 6, 7, 8 (decay_engine wiring, the young macro clock, pre-2019
sector proxies, the options-sim inflation caveat, strategy_registry
abstention, the parked branch, rss_ingester) are **unchanged** — today's work
did not touch any of them. Item 3 (cron de-dup) was already closed this
morning. Item 5 (sim inflation) is now also load-bearing for Directive 1's
design — it is the stated reason halt/margin/veto blocks get no fabricated
P&L.

### Next steps (not started, no directive yet)

- Watch the opportunity-cost read accrue — realistically weeks before 5
  resolved blocks exist for a verdict.
- No further Intelligence & Autonomy work is queued. The next request is
  the owner's to make.

---

## 📍 CURRENT STATE — 2026-07-27, after the deploy-gap close

**Where the system is:** the VM now runs `170aa21` — the 14-commit deployment
gap (VM stuck on `bb99555`/23-24 Jul while main accumulated the DH-905
throttle, the Great Purge, the macro heartbeat and the test speedup) was found
by reading the 07-27 CEO brief against `git merge-base` and closed the same
day. Deploy verified live: cron block reinstalled by `setup_cron.sh`, the
manual duplicate lines for `intraday_tracker`/`macro_nightly` deleted
(open item 3 of 07-25 — **now done**, de-dup check returned exactly 2),
both systemd services active, `/api/health` OK.

**Today's two fixes (`170aa21`), both from reading the CEO brief honestly:**

1. **`ops_monitor` empty-list false alarm.** `"failed": []` (macro_nightly's
   clean-run shape) reached the brief as a problem line — the third shape of
   the zero-stat false alarm (07-14 dict-count, 07-20 count-first). Empty
   brackets now scrubbed; a populated failure list still fires.
2. **`firm_mtm` mislabeled account equity as profit.** The card printed
   "realized Rs.244,215" against a Rs.200,000 base — that figure was
   `account.equity` (base + realized). New `realized_pnl` key carries the
   profit alone (+44,215), signed, on the card; `equity_realized` unchanged
   for MTM composition. Suite 1,591 green in ~96s.

**Watch tonight/tomorrow (first post-deploy signals):**
- 19:50 IST `macro_nightly` should fire the Discord heartbeat card for the
  first time ever on the VM (it shipped in this deploy).
- Tomorrow's CEO brief should show: DH-905 lines gone (throttle now live on
  the VM), `realized +44,215` not `Rs.244,215`, no `"failed": []` alarms,
  and — if the intraday capture misses (79-80/84, DIVISLAB/LUPIN/TATASTEEL)
  were the un-throttled-client story — those gone too. If they persist,
  they are a real data issue, not rate-limiting.

**Open items:** unchanged from 07-25 below **except** item 3 (cron de-dup —
done today). `decay_engine.apply_decay_sweep` is still unwired (item 1,
owner-gated). The parked branch `claude/hello-d9m45n` still holds unmerged
commits (item 7).

### 🎯 STRATEGIC BACKLOG — "Intelligence & Autonomy" (owner roadmap, 2026-07-27)

Owner directive, same day as the deploy-gap close. **Binding constraint:
NO NEW ENGINES.** Every item below reuses existing infrastructure; a design
that proposes a new database, engine, or parallel price-math path violates
the directive and must be pushed back on.

**Directive 1 — Opportunity Cost Tracking (REUSE the shadow architecture).**
When a risk gate blocks a trade (exposure gate, margin gate, adaptive-sizing
veto, equity-desk funding rejection), route the rejected proposal into the
EXISTING shadow tracking so we learn whether the gates save or cost money.
What already exists (verified 07-27, do not rebuild):
- `exposure_gate` ledgers every block to `logs/exposure_blocks.jsonl` —
  but nothing resolves what the blocked trade would have done.
- The shadow architecture already does prospective outcome-tracking:
  `shadow_trades` in brain_map (`validation/trial.record_shadow_fire`),
  ONE resolver (`discovery/shadow_runner.resolve_from_outcomes` — no
  parallel price math, runtime-spy tested to never touch journal/portfolio).
- `adaptive_sizing` vetoes and `equity_desk.fund_entry` rejections already
  keep telemetry rows with named reasons.
The build is WIRING: blocked proposals become shadow rows (mode-tagged so
they can never poison the pattern-learning corpus — the `shadow:`/`sim:`
exclusions in `stat_gates` are the guard rail to extend, not bypass), then
a scoreboard read answers "gates: saving or costing?". Forward Scoreboard
(`analysis/strategy_scoreboard`, SB-2) is the reporting pattern to reuse.

**Directive 2 — The CEO-View Discord (plain-English alerts).**
The owner must glance at a phone after 3 days away and understand the
state. Upgrade payload WORDING from raw data to human sentences (e.g.
"Macro regime matches 2018; executing X") — the one-door rule stands:
everything still flows through `notifier.fire_broadcast`, this is a
wording/formatting layer, not a new notification path. Distinct **Morning
Brief** (does not exist today — 08:00 slot alongside `suggest` is the
natural home) and **EOD P&L Summary** (`eod_summary` 15:45 + `ceo_brief`
16:30 already exist — upgrade their language, don't duplicate them).
Macro sentences must respect the ACCUMULATING honesty: no "matches 2018"
claim before the scoreboard actually supports it.

**Directive 3 — The 3-Day Walkaway Protocol (circuit breakers).**
The system must be safe to ignore for 3 days. What already exists
(verified 07-27 in `portfolio_manager` — this is the halt STACK, reuse it):
lifetime risk-of-ruin drawdown halt (10%), daily 3% breaker, silent
margin-exhaustion, all already enforced at the single entry door. The gap
is the COMMUNICATION and OVERRIDE layer: on any hard halt, (a) new entries
stop (already true), (b) open-position exit management continues (verify —
this must be tested, not assumed), (c) ONE `🔴 SYSTEM PAUSED` Discord card
fires with the reason and the resume command, (d) resumption requires an
explicit human override door — not an automatic timer. De-dup via ledger
(the exposure-gate one-card-per-day pattern).

Sequencing and per-directive design docs are future-session work; nothing
above is started as of this writing.

---

## 📍 CURRENT STATE — 2026-07-25, after the CTO hygiene session

**Where the system is:** live and autonomous on the VM, paper money, with the
Macro Regime Engine (Stage A + Stage B) accumulating a forward track record it
does not yet have enough of. The last four days added no trading logic — they
added a forward clock, then cleaned the house.

**Health at handover:** suite **1,589 green in ~83s**; `src/` is 141 modules,
all on a live execution path; 24 VM cron jobs; the nightly macro clock now
reports its own health to Discord every run.

### ⚠️ Open items and known limitations, most important first

1. **`decay_engine.apply_decay_sweep` is UNWIRED — a latent bug, decision
   pending.** Nothing on any cron, systemd service or LaunchAgent calls it, so
   knowledge-graph EDGE decay (`graph_edges.confidence_score`) **has never run
   in production** — while `graph_engine.py` and `entity_affinity.py`
   docstrings both describe it as live. It is NOT a duplicate of `sleep_phase`'s
   semantic-NODE decay; they act on different tables. Found by the 07-25 AST
   audit. Candidate fix: wire it as a step in the 20:00 `sleep_phase` pass.
   Owner has agreed to do this in a future session.
2. **The October clock is young and that is the whole point.** The Macro Engine
   needs a 60-session forward-scored record before anything it says earns
   authority. Until then `strategy_scoreboard` will honestly report
   ACCUMULATING for nearly every cell. Do not tune against early readings; the
   first real Strategy Registry build already returned "no edge at 6–8 analogs
   yet", with placebos ranking alongside the seeds, and that verdict was
   shipped rather than explained away.
3. **VM cron de-duplication is required at the next deploy.** `setup_cron.sh`
   now installs `intraday_tracker` (#23) and `macro_nightly` (#24), which were
   previously added to the VM's crontab by hand. After the next
   `bash scripts/setup_cron.sh`, **remove any manual crontab lines for those
   two** or they will double-run.
4. **Sector history before 2019 is proxy-based, not observed.** Stage A built
   liquidity-weighted basket proxies with an out-of-sample tracking-error
   validation protocol, plus a local-CSV override where Yahoo could not serve a
   name. NSE pre-2019 index history is bot-walled on every scriptable path;
   owner manual download through `ingestion/index_history` is the only route.
   Treat pre-2019 sector numbers as reconstructions with stated error.
5. **The options simulator's P&L is inflated and is not a forecast.** Synthetic
   chains, roughly 10x, in the known 62–79% generosity band. It is proof the
   engine mechanics work. It is not an expected return, and must never be
   reported as one.
6. **`strategy_registry` mostly abstains on rotations** until pre-2019 sector
   CSVs land — the `MIN_EPISODE_LEGS=5` support floor bites where sector
   history starts in 2019-10. This is correct behaviour, not a failure.
7. **The parked branch `claude/hello-d9m45n` (PR #14) still holds unmerged
   commits.** Only the DH-905 throttle fix (`1867335`) was cherry-picked out of
   it on 07-25. Anything else on that branch is un-reviewed and un-landed.
8. **`rss_ingester` classifies nothing on the VM by design** (#75 — it inherits
   the `ollama` backend, which the VM does not run). Its heartbeat means "ran",
   not "produced". Enabling a cloud backend is a cost decision, not a bug fix.

### What changed on 2026-07-25 (the hygiene session)

- **Phase 1 — the Great Purge.** An AST dependency trace from every real
  entrypoint classified all 152 `src/` files; 12 dead modules moved to
  `research_archive/` with their tests. `pytest.ini` keeps archived tests
  uncollected. 16 intentional manual/offline tools now carry a
  `# MANUAL OFFLINE TOOL` / `# TEST INFRA` first line so future sweeps skip
  them. The stray `intelligent-nobel` worktree and branch were deleted.
- **Phase 2 — the heartbeat.** `macro_nightly` fires one Discord card per run:
  `[🟢 FRED: OK | 🟢 Indices: OK | 🟢 Declare: OK | 🟢 Scorer: OK]`. Holiday
  and honest abstention stay green; a cache miss goes red. A Discord outage
  cannot fail the cron.
- **Phase 3 — the speedup.** Suite **14m09s → 1m23s**, all 1,589 tests still
  passing. Three files were reaching real external systems (84 live quote calls
  per test against the production watchlist; real rate-limit sleeps; real
  Ollama inference). `pytest-xdist` was evaluated and **deliberately declined**
  — at 83 seconds the risk of scattering known cross-test state leakage into
  nondeterministic failures outweighs the gain.
- **Phase 4 — documentation.** `README.md` rewritten (it still described
  "Phase 1: alerting" and yfinance), `ARCHITECTURE.md` given an
  agent-orientation preamble, the end-to-end macro data flow, and the testing
  philosophy; `PROJECT_TIMELINE.md` created from git history.

### The fastest way to orient

```bash
python3 -m pytest -q                       # 83s — proves the checkout works
python3 -m src.bug_ledger --report         # what the machine thinks is broken
tail -5 logs/macro_nightly.log             # did the clock tick, and how healthy
```

Then read `ARCHITECTURE.md` top to bottom. It is the map.

---

## ⛔ STEALTH MODE — the operating posture as of 2026-07-23 (owner pivot, READ FIRST)

**We are a PROPRIETARY DESK. No public surface for ≥6 months and NOT
before October 1st.** Gate G2 (public waitlist / landing page /
storefront) is **SHELVED — do not build it.** The engine must earn a
real, forward-tested **60-session Dept-5 scoring track record** before a
storefront is reconsidered.

The three stealth priorities, in order:
1. **The October clock.** `analysis/macro_nightly` on the VM cron (19:50
   IST) is the SOLE heartbeat — its one job is to run *flawlessly* every
   trading day and grow `logs/macro_regime_declarations.jsonl` toward 60
   sessions. Evaluate Oct 1. **Reliability > features.**
2. **Finish Auto-Discovery AD-2→AD-4** — the significance layer
   (block-bootstrap + phase-randomized surrogates + out-of-sample
   rejection), then court wiring, then dual-catalog.
3. **Internal Prop Desk Dashboard** — a weekly private markdown/HTML
   report (shadow P&L, vetoes, newly discovered regimes). NOT public.

**Immediate next build (post doc-review):** the fingerprint CACHE — so
the nightly `declare()` is light enough to never fail on the e2-micro.
Then AD-2. See `docs/cycle_hunter_plan.md` + `docs/auto_discovery_spec.md`.

**⚠️ Honest VM state:** the nightly has ticked the ledger once manually;
the OFFICIAL clock starts once the cache + a VM re-seed (deep lake +
current templates) land — that's the very next step after doc review.
The VM currently runs the earlier (pre-deep-sector) seed.

## ✅ THURSDAY PROTOCOL — CLEARED 2026-07-22 (owner returned early, ordered "start building")

The 2026-07-21 blocking directive (bug-ledger report → analyze → fix,
before anything else) was executed 2026-07-22 night. Verdict: 55 items,
ONE real code bug (intraday_tracker rate-limit bursts → in-sweep retry,
`8e70e97`) + one suite flake (journal-drift test isolation, same
commit); MACPOWER budget-refusal and the corporate_events arg error were
non-bugs. Full triage record: `docs/observation_week_ledger.md`. The
protocol machinery (`python3 -m src.bug_ledger --report` on the VM)
remains THE first read after any future autonomous stretch.

## 📌 2026-07-23 OVERNIGHT SPRINT — the Macro Regime Engine went from spec to first real opinion

- **M1 macro lake LIVE**: `macro_lake` (FRED REST API, `FRED_API_KEY` in
  .env — 46,392 rows: US10Y→1962, USDINR→1973, BRENT→1987, DXY→2006) +
  `indices_lake` (India VIX, NIFTY, 12 sectors from NSE's daily
  all-indices archive; historical walk 2019→today ran overnight).
- **M2 fingerprint engine LIVE** (`macro_fingerprints`): 17 curated
  episodes → banded multivariate DTW → 4 archetypes, first real build in
  `data/macro_templates.json`. **Read
  `docs/macro_clustering_report.md`** — Ukraine clustered with the
  taper tantrum; IL&FS refused every family, correctly.
- **Darlings re-screen on June-2026 quarters DONE** (233 refreshed):
  +CYIENTDLM +SCHAEFFLER / −BOSCHLTD −UNITDSPR, tier table rebuilt.
- **Time Machine complete**: full-market bhavcopy 2019-09-30→today
  (1,765 sessions, 465MB, ₹0).
- Multi-agent worker mode was tried and RETIRED (collisions —
  dev_workflow §3b); single-lane sprint is the operating mode.

## 📌 2026-07-23 DAY SPRINT — Macro Engine COMPLETE (M2.1→M4 + AD-1), then Stealth pivot

- **M2.1** (`e0f29a5` and earlier): core-channel clustering fixed the
  coverage reshuffle; slow-burn horizon class added.
- **M3 playbooks + M4 tracker + `macro_nightly` VM heartbeat** — all
  live, committed, deployed.
- **Deep-history backfill:** owner manually pulled pre-2019 index CSVs
  (NSE bot-walls every automated path); the ROBUST `index_history`
  clerk ingested them — NIFTY 1995→, Pharma 2005→, Auto 2004→, IT
  2002→, Bank 2000→. +4 slow-burn episodes → clustered n=5 by macro era
  (labels overruled by data again). `docs/macro_clustering_report.md`.
- **AD-1 unsupervised discovery FUNCTIONAL** (`analysis/auto_discovery`),
  AD-2→4 scaffolded (`docs/auto_discovery_spec.md`).
- Then **Stealth Mode** (see top). Feature dev PAUSED for this doc sync.

## 📌 2026-07-22 MILESTONES — the build sprint opened (code freeze lifted by owner)

- **Brain-MCP server** (`src/brain_mcp.py`, `7b7faee`): the data
  product's first door — 9 read-only tools over the brain, zero new
  deps, SEBI posture (data-not-advice) enforced BY TEST; repo
  `.mcp.json` = zero-step Claude Code demo. Localhost-only until gate G2.
- **Time Machine backfill**: NSE free archive floor probed = ~Oct 2019;
  `bhavcopy_clerk --backfill 2500` running on the Mac toward ~6.8 years
  of full-market daily bars. ₹0 spent.
- **Master plan + budget**: `docs/cycle_hunter_plan.md` — replay history
  backward / validate forward, proof-gated spend (G1–G5, ₹1L cap),
  Aug-8 Max-window schedule. THE living plan; PLAN.md is history.
- **Workflow**: `docs/dev_workflow.md` — the Speed & Scale protocol
  (scoped tests while iterating, full suite ONLY as the pre-deploy
  gate, zero-tech-debt rule, parallel-lane rules). Binding on every
  session.

---

Read this to pick up the project cold in a new agent session. For vision see
`OVERVIEW.md`, for system flow see `ARCHITECTURE.md`, for the file index see
`MODULES.md`, for why past calls were made see `DECISIONS.md`. **This file is
updated only at milestone states, not on every commit** — check `git log`
for anything more recent than what's written here.

## ⚠️ COVERAGE GAP IN THIS FILE, READ FIRST (noted 2026-07-20)

**This brief jumps from 2026-07-20 (the section directly below) back to
2026-07-11.** The nine days between — honest paper fills #70 + the stale
LOT_SIZES fix, portfolio Greeks #71, performance #72, book_context #73,
text-intelligence #74, RSS ingestion #75, gated nightly discovery #76, the
unified-main deploy, the whole Dept-8 Analysis department, the F&O intake
tranche, the Issue-21 XBRL clerk, and dual-horizon news sentiment — are
**recorded in `DECISIONS.md` (#70–#76), `MODULES.md`, and
`docs/observation_week_ledger.md`, but were never folded into this file.**
For anything in that window, trust `git log --oneline` + those three files
over this brief's silence. Not reconstructed here rather than risk a
plausible-sounding but unverified summary.

## 🟢 THE AUTONOMOUS RUN — ₹2L clean sheet, ₹10k/trade hard cap, set-and-forget (decision #84, 2026-07-21, owner final override)

**The owner stepped away. The firm reboots at Rs.2,00,000** (account
reset: realized 0, peak 2L; the 10L era is archived in the pre-migration
DB backup; open options spreads CARRY and settle into the new base).
Treasury pool is now DERIVED from the account (never a constant);
granularity rescaled (deadband ₹10k / step ₹25k / round ₹5k); equity
budget seeds ₹60k. **Hard cap `max_risk_per_trade_rs`=₹10,000 on BOTH
desks** applied after percentage sizing: equity risk budget min-capped;
options lots capped by max_loss, and a structure whose max_loss/lot
alone exceeds ₹10k is refused. 100% utilization allowed (no idle
buffers; the one cash door is the only brake). Equity sizing: 5% risk /
25% notional per name. Firm halts auto-rescale: daily 3% = ₹6k, ruin
10% = ₹20k trailing. **Set-and-forget:** an unhandled master_scheduler
crash fires a real-time 🚨 page (traceback tail, then re-raises).
**Directive 4 — 5 Discord messages/day (`notifier.budget_gate` at the
one door):** crash ALWAYS pages; scheduled digests (EOD/CEO/tiers +
Saturday cards) spend the budget; the 2-hourly snapshot DROPS; every
other card (trades, rotations, 🧠 sizing, review flags) SPOOLS to
`logs/discord_digest_queue.jsonl` and lands in the next digest's
"📦 Batched signals" field. This supersedes the 07-16 real-time
review-flag rule for the autonomous run. Kill switch
`discord_budget_enabled`.

## 🟢 THE VM-SHIFT — equity desk is VM-NATIVE, one database, LIVE trading (decision #83, 2026-07-21, owner override)

**Owner formally overruled the observe-first hold, accepted wiping the 5
day-old paper positions, and ordered the shift the same day.** The desk
now lives in the VM's ONE firm database: equity notional locks through
the same `pm.request_entry` door as options margin (`eqd:` prefix = the
desk's identity; deployed/realized are views over tagged rows), the
treasury is ONE atomically-updated row (`treasury_state.equity_budget_rs`
— v1's two-phase/reconcile/SSH machinery deleted; VM cron #21 19:50),
and `run_darling_live_cycle` rides the market loop beside the block-leg
shadow: LIVE exits (stop/target/time at real quotes), Strong-Sell
force-exits, mid-session settlements, LIVE entries when a Buy-tier
name's quote sits INSIDE the strict buy zone (`fill_basis:"live"`).
Quote ids: `data/darling_ids.json`, built weekly ON THE MAC from Dhan's
public scrip master (exact-match only, #78), shipped nightly. **The Mac
is analysis-only now** — its 19:15 chain ends by shipping tiers + levels
+ ids; the VM freshness-gates all three (stale tiers = no new entries,
exits always run; stale ids = unmarked, never guessed). Migration:
Mac desk DB + ledger archived (.bak), resolved autopsies MERGED into the
VM ledger (learning survives), the 5 open positions wiped per the owner
("the system will simply re-enter them"), the old `equity_desk_allocation`
reservation released, budget seeded at the routed ₹4,00,000. Report
cards (#82 surfaces) now render the desk LIVE from local state. First
live session: next market open 09:15.

## 🟢 ONE FIRM VIEW — every report card now shows BOTH desks (decision #82, 2026-07-21, first freeze exception)

**Owner ruling ("lift freeze and fix it — one ledger"):** the 12:00 card
showed only options; the equity desk's 5 funded positions were invisible
outside the Mac's 💼 card. Fixed at the VIEW layer — the physical stores
stay separate BY DESIGN (two machines, two write owners; equity rows in
the options journal would break plan_tracker's sweep). After every Mac
19:15 chain: `equity_desk.publish_snapshot()` → scp to the VM
(`firm_treasury.vm_push_file`) → the 2h Portfolio Report Card (full
section + table), the 15:45 EOD summary (💼 field), and the 16:30 CEO
brief (headline line) all render the equity book beside options. Labeled
"EOD marks" always (Mac holds no token); >30h old = "STALE" on the card;
missing = "no snapshot yet"; every seam fail-open. **Freeze resumed
after this deploy.**

## 🟢 THE FIRM TREASURY — the 7L/3L split is now DYNAMIC (decision #80, 2026-07-20 night, owner Directive 1)

**#79's static split lasted about two hours** — the owner ruled it
capital-inefficient (correct: options' stress-adjusted peak margin use is
~₹1.9L) and green-lit dynamic routing with three pre-agreed pushbacks
(nightly cadence not intraday; evidence-bar learning deferred to
Session 2 `adaptive_sizing`; gap-shock down-weighting). `src/firm_treasury.py`:
mechanical regime router (base 30% equity share; tilts for NIFTY trend,
Buy-tier depth, deep value, high VIX, options margin demand; clamp
15–60%, ₹50k deadband, ₹1L/night max step), runs inside the 19:15 EOD
chain between tier grading and the shadow leg. Capital moves =
subscribe/redeem on the desk's `starting_capital` (peak shifts with base
— the ruin halt stays rupee-honest; NOT the originally-planned 10L
re-init, which would have diluted the desk's 10% halt to ~0.3% —
pushback #4, applied during build) mirrored by the VM's
`equity_desk_allocation` lock under the RAISE-FIRST invariant
**E_vm ≥ E_mac**: any mid-move crash idles capital for a night, never
double-spends it; next run reconciles E_vm := E_mac. Unreachable VM =
frozen split, 3rd consecutive night = one warning card. Ledger
`logs/treasury_ledger.jsonl`; kill switch `treasury_enabled`.
**Session 2 ✅ BUILT (decision #81): `adaptive_sizing.py`** — the
autopsy-driven sizing feedback loop is LIVE on both desks (equity
fund_entry risk-budget multiplier; options lots penalty/veto after
size_lots). Break-even-centered priors = 1.0x until each key's own
record earns otherwise; penalties fast (≥4n, floor 0.25x), vetoes
earned (≥8n, Wilson UPPER bound under break-even, telemetry row kept),
boosts slow (≥10n, LOWER bound clear, cap 1.5x inside existing caps),
gap-shocks half-weight, ticker veto overlay (≥5n). Ledger
`logs/sizing_adjustments.jsonl`, one card per key state-change; kill
switch `adaptive_sizing_enabled`; CLI `python3 -m src.adaptive_sizing`.
Also tonight: the desk's FIRST LIVE FUNDED RUN — 5 darling entries,
₹1,77,540 locked at the 19:15 chain.

## 🟢 THE EQUITY DESK — the darling book now trades PAPER CAPITAL (decision #79, 2026-07-20 night)

**Owner ruling ("10,00,000 of paper money only buddy — let's see how
efficiently our system runs the 10 lakhs"), issued AFTER the recorded
pushback; supersedes #77's zero-capital clause for the darling leg only.**
`src/equity_desk.py` (Dept 3): a Rs.3,00,000 slice of the firm's 10L funds
darling Buy-tier entries — 1% risk / 15% notional cap, whole shares,
delivery-friction-net settlement — through portfolio_manager reused
conn-generic against `data/equity_desk.db` (Mac). Same halts (10% ruin,
daily 3%), same silent exhaustion, zero re-implemented risk rules. The VM's
options account carries the matching standing lock `equity_desk_allocation`
(**run once on the VM:** `python3 -m src.equity_desk --reserve-firm-slice`)
so the firm total stays one honest 10L. Funded entries stamp
`mode="PAPER_CAPITAL"`; funding failures keep the telemetry row with the
reason (the learning ledger never loses a line); the proposer's Dept-3
import ban still holds — seams injected only at `patience_basket.eod_chain`.
One Discord card per EOD run, only when money moved. Kill switch
`equity_desk_enabled` (code default OFF). Desk summary:
`python3 -m src.equity_desk`; crash reconciler: `--sweep`. The block-VWAP
leg stays pure telemetry. The desk's equity curve starts at zero history —
judge it like performance.py judges everything: no verdicts on thin samples.

## 🟢 THE DARLING LIFECYCLE IS LIVE — 7-tier grading + the two-clock architecture; both Mac crons INSTALLED (updated 2026-07-20 evening)

**Decision #77, commits `5c326a3` + `1629bc8`, pushed; suite 1373 green.**
The binary RIPE/waiting basket is SCRAPPED. Dept 8 now runs a lifecycle
system: every darling is graded EVERY EOD into one of seven tiers
(`strong_buy` / `weak_buy` / `strong_hold` / `weak_hold` / `weak_sell` /
`strong_sell` / `watch`) plus an honest Tier-0 `ungraded` for names whose
data can't support a grade. A name is never "done" after entry — the same
table that says BUY also says HOLD and SELL for what the paper book holds.

**The two clocks (do not collapse them into one):**
- **DAILY** — `patience_basket --eod`, **Mac cron 19:15 Mon–Fri**: bhavcopy
  → F&O bundle → pricer → valuation → tier grading → shadow leg. Re-grades
  on PRICE, because prices move daily. This half already existed and was
  already dynamic; a weekly-only recalibration would have made it WORSE.
- **WEEKLY** — `weekly_recalibration`, **Mac cron 10:00 Saturday**: refresh
  quarterly filings → re-screen → No-Orphan pins → rebuild → one card.
  Re-judges FUNDAMENTALS, which only change when filings arrive, and
  OVERRIDES the daily grade through pins.

**Mechanical definitions (never re-derive these by feel):** near-zone =
within 5% above the buy-zone ceiling · momentum = close > 50-DMA AND 50-DMA
> 200-DMA · losing volume = 20-day avg turnover < 60-day avg · near-stop =
within 1 ATR of the stop reference (trailing pivot floor first, else the
hard stop).

**The No-Orphan rule:** a held name failing the weekly screen is never
"orphaned" — it is PINNED (`data/darling_pins.json`) into the tier table
until its paper position closes, then drops entirely. A REJECTED name pins
`strong_sell`; a name the screen merely LOST THE DATA to judge pins
`ungraded` — a sell verdict is never manufactured from absence.

**Shadow book wiring:** entries from `strong_buy` + **in-zone** `weak_buy`
only (near-zone names are watched, never chased). `strong_sell` FORCE-EXITS
an open shadow (`fundamental_break` when pinned, `strong_sell_tier` when
valuation-driven); `weak_sell` does NOT — the position's own stop is
already the thesis-break detector. Still zero-capital PAPER_TELEMETRY,
still advisory-only (Law #63).

**Cards:** family transitions ONLY (buy/hold/sell/watch). Intrafamily moves
(strong_buy → weak_buy) are visible in the table but silent — a valuation
wobbling 25→26→25 would otherwise fire three cards in three days. First
grading fires ONE distribution summary.

**First live grading, 105 darlings:** 0 strong_buy (nothing is
simultaneously in-zone AND ≤25 — an honest empty bucket, not a bug) · 15
weak_buy, 10 of them in-zone and entry-eligible (the old RIPE trio
HEROMOTOCO 30 / ESCORTS 34 / TCS 35 all landed here) · 17 strong_hold · 17
weak_hold · 17 weak_sell · 12 strong_sell (9 below their hard stop) · 17
watch · 10 ungraded.

**Where the VM stands:** two commits behind, DELIBERATELY. Everything in
this work is Mac-only by the boundary doctrine (bhavcopy lake, pricer,
valuation all live on the Mac; the crons are NSE-crawling and must never
run from the VM's IP). No VM pull or restart is needed; it syncs at the
next regular deploy.

**Open / next:** `business_metrics`, `liquidity_rank` and `ticker_dossier`
(landed in `1629bc8`) still have NO dedicated test files — Dept-8 test
debt. First cron-fired EOD run is 19:15 on 2026-07-20; first weekly
recalibration is Saturday 2026-07-25 (its filing-refresh stage takes
15–30 min, which is normal).

## 🟢 HOLY-GRAIL PHASES 4 & 5 COMPLETE, MERGED & DEPLOYED — the discovery brain now RUNS; it is DATA-STARVED by design, not broken (updated 2026-07-11 evening)

**The single most important fact for a cold session: the build has reached
its designed resting point.** Phases 0–3 (substrate + confluence + macro
alignment) were already in; this evening Phase 4 (the proving harness) and
Phase 5's entire offline-buildable surface (the miners + the strategy
evidence view) landed on `main` via PR #4 (`41dcc72`) and PR #5
(`3dc7d21`), and both are pulled to the VM. Suite **909 green**, all
offline. **Nothing more should be BUILT until real data accumulates** —
the remaining Phase 5 pieces are data/human-gated (see "next" below) and
building them now would rediscover the entry gates on an empty corpus
(#50's exact failure). The system's job now is to RUN and accumulate.

**What Phase 4 added — the proving harness (`src/validation/`):** every
pattern the brain ever surfaces must first survive this. `stat_gates.py`
(the shared anti-hallucination toolkit — Wilson lower bounds, exact
binomial, structural breakeven nulls, Benjamini-Hochberg, block-permutation
nulls, split-window stability, `promotable`: sim supports but never solely
justifies; BALANCED floors, all config-tunable via `harness_*` keys),
`registry.py` (pattern lifecycle CANDIDATE→TRIAL→VALIDATED→LIVE_ADVISORY /
QUARANTINED / INSUFFICIENT_N / DEAD, frozen-definition idempotency, audited
soft-only transitions), `trial.py` (walk-forward split + 5-day embargo +
`shadow_trades`, never `journal.jsonl`), `monitor.py` (validation-is-a-lease:
CUSUM + Wilson-crossing auto-quarantine, adaptive lease expiry — wired as
Sleep-Phase Task H), `placebo.py` (seeded information-free hypotheses →
realized false-discovery meter), `digest.py` (the owner's weekly Discord
window — **cron #13, Saturday 10:00 IST**; first fire 2026-07-18).

**What Phase 5 added — the discovery brain (`src/discovery/`):** the brain
starts finding its OWN hypotheses instead of only checking hand-coded ones.
Miners ENUMERATE + REGISTER candidates; they never surface anything (the
harness above is the only path to a card). `cooccurrence_miner.py` (Apriori
over resolved-outcome transactions = event tags ∪ market-day `ctx:` tags,
stratified base rates so it can't rediscover the pipeline's gates),
`sequence_miner.py` (the same core on a time axis — `lag{k}:` antecedents
k trading-days BEFORE entry, the "early tell precedes the move" shape H1/H2
turn on; no look-ahead by construction, timelock-proven), `run_miners.py`
(the manual orchestrator — honest combined report), `strategy_evidence.py`
(the "check WHICH structure" view — per-structure Wilson-bounded win-rates,
real/sim never pooled, ≥5-real render floor, descriptive PREFER/ABSTAIN on
the honest lower bound; the read-only substrate the future duel consumes).

**CRITICAL — the miners are MANUAL-ONLY, deliberately not cron-wired.** On
today's empty corpus `./venv/bin/python -m src.discovery.run_miners` reports
`0 survivors — CORRECT` and says why (support floor not met). That is the
designed output, not a failure. Wiring them into the nightly sleep phase
waits until `daily_context` has enough history for an itemset to plausibly
clear the floor — that wiring gets its own DECISIONS row when the data
justifies it.

**Next per HOLY_GRAIL_PLAN §8.6-8.7 (ALL gated — do NOT pre-build):**
counterfactual structure pricing + the champion/challenger **duel** (needs
a VALIDATED pattern + a ≥30-day disagreement floor + the human dethroning
ritual #49 — never auto-applied), and **skeptic v3** (realized-vs-implied
vol / distance-to-support / days_to_results features; the ablation study is
deferred until the layers reach ≥50% non-NULL coverage, else it would
"prove" the new layers earn zero — an artifact the owner would misread).
The trigger to resume building is DATA: the first pattern reaching
VALIDATED, or layer coverage crossing the ablation gate. Until then, watch
the weekly digest and let the substrate fill.

## 🟢 HOLY-GRAIL PHASES 0–1 DONE + BACKFILL RUN; Sat 07:00 renewal VERIFIED; Monday 07-13 09:10 IST is the first live session (updated 2026-07-11 midday)

**Sat 2026-07-11 session, on top of the overnight builds:** the two
overnight PRs are merged and ON THE VM (PR #1 `84fe9c8`: data lake +
Phase 0 capture jobs + Phase 1 backfill CLI/flows/earnings + Phase 2
Evidence Snapshot substrate + provenance firewall; PR #2 `9e85b87`:
knowledge-graph visualizer + Phase-3 macro→sector prior config). VM
crontab now has 12 jobs — the 5 new capture crons (earnings 19:20,
deals 19:30, flows 19:35, perishables 19:45 daily; chains 15:40
Mon–Fri) fire for the FIRST time tonight — check their logs and
`data/lake/` on the VM after 20:00 IST. Suite 805 green (one
weekend-only test bug fixed: heartbeat test pinned to a fixed Monday).

**The Phase 1 moat is REAL now:** the 3-year NSE bulk/block backfill
ran clean from the Mac after fixing three NSE breakages found live
(retired endpoints → `historicalOR`; JSON API silently truncates to
~70 rows → must use `&csv=true`; homepage 403 would have aborted the
daily pull's warm-up — all fixed in `4aac239`, regression-tested,
ledger Issue 11). Result: **75,600 deals / 742 trading days
(2023-07-11 → 2026-07-10, no gap before tonight's first daily pull),
0 failed windows**, raw CSVs archived to the Mac's lake, JSONL shipped
to the VM, and the VM's entity-affinity ingest folded all of it: 16
`concentrates_in` edges across 6 promoter groups, each with its TRUE
historical `valid_from` (as-of projection verified). Sleep-Phase Task
F continues folding daily increments from here.

**Sat 07:00 renewal (Issue 10 watch item (a)): VERIFIED WORKING** —
cron fired, first attempt "Invalid TOTP", retry waited one TOTP window,
minted clean (expiry 07-12T07:00). Remaining watch items: tonight's
first capture-cron firings, Monday's first live session (clean
afternoon past 12:00, auto-approve keeping `/pending` empty).

**Sat afternoon update — PHASE 2 IS COMPLETE, PHASE 3 LANDED TOO (two
sessions in parallel; watch for concurrent agents in this repo!).** A
mobile session's PR #3 (merged 11:28 IST) built §5.1-wiring, §5.2
daily_context, §5.3 receipts + `python3 -m src.explain`, a timelock
harness (`src/validation/timelock.py` + `tests/test_no_lookahead.py` —
THE canonical one; register new discovery functions there), all of
Phase 3 (descriptive alignment line, composition law = decision #63 +
`tests/test_composition_law.py`, engagement tripwire) and Phase 4's
`stat_gates` start. This session then closed the read-side timelock
hole PR #3 missed (concentration from history-as-of-T, decision #64,
proven by a fails-on-old-code test) and built §5.5: the T+1-open
execution-timing contract (`src/execution_timing.py` +
`run_simulation(eod_signal_days=…)`, decision #65 — EOD signals decide
on T, fill at T+1's true open, rows carry signal_day/signal_age_hours/
entry_basis; refusal-never-interpolation). Suite 860 green, VM synced
and services verified after each push.

**Next per HOLY_GRAIL_PLAN §12:** Phase 4 remainder (pattern registry +
lifecycle, walk-forward trial, stability battery, noise-injection
suite — `stat_gates` P4-1 exists), plus the small §5.6 leftovers
(llm_mined confidence cap, nightly no-LLM audit of outcome_derived
edges). Phase 1's gated sibling streams (delivery %, insider/SAST,
shareholding) wait for flows to run clean 2+ weeks. §4.6 gap-playbook
ids (US/GIFT via verified Dhan ids) also still open.

## 🟢 SCRATCHPAD PHASES 1–8 + REFINEMENT — DEPLOYED TO THE VM Fri 2026-07-10 ~21:45 IST AND SMOKE-TESTED; Monday 07-13 09:10 IST is the first live session (updated 2026-07-10 late night)

**The single most important facts for a cold session: the deploy is
DONE — the VM runs `bf9dc77` (everything below), pushed and pulled Fri
2026-07-10 night, markets closed. The full checklist executed and
verified: `PAPER_AUTO_APPROVE=1` live in the VM `.env`, `setup_cron.sh`
re-ran clean (7-job block, 07:00 renewal restored, 2h report card added),
root's interim `30 6,18` renewal crontab REMOVED ("no crontab for root" —
the single-07:00 cadence of `docs/token_renewal_cadence.md` is now
reality), all 3 services active, regime backfill 366/366. Smoke-tested
live: a manual retry-hardened `renew_token` run minted a real token
(expiry 07-11T21:47), `get_live_price` works on it, the gateway kept
serving keyed requests after the mint with no restart, `/dashboard` is
200 through the tunnel from outside (401 without key). Ledger Issue 10
carries the full verified record. Remaining watch items: Sat 07:00 first
cron-fired renewal on new code, Monday's first live session (clean
afternoon past 12:00). NOT yet done (optional, Mac-side): evolution
LaunchAgent install (`bash scripts/install_evolution_agent.sh`) and the
`pull_snapshot_from_vm.sh` sync. Never restart VM services mid-session
(09:15–15:30 IST) still stands.** Suite went
486 → **710 green**, all offline; the full diff passed an 8-angle
multi-agent review (27 candidates → 10 verified findings → all fixed,
commit `1794ef4`). What landed, by phase:

1. **Self-healing token + Dhan hardening** — `src/token_provider.py` (live
   .env re-read; Issue 5 fix) wired into `dhan_client._get_client`;
   `renew_token` retries "Invalid TOTP" in the next TOTP window (Issue 10);
   `setup_cron.sh` refuses non-IST hosts (Issue 1) and warns on duplicate
   renewal crons; `src/dhan_guard.py` `SafeDhanClient` (classified DH-9xx
   errors, audit trail); in-place double-nest fixes for
   `get_daily_ohlc`/`get_quote`; single-renewal-cadence decision doc at
   `docs/token_renewal_cadence.md` (root cron removal = deploy-day step).
2. **Visibility + cooldown persistence** — `src/positions.py` +
   `python3 -m src.view_positions` (read-only open-positions table);
   gateway `GET /api/discord/positions` + bot `/positions` embed; journal
   entries stamp `created_at` (IST) and `CooldownRegistry.seed_from_journal`
   rebuilds cooldowns across restarts (Issue 8 fix); `/analyze`'s lying
   "Yahoo Finance" string fixed (Issue 7); Ollama-offline logged once
   quietly (Issue 4); edge miner `extractor_ready()` end-to-end probe —
   no more "ok" from a dead extractor (Issue 9).
3. **MFE/MAE expectancy surface** — `src/calibration/mfe_mae_analyzer.py`
   (spec §3.1/§3.2): journal + simulated_trades sources (read-only
   `mode=ro`), one bar-fetch per ticker via SafeDhanClient, winner-based
   Apex TP/SL suggestion with a 20-trade abstention floor; advisory only.
   First real run needs a valid token (VM, post-deploy).
4. **Auto-approve gate + report card** — `PAPER_AUTO_APPROVE` env switch
   (**default OFF**; when on, headless proposals approve through the same
   `decide_pending` path a human tap takes — decision #53);
   `src/portfolio_report.py` 2-hourly read-only Discord book snapshot
   (cron `0 */2`, self-gates to market hours).
5. **Threat mitigation** — `dhan_guard` freshness guard (`StaleDataError`
   when a 200-OK quote/chain is >60s old mid-session; off-hours and
   untimestamped payloads pass); evolution anti-overfitting guards
   (30-trade corpus floor + split-window stability → new verdict
   `unstable_out_of_sample`); evolution scheduling moved OFF the VM cron
   to a Mac LaunchAgent (`scripts/com.alphatrading.evolution.plist` +
   `install_evolution_agent.sh`, Sat 02:00, pinned interpreter) — **not
   yet loaded into launchd**, run the installer to activate.
6. **Event-driven web dashboard** — `src/web/static/dashboard.html`
   (single-file, SSE-driven, deliberately no polling) + `GET /dashboard`,
   `GET /api/web/positions`, `GET /api/web/events` on `src/api.py`.
   Behind the gateway it authenticates via `?api_key=` on the page URL
   (EventSource can't send headers — refinement fix #1).
7. **Semantic resonance & macro horizon matrix** (`d4df8cc`) —
   `src/ingestion/macro_tracker.py` (Crude/Gold-India/Gold-World/USDINR →
   SHORT/MEDIUM/LONG matrix; verified-ids-only Dhan path, fail-open to
   `data/macro_snapshot.json`, index-impact weights), `src/ingestion/
   news_parser.py` (local-Ollama headline → strict 5-key signal frame),
   `src/knowledge_graph/resonance.py` (CONFLICT/RESONANCE/NEUTRAL
   advisories vs open positions, strike/expiry-roll suggestions,
   brain_map strictly mode=ro). All advisory, zero writes to live state.
8. **Engine-published market snapshot** (`0ebd736`) — the live loop
   publishes spots + every position's mark to `data/market_snapshot.json`
   each cycle (`src/market_snapshot.py`); `portfolio_report.
   get_live_marks()` is THE shared mark ladder (snapshot first — zero
   Dhan calls — direct fetch only for uncovered positions), consumed by
   the dashboard AND the 2h report card. Makes the engine the single Dhan
   quote consumer (decision #48 architecture); `scripts/
   pull_snapshot_from_vm.sh` syncs it to the Mac post-deploy.
9. **Refinement pass** (`1794ef4`, Fri evening) — all 10 verified review
   findings fixed: dashboard gateway auth, equity-mark starvation,
   freshness guard scoped to indexes (+ implausible-age escape), honest
   `release_entry` after commit, ragged-payload tolerance, single-sourced
   open-position predicates, auto-approve never on injected books, shared
   mark ladder, one `unwrap_payload`, resonance graph-query memoization.
   Also that afternoon (VM config hotfix, ledger Issue 10 UPDATE): root's
   renewal cron rescheduled to 06:30/18:30 IST and the 07:00 user renewal
   DISABLED — a 12:00 IST mint had blinded the live loop all afternoon
   (stale in-memory token; the deployed code can't re-read `.env`).

**Weekend deploy checklist (user-approved timeline, target Sun 07-12,
live Mon 07-13 09:10 IST):** push the 12 commits → VM `git pull` +
`pip install -r requirements.txt` + restart services (markets closed all
weekend, restart freely) → **token endgame, order matters** (per the
INTERIM STATE note in `docs/token_renewal_cadence.md`): the retry-hardened
`renew_token` is now deployed, so re-enable the 07:00 user renewal
(uncomment the crontab line tagged `#DISABLED-2026-07-10-hotfix`) and
THEN remove root's interim `30 6,18` cron (backups:
`~/root_crontab.bak-20260710-152339`, `~/user_crontab.bak-20260710-152339`)
→ re-run `scripts/setup_cron.sh` (adds the report card; asserts IST) →
restart the Discord bot (`/positions` registers) → verify the dashboard
through the gateway at `/dashboard?api_key=<API_KEY>` (query-param auth is
how the SSE stream authenticates) → optionally `bash
scripts/install_evolution_agent.sh` on the Mac and set up
`scripts/pull_snapshot_from_vm.sh` for Mac-side live marks → watch
Sunday 18:30 (or next) renewal run on new code + Monday's first session,
especially past 12:00 (the old blinding hour — closes ledger Issue 10).
**USER DECISION 2026-07-10: set `PAPER_AUTO_APPROVE=1` in the VM's `.env`
at deploy** (the switch means nothing on the Mac — the VM is the engine,
decision #47). Consequence to expect: proposals auto-journal as APPROVED
and the `/pending` queue stays empty by design; the human role shifts
from Approve/Reject to monitoring, and the margin gate + persisted
cooldown (Phase 1/2) become the only brakes. **UPDATE 2026-07-13: the
flagged concentration/duplicate-exposure gap is CLOSED** — decision #68's
`src/exposure_gate.py` now blocks a second open spread on the same
underlying+direction at proposal time (before margin lock), after the
book was observed carrying NINE near-identical bear put spreads; a
trend-flip exit advisory rides the live loop alongside it. Flip it back off by deleting the line and
restarting `alpha-trading` — it is re-read per call, no code change.

### Merge Protocol — "Unified Main" (standing Saturday-deploy rule, added 2026-07-16)

No fragmented branches during a production push. Before the Fable
Pre-Review and before any VM deployment:

1. **Unified Main.** Every open **backend** side branch — including the
   `market_loop` test-fix branch — is merged into `main` first, so the
   thing that gets reviewed and deployed is one consolidated tree.
   **Carve-out (non-negotiable, per [[project_branch_strategy]] and
   [[project_lovable_terminal_ui]]):** the UI branches — `lovable-ui`
   and the `Trading Terminal/` frontend — are NOT "side branches" for
   this purpose and are NEVER merged into `main`. `main` stays the
   framework-free Python backend; the React UI keeps its own branch.
2. **Unified Test Run.** Post-merge, run the FULL suite locally against
   the consolidated `main` (`python3 -m pytest`). Deploy only on 100%
   green — a red or skipped test on unified `main` blocks the push.
3. **VM Deployment from unified `main` only.** The live-VM deploy
   (`git pull` + restart, per the checklist above) happens strictly from
   this merged, fully tested `main` — never from a side branch and never
   with a fix branch still outstanding.

Order for a Saturday deploy: **merge backend branches → unified test run
(100% green) → Fable Pre-Review → VM deploy.**

## ✅ Regime-Aware Memory — BUILT AND TESTED; skeptic hypothesis honestly NOT confirmed (2026-07-09)

Roadmap item #4. Every trade the learning stack remembers now carries the
market conditions it was born under — `src/regime.py` is the vocabulary
(trend = the proposer's own market_view read; vix_band = low <13 / mid
13–16 / high >16, now the SINGLE source the planner's IV matrix and the
evolution miner share):

- **Capture:** `to_journal_entry` and the simulator's `_entry_for` attach
  `entry["regime"]` at creation (additive key; old entries tolerate).
- **Storage:** `outcomes.regime_trend/regime_vix` (in-place ALTER on
  connect, post_mortem pattern) + the same columns on `simulated_trades`
  (idempotent ALTER in ensure_schema). NULL on pre-feature rows — never
  guessed.
- **Backfill:** `python3 -m src.regime backfill --db <path>` recomputes
  trend AS-OF each historical trade's proposal date from the bars cache
  (the simulator's own no-future-data discipline); vix_band from the
  row's stored vix.
- **Query:** `brain_map.query_similar_events(tags, regime=...)` adds an
  `in_regime` stats block (count/win_rate/avg_r + tag) alongside the
  untouched overall stats — fully backward compatible.
- **Skeptic contract v2:** FEATURE_NAMES += regime_trend/regime_vix_band
  (contract change = retrain by design, decision #44; no model had ever
  shipped, so nothing was invalidated).

**The experiment (the reason this was prioritized):** backfilled all
1,008 scratch trades (2015–2026, zero unknown trends) and retrained.
Result: 5-fold balanced accuracy **0.578 vs 0.594 pre-regime — no
improvement, within noise**. Why, per feature importances: raw `vix`
(0.26) already contains the band (a coarsening of it, 0.027), and the
simulator proposes structures MATCHED to the trend, so trend is nearly
constant within a strategy (0.027). The "regime tags will ship the
skeptic" hypothesis is NOT confirmed for these coarse tags. Gate stays
closed; skeptic keeps abstaining. Next candidates for the 0.60 gate:
features orthogonal to the entry gates (realized vol vs implied,
distance-to-support, day-of-week/expiry-proximity) rather than
re-encodings of inputs the pipeline already filters on.

NOT deployed to the VM yet (observation week): migrations are additive
and auto-apply on the next `git pull` + restart; the production DB's 366
rows backfill with the same CLI when that happens. Tests:
`tests/test_regime.py` (11, offline); suite 521 green.

## ✅ Procedural Evolution — BUILT AND TESTED; NOT YET SCHEDULED (2026-07-09)

`src/evolution.py` closes roadmap item #5: the system studies its own loss
clusters and proposes rule mutations for HUMAN review — it can never apply
anything itself. Pipeline per cluster: mine losses by (underlying ×
strategy × VIX band) with journal_ref provenance → deterministic HER-style
hindsight buckets (bad_risk_parameters / bad_timing / ambiguous) →
counterfactual contrast against the same setup's wins → an Analyst→Critic
→resolution dialectic on LOCAL Ollama (every reply strict-JSON-gated;
unresolved critic BLOCK kills the candidate) → the proposal must come from
the whitelisted `EVOLVABLE_PARAMETERS` registry (VIX gate, risk %, OTM %,
profit-take fraction, pre-expiry days; bounds-checked — the 3B model never
writes code; diffs are generated deterministically) → double backtest via
the Phase 7 simulator (baseline vs `override_parameters`, in-memory DBs,
cached bars) with **RevertOnRegression**: a cluster-fix that degrades
global Sharpe/max-drawdown is discarded. Survivors:
`candidates/evolution_<ts>.md` (4 sections: cluster, dialectic summary,
simulator proof table, unified diff) + a version-tree entry in
`data/evolution_lineage.json` (v1→v2 per parameter; failed attempts are
remembered so future runs know what was tried).

Runs Mac-side only (Ollama; zero API spend — user rule). Backtest bars
come from `data/bars_cache.json`, refreshed THROUGH the VM
(`python3 -m src.evolution --refresh-bars-cache`) since the Mac holds no
live token (decision #48). Wired as sleep-phase Task E with the standard
graceful skip (the VM skips it silently). **Deliberately NOT on any
schedule until the observation-week triage clears it** — run manually.

First live run (2026-07-09): mined 10 real clusters; the worst (13
Bank-Nifty condor losses in the mid-VIX band, Rs.-8.1L) produced an
Analyst proposal that the Critic BLOCKED at the consensus gate — the
adversarial design doing its job. Bug found & fixed during the build:
multi-line python shipped via ssh `--command` gets newline-mangled — both
evolution's bars dump AND the edge miner's apply step now travel as scp'd
FILES (the miner's flaw had never fired: its only prior run had 0 new
edges). Also fixed: a queue-built notifier test hardcoding "today" broke
the suite at midnight. Tests: `tests/test_evolution.py` (14, offline,
scripted fake LLM); suite 500 green.

## ⚠️ Correction (2026-07-09, just after midnight): Mac renew/push crons REMOVED — they raced the VM's token

Discovered by accident: DhanHQ allows only ONE active access token per
client ID — minting a new one silently invalidates the previous token,
even one whose own expiry claim is hours from now. The Mac's 07:00
renewal + 07:10 push (added a few hours earlier the same night as
"deliberate redundancy") meant that on ANY morning where the VM's own
07:00 Secret-Manager renewal happened to land a moment before the Mac's,
the Mac's 07:10 push would overwrite the VM's fresh, valid token with
the Mac's own (now-invalidated-by-the-VM) token — breaking the live
engine's market data for the whole day. **Fixed**: both Mac cron entries
removed. The VM's Secret-Manager renewal is proven reliable on its own
(verified twice); it needs no backup, and the "backup" was actually the
risk. `scripts/push_token_to_vm.sh` stays in the repo as a manual/dev
tool only — never on an automatic schedule again. Decision #48.

## ✅ THE VM IS THE ENGINE — full migration, LIVE AND VERIFIED (2026-07-08 night)

The Mac is no longer required for anything market-hours. Topology
(decision #47):

| Concern | Where | How |
|---|---|---|
| Live session 09:15–15:30 | VM | `src.master_scheduler`, cron 09:10 Mon-Fri |
| Token renewal | VM, 07:00 | `src.renew_token` — V2 creds fetched at runtime from **GCP Secret Manager** (verified live: mints with ZERO V2 keys on VM disk) |
| Paper state (journal/portfolio/brain_map) | VM `data/` | Mac's live state migrated 2026-07-08; VM authoritative |
| Alerts 15:35 / suggestions 08:00 / sleep-phase decay 20:00 / ops sweep 20:30 | VM cron | `scripts/setup_cron.sh` (6 jobs, CRON_TZ=Asia/Kolkata) |
| API gateway + Discord bot + tunnel | VM (unchanged) | systemd, all `Restart=always` |
| Causal edge mining (Ollama, no API spend) | **Mac, opportunistic** | `src/edge_miner.py` via LaunchAgent (login + 21:00): pull VM brain_map → mine locally → apply idempotent edges back → refresh Mac's read copies |
| chat_agent, development | Mac | reads the miner-refreshed local copies |

Key facts for a cold pickup:
- The VM's OAuth **scopes** were upgraded to `cloud-platform` (required a
  stop/start 2026-07-08) — without that, Secret Manager answers 403 even
  with correct IAM. Secrets `dhan-pin`/`dhan-totp-secret`/`dhan-api-key`/
  `dhan-api-secret` live in Secret Manager, granted per-secret to the VM's
  default service account.
- The old `alpha-market-loop.service` is **disabled** (stale pre-6E code);
  the scheduler cron replaced it. Do not re-enable.
- The Mac's crontab retains renew_token 07:00 + push_token_to_vm 07:10 as
  DELIBERATE redundancy: when the Mac is awake it refreshes the VM's token
  too (harmless either order); when asleep, the VM self-renews. Remove any
  time with `crontab -e` if unwanted.
- The Mac's pre-migration state is archived at `data/mac-archive-pre-vm/`
  (created by the miner's first run) and the VM had NO prior data (its
  market loop never journaled — dead token since creation).
- If the Mac stays closed for a week: everything runs except NEW causal
  edges (graph still decays nightly on the VM). Nothing breaks.

## ✅ Phase 7A: Master Scheduler & Live Execution Loop — BUILT AND TESTED (2026-07-08)

`src/master_scheduler.py` (`python3 -m src.master_scheduler`) is the
one-command entry point for a fully automated live paper-trading day.
**Deliberately NOT `src/main.py`** — that name is the Phase 1 alert job the
VM cron runs at 15:35 IST; clobbering it would have silently killed the
alert pipeline.

`run_trading_session()` runs strictly Mon-Fri 09:15–15:30 IST: launched
early it sleeps until the open; launched after the close it exits
immediately (cron-misfire safe); at 15:30 it shuts itself down. During the
window it supervises the two existing live loops as asyncio tasks — ENTRY
(`market_loop.run_market_loop` fed by the Phase 6H live adapter → margin-
gated, PENDING_APPROVAL proposals; decision #11's human-in-the-loop stands,
nothing is auto-approved) and EXIT (`live_bridge.run_live_loop` advisory
profit-take/pre-expiry alerts). Session bookends go to Discord: the 🟢 OPEN
card carries the Phase 6G account snapshot + the Phase 6I planner's
advisory playbook per underlying; the 🔴 CLOSE card the end-of-day account.
Graceful shutdown: SIGINT/SIGTERM set an asyncio.Event, both loops are
cancelled and awaited; state cannot corrupt because every httpx client and
SQLite touch in this codebase is per-call scoped (open-commit-close) — no
long-lived handles exist to strand mid-write. A dying loop brings the
session down safely (never a zombie). `CRON_SETUP.md` (project root)
documents the exact Mac crontab line (09:10 Mon-Fri + Full-Disk-Access and
wake-schedule caveats). Tests: `tests/test_master_scheduler.py` (8 offline
tests with a hand-wound IST clock; suite 463 green). Decision #45.

## 🟡 Phase 7b: Skeptic Trainer — BUILT AND TESTED; MODEL DELIBERATELY NOT SHIPPED (2026-07-08)

`src/train_skeptic.py` (`python3 -m src.train_skeptic [--dry-run|--force]`)
fits the Phase 11 skeptic's Random Forest on `simulated_trades` in the
frozen `FEATURE_NAMES` order (graph slots honestly zero for simulated rows
— the simulator never consults the graph, so backfilling them would be
look-ahead leakage), evaluates on a stratified 25% holdout, and persists
`data/skeptic_model.pkl` + `skeptic_model_meta.json` ONLY above a
`MIN_BALANCED_ACCURACY = 0.60` ship gate (decision #44).

**The honest outcome so far**: the training corpus was grown from 82
VIX-less rows to **366 resolved simulated trades with true VIX** (290 wins
/ 76 losses; NIFTY 50 + NIFTY BANK, 2023-01 → 2026-06) — the simulator CLI
now fetches India VIX history natively (`_fetch_vix_series`, `--no-vix` to
skip) and the 82 legacy NULL-VIX rows were backfilled from real history.
Even so, the forest scores **~0.55 five-fold balanced accuracy — a coin
flip**: the 10 frozen features don't separate wins from losses for
structures that already passed the pipeline's own gates. So the trainer
correctly REFUSES to persist, and the skeptic keeps abstaining (its
designed no-noise behavior). To go live the model needs richer signal:
regime-aware features (pending "Regime-Aware Memory" phase), real graph
context at simulation time, or a feature-contract revision (which means
retraining by design).

## ✅ Phase 6J: Strict Portfolio Realism — BUILT AND TESTED (2026-07-08)

A four-part hardening pass tying the 6G–6I layers into enforced real-world
boundaries (committed as one unit; the user's spec called it "Phase 6H" but
that letter was already the live bridge):

1. **Test-environment webhook muzzle** (`src/notifier.py`) —
   `webhooks_muzzled()` blocks EVERY Discord webhook HTTP request (text path
   `send_discord_message` AND embed path `broadcast_alert`) when
   `IS_TEST_ENV` is truthy or a pytest run is detected
   (`PYTEST_CURRENT_TEST`); muzzled sends are logged locally and report
   False. Webhooks only fire from true live runs. Tests that exercise the
   dispatch machinery itself set `notifier.WEBHOOK_MUZZLE_OVERRIDE = False`
   (autouse fixture in `tests/test_notifier.py`). The simulator needs no
   muzzle — it is source-guarded against importing notifier at all.
2. **Margin gate at trade ACCEPTANCE** (`options_proposer.decide_pending`) —
   approving a pending entry now requests its margin
   (`spread.margin.total_margin × lots`) from the Phase 6G capital layer
   first (idempotent when the headless gate already locked it at proposal
   time). A margin-blocked approval returns a new
   `{"status": "margin_blocked"}` and leaves the entry pending — nothing
   journaled, broadcast, or settled. With the existing run_headless gate,
   every acceptance path now bounds concurrent trades by the Rs.10L pool.
3. **Theoretical plan economics** (`trade_planner.estimate_plan_economics`)
   — every tradeable plan now carries leg premiums (modeled via the
   simulator's synthetic chain — same world the tracker/replay price in),
   `net_credit`/`net_debit`, `spread_width`, and per-lot `max_profit`/
   `max_loss`, so no broadcast can ever show Rs.0 placeholders. Credit
   structures: profit = credit, loss = width − credit; debit structures
   mirror it; identities are test-asserted.
4. **Portfolio snapshot command** (`src/chat_agent.py`) — `@ADiTrader
   portfolio` (exact match after mention-strip) bypasses Ollama entirely:
   `build_portfolio_snapshot()` formats the live Phase 6G account as hard
   numbers — Starting Capital, Free Cash, Locked Margin, Active Trades
   (= active margin locks), Net PnL. Money numbers are never paraphrased
   by an LLM.

Tests: +6 muzzle tests (network-tripwired), +1 decide_pending gate test,
+6 planner economics tests, +4 chat-agent snapshot tests. Suite 443 green.
Decision #43 in `DECISIONS.md`.

## ✅ Phase 6I: Technical-to-Options Strategy Planner (trade_planner) — BUILT AND TESTED (2026-07-08)

`src/trade_planner.py` is a PURE evaluation matrix from a technical market
read to the appropriate defined-risk options structure — zero side effects
(no market data, DB, journal, or network; import-guard tested), fully
deterministic. `map_technical_to_strategy(technical_state)` ingests trend
(explicit, or classified from spot's % distance to the fast/slow SMAs — ±2%
on the slow SMA marks "strong", the fast SMA must agree in sign), IV regime
(explicit, or from VIX: <13 low, 13–16 high, >16 extreme), and optional
support/resistance boundaries. The routing matrix:

- **Range-Bound + High IV → Iron Condor** — shorts at 2% OTM (or tucked
  under support / over resistance when boundaries are supplied), wings
  `WING_STEPS × step` further out. "High" means rich-but-tradeable: above
  VIX 16 the planner returns no_trade, NEVER contradicting the existing
  `strategy.validate_regime` hard gate.
- **Strong Bullish + Low IV → Bull Call Spread** (ATM + wing; rich IV is a
  deliberate no_trade — debit structures want cheap options).
- **Bearish + High IV → Bear Call Spread** (credit sold above resistance);
  **Bearish + Low IV → Bear Put Spread** (the proposer's own structure).
- Everything else (weak bullish, unknowns, panic VIX) → no_trade with a
  rationale.

Output legs are structural specs — side, CE/PE, concrete strike AND offset
from ATM, snapped to the underlying's grid, optimized for Bank Nifty (step
100, lot 35; NIFTY 50 gets 50/75) — consistent with options_proposer's own
geometry so a planned condor is the same condor the headless pipeline
builds. Tests: `tests/test_trade_planner.py` (21 offline tests: full matrix,
classifier boundaries, strike snapping, S/R overrides, purity + import
guard; suite 426 green).

## ✅ Phase 6H: Live Market-Hour Data Adapter (live_bridge) — BUILT AND TESTED (2026-07-08)

`src/live_bridge.py` decouples the pipeline from daily-close replay during
NSE market hours (Mon-Fri 09:15-15:30 IST), via the verified DhanHQ V2
token framework. Two real-time jobs:

- **Entry** — `fetch_live_market_state(underlying)` is a drop-in for
  `market_loop.fetch_market_state` (the loop's documented `fetch_fn=`
  injection seam): it appends the live spot as today's provisional close
  before the same SMA/RSI read the simulator replays
  (`simulator.analysis_from_closes`), so the trend read reacts intraday.
  Same contract: `{"analysis", "vix"}` (+ `"vol_overrides"` from the Phase
  6F bridge), None outside market hours / dead quote / thin history.
- **Exit** — `evaluate_open_positions()` marks every ACTIVE approved open
  spread in the journal against live spots using `plan_tracker`'s own pure
  helpers (`_spread_mark`, the no-arbitrage clamp, the 65% profit take, the
  pre-expiry gamma rule) and returns advisory exit signals hours before the
  tracker's end-of-day sweep. `live_cycle()` snapshots each underlying,
  folds packets into 15-minute `CandleAggregator` OHLC buckets, and fires
  ONE de-duplicated Discord note per (position, signal) via `AlertRegistry`.

Hard sandbox rule (decision #41): the module is READ-ONLY on all trade
state — it never writes journal.jsonl, never settles cash
(`_settle_spread_cash` stays the tracker's exclusive job), never touches
portfolio.json; a live exit signal is an alert to the human, not an
execution (runtime-spy tested). Daemon: `python3 -m src.live_bridge`
(60s cycles, fail-safe — a dead quote feed or Discord outage never kills
the loop). Tests: `tests/test_live_bridge.py` (19 offline packet-playback
tests; suite 405 green).

## ✅ Phase 6G: Capital & Margin Allocation Layer — BUILT AND TESTED (2026-07-08)

`src/portfolio_manager.py` gives the automated options pipeline a dedicated
account profile: a simulated pool of Rs.10,00,000 starting capital living in
`brain_map.db` (four additive tables owned by the module: `account_state`,
`margin_locks`, `equity_curve`, `account_events` — core tables untouched,
same pattern as the simulator's `simulated_trades`). Three strict guards:

- **Margin locking** — when the headless proposer fires an entry signal, the
  structure's SPAN margin (`portfolio.calculate_span_margin` total × lots) is
  digitally locked under the entry's journal `short_id` BEFORE the proposal
  goes out. Locks release when the tracker resolves the trade (realized P&L
  settles into the account) or the human rejects it (zero P&L).
- **Margin exhaustion** — an entry needing more margin than the available
  liquid cash (equity − active locks) is SILENTLY rejected: no journal line,
  no Discord alert, just a `margin_exhaustion` row in `account_events`.
- **Risk of ruin** — the account tracks its equity curve and trailing
  drawdown from a ratcheting peak; once drawdown ≥ the hard-coded 10%
  (`MAX_DRAWDOWN_PCT`), ALL entries are blocked (`risk_of_ruin_halt` logged),
  however affordable, until equity recovers above the line.

Scope rule (decision #40): the gate applies ONLY when `run_headless` trades
the real paper book — a caller-injected `book` (the Phase 7 simulator, every
test, any what-if run) is its own capital world and neither consults nor
touches the real account. The paper cash flow itself is unchanged
(`plan_tracker._settle_spread_cash` still net-settles `portfolio.json`);
margin here is *virtually* blocked, like a real clearing house blocks SPAN.
Fail-safe at the seams: the proposer/tracker call `gate_headless_entry` /
`release_entry`, which never raise — a dead DB prints a note and fails OPEN.
Inspect the account: `python3 -m src.portfolio_manager`. Tests:
`tests/test_portfolio.py` (Phase 6G section — 16 new tests, in-memory DB,
margin boundaries, consecutive-loss drawdown scenarios, halt behavior,
`run_headless` gate integration; suite 386 green).

## ✅ Broadcast Alert Engine + EOD Summary — BUILT AND TESTED (2026-07-08)

`src/notifier.py` gains two new exports:

* **`broadcast_alert(payload: dict)` (async)** — posts a colour-coded Discord
  embed card directly to `DISCORD_WEBHOOK_URL` via httpx using Discord's
  `{"embeds": [...]}` API (not the existing `{"content": "..."}` text path).
  Colour scheme: green = opened/win, orange = closed-neutral, red = stop_loss/loss,
  blue = EOD. Fail-safe: missing webhook, any network error, or httpx absent all
  return False without raising.

* **`fire_broadcast(payload: dict)` (sync bridge)** — dispatches
  `broadcast_alert` from sync calling contexts. Detects whether an event loop is
  running (`asyncio.get_running_loop()`): if yes, schedules a fire-and-forget
  `Task`; if no, calls `asyncio.run()`. Never raises — the trade journal is never
  blocked by a Discord outage.

**Wired into the execution loop at three points:**
- `plan_tracker.run_tracker()` — embed on every equity and spread resolution
  (`"closed"` event for profit-take/pre-expiry/target/time-stop; `"stop_loss"`
  for stop_hit). All inside try/except — existing journal write never blocked.
- `options_proposer.run_session()` — embed when the user types `y` in the
  terminal session (the `"opened"` event fires after `journal.log`).
- `options_proposer.decide_pending()` — embed when the Discord/API bridge or
  `--review-pending` approves a pending entry (same `"opened"` event).

**`src/eod_summary.py`** — new standalone daily broadcaster (run at 15:30 IST /
10:00 UTC): queries `data/journal.jsonl` (today's resolved P&L, active approved
positions) and `data/brain_map.db` (outcomes win/loss count), computes
strategy-level net delta exposure across open spreads, and posts a terse embed
status card via `broadcast_alert`. Run manually: `python3 -m src.eod_summary`.

Cron schedule on VM:
```
0 10 * * 1-5  cd /home/aditya/alpha_trading && \
              ./venv/bin/python3 -m src.eod_summary
```

**Tests**: `tests/test_notifier.py` — 53 new offline tests (pytest-mock
`mocker` fixture, no network). Suite: 317 → 370 tests, all green.
`pytest-mock` added to `requirements.txt`. Decision #39 in `DECISIONS.md`.

## ✅ RESOLVED AND VERIFIED LIVE (2026-07-08): DhanHQ V2 auth refactor

**Fully closed, not just fixed-in-code — confirmed against Dhan's live
API on the Mac.** `src/renew_token.py` is V2-FIRST: with `DHAN_CLIENT_ID`
+ `DHAN_PIN` + `DHAN_TOTP_SECRET` (+ `DHAN_API_KEY`/`DHAN_API_SECRET` app
headers) in `.env`, it computes the current TOTP via `pyotp` and POSTs
`auth.dhan.co/app/generateAccessToken` — minting a **brand-new 24h token
headlessly**, even from a fully dead old token (the exact failure that
forced a manual dashboard paste on 2026-07-07). Without those keys it
falls back to the DEPRECATED legacy `/v2/RenewToken` — that path is what
broke with `DH-905` after DhanHQ's 2025-10-01 auth overhaul. Sources:
[the change notice](https://github.com/marketcalls/openalgo/issues/488),
[DhanHQ v2 auth docs](https://dhanhq.co/docs/v2/authentication/).
`pyotp` added to `requirements.txt`; offline tests in
`tests/test_renew_token.py`.

**Live verification (2026-07-08, Mac)**: after the one-time Dhan-web setup
(API key + secret via the developer console's "API Key" tab; TOTP 2FA
enabled with the plain-text secret captured during enrollment — NOT the
account's general login settings, and NOT re-viewable after the fact, so
disable/re-enable was needed once to see it) and populating `.env`,
`python3 -m src.renew_token` printed **"Token renewed successfully. New
expiry: 2026-07-09T12:24:11"** — a genuine fresh token from Dhan's live
API, headlessly, with no deprecation note. **Phase 7b is now unblocked
for real**: large simulator runs no longer risk the token dying mid-run.

**Still to do**: replicate the same four `.env` keys on the **VM**
(`git pull` + `pip install -r requirements.txt` for `pyotp`, then the
same base64 `.env` transfer trick since these values would otherwise
mangle in the browser SSH terminal) so its 07:00 IST cron renewal also
uses V2 instead of the legacy fallback.

## ✅ Phase 6F: Quantitative Execution Bridge (vol_bridge) — BUILT AND TESTED (2026-07-08)

`src/vol_bridge.py` is a stateless routing module that reads the active
`graph_edges` from `brain_map.db`, computes a signed net-weight signal
(`_net_signal` = Σ polarity × confidence_score over active edges where
polarity is −1/+1/0 from the target node's keywords), and classifies the
macro regime:

- **Expansion** (`net_signal < -0.5`): negative-node weight dominates — the
  knowledge graph's evidence tilts toward losses/bearish outcomes.
- **Contraction** (`net_signal > +0.5`): positive-node weight dominates.
- **Neutral**: neither threshold reached.

Under **Expansion** two defensive modes translate the regime to iron condor
parameters (caller selects via `mode=`):
- `"scale_risk"` (default) — `risk_pct = base × 0.70` (30 % fewer contracts,
  lower max loss per cycle)
- `"widen_wings"` — `short_strike_otm_pct = base × 1.50` (short put moves
  50 % further OTM, widening the tail-risk buffer)

Wired end-to-end:
- `market_loop.fetch_market_state` calls `compute_regime_overrides()` and
  stashes the result as `state["vol_overrides"]`.
- `options_proposer.run_headless` strips `vol_overrides` from state before
  unpacking into `build_proposal`, forwarding `risk_pct` / `short_strike_otm_pct`
  as explicit kwargs.
- `build_proposal` gained two optional kwargs (`risk_pct`, `short_strike_otm_pct`)
  that fall back to the module constants — fully backward-compatible.

Fail-safe throughout: missing DB / empty graph / any exception returns `{}`
so the proposer runs unchanged. Tests: `tests/test_vol_bridge.py` (31 tests,
offline in-memory SQLite, covering polarity classification, net-signal
arithmetic, boundary precision, macro shock scenarios, and the
`run_headless` integration). Decision #38 in `DECISIONS.md`.

## ✅ Phase 6E: Temporal Signal Decay — BUILT AND TESTED (2026-07-08)

`src/decay_engine.py` is a standalone daily sweep that applies exponential
decay to every active `graph_edges` row: `w(t) = w₀·exp(−λ·t)` where `t` is
days since the edge was last written or swept, and `λ` is the per-edge
`decay_lambda` (default 0.05 — matching the Sleep Phase's semantic-node
decay rate). When a decayed weight falls below 0.1 the edge is soft-expired
(`invalid_at` stamped) so `GraphEngine` excludes it from inference; it is
never deleted, so a re-observed pattern (same triple via `add_edge`) reactivates
it automatically (decision #37). Three additive columns were added to
`graph_edges`: `valid_from` (creation/last-sweep timestamp), `invalid_at`
(expiry marker, NULL = active), `decay_lambda` (per-edge rate). `add_edge`
now stamps `valid_from = now` and clears `invalid_at` on both first write and
reinforce. `GraphEngine.__init__` loads only `WHERE invalid_at IS NULL`.
Migration is idempotent — existing DBs are upgraded in place on next connect.
Run manually: `python3 -m src.decay_engine`. Tests: `tests/test_decay.py`
(22 tests, all offline). **No network I/O, no market data** (decision #30 holds).

## Current production state (as of 2026-07-06)

- **Phases 1-4 (alerting, suggestions, paper trading, journal/plans/tracking/
  news/forecast/tuner) are feature-complete.**
- **Phase 5 (frontend + local API) is live**: unified FastAPI backend
  (`src/api.py`), a React dashboard (`lovable-frontend/`, Supabase-free),
  direct Gemini integration (no cloud AI gateway), an hourly auto-sync loop,
  and a Discord analyst bot (`src/discord_bot.py`).
- **Market data has been fully migrated from yfinance to the DhanHQ Data
  API** (`src/dhan_client.py`). This is the single source of prices/OHLC for
  the whole engine now.
- **The backend is deployed to a fresh GCP VM (2026-07-06)** running the
  DhanHQ-backed FastAPI server continuously as a systemd service — see
  "GCP VM (cloud hosting)" below. The old cron VM is superseded.
- **Phase Operational — DONE (2026-07-06):** `scripts/setup_cron.sh` deploys
  the token-renewal (`src.renew_token`, 07:00 IST) and email-digest
  (`src.main` 15:35 IST, `src.suggest` 08:00 IST) cron schedules on the VM,
  closing the "known gap" that used to be documented here. `src/api.py`
  also now runs a `_poll_watchlist_loop` background task (60s cadence,
  `asyncio.to_thread` for the blocking DhanHQ/analysis calls) that
  deduplicates rule breaches per-day and fires `src.notifier.send_digest`
  email alerts directly from the live server, independent of the hourly
  auto-sync loop.
- **Phase 5 (Options) — COMPLETE (2026-07-06), both parts.**
  *Part A (frictions)*: `src/portfolio.py` applies the full 2026 cost
  stack per executed leg — STT 0.15% (sell side ONLY), Stamp Duty 0.003%
  (buy side only), flat ₹20 brokerage, NSE exchange charges (0.00345%),
  SEBI turnover fees (0.0001%), and 18% GST on the service charges — plus
  `calculate_span_margin()`, a SPAN simulation with hedge offsets (a
  defined-risk spread blocks only its net risk, a naked short gets the
  punitive treatment). `src/plan_tracker.py` applies dynamic bid-ask
  slippage on resolution (0.05% index; 0.1%-0.5% options by liquidity;
  0% stocks).
  *Part B (spreads)*: `strategy.StrategyConstructor` builds defined-risk
  structures ONLY (bull call / bear put verticals, iron condor / iron
  butterfly — zero naked legs by construction), gated by India VIX
  (range-bound strategies strictly blocked when VIX > 16 *or* VIX is
  unavailable) and sized by ABSOLUTE MAX LOSS, capped by SPAN margin vs
  cash. India VIX lives in `dhan_client` (`get_india_vix()`, security id
  21 verified against Dhan's scrip master). The tracker resolves spreads
  as ATOMIC BASKETS (no per-leg exit path exists — the SPAN-spike
  sequencing bug is structurally impossible) with auto-exit at 65% of max
  profit or strictly 2 days before expiry (gamma rule), modeled P&L
  clamped to the structure's defined-risk bounds, and net-of-frictions
  journaling. The proposal wiring is `src/options_proposer.py`
  (`python3 -m src.options_proposer`, terminal, human-in-the-loop):
  trend read via suggestions.analyze -> India VIX + real Dhan option
  chain -> regime-matched spread (bullish: bull call; bearish: bear put;
  neutral: iron condor, VIX-gated) -> sized by the dedicated
  `options_risk_per_trade_pct` budget (config.json, 10% — decision #28)
  -> approve/reject + why -> journal entry the tracker resolves.
  **Discord-surfaced (2026-07-06)**: the moment a proposal is built, a
  rich 🚨 PROPOSAL ALERT (regime/VIX, legs in a code block, economics
  incl. max loss + SPAN margin, action-required note) fires to Discord
  BEFORE the terminal pauses for y/n, and a short ✅/❌ decision
  follow-up after — both fail-safe, an unreachable Discord never blocks
  the session. Dashboard surfacing still open.
- **Discord connectivity dry run**: `python3 -m src.plan_tracker
  --mock-trade-strategy IRON_BUTTERFLY` pushes a synthetic [MOCK] Trade
  Episode through the real notifier path (nothing journaled; exit code 0
  only if Discord actually accepted it). Needs `DISCORD_WEBHOOK_URL` in
  `.env`. The options proposer also pushes a "Spread proposed" message on
  every journaled decision.
- **Phase 10B extractor BUILT (2026-07-06)**: `src/local_parser.py` —
  `LocalExtractor` (OpenAI-compat calls to local Ollama only,
  `OLLAMA_BASE_URL`/`OLLAMA_MODEL` in `.env`, defaults
  `http://localhost:11434/v1` / `llama3`), `extract_event_json()` (strict
  EEF JSON with schema coercion), and `process_unstructured_input(conn,
  text)` writing idempotently into the Brain Map `events` table
  (`brain_map.py` itself untouched and still network-free). Fully
  fail-safe; guardrail test enforces zero market-data imports (decision
  #30). **Ollama IS installed on the host with `llama3` pulled
  (confirmed 2026-07-06)** — the parser is live-capable; offline tests
  stay mocked regardless.
- **Phase 10B "Sleep Phase" BUILT (2026-07-06)** — `src/sleep_phase.py`
  (`python3 -m src.sleep_phase`, run off-market hours / cron it): three
  sequential fail-safe tasks against `data/brain_map.db`. (A) *Ingestion*:
  journal free text (signal + "why") -> EEF events via the local parser,
  hash-deduped in a new `ingest_log` table holding provenance pointers
  (journal_ref) back to the source rows; failures aren't logged so they
  retry when Ollama is back. (B) *Consolidation*: last-24h events -> ONE
  Ollama call clustering themes into `semantic_nodes` (confidence 1.0)
  with `semantic_event_link` graph edges; re-observed themes are
  reinforced (confidence reset, reactivated) instead of duplicated.
  (C) *Decay*: `score_new = score * e^(-λ·Δt)` anchored on
  last-reinforced/last-decayed so repeat runs never double-count days;
  below 0.20 the node is flagged `active=0` (never deleted). Knobs are
  optional `config.json` keys (`sleep_decay_lambda` 0.05,
  `sleep_prune_threshold` 0.20, `sleep_consolidation_hours` 24). The three
  new tables are created and owned by `sleep_phase.py` — `brain_map.py`'s
  core schema stays untouched. Decision #30 holds: no market data, no
  trading, local Ollama only. **Cron automation DONE (2026-07-06)**:
  `scripts/setup_cron.sh` entry #4 schedules it daily at 20:00 IST
  (`CRON_TZ=Asia/Kolkata` pins IST on Linux), logging to
  `logs/sleep_phase.log`. ⚠️ Placement note: the sleep phase only does
  real work on the machine holding `data/journal.jsonl`,
  `data/brain_map.db` AND Ollama (currently the Mac — the VM deploy
  excludes `data/` and can't run llama3 on an e2-micro); elsewhere it
  degrades to a harmless decay-only pass.
- **Market loop + headless proposals BUILT (2026-07-06)**:
  `src/market_loop.py` (`python3 -m src.market_loop`) is an async daemon
  that polls NIFTY 50 / NIFTY BANK every 15 min during NSE hours
  (Mon-Fri 09:15-15:30 IST; sleeps otherwise) via the abstract
  `fetch_market_state()` seam (pure-Python indicators + VIX — the exact
  injection point for the Phase 7 simulator), and on a favorable setup
  triggers `options_proposer.run_headless()`: 🚨 Discord alert + journal
  entry with decision `pending_approval`, NO terminal pause. Per-index
  2h cool-down stops Discord spam; blocked/no-signal cycles don't burn
  it. Pending entries are tracked hypothetically like rejected ones
  (user's call — see decision #31); decide them any time with
  `python3 -m src.options_proposer --review-pending` (reads the stored
  spread payload from the journal, NO market data fetched: y -> approved
  on paper, tracker takes over; n -> rejected + why; entries the tracker
  already resolved hypothetically are left alone — no hindsight
  approvals). One bad cycle never kills the loop.
- **Discord approval buttons — DONE (2026-07-07):** `/pending` in Discord
  lists every PENDING_APPROVAL proposal with tappable ✅ Approve / ❌
  Reject buttons (persistent across bot restarts — the trade_id round-trips
  through the component custom_id via `discord.ui.DynamicItem`); each tap
  opens a one-line "why" prompt, then POSTs to the gateway's
  `POST /api/discord/action` with the `x-api-key` — the bot never touches
  the journal or engine modules itself (its read-only guardrail holds; the
  gateway owns the mutation). New read side: `GET /api/discord/pending` on
  `src/api_server.py`. The bot reads `BRIDGE_BASE_URL` (default
  `http://127.0.0.1:8000` — correct when it runs on the same VM as the
  gateway, which also makes the quick-tunnel URL irrelevant for approvals).
  Tests: `tests/test_discord_buttons.py` + pending-list tests in
  `tests/test_api_server.py`.
- **Phase 11 scaffolding: Random Forest Skeptic Agent — BUILT (2026-07-07),
  model untrained by design:** `src/skeptic_agent.py` (`RandomForestAuditor`)
  merges the knowledge graph's 2-hop evidence (edge count, cumulative/avg
  confidence, Brain-Map avg R for the active tags) with the proposal's
  market numbers (VIX, signed net premium, spread width, days to expiry,
  max loss/lot, lots) into the frozen `FEATURE_NAMES` vector, and — once
  the Phase 7 simulator trains and saves `data/skeptic_model.pkl` — scores
  P(win) with a Random Forest. Wired into `options_proposer` right before
  the alert is formatted: below 0.40 a strictly formatted "⚠️ Skeptic
  Agent Warning" rides in the Discord PROPOSAL ALERT. Until a trained
  model exists it ABSTAINS silently (decision #35 — no fake warnings from
  an untrained forest), sklearn loads lazily only when a model file is
  present, and every failure abstains rather than blocking a proposal.
  Advisory only, never gates. `scikit-learn` added to `requirements.txt`.
  Tests: `tests/test_skeptic_agent.py` + proposer integration tests.
- **Phase 7 Time-Travel Simulator — BUILT AND VALIDATED END-TO-END ON REAL
  DATA (2026-07-07):** `src/simulator.py`
  (`python3 -m src.simulator --start YYYY-MM-DD --end YYYY-MM-DD`) replays
  history through the REAL pipeline: as-of-date SMA/RSI analysis (no future
  data ever enters a proposal), historical VIX, a synthetic option chain,
  the actual `build_proposal()` logic, auto-approve, then resolution via
  `plan_tracker`'s own pure helpers — 65% profit take, pre-expiry gamma
  rule, and the FULL 2026 friction stack, byte-identical to live. Results
  land idempotently (deterministic `sim:` journal_refs) in the additive
  `simulated_trades` table + standard `outcomes`/`events`/links, and
  `encode_causal_links` runs the Sleep Phase's Task D over the simulated
  window so graph_edges mint from simulated post-mortems exactly like real
  ones (decision #36). The real journal/portfolio are never touched; no
  notifier/network imports (both guard-tested).
  **Live validation run (2026-07-07, real DhanHQ history, NIFTY 50,
  2025-07-01 → 2026-06-30, 56 trading days scanned):** 56 iron-condor
  proposals, 56/56 resolved — **48 wins (avg +Rs.140,532, avg R +1.43)**,
  **8 losses (avg −Rs.76,802, avg R −0.78)**, 0 scratches; `brain_map.db`
  went from empty to 182 events / 56 outcomes / 168 links; the causal
  writer minted the graph's first two real edges,
  `iron_condor RESULTS_IN win` and `iron_condor RESULTS_IN loss` (both
  confidence 1.0) — the Phase 6C/6D memory stack now has real content for
  the first time. **Phase 7 is officially validated, not just built.**
  Also fixed in passing: spread outcomes now record their strategy as the
  Brain Map `archetype`
  ("iron_condor", not "other"), so causal summaries name the trade for
  real trades too. Tests: `tests/test_simulator.py`.
- **Full offline test suite: 244/244 passing** (`python3 -m pytest tests/`;
  the `for f in tests/test_*.py; do python3 "$f"; done` __main__ loop runs
  all 23 files clean too), including `tests/test_options_spreads.py`
  (condor max-loss math, STT sell-side-only, VIX gate, atomic tracker
  resolution), `tests/test_options_proposer.py` (regime mapping,
  strike selection off a fake chain, budget sizing, journal contract),
  `tests/test_api_server.py` (Phase 9 gateway auth + Discord bridge),
  `tests/test_graph_engine.py` (Phase 6C 2-hop BFS + confidence sorting),
  and `tests/test_causal_writer.py` (Phase 6D triple extraction + decision
  #34 sourcing).
- **Discord episodic encoder — DONE (2026-07-06):** `src/discord_client.py`
  (async `httpx` webhook client, `DISCORD_WEBHOOK_URL` in `.env`, optional
  `thread_id` grouping, fully fail-safe) + `notifier.send_discord_message()`.
  The API's poll loop pushes watchlist alerts to Discord alongside email,
  and the hourly auto-sync loop pushes a structured "Trade Episode"
  (market sentiment + prices + rule that fired) for every resolution —
  built by the pure `brain_map.build_episode_snapshot()` and handed out of
  the sync tracker via `run_tracker(on_episode=...)`, so the Brain Map
  itself still does zero network I/O (decision #25's additive rule holds).
- **Discord delivery VERIFIED LIVE end-to-end (2026-07-06)**: a real
  webhook was created on the "Alpha Trading" Discord server (#general),
  `DISCORD_WEBHOOK_URL` set in `.env` both locally and on the VM (via the
  base64-paste method below), and confirmed working by two live sends —
  a plain connectivity ping and the `--mock-trade-strategy` dry run — both
  landing in #general with `Discord delivery: OK`. The VM's systemd
  service was restarted afterward and came up clean
  (`systemctl status alpha-trading` → `active (running)`, both background
  loops armed), so live watchlist alerts and real resolved-trade episodes
  now push to Discord in production, not just locally.
- **Phase 9 Public API Gateway & Discord Bridge — DONE (2026-07-07):** `src/api_server.py` implements a strict fail-closed API-key gateway (requiring `X-API-Key` or `Authorization: Bearer` token) that wraps the `src.api` FastAPI app. It also hosts the two-way Discord bridge endpoint `POST /api/discord/action` to securely decide pending approvals directly from phone notifications/Discord webhook callbacks. Tested and verified offline via `tests/test_api_server.py`.
- **Phase 6C Knowledge Graph Reasoning Layer — DONE (reader; 2026-07-07):**
  `src/graph_engine.py` — a `GraphEngine` that loads the additive
  `graph_edges` table (`source_node, relation, target_node,
  confidence_score`) from `data/brain_map.db` into a `networkx.DiGraph`
  once at construction, then answers `get_relevant_context(node,
  max_hops=2)` — a BFS to depth 2 returning linked edges sorted by
  confidence — purely from memory. Strictly READ-ONLY, never writes during
  inference (decision #33). Wired into `src/options_proposer.py`: each
  proposal runs a fail-safe "Memory Query" on its ticker and appends a 🧠
  Memory block to the Discord PROPOSAL ALERT rationale (advisory only —
  no rule/score change, decision #26 philosophy). Additive: `brain_map.py`
  untouched; SQLite stays the only persistent store, `networkx` is just the
  in-memory reasoning layer (no new DB). Tests: `tests/test_graph_engine.py`
  (+ proposer memory-block tests). `networkx` was added to
  `requirements.txt`.
- **Phase 6D Causal Triple Writer — DONE (2026-07-07):** the Sleep Phase now
  WRITES the graph. `src/sleep_phase.py` gained Task D `write_causal_links`
  (the pass is now A→B→C→**D**): it reads reviewed trades from the
  `outcomes` table (with their `src/analyst.py` post-mortems), calls the new
  `local_parser.LocalExtractor.extract_causal_triples()` — which mines
  `(subject)-[predicate]->(object)` triples, predicate ∈ RESULTS_IN /
  PRECEDES / INDICATES / CONTRADICTS — and writes each into `graph_edges` at
  confidence 1.0, idempotently (a `UNIQUE(source, relation, target)` upsert;
  a new nullable `context` column preserves the "when VIX > 20" qualifier).
  **Sourced ONLY from reviewed outcomes, never raw news sentiment
  (decision #34)** — with no resolved trades it makes no LLM call at all.
  The proposer's Memory Query now seeds on ticker + view + **strategy**, so
  these concept-keyed causal edges actually surface in the Discord PROPOSAL
  ALERT. Tests: `tests/test_causal_writer.py`. Live effect appears once the
  first trades resolve and a Sleep Phase runs with Ollama up.

## Credentials & environment variables

All secrets live in `.env` (repo root, git-ignored — `.env.example` is the
safe versioned template). Load pattern used everywhere: a self-contained
reader in each entry point (`_load_env()`), not a shared library, by design
(modularity — see `DECISIONS.md`).

| Variable | Purpose | Notes |
|---|---|---|
| `DHAN_CLIENT_ID` | DhanHQ account id | `1109738713` as of this writing |
| `DHAN_ACCESS_TOKEN` | DhanHQ Data API token | **Short-lived (~24h)**, auto-minted daily by `python3 -m src.renew_token`. V2 flow (post Oct-2025 overhaul) needs `DHAN_PIN` + `DHAN_TOTP_SECRET` (+ `DHAN_API_KEY`/`DHAN_API_SECRET`) in `.env` — see the "✅ RESOLVED" block at the top of this file for the one-time Dhan-web setup. Without those keys it falls back to the deprecated legacy renewal (expect `DH-905` + manual pastes). |
| `DHAN_PIN` / `DHAN_TOTP_SECRET` / `DHAN_API_KEY` / `DHAN_API_SECRET` | DhanHQ V2 headless auth (daily token minting) | PIN = the Dhan login PIN. API key + secret: `developer.dhanhq.co/live-environment` → "API Key" tab (not "Access Token") → name an app, any placeholder `https://` URL works for Redirection (never actually used by our headless flow) → Generate. TOTP secret: **on that same "API Key" tab**, enable TOTP — the plain-text secret is shown only once at enrollment, so copy it immediately; if missed, Disable then re-enable to see a fresh one (confirm the re-enrollment code with `python3 -c "import pyotp; print(pyotp.TOTP('SECRET').now())"`, no phone app needed). Needed on BOTH the Mac and the VM. |
| `GEMINI_API_KEY` | Google Gemini (news sentiment + chat) | Get from Google AI Studio, create the key against the *existing billed* `alpha-trading-app-2026` GCP project (a key from AI Studio's "new project" flow gets zero free-tier quota — see `DECISIONS.md`). |
| `DISCORD_BOT_TOKEN` | Discord bot login | From the Discord Developer Portal, needs "Message Content Intent" enabled. |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook (alerts + trade episodes push) | **Set and verified live 2026-07-06**, both locally and on the VM. Different thing from the bot token above — a channel gear icon → Integrations → Webhooks → New Webhook → Copy Webhook URL. Pushes to the "Alpha Trading" server's #general channel. Verify anytime with `python3 -m src.plan_tracker --mock-trade-strategy IRON_BUTTERFLY` (prints `Discord delivery: OK`/`FAILED`, journals nothing). |
| `ALERT_EMAIL_FROM` / `ALERT_EMAIL_APP_PASSWORD` / `ALERT_EMAIL_TO` | Gmail SMTP for alert/suggestion/session digests | App Password (16-char), not the normal Gmail password. |

`lovable-frontend/.env` (separate, its own git-ignore inside that folder)
needs only `VITE_API_BASE_URL="http://localhost:8000"` — no Supabase keys
(stripped 2026-07-06).

## Boot commands

```bash
# 1. Python engine dependencies (from repo root)
python3 -m pip install -r requirements.txt

# 2. The unified local API (serves the dashboard + all /api/* routes)
# Run the raw server (no key required, localhost dev):
uvicorn src.api:app --reload --port 8000
# Or run the strict API-key gateway (Phase 9 public exposure mode):
uvicorn src.api_server:app --reload --port 8000

# 3. The React dashboard (separate terminal)
cd lovable-frontend && npm install && npm run dev   # localhost:8080 (falls back :8081)

# 4. The Discord analyst bot (separate terminal, optional)
python3 -m src.discord_bot

# 5. Interactive paper-trading session (terminal, when you want to trade)
python3 -m src.trade

# 5b. Options spread proposer (terminal; needs a valid Dhan token for the
#     live chain/VIX — proposes ONE defined-risk spread, you approve/reject)
python3 -m src.options_proposer            # NIFTY 50
python3 -m src.options_proposer "NIFTY BANK"
python3 -m src.options_proposer --review-pending   # decide market-loop
                                                   # PENDING_APPROVAL entries
                                                   # (offline, no market data)

# 6. Offline test suite (no internet/API calls needed)
python3 -m pytest tests/                          # expect 244 passing

# 7. Market loop daemon (market hours only; headless proposals to Discord)
python3 -m src.market_loop

# 8. Discord connectivity check (needs DISCORD_WEBHOOK_URL set; journals nothing)
python3 -m src.plan_tracker --mock-trade-strategy IRON_BUTTERFLY

# 9. Public gateway (Phase 9 exposure mode — strict x-api-key, wraps src.api)
uvicorn src.api_server:app --host 127.0.0.1 --port 8000
```

Manual/on-demand engine scripts (not on a schedule locally — only via VM cron
or run by hand): `python3 -m src.main` (alerts), `python3 -m src.suggest`
(suggestions), `python3 -m src.news_processor` (refresh news sentiment),
`python3 -m src.forecast` (print forecasts), `python3 -m src.tuner` (refresh
learned weights), `python3 -m src.plan_tracker` (manual resolve sweep — also
runs automatically at the start of every `src.trade` session and every hour
inside `src.api`), `python3 -m src.review` (7-day scorecard for pre-plan
entries).

## GCP VM (cloud hosting)

**Rebuilt from scratch 2026-07-06.** The original cron VM (project
`alpha-trading-app-2026`) had a lost login and is abandoned; a new VM was
created and now runs the current DhanHQ FastAPI backend.

- **VM**: `alpha-trading-vm`, project `project-37632031-10d0-47dd-b6f`
  ("My First Project", org `adigupta1998-org`), zone `us-central1-a`, machine
  type `e2-micro`, Debian 13 (trixie), Python 3.13. Billing has ₹28,321
  free-trial credit expiring 2026-10-01.
- **External IP**: `35.239.254.99` — ⚠️ *ephemeral*, can change if the VM is
  stopped/started. Reserve a static IP before relying on it externally.
- **SSH**: GCP Console → Compute Engine → VM instances → **SSH** button
  (browser terminal, no key files). `gcloud compute ssh` also works if the
  gcloud CLI is configured locally, but it is not set up as of this writing.
- **Code lives at** `~/alpha_trading` on the VM, cloned from GitHub (`main`),
  with a Python venv at `~/alpha_trading/venv`.
- **Runtime**: the unified FastAPI API (`src.api:app`) runs continuously on
  port 8000 as a **systemd service** named `alpha-trading`
  (`/etc/systemd/system/alpha-trading.service`): `Restart=always`, enabled on
  boot. This includes the built-in hourly auto-sync loop. Health check:
  `http://localhost:8000/api/health` → `{"status":"ok","mode":"paper-only"}`.

  ```bash
  # deploy an update (on the VM)
  cd ~/alpha_trading && git pull && venv/bin/pip install -r requirements.txt
  sudo systemctl restart alpha-trading

  # operate
  systemctl status alpha-trading          # is it running?
  sudo journalctl -u alpha-trading -f      # live logs (Ctrl+C to exit)
  sudo systemctl restart|stop alpha-trading
  ```

- **`.env` on the VM** is NOT in git and must be transferred by hand. ⚠️
  **Do not paste the DhanHQ JWT directly into the browser SSH terminal** — a
  secret-scanner silently replaces the `eyJ...` token with bullet characters,
  causing `'latin-1' codec can't encode` errors at runtime. Working method:
  on the Mac, `base64`-encode `.env` and pipe a decode command to the
  clipboard, then paste that (the base64 blob isn't recognized as a token, so
  it survives):
  ```bash
  # on the Mac (fills clipboard with a ready-to-run command):
  printf 'echo %s | base64 -d > ~/alpha_trading/.env && echo OK\n' \
    "$(base64 < ~/Documents/Claude/alpha_trading/.env | tr -d '\n')" | pbcopy
  # then paste into the VM SSH window + Enter, then restart the service.
  ```
  Because `DHAN_ACCESS_TOKEN` is short-lived (~24h), keep it alive with the
  auto-renewal script instead of daily manual pastes: after ONE manual seed
  of a valid token, schedule `python3 -m src.renew_token` on the VM
  (`crontab -e`, e.g. `0 6 * * * cd ~/alpha_trading && venv/bin/python -m
  src.renew_token >> logs/renew_token.log 2>&1`). The manual base64 paste
  above is then only needed if a renewal window is missed and the token
  dies (script prints CRITICAL).
- **No firewall port is ever opened — inbound goes through a Cloudflare
  Tunnel only** (Phase 9, decision #32) — **LIVE end-to-end 2026-07-07**:
  port 8000 is reachable only on the VM itself, bound to `127.0.0.1`
  (`alpha-trading.service`'s `ExecStart` now runs
  `uvicorn src.api_server:app --host 127.0.0.1 --port 8000`, the strict
  gateway wrapping the full `src.api` app + the two-way Discord bridge
  `POST /api/discord/action`). `cloudflared` is installed and runs as its
  own systemd service, `cloudflared-tunnel` (`ExecStart=<cloudflared path>
  tunnel --url http://localhost:8000`, `Restart=always`, enabled on boot,
  `Requires=alpha-trading.service`), dialing OUT to Cloudflare and
  forwarding public HTTPS traffic in. The gateway is fail-closed: every
  request needs an `x-api-key` header matching `.env`'s `API_KEY` (401
  otherwise), and it refuses everything with 503 if `API_KEY` is unset —
  only `GET /api/health` stays public. Verified live from an outside
  network (not just VM loopback): `GET /api/health` → 200, and
  `POST /api/discord/action` with a real key and a bogus `trade_id` → 404
  (proving the full chain: Cloudflare edge → tunnel → gateway auth →
  `options_proposer.decide_pending` → journal lookup).
  ⚠️ **This is a "quick tunnel"** (no Cloudflare account/domain needed) —
  free and fast to stand up, but the public URL is **randomly regenerated
  on every restart** of `cloudflared-tunnel` (crash, VM reboot). Fetch the
  current one anytime with:
  `sudo journalctl -u cloudflared-tunnel --no-pager | grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' | tail -1`
  For a permanent, never-changing URL (needed before hardcoding it into a
  Discord bot integration), upgrade to a **named tunnel** — requires adding
  a domain to a Cloudflare account (`cloudflared tunnel create` +
  `tunnel route dns`). Not done — deferred until a domain is available.
- **Scheduled jobs**: `scripts/setup_cron.sh` (idempotent, safe to re-run
  after every `git pull`) installs the full cron block — `src.renew_token`
  07:00 IST daily, `src.main` 15:35 IST Mon-Fri, `src.suggest` 08:00 IST
  Mon-Fri, and `src.sleep_phase` 20:00 IST daily — each logging to
  `logs/<name>.log`, pinned to IST via `CRON_TZ=Asia/Kolkata`. Run it on
  the VM with `bash ~/alpha_trading/scripts/setup_cron.sh`; note the sleep
  phase only does real work where `data/` + Ollama live (see the Phase 10B
  bullet above).
- `data/`, `tests/`, `logs/` are not part of the deploy (paper-trading state
  stays local only; see `OVERVIEW.md`). `config.json` and `.env` are required
  — `src/config.py` fails loudly at import without `config.json`, and
  `src/dhan_client.py` needs `.env`'s Dhan keys.

## Watchlist (current)

10 tickers in `config/watchlist.yaml`, each with `percent_up`/`percent_down`
alert rules at 3%: `HDFCBANK.NS`, `ICICIBANK.NS`, `TCS.NS`, `INFY.NS`,
`RELIANCE.NS`, `ONGC.NS`, `HINDUNILVR.NS`, `ITC.NS`, `MARUTI.NS`, `TMPV.NS`.
All 10 are present in `src/dhan_client.py`'s `SECURITY_ID_MAP` — a ticker not
in that map cannot be priced by the current data layer.

## Live paper-trading data (IMPORTANT — do not reset)

`data/journal.jsonl` and `data/portfolio.json` are git-ignored and hold real
(paper) user activity: an original ONGC.NS buy (2026-07-03) plus several
2026-07-06 dashboard test trades (TCS/MARUTI/ONGC) made by clicking the
frontend's seeded demo proposal cards — kept intentionally, per the user.
Note those demo trades used bare tickers (`TCS`, not `TCS.NS`); resolving
them correctly depends on `dhan_client`'s alias resolution.
**Never reset these files.** When testing anything that writes to them, back
up first and restore after (or point at an isolated temp dataset) — this is
the working pattern used throughout this project's history.

## Next steps / roadmap

**Phase 6 (Brain Map) steps 1–2 landed 2026-07-06**: `src/brain_map.py`
(native `sqlite3` store at `data/brain_map.db` — `events`, `outcomes`,
`event_outcome_link` tables, record/link helpers, and
`query_similar_events(tags)` returning `{count, win_rate, avg_r_multiple,
examples}`) plus `tests/test_brain_map.py` (offline in-memory tests). The
design remains banked in `DECISIONS.md` → "Phase 6 — Brain Map design".

**Phase 6 steps 3–4 landed later on 2026-07-06**: new journal entries now
carry a stable `short_id` (8-char uuid hex, `src/journal.py` — older lines
without one are fine, readers fall back to a composite
`date|ticker|action|price` key via `brain_map.journal_ref_for()`), and
`ingest_existing()` in `src/brain_map.py` idempotently seeds the map from
resolved `journal.jsonl` trades and `data/news_sentiment.json`. Run it any
time with `python3 -m src.brain_map ingest` (re-running is safe and picks
up newly resolved trades). The real `data/brain_map.db` now exists,
holding 10 news events; 0 outcomes so far because no journal trade has
resolved yet. Full suite: **55/55**.

**Phase 6 step 5 (the final step) landed later on 2026-07-06 — PHASE 6 IS
COMPLETE.** `forecast.py` now queries the map: when the current setup has
active pattern tags (fresh Golden Cross → `fresh_cross`+`golden_cross`,
oversold RSI → `rsi_oversold`), the forecast payload gains `memory` stats
and a `memory_context` line ("Historical Performance for active patterns
[...]: Win Rate: X%, ...") that `describe()` prints (terminal + Discord
`/analyze`). Advisory only — zero score points (decision #26 in
`DECISIONS.md`); empty/missing DB degrades to `memory: null` with the
standard flow untouched. `tuner.py`/`brain_weights.json` were never
modified. Suite: **63/63**. Contract addition documented in
`DATA_CONTRACT.md` § 2.4.

**Phase 6 core loop also landed 2026-07-06 (after step 5)** — the
feedback loop is now fully automatic. The moment `plan_tracker` resolves
a plan it (a) captures the original thesis + realized execution metrics,
(b) asks the new post-mortem analyst (`src/analyst.py`, Gemini,
never-raises) for a structured `{variance_analysis, unexpected_variables,
future_guardrails}` JSON, and (c) writes outcome + events + post-mortem
into the Brain Map keyed by the entry's `short_id`
(`brain_map.record_resolved_entry`, shared with `ingest_existing`). The
`outcomes` table gained a `post_mortem` column (auto-migrated in place on
connect). All fail-safe: no Gemini key / locked DB just prints a note,
journal resolution is never blocked. Suite: **71/71**.

**Ongoing Brain Map operation**: nothing manual needed anymore — resolved
trades flow in live via the tracker. `python3 -m src.brain_map ingest`
remains available as a backfill/repair sweep (it won't have post-mortems,
which only generate at live resolution). `memory_context` lines appear in
forecasts once the first trades resolve.

**Phase 9 backend landed 2026-07-07, and the VM exposure is now LIVE**:
`src/api_server.py` is the strict public gateway (fail-closed API-key auth
on every route, wraps the full `src.api` app) with the two-way Discord
bridge `POST /api/discord/action` — approve/reject a `pending_approval`
journal entry by its `short_id`, exactly the `--review-pending` semantics
(`options_proposer.decide_pending`). Tests: `tests/test_api_server.py`. On
the VM: `alpha-trading.service` now runs `src.api_server:app` on
`127.0.0.1:8000`, and `cloudflared` runs as its own systemd service
(`cloudflared-tunnel`) forwarding a public quick-tunnel URL to it — see the
GCP VM section above for the exact setup and the "URL changes on restart"
caveat. Verified end-to-end from an outside network: health check and the
Discord bridge both round-trip correctly through the tunnel.

**Discord approval buttons landed later on 2026-07-07** (see the bullet in
"Current production state"): `/pending` + persistent Approve/Reject buttons
in the bot, `GET /api/discord/pending` on the gateway. For the phone flow
to be fully hands-off, the bot (`python3 -m src.discord_bot`) and the
market loop (`python3 -m src.market_loop`) need to run continuously on the
VM (systemd services, same pattern as `alpha-trading`) — note the pending
entries then live in the VM's own `data/journal.jsonl`, a separate file
from the Mac's local journal.

**Next up, in priority order**: (1) ~~the DhanHQ V2 auth refactor~~ ✅
DONE AND VERIFIED LIVE on the Mac 2026-07-08 — see the "✅ RESOLVED" block
at the top; only replicating the same `.env` keys on the **VM** remains
(so its cron renewal also uses V2); (2) training the skeptic model on
simulated trades (Phase 7b, now genuinely unblocked); (3) upgrading to a
named Cloudflare tunnel for a permanent URL (needs a domain); (4) analyst
procedural evolution (see `DECISIONS.md` → "Still open"). The VM's
scheduled jobs are handled by `scripts/setup_cron.sh`
(see the GCP VM section).

## Where to look for more detail

- **Deep phase-by-phase build history** (what was built, when, and how it
  was verified) lived in this file through 2026-07-06 and has moved to git
  history / commit messages — `git log --oneline` and the commit bodies are
  the detailed record now. This file stays a lean cold-start brief going
  forward, per the user's instruction not to bloat it on every change.
- **Phase 4's step-by-step plan** (4A-4F): `PLAN.md`.
- **The Phase 5+ vision** (Discord, Brain Map, simulator, event ingestion):
  `VISION_PLAN.md`.
- **Frontend JSON contracts**: `DATA_CONTRACT.md`.

---
## 🚀 The Master Execution Plan (Current Targets)
(Note: Do not execute these until explicitly prompted by the user)

### Phase Operational: Fix VM Gaps & Token Automation — ✅ DONE (2026-07-06)
* ~~Create `scripts/setup_cron.sh` to schedule `src.renew_token` at 07:00 AM IST.~~
* ~~Add cron schedules for `src.main` (15:35 IST) and `src.suggest` (08:00 AM IST).~~
* ~~Add a fast background asyncio loop to `src/api.py` to poll prices via DhanHQ and trigger workflows only on watchlist breaches.~~

### Phase 5: Options Trading & Frictions — ✅ DONE (2026-07-06)
* **Part A (Frictions) — ✅ DONE:** ~~Update `src/portfolio.py` with 2026 STT (0.15%), SPAN margin simulation, and bid-ask slippage.~~ Full 2026 stack (STT sell-only, Stamp Duty buy-only, brokerage, NSE exchange charges, SEBI fees, GST on service charges) + `calculate_span_margin()` hedge-offset simulation in `src/portfolio.py`; dynamic bid-ask slippage in `src/plan_tracker.py`.
* **Part B (Strategy) — ✅ DONE:** ~~Update `src/strategy.py` to propose defined-risk spreads ONLY (Bull Call/Bear Put/Iron Condors). Integrate India VIX filtering (Block Iron Condors if VIX > 16). Update tracker for early exits at 60-70% max profit to kill Gamma risk.~~ `StrategyConstructor` + VIX gate (via `dhan_client.get_india_vix()`) + max-loss sizing; tracker resolves spreads as atomic baskets with 65%-of-max-profit / 2-days-before-expiry auto-exits. Proposal wiring also DONE: `src/options_proposer.py` (`python3 -m src.options_proposer`) fetches the real chain + VIX, builds the regime-matched spread, sizes it via `options_risk_per_trade_pct` (decision #28), and journals your approve/reject. Dashboard/Discord surfacing still open.

### Phase 6 (Advanced): Memory Consolidation & Evolution
* ~~Update Brain Map schema for `confidence_score` and temporal decay.~~ ✅ DONE 2026-07-06 — landed as the `semantic_nodes` table (confidence_score, last_reinforced/last_decayed, active flag) owned by `src/sleep_phase.py`, additive to brain_map's core schema.
* ~~Create a "Sleep Phase" background task to process memory off-market hours.~~ ✅ DONE 2026-07-06 — built as a standalone cron job (`src/sleep_phase.py`, 20:00 IST via `scripts/setup_cron.sh`) rather than inside `src/api.py`, so local LLM inference never shares a process with the live server.
* Add procedural evolution to `src/analyst.py` (proposing new trading rules to a `/candidates` folder based on loss clusters). — NOT STARTED.

### Phase 6C: Knowledge Graph Reasoning Layer — 🟡 READER DONE (2026-07-07)
* ~~Build `src/graph_engine.py`: a read-only `GraphEngine` loading the additive `graph_edges` table from `data/brain_map.db` into a `networkx.DiGraph`, with `get_relevant_context(node, max_hops=2)` (2-hop BFS, confidence-sorted).~~ ✅ DONE — memory-resident, never writes during inference (decision #33); `tests/test_graph_engine.py`.
* ~~Wire the Memory Query into the proposal path so linked historical patterns ride along in the Discord PROPOSAL ALERT rationale.~~ ✅ DONE in `src/options_proposer.py` (fail-safe 🧠 Memory block; advisory only, decision #26 philosophy). Query now seeds on ticker + view + strategy so concept-keyed causal edges surface.
* ~~Teach `src/sleep_phase.py` to WRITE causal edges into `graph_edges`.~~ ✅ **Phase 6D DONE 2026-07-07** — Task D `write_causal_links` mines `(subject)-[predicate]->(object)` triples from reviewed outcomes + post-mortems only (decision #34), confidence 1.0, idempotent; `local_parser.extract_causal_triples()` + `tests/test_causal_writer.py`. `networkx` added to `requirements.txt`. Populates once trades resolve and a Sleep Phase runs with Ollama up.

### Phase 7: The Time-Travel Simulator — ✅ DONE AND VALIDATED ON REAL DATA (2026-07-07)
* ~~Build `src/simulator.py` to override `datetime.now()` and loop over historical DhanHQ data.~~ ✅ Built with **as-of-date injection instead of `datetime.now()` monkeypatching** (the safer path recorded as a caveat when this phase was planned — decision #36): per historical day it computes the same SMA/RSI analysis over only the closes known then, and drives the REAL `options_proposer.build_proposal()` (regime map, VIX gate, max-loss sizing) with historical VIX + a synthetic option chain (premiums modeled — historical chains aren't retrievable). Run: `python3 -m src.simulator --start YYYY-MM-DD --end YYYY-MM-DD [--underlying "NIFTY 50"] [--skip-causal]`.
* ~~Instantly fast-forward plans to resolution to populate the Brain Map without waiting months in real-time. Use a simulated portfolio to protect the live paper state.~~ ✅ Resolution reuses `plan_tracker`'s pure helpers, so exits + the FULL 2026 friction stack are byte-identical to live. Results land idempotently (deterministic `sim:<hash>` journal_refs) in the new `simulated_trades` table + the standard `outcomes`/`events`/links — which the Sleep Phase's causal writer (decision #34) then turns into `graph_edges`. The real journal/portfolio are never touched (runtime-spied in `tests/test_simulator.py`); the simulated book is a plain dict.
* ✅ **Validated end-to-end on real DhanHQ history same day** (NIFTY 50, 2025-07-01 → 2026-06-30): 56 iron-condor proposals resolved (48 wins / 8 losses), `brain_map.db` populated from empty to 182 events / 56 outcomes / 168 links, and the causal writer minted the graph's first two real edges (`iron_condor RESULTS_IN win` / `RESULTS_IN loss`, confidence 1.0). See the production-state bullet above for full figures. **Not just built — proven working.**
* Still open (Phase 7b): a training script that fits the Phase 11 skeptic's Random Forest on `simulated_trades` rows and saves `data/skeptic_model.pkl` (the table already stores every `FEATURE_NAMES` input + the win/loss label). **Blocked on the DhanHQ auth debt below** — Phase 7b will want to simulate a much larger date range for a meaningful training set, and the current token/renewal setup can't sustain that unattended.

---
## 📋 Pending Phases
Estimated Sequencing: **Cross-Asset Integration (Asset Expansion) ➔ Dual-Horizon Sentiment (Dual Sentiments) ➔ ATR-Based Trailing Stoplosses (Trailing Stoploss)**

These upcoming features are officially added to the roadmap:

### 1. Cross-Asset Integration (Asset Expansion)
* **Objective:** Expand the data layer and ingestion pipeline to fully support MCX Commodities (Gold, Crude Oil) and Global Indices.
* **Details:** Leverages the DhanHQ API migration to fetch real-time and historical data for these instruments, enabling diversified multi-asset paper trading without additional third-party data feeds.

### 2. Dual-Horizon Sentiment (Dual Sentiments)
* **Objective:** Upgrade `news_processor.py` to support dual-horizon JSON outputs.
* **Details:** Separates news sentiment analysis into `short_term_catalyst_score` and `long_term_macro_score`, feeding distinct granular durations into the Brain Map.

### 3. ATR-Based Trailing Stoplosses (Trailing Stoploss)
* **Objective:** Upgrade the `plan_tracker` to implement dynamic, volatility-adjusted trailing stops.
* **Details:** Replaces rigid bracket orders with dynamic, ATR-buffered trailing stops to protect capital while letting profitable swing trends run.

### 4. Regime-Aware Memory
* **Objective:** Add regime tags to the Brain Map's event-outcome links.
* **Details:** Captures and links current market regimes (e.g., trend, volatility, regime type) to trades so the learning loop can query patterns specifically under matching market conditions.

### 5. Procedural Evolution
* **Objective:** Support human-in-the-loop candidate generation for rule changes.
* **Details:** Evaluates post-mortem clusters of losses in `src/analyst.py` and proposes rule adjustments to a `/candidates` folder for user review, driving iterative rule enhancement.

---
## 🔮 The Long-Term Vision (Phases 9 - 13)
(To be executed only after Phase 7 Simulator proves statistical Alpha)

### Phase 9: Secure Web Exposure & UI Deployment
* ~~Expose GCP VM API to the internet securely via Cloudflare Tunnel with API-key middleware to connect the React dashboard and Discord bot.~~ ✅ **DONE 2026-07-07, end to end**: `src/api_server.py` (strict fail-closed `x-api-key` gateway wrapping the full `src.api` app) + two-way Discord bridge `POST /api/discord/action` (approve/reject pending journal entries by `short_id`, `--review-pending` semantics). On the VM, `alpha-trading.service` runs the gateway on `127.0.0.1:8000` and a new `cloudflared-tunnel.service` forwards a public quick-tunnel URL to it (`Restart=always`, enabled on boot). Verified live from an outside network: health check + the Discord bridge both round-trip correctly. Still open: this is a quick tunnel, so the URL changes on restart — upgrading to a named tunnel (permanent URL) needs a Cloudflare-registered domain; and the React dashboard / Discord bot aren't yet pointed at the tunnel URL (the bot in particular has no button/command calling the bridge endpoint yet).

### Phase 10: Local LLM "Maker/Checker" (Hallucination Guardrails)
* Run a local open-source model (Llama 3 / Phi-3) on the local Mac as a strict auditor.
* Validate Gemini's cloud-generated plans against raw data to catch logical contradictions before Brain Map logging.

### Phase 10B: Local LLM Episodic Event Extractor (NOT the same as Phase 10 above — FULLY BUILT + CRON'D 2026-07-06)
A separate use of a local LLM from Phase 10's "maker/checker" auditor — this one is a text-to-structured-data parser feeding the Brain Map, not a plan validator. **All four steps below are built** (`src/local_parser.py`, `src/sleep_phase.py`, tests), Ollama + `llama3` are installed on the host, and the Sleep Phase is scheduled via `scripts/setup_cron.sh` (20:00 IST daily → `logs/sleep_phase.log`).

**Architectural rule this phase is built on:** an LLM (local or cloud) must NEVER be used for continuous 24/7 market monitoring — checking whether a price crossed a level or a moving average is pure math and belongs in `src/rules.py` / `src/dhan_client.py` on the VM, exactly as today. Using an LLM for constant price polling would be a massive, pointless compute cost. A local LLM's only job here is the "light work" of turning unstructured text (news, Discord chat, journal summaries) into structured JSON for the Brain Map — never live price decisions.

Planned build (when explicitly greenlit, one file at a time, offline-first, native `sqlite3` only — same discipline as every other phase):
1. **Ollama on the Mac** — install it as a free local model server (e.g. Llama 3 8B or Phi-3). Add `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`) to the env-loading logic (`src/config.py` or equivalent), OpenAI-compatible API.
2. **`src/local_parser.py`** — an "Episodic Event Frame (EEF) Extractor": one function that takes raw text (e.g. a news headline) and returns strict JSON `{"event_type": str, "tag": str, "sentiment": int, "entities": list}` — no conversational output, a narrow structured-extraction task only.
3. **Wire into `src/brain_map.py`** — feed that JSON into the `events` table via the existing `record_event()`/`_get_or_create_event()` helpers, additive only (decision #25's rule still applies — no execution or portfolio access).
4. **Async "Sleep Phase" loop** — runs off-market hours only, so local LLM inference never competes with the live trading loop; distills the day's raw text into Brain Map events in the background.

### Phase 11: The "Skeptic Agent" (Multi-Agent Debate) — 🟡 SCAFFOLDING BUILT (2026-07-07)
* ~~Introduce a dedicated Skeptic Agent to counter the primary Analyst's long-directional bias.~~ **Quantitative half scaffolded**: `src/skeptic_agent.py` (`RandomForestAuditor`) — frozen 10-feature vector merging knowledge-graph evidence + the proposal's market numbers, wired into the proposer so a low modeled P(win) appends a "⚠️ Skeptic Agent Warning" to the Discord alert. **ABSTAINS until the Phase 7 simulator trains `data/skeptic_model.pkl`** (decision #35 — no fake warnings from an untrained forest); advisory only, never gates.
* Still open: training the model (blocked on Phase 7), and the original multi-agent structural-debate idea (an LLM skeptic arguing the counter-case) if still wanted once the numerical auditor is live.

### Phase 12: The Intraday Trading Loop
* Transition from hourly/daily OHLC swing-trading to a real-time streaming websocket architecture for rapid same-day fetch-decide-execute loops.

### Phase 13: Live Broker Execution
* Remove the strict "Paper-Trading Only" guardrail.
* Connect DhanHQ /v2/orders execution endpoints to route real capital to the NSE.

---
## 🌐 Future Frontiers
(Architecture documented ahead of the build — not started, not scheduled)

* Phase 8: Semantic News Ingestion (Spec fully defined in docs/PHASE_8_NEWS_INGESTION_SPEC.md).
---
