# EVENT_ENGINE_PLAN.md — the policy-event engine: the ORIGINAL PLAN (historical)

> **STATUS (restored 2026-10-21): SUPERSEDED BY BUILD.** This was the plan written on 2026-10-20 under a
> branch that was later deleted. The engine was built the next day as **decision #156** (`src/event_engine/`,
> `tests/test_event_engine.py`); read DECISIONS.md #156 for what actually shipped. Where they differ, #156 and
> the code win. Known differences: this plan numbered itself "#145" (that number belongs to the Architect's
> 10-16 loss-diagnostic rulings); it proposed approval through Discord, but the Architect ruled auto-execution
> for PAPER_EVENTS only; it kept estimates computed by our code, but the Architect then allowed LLM estimates
> to pass the gates, tagged `estimate:llm`; and the book is long OTM options, not cash equity. Kept because the
> milestones E0-E6, the resource plan and the assumptions record the reasoning.

*2026-10-20. Source of the method: `docs/EVENT_PLAYBOOK.md` (the owner's uploaded playbook, saved verbatim).*

## Owner rulings (2026-10-20)

1. **Paper trades for everything.** An approved event suggestion becomes a paper trade. Decision models to
   PICK trades and SQUARE OFF trades are coming soon, so the engine needs slots for them. Build a **₹10,00,000
   sandbox** ("1.1") that can use the history of the ₹10L portfolio plus whatever new is added, **so that if
   something breaks it is okay** (it must not be able to hurt the real paper books).
2. **Sized for the Mini PC, but scalable.** Heavy compute and bigger/faster local storage may be needed later.
   **No local LLM:** use the free LLM API key (Gemini free tier). Solve the resource crunch small now; go full
   later by turning knobs, not rewriting.
3. **Discord budget 5 → 7 per day, for everyone.**
4. **Estimates come from our own calculation, never from the LLM.** The LLM only reads text and extracts
   reported figures. **Layers A, B and C are all switched on together.** The API key goes in only when the
   plan is accepted. This is not easy to build; plan first.

## Assumptions I made (change any of them)

- **"History of 10" = read-only access to PAPER_10L's resolved outcomes and journal** as prior history for the
  decision models. The sandbox starts with NO open positions. (If you meant a full copy of its open book, say so.)
- **V0 is long-only cash equity.** Short ideas wait for a model that can use a bear spread on F&O names.
- **The sandbox uses the existing paper-account door** (the one PAPER_2L and the shadow learner use), with its
  own account id `PAPER_EVENTS`, not a second ledger. House rule RULE 7: one settlement path.
- **While the node trial runs, the sandbox lives in the node's database** and its account id is excluded from the
  nightly VM-vs-node reconcile, the firm treasury, and the dashboard totals.

## Architecture

```
news feed / RSS ─▶ A. detect + classify + extract (Gemini free API, reads text only)
                      │  reported figures, each with its source quote (code checks the quote is in the text)
                      ▼
              B. event card  ◀── config/exposure_map.yaml (hand-seeded, tagged estimate/reported)
                 steps 3-9: exposure, price transmission, demand response, constraints,
                 MATERIALITY gate, PRICED-IN check, timeline   — ALL numbers computed by OUR code, with ranges
                      │
                      ▼
              C. suggestion ─▶ Discord Approve / Dismiss ─▶ PAPER_EVENTS (₹10L) ─▶ resolve on real prices
                                                            ▲                      │
                                       decision-model slots: pick() / square_off()   ▼
                                                            Brain Map outcomes ─▶ confidence per event type
```

Authority: the event engine is a Department 8 opinion that writes cards. It reaches money only through the
owner's Approve press, and only inside `PAPER_EVENTS`. It never touches PAPER_10L, the VM's book, sizing or the
Macro Regime Engine. A kill switch `event_engine_enabled` turns every part off.

## Build order (milestones; stop at any of them)

| # | Milestone | Done when |
|---|---|---|
| E0 | Foundations: config switches, `PAPER_EVENTS` ₹10L account via the existing door, kill switch, isolation | tests prove it cannot read or write PAPER_10L's open book, treasury, or the VM-vs-node diff |
| E1 | Layer A: classify + extract mechanics; free-API client with cache, quota cap and backoff | a headline yields a saved card with sourced `reported` figures; a failed gate still saves the card |
| E2 | Layer B: exposure map seeded from the FTA, calculators (steps 3-9) | the FTA card reproduces the playbook's conclusions: Tata fails the materiality gate, M&M reads as an overreaction |
| E3 | Layer C: suggestion → Discord → paper entry → settle | an approved card becomes a PAPER_EVENTS position and resolves against real prices |
| E4 | Decision-model slots `pick()` / `square_off()`; every trade tagged with the model version | two baseline models compared head to head in the sandbox |
| E5 | Library replay of hand-built past events (FTA, GST 2.0, UK deal, RBI MPC days) | confidence per event type starts from replayed outcomes, labelled as replay |
| E6 | Node schedule, daily event report, disk caps, log rotation | the nightly run is boring |

## Rules that carry over

- **Estimates are computed, not asked.** The LLM never produces a number tagged `estimate`. It extracts `reported`
  figures and the code verifies the quoted sentence exists in the fetched text.
- **Ranges are mandatory** (low / base / high). No suggestion from a point estimate.
- **Materiality gate** default 2% of annual revenue, adjustable. A failing card is still saved.
- **Priced-in check** uses bhavcopy end-of-day prices from the lake (on the node), yfinance only as a fallback
  (yfinance is home-IP only; the VM cannot use it).
- **Confidence starts low and is earned** from resolved sandbox trades by event type.
- **Abstain, never invent:** missing data gives a named skip reason.

## Resource plan (free API, small box, room to grow)

- **Free-tier limits are the real crunch.** One LLM call per event, not per headline; cache by content hash;
  a daily call cap in config; a queue with backoff; when quota is gone the engine abstains with a named reason.
- **Only public news text goes to the API.** Never book, positions or P&L. Free tiers may retain prompts.
- **8 GB RAM, 232 GB disk:** one job at a time, nothing over about 1 GB, run off-hours, event lake and database
  paths in one config constant so they can move to a bigger or faster disk.
- **Scaling later is configuration:** a higher API tier, a second worker, a bigger disk path. Batch jobs
  (replay, price pulls) checkpoint so they can resume or move to a bigger machine.

## Discord

Daily cap 5 → 7 for all hosts (a config change that ships with the next VM deploy and affects the VM). The
event digest is one scheduled card inside the 7. Approve/Dismiss suggestion cards count in the 7. The crash,
live-exit and token pages stay "always send" and outside the cap.

## Open items

1. Short ideas: bear spreads on F&O names once a decision model can choose them.
2. The exact NSE symbol for the Tata Motors passenger-vehicle company (the playbook says TMPV; verify).
3. The playbook's JLR revenue figure is "approximate, from memory": it stays tagged `estimate` until sourced.
4. After the node replaces the VM, where `PAPER_EVENTS` lives is decided with that migration.
