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

### Issue 5 — Mid-session token death (DH-906 again) after the timezone fix
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

### Issue 6 — `get_expiry_list` double-nesting silently blocked EVERY proposal
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

### Issue 1 — No trading session / no Approve-Reject cards this morning
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

### Issue 2 — The same trade-closed cards repeating every hour
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

### Issue 3 — Yesterday's DH-906 "Invalid Token" flood in suggest.log
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

### Issue 4 — Sleep phase "Ollama call failed: Connection refused" (VM)
- **Symptom:** the 02:00 IST ops card flagged
  `sleep_phase.log: (local parser: Ollama call failed: [Errno 111])`.
- **Root cause:** **expected behavior, not a bug** — the VM has no Ollama
  (1GB RAM) by design (decision #47); the sleep phase there degrades to
  the decay-only pass, and ingestion correctly skipped 5 duplicates.
  Causal mining runs from the Mac opportunistically instead.
- **Resolution:** none needed. Noted so nightly cards quoting this line
  aren't re-triaged. (A quieter log message on the VM is a triage-week
  candidate if the noise annoys.)

### Issue 7 — Analyst "/analyze" reports missing history for TCS.NS (message blames Yahoo; it's actually Dhan)
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

### Issue 8 — Session restart resets the in-memory cool-down → duplicate proposals (positions doubled)
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

### Day-1 wrap — 20:30 IST ops sweep triage (all 10 lines accounted for)
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

### Issue 9 — Edge miner's first "successful" run was a silent no-op (third unpinned-interpreter incident this week)
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

### Context for triage (not an issue)
- The ops sweep's "silent job" heartbeats from the 02:00 IST card
  (renew/suggest/main/master_scheduler "did not run today") were all
  downstream of Issue 1's timezone shift, not independent failures.
- Issue numbering here is chronological-append, not the sequence the
  user quotes in chat — this "Issue 7" is what the user called the
  forecast/"Issue #3" report on 2026-07-09.

---

## Date: 2026-07-10

### Context for triage (not an issue)
- Today's two approved journal entries (`f0ae401e` NIFTY 50,
  `2df15c4d` NIFTY BANK) carry `"why": "Test"` — confirmed with the
  user this is an intentional label they're entering by hand during
  the observation week, not a pipeline bug or default string. Noting
  it here so a future triage pass doesn't misread it as a defect.

### Issue 10 — 07:00 IST token renewal failed ("Invalid TOTP"); real cause is a second, undocumented renewal cron racing the documented one
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

### Issue 10 — UPDATE 2026-07-10 ~13:35 IST: the 12:00 IST firing DID land mid-session and BLINDED the live trading loop (Issue 5 recurrence). The morning "impact: none observed" assessment was incomplete.
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

### Issue 10 — RESOLVED 2026-07-10 ~21:45–22:00 IST: weekend deploy executed, single-07:00 cadence restored (all steps verified on the VM)

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

### Issue 11 — FOUND AND FIXED 2026-07-11 ~11:30 IST: NSE deals fetch was broken three ways (caught on the first real backfill run)

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

