# Adversarial audit — Chunk 3: Analytics & Sizing (2026-10-09)

Method: three finder lenses (statistical correctness, sizing arithmetic & capital invariants, wiring/staleness) read the files below whole; every finding was then refuted or confirmed by the lead agent against the code and the tests, defaulting to "refuted" when uncertain.

Pre-audit fix already shipped under this chunk: **#135, the phantom ratchet** — the profit ratchet moved on the modeled mid-price capture (70% model vs 29% crossed), so a lock the market never offered forced exits; the Court and the Wilson bounds count win/loss only and could not see it. `live_bridge.evaluate_position` now moves peak/lock/hit on crossed capture only.

## Scope
`src/adaptive_sizing.py`, `src/position_sizing.py`, `src/performance.py`, `src/tuner.py`, `src/decay_engine.py`, `src/opportunity_cost.py`, `src/vol_bridge.py`, `src/vol_rank.py`, `src/firm_treasury.py` (routing math), `src/evolution.py`, `src/edge_miner.py`, `src/human_pulse.py`, `src/review.py`, `src/analysis/{strategy_scorer,strategy_scoreboard,strategy_registry,weekly_recalibration,regime_filters,underlying_router}.py`, the Court/Wilson parts of `src/plan_tracker.py` and `src/strategy_router.py`, the sizing parts of `src/options_proposer.py`, `src/portfolio_manager.py`, `src/equity_desk.py`, `src/execution/live_pricer.py`.

