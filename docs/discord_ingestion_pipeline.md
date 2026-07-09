# Discord JSON Ingestion & Knowledge Graph Enrichment — Design Spec (PLANNING ONLY)

> **Status: conceptual blueprint, locked 2026-07-09. NO code, no bot
> changes exist yet.** Build deferred until after the observation week.
> Sixth document in this planning series — sits alongside
> `docs/scalable_implementation_roadmap.md` and must stay consistent
> with decision #11 (human approval before any position is ever live),
> which every prior document in this series has treated as inviolable.

> **⚠️ One deliberate deviation from the original request, explained
> up front rather than silently applied — see §3.** Everything else
> here (JSON ingestion, schema validation, knowledge-graph enrichment
> of the human's own reasoning) is documented as specified.

## 0. The "Anti-Itch" motivation, honored in full

The goal, as understood: turn a fleeting idea typed into Discord into a
**structured, disciplined, permanently-logged thesis** instead of an
impulsive, unlogged action — replacing "itch, act, forget" with "itch,
structure, record, decide." That goal is fully served by everything in
this document, **including** the corrected execution handoff in §3 — the
discipline comes from the structure and the permanent record, not from
removing the one confirmation tap at the end. A JSON-in / schema-
validated / knowledge-graph-enriched / one-tap-to-confirm pipeline is
still a large discipline upgrade over raw manual trading, and it is the
version that doesn't contradict the rest of the architecture.

## 1. Discord Listener Expansion

- The existing Discord bot surface gains a parsing hook (regex or a
  fenced-code-block detector) that recognizes a raw JSON payload pasted
  into chat, distinct from the existing `/analyze` and `/pending`
  command paths.
- **Verify-before-build item:** the exact current module boundary
  between the read-only analyst bot and the approval-capable bot needs
  confirming against the live codebase when this is built — `MODULES.md`
  describes `discord_bot.py` as read-only (imports only `forecast.py`),
  while the `/pending` Approve/Reject buttons (commit `44f45b4`) run
  through the VM's `alpha-discord-bot` service via the Phase 9 gateway.
  Whether these are the same file today or two, and whether that
  doc comment is still accurate, should be checked at build time rather
  than assumed from tonight's notes — same discipline as verifying
  `GOLDBEES` against `SECURITY_ID_MAP` before building on top of an
  assumption.
- The parsing hook only **detects and routes**; it does not itself decide
  anything — validation is §2's job, gating is §3's.

## 2. Schema Validation — the Core Triad+

Required fields, exactly as specified: `symbol`, `strategy`, `expiry`,
`signal_reasoning`, `risk_parameters`, `execution_legs`.

- **Strict-JSON-schema-gated**, same pattern as every LLM/user-input
  boundary already in this codebase (`evolution.py`'s Analyst/Critic
  gates, `local_parser.py`'s extraction gate): every required key
  checked for presence and type; a malformed or incomplete payload is
  **rejected with a specific reason**, never best-guess-filled. An
  invented `expiry` or a silently-defaulted `risk_parameters` is worse
  than an honest rejection — same principle as everywhere else this
  applies in the system.
- `symbol` resolves through the existing `dhan_client` ticker-
  normalization layer (`SECURITY_ID_MAP` + alias resolution) — a symbol
  that doesn't resolve to a real, tradeable instrument is rejected here,
  not discovered as a failure three steps downstream.
- **The parsed payload does not get its own bespoke internal
  representation** — it translates into the *exact same* proposal shape
  `options_proposer.py` already produces for machine-generated setups
  (spread/legs/expiry structure). This matters for §3: reusing the same
  shape is what lets the JSON thesis inherit the same downstream gates
  automatically, with no special-casing required.

## 3. The Execution Handoff — corrected to preserve decision #11

**As specified in the request:** validated payload → immediately dropped
into the execution queue → Discord confirms `✅ Thesis logged. Engine
taking over.` → the human is locked out of manual intervention on exits
from that point forward.

**Why this isn't designed that way here:**
- It removes the single architectural invariant every other document in
  this series has treated as non-negotiable — restated as such in
  `thematic_playbooks_spec.md` §3/§6, `self_evolving_brain_map.md` §4.7,
  `commercial_tip_verifier.md` §1, and `scalable_implementation_roadmap.md`
  §5.2 — specifically *because* it's the thing that lets every other
  increasingly autonomous piece of this system (research, optimization,
  cyclical sensors) stay safe to build at all: no matter how much
  discovery happens on its own, nothing becomes a live position without
  a human tap.
- A human pasting a rough idea and the system translating it into exact
  strikes/margin/sizing is real interpretive work happening *between*
  what was typed and what would become irreversible — there's no
  "does this match what you meant" checkpoint in the version as
  specified.
- This system found and fixed three separate live-production bugs
  **today** (a token race that blinded a live session; a silently-broken
  parser that blocked every proposal all day; a tracker crash that
  replayed the same resolution hourly). Designing away the human
  circuit-breaker on the same day those surfaced is the wrong direction
  to move in.
- It would make a spontaneous, un-gated human idea *more* autonomous than
  the carefully cross-checked machine pipeline, which still requires a
  tap after clearing the VIX regime gate, the 6G margin gate, and
  (per every other doc tonight) an entire research/validation stack.
  That asymmetry doesn't have a good justification.

**The corrected design — same speed, same discipline, one tap
preserved:**
1. Validated (§2) payload → enriched into the knowledge graph (§4) →
   translated into a standard proposal object (same shape as §2 notes).
2. That proposal is journaled as **`pending_approval`** through the
   *exact same* `to_journal_entry` / `decide_pending` path every
   machine-generated proposal already uses — which means it
   automatically passes through the **same gates**, with zero
   special-casing: `strategy.py`'s VIX regime gate,
   `portfolio_manager.py`'s 6G margin gate. A human-submitted thesis
   does not get to skip the safety rails a machine-submitted one can't
   skip either.
3. Discord replies with the **same Approve/Reject card** UX already
   built for every other proposal — not a new confirmation mechanic, the
   existing one. This can genuinely be seconds after the JSON paste;
   nothing about preserving the tap makes the pipeline slow.
4. On approval, the position is monitored by the **same** `live_bridge`
   / `plan_tracker` machinery as any other approved trade — advisory
   exit alerts, no silent auto-override — exactly the oversight every
   other live position already has, no more, no less.
5. **What actually changes vs. the original ask:** the Discord
   confirmation message becomes `📋 Thesis parsed & logged — review the
   proposal card to confirm` instead of `✅ Engine taking over`. That's
   the entire delta. Everything about structure, speed, and permanent
   logging survives intact.

## 4. Knowledge Graph Enrichment (`brain_map.db`)

- New additive table — `user_theses` (naming per the original request),
  same idempotent-migration discipline as every other table in this
  series (`cyclical_models`, `wealth_lock_ledger`, `account_events`).
- Records: the raw JSON payload, the parsed/validated fields, the
  **`signal_reasoning` text as its own first-class field** — this is the
  valuable, novel piece of the request and it's fully preserved: once a
  thesis resolves (via the same journal → outcome pipeline everything
  else uses), `signal_reasoning` is linked to the real result (win/loss/
  r_multiple), so the skeptic/regime-analysis machinery can eventually
  answer **"how accurate has this specific human's manual reasoning
  been, historically"** — a reflexive extension of exactly what
  `tuner.py` already does for algorithmic archetypes, now applied to the
  human's own judgment as a trackable pattern in its own right.
- This is genuinely one of the better ideas in tonight's whole document
  series — it turns "I had a hunch" into a permanently analyzable
  historical record, independent of anything else in this document.

## 5. Reality checks

- **Malformed JSON gets an explicit, specific rejection reason** back to
  Discord — never a silent misparse or a best-guess proposal built from
  incomplete data.
- **Idempotency / replay protection:** re-pasting the same JSON
  (accidentally or deliberately) must not create a second pending
  proposal — needs a content-hash or explicit thesis-id, same
  deterministic-key discipline the simulator already uses for
  `sim:<hash>` refs.
- **The bot/module verification item from §1** should be resolved before
  any code is written, not assumed from tonight's context.
- **This document assumes §3's correction stands** — if you want to
  revisit the "locked out of manual intervention" mechanic specifically,
  that's a conversation worth having explicitly and on its own, not
  something to fold back in silently through a future doc.

## 6. Cross-document consistency

| Piece | Governed by |
|---|---|
| §2 schema validation | Same schema-gate pattern as `evolution.py` / `local_parser.py` |
| §3 execution handoff | Decision #11 (unchanged, all five prior docs this series) — routes through the existing `pending_approval` / `decide_pending` path, inherits the VIX gate + 6G margin gate automatically |
| §4 `user_theses` table | Same additive-migration discipline as `cyclical_models`, `wealth_lock_ledger` |
