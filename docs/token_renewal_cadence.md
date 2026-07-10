# Token Renewal Cadence — Single 07:00 IST Standard (decision, 2026-07-10)

**The rule: exactly ONE scheduled DhanHQ token renewal exists in this
system — the 07:00 IST job in the user crontab installed by
`scripts/setup_cron.sh`. Nothing else may mint tokens on a schedule.
Not root's crontab, not a systemd timer, not a Mac cron/LaunchAgent.**

## Why (ledger Issue 10, 2026-07-10)

DhanHQ allows **one active access token per client id** (decision #48):
minting a new token silently kills the previous one, and V2 TOTP codes
are effectively single-use. Two independent renewal schedules therefore
race each other by construction. This was observed live twice:

- 2026-07-09 (Issue 5): a "mystery" renewal invalidated the running
  session's in-memory token mid-market — that renewal turned out to be
  root's undocumented 12h cron (below).
- 2026-07-10 (Issue 10): the documented 07:00 IST renewal was rejected
  with `Invalid TOTP` while root's midnight run had succeeded hours
  earlier — the engine survived on the root job's token by luck, not
  design.

## The undocumented duplicate (what to remove)

Found 2026-07-10 in **root's crontab** on `alpha-trading-vm`
(`/var/spool/cron/crontabs/root`, created 2026-07-06 17:16 IST — a
leftover from the initial Phase 9 deployment, never recorded in any doc):

```
0 */12 * * * /home/adigupta1998/alpha_trading/venv/bin/python /home/adigupta1998/alpha_trading/src/renew_token.py >> /home/adigupta1998/renew.log 2>&1 && systemctl restart alpha-trading
```

It renews at 00:00 and 12:00 IST and **restarts `alpha-trading.service`
after every successful mint** (confirmed: that restart only bounces the
FastAPI gateway, not the `master_scheduler` trading loop — so no Issue-8
cooldown reset — but a 12:00 IST firing still drops the Discord
bridge/API for a few seconds mid-market).

## Removal steps — run ON THE VM, at deploy time only

> ⛔ **Observation-week boundary: do NOT run these before the triage
> review clears VM changes.** Sequence matters: remove the duplicate in
> the same session that deploys the renewal-retry hardening (see below),
> otherwise the surviving 07:00 job keeps its known failure mode with no
> backup.

```bash
# 1. Look before touching — confirm the root crontab holds ONLY the
#    renewal line (it did on 2026-07-10; re-verify, don't assume):
sudo crontab -l

# 2. If the renewal line is the ONLY entry, remove root's crontab whole:
sudo crontab -r

#    If other entries exist, remove just the renewal line instead:
#    sudo crontab -l | grep -v "renew_token" | sudo crontab -

# 3. Verify:
sudo crontab -l 2>&1   # expect "no crontab for root" (or no renew_token line)
crontab -l | grep renew_token   # the ONE remaining schedule: 0 7 * * *

# 4. Keep root's old log for the record (do not delete):
#    /home/adigupta1998/renew.log — last entries document Issue 10.
```

## Why the single cadence is now safe (the paired hardening)

A 07:00 IST mint produces a token that expires ~07:00 IST the next day —
i.e. the renewal fires at the moment its predecessor dies, with **zero
slack**. Under the old code, one transient failure (like Issue 10's
`Invalid TOTP`) meant a dead token all market day. Two changes pair with
this standardization:

1. **`src/renew_token.py` now retries TOTP rejections** — up to 3
   attempts, waiting 31s between tries so each retry lands in a fresh
   TOTP window. Non-TOTP failures still fail immediately and loudly, and
   `.env` is never touched on failure.
2. **`src/token_provider.py` + `dhan_client`** make the running engine
   re-read the token from `.env` mid-session (Issue 5's fix), so even a
   late/manual re-mint reaches the live session with no restart.

If the 07:00 renewal ever hard-fails all attempts, the fallback is a
manual run (`venv/bin/python -m src.renew_token` on the VM) — visible in
`logs/renew_token.log` and surfaced by the 20:30 IST ops sweep either way.
