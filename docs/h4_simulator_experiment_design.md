# H4 Simulator Experiment — Design (draft, not built)

Status: **design review only** — no code yet. Owner directive 2026-07-27:
pivot to H4 (asymmetric position management), first deliverable is this
design doc, reviewed before anything is coded.

Source of truth for the hypothesis: `docs/hypotheses.md` §H4. This doc does
not restate the three hazards there; it answers the three questions the
owner asked, grounded in the actual `src/simulator.py` / `src/performance.py`
/ `src/portfolio_greeks.py` code as it exists today.

---

## 0. What the simulator does today (the baseline being modified)

`run_simulation()` (`src/simulator.py:294`) already enforces H4's opposite —
the current one-and-done gate — mechanically, via `blocked_until`:

```python
blocked_until = ""  # skip days while a simulated position is open
for i, (day, ...) in enumerate(bars):
    if not (start <= day <= end) or day <= blocked_until:
        continue
    ...
    blocked_until = outcome["exit_date"]   # set after every entry/resolve
```

One underlying = one open position, ever. A repeat signal on the same
underlying while a spread is open is silently skipped — never proposed, never
recorded. This is decision #68's hard block, replicated inside the
simulator. **H4's mechanism change is entirely inside this loop**: instead of
"skip while blocked," ask "does this repeat signal MANAGE the open stack?"

---

## 1. The Mechanism — confirmation vs. repetition

The owner's H4 note already names the exact failure mode: the #68 pileup
(nine near-identical bear put spreads) happened because `suggestions.analyze()`'s
`uptrend` bit stays "bearish" across sessions and re-fires the same view every
day it's asked. If H4 treats "signal fired again" as "continue," it
reproduces that pileup with a management label on it.

**Definition used here: continuation = the OPEN POSITION has moved in its
favor since entry, evidenced two ways, both must hold:**

1. **Unrealized mark improvement.** Re-price the open spread's *current*
   legs against *today's* synthetic chain (the same `build_synthetic_chain`
   model already used for entry/exit — no new pricing surface). Continuation
   requires `mark_now > mark_at_entry` for a credit spread's short-side
   decay-in-your-favor read, i.e. the position is genuinely ITM on paper, not
   merely "the checklist still says bearish."
2. **A fresh extreme, not a stale threshold.** The underlying's close today
   must be a new N-day extreme in the position's direction (new N-day low for
   a bear view, new N-day high for a bull view) — reusing the existing
   `analysis_from_closes()` closes slice, no new data source. This is what
   distinguishes "the trend continued" from "the trend is still technically
   true," the exact gap the owner's note identifies.

**Adverse = the negation, with its own threshold, not just "not-continuation":**
mark has moved against entry by more than a configurable buffer (e.g.
`H4_ADVERSE_MARK_PCT` of max_loss) — reusing `pt._spread_exit_costs`-style
mark-to-market that `_resolve_and_score` already computes for exits, called
early instead of at expiry/profit-take.

**Signal repetition alone (same `uptrend`/`fresh_cross` reading, no mark or
extreme confirmation) does nothing** — the day is skipped exactly as it is
today. This is the one-line guarantee that keeps H4 from re-creating #68: the
gate is loosened only along the two confirmation axes above, never along
"the checklist fired."

**Stack mechanics (per the owner's own note, hazard #3):** because spreads
don't pyramid like futures, "add" = open a NEW spread at today's strikes
alongside the existing one(s) for that underlying+direction; "trim" = close
the single WORST-marked spread in the stack in full (atomic basket exit,
matching `_resolve_and_score`'s existing all-or-nothing resolution — no
partial-leg trim is invented). A stack is capped at `H4_MAX_STACK` spreads
(config, proposed default 3) so "pyramid" can't become "unlimited."

---

## 2. The Telemetry — what proves or kills it

**Isolation of the comparison, inside the sim corpus only (see §3): run the
identical date range twice, tagged by policy.**

Add one column to `simulated_trades`: `policy TEXT` (`'baseline'` or
`'pyramid'`) and `stack_id TEXT` (groups the adds/trims belonging to one
logical position, NULL under baseline). Idempotency key (`sim_ref`) already
includes `strategy`/`expiry`/`basis`; policy gets folded into the ref hash so
baseline and pyramid runs never collide or overwrite each other's rows in the
same table — no second table needed.

**Metrics — reuse `src/performance.py`'s math (`sharpe`, `sortino`,
`max_drawdown`), NOT its `compute()` entry point**, because `compute()`
hard-reads `journal.read_all()` and the whole point of #49/#65 is that the
sim corpus must never be presented through the real-track-record door. A new
thin function (proposed home: `src/simulator.py` or a
`src/h4_experiment.py` companion, decided at build time) does:

```
rows_baseline = simulated_trades WHERE policy = 'baseline' AND <range/tag>
rows_pyramid  = simulated_trades WHERE policy = 'pyramid'  AND <range/tag>
for each: r_series = [r_multiple, ...]; pnl_series = [pnl_net, ...]
sharpe(r_series), sortino(r_series), max_drawdown(r_series), max_drawdown(pnl_series)
plus: stack-depth histogram (how often 1/2/3 spreads were concurrently open),
      trim_rate (trims ÷ adds — is it actually cutting losers or only adding),
      worst single-stack drawdown vs worst single-spread drawdown (baseline)
```

