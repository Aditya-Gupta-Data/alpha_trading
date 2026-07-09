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
