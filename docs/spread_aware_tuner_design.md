# Spread-Aware Tuner — Design (draft, not built)

Status: **design review only** — no code. Owner directive 2026-07-27,
Option 2 ("scope the spread-aware tuner properly, design doc first").

**Why this exists.** `src/tuner.py` learns only from journal entries with
`action == "BUY"` that carry a `plan` ([tuner.py:71](../src/tuner.py)).
The desk trades options spreads (`action == "SPREAD"`). The tuner is
therefore structurally blind to the live book: 17 SPREAD entries (15
resolved) are invisible to it, while the 3 equity BUYs it *can* see sit
below `TUNER_MIN_SAMPLES` (5). `data/brain_weights.json` has read
`resolved_trade_count: 0` since 2026-07-05. This is architectural drift
from the equity era, not a bug in the tuner's own logic — which is sound.

`tuner.py` carries a `# MANUAL OFFLINE TOOL` marker (Phase-1 audit
2026-07-25, `1003611`), protected by CLAUDE.md Rule 5. **This design does
not remove that marker.** Whether the spread-aware tuner earns a cron slot
is a separate decision at the end, not an assumption at the start.

---

## 0. Ground truth — what the live book actually contains

Measured 2026-07-27 against `data/journal.jsonl`, not assumed:

| Fact | Value |
|---|---|
| Resolved spreads (`r_multiple` present) | **15** |
| Distinct `spread.strategy` values among them | **1** — `bear_put_spread` |
| Resolutions | `profit_take` 10, `pre_expiry_exit` 5 |
| R-multiples | 5 losses ≈ −0.95, 10 wins +0.98…+1.61 |
| Average R | **+0.62** · win rate 66.7% |
| `regime` stamped on resolved entries | **7 of 15** — see below |
| Distinct regimes among those 7 | **1** — `('bearish', 'mid')`, all seven |
| `pattern_tags` shape | `['bear_put_spread']` — one element only |

**Correction (2026-07-27).** An earlier draft of this doc claimed live
journal entries carry `regime: None` and that a journaling fix was needed
to "stop the bleeding." **That was wrong, and it was my error** — I
inspected only the oldest entry and generalized. `options_proposer.
to_journal_entry()` has stamped `entry["regime"] = regime_for(view, vix)`
since commit `ef631db` (2026-07-09, Regime-Aware Memory). Every entry from
2026-07-13 onward carries it correctly. Only the 8 entries dated 07-09/07-10
— which predate the stamp landing — are `None`. **No fix is required and
none was made.** Those 8 are not backfillable: the VIX reading at their
entry moment was never recorded, and reconstructing it would be exactly the
fabrication Rule 3 forbids. They stay `None`, honestly.

**The two findings that shape this design:**

1. **One structure.** All 15 resolved trades are `bear_put_spread`. A tuner
   that learns a weight *per strategy* would learn exactly one number
   applied to every trade — a uniform rescale, not learning. Iron condors
   exist in the code but have never resolved live.
2. **One regime.** All 7 regime-stamped trades are `bearish` + `mid` VIX.
   So `vix_band` is *recorded correctly and still useless as a learning
   axis* — not for lack of data, but for lack of **variance**. There is
   nothing to contrast against.

Together these mean the live book is currently **degenerate along every
structural axis**: one structure, one regime, one direction. Any archetype
partition over it collapses to a single bucket. This is the central
constraint the design must respect, and it is a fact about the book's short
history, not a flaw in the tuner.

---

## 1. Challenge 1 — mapping options archetypes to the learning logic

The equity tuner's archetypes (`fresh_cross`, `rsi_oversold`) worked because
they *partitioned* the book — trades genuinely fell into one or the other,
and their average Rs could diverge. The options analogue must do the same.
Given §0, the candidate axes ranked by how well they partition today's book:

| Axis | Source field | Partitions today? | Verdict |
|---|---|---|---|
| Strategy structure | `spread.strategy` | **No** — 15/15 identical | Defer until ≥2 structures resolve |
| Volatility band at entry | `regime.vix_band` | **No** — available on 7, but all 7 are `mid` | Defer until the book sees another band |
| Trend read at entry | `regime.trend` | **No** — all 7 `bearish` | Defer until a bullish/range trade resolves |
| Exit style | `outcome.resolution` | **Yes** — 10 `profit_take` / 5 `pre_expiry_exit` | **Diagnostic only, NOT learnable** (§1b) |
| Calendar cycle at entry | recomputed from `date` via `src/cycles.py` | **Yes** — pure date math, no stamping needed | **The one honest axis available today** |
| Spread width / moneyness | `spread.spread_width`, `entry_spot` vs strikes | Yes, continuously | Viable, needs bucketing (§1c) |

