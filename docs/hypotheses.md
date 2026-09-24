# Hypothesis Register

The owner's trading theses — claims about what has edge, kept in ONE place
so none is lost and each can be tested the same disciplined way (the
Phase-4 proving harness: a frozen definition, out-of-sample validation,
placebo control, real evidence required). A hypothesis here is NOT a
shipped rule; it is a claim waiting for the harness to accept or reject.
Nothing hand-wires a hypothesis into sizing/scoring before it earns it
(composition law #63; the twice-failed skeptic #44/#50).

| # | Thesis (one line) | Where it lives | Status |
|---|---|---|---|
| H1 | A leader going extended + high-vol PRECEDES the laggard's breakout (an early tell that leads the move). | `src/discovery/sequence_miner.py` (lagged-antecedent miner) | Miner built; awaiting enough daily_context history to test. |
| H2 | Smart-money distribution PRECEDES the sector's drawdown. | `src/discovery/sequence_miner.py` + entity-affinity layer (#61) | Miner + affinity built; advisory, unvalidated. |
| H3 | Confidence should be higher when a calendar cycle (expiry-week, quarter-end, results proximity) also aligns. | Decision #66, `src/cycles.py` (tuner-learned `cycle_points`) | SHIPPED as an earned tuner channel (0.0 until a cycle has samples). |
| H4 | Asymmetric position management beats one-and-done: ADD on a price-confirmed continuation, TRIM on an adverse move — so profits compound and losses stay capped. | This doc (below); guardrails already built: #68 gate, #71 Greeks, #72 metrics | REGISTERED 2026-07-15 — not built. Simulator experiment first. |

---

## H4 — Asymmetric position management (add winners / trim losers)

**Owner's idea (2026-07-15):** the same market view recurs on consecutive
days (observed ~4-5 times). Instead of the current hard block (one open
spread per underlying+direction, decision #68), on a day the view
CONTINUES add to the position, and on a day it turns adverse trim a bit —
so the winning direction is sized up and the losing one is capped. This is
the **anti-Martingale** shape the research audit endorsed
(`docs/gemini_research_gap_analysis.md` §5): scale into strength, cut
weakness — the opposite of averaging down into losers.

**Why the instinct is right:** adding to winners and cutting losers is
sound risk management and the correct polarity. The direction of the idea
is not in question.

**Why it is NOT hand-wired now — the three real hazards:**

1. **"Continuation" must mean PRICE confirmation, not signal repetition.**
   The #68 pileup (nine near-identical bear put spreads) happened precisely
   BECAUSE the daily binary trend read (SMA50 vs SMA200) stays "bearish"
   across sessions and re-fires the same view. That pileup was a *symptom
   of a weak repeating signal*, not a feature waiting to be managed. If
   "continuation" = "the signal fired again", H4 just re-creates the
   pileup, graded. It must mean the market CONFIRMED the view (the position
   is already in profit / a new extreme printed) — adding on evidence, not
   on repetition. This distinction is the whole hypothesis.

2. **The signal is ~coin-flip quality (skeptic #44/#50).** Pyramiding on a
   coin flip adds money to noise. H4 can only be trusted on top of a signal
   the harness has shown has out-of-sample edge — which is exactly why it
   belongs in the harness, not in a hand-coded rule.

3. **Defined-risk spreads don't pyramid like futures.** Each "add" is a NEW
   spread at NEW strikes (spot has moved), so a "position" becomes a STACK
   of correlated different-strike spreads — the very concentration the
   Greeks advisory (#71) now measures. And exits are atomic-basket (#27):
   there is no partial-leg trim, so "trim a bit" means *close the worst
   whole spread in the stack*. H4 is therefore a **stack-management**
   overlay, not intra-spread sizing.

**This is a sizing/conviction decision — deferred three times** (#63/#44/#50
to the Phase-4 harness). H4 does not get to override that by hand; it earns
its way in.

**How to test it honestly (the cheap first step — no live-state change):**

1. **Simulator experiment.** Add a management-policy variant to
   `src/simulator.py`: replay the same historical signals under (a) the
   current one-and-done gate vs (b) "pyramid on price-confirmed
   continuation + trim the worst spread on adverse". Same data, two
   policies.
2. **Measure with #72.** Compare the two policies on Sharpe / Sortino /
   max-drawdown over the resolved trades (`src/performance.py`). H4 only
   advances if the asymmetric policy genuinely improves risk-adjusted
   return out-of-sample — not just raw P&L (raw P&L rewards a gambler).
3. **Cap with #71.** Any "add" must respect the net-Vega/net-Delta budget
   (`src/portfolio_greeks.py`), so pyramiding can never silently
   over-concentrate the book.
4. Only a policy that survives (1)-(3) and the harness's placebo/embargo
   discipline ever touches real sizing.

**Net:** good instinct, right polarity; the risk is driving it off the
weak repeating signal. Define continuation as confirmation, prove it in the
simulator against #72, ceiling it with #71 — then, maybe, wire it. Today's
builds (#71 guardrail + #72 measurement) are exactly the substrate this
experiment needs.


## H-ToD — Time-of-day entry window (queued 2026-09-23, decision #109)

**Claim.** Options entries made strictly between 10:00 and 14:30 IST have a
higher per-trade Sharpe (mean R / stdev R) than all-day entries.
**Cohorts.** `in_window` = resolved approved spreads whose journal
`created_at` falls in the window; `all_day` = every resolved approved spread.
**Source.** The options journal's own out-of-sample record.
**Verdict rule.** `insufficient_n` below 20 per cohort; else `supports` if
Sharpe(in_window) > Sharpe(all_day), `contradicts` otherwise. Scored nightly
by `src/validation/hypotheses/tod_entry_window.py` inside the 21:00 court.
**Status.** CANDIDATE → TRIAL by the court's aging rule. NOT a live filter.

## H-RS — Mansfield relative strength filter (queued 2026-09-23, decision #109)

**Claim.** Equity-desk entries whose Mansfield RS vs NIFTY is positive at
entry (`MRS = (close/NIFTY ÷ SMA252(close/NIFTY) − 1) × 100 > 0`) have a
lower max drawdown of the cumulative-R path than unfiltered entries.
**Cohorts.** `rs_filtered` (MRS > 0 on the entry date) vs `unfiltered`
(every resolved equity-ledger trade); names without bars are `unscored`.
**Source.** The equity ledger's exits (autopsy `r_multiple`), darling bars by
scrip id, NIFTY closes from the bars cache.
**Verdict rule.** `insufficient_n` below 20 per cohort; else `supports` if
maxDD(rs_filtered) < maxDD(unfiltered). `src/validation/hypotheses/mansfield_rs.py`.
**Status.** CANDIDATE → TRIAL by the court's aging rule. NOT a live filter.


### Court readout — 2026-09-24 18:47 IST (manual `run_nightly` on the VM, decision #114)

| Hypothesis | Cohort | n | mean R | win rate | per-trade Sharpe | max DD (R) | Verdict |
|---|---|---|---|---|---|---|---|
| A ToD 10:00–14:30 | in_window | 14 | +0.844 | 71.4% | 0.709 | 2.51 | **insufficient_n** (14 < 20) |
| A ToD | all_day | 38 | +0.502 | 63.2% | 0.438 | 5.61 | (7 entries unstamped) |
| B Mansfield RS > 0 | rs_filtered | 54 | +0.186 | 44.4% | 0.167 | 6.48 | **supports** (both n ≥ 20) |
| B Mansfield RS | unfiltered | 89 | +0.066 | 44.9% | 0.061 | 11.63 | (13 unscored: no bars) |

Reading: A leans the right way (Sharpe 0.71 vs 0.44, DD 2.5R vs 5.6R) but is
14 trades — not evidence. B clears the n floor and halves the drawdown
(6.48R vs 11.63R) with ~3× the Sharpe — but this is the first scoring, on the
same ledger the hypothesis was written against (in-sample), the filtered
cohort is a SUBSET of the unfiltered one (a subset with fewer trades
mechanically has a smaller cumulative drawdown), and the win rate is
unchanged (44.4% vs 44.9%): the improvement is in the size of the losers,
not their frequency. Neither is promoted: the court's own rule is the
out-of-sample window + placebo FDR, not a first in-sample lean.