**Pass/fail bar (from the owner's own note, "genuinely improves risk-adjusted
return... not just raw P&L"):** pyramid graduates only if, over the same
signal set:
- Sortino(pyramid) > Sortino(baseline) — the downside-only bar, since a
  seller book's real risk is the loss tail, not upside variance, and
- max_drawdown_r(pyramid) does not exceed max_drawdown_r(baseline) by more
  than a pre-registered tolerance (proposed: 10%) — the anti-Martingale
  instinct fails if it merely trades a fatter left tail for a better mean,
- both measured on the same underlying signal set, same date range, so the
  only variable is the management policy.

**Vega/Delta ceiling (owner's hazard #3, guardrail #71):** every simulated
"add" is passed through `portfolio_greeks.aggregate()` +
`evaluate()` against the stack's own open spreads before it's allowed to
record. An add that would breach the configured net-Delta/net-Vega budget is
refused and counted (`stack_capped_by_greeks` stat), never silently sized
down — so the experiment also reports how often the Greeks ceiling, not the
`H4_MAX_STACK` count cap, was the binding constraint. That numeric split
matters for #71: if the Vega cap never binds, it isn't actually doing
anything in this experiment.

---

## 3. The Data — isolation from real trading logs

Already structurally true, verified by reading, not assumed:

- **`simulated_trades` is its own table**, additive, separate from
  `journal.jsonl` / `outcomes`. `_record()` (`src/simulator.py:262`) writes
  there and *also* calls `brain_map.record_resolved_entry()` — the same
  write the Sleep Phase's causal-link miner reads. That second write is
  already how sim trades reach `graph_edges`; H4 runs would do the same,
  tagged with `policy`, so the causal miner sees pyramid-policy trades as
  their own population if it ever keys off `simulated`/`policy` metadata —
  worth a follow-up check but not new risk, since sim trades already flow
  there today.
- **`performance.py` never reads `simulated_trades`** — confirmed by
  `resolved_returns()` (`src/performance.py:63`), which calls
  `journal.read_all()` exclusively. H4's baseline-vs-pyramid comparison uses
  its own reader over `simulated_trades`, so it can never leak into the real
  Sharpe/Sortino card the owner sees on the CEO brief.
- **No broker, no journal write, no notifier** — `simulator.py`'s existing
  safety block (module docstring, guard-tested) is untouched by adding a
  `policy` column; the pyramid policy only changes which synthetic spreads
  get opened/closed inside the sim book (`{"cash": SIM_BOOK_CASH, ...}`),
  never `portfolio.json`.
- **New isolation needed:** the `policy` tag itself, so a stray query against
  `simulated_trades` (e.g. any future dashboard or digest reading that table)
  doesn't average baseline and pyramid rows together into a meaningless
  blended number. Proposed convention: any reader of `simulated_trades` for
  a performance read must filter `policy = 'baseline'` unless explicitly
  computing the H4 comparison — flagged here so it's not a silent trap for
  whoever builds the next sim-corpus consumer.

---

## 4. Config surface (new, all additive to `config.json`)

```
"h4_experiment": {
  "enabled": false,
  "max_stack": 3,
  "adverse_mark_pct_of_max_loss": 25,
  "extreme_lookback_days": 20,
  "drawdown_tolerance_pct": 10
}
```

Defaults chosen conservatively (tight stack cap, meaningful adverse
threshold) so a first run is a genuine test, not a policy tuned to win.

---

## 5. What this design does NOT do

- Does not touch `blocked_until` semantics for any signal outside H4's own
  test path — the one-and-done gate (#68) stays the production default;
  `policy='pyramid'` is opt-in per simulation run only.
- Does not register H4 in `validation/registry.py` — per the owner's own
  note, that only happens if the simulator result clears the bar in §2.
- Does not modify `plan_tracker`'s live resolution helpers — reused
  read-only (`_resolve_spread`, `_spread_exit_costs`), never edited.
- Does not build a new pricing model — the existing `build_synthetic_chain`
  is reused for both the entry mark and every subsequent re-mark check.

---

## Open questions for review before coding

1. **Extreme-lookback window (proposed 20 days)** — arbitrary; is there a
   preferred horizon, or should it default to the trade's own `days_in_trade`
   so far (self-relative rather than a fixed lookback)?
2. **Adverse threshold (proposed 25% of max_loss)** — trim-trigger sensitivity
   directly trades off "protects capital" vs "chops winners that dipped
   briefly." Worth a first run at 2-3 thresholds rather than committing to one?
3. **Where does the comparator function live** — a new small module
   (`src/h4_experiment.py`) vs. extending `src/simulator.py` directly? Leaning
   new module to keep `simulator.py`'s existing single-position mental model
   intact for readers who never touch H4.
