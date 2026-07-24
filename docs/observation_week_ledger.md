# Observation Week Ledger (2026-07-09 → triage)

Running log of every operational anomaly, error, and hotfix during the
observation week. One entry per issue, **verified against logs/DBs before
being written** — this file feeds next week's triage review, so no
unconfirmed claims. Newest issues appended under their date.

Conventions: `Symptom` = what was observed (Discord/logs), `Root cause` =
confirmed mechanism, `Resolution` = what was actually done + commit ids,
`Follow-up` = anything the triage should still decide.

**What code was live at the time:** since 2026-07-13 every service
startup on the VM appends its running commit to `logs/deploy_log.jsonl`
(VM-local, git-ignored) — view with `python3 -m src.deploy_log` on the
VM. New issue entries should cite the sha that was live when the
symptom appeared, so triage can tell "broke after deploy X" apart from
"was always broken". Deploys before 07-13 predate the log; the only
verified deploy timestamp is the full scratchpad-phases deploy Fri
2026-07-10 ~21:45 IST (per HANDOVER) — the 07-12 ops fixes' exact
deploy time went unrecorded, which is precisely the gap this log closes.

---

## Date: 2026-07-09

## Issue 5 — Mid-session token death (DH-906 again) after the timezone fix
- **Symptom:** ~1h after the manually-launched 11:00 IST session started
  successfully, `master_scheduler.log` began repeating "no market state
  this cycle" with `DH-906 Invalid Token` underneath, at ~12:08 IST.
- **Root cause:** confirmed the token ON DISK was actually valid the
  whole time (a fresh process using it worked immediately) — the
  RUNNING session process had simply loaded an older, since-superseded
  token into memory at its 11:00 IST startup and never re-reads `.env`.
  **What superseded it remains unidentified**: `renew_token.log` was
  empty (cron never fired), no code path in this repo auto-calls
  `renew_token`, and the Mac's own token was untouched — so some
  renewal outside our tracked automation happened around 12:00 IST.
  Given decision #48 (Dhan allows one active token per account), any
  external renewal — a Dhan mobile/web app login, for instance — would
  produce exactly this symptom.
- **Resolution:** killed and relaunched `master_scheduler` so it picked
  up the current valid token. (Process-management note: `pkill -f
  master_scheduler` / `kill $(pgrep -f master_scheduler)` twice killed
  the SSH shell itself, since the remote command's own text contains
  that string and `-f` matches full command lines — use `ps -eo
  pid,comm,args | awk '$2=="python3"'` to target only the real process
  next time.) One genuine bright spot: the scheduler's SIGTERM handler
  fired exactly as designed both times — clean "session stopped" log
  line, no corrupted state.
- **Follow-up:** if this recurs, it's worth adding a periodic re-read of
  `DHAN_ACCESS_TOKEN` from `.env` inside the long-running session
  instead of loading it once at startup — would make the process
  self-healing against any future external-renewal race.

## Issue 6 — `get_expiry_list` double-nesting silently blocked EVERY proposal
- **Symptom:** even once Issue 5's fresh token was flowing, the loop
  logged "no proposal (no usable expiry (need >= 7 days out))" every
  cycle, for both underlyings, despite NIFTY 50 having 18 listed
  expiries and the nearest qualifying one (`2026-07-21`) being 12 days
  out — well past the 7-day minimum.
- **Root cause:** Dhan's actual SDK response for `expiry_list` is
  doubly nested — `{"data": {"data": [...dates...], "status": ...}}` —
  but `dhan_client.get_expiry_list` only unwrapped one layer, handing
  `pick_expiry` a dict instead of a list. Iterating a dict yields its
  KEYS ("data", "status"), neither of which parses as a date, so
  `pick_expiry` silently matched nothing and returned `None` on every
  call. This looked exactly like "the market's just quiet" but was
  actually blocking every proposal outright, all day, regardless of
  setup quality — likely broken since whenever Dhan's API took on this
  shape, not just today.
- **Resolution:** commit `5fe5647` — unwrap defensively (handles the
  current double-nested shape, a plain single-nested shape in case Dhan
  reverts, and degrades to `[]` on anything else). New test file
  `tests/test_dhan_client.py` (6 tests; no dhan_client tests existed
  before this). Deployed to the VM mid-session; verified live —
  `get_expiry_list("NIFTY 50")` now returns 18 real dates and
  `pick_expiry` correctly selects `2026-07-21`.
- **Follow-up:** watch today's remaining cycles for a real proposal
  firing now that both blockers (token + expiry parsing) are cleared —
  a quiet rest-of-day is now genuinely "no qualifying setup," not a bug.

## Issue 1 — No trading session / no Approve-Reject cards this morning
- **Symptom:** No 🟢 session-open card at 09:15 IST and no proposal cards.
  The overnight ops card arrived at 02:00 IST labeled "20:30". The 07:00
  token renewal and 08:00 suggestions also hadn't run by mid-morning.