**Status (2026-10-09 ~03:30 IST):** Batches A, B, C FIXED and pushed (`d7e9ba1`, `4c7e7a4`, `aedbc5e`; decision #136). NOT deployed. Rulings listed under 'Fix plan' remain open.

## Findings

### Lens Z — sizing arithmetic & capital invariants (7 raised; lead verified Z1–Z4 on the code, Z5–Z7 on inspection)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| Z1 | **major** | CONFIRMED | `vol_bridge._node_polarity` (`:84-93`) is a substring match: "win" ⊂ "wing", "gain" ⊂ "against", "strong" ⊂ "stronger_downside". Bearish graph nodes count positive; the net signal sets `risk_pct` for every account. | `_node_polarity` on `wider_wing_spacing`, `time_decay_against_our_long`, `following_the_signal`, `stronger_downside_momentum` → `[1,1,1,1]`. On the VM mirror graph the signal is +0.45 → Neutral → 2.0%; with whole-token matching −8.0 → Expansion → 1.4%. Every trade since is sized ~43% larger than the bridge's own rule. |
| Z2 | **major** | CONFIRMED | `options_proposer.py:661-672`: the primary sizes risk on `pm.equity` but its margin wall (`available_cash=book["cash"]`) is the legacy `portfolio.json` cash, never reduced by margin locks. | VM 09-25: eight proposals sized ~5 lots needing ₹82-86k while PAPER_10L had ₹13,460 liquid; all refused `margin_exhaustion` at the door instead of sized down. Reverse case: Mac cash ₹28,980 caps a 3-lot trade at 1. |
| Z3 | medium | CONFIRMED | #106 says `floor_applied` is journaled; `journal.new_entry` drops `proposal["sizing"]` and the shadow verdict keeps only status/lots/margin/reason. | 0 of 78 spread rows in the mirror journal carry `sizing`. RELIANCE 62d04919 on 2L: max loss ₹13,400 = 6.7% vs 1.4% budget, no trace. |
| Z4 | medium | CONFIRMED | Margin wall uses the UNSTRESSED per-lot SPAN (`position_sizing.py:60-62`), the door locks the VIX-stressed amount (×1.15 at VIX≥16). When margin binds the trade is refused, not sized down. | `fractional_lots(2L, 1956, 2.0, margin 17673, cash 40000)` → 2 lots; `required_margin_for(2 lots, VIX 17)` → ₹40,648 > ₹40,000 → refused, though 1 lot fits. |
| Z5 | low | plausible | Adaptive boost ×1.5 on the desk's 5% budget → 7.5% risk when the stop is wide; only the 25% notional cap binds. | `size_entry(1000, 760, 4L, risk_pct=7.5)` → 6.0% of desk capital at risk. |
| Z6 | low | plausible | Adaptive penalty applies to the primary only (`adjust_option_lots` has one caller); shadows size unpenalised, so the #120 comparison differs by more than capital. | bull_call penalty ×0.429 active on the mirror ledger. |
| Z7 | low | plausible | `_primary_equity` falls back silently to the legacy book's total value when brain_map is unreadable; under pytest that is the only path, so the live seam is untested. | `options_proposer.py:722-737`. |

Refuted by the lens (not re-checked): per-lot/per-share units consistent; shadows size on their own equity; 0-lot paths are named; vol×adaptive×treasury cannot compound upward for options; LIVE re-size only shrinks; 5%/25% desk caps are the config, not a defect.

### Lens S — statistical correctness (10 raised; lead reproduced S1, S2, S4, S5, S8 by command; S3, S6, S7, S9, S10 on inspection)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| S1 | major | CONFIRMED mechanics; **Dept-5 ruling needed** | `strategy_scorer.py:118-122` grades a forward call over `episode_phase_returns(decl_date)`, so a P3 declaration made at analog offset ~80 is graded on decl+46..120 — a window the in-sample registry never scored. | `_phase_for(80)` → P3, `_phase_hi('shock','P3')` → 120. ~12 A2/P3 declarations pending since 09-08 are measured on the wrong window. Changing the scorer changes Stage B's question → decision, not a hotfix; ledgers stay immutable. |
| S2 | major | CONFIRMED; **Dept-5 ruling needed** | `strategy_scoreboard.py:38-53`: consecutive nightly declarations of one cell overlap >95% yet count as independent Wilson n; `MIN_FWD_CALLS=7`. | `_status(6,7)`, `_status(7,7)` → FORWARD_CONFIRMED. A2/P3 declared 10 nights 09-08..10-06 = one market draw as n=7+. |
| S3 | major | plausible; **Dept-5 ruling needed** | The 0.5 null ignores persistent sector drift; `long_metal_reflation` P3 unconditional hit rate ≈0.68 (n=306), so FORWARD_CONFIRMED can be reached with zero conditional edge. Placebos share the biased null. | Lens snippet over 2019-11..2025-12 anchors. Fix = null from the recipe's own unconditional rate. |
| S4 | major | CONFIRMED | `opportunity_cost.py:63-103`: blocked rows dedupe per (gate, day, ticker, direction), not per host; the verdict is raw wins>losses. | Mirror: raw 86W/64L → "COSTING"; per host 19W/13L (32 independent). Host e38312e1 alone = 13 identical wins. |
| S5 | major | CONFIRMED | `adaptive_sizing.py:118-139`: every journal row is a Wilson trial; identical same-session positions are never collapsed. Live (`adaptive_sizing_enabled: true`). | ICICIBANK bear_put ×3 on 09-23 (749f6b80, bf9068f3, 5767209e), all exited 09-24 — one outcome counted 3×. 12/12 `darling_ripe` exits from 07-20 entries → ×1.5 boost. |
| S6 | major | plausible | `adaptive_sizing.py:154-164`: break-even p* is estimated from the same sample and treated as exact; a veto compares a CI to a noisy point. | `block_vwap_pullback` n=61 vetoed by 0.009 margin while its expectancy upper bound is +0.021R. Telemetry-only today; same math sizes `darling_buy` (×0.836). |
| S7 | major | plausible | `edge_miner.py:155-185` ships only triples absent from the snapshot, so a re-observed edge is never reinforced/revived on the VM; decay = time since first seen. | Temp-DB test: `mine_new_triples` with the same triple → `new == []`. |
| S8 | minor | CONFIRMED | `adaptive_sizing.py:78`: nominal p*=0.50 before 3 wins AND 3 losses; condors (small wins, big losses) get a boost card on negative expectancy. | 10×+0.25R + 2×−1.5R (mean −0.04R) → `boost ×1.5`. Boosts never change option lots; the missing penalty does. |
| S9 | minor | plausible | `strategy_registry.py:226-228`: `benchmark_tilt` (+weight) is sign-identical to `long_sector`, so `half_tilt_pharma` = `long_pharma` and double-counts a forward call / takes a top-3 slot. | `macro_strategies.json` shows identical n/hit/LB for both in A1/P1 and A2/P1-P3. |
| S10 | minor | plausible | Equity autopsy R is gross (`equity_shadow_proposer.py:365-366`); options R is net. Scratch exits (+0.04R) count as wins. | LTF.NS +0.04R, INFY +0.35R counted as `darling_ripe` wins. |

Refuted by the lens: voided/rejected trades already excluded; Wilson math one source and correct; performance.py Sortino/drawdown/Sharpe fine; decay_engine composes correctly; scorer grades only matured windows; registry binomial at n=5 is conservative; tuner.py is a manual offline tool; evolution.py human-gated.

### Lens W — wiring, staleness, silent no-ops (7 raised; lead verified W1, W2 by grep/command; W3 = Z1; W6 = Z6)

| ID | Sev | Verdict | Defect | Evidence |
|----|-----|---------|--------|----------|
| W1 | **major** | CONFIRMED | `regime_filters.advise()` is called only in `market_loop.fetch_market_state`; production passes `fetch_fn=live_bridge.fetch_live_market_state`, which never sets `advisory`. The smart-money/sector bullish veto and the War-Playbook condor block (`options_proposer.py:502-509`) have been dead since 07-17. Even the market_loop call passes no `prev_vix`/`as_of`, so the VIX-spike leg is dead there too. | `grep -rn regime_filters src/` → one call site, `market_loop.py:128`. MODULES/ARCHITECTURE/staleness_guard call it LIVE. Wiring it changes entries → deploy is the owner's call. |
| W2 | **major** | CONFIRMED (8 sites) | `fire_broadcast({"text"/"embeds": …})` at `performance.py:233`, `portfolio_greeks.py:444`, `validation/digest.py:172`, `validation/monitor.py:189`, `ingestion/deals_tracker.py:563`, `ingestion/scrip_master.py:305`, `knowledge_graph/entity_affinity.py:684`, `discovery/run_miners.py:96`. The notifier reads only `event`/`description`: the budget gate spools an empty row; unbudgeted it renders "📌 Event — ?". The Sat digest (opportunity cost), the performance card, deals alias review (its de-dup ledger still marks them announced) and quarantine cards all go out blank. | `_build_embed({"text":"x"})` → no description. Breaks the review-flags-to-Discord rule. |
| W3 | medium | = Z1 (+ CONTRADICTS edges not inverted, threshold unnormalised) | 27/165 active edges are CONTRADICTS and count with their raw sign. | Inverting them flips the mirror signal again (+0.87). Semantic choice → ruling. |
| W4 | medium | CONFIRMED on inspection | `underlying_router.macro_score` reads `events.long_term_macro_score`, written only by the manual `brain_map ingest`; stock tickers stored `RELIANCE`, queried `RELIANCE.NS`; `build_proposal(macro_score=)` never passed. Indices always rank 0.00, so stock options are scanned first every cycle. | Mirror: latest news event 2026-07-10, 0 scored. |
| W5 | medium | plausible, latent | Mac weekly_recalibration pins from the Mac's frozen ledger (last written 07-20): 17 phantom "open" darlings vs 9 real on the VM; `darling_pins.json` (09-15) pins three July phantoms. Last ran 09-15; Mini PC cron not installed. | Orphan risk if the re-screen drops a name the VM holds; all 9 VM names still in the queue today. |
| W6 | medium | = Z6 | Adaptive penalty on PAPER_10L only. | |
| W7 | low | plausible | Adaptive ledger/cards say "neutral → boost (×1.5)" for options although `adjust_option_lots` never boosts; a ticker-only veto is recorded under the setup key and flip-flops. | `bear_put_spread` n=45 shows boost on the mirror. |

Refuted by the lens: decay_engine IS wired (sleep_phase Task K); edge_miner is applying edges (6/2/26 on 10-04..06; Ollama HTTP 500 on 10-07/08 — watch); adaptive sizing is on; voided 7f4a4897 excluded; vol_bridge risk_pct reaches shadows (F12); scorer/scoreboard/registry reach no sizing path.

## Fix plan (lead, 2026-10-09)

Needs no ruling — code defects, fixed in batches with the usage guardrail:
- **Batch A (reporting + sizing inputs):** W2 one-door normalisation in `notifier` (accept `text`/`embeds` payloads, name the event); Z1 whole-token polarity in `vol_bridge`; S4 per-host dedupe + a named sample size in `opportunity_cost`.
- **Batch B (sizing door):** Z2 + Z4 — the primary's margin wall from `pm.available_cash` on the VIX-stressed per-lot margin, sized DOWN not refused; Z3 — journal the `sizing` dict (`floor_applied`) on the primary and the shadow verdicts; S5 — collapse identical same-session positions to one Wilson trial.
- **Batch C (wiring):** W1 — compose `regime_filters.advise(prev_vix, as_of)` in `live_bridge.fetch_live_market_state`; activating three dormant vetoes changes entries, so it ships behind the deploy decision with a dry-run over the open book.

Needs a ruling (recorded as open questions, nothing changed):
- Dept 5 / Architect: S1 (grade on the declared phase's remaining window vs the declaration anchor), S2 (overlapping nightly calls as one observation), S3 (recipe-own unconditional null), S9 (tilt recipes duplicate long_sector).
- Owner: S6/S8 (break-even p* nominal per family and its uncertainty), W3 CONTRADICTS inversion, W4 (macro legs: wire `news_processor` scores or drop the leg), W5 (Mac recalibration clock → VM), Z5 (desk boost cap), Z6/W6 (penalty for shadows), Z7, S7, S10, W7.
