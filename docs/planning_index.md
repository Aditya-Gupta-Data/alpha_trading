# Planning Pipeline Index — everything queued for after the observation week

> **This is the single map of what's been designed and what's actually
> confirmed to build first.** Six planning documents accumulated in one
> night; this file exists so nobody (including a future session) has to
> hunt through all of them to answer "what's actually next." It is
> itself a planning doc — no code, no migrations, observation-week
> boundary held.
>
> **Not covered here:** `docs/observation_week_ledger.md` is a different
> kind of document — the live incident/hotfix record for the current
> week, not a design spec. Keep it separate; it feeds the triage review,
> this file feeds the build queue that comes after.

## 1. What's confirmed to build FIRST — no gate, already approved

These sit **above** everything else in this index. Agreed explicitly
(see `project_alpha_trading_status` memory), because they hardened real
failures the live system hit *today*, not speculative future features:

1. **Self-healing token refresh** — the live session loads
   `DHAN_ACCESS_TOKEN` once at startup and never re-reads `.env`; today's
   mid-session blackout happened because an external renewal (decision
   #48's single-token-per-account rule) invalidated the in-memory token
   with no recovery path but a manual restart. Fix: periodic re-read.
2. **`dhan_client` response-shape audit** — the identical double-nesting
   bug hit `get_expiry_list` and `get_option_chain` within minutes of
   each other today (commits `5fe5647`, `e0dcfba`). Audit the remaining
   parsers (`get_quote`, `get_ohlc_since`, `get_live_price`) for the same
   latent issue; extend `tests/test_dhan_client.py`.

Everything below this line is the *next* layer — real feature work, but
strictly after #1–#2, and only once the observation-week triage clears
new building.

## 2. The six planning documents, at a glance

| Doc | Covers | Status / gate | The one thing to remember |
|---|---|---|---|
| `thematic_playbooks_spec.md` | Multi-month macro cycle tracking: dual-trigger AND gate (§2), staggered execution (§3), generalization (§4), autonomous hypothesis simulation (§5), continuous heat-map/weekend-sim/self-seeding loop (§6), literature-derived playbooks (§7), Mac/VM infra mapping (§8) | Open — biggest open question is transcript sourcing (§ "Open questions") | Foundational: later docs (self-evolving brain map, roadmap) build on this one's Mac/VM split and cyclical_models table |
| `self_evolving_brain_map.md` | DTW/K-Shape pattern-coordinate matching (§1), Bayesian per-cluster confidence — "skeptic v3" (§2), MFE/MAE Apex Target optimization (§3) | Open — §4.2 calibration is the load-bearing prerequisite for everything else in the doc | Explicitly designed as the NEXT GENERATION of the existing `skeptic_agent.py`, not a second confidence engine — never let two confidence numbers disagree on one alert |
| `commercial_tip_verifier.md` | Public tip-verification product: NLP ingestion, alt-data + technical proof framework, confidence/pump-risk/proof-ledger output | **Blocked** — §1 requires a SEBI Research Analyst / Investment Adviser legal review before ANY code | The only externally-gated item in this whole index — cannot be sequenced into a build queue until a lawyer clears it |
| `scalable_implementation_roadmap.md` | Decoupled Mac-now/cloud-later scaling for: K-Shape brain map (§1), a raised 0.70 skeptic gate via FBST (§2), the tip-verifier queue (§3, still gated by the doc above), the deterministic safety envelope (§4, mostly already shipped), the wealth-locking flywheel (§5) | Open — §2.2's FBST claim and §5.2's real-vs-paper sweep are both explicitly unresolved by design | Two "do not assume" flags: FBST hitting 0.70 is a hypothesis not a result; the Gold-ETF sweep's real-money-vs-paper semantics is a decision only the user can make |
| `discord_ingestion_pipeline.md` | JSON thesis ingestion from Discord (§1–§2), corrected execution handoff preserving decision #11 (§3), knowledge-graph enrichment of the human's own reasoning (§4) | Open — §1's bot/module split needs verifying against live code | §3 deliberately does NOT implement what was originally asked (auto-fire with no approval) — routes through the existing `pending_approval` gate instead; flagged explicitly, not silently swapped |
| `PHASE_8_NEWS_INGESTION_SPEC.md` | *(pre-dates tonight's series, 2026-07-08)* Semantic news ingestion | Separate, older thread — not part of tonight's cross-referencing chain | Listed here only so it isn't lost; not otherwise connected to docs 1–5 above |

## 3. Every open item that needs an explicit answer, in one place

Pulled from each doc's own "reality checks" / "open questions" section —
this is the complete list, so triage doesn't have to re-derive it:

**Needs the user's judgment call specifically (not a technical unknown):**
- `scalable_implementation_roadmap.md` §5.2 — does the Gold-ETF sweep
  activate only once real capital is in play, or stay fully paper/
  simulated indefinitely? Document defaults to "paper" as the safer
  assumption but treats this as explicitly not resolved.
- `discord_ingestion_pipeline.md` §3 — whether to revisit the "locked
  out of manual intervention" mechanic at all is a standalone
  conversation, deliberately not reopened by any later doc.
- `commercial_tip_verifier.md` §1 — output framing (scored "confidence"
  vs. neutral unscored evidence) materially changes regulatory exposure;
  worth deciding with counsel, not engineering alone.

**Needs empirical testing, not assumption (the "prove it, don't assert
it" list):**
- `scalable_implementation_roadmap.md` §2.2 — FBST epistemic e-values
  reaching 0.70 balanced accuracy is unproven; decision #50 (same night)
  showed a different orthogonal feature addition produced *no*
  improvement. Needs its own backfill/retrain experiment, own decision
  entry, win or lose.
- `self_evolving_brain_map.md` §4.2 — any confidence percentage must be
  calibration-checked before it's ever shown on a live alert, or it must
  default to abstaining. This is the single prerequisite the rest of
  that document's build order (§5) is sequenced around.
- `thematic_playbooks_spec.md` §5.5/§6.5/§7.5 — overfitting in the
  cycle-generalization and self-seeding loops needs an out-of-sample
  validation discipline before any sensor is trusted, mirroring decision
  #50/#36's existing standards.

**Needs verification against current code before building on top of it:**
- `scalable_implementation_roadmap.md` §5.4 — `GOLDBEES` confirmed
  ABSENT from `dhan_client.SECURITY_ID_MAP` (checked directly tonight).
- `discord_ingestion_pipeline.md` §1 — which Discord bot module actually
  owns `/pending` today; `MODULES.md`'s "read-only" note on
  `discord_bot.py` may predate the approval-buttons commit.
- `thematic_playbooks_spec.md` §4 — whether `CNXIT` (or any sector
  index) is in `SECURITY_ID_MAP`, needed for Trigger B.

## 4. A workable build order, once the observation week clears

Not a commitment — a reasoned sequencing suggestion, respecting the
dependencies above:

1. **§1's two priorities** (token refresh, dhan_client audit) — always
   first, unconditionally.
2. **`self_evolving_brain_map.md` §4.2** (calibration framework) — the
   doc's own build-order already names this first, and nothing else in
   this whole index that outputs a confidence number is trustworthy
   without it.
3. **`self_evolving_brain_map.md` §3** (MFE/MAE Apex Target) — cheapest
   next win: buildable on data `plan_tracker` already collects, useful
   independent of the clustering work.
4. **`thematic_playbooks_spec.md` §2 Trigger B** (relative-strength
   technical confirmation) — pure extension of existing TA, low risk,
   and a prerequisite for the rest of the playbooks work.
5. Everything downstream of those four — DTW/K-Shape clustering, Trigger
   A (needs the transcript-sourcing question resolved first), the
   continuous heat-map/self-seeding loop, the 0.70 FBST experiment, the
   Discord ingestion pipeline (once the bot-module question is checked)
   — roughly in the order each source document already lays out
   internally.
6. **Permanently parked until externally cleared:** the commercial tip
   verifier (`commercial_tip_verifier.md` §1's legal review) — not
   scheduled into the sequence above at all until that happens.

## 5. Document lineage (who references whom)

```
thematic_playbooks_spec.md  (foundational: Mac/VM split, cyclical_models)
        │
        ├──► self_evolving_brain_map.md   (extends the confidence-scoring
        │                                   idea; "skeptic v3")
        │
        ├──► scalable_implementation_roadmap.md
        │        ├─ §1  → self_evolving_brain_map.md §1.1 (K-Shape = algo choice)
        │        ├─ §2  → train_skeptic.py + decision #44 (0.60 → 0.70 gate)
        │        ├─ §3  → commercial_tip_verifier.md §1 (still gated)
        │        └─ §5  → portfolio_manager.py (singleton account fork)
        │
        └──► discord_ingestion_pipeline.md  (routes through the SAME
                                              pending_approval path every
                                              other doc's proposals use)

commercial_tip_verifier.md  — stands apart, gated on external legal review,
                               referenced BY the roadmap (§3) but not
                               dependent on the other four
```