### 1a. No prerequisite journaling work — the plumbing is already correct

Struck. An earlier draft proposed stamping `regime` at proposal time; that
is already done (`ef631db`, see the §0 correction). The data is being
captured properly. What is missing is **variance**, which no code change can
manufacture — only more trading across different conditions produces it.

The practical consequence: `vix_band` and `trend` are correctly-populated
axes that must stay dormant until the book actually trades a second regime.
The tuner should scaffold them and pin them at neutral, so they activate on
evidence rather than needing a future code change.

### 1b. Why exit style must NOT be a learned weight

`outcome.resolution` partitions cleanly (10/5) and correlates almost
perfectly with R — `profit_take` trades are the winners by construction, and
`pre_expiry_exit` trades are mostly the losers. Learning "weight
`profit_take` higher" is **circular**: it is the outcome relabelled as a
predictor, and it is not knowable at entry. It belongs in the printed
diagnostic report (like `pattern_tag_report` today — visible, never fed into
weights), never in `weights`.

This is the same circularity `stat_gates` already guards against by refusing
to derive a null from a pattern's own R.

### 1c. Recommended first archetype set

Ship with **two** axes, both honestly computable at entry:

1. **`cycle:*`** — already implemented and proven in `tuner._cycle_points`
   ([tuner.py:155](../src/tuner.py)). It recomputes cycle membership from
   the entry's own date via pure calendar math, so it works retroactively on
   all 15 resolved spreads with zero look-ahead. **This axis needs no new
   data and is the only one with any chance of clearing the floor today**
   — and at `TUNER_MIN_SAMPLES = 10` (§3 decision below), even that is
   unlikely on 15 trades.
2. **`vix_band:*` and `trend:*`** — scaffolded, pinned neutral. Activate
   automatically once a second band/trend accumulates ≥`TUNER_MIN_SAMPLES`.

`strategy:*` is likewise scaffolded but stays at neutral `1.0` until ≥2
structures clear the floor independently — enforced in code, not by
convention, so a second structure appearing doesn't silently start moving
weights before it has evidence.

**A degeneracy guard is required.** Because every axis currently collapses
to one bucket, the tuner must refuse to emit a weight for an axis with only
a single populated bucket, even if that bucket clears the sample floor.
Learning "bear_put_spread = 1.16" when it is the only structure traded is
not a discriminator — it is a uniform rescale of the entire book, which
changes nothing relative to itself while *looking* like learning. Proposed
rule: **an axis needs ≥2 buckets each ≥`TUNER_MIN_SAMPLES` before any of
its weights leave 1.0.**

---

## 2. Challenge 2 — extracting P&L metrics from the spread logs

Mostly solved already; the spread outcome is *richer* than the equity one.

**Use `outcome.r_multiple` as the learning unit, unchanged.** It is
`pnl_net ÷ (max_loss × lots)` — already risk-normalized, already net of the
full 2026 friction stack (STT, stamp duty, brokerage, exchange/SEBI, GST,
laddered slippage), and already the unit `tuner._weight_for` consumes. No
new arithmetic is needed, which is the point: the equity and spread paths
should agree on what "one unit of result" means.

Reuse verbatim, no changes:
- `_weight_for()` — capped linear rule, bounds `(0.5, 1.5)`, sensitivity `0.25`
- `TUNER_MIN_SAMPLES` floor semantics — below it, weight pinned to exactly `1.0`
- `_cycle_points()` — floor-gated, capped ±1.0/cycle

**Two exclusions that must be explicit in code:**
- `outcome.hypothetical` — the #31 tracked-but-not-real shadows, already
  excluded by `performance.py`; the tuner currently has no such filter
  because equity BUYs never carried the flag. Spread entries can.
- `mode = 'blocked'` opportunity-cost rows (Directive 1, 2026-07-27) — these
  are gate bookkeeping, not trades the desk took. `trial.py` already keeps
  them out of the learning corpus via three layers; the tuner must not
  re-admit them through a different door.

**Fix the unconditional overwrite** (found in the 07-27 review):
`write_weights()` rewrites `brain_weights.json` even when there is nothing
to learn, so a truncated journal could flatten real learned weights back to
neutral. Proposed: refuse to write when `resolved_trade_count` is 0 *and* a
file with a non-zero count already exists — abstain loudly rather than
silently degrade. Same doctrine as `performance.py`'s honest abstention.

### Sample-size reality check

With 15 resolved trades and a floor of **10** (§3), plus the ≥2-bucket
degeneracy guard above, the honest expectation is that **the first run
emits nothing but neutral weights.** No axis currently has two populated
buckets, so none is eligible regardless of sample count.