- **Root cause:** The VM's system clock is (was) UTC, and **Debian's stock
  `cron` does not support the `CRON_TZ=Asia/Kolkata` line** that
  `scripts/setup_cron.sh` relies on (that works on cronie/RHEL-family
  only — the script's own comment claiming "any VM" was wrong). Every
  "IST" schedule silently fired 5h30m late: the "09:10 session" was
  actually scheduled for 14:40 IST, the "20:30 ops sweep" ran at 02:00
  IST, etc.
- **Resolution:** `sudo timedatectl set-timezone Asia/Kolkata` on the VM +
  `systemctl restart cron` (11:00 IST) — cron's clock now IS IST, making
  the CRON_TZ line harmless. Today's already-missed session was launched
  manually at 11:00 IST (`nohup venv/bin/python3 -m src.master_scheduler`)
  and confirmed running (entry + exit loops armed). ~1h45m of today's
  market window was lost (09:15–11:00).
- **Follow-up:** none needed if tomorrow's cards appear on schedule;
  optionally the setup script could assert the host timezone at install.

## Issue 2 — The same trade-closed cards repeating every hour
- **Symptom:** Identical "Stop-Loss Hit — TCS (Rs.-775)" and "Trade
  Closed — MARUTI (Rs.+32,098, MISSED GAIN)" embeds posted at 08:30,
  09:30, 10:30 IST (and hourly before that).
- **Root cause (two bugs interlocking):** (a) the tracker's digest
  formatter crashed on a legitimate `r_multiple=None` (hypothetical
  resolution of the rejected MARUTI entry) — `NoneType.__format__`;
  (b) the journal rewrite lived at the very END of the sweep, so the
  crash meant outcomes were computed and broadcast but never SAVED. The
  api's hourly auto-sync loop then re-resolved and re-announced the same
  trades every hour (`[Auto-Sync] refresh failed … NoneType.__format__`
  in journalctl). A stale code comment even claimed the outcome "is
  already written above" — it wasn't.
- **Resolution:** hotfix commit `f8245f3` — None-safe digest formatting
  (`_fmt_signed`, renders "n/a") and `journal.rewrite_all` immediately
  after EACH resolution in both sweeps (a broadcast resolution can never
  be un-resolved by a later crash). Deployed to the VM, service
  restarted, and one muzzled tracker pass persisted the stuck outcomes
  (TCS loss, MARUTI missed-gain, plus an ONGC "GOOD SKIP" that had been
  invisibly wedged behind the same crash). Regression test added.
- **Follow-up:** none — verified: all journal outcomes persisted, only
  the live ONGC.NS position remains open.

## Issue 3 — Yesterday's DH-906 "Invalid Token" flood in suggest.log
- **Symptom:** The 21:40 IST (2026-07-08) ops card quoted dozens of
  DH-906 / connection-reset errors from `suggest.log` (Mac).
- **Root cause:** Two FORGOTTEN Phase-1/2-era macOS LaunchAgents
  (`com.alphatrading.dailyalert`, `com.alphatrading.dailysuggestions`)
  were still running `src.main`/`src.suggest` on the Mac daily with the
  Mac's dead token — the Mac token had been invalidated by the VM's
  renewal, because **DhanHQ allows only ONE active token per client id**
  (decision #48; the same fact that forced removing the Mac's renew/push
  crons at ~00:30 IST today).
- **Resolution:** both LaunchAgents unloaded and archived
  (`~/Library/LaunchAgents/retired-alphatrading/`), Mac crontab emptied.
  The Mac now runs NOTHING scheduled except the edge-miner agent.
- **Follow-up:** lesson recorded — a Mac task audit must include
  `launchctl list`, not just `crontab -l`.

## Issue 4 — Sleep phase "Ollama call failed: Connection refused" (VM)
- **Symptom:** the 02:00 IST ops card flagged
  `sleep_phase.log: (local parser: Ollama call failed: [Errno 111])`.
- **Root cause:** **expected behavior, not a bug** — the VM has no Ollama
  (1GB RAM) by design (decision #47); the sleep phase there degrades to
  the decay-only pass, and ingestion correctly skipped 5 duplicates.
  Causal mining runs from the Mac opportunistically instead.
- **Resolution:** none needed. Noted so nightly cards quoting this line
  aren't re-triaged. (A quieter log message on the VM is a triage-week
  candidate if the noise annoys.)

## Issue 7 — Analyst "/analyze" reports missing history for TCS.NS (message blames Yahoo; it's actually Dhan)
- **Symptom (user-reported):** Discord `/analyze` returned "TCS.NS: not
  enough price history to forecast (needs 200+ trading days on Yahoo
  Finance)" for a mega-cap with decades of history.
- **User's proposed root cause:** Yahoo Finance rate-limiting/blocking the
  GCP VM IP, returning empty dataframes (cf. the earlier TATAMOTORS
  "possibly delisted" Yahoo error in run.log).
- **VERIFIED root cause — DIFFERENT from the proposal (flagged to user
  2026-07-09):** the "on Yahoo Finance" text is a STALE HARDCODED STRING
  in `src/discord_bot.py:111`, never updated during the 2026-07-06
  yfinance→DhanHQ migration. The real data path for `/analyze` is
  `forecast() → suggestions.analyze() → dhan_client.get_daily_closes()`
  — pure DhanHQ (confirmed by reading the source; `suggestions.py:13`
  imports `get_daily_closes`, no yfinance). So this failure is a **DhanHQ
  fetch returning <200 closes for TCS.NS**, NOT Yahoo blocking. Consistent
  with today's confirmed Dhan trouble (the DH-906 token deaths, Issues 5
  & the suggest.log connection-reset flood) rather than a Yahoo issue.
  The precise reason Dhan returned short history for TCS.NS specifically
  (rate-limit vs. a SECURITY_ID_MAP gap for `TCS.NS` vs. a token blip at
  that moment) was NOT chased — requires a VM log check, deferred to
  triage to honor the standby directive.
- **NOTE on the TATAMOTORS "delisted" error:** that one came from a
  DIFFERENT, still-yfinance code path (real Yahoo error text in run.log),
  so a residual yfinance dependency DOES appear to exist somewhere — but
  it is NOT the source of this TCS.NS `/analyze` error. Don't conflate
  them at triage.
- **Proposed triage fix (revised):** (a) fix the lying error string in
  `discord_bot.py` first — it actively misdirected diagnosis; (b) then
  investigate why Dhan returned short history for TCS.NS (check
  SECURITY_ID_MAP coverage + the rate-limit retry in `get_daily_closes`);
  (c) SEPARATELY, hunt down and migrate the residual yfinance path that
  produced the genuine TATAMOTORS Yahoo error. The user's "migrate off
  Yahoo" instinct is right for (c) but moot for this specific issue,
  which is already on Dhan.
- **DO NOT FIX THIS WEEK** — logged for triage only (user directive).

## Issue 8 — Session restart resets the in-memory cool-down → duplicate proposals (positions doubled)
- **Symptom:** four approved positions at day's end instead of two — the
  12:16 IST session proposed NIFTY 50 + NIFTY BANK spreads (`25da25ec`,
  `7b84bd44`, user approved), then after the 12:34 restart (deployed the
  expiry-parser hotfix) the NEW session immediately re-proposed both
  indices (`1d796dd6`, `af18c8cf`, also approved).
- **Root cause:** `market_loop.CooldownRegistry` is **in-memory only** —
  a restarted session has no memory of proposals the previous process
  made minutes earlier, so the 2h-per-index cool-down silently resets.
  Any mid-session restart (crash, deploy, token refresh) can double
  positions.
- **Impact:** contained by design — the 6G margin gate priced and locked
  all four honestly (₹79,942.75 total, ~8% of pool), and the user did
  explicitly approve all four cards. But the doubling was unintended
  and would scale badly with more restarts.
- **Resolution:** none this week (no-build boundary). **Triage fix
  candidate:** persist the cool-down (e.g. derive it from the journal —
  "was a proposal for this underlying journaled in the last 2h?" — which
  survives restarts with no new state file).
- **Follow-up:** queued for triage; positions themselves are fine.

## Day-1 wrap — 20:30 IST ops sweep triage (all 10 lines accounted for)
- ⏰ renew_token / suggest "did not run today" → **Issue 1** (the
  timezone fix landed at 11:00 IST, after both jobs' 07:00/08:00 IST
  slots had already passed). One-time; both fire correctly tomorrow.
- `master_scheduler.log` DH-906 ×2 → **Issue 5** (the morning token
  death, pre-fix lines; first VM sweep since 02:00 IST reports the whole
  day). No DH-906 after the 12:34 restart.
- "option chain unavailable" ×2 → **Issue 6** (pre-hotfix lines, same
  recap effect).
- `sleep_phase.log` Ollama refused ×5 + `failed: 4` → **Issue 4**
  (expected VM degradation, no Ollama there). The 4 "failed" ingestions
  are today's new journal entries awaiting LLM ingestion — which
  currently happens NOWHERE, because the Mac-side edge miner is still
  blocked on the Full Disk Access grant (see 2026-07-08 note). The
  system trades fine; it just isn't learning until that one-click grant
  happens.
- **Afternoon health confirmed independently:** session self-completed
  cleanly at 15:30 IST (first full graceful close on the VM), proposals
  fired normally post-fix, and the live bridge fired its **first-ever
  real-time exit advisory** — NIFTY 50 spread at 93% of max profit
  intraday. Positions stay open per design (spreads resolve on daily
  bars, never same-day); the tracker acts on tomorrow's data.
- **Token runway verified for tomorrow:** on-disk token expires 12:00
  IST 2026-07-10; the (now correctly-timed) 07:00 IST renewal precedes
  both the expiry and the 09:15 open. (That expiry timestamp also dates
  the Issue-5 mystery renewal to ~12:00 IST today — consistent with the
  12:35 IST residential-IP SSH login; still unattributed, still open.)

