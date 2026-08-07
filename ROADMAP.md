# ROADMAP.md — the V1.x pipeline

*Created 2026-08-07. This file is the ORDER OF WORK, not a promise about
dates. `HANDOVER.md` remains the state of the system today; `DECISIONS.md`
remains the record of why things are the way they are. Nothing here is
approved to build — each item becomes work when the architect says so, and
V1's code freeze holds until then.*

**Read the freeze first.** Every item below touches sizing, exits or the
data layer. None of them is a hotfix. The 2026-08-05 Sunday freeze permits
observability and hygiene only; each of these needs an explicit unfreeze.

---

## V1.1 — Dynamic Position Sizing

**Replace the static ₹10,000 per-trade risk cap with a sized one.**

The cap is doing real work today and doing it bluntly. From the live
2026-08-07 session, every one of these was refused for size, not for signal:

| Underlying | Refusal |
|---|---|
| NIFTY 50 | max loss ₹10,166–10,257/lot vs the ₹10,000 cap — missed by ~2% |
| INFY.NS | ₹12,240–12,520/lot |
| TCS.NS | ₹14,692–15,008/lot |
| HDFCBANK.NS | ₹12,708–17,062/lot |
| RELIANCE.NS | ₹20,900–22,925/lot |

A flat rupee cap denominated against a growing book gets tighter as the
book grows, and it cannot tell a ₹10,166 NIFTY condor apart from a
₹22,925 RELIANCE spread except by the number. Two candidate replacements,
both standard:

* **Portfolio %** — risk a fixed fraction of equity per trade. Scales
  with the account, which is what "hard cap" was approximating.
* **ATR-based** — size so that a stop at N×ATR costs the same fraction of
  equity regardless of instrument volatility. `dynamic_pricer.atr` and
  `plan_tracker.atr_from_bars` already compute ATR; no new machinery.

**What must not be lost.** The cap is currently the ONLY thing standing
between the desk and a single trade that dominates the book. Whatever
replaces it inherits that job, and the replacement is a Department 3
decision (only Risk blocks — #63), gated on the proposal ledger showing
what the current cap actually costs. **`data/proposal_ledger.jsonl` (built
2026-08-07) is the evidence base for this item** — it now records every
refusal with the rupee figure the refusal names, so "how much did the cap
cost us" becomes a query rather than an argument.

**Prerequisite:** ≥2 weeks of proposal-ledger rows.

**Update 2026-08-07 (evening).** The pool was raised ₹2,00,000 →
₹10,00,000 by architect order, which removes the *margin* half of the
starvation but not the cap half: NIFTY 50 was missing the ₹10,000
per-trade cap by ₹166–257/lot, and that refusal is unchanged by more
cash. The `ghost_tracker` built the same day now measures what those
cap-refusals were worth — that is the number this item should be decided
on, not on the refusal count.

---

## V1.2 — Smart Exits (opt in the dormant ATR trailing stop)

**The machinery is already built, tested, and switched off.**

`plan_tracker`'s ATR trailing stop shipped 2026-08-05: a floor that
ratchets to `atr_mult × ATR` below the highest high since entry, never
downward, clamped so it can never sit below the plan's own stop — strictly
risk-REDUCING versus the fixed bracket. It is **opt-in per entry** via
`plan.trailing.atr_mult`, and absent that key `_resolve` is byte-identical,
which is why every existing journal row is untouched.

So V1.2 is not a build. It is a decision to start writing that key, plus
the evidence to justify it.

**Two hard constraints already established, do not relitigate:**

1. **LONG EQUITY PATH ONLY.** `_resolve_spread` is deliberately untouched
   and an anti-drift test enforces it. A vertical is defined-risk — max
   loss is capped by construction — so a trail adds no protection there
   and would only cut winners early.
2. The floor ratchets AFTER the exit checks, so a wide bar can never both
   lift the floor and trip it. Any change to that ordering re-opens a
   look-ahead bug.

**What is missing:** nothing in code. What is missing is a chosen
`atr_mult` and the out-of-sample evidence for it. The equity desk's own
closed positions are the sample.

---

## V1.3 — Cross-Asset integration

**Turn the capture-only tap into something the desk can read.**

Where it stands as of 2026-08-07:

* `src/ingestion/cross_asset.py` is **wired to cron** (daily 19:40 IST,
  added 2026-08-07) and writing `data/lake/cross_asset/`.
* **CRUDE returns bars** (verified from the VM: 3 bars, last 08-06).
* **GOLD_INDIA is dead**: `CA-410 contract expired 2026-08-05`. MCX
  futures ids die with their contract, and an expired id returns nothing
  silently rather than erroring — hence the explicit staleness check.
* `config/global_indices.json` ships **EMPTY BY DESIGN**. No global index
  id has been verified against the scrip master, and the house rule
  forbids writing an unverified one.

**The order of work, smallest first:**

1. Roll GOLD_INDIA's contract id (and build whatever keeps it rolled — an
   id that expires quarterly and is renewed by hand will rot again).
2. Verify global-index ids against the scrip master and fill
   `config/global_indices.json`.
3. Accumulate. A cross-asset signal needs history, and the lake has days.
4. **Only then** consider reading it from anywhere on the trading path —
   and that crossing is a Department 5 decision gated on a passed
   statistical test, exactly like the Macro Regime Engine's. Capture is
   free; execution authority is not.

---

## Not on this roadmap, deliberately

* **A broker/order path.** Paper money only. No order path exists in
  `src/` and none is planned (house rule, `CLAUDE.md` §7).
* **Wiring the Macro Regime Engine to sizing or entry.** Zero execution
  authority by standing rule; it is a Department 5 decision gated on a
  passed statistical test, never a code change taken on initiative.
* **The midcap momentum leg.** NIFTY MID SELECT reads an honest 0 until a
  midcap series is added to `config/sector_universe.json`. That is a
  one-file analysis-side addition already marked freeze-compatible, not a
  roadmap item.
