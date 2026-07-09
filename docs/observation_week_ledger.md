# Observation Week Ledger (2026-07-09 → triage)

Running log of every operational anomaly, error, and hotfix during the
observation week. One entry per issue, **verified against logs/DBs before
being written** — this file feeds next week's triage review, so no
unconfirmed claims. Newest issues appended under their date.

Conventions: `Symptom` = what was observed (Discord/logs), `Root cause` =
confirmed mechanism, `Resolution` = what was actually done + commit ids,
`Follow-up` = anything the triage should still decide.

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