## Issue 9 — Edge miner's first "successful" run was a silent no-op (third unpinned-interpreter incident this week)
- **Symptom:** after the user granted `/bin/bash` Full Disk Access (fixing
  Issue "edge miner TCC block" from 2026-07-08), the LaunchAgent run
  completed with `status: ok` — but line 1 of its output was
  `(local parser skipped: httpx not installed)` and it extracted
  **0 patterns from 10 outcomes**.
- **Root cause:** `scripts/mine_edges.sh` resolved `python3` from PATH,
  and its own PATH export puts `/opt/homebrew/bin` (which contains a
  bare, package-less Homebrew python3) ahead of the Framework python.
  **Third variant of the same disease in 48 hours:** Mac cron resolved
  CommandLineTools python (2026-07-08), VM cron needed the venv path,
  now the LaunchAgent resolved Homebrew python. Manual terminal runs
  always worked because interactive shells order PATH differently —
  which is exactly why this class of bug survives testing.
- **Secondary finding (honesty gap, triage item):** the miner reported
  `"status": "ok"` while its LLM extractor was completely
  non-functional. Its guard checks the Ollama *server* (stdlib urllib —
  passed) but not whether the extractor itself can make calls (httpx —
  absent). An "ok" that silently did nothing defeats the ops-monitor
  heartbeat model. Triage fix: the miner should verify the extractor
  end-to-end in its guard and report `skipped: extractor unavailable`.
- **Resolution:** interpreter PINNED in `mine_edges.sh` (absolute
  Framework-python path, never `python3`-from-PATH) with an in-file
  comment naming all three incidents. Verified with a forced full run:
  **10 outcomes considered → 12 triples mined → 5 new edges applied to
  the VM's live graph (3 → 8)** — the first genuinely end-to-end
  learning cycle in the system's history, and also the first live
  exercise of the ship-as-file remote-apply path (fixed 2026-07-09
  early hours) with a non-zero payload. Cross-checked on the VM: all 8
  edges present, including newly mined semantic links
  (`fresh_cross CONTRADICTS bullish_thesis` from the TCS stop-loss
  post-mortem).
- **Standing lesson for triage:** every scheduled/agent-launched entry
  point in this system must invoke its interpreter by ABSOLUTE PATH.
  A sweep of all remaining launchers for bare `python3` is a cheap,
  high-value triage-week item.

## Context for triage (not an issue)
- The ops sweep's "silent job" heartbeats from the 02:00 IST card
  (renew/suggest/main/master_scheduler "did not run today") were all
  downstream of Issue 1's timezone shift, not independent failures.
- Issue numbering here is chronological-append, not the sequence the
  user quotes in chat — this "Issue 7" is what the user called the
  forecast/"Issue #3" report on 2026-07-09.

---

## Date: 2026-07-10

## Context for triage (not an issue)
- Today's two approved journal entries (`f0ae401e` NIFTY 50,
  `2df15c4d` NIFTY BANK) carry `"why": "Test"` — confirmed with the
  user this is an intentional label they're entering by hand during
  the observation week, not a pipeline bug or default string. Noting
  it here so a future triage pass doesn't misread it as a defect.

