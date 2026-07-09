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

### Context for triage (not an issue)
- The ops sweep's "silent job" heartbeats from the 02:00 IST card
  (renew/suggest/main/master_scheduler "did not run today") were all
  downstream of Issue 1's timezone shift, not independent failures.