- **DH-905 host-wide throttle fix CHERRY-PICKED to main — 2026-07-25 (Phase-1 hygiene session).** The fix (commit `1867335` on the parked `claude/hello-d9m45n` branch, authored 07-22) replaced `dhan_client._throttle()`'s process-global pacing with a HOST-WIDE flock on `data/.dhan_throttle` — the several market-hours processes sharing one Dhan account (live loop, report/greeks cards, intraday tracker, equity desk) previously couldn't see each other and collectively blew the rate limit (the 07-22 intraday_15m bursts + suggest.log DH-905). Landed on main as `ad9d586` (one CRON_SETUP.md conflict, resolved to the branch's newer 23-job table); its 6 throttle tests green. Also brought `intraday_tracker` into `setup_cron.sh` as job #23 (was VM-only drift).

- **`macro_nightly` cron drift CLOSED — 2026-07-25.** `src.analysis.macro_nightly` (the 19:50 IST Dept-8 macro heartbeat) was live on the VM's crontab but MISSING from `scripts/setup_cron.sh` — the idempotent installer would have silently dropped the macro clock on its next re-run. Added as job #24 (+ CRON_SETUP.md row). NOTE for next VM deploy: after `bash scripts/setup_cron.sh`, remove any manual crontab lines for macro_nightly/intraday_tracker so they aren't double-run.

- **`decay_engine.apply_decay_sweep` is UNWIRED — flagged 2026-07-25, NOT yet fixed (owner decision pending).** Phase-1 AST audit: nothing on any cron/systemd/LaunchAgent path calls it, so knowledge-graph EDGE decay (graph_edges.confidence_score) never actually runs in production — `graph_engine.py` and `entity_affinity.py` docstrings both describe it as live. Distinct from sleep_phase's semantic-NODE decay (initially misread as a duplicate; the archive ruling was reversed on verification). Kept in `src/`, flagged in MODULES.md; candidate wiring: a step in the 20:00 sleep_phase pass.

- **Test suite 14m09s → 1m23s (Phase-3 streamlining, 2026-07-25, `48e15a8`).** Three files were reaching REAL external systems from inside the suite; each was a hermeticity/correctness bug as much as a speed one, because what ran depended on the host. (1) `test_api_auth.py` ~660s → 0.31s: the three tests that pass auth execute the `/api/watchlist` handler, which calls `get_quote()` once per distinct ticker in the REAL production watchlist — 84 live quote requests per test, 11 of the suite's 14 minutes in 3 tests, and a hang on a network-less CI box. Fixed with an autouse `hermetic_quotes` fixture (stubs `watchlist_store.load_items` + `src.api.get_quote`, resets the `_quote_cache`/`_cache_at` globals); the `__main__` block now delegates to `pytest.main` because calling the test fns directly bypassed the fixture. (2) `test_dhan_client.py` 15.4s → 0.03s: the SDK is mocked, so the NEW host-wide `_throttle()` (DH-905, landed same day) was sleeping 1.1s per call to pace calls that never leave the process — stubbed per-file, throttle behaviour still covered by `test_dhan_throttle.py` on a fake clock. (3) `test_daily_context.py` 29s → 1.22s: `test_sleep_phase_runs_task_g` built a real `LocalExtractor`, so on a Mac with Ollama up it ran real LLM inference for sleep-phase A/B/D — a DIFFERENT path than CI/VM take (no Ollama by design); now injects a not-reachable extractor. Knock-on: `test_darling_shadow` 41.79s → <2s untouched (it had been starved behind the auth tests' network calls). 1589 passed both before and after — same tests, same assertions. **pytest-xdist judged unnecessary at 1m23s**; the remaining slowest entries (noise-injection placebo runs 11.4s, strategy_registry DTW 10.2s/8.7s) are genuine computation.

- **14-commit VM deployment gap CLOSED — 2026-07-27.** The 07-27 CEO brief still showed DH-905 lines in `suggest.log`; cross-checking the brief's deployed SHAs against git showed the VM on `bb99555` (24 Jul) while the throttle fix `ad9d586` was NOT an ancestor (`git merge-base --is-ancestor` = no) — the whole 07-25 hygiene session (throttle, Great Purge, macro heartbeat, 10x suite) had never shipped. Owner deployed same day via runbook: VM pulled to `170aa21`, `setup_cron.sh` reinstalled the 24-job block, the manual crontab duplicates for `intraday_tracker`/`macro_nightly` were deleted (verified: 2 non-comment matching lines remain, both installer-owned; backup at `~/crontab.bak-*` on the VM), `alpha-trading` + `alpha-discord-bot` restarted active, `/api/health` returned ok.
- **ops_monitor zero-stat false alarm, THIRD shape — FIXED 2026-07-27 (`170aa21`).** `macro_nightly` writes clean runs as `"failed": []` (empty LIST); the scrubber only knew zero COUNTS (`'failed': 0`, `0 failed`), so two clean macro_nightly runs (07-24, 07-25) reached the CEO brief as problem lines. Empty brackets now scrubbed; `"failed": ["TCS.NS"]` still fires. Regression test added; same false-alarm family as 07-14 and 07-20.
- **firm_mtm "realized" label printed account equity, not profit — FIXED 2026-07-27 (`170aa21`).** The card said `realized Rs.244,215` against a Rs.200,000 base: the value was `account.equity` (base + realized P&L), mislabeled — a 6-day paper run dressed up as +2.4L profit. `compute()` now also returns `realized_pnl` (equity − base) and `render_line` prints that, signed (`realized +44,215`). `equity_realized` kept unchanged for MTM composition. Regression test added; suite 1,591 green.
- **Intraday capture misses (79-80/84: DIVISLAB, LUPIN, TATASTEEL) — OBSERVED 2026-07-27, cause UNCONFIRMED.** Plausibly the un-throttled client (fix was undeployed until today). Re-read after the next full session; if the misses persist on the throttled client, it is a real data issue, not rate-limiting.

