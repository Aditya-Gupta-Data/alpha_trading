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
