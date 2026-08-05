# PROP_ROADMAP.md — The climb, in percentages

**Companion to `SYSTEM_XRAY.md`. Written 2026-08-05 against measured state.**

**The vision, in the owner's terms:** a prop trading firm — multiple portfolios,
hired people running different desks, consistent returns, capital always
productively deployed, **never all-in on options**. Capital spread across
short-term, mid-term, long-term and temporary-trend buckets. Own capital
compounding toward **₹10 crore** before or while people join.

**Rules this document obeys:**
- **No dates anywhere.** Not one. Calendars slip; standards do not (decision #86).
- Every percentage is computed by the system from data it already writes, or
  from data whose writer is named in the milestone.
- No motivational language. A milestone at 12% is reported as 12%.
- Architecture gaps that the current design **fundamentally cannot support** are
  flagged in the milestone that hits them, not buried at the end.

---

## Where the climb actually starts

| Measured, 2026-08-05 | Value |
|---|---|
| Resolved real paper trades, all time | **19** (12W / 7L) |
| Distinct strategies in that record | **1** — `bear_put_spread` |
| Distinct directions in that record | **1** — short |
| Paper pool | ₹2,00,000 (clean sheet, #84) |
| Realized | ₹39,424 |
| Open positions | 7 (4 options, 3 equity — oldest open 14 days) |
| Patterns ever registered in the proving court | **0** |
| Macro declarations graded out-of-sample | **0** of 17 |
| Consecutive nights the discovery miner has skipped | **17** |

That is the true starting line. M1 is not "nearly done" — M1 has barely begun,
and the reason is honest: not enough trades have happened yet.

---

# M1 — TRUSTED EQUITY ENGINE

> *The system's paper record is statistically real, its reports answer the
> owner's questions, and it fails loudly.*

**Entry criteria:** met. The engine runs unattended.

**Exit criteria — six gates, all measurable, all computed from existing stores:**

| Gate | Threshold | Source of truth |
|---|---|---|
| **G1 · Sample** | ≥ 100 resolved real trades | `outcomes` where `journal_ref NOT LIKE 'sim%'` |
| **G2 · Significance** | Wilson 95% lower bound of the win rate **> the structural breakeven null** for the traded R:R | `validation/stat_gates.wilson_lower` + `structural_breakeven` |
| **G3 · Diversity** | ≥ 3 distinct archetypes with ≥ 20 resolved trades each, **and** both directions represented | `outcomes.archetype` + journal `direction` |
| **G4 · Drawdown** | max drawdown on the cumulative-R curve ≤ **8R**, never breached | `performance.max_drawdown` |
| **G5 · Reports** | all 10 owner questions in `SYSTEM_XRAY.md` §9(a) answerable from a card the system sends unprompted | checklist, human-scored, stored as a 10-item JSON |
| **G6 · Silence** | 30 consecutive trading days with **zero silent failures** — every skip, block, abstain and outage appears in a card | `ops_monitor` + the freshness sentinel (fix #6) |

**% formula (equal weight, each gate capped at 100%):**

```
G1% = min(100, resolved_real / 100 * 100)
G2% = 100 if wilson_lower > breakeven_null else 0        # binary, no partial credit
G3% = min(100, (archetypes_with_n>=20 / 3) * 100) * (1.0 if both_directions else 0.5)
G4% = 100 if max_dd_R <= 8 else max(0, 100 - (max_dd_R - 8) * 12.5)
G5% = answered_questions / 10 * 100
G6% = min(100, clean_consecutive_days / 30 * 100)

M1% = mean(G1..G6)
```

**Today: G1 19% · G2 0% · G3 17% · G4 100% · G5 ~20% · G6 0% → M1 ≈ 26%.**

G2 is deliberately binary because a *partially* significant edge is not an edge.
G4 sits at 100% only because the sample is too small to have drawn a real
drawdown yet — it will fall before it rises, and that is correct.

**The blocker nobody can code around:** G1 needs ~80 more resolved trades. At
the current rate (19 trades in ~4 weeks of live sessions), that is arithmetic,
not engineering. **The only lever that moves M1 faster is trade frequency and
strategy diversity — not more modules.** Note that #83's "NO NEW ENGINES"
constraint and this milestone agree with each other.

**Smallest next action available today:** ship `SYSTEM_XRAY.md` §9 fixes **1–5**
(all sub-20-line edits in `eod_summary.build_eod_card`, `ceo_brief._risk_field`,
`performance.run`). That moves G5 from ~20% to ~70% in one session, with no new
engine and no new risk.

---

# M2 — REAL MONEY, SMALL

> *Own capital, deliberately small size, the same engine.*

**Entry criteria (hard):**
- M1 = 100%.
- An **execution path exists and has been separately proven** — see the gap below.
- A **kill switch the owner can hit from their phone**, tested, that flattens
  nothing but blocks all new entries.
- Real-money sizing floor: no position risks more than 0.25% of the real pool.

## 🚩 ARCHITECTURE GAP — the biggest one in this document

**`src/` contains no broker/order path, by house rule (CLAUDE.md Rule 7:
"Paper money only. No broker/order path exists in `src/`. Do not add one.").**
`dhan_client` is explicitly data-only — no order methods. So M2 is **not a
config flip.** It requires a new department that does not exist:

| Missing piece | Why paper never needed it |
|---|---|
| Order placement + modification + cancel | `plan_tracker` closes positions by *deciding* they closed |
| Order-state machine (pending / partial / rejected / filled) | paper fills are instantaneous and total |
| Reconciliation against the broker's book | `margin_locks` **is** the book today |
| Real slippage & fill accounting | modelled by the slippage ladder (#70) |
| Rejection handling (margin, freeze qty, circuit) | silent reject only (`request_entry`) |
| Idempotency across restarts | a duplicated paper entry costs nothing; a duplicated real order costs money |

**This is a full milestone of engineering on its own, and it must be proven
against paper before a rupee moves.** Do not let M2's entry criteria read
"M1 = 100%" and hide this behind it.

**Success metrics (rolling, computed from the same tables):**
- realized real-money P&L ≥ 0 over any 20-trade window
- real-money slippage vs modelled slippage within ±30% (proves the paper model)
- zero order-state reconciliation breaks

**Kill criteria (any one → back to M1, immediately, no discussion):**
- drawdown > 6% of the real pool
- any reconciliation break (system's book ≠ broker's book)
- any unexplained fill
- two consecutive weeks where the weekly report contains a line the owner
  cannot explain

**% formula:**
```
M2% = mean(
  execution_layer_tests_passing / execution_layer_tests_total * 100,
  min(100, real_trades_resolved / 30 * 100),
  100 if reconciliation_breaks == 0 else 0,
  min(100, days_since_last_kill_trigger / 60 * 100)
)
```

**Smallest next action today:** nothing. M2 is correctly blocked. Do not
pre-build the execution layer — building it now creates a live order path
sitting next to an engine with 19 trades of evidence.

---

# M3 — CONSISTENT PROFITS

> *Not "profitable". Consistently profitable, with the drawdown discipline
> stated in advance.*

**Entry criteria:** M2 = 100% and ≥ 60 resolved real-money trades.

**Exit criteria — the windows, stated precisely so they cannot be moved later:**

| Metric | Window | Threshold |
|---|---|---|
| Rolling profitability | trailing **3 calendar months**, computed monthly | positive in ≥ 4 of the last 6 monthly readings |
| Sharpe (per-trade, R units) | trailing 100 trades | ≥ 0.8 |
| Sortino | trailing 100 trades | ≥ 1.2 |
| Max drawdown | all time, real money | ≤ 10% of peak equity |
| Recovery | any drawdown | recovered to prior peak within 40 trading sessions |
| Worst month | trailing 12 months | ≥ −6% |

All six already computable: `performance.sharpe`, `sortino`, `max_drawdown`,
plus `equity_curve` for the peak/recovery pair. Only the rolling-window
bookkeeping is new (~60 lines).

**% formula:** `M3% = (metrics_currently_met / 6) * 100`, reported with the
window's `n` beside it — a metric met on 20 trades reads as met-but-thin.

**Smallest next action today:** add `rolling_window(trailing_n)` to
`src/performance.py` and start computing it on the paper record now. The
numbers will be meaningless at n=19; the *plumbing* being ready is the point,
and it costs nothing to run early.

---

# M4 — MULTI-BUCKET PORTFOLIO

> *Four buckets. Options is ONE of them, with a hard cap. Capital always
> productively deployed.*

**Entry criteria:** M3 = 100%. **Non-negotiable** — running four strategy
families before one is proven is how a firm turns one edge into four unproven
ones.

## The bucket architecture

| Bucket | Horizon | Strategy family | Data it needs | Have it? | Risk budget | New modules required |
|---|---|---|---|---|---|---|
| **B1 · OPTIONS** | days–weeks | defined-risk spreads (today's engine) | chains, VIX, trend | ✅ all live | **≤ 25% of book, HARD CAP, enforced in `firm_treasury`** | none — cap enforcement only |
| **B2 · SHORT-TERM** | 2–15 days | momentum / mean-reversion on liquid equities | intraday 15m bars, delivery %, bhavcopy | ✅ **`intraday_15m.jsonl` is the orphan that becomes this bucket's fuel** | ≤ 20% | `analysis/short_term_signals.py`, an intraday bar store (the flat JSONL must become date-partitioned) |
| **B3 · MID-TERM** | 1–6 months | darling tiers + valuation (today's equity desk) | bhavcopy, filings, valuation, F&O liquidity | ✅ all live | ≤ 30% | none — this bucket already exists as `equity_desk` |
| **B4 · LONG-TERM** | 1 year+ | quality compounders + the GOLDBEES wealth lock | filings, annual reports, corporate actions | ⚠️ **partly — see gap** | ≤ 25% | corporate-action adjuster, `analysis/long_term_book.py` |
| **B5 · TEMPORARY-TREND** | opportunistic | regime-conditioned sector rotation | macro lake, `strategy_registry` recipes | ✅ data live, **authority not earned** | ≤ 15%, **and only after Stage B graduates a cell** | `strategy_registry` → execution seam (a Dept-5 gate, never a code change) |

Caps sum to 115% deliberately — they are ceilings, not an allocation. The
allocator picks inside them. Cash is a legitimate position; "always productively
deployed" must not become "always fully deployed".

## 🚩 ARCHITECTURE GAPS this milestone hits

1. **`account_state` is ONE ROW. `treasury_state` is ONE ROW.** There is no
   `portfolio_id` anywhere in the schema. `firm_treasury` today routes between
   exactly two desks by moving a single integer. **Five buckets need a real
   allocation table and a per-bucket equity curve.** This is a schema migration
   of the money layer — the highest-risk change in the entire roadmap, and it
   must happen while the book is flat.
2. **No corporate-action adjustment anywhere.** `bhavcopy_clerk` stores raw
   NSE closes. A 1:5 split on a B4 holding will silently show as an 80% loss,
   and `dynamic_pricer`'s anchored VWAP will be wrong for 60 sessions.
   Harmless for options and short holds; **fatal for a long-term bucket.**
3. **The e2-micro cannot host five buckets.** 965 MB RAM, ~62% used at idle,
   2.0 GB free disk, and `macro_nightly` is already the documented OOM risk.
   B2 in particular needs an intraday store that `intraday_15m.jsonl` (flat,
   unindexed, unbounded, 2.8 MB after three weeks) is not.
4. **B5 cannot be built by decision.** Rule 5 and #63: wiring macro output to
   sizing or entry is a Department 5 ruling gated on a passed statistical test.
   Stage B has graded **0 of 17** declarations. That clock runs on calendar
   time and nothing accelerates it.

**% formula:**
```
per bucket:  spec_written(20) + data_verified(20) + module_built(20)
           + paper_proven_20_trades(20) + cap_enforced_in_treasury(20)
M4% = mean over the five buckets
```
Today: B1 ≈ 80% (needs the cap), B3 ≈ 80% (proven-at-n=1), B2/B4/B5 ≈ 20%.
**M4 ≈ 44% — and that number is misleading**, because it is dominated by the
two buckets that already exist. Report it per bucket, never as one figure.

**Smallest next action today:** two things, both cheap and both reversible.
(a) Add the B1 hard cap to `firm_treasury` — it already clamps 15–60%; a
25%-of-book options ceiling is the same arithmetic and it makes "never all-in
on options" a property of the code rather than a preference.
(b) Date-partition `intraday_15m.jsonl` into the lake's standard layout before
it grows further. Both are structural, neither adds a strategy.

---

# M4 ADDENDUM — A / B / C

> **Approved by owner 2026-08-05. DESIGN-LEVEL ONLY.**
>
> **🔒 SEQUENCING LOCK:** design docs for M4A / M4B / M4C may be written at any
> time. **No code until M1 = 100% by its own six gates.** An agent that reads
> this section and starts building is violating an explicit owner instruction.
> The reason is the same one M4 already carries: the engine has 19 trades of
> evidence and one strategy. Staged scaling and allocation enforcement are both
> *multipliers* — they multiply whatever edge exists, including a negative one.

---

## M4A — POSITION SCALING MODULE

> *Replace binary in/out with staged position management.*

**What it changes:** today a position is one entry, one exit. `exposure_gate`
(#68) enforces exactly ONE open spread per underlying+direction — that gate
*is* the binary rule. M4A replaces it with: partial entry → rule-based adds →
rule-based trims → full exit only on thesis-break or stop.

**We already have the prototype, and it is honest about itself.** `docs/hypotheses.md`
§H4 states this hypothesis; `src/validation/h4_comparator.py` is the harness that
runs `baseline` (today's one-and-done gate) against `pyramid` (stacked adds);
`src/validation/h4_shadow.py` runs it nightly as `sleep_phase` Task J with
**zero execution authority** by owner ruling (2026-07-30). M4A is the graduation
of H4, not a new idea. That matters: the evidence path already exists.

### The rule grammar

Every scaling rule is a frozen, hashable predicate — the same contract
`validation/registry.py` already enforces for mined patterns (`pattern_id` =
canonical-JSON hash; re-discovery is a no-op, dead ends stay dead, #49).

```
RULE := { rule_id, kind, trigger, action, guard, provenance }

kind    := ENTRY_TRANCHE | ADD | TRIM | EXIT
trigger := an ALL/ANY tree over typed atoms, each atom point-in-time computable
           from data the system already writes:
             price_vs_entry(op, x_R)          -- R units, never rupees
             price_vs_level(level_ref, op, x_ATR)  -- level_ref ∈ darlings_levels
             bars_since(event, op, n)
             mark_improved(source="plan_tracker._spread_mark")   -- H4's own test
             regime_is(vix_band | trend | archetype)
             evidence_layer(layer, stance)    -- confluence/evidence, 6 layers
             gate_verdict(gate_name, verdict)
action  := SIZE_DELTA(fraction_of_full, cap)  -- fractions only, never absolutes
guard   := MAX_TRANCHES(n) | MIN_BARS_BETWEEN(n) | TOTAL_RISK_CEILING(R)
           | NEVER_ADD_BELOW_STOP | NEVER_ADD_INTO_EARNINGS(days_to_results)
provenance := { hypothesis_ref, registered_at, frozen_predicate_hash }
```

**Hard constraints on the grammar, so it cannot become a backdoor:**
- Sizes are **fractions of the already-approved full size**, never new absolute
  amounts. Scaling can never increase total risk beyond what entry sizing
  approved — `TOTAL_RISK_CEILING` is checked on every add.
- An ADD is still a proposal. It passes `portfolio_manager.request_entry`,
  `adaptive_sizing`, and the halt stack like any other entry. **Scaling is not
  an exemption from Risk** (#63 — only Risk blocks).
- A TRIM routes through `plan_tracker` — THE one settlement path. No second
  exit door, ever.
- `NEVER_ADD_BELOW_STOP` is non-negotiable and lives in the guard layer, not
  in a rule an experiment can turn off. Averaging down is not a scaling rule.

### Evidence logging

Every scaling event is journalled and evidence-stamped exactly like an entry —
`confluence/evidence.capture_for_entry` runs per tranche, so `src/explain.py`
can reconstruct *why the third add fired* six months later. This is not
optional: an unlogged tranche makes the whole trade unauditable.

### 🚩 Architecture gaps M4A hits

| Gap | Detail |
|---|---|
| **One trade = one row** | `outcomes.journal_ref` is UNIQUE and carries a single `r_multiple`. A staged trade has no single R — it has a weighted-average entry, a schedule of exits, and a realised R that depends on the whole path. **`outcomes` needs a parent trade id + tranche rows, or a companion `trade_legs` table.** This is a memory-layer migration. |
| **Margin locks whole-position** | `portfolio_manager.request_entry` locks one `margin_rs` per `journal_ref` and `release_margin` settles it once. Tranches need per-tranche locks under one parent, and partial release. |
| **`exposure_gate` is the opposite rule** | #68 exists to block a second position in the same underlying+direction. M4A adds them deliberately. The gate must learn the difference between *a duplicate* and *a sanctioned tranche* — a `parent_trade_id` check, not a removal of the gate. |
| **`adaptive_sizing` reads resolved outcomes** | Its Beta posterior is centred on break-even per resolved trade. Feed it tranches and every staged trade counts as N observations instead of one. It must consume parent trades, not legs. |

### How the proving harness validates scaling rules SEPARATELY from entry signals

This is the part that must not be fudged. A scaling rule bolted onto a good
entry signal will look profitable **because the entry was good**. The separation:

1. **Fixed-entry design.** The entry signal is held constant. `h4_comparator`
   already does exactly this: same entries, two management policies, compare.
   That is the template — extend it from one hypothesis to the rule registry.
2. **The measured quantity is the DIFFERENCE, not the P&L.** The statistic is
   `ΔR = R(staged) − R(baseline)` **paired per trade**. A paired test on the
   same entries removes the entry's contribution by construction. Registry
   stats are stored as ΔR, never as absolute return.
3. **Its own null.** Not the structural breakeven null (that is an entry-signal
   null). A scaling rule's null is **a random tranche schedule with the same
   number of adds, same total size, same guards** — i.e. "did the *timing* of
   the adds matter, or just the fact of adding?". `stat_gates` needs one new
   surrogate generator; the permutation machinery is already there.
4. **Separate registry namespace.** `kind = 'scaling'` in `candidate_patterns`,
   with its **own Benjamini-Hochberg batch**. A scaling rule must never inflate
   or ride an entry-signal batch's FDR denominator — `placebo.py` already runs
   its corrected batch in parallel for exactly this reason; scaling gets the
   same treatment.
5. **Walk-forward with the same embargo.** `trial.split_windows`, 5-day embargo
   unchanged, so a cross-boundary tranche resolution cannot leak.
6. **Shadow before live, always.** A registered scaling rule fires into
   `shadow_trades` (Task J's existing path) until it earns TRIAL → VALIDATED
   through the normal lifecycle. `validation/monitor.py`'s CUSUM auto-quarantine
   applies unchanged.

**Backtest requirements before any rule may be registered:**
- ≥ 100 paired trades in the discovery window, ≥ 100 in the validation window.
- ΔR Wilson-style CI **excluding zero** on the validation window alone.
- Survives the random-schedule null at the batch-corrected q.
- Survives a **cost-loaded** replay: every add and trim pays the full slippage
  ladder (#70). Staged management multiplies transaction count; a rule that only
  works frictionless is a rule that does not work.
- Simulated corpus results are reported **separately and never pooled** — the
  simulator's P&L is ~10× inflated (`SYSTEM_XRAY.md` §6). Sim proves mechanics,
  never economics.

**% formula:**
```
M4A% = mean( grammar_spec_written(100 or 0),
             registry_schema_migrated(100 or 0),
             paired_harness_generalized_from_h4(100 or 0),
             min(100, registered_scaling_rules_at_TRIAL / 3 * 100),
             min(100, rules_VALIDATED / 1 * 100) )
```
Today: **0%** — and correctly so. H4 exists as a shadow, not a registered rule.

**Smallest next action available today (design only):** write
`docs/position_scaling_spec.md` containing the grammar above and the paired-ΔR
statistic. Zero code. It also gives H4 a home to graduate into.

---

## M4B — USER-DEFINED ALLOCATION FRAMEWORK

> *Enforcement, not advice. The user sets the numbers; the system holds the line.*

**The design line, stated once and load-bearing everywhere below:** the system
**enforces, reports and alerts on the user's own policy. It never proposes what
the percentages should be.** That boundary is what keeps this risk-management
software rather than investment advice, and it must be enforced in code, not
just in intent — see the guard below.

### Portfolio setup — what is captured

| Captured at setup | Type | Used for |
|---|---|---|
| Bucket policy: options/short-term, equity/mid-term, debt/stability, cash/tactical | four percentages + hard floors/ceilings | the enforcement engine |
| Hard limits, user-worded | e.g. `options ≤ X%`, `debt ≥ Y%`, `cash ≥ Z%` | binding constraints |
| Risk appetite | **self-declared**, free choice | recorded and displayed; **never used to derive a suggested allocation** |
| Themes of interest | list | filters what is *shown*; never a buy reason |
| Investment mode | lumpsum / periodic top-up | top-up parking behaviour |

### The four buckets and how policy is checked

```
POLICY := { bucket: {target_pct, min_pct, max_pct}, ... } + invariants
INVARIANTS (system-checked at save time, not opinions):
  • sum(target_pct) == 100
  • min_pct <= target_pct <= max_pct for every bucket
  • every bucket named; a bucket the user wants at 0% is written as 0, not omitted
```

**Enforcement seam:** `src/exposure_gate.gate_entry` is already the pre-margin
gate every entry passes, already fail-open, already ledgers its blocks and
already routes an opportunity-cost row into `shadow_trades`. **The allocation
check belongs there** — one more verdict in an existing gate, not a new door.
A trade that would push a bucket past its ceiling is blocked with a named
reason; a policy breach that already exists (from price drift, not a trade) is
a **drift alert**, never a forced sale.

**Reporting:** one card section — `current % vs policy %` per bucket, with the
distance to each hard limit. Drift alerts fire when a bucket crosses its
min/max, de-duped per bucket per day (the `exposure_gate` ledger pattern).

**Periodic top-up (SIP-style):** incoming capital lands in the **cash bucket,
full stop.** Deployment happens only when the user's own policy calls for it.
The system never auto-deploys a top-up, because choosing when to deploy is a
recommendation.

### 🚩 The advice-boundary guard — build this first or not at all

The boundary is easy to state and easy to erode. Three code-level protections:

1. **No `suggested_allocation` field may exist anywhere in the policy schema.**
   Enforce with a test, the `equity_shadow_proposer` import-ban precedent.
2. **The policy object is read-only to every module except the user-facing
   setter.** No optimiser, no tuner, no LLM path may write a percentage.
3. **Language gate.** All allocation copy goes through one renderer (the
   `ceo_language` precedent — one place turns data into a sentence so no call
   site can skip the honesty gates). It emits only three sentence shapes:
   *you are at X%*, *your limit is Y%*, *you crossed it*. Never *you should*.

### 🚩 Other architecture gaps M4B hits

| Gap | Detail |
|---|---|
| **No policy layer exists at all** | `firm_treasury` routes between two desks with a *mechanical regime router* that decides the split itself (tilts on NIFTY trend, buy-depth, value, VIX). That is the system choosing an allocation — **the exact opposite of M4B's design.** Under M4B the router must become clamped-by-policy: it may move capital only inside the user's own min/max, or be retired for policy-driven portfolios. This is a genuine conflict between decision #80 and this addendum and needs an explicit owner ruling. |
| **Single-row money schema (already flagged in M4)** | `account_state` and `treasury_state` are one row each, no `portfolio_id`, no bucket dimension. A policy needs a real allocation table. |
| **No debt instruments** | `SECURITY_ID_MAP` covers equities, indices and one ETF (GOLDBEES, id 14428, resolved 07-20). **We cannot price a bond, a liquid fund, or an FD.** A "debt/stability" bucket currently has nothing to hold. Nearest available proxies are debt ETFs — each needs a verified scrip id through `scrip_master`'s wanted-list path, never a guessed one. |
| **Cash is not a tracked position** | Free margin exists (`account_state`) but "cash bucket %" as a first-class, reportable allocation does not. |

**% formula:**
```
M4B% = mean( policy_schema_spec(100/0), advice_boundary_tests_green(100/0),
             enforcement_wired_into_exposure_gate(100/0),
             allocation_report_on_a_card(100/0), drift_alerts_live(100/0),
             topup_parking_implemented(100/0) )
```
Today: **0%.**

**Smallest next action today (design only):** write
`docs/allocation_policy_spec.md` with the schema, the invariants, and the
three-sentence language gate. Then get the owner's ruling on the #80 conflict —
that ruling is the real blocker, and it costs nothing to ask early.

---

## M4C — MULTI-USER VIRTUAL LAYER (testing only)

> *Virtual portfolios for invited testers. Zero real money. Purpose: break the
> enforcement engine before real capital ever meets it.*

**Purpose, in the owner's words:** *"kuch gadbad nikal ke laana"* — find the
edge cases. That is a legitimate and well-chosen use of testers: an enforcement
engine is exactly the kind of software where other people's odd policies find
bugs your own never will.

**Scope, hard-bounded:**
- Virtual portfolios only. **No real money, no order path, no broker link** —
  and note M2's gap: no order path exists to accidentally connect to.
- Each tester sets their **own** policy. The system enforces theirs, suggests
  nothing (M4B's guard applies per-tenant).
- Educational-simulation framing throughout.
- Isolated data per user.
- **No real-money features until the owner explicitly approves a separate
  compliance review.** Written here so a future session cannot treat M4C as a
  soft-launch ramp.

**What testers are for, concretely — the bug classes to hunt:**
policy invariants that sum to 100 but are unsatisfiable; a bucket ceiling that
makes every trade blockable; drift alerts that fire in a loop; two buckets
claiming the same instrument; a top-up that lands mid-drift; zero-percent
buckets; policies that make cash mathematically impossible to hold.

### 🚩 Architecture gaps M4C hits — this is the largest tenancy gap in the repo

| Gap | Detail |
|---|---|
| **Decision #1: "personal use only — no multi-user infrastructure."** | M4C contradicts a locked decision. It must be **formally superseded in `DECISIONS.md`** with the scope written down (virtual-only, testers, no real money), not quietly ignored. This is the single most important non-code action in M4C. |
| **Zero tenancy anywhere** | Every path in `src/` is an absolute singleton: one `data/brain_map.db`, one `data/journal.jsonl`, one `config.json`, one `data/portfolio.json`. There is no `user_id` in any table. Isolation cannot be added by convention — it needs either per-tenant DB files behind one accessor seam, or a tenant column on every money table. **Per-tenant DB files is the safer shape** and fits the existing "one file per store" doctrine. |
| **Auth is one shared API key** | `api_server` is fail-closed (503 unset / 401 wrong) but has **no users, no roles, no per-request identity, no audit trail of who did what**. Multi-user needs all four. |
| **Discord is a single channel with a 5-card/day budget** | Per-tester notifications cannot ride the existing door. Testers need in-app reporting or nothing. |
| **The e2-micro** | 965 MB RAM, ~62% used at idle, 2.0 GB free disk, `macro_nightly` already the documented OOM risk. It cannot host N tenant simulations. M4C needs its own box, or it runs offline in batch. |
| **Data-protection posture is undefined** | Inviting outside people means holding other people's self-declared risk appetite and portfolio policies. There is currently no retention policy, no deletion path, no access log. |

**One flag, stated plainly and once:** even with zero real money and an
educational framing, inviting outside users into something that enforces
financial limits is a step with a legal dimension I am not the right judge of.
The design boundary in M4B (enforce, never suggest) is exactly the right
instinct. Worth a professional read before invitations go out, not before the
design is written.

**% formula:**
```
M4C% = mean( decision_1_formally_superseded(100/0), tenancy_seam_spec(100/0),
             per_tenant_isolation_tests_green(100/0), authz_with_audit(100/0),
             min(100, testers_running / 5 * 100),
             min(100, edge_cases_found_and_fixed / 10 * 100) )
```
Today: **0%.**

**Smallest next action today (design only):** write
`docs/multiuser_virtual_layer_spec.md` covering the tenancy seam (per-tenant DB
files behind one accessor) and the isolation test list. And draft the
`DECISIONS.md` entry that supersedes #1 — for the owner to approve, not for an
agent to land unilaterally.

---

## M4 addendum rollup

| Sub-milestone | Today | Gate |
|---|---|---|
| M4A Position scaling | 0% | M1 = 100% |
| M4B Allocation enforcement | 0% | M1 = 100% **+ owner ruling on the #80 router conflict** |
| M4C Multi-user virtual | 0% | M1 = 100% **+ #1 formally superseded** |

`M4_addendum% = mean(M4A%, M4B%, M4C%)` — reported beside M4's bucket
percentages on the weekly card, never merged into them. They are different
kinds of work: M4 is *what we hold*, the addendum is *how we manage and govern
it*.

**Design docs allowed now. Code is locked behind M1 = 100%.** Three specs, no
modules, no schema changes, no tests. If a future session finds itself editing
`src/` for any of A, B or C before M1 reads 100%, the sequencing lock has been
broken and the correct action is to stop and re-read this section.

---

# M5 — SCALE OWN CAPITAL toward ₹10 crore

> *The compounding path, and what physically changes at each tier.*

**Entry criteria:** M4 = 100%, and ≥ 12 months of real-money record.

| Tier | What changes |
|---|---|
| **₹2L → ₹10L** | Nothing structural. Same instruments, same sizing %. This tier exists to prove the % sizing model survives a 5× notional. |
| **₹10L → ₹50L** | Slippage stops being modelled and starts being measured per fill. Position sizing must consult **real** liquidity (`fo_liquidity` tiers become binding, not advisory). First hard capacity question: which darlings can absorb our size? |
| **₹50L → ₹2Cr** | The e2-micro is retired. Real market-data subscription (tick or depth). Intraday bar store becomes a real time-series DB, not JSONL. Execution algos (TWAP/slicing) become mandatory — a market order at this size *is* the slippage. |
| **₹2Cr → ₹10Cr** | Capacity becomes the binding constraint, not edge. Some strategies get *capped by design*. Tax and accounting become a first-class module. Custody/audit posture matters. B5's index-level recipes carry more capital than the single-name buckets, by capacity not preference. |

**% formula (log-scaled — a linear % would sit near zero for years and tell you nothing):**
```
M5% = min(100, log(current_own_capital / 200000) / log(10000000 / 200000) * 100)
```
At ₹2L: 0%. At ₹10L: 41%. At ₹50L: 70%. At ₹2Cr: 92%. At ₹10Cr: 100%.

**🚩 Gap:** there is **no capacity model** anywhere in the system. Every sizing
decision assumes our order does not move the price. That assumption is safe on
paper, fine at ₹2L, and false by ₹50L. `fo_liquidity` and bhavcopy turnover
give us the raw material; nothing consumes it as a capacity constraint.

**Smallest next action today:** none. This milestone is downstream of M2–M4 and
premature work here is waste.

---

# M6 — FIRM-READY

> *Multi-seat. What each hire owns, consumes, delivers, and must never let slip.*

**Entry criteria:** M5 ≥ 70% (~₹50L own capital) **and** M3 held for 12 months.
Hiring before a proven, documented edge means paying people to run an experiment.

The 8-department structure in `ARCHITECTURE.md` already maps almost 1:1 onto
seats. That is the single biggest asset this project has for M6 — the org chart
was written before the org.

| Desk | Owns (today's modules) | Consumes | Must deliver | Non-negotiables | Day-one docs — exist? |
|---|---|---|---|---|---|
| **Data & Infra** (Dept 1) | `dhan_guard`, `token_provider`, all 20 `ingestion/` clerks, `lake`, cron, VM | broker APIs, NSE/FRED archives, scrip master | every feed fresh by its stated cadence; honest outage codes; the lake never gaps | never guess a value (NULL-honest); never a second market-data door; one NSE job at a time | `ARCHITECTURE.md` Dept 1 ✅ · `CRON_SETUP.md` ✅ (stale count) · **Data Health card ❌** · **feed SLA table ❌** |
| **Research & Patterns** (Depts 8 + 5) | `analysis/*`, `discovery/*`, `validation/*` | the lake, `daily_context`, `outcomes` | hypotheses with pre-registered predicates; honest p-values; kill their own ideas | a pattern earns authority in Dept 5 or it has none; discovery data counts for nothing out-of-sample; append-only ledgers | `docs/hypotheses.md` ✅ · `docs/auto_discovery_spec.md` ✅ · `docs/stage_b_forward_scoring_spec.md` ✅ · **research intake/review process ❌** |
| **Risk** (Dept 3) | `portfolio_manager`, `plan_tracker`, `exposure_gate`, `portfolio_greeks`, `adaptive_sizing`, `firm_treasury` | proposals, book state, VIX, margin | every entry gated; every exit settled; bucket caps enforced; drawdown reported daily | only Risk may block (#63); advisory-only exits except the one sanctioned seam (#69); no gate is ever bypassed "just once" | `ARCHITECTURE.md` Dept 3 ✅ · `DECISIONS.md` ✅ · **written risk limits doc ❌** · **escalation ladder ❌** |
| **Execution & Ops** (Depts 2 + 7) | `master_scheduler`, `market_loop`, `options_proposer`, `live_bridge`, `api_server`, `ops_monitor` | approved proposals, live quotes | orders placed correctly; the box up; the book reconciled daily | one proposer (#63); reconcile before close, every day; a failed job pages, never sleeps | `CRON_SETUP.md` ✅ · **runbook ❌** · **on-call/escalation ❌** · **the whole execution layer ❌ (M2 gap)** |
| **Reporting** (Dept 6) | `notifier`, `eod_summary`, `ceo_brief`, `morning_brief`, `performance`, `validation/digest`, `ceo_language` | every other desk's output | the owner's questions answered before they are asked; every number with its n | one Discord door; never a number without its confidence; silence is never health | `docs/ceo_view_discord_design.md` ✅ · **the §9 gaps ❌** |

**% formula:**
```
per desk: docs_complete(25) + dashboards_live(25) + handover_runbook_written(25)
        + 30_days_run_by_someone_other_than_the_owner(25)
M6% = mean over the five desks
```
Today: docs are strong, everything else is zero. **M6 ≈ 20%** — and that 20%
is real, not flattering. The documentation discipline in this repo is genuinely
ahead of the trading.

**🚩 Gaps:**
1. **Decision #1 says "personal use only — no multi-user infrastructure."**
   The gateway is a single shared API key with no roles, no per-user audit, no
   per-desk permissions. M6 contradicts decision #1 and will need it formally
   superseded, not quietly ignored.
2. **No four-eyes principle anywhere.** Every gate is fail-open and
   single-actor. A firm needs at least one control where two people are
   required — typically capital allocation.
3. **The Mac is load-bearing.** `graph_edges` (causal mining), the darling
   pipeline, and evolution all require the owner's laptop to be awake. That is
   fine for one person and impossible for a firm. Ledger evidence: the Mac
   slept through 4 of 11 weekdays and bhavcopy silently missed them.

**Smallest next action today:** write **one** desk's runbook — Data & Infra,
because it is the most complete department and the exercise will reveal the
template. One page: what runs, what breaks, how you'd know, what you'd do.

---

# WEEKLY PROGRESS TEMPLATE

The system fills this and sends it. One Discord slot (the Saturday budget has
room — `digest` and `performance` are the only two scheduled Saturday cards).
Every number is a query this document has already specified.

```
📈 PROP PROGRESS — week ending <IST date>

M1 Trusted engine       ████░░░░░░  26%   (G1 19/100 trades · G2 ✗ · G3 1/3 archetypes
                                           · G4 ok · G5 2/10 questions · G6 0/30 days)
M2 Real money, small    ░░░░░░░░░░   0%   blocked: M1 <100%, execution layer not built
M3 Consistent profits   ░░░░░░░░░░   0%   blocked: M2
M4 Multi-bucket         ███░░░░░░░  44%   B1 80 · B2 20 · B3 80 · B4 20 · B5 20
   └ addendum A/B/C     ░░░░░░░░░░   0%   scaling 0 · allocation 0 · multiuser 0
                                          🔒 code locked until M1 = 100%
M5 Scale to ₹10cr       ░░░░░░░░░░   0%   ₹2,00,000 own capital
M6 Firm-ready           ██░░░░░░░░  20%   docs 4/5 desks · runbooks 0/5

WHAT MOVED
• +3 resolved trades (19 → 22). G1 19% → 22%.
• daily_context 25 → 30 frames (miner unlocks at 60).
• <one line per real change, from git log + the ledgers>

WHAT'S BLOCKED
• G3 diversity: still 1 strategy, 1 direction. Nothing in flight changes this.
• Stage B: 17 declarations, 0 graded. Calendar-bound, no action available.
• Bug ledger: 74 items untriaged (day 15).

THE NUMBERS, WITH THEIR CONFIDENCE
• Win rate 63% (12W/7L, n=19) · Wilson 95% lower bound 41% · breakeven null 40%
  → NOT significant. One more loss flips it.
• Max drawdown 0.0R — the sample has not been tested yet, not a strength.
• Firm MTM ₹236,056 · +18.0% absolute · 19 trades · ONE strategy, ONE direction.

TRUST LINE
"This week the system did / did not do anything I cannot explain."
  → <the honest sentence, and if 'did', the thing, named>
```

**Rules for the trust line — the most important line on the card:**
- It is **auto-set to "did"** whenever any of these is true: a silent-failure
  detector fired, an artifact went stale past its cadence, a gate blocked
  without a logged reason, a number changed by more than 20% with no
  attributable cause, or `live_quote` (or any fail-open path) swallowed an
  error it could not name.
- The system may **never** write "did not" by default. Clean has to be earned
  by every detector reporting green, not by no detector reporting red.

That inversion is the whole point. Trust does not come from good numbers — it
comes from the system being unable to hide a bad one.

---

## The five gaps, collected

For the record, the things this vision needs that the current architecture
**fundamentally cannot** provide today:

1. **No order path.** Paper-only by house rule; M2 is a full build, not a flag.
2. **Single-row money schema.** `account_state` and `treasury_state` are one
   row each with no `portfolio_id`. Multi-bucket needs a money-layer migration.
3. **No corporate-action adjustment.** A long-term equity bucket will silently
   mis-price on the first split or bonus.
4. **No capacity model.** Every sizing decision assumes zero market impact —
   true at ₹2L, false by ₹50L.
5. **Single-user by decision (#1), single-machine by dependency (the Mac).**
   Multi-seat contradicts a locked decision and a live architectural
   dependency. Both are fixable; neither is fixable quietly.

Three more, added by the M4 addendum:

6. **One trade = one `outcomes` row with one `r_multiple`.** Staged position
   management (M4A) has no single R. Needs a parent-trade / tranche split in the
   memory layer, and `portfolio_manager`'s whole-position margin lock has the
   same problem.
7. **`firm_treasury` decides the allocation itself** (decision #80's mechanical
   regime router). M4B says the *user* decides and the system only enforces.
   These two cannot both be true — it needs an owner ruling, not a merge.
8. **Zero tenancy.** One `brain_map.db`, one `journal.jsonl`, one `config.json`,
   no `user_id` in any table, one shared API key with no roles and no audit
   trail. M4C's isolation cannot be added by convention.

None of these blocks M1. All of them block M2 onward. Knowing that now is worth
more than any module built this week.
