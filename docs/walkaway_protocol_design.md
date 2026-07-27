# The 3-Day Walkaway Protocol — Directive 3 design (2026-07-27)

**Owner directive:** the system must be safe to ignore for 3 days. Constraint:
**NO NEW ENGINES** — reuse the existing halt stack, the one notifier door, and
the existing card/de-dup patterns.

## What the 07-27 code audit established (verified, not assumed)

| Fact | Where |
|---|---|
| Both firm halts live in ONE ordered list at the single entry door | `portfolio_manager.ENTRY_HALT_CHECKS` → `request_entry` |
| Exits continue during any halt: settlement has no halt check | `release_margin` (deliberately halt-free) |
| Entry and exit loops are separate asyncio tasks — a halt starves entries only | `master_scheduler.run_automated_session` |
| Daily 3% breaker already fires one card per IST day | `_daily_breaker_card` (ledger = `account_events`) |
| **The 10% risk-of-ruin halt is SILENT — log row only, no Discord card** | `release_margin` transition / `request_entry` rejection |
| CEO brief + EOD summary do not show halt state (2h report does) | `ceo_brief.py`, `eod_summary.py` |
| Equity desk has its own 10% desk-ruin halt; firm halts gate both desks | `equity_desk` (its desk card already shows ⛔ DESK HALTED) |

## Owner rulings (2026-07-27, in chat)

1. **Resume semantics: NO override door.** A risk-of-ruin halt is final for
   that capital era. The only way back is the existing clean-sheet reset
   (fresh base, old era archived) — a deliberate owner action, never a
   command this layer provides. The card states this; no resume code exists.
2. **Reminder cadence: daily re-fire.** The 🔴 SYSTEM PAUSED card fires at
   the halt moment and re-fires once per IST day while the halt is active.

## The build (communication layer only)

1. **`🔴 SYSTEM PAUSED` card** for the risk-of-ruin halt, plain English:
   what tripped, current drawdown, "entries blocked — exits still managing
   open positions", and "resume requires an owner clean-sheet decision".
   - Fired from the two transition sites (`release_margin` when a settlement
     trips the threshold; `request_entry` when a rejection finds the halt
     already up — the cold-start case) **and** from a daily sweep seam.
   - De-dup: one card per IST day via an `account_events` `ruin_halt_card`
     row — the exact `_daily_breaker_card` pattern, same ledger.
   - Card goes through `notifier.fire_broadcast` (the one door), fail-open:
     a Discord outage can never affect settlement or the entry gate.
2. **Halt banner in the daily papers.** `eod_summary` and `ceo_brief` open
   with a red HALTED line while any firm halt is active (this is also what
   triggers the daily re-fire — both run Mon-Fri; a halt cannot newly occur
   on a weekend because nothing settles). Non-halted days: zero change to
   either card.
3. **The regression test that pins the promise:** with a halted account,
   `release_margin` still settles and the equity curve still appends —
   exits-during-halt becomes tested behaviour, not an accident of history.

## Explicitly NOT built (and why)

- **No resume/override command** — owner ruling 1.
- **No new halt types.** The daily breaker's existing card stays as-is
  (wording upgrades belong to Directive 2).
- **No new ledger/db/config.** De-dup rides `account_events`; no kill switch
  needed because a reporting layer that fails open cannot affect trading.