- **Opportunity-cost seam wrote FIXTURE ROWS INTO THE LIVE brain_map.db — found and fixed within the hour it shipped, 2026-07-27 (Directive 1).** The new `exposure_gate._record_opportunity_cost` seam opened `brain_map.connect()` (the real DB) when no `record_fn` was injected. `tests/test_exposure_gate.py` sandboxes `LEDGER_PATH` via `_TempLedger` but had no way to sandbox a connection the seam opened itself, so a full suite run inserted **4 rows** into the production `shadow_trades` table (`host_ref='ab12cd34'`, the test fixture's short_id, all stamped `2026-07-27T14:22:40+00:00`). `python3 -m src.opportunity_cost` then reported *"the exposure gate has refused 4 duplicate trade(s)"* — a fabricated number inside a risk report, from test data. Caught by inspecting the real DB after the suite rather than trusting 1,626 green. FIX: the seam is now muzzled under `PYTEST_CURRENT_TEST` (same doctrine as `notifier.webhooks_muzzled()` — automatic, so a future test that forgets a fixture cannot re-poison the record); tests exercise it through the `record_fn=` seam. The 4 rows were purged (backup taken first; `shadow_trades` is not one of the immutable append-only ledgers, and the rows were verified fabricated before deletion). Regression test `test_the_suite_can_never_write_into_the_real_brain_map` fails the build if `brain_map.connect()` is ever reached from inside the suite via this path. Verified after: full suite 1,627 green AND the live `shadow_trades` still at 0 rows. **Third instance of the same family** (07-22 journal drift, 07-23 digest-queue sandbox leak): a module reaching live state through a seam its tests could not inject. The standing lesson is now explicit — a new seam that opens its own DB connection needs the pytest muzzle from the first commit, not the second.

- **H4 comparator reported a DATA FAILURE as an experimental result — FIXED 2026-07-27 (`7e0d635`).** The first real-data run of `src/validation/h4_comparator.py` (`--start 2022-01-01 --end 2025-06-30`) printed a clean four-line report ending `verdict: insufficient_data` for all three lookbacks. It had simulated **nothing**: the local Dhan token is DH-901 expired, `dhan_client.get_ohlc_since` fail-open-returns `[]` by design, and the comparator walked zero bars to a tidy n=0 report that reads exactly like "the experiment ran and the sample was thin." Verified no rows were written (`simulated_trades` 366 before and after, zero `sim:h4:` rows) — the output was cosmetic, not corrupting. FIX: `H4DataError` + `_validate_bars` abort before any policy walks a day, on empty history OR fewer than `MOVING_AVERAGE_SLOW+1` bars (below that every day is skipped and zero trades is guaranteed); the CLI names the likely cause (expired token → regenerate `.env`) and exits 1; an empty India VIX series now warns loudly on stderr because it silently changes which structures are proposable. Verified: same command now aborts with exit 1; happy path (injected synthetic bars) still runs; both empty and under-warmup injected series raise. **The general lesson: a fail-open data layer plus a fail-quiet consumer produces a confident wrong answer.** `dhan_client`'s `[]` is correct for the live tracker (wait for the next session) and wrong for a research harness — the harness, not the data layer, owns that distinction.

- **Analyst error: a false "`regime` is never stamped" finding, self-caught and corrected same session — 2026-07-27.** While scoping the spread-aware tuner I reported that live journal entries carry `regime: None`, called it "a critical data leak," and the owner issued a directive to fix the journaling. **The finding was wrong.** `options_proposer.to_journal_entry()` has stamped `entry["regime"] = regime_for(view, vix)` since `ef631db` (2026-07-09); only the 8 entries dated 07-09/07-10 predate it, and every entry from 07-13 onward carries `{'trend','vix_band','vix'}` correctly. Root cause of the error: I inspected `e[0]` — the OLDEST resolved entry — and generalized to the whole book, exactly the sampling mistake that makes a stale record look like a broken one. **No code change was made**, the directive was reported back as a no-op, and `docs/spread_aware_tuner_design.md` carries the correction inline rather than a silent edit. The 8 `None` rows are deliberately NOT backfilled: the entry-moment VIX was never recorded and reconstructing it would be fabrication (Rule 3). Knock-on discovery that mattered more than the original claim: all 7 regime-stamped resolved spreads are `('bearish','mid')` — the field is captured perfectly and has **zero variance**, so it is useless as a learning axis for a different reason than claimed. That drove the ≥2-populated-bucket degeneracy guard in the tuner design.

- **Near-miss: a delete directive built on hallucinated context was refused — 2026-07-27.** The owner issued a four-part directive citing a "Forensic Sweep Report" that does not exist in the session, instructing deletion of `src/decay_engine.py` ("`sleep_phase.py` already handles it") and `src/knowledge_graph/resonance.py` ("dead weight"). Verified before acting, per Rule 4: `decay_engine` is imported by `graph_engine.py` and `entity_affinity.py`, and MODULES.md's own 07-25 audit row states verbatim that it is **"NOT a duplicate of sleep_phase's node decay"**; `resonance.py` is imported by four modules (`knowledge_graph/__init__`, `entity_affinity`, `news_parser`, `macro_tracker`) and carries a green 27-test suite. Both deletions were refused with the import graph and the contradicting MODULES.md line quoted back; the owner confirmed the premise was hallucinated on their end. **Nothing was deleted.** The `tuner.py` half of the same directive WAS accurate (genuinely uncalled) — a report can be right about one file and inventing the rest, so each claim needs its own verification, not one spot-check that licenses the batch.

- **`tuner.py` "dormant" was mis-framed by me before the Rule 5 marker was read — corrected 2026-07-27.** Asked to cron `src/tuner.py`, I grepped for callers, found none, and endorsed the owner's "disconnected/dormant" framing as a "massive finding." Line 1 of the file reads `# MANUAL OFFLINE TOOL — not on any cron/systemd path; keep out of dead-code sweeps (Phase-1 audit 2026-07-25)` — the off-cron status is a **deliberate, documented decision** protected by CLAUDE.md Rule 5, not neglect. Reading the file (rather than only grepping around it) also surfaced the substantive blocker: `_resolved_buy_outcomes` filters `action != "BUY"`, so the tuner is structurally blind to the desk's 17 SPREAD entries and sees only 3 equity BUYs — below even the old floor of 5. Cronning it would have rewritten `brain_weights.json` with the identical neutral values it has held since 2026-07-05, on a weekly Discord card, forever. **Outcome: no cron installed**; scoped `docs/spread_aware_tuner_design.md` instead. `tuner_min_samples` raised 5 → 10 (owner ruling, aligns with `stat_gates`' 8–30 range); global value, but the equity path has 3 trades so it was already neutral at 5 and stays neutral at 10 — no behaviour change, suite 1,627 green.

- **08:00 `src.suggest` DH-905 skips — root-caused and FIXED (code committed, NOT yet deployed to the VM) 2026-07-30.** Symptom: one-plus DH-905 `Input_Exception` per 08:00 run in `suggest.log` (22 lines / 15 runs), each costing one early watchlist name behind "not enough price history yet". The obvious theory — an HDFCBANK-specific bad security id — is WRONG on the evidence: the skipped ticker varies (HDFCBANK 8×, ICICIBANK 5×, TCS 4×, INFY 2×, HINDUNILVR 2×, ITC 1× — always among the FIRST few names, never the later ~78; HDFCBANK is merely first in the watchlist), id 1333 works daily on the quote path (238 intraday-lake rows) and on historical in 7 of 15 runs. Client-side pacing is also ruled out: the first call of the run has nothing to collide with, nothing else Dhan-facing runs at 08:00 on the VM (crontab checked: renew 07:00, morning_brief 08:05 no-Dhan, scheduler 09:10) or the Mac (crontab + LaunchAgents: 19:15/21:00/Sat), and the errors persisted after the host-wide flock deployed (07-28/29/30 in `problems.jsonl`). Confirmed shape: a transient Dhan-SIDE rejection window in the first seconds after 08:00 IST; both in-function attempts (1.1s apart) land inside it; the next ticker succeeds seconds later. Exact Dhan-side trigger UNCONFIRMED — the old error print truncated at 160 chars, cutting the message off right before Dhan names the offending parameter. FIX (two-part): `_fetch_daily` now prints the failing id/seg/inst/date-range and 400 chars of response (the next real failure will name the parameter), and `run_once` retries first-pass skips ONCE at end of run — by then the window has always passed (intraday_tracker retry doctrine; both-pass failures stay skipped and say so). 3 regression tests in new `tests/test_suggest.py` (flaky-fake analyze; digest/e-mail monkeypatched); suite 1,630 green. **Verification pending: the next VM 08:00 run after deploy — expect `recovered` lines instead of lost names, and a full error message if the window fires.**

- **Edge-to-Cloud asynchronous architecture formalised — the Mac Handover Queue, 2026-07-30 (owner directive).** Two standing rules: the 1 GB e2-micro NEVER runs data-heavy/compute-heavy work, and the VM's nightly pipeline NEVER depends on the Mac being online. The VM already honoured the first (`macro_nightly` runs `declare(require_cache=True)` — the 07-23 dumb-executor directive: abstain fast and scream rather than grind 30 minutes) but a declined task was only a log line that scrolls away, so the run stayed up while the REASON evaporated. `src/mac_queue.py` is the missing sink: on the EXISTING cache-miss branch the VM now appends one request to `data/mac_pending_tasks.jsonl` and carries on. **Deliberately not a new detector** — it is wired to abstention the VM already performs, which is why this adds no new failure mode. Append-only (`resolve()` writes a closing row, never rewrites), idempotent per (task, day) so a nightly re-detection leaves one row per day rather than a flood, fail-open in every function, and pytest-muzzled so a forgotten fixture cannot post phantom work to the live queue. **The non-dependency is regression-tested, not just asserted in prose:** one test explodes the queue with a RuntimeError and proves the nightly run still reaches its final stage. `scripts/daily_health_and_queue.sh` renders the clock + the queue as a literal copy-paste line for the owner; it uses `set -uo pipefail` WITHOUT `-e` on purpose, so a failure in one half still prints the other. 10 new tests.

- **Stage A verified ALREADY UNLOCKED, and the Stage-B clock is 8 sessions SHORT of Oct 1 — 2026-07-30.** Asked to ingest owner-downloaded 2013-2018 sector CSVs, three premises turned out wrong and are recorded rather than quietly worked around: (1) the Desktop file (`Nifty 50 Historical Data.csv`) is the BROAD index in Investing.com format, not sector data, and is redundant with the NIFTY history already in the lake to 1995 — the real 150 NSE-format sector files were in `~/Downloads`; (2) there is no repo "drop-folder" — `index_history.py` takes `--folder` and its docstring names Downloads as the drop folder by design; (3) the CSVs must NOT be synced to the VM — the VM reads BUILT ARTIFACTS (it already holds `macro_strategies.json`/`macro_templates.json` from 07-25 plus its own 19-channel lake), and rebuilding on the 1 GB e2-micro is the documented OOM-wedge risk, so no VM disk was consumed. **The ingest was run anyway and added exactly 1 new date (NIFTY_PHARMA); all 132 mapped files were already ingested on 07-24.** Lake depth confirmed: BANK→2000, IT→2002, AUTO→2004, PHARMA→2005, METAL/ENERGY/PSU_BANK/MEDIA→2011, FMCG→2013, HEALTHCARE→2021. **Registry verification at `MIN_EPISODE_LEGS=5`: 8 of 9 cells RENDER with 5-10 legs each — sector rotations are NOT abstaining** (1 PREFER: `A1/P1_shock long_energy_oil` n=9, hit 88.9%, Wilson LB 0.623, significant; 7 SHOW; placebo meter 1 survivor of 24 ≈ 4%, at the ~5% chance rate, so the gate is not too loose). The single remaining ABSTAIN (`S1/S1_buildup`) is NOT a data-floor problem — the slow-burn archetype has too few episodes in that phase; more sector CSVs cannot fix it. Owner ruled: do NOT add NIFTY NEXT 50 / OIL & GAS to the roster (15 files stay unused), do NOT rebuild the artifacts. **Remaining named gaps: India VIX pre-2019 (floors 2019-10-01; NO VIX files were in the download set) and 3 header-only NIFTY METAL files (2004/2007/2008, zero data rows — named skips, re-download if wanted).**

  **RULED SAME NIGHT — 2026-07-30, decision #86: the standard does not slip, the calendar does.** The 60-session bar for a MATURE verdict is held; the official Stage-B completion target moves to **~2026-10-13**. **Oct 1 survives as a PRELIMINARY, NON-BINDING read** — if early forward windows show significance by then, good; otherwise wait for the 13th. Zero graded calls is explicitly accepted as expected (forward windows need calendar time). `stage_b_tracker.py` now encodes both dates and projects sessions-at-prelim, so the tool measures against the ruling instead of a superseded date. The reasoning is in DECISIONS.md #86: a verdict computed on 52 sessions would be a weaker test wearing the 60-session label, and the bar must stay fixed BEFORE the result is known — the same doctrine as the registry's floors/BH/placebo layer.

  **⚠️ THE FINDING THAT DROVE THAT RULING, surfaced by the new tracker on its first run:** the Stage-B clock is at **7 distinct sessions, not the 12 I reported earlier today** — 12 is the RAW row count, but the 07-22/23/24 build era wrote 2-4 rows per session during manual verification runs (the ledger is append-only and those rows correctly stay; the honest denominator is DISTINCT `as_of_session`, which is what the tracker counts). Since 07-27 it is exactly 1 row per weekday, i.e. the cron is clean. **Consequence: 53 sessions are still needed and only 45 weekdays remain before 2026-10-01 — the 60-session target is arithmetically unreachable by that date even at 100% uptime** (perfect uptime lands at ~52). This is an owner decision, not something to tune away: accept the read at ~52 sessions, or slip the Dept-5 verdict ~2 calendar weeks to ≈Oct 13. Deeper constraint, already in the spec and unchanged: **graded calls are still 0** — sessions on the ledger are not evidence; a verdict needs matured forward windows (`MIN_FWD_CALLS`=7), and "60 sessions" was always calendar time rather than 60 graded calls.

- **`decay_engine` UNWIRED — CLOSED 2026-07-30 (owner-approved wiring; open since the 07-25 Phase-1 audit).** `apply_decay_sweep` was on no cron/systemd/LaunchAgent path, so knowledge-graph EDGE decay (`graph_edges.confidence_score`) never ran in production while `graph_engine.py` and `entity_affinity.py` both documented it as live — edges written in July 2026 still carried their birth confidence. Fixed by the audit's OWN candidate wiring: a new **Task K in the 20:00 sleep_phase pass** (`sleep_phase.run_edge_decay`), `decay_engine.py` itself unchanged (owner constraint: no new engines). Placed LAST, after the edge writers (Task D causal links, Task F entity affinity) — `apply_decay_sweep` stamps `valid_from` on first touch WITHOUT changing weight, so decaying after the writers ages yesterday's edges instead of immediately aging the ones just written. Fail-open like every sibling task (a crash logs `K. edge decay failed` and the pass still returns every other task's result — regression-tested). **DB muzzle, deliberately NOT a copy of Task J's:** Task J opened its own connection so an env check sufficed; Task K RECEIVES one, so an env-only guard would have been dead code. `_targets_the_real_brain_map(conn)` reads `PRAGMA database_list` off the connection itself and refuses when the resolved path IS the production `brain_map.db` — no caller has to be trusted to declare what it passed. It **fails SAFE** (unreadable/uncomparable path ⇒ assume real ⇒ refuse), inverting the module's usual fail-open rule, because a muzzle that fails open is not a muzzle; the 07-27 opportunity-cost incident (4 fixture rows in the live `shadow_trades`) is the cost of getting that backwards. 9 hermetic tests incl. the forgot-to-sandbox reproduction (confidence stays 0.9, undecayed), the fail-safe sensor, a not-too-broad check so the others can't pass vacuously, and a `brain_map.connect` bomb across the whole pass. Suite 1,651. **Still open and NOT changed by this: nothing else about the knowledge graph was touched — this closes the wiring gap only, and the first production sweep's `swept`/`expired` counts should be read in the 20:00 log before drawing any conclusion about graph health.**

- **Task K's first production sweep — 45 of 89 edges expired in one night; VERIFIED CORRECT, but it surfaces an OPEN DESIGN QUESTION (owner's call, nothing changed) — 2026-07-30 20:00.** First-ever run of the newly-wired edge decay: `{'swept': 86, 'decayed': 86, 'expired': 45}`. The headline number looks alarming and is not: the split by relation type is perfectly clean — **all 45 expired are `concentrates_in`** (broker/entity affinities from the deals backfill), **all 44 survivors are the causal-reasoning edges** (`RESULTS_IN` 26, `INDICATES` 12, `PRECEDES` 4, `CONTRADICTS` 2). The expired rows carry confidences like `1.19e-98` — not one day of decay but YEARS of it, which is exactly what `graph_engine.add_edge`'s historical-backfill seam intends: its docstring states verbatim that a replayed 2023 deal "must age from 2023, not read as born-today (else decay_engine treats a long-dead affinity as maximally fresh)." Those edges were stamped with historical `valid_from` dates specifically so they WOULD be aged, by a sweep that had never once run. Tonight it ran and the arrears cleared in a single step. Two machinery confirmations: **all 4 decay-exempt edges (λ=0) survived** — loss-permanence held, lessons paid for with a loss did not fade — and nothing was deleted (`invalid_at` stamped only; re-observation clears it, decision #37). Expect `expired` to collapse to ~0 from tomorrow now that the clock is current; a large count on night one is backlog, NOT graph ill-health, and must not be read as an incident.

  **RULED AND FIXED SAME NIGHT — 2026-07-30 (owner: slow the structural rate and resurrect the premature expiries).** `entity_affinity` now writes `concentrates_in` edges with an explicit `CONCENTRATION_DECAY_LAMBDA = 0.002` (~347-day half-life, ~3.2-year expiry horizon); causal edges keep the 0.05 default, pinned by a test so the whole graph can't be slowed by accident. **The naive repair was rejected as a self-reverting no-op:** `UPDATE graph_edges SET decay_lambda=0.002, invalid_at=NULL` would leave the sweep's already-crushed `confidence_score` (as low as 1.19e-98) and its overwritten `valid_from` in place — producing an ACTIVE edge with ~zero confidence that the very next sweep re-expires, i.e. tonight's repair silently undone tomorrow. `scripts/resurrect_affinity.py` instead REBUILDS each edge from source of truth (concentration recomputed by `_client_concentration` off the untouched 19,819-row `entity_affinity` table; anchor restored from that table's own `last_seen`) and writes through the existing `graph_engine.add_edge` seam — no raw SQL surgery, no new machinery. **Live dry run before applying: of 45 expired edges only 11 were expired PREMATURELY** and were restored; **32 stay dead** (no disclosed deal in >3 years — still below threshold at the new rate, correctly stale) and **2 stay dead** because their entity no longer clears `MIN_CONCENTRATION` (the edge asserts a fact; reviving it would be revival by fiat). **Knock-on find — a pre-existing latent flake, not caused by this change:** `test_backfilled_affinity_edges_age_from_their_deal_dates` anchors a FIXED past deal date but sweeps on the real clock, so its scenario's age drifts with the calendar; at 1,110 days the new rate put it at `1.0·e^(-2.22)` = 0.1086 against a 0.100 threshold — alive by 0.0086, and it would have flipped back to expiring around 2026-09-09 unaided. Deal dates moved to 2015 so the case is unambiguous at either λ for a decade; the invariant under test is unchanged. Same family as the 07-22 journal-drift bug (a test whose verdict depends on wall-clock time). 8 new tests; suite 1,659.

  **ORIGINAL OPEN QUESTION as logged (2026-07-30, before the ruling):** is `DEFAULT_LAMBDA = 0.05`/day — a **~14-day half-life** — the right rate for *every* edge class? For causal-reasoning edges it is sensible. For `concentrates_in` it means a structural fact ("Dodona Holdings takes 95% of its 20 deals in Tata") can essentially never stay active, because that class is only re-observed when a new deal lands, which is rarer than the half-life. The consequence is that the entire deal-affinity layer sits permanently invalid between deals. **This needs no new code to change:** `decay_lambda` is already per-edge and already carries a differentiated value in production (λ=0 for loss-permanence), so a slower rate for `concentrates_in` is a one-line change at the `entity_affinity` write site. Both positions are defensible — a stale affinity genuinely should not read as fresh — so this is a judgment call, deliberately left to the owner rather than tuned on my own initiative. Until ruled on, current behaviour stands.

- **H4 shadow would have NEVER FIRED — caught by the pre-run check, fixed within the hour, 2026-07-30.** The shadow shipped at ~19:20 with a freshness guard requiring `bars[-1].date == today`. Checking it before its first 20:00 cron run (dry-run seam, no DB touched) returned `{'scanned': 2, 'fired': 0, 'skips': {'no_fresh_bar_today': 2}}` — and the cause is structural, not transient: **Dhan's `historical_daily_data` only carries COMPLETED sessions**, verified directly at 19:36 IST (NIFTY 50 / NIFTY BANK / TCS.NS all ended on the 07-29 bar, four-plus hours after the 07-30 close). The guard could therefore never be satisfied at 20:00, on any night, for any instrument: the pass would have logged a plausible-looking skip forever while the forward-evidence ledger stayed empty — and "the signal is being selective" and "the signal is structurally dead" would have been indistinguishable in the log. **Same failure class as the H4 comparator's own `insufficient_data` incident 3 days earlier** (a fail-quiet consumer turning a data-availability fact into what reads like a result), reproduced by me in the consumer while writing the fix for it. FIX: the signal is now dated by ITS BAR, not by wall-clock today — `fire_date` = the bar's date, idempotency keyed per (signal, host, bar), mark-improvement computed as-of the bar's date (close and time-decay from the same day), plus two REAL guards that were missing: `stale_feed` (bar older than `MAX_BAR_AGE_DAYS`=5) and `bar_not_after_entry`. Each trading day's bar is now evaluated exactly once, the night after; weekend re-runs dedup instead of double-counting. Post-fix dry run on the same data: `{'scanned': 2, 'fired': 0, 'skips': {'no_fresh_extreme': 2}}` — an honest evaluation of the actual condition (both open spreads are bearish; NIFTY 50 closed UP at 24,250.2 on 07-29, so no fresh 10-day low). Tests updated to pin the CORRECT semantics: the T-1 bar case is now asserted to FIRE and to carry the bar's date, and the old test that asserted the broken behaviour was deleted, not kept green. 12 tests, suite 1,640. **The lesson, third time in a week: verify what the data layer actually returns before writing a guard against it — a guard built on an assumed feed shape fails silently and looks like normal operation.**

- **First REAL-DATA H4 run + shadow deployment — 2026-07-30 (evening, after the owner supplied a fresh Dhan token).** `h4_comparator` ran clean on NIFTY 50 daily history 2022-01-01..2025-06-30 (Mac, exit 0, artifact `logs/h4_run_2026-07-30.log`): baseline n=62 Sortino 2.42 maxDD 6.69R; pyramid lb-3 **does_not_graduate** (11.55R drawdown — the #68 pileup reproduced in sim, the guard's whole point), lb-5 does_not_graduate, **lb-10 graduates** (Sortino 2.62, maxDD 6.54R, 13 adds in 3.5y). Standing caveat applies: synthetic-chain absolutes are inflated (~62-79% generosity band) — the defensible part is the RELATIVE A/B through identical machinery. Isolation verified: 561 new rows all under `sim:h4:` (total 927, prior 366 untouched), journal clean. Token note: the new PARTNER-type token did NOT invalidate the VM's token (checked both sides — the 07-09 one-token race did not recur). **Owner ruling: option 2, shadow it.** Built same evening: `src/validation/h4_shadow.py` (sleep_phase Task J) + `trial.record_signal_fire` (third `mode`, `SIGNAL_SHADOW`) — hypothetical lb-10 adds recorded host-linked against open spreads, resolved by the existing Task I sweep, zero execution authority, pytest-muzzled from the first commit (the 07-27 lesson applied proactively this time). 10 new tests incl. a brain-map bomb; suite 1,640 green. **Forward-evidence clock starts at the first VM 20:00 sleep_phase after deploy; expect `no_open_spreads`/`no_fresh_bar_today` skips on most nights — that is the honest baseline, not a failure.**

- **Intraday capture misses RESOLVED as rate-limiting — VERIFIED 2026-07-30 (closes the 07-27 UNCONFIRMED entry above).** First full market session observed on the throttled client (`ad9d586` host-wide flock, on the VM since the 07-27 deploy to `170aa21`): `logs/intraday_15m.log` shows **84/84 captured at every 15-minute slot, `"failed": []` throughout** — including DIVISLAB/LUPIN/TATASTEEL, the three names that were dying at 79-80/84 on 07-27. The in-sweep retry pass is visibly doing its job: `"recovered": 1` at the 14:00 slot and `"recovered": 2` at 15:00 (first-pass collisions that succeeded on the spaced retry), zero both-pass failures. Per the 07-27 entry's own decision rule — "if the misses persist on the throttled client, it is a real data issue, not rate-limiting" — they did not persist, so the cause is confirmed as rate-limit collisions and the DH-905 host-wide flock functioned as intended. No action needed; entry closed. Separate and still open: one DH-905 per `src.suggest` run (22 in `suggest.log`) is an `Input_Exception` (malformed request) on the historical call for HDFCBANK.NS — a different failure class the throttle cannot and should not fix, tracked as its own open item in HANDOVER.

- **`office_close.command` never slept the Mac — the "walk away" button was broken since 2026-07-21, found and fixed 2026-07-30.** Reported as a Terminal pop-up ("Do you want to terminate running processes in this window? … bash (2), sleep, osascript"). The three named processes map exactly onto three lines: the script's own bash plus the backgrounded subshell `( sleep 3; pmset sleepnow ) &`, that subshell's `sleep`, and the `osascript -e 'tell application "Terminal" to quit'` issued on the very next line. Terminal was being asked to quit **while its window still hosted a live background job**, so it raised its standard confirmation. **The consequence is the actual bug, and it inverts the script's purpose: the highlighted DEFAULT button is "Terminate", and pressing it kills the backgrounded subshell BEFORE its `pmset sleepnow` fires — so the Mac stays awake all night.** A 3-second race decided the outcome: dismiss inside 3s and sleep was cancelled; take longer and the Mac slept with a stale dialog left on screen for wake. FIX: no background job and no Terminal quit at all — `pmset sleepnow` runs in the foreground as the last statement, and the window closes by itself because this Mac's Terminal has `shellExitAction = 2`. **A hypothesis I tested and REJECTED rather than shipped:** that the `osascript` in the list was hung on `if application "X" is running`, the idiom measured earlier the same evening blocking a full 2 minutes on a non-running app. Timed directly, both `"Code"` and `"Visual Studio Code"` returned in ~2s and launched nothing, so that idiom is NOT what produced this dialog — it was replaced anyway (see below) but it is not the cause, and the ledger should not record it as one.

  **Merged the same night, with two more defects fixed and each reproduced before the fix.** The close-down sequence had drifted into two tools that both quit Chrome and neither of which did the other's job — `scripts/office_close.command` (EOD chain + VM push + sleep) and a Mac-local `Office Close.app` (note capture + RAM sweep) — so running one silently skipped either the artifact push or the memory sweep. They are now one ordered pass in the repo script; the `.app` is a **thin launcher holding no logic**, since two copies of a close-down routine is the drift that caused this. (a) `tell application X to quit` LAUNCHES a non-running app; an `app_running` guard now reads `ps -axo comm=` first, and the bogus app name `"Code"` was dropped (no such bundle exists — it errored silently behind `2>/dev/null` every night). `comm=` not `command=`: the argument list self-matches, because the grep in the pipeline carries the search string in its own argv, which was observed reporting Docker and VS Code as "running" when neither was. (b) the orphaned-`main.py` sweep regex is now ANCHORED to `<pid> <python-binary> [flags] main.py`; unanchored it selected a wrapper `zsh -c` whose argv merely MENTIONED `main.py`, which escaped being killed only because that same argv happened to contain the word `uvicorn` and hit the protected-list filter. Guardrail verified by experiment, not assertion: two dummy `main.py` workers were started and swept while `uvicorn` (PID 58268, 20 days up) survived. `--dry-run` added. **NOT verified: the full run end-to-end.** The dialog, the tracker append, phases 1-4 and the sweep were each exercised (dry-run plus a stubbed dialog), but no real `pmset sleepnow` was issued and the EOD-chain slow path did not execute, because the tier table was already fresh (`as_of 2026-07-30T19:15:48`). First real slow-path run is still unobserved.

  **Standing limitation, stated because the owner's expectation was explicitly "shut the lid and let it run": it cannot.** Closing the lid sleeps the Mac; work in flight is frozen, not continued, and resumes only on wake. `caffeinate -i -s` now wraps the EOD chain and holds off IDLE sleep (`-s` is AC-only per its man page, and this Mac is on AC), but neither flag survives a clamshell close — only `sudo pmset -a disablesleep 1` does, which is a system-wide setting that would keep a closed laptop awake in a bag and was deliberately NOT set. In the common case this does not matter: when the tier table is already fresh the whole pass takes ~10s and the script sleeps the Mac itself, so the lid is redundant. It matters only on the slow path, where the script now prints a loud KEEP THE LID OPEN warning before starting the chain.
