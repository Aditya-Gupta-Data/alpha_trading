# GEX (Gamma Exposure) and Greek P&L — architectural blueprint (2026-10-20, decision #152)

**Status:** seam 1 BUILT (`src/analysis/gamma_profile.py` + `scripts/backfill_gex_profiles.py`, read-only,
no trading path). Everything else below is spec. The Architect approved D1 (one new pure module), D2 (naive
sign) and D5 (shadow-first) on 2026-10-20; D3 uses the recommended defaults; D4, D6 and D7 are open.

## 0. Ground truth (checked 2026-10-20)

- **`dynamic_pricer` fetches no option chains.** It is the darling desk's level engine (bhavcopy daily bars).
  The chain door is `dhan_client.get_option_chain`.
- **What a chain carries:** every Dhan `oc` strike has, per side, `oi`, `previous_oi`, `volume`,
  `implied_volatility`, `greeks{delta, gamma, theta, vega}` and top bid/ask. `portfolio_greeks.leg_greeks`
  already reads it.
- **History:** `data/lake/chains/<slug>/` on the VM, one end-of-day capture (~15:40), nearest 3 expiries
  (NIFTY 4):
  - NIFTY + BANKNIFTY: 63 sessions (07-13 → 10-19)
  - FINNIFTY + MIDCPNIFTY + TCS: 45 sessions (08-07 → )
  - other names: sparse
  - no intraday chain history at all
- **Live:** the proposer fetches a chain at every proposal. The live bridge fetches one per held
  (ticker, expiry) every 60 s (#145 Q). A GEX read at decision time costs zero extra Dhan calls.

## 1. The GEX module (seam 1 — BUILT)

**Per strike K, per side:**

    GEX_call(K) = + gamma_c(K) * OI_c(K) * S^2 * 0.01
    GEX_put(K)  = - gamma_p(K) * OI_p(K) * S^2 * 0.01

- The unit is rupees of dealer delta per 1% move.
- OI is taken as Dhan reports it, in quantity. `oi_lot_multiple` checks this per chain: a value of 1.0 means
  every OI is a whole multiple of the lot.
- `net = call + put`; `abs = |call| + |put|`.
- The OI-change variant uses `oi − previous_oi` (fresh positioning).

**Profile, over the strikes within ±band of spot** (D3: 5% index, 10% stock):
- the regime is the sign of net GEX;
- the call wall is the strike with the largest positive call GEX;
- the put wall is the strike with the largest negative put GEX;
- the flip is where the strike-ordered cumulative net GEX crosses zero, interpolated. This is an
  approximation: gamma is read at today's spot only;
- also net share (net/abs), concentration (the top strike's share of abs), coverage and spot-vs-flip %;
- it abstains, with a named reason, when fewer than 80% of in-band strikes carry gamma and OI on both sides.

**Sign (D2, NAIVE):** dealers long calls, short puts. This is unverified for NSE, where much option selling is
prop/FII. Calibration from NSE's daily participant-wise OI (client/DII/FII/pro, long/short calls/puts) is a
later, separate change.

**Day record (backfill):**
- one row per underlying per day: a profile per expiry, plus a combined profile over expiries ≤ 35 days out;
- written to `data/lake/gex/<slug>/` and replaced on re-run;
- reads `chains/` only. It runs on the analytics host.

## 2. Override rules (draft; D5: SHADOW-FIRST — not built)

**G1 — the Gamma Wall confirms an iron condor/butterfly.** It confirms only when all of these hold:
- the regime is positive;
- the put wall ≤ the short put and the short call ≤ the call wall (the shorts sit inside the walls);
- concentration ≥ C;
- spot is ≥ F% above the flip.

If promoted, it is a filter: a neutral structure without confirmation is refused. G1 can only remove
condors.

**G2 — the Negative Gamma Zone is a glassbreaking trigger.** It fires when spot is below the flip (net < 0)
and |session move| ≥ M%. The direction is the sign of the move against the previous close.
- (a) It refuses short-premium structures. It plugs into `trade_planner.glassbreaking_block` (#151) as a
  second trigger beside the gap-up read.
- (b) It may bypass the SMA view and propose a directional debit spread in the move's direction. This
  CREATES trades.

**Precedence:** #151 earnings gate > G2 > G1 > SMA view. Exposure (#68), sizing (#106/#145 R) and `vol_bridge`
are unchanged.

**Authority:**
- Both rules start as SHADOW: each proposal is stamped with what G1/G2 would have done (the `event_gates`
  pattern), Court rows use mode `GEX_SHADOW`, and the ghost tracker marks the refused or would-be trades.
- Promotion follows Department 5: n ≥ 20 per rule cell and a one-sided Wilson lower bound above the breakeven
  null (the #109/#110 pattern).
- The learner may take G2(b) trades as raw setups (#140).

## 3. Greek P&L post-mortem (spec — not built)

**Inputs:**
- entry Greeks, stamped at entry from the chain already in hand (`portfolio_greeks.position_greeks`, zero
  calls);
- the end-of-day chain lake for every held day;
- the exit mark.

**Per day t→t+1, per leg** (side sign s, quantity q):

    delta_pnl = s*q*delta_t*dS
    gamma_pnl = s*q*0.5*gamma_t*dS^2
    theta_pnl = s*q*theta_t*d_days
    vega_pnl  = s*q*vega_t*dIV
    residual  = s*q*dP − (sum of the four)

- The days are summed; frictions stay separate (already booked).
- The output is ₹ and % per component, plus coverage (days with a chain / days held) and the residual share.
- It abstains below the coverage floor (D7, proposal 80%).

**Units to verify first:** whether Dhan's theta is per calendar day or per trading day, and that vega is per
1 IV point.

**Storage:**
- The outcomes ledger is append-only and immutable, so attribution goes to its own additive table,
  `greek_attribution` (journal_ref PK, components, coverage, method, as_of), plus a line in NEW post-mortems.
  Old outcome rows are never rewritten.
- Backfill covers every closed spread since 07-13 whose underlying+expiry is in the lake. The Mar-2027 NIFTY
  spread `4e40647d` is beyond the 3-expiry archive and abstains.

**First use:** it settles the 10-16 question — were the Q2 IT losses direction (delta) or IV (vega)?

## 4. Decisions

| # | Decision | Ruling / recommendation |
|---|---|---|
| D1 | NO NEW ENGINES (07-27) vs one new pure module | **Ruled 10-20:** one new pure module, `src/analysis/gamma_profile.py` |
| D2 | Naive sign vs participant-OI calibration | **Ruled 10-20:** naive first, labelled; calibration later |
| D3 | Band and expiries | Default in code: ±5% index / ±10% stock; combined = expiries ≤ 35 days |
| D4 | Thresholds C, F, M for G1/G2 | Open — set from the backfill's distribution, not guessed |
| D5 | Authority of G1/G2 | **Ruled 10-20:** shadow-first |
| D6 | May G2 CREATE a directional trade against the SMA? | Open — recommend refuse-only live, create-trade shadow-only |
| D7 | Greek attribution granularity and floor | Open — recommend daily EOD, abstain below 80% coverage |

## 5. Next seams (in order)

1. **Done:** `gamma_profile.strike_gex` / `profile` / `merge_rows` + the backfill.
2. Stamp a `gex` block on every proposal from the chain in hand (annotation only), and capture it forward
   from `chain_archiver`'s end-of-day row.
3. Stamp `greeks_at_entry` in `options_proposer.to_journal_entry` (`portfolio_greeks.position_greeks`), and
   build the `greek_attribution` table plus its backfill.
4. G1/G2 as shadow Court rows once D4 and D6 are ruled.