That is the correct outcome, and this design should be judged on whether it
abstains cleanly rather than on whether it produces interesting numbers
early. A first run that outputs all-neutral is a **pass**. If it produces
non-neutral weights on today's book, something is wrong and the degeneracy
guard has failed.

---

## 3. Challenge 3 — wiring `forecast.py` into the live options proposer

**This is the part that must not be done as a code change on its own
initiative, and this document recommends against scoping it now.**

Current state, verified: `forecast.py` is imported only by `discord_bot.py`
and `confluence/evidence.py`. The live options path
(`market_loop` → `options_proposer` → `portfolio_manager` / `plan_tracker`)
does not consult it at all. So "wiring forecast back in" is not reconnecting
something that came loose — it is **granting execution authority to a
scoring surface that has never had it.**

Three standing rules govern that, and all three point the same way:

- **CLAUDE.md Rule 5** — wiring an advisory engine's output to sizing or
  entry is "a Department 5 decision gated on a passed statistical test —
  never a code change you make on your own initiative."
- **Decision #63** (composition law) and the twice-failed skeptic
  **#44/#50** — sizing/conviction changes have been deferred to the Phase-4
  harness three times. H4 is currently going through exactly that process.
- **The registry lifecycle** — `validation/registry.py` exists so that
  nothing reaches a live surface without CANDIDATE → TRIAL → VALIDATED, with
  `stat_gates.promotable` requiring a Wilson lower bound that beats a
  structural null on real (not sim-only) evidence.

Weights learned from **15 trades, all one structure, avg R +0.62** would not
survive that gate, and should not be asked to. The honest sequence is:

1. Ship the spread-aware tuner as **advisory/read-only** — it writes
   `brain_weights.json` and prints its report; nothing consumes the weights
   for a live decision. Identical doctrine to `portfolio_greeks` (#71) and
   `performance` (#72), both of which are deliberately not wired into the
   loop.
2. Let it accumulate across regimes. Re-read it when the resolved-spread
   count is materially larger and more than one structure has traded.
3. **Only then** open the wiring question, as a Department 5 decision with a
   registry trial behind it — not as an implementation detail of this doc.

Recommendation: **descope challenge 3 from the build.** It is a governance
decision wearing an engineering costume, and taking it now would repeat the
exact pattern (hand-wiring a weak-sample signal into live sizing) that
decisions #44/#50/#63 were each written to prevent.

---

## 4. Proposed build order

| Step | What | Risk | Gate |
|---|---|---|---|
| ~~1~~ | ~~Stamp `regime` on live entries~~ | — | **Already done** (`ef631db`). Struck. |
| 2 | Spread-aware corpus reader + `cycle:*` axis + `vix_band:*`/`trend:*`/`strategy:*` scaffolded-neutral + the ≥2-bucket degeneracy guard + the two exclusions + the overwrite fix | None — advisory, writes only `brain_weights.json` | Suite green; **first run expected all-neutral** |
| 3 | (folded into step 2 — the axes are scaffolded together and self-activate on evidence) | — | — |
| 4 | Cron slot decision (removes the Rule 5 marker) | Low | Only once step 2 produces a non-neutral read worth refreshing weekly |
| 5 | Live wiring (challenge 3) | **High** | Dept 5 + registry trial. **Not in this build.** |

Step 2 is additive and reversible — it writes one JSON file nothing
currently consumes for a live decision. Step 4 is the first that touches the
Rule 5 marker and needs the owner's explicit sign-off. Step 5 is out of
scope by the recommendation above.

## 3. Settled decisions (owner, 2026-07-27)

1. **Challenge 3 (live wiring) is descoped.** The spread-aware tuner runs
   purely advisory/shadow until the registry promotes it. Owner: *"wiring it
   now violates our annotate-then-earn doctrine."*
2. **`TUNER_MIN_SAMPLES` raised 5 → 10** (`config.json`), aligning with
   `stat_gates`' 8–30 range instead of inheriting the equity-era number.
   **Applied.** Note this is a *global* value shared with the equity BUY
   path; that path currently has 3 resolved trades, so it was already
   neutral at 5 and remains neutral at 10 — no behaviour change today.
   `tests/test_tuner.py` references the constant symbolically, not as a
   literal, so the bump does not perturb the suite.

## 5. Open questions for review

1. **Should the tuner keep the equity BUY path at all?** It currently learns
   from 3 equity trades that will likely never grow (the desk is options
   now). Options: keep both corpora separate with their own floors, or
   retire the equity archetypes. Leaning keep-separate — retiring it deletes
   history for no gain.
2. **Should `TUNER_MIN_SAMPLES` eventually split per corpus?** It is now one
   global number serving both the equity archetypes and the future spread
   archetypes. If the two books ever diverge in trade frequency, they will
   want different floors. Not urgent — flagged so it isn't discovered later
   as a surprise.
