# Adversarial audit — Chunk 6: Equity Desk & Knowledge Graph (2026-10-09)

Method: two finder lenses (the equity desk; the Brain Map and its learning loop), lead-verified against the code and the VM mirror snapshot (10-09 00:52, read-only).

**Status (2026-10-09 ~08:30 IST):** E1, E2, E4, E5, E6, E7, K4 FIXED and pushed; K2 has an offline repair tool (`scripts/repair_k2_shadow_trades.py`, dry-run default — the owner runs `--apply` on the VM); K1, E3, K3, K5 need rulings/ops. NOT deployed.

### Lens E — the equity desk (7 raised; lead reproduced E1 on the mirror ledger, E2/E4 by probe)

| ID | Sev | Verdict | Defect | Evidence / fix |
|----|-----|---------|--------|----------------|
| E1 | **major** | CONFIRMED, FIXED | `force_exit_strong_sell` sold any darling graded strong_sell; the grade "close below the hard stop" uses the PRICER's stop, which moves nightly with the anchored-VWAP zone — never the position's own stop. | DIXON bought 09-24 @13,200 (own stop 10,149), force-sold 09-25 @13,228; MOTHERSON (own stop 143, sold 162); DABUR sold 388 (own stop 374), re-bought next day. 3 of 5 capital exits since 09-24. Fix: a "hard stop" grade is skipped while price ≥ the position's own stop; valuation/pinned grades unchanged. |
| E2 | **major** | CONFIRMED, FIXED | `compute_trail` rebuilt the trail from daily bars + the current quote each cycle, so an intraday spike lifted the trail for one cycle only (#107 says one-way ratchet). | Probe: spike to 125 → trail 113.43; next cycle at 110 → trail back to 98.43, no `trail_hit`. Fix: `trail_for_position` remembers the intraday extreme per position (process memory, like the live arm's rung memory). |
| E3 | medium-high | plausible — **ops ruling** | Weekly No-Orphan pins are built on the Mac against a ledger frozen 07-20; VM positions opened since can never be pinned, so a dropped name's forced exit can never fire. | Move `weekly_recalibration` to the VM (owner pastes the cron) or ship the VM ledger back. |
| E4 | medium | CONFIRMED, FIXED | The entry-day bar's HIGH counted as the running high even when it printed before the fill. | Entry-day bar now contributes its close. |
| E5 | medium | CONFIRMED, FIXED | `open_positions` is keyed by ticker regardless of setup: a zero-capital block-VWAP telemetry row blocked a funded darling entry (BAJFINANCE 10-06; TCS ~3 weeks). | The darling gate now sees darling rows only (open and exited-today). |
| E6 | low | FIXED | The live cycle's inline `funding_revoked` lacked the `REVERSE_SWEEP_FROM` guard (Mac-era rows "stay as they are"). | Guarded. |
| E7 | low | FIXED | An unfilled desk EXIT ticket stayed PENDING while the modeled exit booked; the next sweep could fill a SELL of settled shares. | Cancelled before the modeled exit books (the rotation path's rule). |

Refuted by the lens: ruin latch not cleared by a budget raise (by design); funding goes through `pm.request_entry`; no double exits since #107; P&L net; .NS handling consistent; id-map carry-forward; fills inside the zone; trail arms from day 2; liquidity-tier consumers reload the file.

### Lens K — the Brain Map and its learning loop (5 raised; lead reproduced K1 counts and K2 rows)

| ID | Sev | Verdict | Defect | Evidence / fix |
|----|-----|---------|--------|----------------|
| K1 | **major** | CONFIRMED — **owner ruling** | Loss-derived edges are written λ=0 (never fade); `vol_bridge` counts every active outcome edge as a current regime vote. 62 permanent edges contribute −13.0 forever; the decaying ones +11.1 and fading. | Mirror: `decay_lambda 0.0 → 62, 0.05 → 103`. With Chunk 3's whole-token polarity the net is −1.86 → Expansion → **risk ×0.70 for every account** once deployed, and permanently once the decaying edges fade. Ruling: exempt λ=0 edges from `vol_bridge`, or let them decay. **Deploy note: Z1 + K1 together change sizing on day one.** |
| K2 | **major** | CONFIRMED (44 rows) — tool ready | `resolve_shadow` keeps the first resolution; Task I copied the five voided Issue-31 stop-loss results into `shadow_trades` before #121 repaired `outcomes`. 17 rows read loss where the host is a win; 8 resolved loss on f8356c9c, still open. They feed the opportunity-cost verdict and the H4 evidence. | `scripts/repair_k2_shadow_trades.py` (dry-run default; `--apply` re-resolves from the repaired outcomes, un-resolves open hosts, logs one `k2_shadow_repair` account event). Owner runs it on the VM after a dry-run read. |
| K3 | medium | CONFIRMED by log — **ledger correction** | #121 / Issue 39 say "no knowledge-graph edges came from these rows", citing only Task D. The Mac `edge_miner` mined the same window on 09-23 (3 edges) and 09-25 (20 edges), between the bad rows and the repair; two real wins went into the permanent loss bucket. `graph_edges` carries no outcome provenance, so they cannot be purged. | Correction appended to the ledger (below). |
| K4 | minor | FIXED | MCP `event_history` returned `sim:` synthetic outcomes unlabelled (546 of 596 for NIFTY 50) and no `as_of`; the Mac copy ages silently when Ollama fails. | Each outcome row carries `simulated`; the payload carries `as_of` and `simulated_outcomes` with the ~10× caveat. |
| K5 | low | documented | Tasks A/B print "deferred to a host with Ollama" but no host ever runs them; `semantic_nodes` last touched 07-07; nothing reads it. | Silent no-op; cosmetic. |

Refuted by the lens: outcomes match the journal today; loss-permanence holds; decay composes once; weights bounded; invalidation clears on rewrite; ALTERs guarded; busy timeouts via `brain_map.connect`; promotion excludes BLOCKED rows; quarantine card fixed (W2); affinity edges kept out of `vol_bridge`.
