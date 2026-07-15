# Gemini Deep Research → Gap Analysis (2026-07-15)

Source: `docs/Advanced Systematic NSE Options Trading.txt` (Gemini Deep Research,
commissioned 2026-07-15). This file is OUR read of it: what it validates, what
it changes, what to verify before acting. Priorities at the bottom.

## What the report validates (no action)

- **We are a solid "Stage 3" system** (its 5-stage maturity model). It calls a
  statistical harness with placebo hypotheses + lifecycle governance
  "institutional-grade" — that is exactly `src/validation/`. Signal generation
  is NOT our bottleneck anymore.
- **Deferring nightly miner wiring was right** — the report's biggest warning
  (blind mining on thin data → noise) is the exact reason run_miners is
  manual-only.
- **Defined-risk-only + advisory exits** matches its catastrophic-gap-risk
  guidance for small desks.
- **Tuesday-expiry migration** — already handled era-aware in `src/cycles.py`.

## What it changes — the real gaps (ranked)

### 1. Portfolio-level Greeks ledger (Stage 5 gap — biggest genuine hole)
We think per-trade; the exposure gate (#68) caps structure count, not risk.
Ten condors = one concentrated short-Vega bet. Needed eventually:
- Net Delta / Vega ledger across all open positions (needs a pricing model or
  chain-derived Greeks), with budgets as % of equity; throttle NEW entries when
  a budget is consumed (advisory-first, same pattern as #68).
- Drawdown-based sizing throttle (anti-martingale): cut size multiplier after a
  peak-to-trough breach.
- Gamma/pin-risk throttle: force-review shorts 24–48h before Tuesday expiry.

### 2. Honest paper fills (paper-to-live credibility)
If our paper fills assume mid/LTP, every win-rate the harness blesses is
inflated. Report: OTM legs can slip 0.10–0.50% per leg; retail reality is
crossing the spread. Action: AUDIT how simulator/plan_tracker price entries and
exits; if mid-based, charge a per-leg slippage penalty (cross-the-spread fill
model) so the stat harness validates NET edge. This multiplies the value of
everything already built.

### 3. Regulatory audit of margin/lot assumptions — VERIFIED 2026-07-15
Checked every claim against NSE/broker sources. Results:

- **Lot sizes — REAL BUG, NOW FIXED.** Current NSE sizes (Jan-2026 SEBI
  revision): NIFTY 50 **65** (was 75), NIFTY BANK **30** (was 35), live since
  the Jan-2026 contract series. The report's "Dec 30 2025" effective date was
  wrong (it's Jan 2026) but the numbers are right. `LOT_SIZES` in
  `options_proposer.py` still had the old 75/35, so the live engine was pricing
  max_loss / max_profit / SPAN margin / lot sizing on stale contract sizes.
  Fixed to 65/30 (single source; simulator uses it too — only scales
  absolute-rupee P&L, not the R-multiples/win-rates the harness scores). Two
  trade_planner test assertions updated.
- **2% expiry-day ELM — CONFIRMED real (eff. Nov 20 2024) but DOES NOT APPLY
  to us.** Entry needs ≥7 days to expiry (`MIN_DAYS_TO_EXPIRY=7`) and exit
  fires at 2 days out (`PRE_EXPIRY_EXIT_DAYS=2`), so a short is never held into
  expiry day (0 DTE). The peak-margin expiry-day surcharge never touches our
  positions — no margin-gate change needed. (This structural exit rule is the
  defense; if PRE_EXPIRY_EXIT_DAYS ever went to 0, revisit.)
- **Calendar-spread margin removal — N/A.** We trade no calendar spreads; every
  structure (bull call, bear put, iron condor/butterfly) is single-expiry.
- **BANKNIFTY weeklies dead (Nov 2024) — no code assumption.** `pick_expiry`
  takes whatever Dhan serves, sorted, first ≥7 days out — for BANKNIFTY that is
  now always a monthly. No breakage; just note BANKNIFTY trades are now
  longer-dated (different theta/gamma profile than the weekly era).
- **Tuesday expiry — already handled** era-aware in `src/cycles.py`.

### 4. Pre-live infrastructure (build only when live trading is scheduled)
- **Reconciliation daemon**: every few minutes compare internal book vs
  broker's actual positions; discrepancy → halt + alert. Meaningless in paper,
  mandatory before real orders.
- **Execution engine**: limit-chasing (start mid, walk toward touch, abort past
  max-slippage), protective-leg-FIRST sequencing on multi-leg entries (no
  naked-short moment, and margin benefit registers before the short leg).
- **TOTP-automated token renewal** (removes the manual daily-token seam) +
  VM-state backup/restore drill (VM is ephemeral; can we resume on a fresh
  instance in minutes?).

### 5. Hypothesis-first discipline (adopt the spirit, not the amputation)
Report says blind mining is our "most critical vulnerability" and to abandon
it. Overstated — our stratified nulls/placebo/BH already police exactly that
failure mode, and the owner's H1/H2/H3 are hypotheses. The cheap upgrade:
require a one-line ECONOMIC RATIONALE field before any pattern leaves TRIAL
(promotion gate in the registry). A pattern nobody can explain doesn't go
LIVE_ADVISORY, whatever its p-value. Also steal its hypothesis list as seeds:
VRP harvesting, event-window IV crush (RBI/budget), Tuesday-close
institutional-repositioning flows.

### 6. Position sizing for live (later, with #4)
Fixed-fractional 1–2% of equity per trade as the HARD CAP; fractional
(quarter/half) Kelly inside that cap only once enough live trades exist;
volatility-scaled (shrink when VIX spikes). Report's bar: 3–6 months live
forward-testing, ~1,000 trades before trusting Kelly math.

## Trust notes on the report itself

- Broker latency numbers (Dhan 25–40ms etc.) come from Reddit threads —
  directionally fine, don't hard-code.
- Its "GT-Score" citation is a random arXiv preprint; ignore.
- Internal inconsistency spotted (Tuesday vs Monday expiry across its own
  sources) → treat every regulatory number as a lead, not a fact; primary
  sources (NSE circulars) before any code change.

## Suggested order of work

1. ~~**Fill-honesty audit** (#2)~~ — DONE 2026-07-15, decision #70 (entries
   cross the bid-ask; pushed `412e57e`).
2. ~~**Regulatory verification pass** (#3)~~ — DONE 2026-07-15. Lot sizes fixed
   (75/35→65/30); 2% ELM / calendar-spread / BANKNIFTY-weekly all verified N/A.
   No margin-gate change was needed after all.
3. **Portfolio Greeks advisory ledger** (#1) — the next big build phase (next
   week). Dhan's chain carries per-strike Greeks, so no pricing engine needed.
4. Promotion-rationale gate (#5) — small registry change; candidate for the
   Friday slot before the Saturday deploy.
5. Pre-live infra (#4) + sizing (#6) — when a live date exists.