## Issue 10 — 07:00 IST token renewal failed ("Invalid TOTP"); real cause is a second, undocumented renewal cron racing the documented one
- **Symptom:** `logs/renew_token.log` (the officially documented
  07:00 IST renewal job, per [[project_gcp_vm_deployment]]) shows a
  failure this morning: `Token renewal failed: no token in Dhan's V2
  reply — .env left untouched`, with Dhan's response `{"message":
  "Invalid TOTP", "status": "error"}`.
- **Root cause — CONFIRMED, two independent renewal crons exist on
  the VM, not one:** (a) the documented user-crontab job, `0 7 * * *`
  → `src.renew_token` → `logs/renew_token.log`; (b) a SEPARATE,
  previously-undocumented **root** crontab entry
  (`/var/spool/cron/crontabs/root`, file dated 2026-07-06 17:16 —
  predates the observation week, evidently a leftover from initial
  Phase 9 deployment that was never recorded in project memory)
  running `src.renew_token.py` every 12h at local 00:00 and 12:00 IST
  (`0 */12 * * *`; root's crontab has no `CRON_TZ` line, but the VM's
  system timezone is now Asia/Kolkata per Issue 1's fix, so the
  schedule resolves to IST wall-clock — confirmed via `timedatectl`
  and a matching `journalctl` cron-fire entry at
  `2026-07-09T18:30:01 UTC` = `00:00 IST`), logging to a separate file
  (`~/renew.log`) and always chaining `systemctl restart alpha-trading`
  after a successful mint. Both crons independently call the same Dhan
  V2 PIN+TOTP mint endpoint against the same account. Confirmed via
  `renew.log`: the root job's 00:00 IST run succeeded ("Token renewed
  successfully. New expiry: 2026-07-10T12:00:02") — hours before the
  documented 07:00 IST job ran and got rejected. The exact rejection
  mechanism (TOTP single-use/replay window vs. Dhan-side lockout from
  two callers sharing one TOTP secret) was NOT chased further —
  deferred to triage.
- **Impact:** none observed — `renew_token.py` fails closed (`.env`
  left untouched on failure), and the token was already fresh from the
  root job's midnight renewal, so the 07:00 failure never left a stale
  token in place. Also confirmed: `systemctl restart alpha-trading`
  only restarts the FastAPI gateway (`uvicorn src.api_server:app`,
  verified via `ps` as a distinct PID from `master_scheduler`) — it
  does NOT touch the trading loop process, so this does not carry
  Issue 8's cooldown-reset risk. It would, however, cause a
  few-second Discord-bridge/API outage if the 12:00 IST firing lands
  mid-market-hours (09:15–15:30 IST) — worth a same-day watch, not
  confirmed either way as of this writing (10:09 IST, before today's
  12:00 firing).
- **Resolution:** none this week (no-build boundary). Logged here for
  triage.
- **Follow-up:** triage should decide whether to (a) remove the
  undocumented root cron now that it's identified, keeping only the
  documented 07:00 IST path, or (b) formally adopt the 12h cadence and
  retire the 07:00 one — but NOT keep both, since decision #48
  established that any redundant renewal against the same Dhan account
  races the other, and this morning's failure is consistent with
  exactly that race.

## Issue 10 — UPDATE 2026-07-10 ~13:35 IST: the 12:00 IST firing DID land mid-session and BLINDED the live trading loop (Issue 5 recurrence). The morning "impact: none observed" assessment was incomplete.
- **Symptom (verified now, read-only):** today's running
  `master_scheduler` session (log line 45 `[Scheduler] session open`,
  started ~09:10 IST) is logging `[Market Loop] NIFTY 50: no market
  state this cycle` / `NIFTY BANK: no market state this cycle`
  continuously on every cycle in `logs/master_scheduler.log`, with a
  `DH-906 Invalid Token` on the Dhan data path. The loop is fetching
  no market data — so no entry proposals and no Live-Bridge advisory
  exit alerts on the 4 open spreads for the rest of today's session.
- **Root cause (mechanism confirmed; onset-minute inferred):** the
  on-disk `.env` token is CURRENTLY VALID (decoded JWT `exp`
  1783751402, in the future) — so this is NOT bad-token-on-disk. It is
  Issue 5's stale-in-memory token: the deployed VM code predates the
  scratchpad's `token_provider` live-`.env`-reread, so the process
  keeps the token it loaded at 09:10 startup and never re-reads. Per
  `renew.log`, renewals landed at expiry `2026-07-11T00:00:02` (minted
  ~12:00 IST today) and `2026-07-11T12:00:02` — i.e. the root cron's
  12:00 IST firing (flagged in the entry above as "worth a same-day
  watch") minted a fresh token, which under decision #48 invalidated
  the one the 09:10 process holds → DH-906 → blind. Exact blind-onset
  minute not pinned (the loop only logs the negative "no market state"
  line, so there is no positive mark to bracket against), but it is
  consistent with the ~12:00 renewal.
- **Correction to the morning Impact note:** that note scoped the
  12:00 firing's risk to "a few-second Discord-bridge/API outage" from
  the chained `systemctl restart alpha-trading`, and "none observed"
  for the trading loop. That under-counted the failure: the renewal
  ALSO mints a new token, and via Issue 5 that silently blinds the
  separately-running `master_scheduler` for the remainder of the
  session. The duplicate-root-cron race therefore has a second, larger
  failure mode than the gateway blip — it takes the live loop offline
  every afternoon the 12:00 renewal fires mid-session.
- **NOT caused by this session's Mac dashboard work:** a Mac-side task
  copied the VM's current (valid) `.env` token to the Mac at ~13:21
  IST for read-only local quotes. That is a READ — it mints nothing
  and cannot invalidate a token; the loop's DH-906 is renewal-driven
  and independent, and the ~12:00 onset predates the copy. Verified
  the copied token still returns live quotes from a fresh process, i.e.
  the token is good — only the long-running VM process cannot see it.
- **Resolution (HOTFIX APPLIED 2026-07-10 ~15:24 IST, on the user's
  explicit "fix this asap" instruction — config-only, no code deployed,
  no service restarted, freeze on code otherwise intact):** stopped the
  recurrence by making sure no token mint can ever land mid-session,
  while keeping the renewal path that is actually proven to work:
    1. root's renewal cron rescheduled `0 */12 * * *` (00:00/12:00 IST;
       the 12:00 firing is the blinder) → `30 6,18 * * *` (06:30/18:30
       IST, both outside 09:15–15:30). Command byte-identical, only the
       schedule field changed; its chained `systemctl restart
       alpha-trading` now also lands off-hours only (kills the Issue-10
       gateway-blip concern too).
    2. the documented 07:00 IST user-cron renewal DISABLED (commented
       in place, not deleted): it failed this morning with `Invalid
       TOTP`, and per decision #48 / docs/token_renewal_cadence.md two
       schedules racing one Dhan account is the underlying disease.
       Single renewal now = root's, at safe hours. This inverts the
       cadence doc's deploy-day plan (which keeps 07:00 and removes
       root) — deliberately, because the retry hardening that makes the
       07:00 job trustworthy is still undeployed; triage flips it back
       when that ships.
  Backups on the VM: `~/root_crontab.bak-20260710-152339`,
  `~/user_crontab.bak-20260710-152339` (restore = `sudo crontab
  <file>` / `crontab <file>`). Today's blinded `master_scheduler` was
  deliberately NOT restarted (~15 min to close, defined-risk spreads);
  it self-terminates at 15:30 by design and tomorrow's 09:10 launch
  reads the valid on-disk token.
- **Follow-up:** (a) ✅ VERIFIED 2026-07-10 ~20:35 IST — the rescheduled
  cron's first firing at 18:30 IST succeeded: `~/renew.log` shows "Token
  renewed successfully. New expiry: 2026-07-11T18:30:02", the `.env`
  token decodes valid (~21h left), and `sudo crontab -l` confirms the
  `30 6,18` schedule. (The Invalid TOTP that surfaced in tonight's 20:30
  ops sweep is the 07:00 USER job from THIS MORNING — `logs/
  renew_token.log` mtime 07:00:13 — now disabled, not a tonight failure;
  the ops sweep scans `logs/*.log`, not root's `~/renew.log`, so it can't
  see the successful 18:30 mint.) Still to watch: a clean full session
  post-deploy with NO "no market state" runs after 12:00; (b) triage still owns the real
  fix — deploy the self-healing token re-read + renewal retry
  (scratchpad Phase 1), then restore the documented single-07:00
  cadence and remove the root cron per docs/token_renewal_cadence.md;
  (c) note the Mac's copied dashboard token gets invalidated by the
  18:30 mint (expected; the phase-8 snapshot sync is the durable
  answer there, not token sharing).

## Issue 10 — RESOLVED 2026-07-10 ~21:45–22:00 IST: weekend deploy executed, single-07:00 cadence restored (all steps verified on the VM)

- **What ran (deploy, markets closed):** the 13 unpushed commits
  (`dfcdf9b` → `bf9dc77`) were pushed and pulled onto the VM
  (`git pull` fast-forward `e0dcfba` → `bf9dc77`), deps installed,
  `PAPER_AUTO_APPROVE=1` set in `.env` (decision from deploy-day
  choices), `scripts/setup_cron.sh` re-ran clean (IST assertion passed;
  7-job block incl. the 07:00 renewal + 2h report card installed),
  root's interim `30 6,18` crontab REMOVED whole (`sudo crontab -l` →
  "no crontab for root"; it held only the renewal entry — verified
  before removal; backups from the hotfix remain in `~`), all 3
  services restarted and active, regime backfill tagged 366/366
  simulated trades (bars cache scp'd from the Mac).
- **Verified working on the new build (not assumed):** manual
  `src.renew_token` run minted a REAL new token through the
  retry-hardened path (exit 0, expiry 2026-07-11T21:47, `.env`
  fingerprint changed, `.env.bak` written); a fresh
  `dhan_client.get_live_price("RELIANCE.NS")` returned 1307.8 on that
  token; the gateway kept answering keyed `/api/health` after the mint
  with NO restart; `view_positions` lists the 7 open paper spreads;
  Discord bot reconnected to the gateway; external checks THROUGH the
  quick tunnel from the Mac: keyed `/api/health` 200-ok,
  `/dashboard?api_key=` 200, unauthenticated `/dashboard` 401. New
  tunnel URL (rotated by the restart, expected):
  `https://generates-edgar-scored-cancel.trycloudflare.com`.
- **Still to watch (the only unproven pieces):** (a) Sat 2026-07-11
  07:00 IST — first CRON-fired renewal on the new code (check
  `logs/renew_token.log`); (b) Mon 2026-07-13 — first live session on
  the new build, especially a clean afternoon past 12:00 (the old
  blinding hour) and the in-session token_provider re-read under a real
  mid-session mint; (c) auto-approve behaviour (`/pending` stays empty
  by design — proposals journal straight to APPROVED).

## Issue 11 — FOUND AND FIXED 2026-07-11 ~11:30 IST: NSE deals fetch was broken three ways (caught on the first real backfill run)

- **What happened:** the first 3-year deals backfill attempt
  (HOLY_GRAIL Phase 1, run from the Mac) failed every window. Root
  causes, each verified by hand against nseindia.com before fixing:
  (1) the module's `/api/historical/{bulk,block}-deals` endpoints are
  RETIRED — they now serve an HTML challenge page / plain 503 even to
  a real browser fingerprint; the live endpoint is
  `/api/historicalOR/bulk-block-short-deals?optionType=...`;
  (2) that JSON API silently TRUNCATES every response to ~70 rows no
  matter how small the from/to window (a 1-day window still capped) —
  only the `&csv=true` download variant returns the complete window
  (763 vs 74 rows over the same test week);
  (3) NSE's homepage now 403s non-browser clients, and the daily
  pull's cookie warm-up ran INSIDE the same try as the API call — the
  403 would have aborted tonight's first VM 19:30 pull into snapshot
  fallback even though the API answers fine without cookies.
- **Fix (commit `4aac239`, deployed to the VM the same hour):**
  historical fetch switched to the csv=true endpoint with era-tolerant
  substring header mapping into the same `normalize_deal`; raw CSV
  windows archived to the lake as `.csv`; homepage warm-up made
  best-effort in the daily path; regression tests for all three.
- **Verified outcome (not assumed):** full 3-year crawl then ran clean
  from the Mac — 75,600 deals / 742 trading days spanning 2023-07-11
  → 2026-07-10, 0 failed windows; JSONL shipped to the VM; VM's
  entity-affinity ingest folded all 75,600 (742 new days) and
  projected 16 `concentrates_in` edges across 6 linked promoter
  groups, each carrying its true historical `valid_from` (as-of
  projection working — no born-today lie on 2023 links).
- **Also verified this morning (watch item (a) of Issue 10):** the Sat
  07:00 IST cron-fired renewal on new code WORKED — first attempt got
  "Invalid TOTP", the retry waited for the next TOTP window and minted
  clean (expiry 2026-07-12T07:00). The retry hardening earned its keep
  on its very first scheduled firing.

## Issue 12 — correlated duplicate-exposure pileup (2026-07-13, first live Monday)

- **Observed (verified against the VM's journal + margin_locks):** the live
  paper book held NINE open bear put spreads accumulated over three sessions
  (Jul 9 ×4 — user's own manual "Test" entries; Jul 10 ×3 and Jul 13 ×2 —
  engine proposals, the Jul-13 pair auto-approved). All nine expressed the
  same bearish index view (4× NIFTY 50, 5× NIFTY BANK), ~Rs.49.4k combined
  max loss, ~Rs.1.79L margin locked. At the 11:02 IST mark, 7 of 9 were
  underwater (combined open P&L ≈ −Rs.11.3k) while spot chopped sideways.
- **Root cause:** nothing between the 2h per-underlying cooldown and the
  margin gate inspects open positions at proposal time. The binary trend
  read (SMA50<SMA200) stays "bearish" across sessions, so each morning
  re-proposes the same trade; with PAPER_AUTO_APPROVE=1 the human judgment
  that used to catch duplicates is out of the loop (exactly the gap the
  deploy-day handover note flagged).
- **Fix (decision #68, built + tested this session, 25 new tests, suite
  949 green; NOT yet deployed to the VM at the time of this entry):**
  `src/exposure_gate.py` — one open spread per underlying+direction,
  enforced in `run_headless` before the margin gate, fail-open,
  sandbox-exempt; blocks ledgered to `logs/exposure_blocks.jsonl` with a
  once-per-day Discord note. Companion trend-flip exit advisory in the
  live loop (advisory only, one de-duped card per flip). Confidence-based
  trade prioritisation deliberately deferred to the Phase-4 harness.
- **Also this session (separate, minor):** the Jul-3 ONGC.NS "testing
  default suggestions" equity entry was removed from the VM journal
  (backup `data/journal.jsonl.bak-20260713-100420`); the Jul-9 "Test"
  spreads were left in place by user decision. A `rejected` spread
  (`f2b9edbd`, Jul 10) was found to have locked margin for ~2 minutes
  before releasing at Rs.0 — self-healed, logged here as a watch item on
  the reject path.

## Issue 13 — stale NSE lot sizes in the live engine (2026-07-15, research-audit catch)

- **Observed (verified 2026-07-15 against NSE lot-size bulletins):** the
  SEBI Jan-2026 index-derivatives revision cut lot sizes — NIFTY 50 from
  75 to **65**, NIFTY BANK from 35 to **30** — live since the Jan-2026
  contract series. `LOT_SIZES` in `src/options_proposer.py` still held the
  pre-revision `{"NIFTY 50": 75, "NIFTY BANK": 35}`, so for ~6 months the
  live proposer priced `max_loss` / `max_profit` / SPAN margin / lot
  sizing on contract sizes ~13–15% too large. Same-expiry defined-risk
  structures only, so no naked exposure resulted; the error was in the
  rupee economics and margin reservation, not in trade safety.
- **Root cause:** lot sizes are a hardcoded contract-spec constant (Dhan's
  option-chain payload carries no lot-size field to read dynamically), and
  the constant was written before the Jan-2026 revision. Surfaced by the
  Gemini deep-research regulatory audit (`docs/gemini_research_gap_analysis.md`
  §3), then confirmed against primary sources before changing code.
- **Fix (decision-free correctness patch, 2026-07-15):** `LOT_SIZES` →
  `{"NIFTY 50": 65, "NIFTY BANK": 30}` with a dated provenance comment;
  two `test_trade_planner.py` assertions updated (75→65, 35→30). Full
  suite 970 green (the one unrelated `test_market_loop` failure predates
  this change — separate task). The simulator uses the same current sizes
  for historical replays; this only scales absolute-rupee P&L, never the
  R-multiples/win-rates the validation harness scores (both lot-size-
  invariant), so the learning corpus is unaffected.
- **Also verified N/A in the same audit:** the 2% expiry-day ELM (we exit
  ≥2 days before expiry, never hold 0-DTE shorts), calendar-spread margin
  removal (we trade no calendar spreads), and BANKNIFTY weekly
  discontinuation (`pick_expiry` adapts to whatever Dhan serves — now
  monthlies for BANKNIFTY).

## Issue 14 — no proactive pacing on Dhan data calls ("DH-905 rate-limit", 2026-07-17, owner-reported)

- **Observed (code-verified 2026-07-17):** every Dhan API call site in
  `src/dhan_client.py` (`_fetch_daily`, `_quote_sec`, `get_expiry_list`,
  `get_option_chain`) fired back-to-back with no spacing between
  consecutive calls — the only defence was a retry-once after a 1.1s
  pause *inside* each call. With the tracked universe now at 18+ cash
  equities, watchlist loops hit Dhan's ~1/sec data-API limit on the
  first attempt of nearly every call, burning a rejection + 1.1s retry
  per instrument. Owner reported this as "DH-905" from a parallel
  session; note the DH-905 code itself is classed as auth/input in
  `src/dhan_guard.py` — the live symptom (blocked fetches during
  session) is what was fixed, the label is unconfirmed.
- **Fix (2026-07-17, pre-deploy):** module-level `_throttle()` in
  `src/dhan_client.py` — enforces a minimum `_RATE_PAUSE` (1.1s) gap
  since the previous Dhan call, process-wide, called in front of all
  four API call sites. Retry-once kept as the recovery layer.

## Issue 15 — stale symbols in the sector-expansion watchlist (2026-07-17, test-suite catch, deploy-blocking)

- **Observed (verified 2026-07-17 against Dhan's live scrip master):** the
  2026-07-16 sector-universe expansion added `LTIM.NS` and
  `TATAMOTORS.NS` to `config/watchlist.yaml` + `config/sector_universe.json`
  without SECURITY_ID_MAP entries. `test_every_watchlist_ticker_resolves...`
  failed (the only red in an otherwise 1087-green suite). Scrip-master
  lookup showed BOTH symbols no longer exist on NSE: Tata Motors demerged
  into TMPV (passenger vehicles, kept old id 3456) + TMCV (commercial
  vehicles, id 759782), and LTIM has no NSE EQ listing at all in the
  current scrip master.
- **Fix (2026-07-17, pre-deploy):** LTIM removed from both configs;
  TATAMOTORS → TMPV in the watchlist (EV-sector thesis), TMPV+TMCV in the
  AUTO sector basket; TMCV.NS added to SECURITY_ID_MAP with the verified
  id. Had this shipped, the live engine would have burned two unresolvable
  fetch slots every loop all session.

## Issue 16 — analysis decision-day derived from host timezone (2026-07-19, review-#2 follow-up, code-verified)

- **Observed (code-verified 2026-07-19):** `src/analysis/regime_filters.py`'s
  `_distribution()` computed its decision day with `datetime.date.today()` —
  the HOST timezone's date. The live engine runs on the GCP VM, which keeps
  UTC: between midnight IST and 05:30 IST, `date.today()` there returns
  *yesterday's* IST date, shifting the smart-money veto's 90-day deals window
  by one day. Materiality is low during market hours (the loop runs
  09:15–15:30 IST, when the two dates agree) — but the repo's own rule is
  that all timing is IST regardless of host (`market_loop`'s design note),
  and the analysis package claims strict point-in-time discipline. Found
  while writing the Department 8 test coverage mandated by review #2 —
  the package had ZERO dedicated tests when this shipped live in `6d89eb4`.
- **Fix (2026-07-19):** `_distribution()` now derives its default decision
  day from the shared IST clock (`market_loop.ist_now`), and `advise()`
  threads its existing `as_of` parameter through to `_distribution` so
  point-in-time callers pin the date explicitly. Pinned by
  `tests/test_analysis_signals.py` (a 01:00-IST clock must see an
  IST-yesterday deal that a UTC clock would miss) alongside the new
  58-test Department 8 coverage (`test_regime_filters.py` +
  `test_analysis_signals.py`).

- **Issue 16 addendum (2026-07-19, same bug class, second module):** the
  merge of the daily circuit breaker exposed that
  `src/portfolio_manager.py`'s `_now_iso()` also stamped host-timezone
  wall-clock (`datetime.now()`) into `margin_locks.locked_at/released_at`
  and `account_events.ts` — and the breaker's "today" boundary reads
  `released_at` back, so on the UTC VM a post-19:30-IST settlement would
  have landed on the wrong day. Fixed with the merge: `_now_iso()` now
  stamps IST wall-clock (naive format unchanged); test-pinned in
  `tests/test_margin_stress.py` (stamp prefix must equal the IST date).

## Issue 17 — needle grading aimed at printed page numbers, not extracted indices (2026-07-19, sniper-recon catch)

- **Observed (verified 2026-07-19):** the model-matchup benchmark graded
  "caught the eMudhra R&D needle" against pages 153-155 because the
  human benchmark JSON cites "page 154" — but that is the report's
  PRINTED page number. The ₹476.38 Mn product-development sentence
  lives on EXTRACTED page 156 (pypdf indexing; offset 2 from cover
  inserts). All three models were graded against a window two pages
  left of the target. Scope: confirmed for EMUDHRA FY26; the other
  benchmark reports' cited pages were content-verified against
  extracted indices during condenser tuning (VEDL 289/291, AZAD 120,
  NALCO 34-71) and matched.
- **Also established by the same sniper test:** the needle is NOT in a
  table — it is clean MD&A cash-flow prose with no "capitalised"
  keyword nearby. Layout-preserving extraction tripled the text and
  blew the 4096-token context; aimed at the single page with a
  targeted forensic-accountant prompt, llama3.2:3b quoted the correct
  sentence region verbatim but chose the headline cash-outflow figure
  — the analyst's finding requires the inference "investing outflow on
  product development = capitalized R&D," which is a synthesis step a
  3B does not make. The "synthesis wall" is real and now precisely
  located; the aim bug was ours.
- **Fix (2026-07-19):** `model_benchmarker.needle_checks` window
  corrected to extracted 154-158; saved bench outputs re-graded
  offline (verdict unchanged — llama3.2:3b's YES strengthens: it had
  validated findings ON extracted p156; phi3/qwen still zero there).

## Issue 18 — Issue 17's root cause also broke the human lake JSON itself, not just the benchmarker's grading window (2026-07-18, Chief Forensic Auditor acid test)

- **Observed (verified 2026-07-18):** Issue 17 fixed `model_benchmarker`'s
  grading window but never touched `data/lake/fundamental_reports/EMUDHRA/FY26.json`
  (analyst `claude-fable-5`) — the file the ledger entry itself was diagnosing.
  Running the mandated acid test (condense the eMudhra FY26 PDF, verify every
  citation's quote against the raw extracted page it names) found the
  contamination was worse than the one known page: of 6 checkable findings,
  only 1 (`Total Borrowings`, p344) had a citation that actually verified.
  The other 5: the capitalized-development flag cites p154 (real page 156 —
  Issue 17's exact bug, still live in the file); the unbilled-revenue flag
  cites p331 with an ellipsis-joined, non-verbatim "quote" (real sentence is
  on p268); the DSO flag cites p101 (real page 154 — ironically the SAME
  page number Issue 17 flagged as wrong for a *different* finding); the
  revenue-growth quote doesn't appear verbatim anywhere in the document at
  all (real page 55, but the JSON's exact text — hyphen instead of em-dash,
  "diversified" instead of the ligature-broken "diversifi ed" — was
  retyped, not copied); the director-resignation flag cites p310 (real page
  124). Method: `extract_pages()` on the source PDF + exact substring
  search against the RAW per-page text (not the condensed corpus) for every
  finding's `quote` field, cross-checked by re-running all 7 corrected
  findings through the pipeline's own `validate_findings()` guard (0 dropped).
- **Root cause:** unconfirmed for certain, but consistent with Issue 17's
  standing hypothesis — a manual read that tracked printed footer numbers
  rather than the extracted-page index pypdf actually assigns (this report's
  offset is not even constant: p154-printed→p156-extracted for one finding,
  p101-printed→p154-extracted for another, suggesting hand-transcription
  error compounded the footer/index mismatch, not a single fixed offset).
- **Impact:** advisory-only research lake data, never touched a live
  decision (Dept 8 iron rule) — no trading impact. But it is exactly the
  failure mode this department's "a finding without a quote does not
  exist" rule exists to prevent, and it was sitting in a file another
  session had already labeled an acid-test reference case.
- **Resolution (2026-07-18):** per the standing hard rule (never overwrite
  another session's lake JSON), wrote
  `data/lake/fundamental_reports/EMUDHRA/FY26.v2.json` — same substantive
  thesis (fast grower, QoE concern from capitalized development + rising
  unbilled revenue) preserved, all citations independently re-verified,
  plus one new finding (management's own "Organic IP Investment" framing
  of the identical ₹601 Mn capitalised spend on the FY26 highlights page,
  p55) that strengthens the existing thesis rather than changing it. Both
  files now coexist; `FY26.v2.json` carries a `conflict_note` field
  documenting the discrepancy for whoever reconciles them.
- **Follow-up:** triage should decide which file is canonical (or merge),
  and whether the SAME citation-integrity check (re-verify every existing
  lake JSON's quotes against `extract_pages()`) is worth running across
  the other three on-disk benchmarks (AZAD, JWL, VEDL FY25) before they're
  trusted as ground truth for future model-benchmarker runs.

## Issue 19 — the citation rot is ALL FOUR manual benchmarks, not just eMudhra (2026-07-18, triage of Issue 18's follow-up)

- **Observed (verified 2026-07-18):** ran Issue 18's citation-integrity
  method (every finding's `quote` substring-checked, whitespace/ligature-
  normalized, against the RAW `extract_pages()` text of the source PDF)
  across every human-authored benchmark lake JSON. Verify rates:
  AZAD/FY25 2/5, JWL/FY25 3/6, VEDL/FY25 2/11, EMUDHRA/FY26 1/6. Only
  `EMUDHRA/FY26.v2.json` — the condenser+`validate_findings`-assisted
  rebuild — passed clean (7/7). The failure modes mix: some cite the
  wrong extracted page (EMUDHRA p154→156, JWL p30→73), most have quotes
  that appear NOWHERE verbatim (VEDL 9/11), i.e. hand-transcribed
  summaries typed as if they were copied quotes.
- **Root pattern:** all four originals were MANUAL reads (a human/chat
  session reading the PDF and typing JSON). The single clean file is the
  one built THROUGH the coded pipeline's quote-validator. This is direct
  evidence for consolidating onto the automated read — hand-transcription
  is the contamination source, and `validate_findings` structurally
  cannot emit an unquotable citation.
- **Impact (bounded):** advisory-only research data — never touched a
  live trade (Dept 8 iron rule). BUT it is trusted as GROUND TRUTH by
  (a) `tests/test_annual_report_analyzer.py`, which reads needle pages
  dynamically from these JSONs to assert condenser recall, and (b) the
  `model_matchup.md` "analyst benchmark" row (built on the contaminated
  EMUDHRA/FY26). Neither is corrected yet — flagged here.
- **Ruling (triage):** (1) `EMUDHRA/FY26.v2.json` is CANONICAL; the
  original `FY26.json` is superseded (retain for provenance, mark
  deprecated). (2) AZAD/JWL/VEDL FY25 are NOT trustworthy as ground
  truth until regenerated the same machine-assisted way v2 was —
  condense → read → every citation `validate_findings`-checked before
  write. Do NOT let the 50-company parallel run anchor on them meanwhile.
- **Resolution:** pending — regeneration folds into the Gemini-synthesis
  pipeline consolidation (the automated read replaces the manual one).

## Issue 20 — RD-404 stale-symbol outages during the owner's small/micro-cap diligence run (2026-07-18)

- **Observed (verified 2026-07-18, `logs/report_downloader.jsonl`):** while
  fetching annual reports for three owner-supplied small/micro-cap ticker
  lists, `report_downloader` returned honest RD-404 ("no usable
  annual-report rows") for 9 symbols across two batches: `PREVEST`,
  `COOLCAPS`, `SIKA`, `CHEMTECH` (batch 2), and `LGBROSLTD`, `NITTAGELA`,
  `SAVAITAOIL`, `RPITECH`, `HAWKINCOOK` (batch 3). Per the LTIM/TATAMOTORS
  lesson (Issue 15), this means NSE's
  annual-reports API has nothing filed under exactly that symbol — not
  necessarily that the company doesn't exist, since small/SME-listed
  names are more prone to symbol drift (BSE-vs-NSE listing, SME-platform
  vs mainboard, a recent rename/relisting) than the large-cap watchlist
  this clerk was originally built against.
- **Not investigated further this session** (out of scope for a Dept 8
  research pass — this is a data-availability gap, not a code bug): each
  symbol was logged and skipped per the downloader's fail-open design;
  the owner was told inline which tickers had no report available rather
  than the loop silently omitting them from the forensic batch.
- **Follow-up (if this recurs on the next batch):** worth a quick manual
  NSE-symbol-search check on 1-2 of these to confirm whether it's a true
  gap or a symbol variant (e.g. `SIKA` vs `SIKAINTER`, `CHEMTECH` vs a
  different exchange code) before assuming the company has no annual
  report filed at all.

## Observation — benchmark PDFs removed from the Mac (2026-07-19 night, data note, not a bug)

- **Verified:** the Desktop "annual reports" folder is empty and none of
  the 16 benchmark PDFs (VEDL/RELIANCE/NALCO/ADANIPORTS/AZAD/JWL/EMUDHRA
  FY24-FY26) exist anywhere on the Mac — removed outside the build
  sessions (Desktop cleanup, presumably). Effect: the 4 benchmark-corpus
  tests in tests/test_annual_report_analyzer.py now SKIP (cleanly, by
  their design — they gate on file presence). The lake deep-reads are
  intact; no pipeline is affected.
- **Restore path (queued for tonight, after the results re-sweep):** the
  originals are all NSE-archive fetches — `report_downloader --tickers
  ... [--fiscal YYYY]` re-fills them into data/fundamental_reports/; the
  corpus tests' glob then needs updating from the Desktop path to the
  dropzone path (they were written against the Desktop location).

## Issue 21 — NSE results-comparision API serves a FROZEN window (ends Q3 FY25) for every symbol (2026-07-19 night, caught by the staleness guard on the valuation engine's FIRST live run)

- **Observed (verified):** every capture in data/lake/financial_results/
  holds exactly 5 filed quarters ending 31-Dec-2024 — TCS, MARUTI,
  KPITTECH, ANANDRATHI, ASHOKLEY, RPPINFRA all identical windows. The
  results-comparision endpoint returns a stale fixed comparison set,
  ~18 months behind, uniformly. Discovery chain: first valuation run
  printed BAJFINANCE P/E 4.05 (fake-cheap: pre-split Dec-2024 EPS vs
  today's post-split price) -> staleness guard added -> guard zeroed
  the ENTIRE universe -> stored windows inspected -> frozen API window
  established as fact.
- **Consequences, owned in full:**
  (a) the 73-darling QUANT screen ran on Dec-2024-vintage growth — it
  is a consistent cross-section (same as-of for everyone, so relative
  filtering retains meaning) but it is NOT "current growth" as
  reported earlier tonight;
  (b) tonight's first valuation scores AND the 8-name RIPE card are
  WITHDRAWN (erratum card fired); the corrected basket now honestly
  shows no_valuation for all until a fresh source lands;
  (c) UNAFFECTED and still current: the forensic deep-reads (FY25/FY26
  documents), the pricer levels (Friday's bhavcopy bars), the zones/
  stops/extension states, the VM deploy.
- **Fix path (next session):** (1) probe the corporates-financial-
  results LISTING sorted by broadcast date for 2026 filings and crack
  the -data detail endpoint params (the site itself uses it — current
  data exists behind it); (2) pragmatic fallback: ANNUAL results
  (FY26 annuals filed ~Apr-May 2026, well inside any staleness bound)
  for TTM valuation inputs; (3) re-label the darlings queue criteria
  with its data vintage either way.
- **The system-design vindication worth recording:** the guard built
  from the FIRST anomaly (one fake-cheap P/E) caught a dataset-wide
  integrity failure on the same night it shipped, and the basket
  self-corrected to zero rather than keep advertising stale ripeness.
  NULL-honesty extended to TIME is now a standing rule: no valuation
  without a freshness check on the inputs.

- **Issue 21 RESOLUTION (2026-07-20 pre-dawn):** the missing river found —
  post-Jan-2025 filings live in SEBI's INTEGRATED-FILING regime
  (`/api/integrated-filing-results` + per-row filed XBRL XML). New clerk
  `integrated_results.py` parses the primary documents directly
  (validated live on TCS: 30-Jun-2026 quarter, broadcast 09-Jul-2026 —
  ten days old — rev/PAT/EPS/share-count all matching reality). Fresh
  sweep of the 91 queued darlings launched; valuation + basket re-run
  on landing. The old results-comparision path stays only as history.

## Issue 22 — news sentiment's `stale` flag never ages: 11 days on a July-5 read at full weight, and the lake archived the duplicates as fresh history (2026-07-20, found while triaging the 07-19 ops-sweep "silent jobs" card)

- **Observed (verified on the VM):**
  (a) `data/lake/news_daily/` holds 9 dated partitions (07-11→07-19)
  but only TWO distinct `generated` stamps inside them:
  2026-07-05T12:02:44Z (five partitions, 07-11→07-15) and
  2026-07-16T10:05:26Z (four partitions, 07-16→07-19). The archiver
  (19:45 IST) faithfully re-copied a file that news_processor was not
  refreshing — news_processor had no cron line until the 07-16 partial
  deploy, and the 07-19 20:36 crontab reinstall explains the sweep's
  "silent jobs" card (lines installed AFTER their daily slots had
  passed; `journalctl -u cron | grep CMD` shows no 18:50/19:10/20:20
  firings that evening — that grep is now the standing one-shot
  diagnostic for this alarm class).
  (b) Every entry in those stale copies carries `"stale": false` —
  because `stale` records "the Gemini call did not fail" at WRITE time
  and never ages. `forecast._news_driver` checked only that flag, so a
  July-5 TCS read (−5, "IT sector slump") scored −2.0 pts — exactly
  the BEARISH_THRESHOLD — in every forecast through 07-16.
  `confluence/evidence.news_evidence` had the identical hole.
- **Fix (committed this session, suite green before push):**
  (1) `news_processor.entry_is_fresh()` — freshness is now a READ-time
  judgment owned by the module that owns the file format
  (NEWS_MAX_AGE_HOURS=48: one missed 19:10 refresh tolerated, no
  more; missing/unparseable timestamp = NOT fresh).
  (2) `forecast._news_driver` and `evidence.news_evidence` both gate
  through it (single source — the two consumers can never disagree).
  (3) `daily_archiver.archive_news` skips a file whose `generated` is
  >24h old: the lake gets an honest HOLE, never a duplicate
  masquerading as a fresh day (no-`generated` legacy payloads still
  archive, fail-open).
- **Not fixed here (owner decisions pending):** the 8 fabricated
  partitions already in the VM's lake (delete vs keep); rss_ingester
  classifies nothing on the VM by design (#75 ollama default) so its
  heartbeat means "ran", not "produced" — unchanged.

- **Issue 22 ADDENDUM (2026-07-20 15:35 IST, caught during the VM deploy smoke-run):** the v3 dual-horizon prompt's FIRST live Gemini call answered with a JSON ARRAY instead of the requested object — `scored.get(ticker)` crashed the whole run ('list' object has no attribute 'get'), leaving the on-disk file stale (which, post-freshness-fix, would blank news for every consumer after 48h). Intermittent: runs 2-3 minutes later returned proper objects. Hardened same hour (`d6015de`-ish, see git): `_as_mapping()` coerces the array shapes back to {ticker: entry} (unrecognizable rows → honest stale-neutral, never a crash) + the prompt now says "single JSON OBJECT (never an array)". Deploy verified after: 84/84 real reads, dual-horizon schema live, prev-linking working (same-day baseline).

- **Thursday Protocol triage (2026-07-22 session, owner returned early, owner's "start building"): the autonomous run's bug report read clean-ish — 55 items, ONE real code bug.**
  Report pulled via `python3 -m src.bug_ledger --report` on the VM (deployed commit `c2132c3` at read time).
  - **Real bug, FIXED this session — intraday_15m failure bursts.** `src/ingestion/intraday_tracker.py` fired 84 sequential quote calls with no retry; failures clustered by SLOT (15-26 big names dead at 11:00, all fine at 11:15) = rate-limit collisions with other Dhan consumers, not dead scrips. Fix: one spaced in-sweep retry pass (RETRY_SLEEP_SECONDS=2, injectable sleep_fn), both-pass failures stay named, summary gains `recovered`. Tests: 3 new + 2 updated in tests/test_intraday_exit.py, file green 18/18.
  - **NOT a bug — MACPOWER.NS "equity budget exhausted" (07-22 09:35).** The ₹10k hard cap caps RISK (stop-distance × qty), not notional (equity_desk.size_entry); a ₹14,599 notional needing more than the desk's remaining ₹3,096.80 of ₹60k was correctly refused. The desk is ~95% deployed — health signal, logged as designed.
  - **NOT a live bug — `corporate_events.py: unrecognized arguments --backfill/--throttle` (07-17).** One-shot wrong manual invocation during the quant-sprint backfill; no cron runs corporate_events (verified `crontab -l`), and the backfill itself completed via the correct args same day (62,725 flagged events, 0 failed). Ledger noise.
  - **Known/resolved eras, no action:** 07-08→07-13 token/TOTP items (fixed by the 07-10 weekend deploy), NSE 403/timeouts falling open to snapshots (designed), VM `skipped_no_llm` ingestion (by design, #75).
  - **Second real bug found by the full-suite run, FIXED same session — data-drift test isolation.** `tests/test_equity_shadow.py::test_market_loop_hook_is_off_by_default_and_fail_open` pinned its clock to 2026-07-17 11:00 IST but let `run_market_loop`'s cooldown seed read the REAL `data/journal.jsonl` (which the edge miner refreshes from the VM) — a live NIFTY 50 entry drifted inside the 2h cooldown window and the loop skipped `fetch_fn`, failing the assertion on data, not code. Fix: monkeypatch `journal.JOURNAL_PATH` to an empty tmp path inside the test. File green 14/14 after.

- **ceo_brief digest-queue sandbox leak — FIXED 2026-07-23 (macro sprint gate).** `build_brief_card` drained the Discord digest queue via `drain_digest_queue()` with NO path argument, so it read the REAL `logs/discord_digest_queue.jsonl` even under an injected `logs_dir` — escaping the sandbox every other collector honors. A live `macro_regime.declare()` transition card spooled into that queue mid-session and surfaced as a phantom 5th field, failing `test_build_brief_card_shape` (this ALSO retro-explains the 07-22 day-one flake of the same test). Root cause is the same class as the 07-22 journal-drift bug: a test reading live production state through an un-injected seam. Fix: derive the queue path from the injected `logs_dir` (`logs_dir / "discord_digest_queue.jsonl"`) — byte-identical in production (LOGS_DIR == ROOT/logs), fully sandboxed in tests. Added `test_digest_drain_is_sandboxed_to_the_injected_logs_dir` regression. Suite 1544 green.
