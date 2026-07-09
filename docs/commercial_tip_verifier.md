# Commercial Tip Verification & Breakout Proof Engine — Design Spec (PLANNING ONLY)

> **Status: conceptual blueprint, locked 2026-07-09. NO code, routes, or
> schemas exist yet.** This document is a technical design capture, not
> a go/no-go decision — that decision needs §1 resolved first.
>
> **This is a different category of thing from every other spec in this
> repo.** Everything else here (the core engine, the thematic playbooks
> spec, the self-evolving brain map spec) is a **private, paper-only,
> human-approved personal system** (decision #11) — nothing it does
> reaches another person. This document describes a **public-facing
> commercial product** that outputs scored opinions about specific
> securities to third parties. That distinction is the single most
> important thing in this file, which is why it comes before the
> architecture instead of after it.

## 1. Regulatory framing — read this before anything else

**In India, providing recommendations, research, or "verification" of
buy/sell opinions on specific securities to the public is a regulated
activity**, primarily under two SEBI frameworks:

- **SEBI (Research Analyst) Regulations, 2014** — covers publishing
  research/recommendations on securities for consideration by clients.
- **SEBI (Investment Adviser) Regulations, 2013** — covers advising
  others on securities investment decisions for consideration.

A tool that ingests a tip ("BUY SYMBOL at X for breakout target Y"),
scores it, and hands back a consumer-facing **"Breakout Confidence
Score"** is very plausibly functioning as either research or advice in
substance, regardless of whether it's framed as "verification" rather
than "origination" — the output (a scored opinion on a specific security
that a member of the public could act on) is what regulators look at,
not the framing. SEBI has been **actively and visibly enforcing** against
unregistered tip/signal/"finfluencer" operations in recent years; this is
not a dormant or theoretical area of scrutiny.

**What this means concretely:**
- Operating this publicly, for money, without RA/IA registration is a
  **real legal exposure to you personally** — not a compliance nitpick to
  clean up post-launch.
- I am not a lawyer and this document does not constitute legal advice.
  **Before any public launch, this needs review by a securities lawyer
  familiar with SEBI RA/IA regulations** — that review is the actual
  first deliverable of this project, ahead of any code.
- **Output framing materially affects risk.** A numeric "confidence
  score" plus an explicit buy-trigger-target framing reads much closer to
  advice than a neutral "here is the technical evidence for and against
  this claim, unscored, with sources" framing would. If this is pursued,
  discuss the framing question with counsel specifically — it is likely
  the single biggest lever available for reducing regulatory scope.
- **Until that legal review happens, this should stay exactly where
  everything else in this repo lives: private, personal-use, never
  distributed to other users** — the same discipline the observation
  week and decision #11 already enforce, just applied to a "who sees the
  output" axis instead of a "who approves the trade" axis.
- The rest of this document assumes that gate is resolved favorably and
  describes the technical architecture on the other side of it. It is
  written so the engineering work is ready to reference once (and only
  once) that's true.

## 2. Ingestion & NLP — the Core Triad

- Ingests unstructured tip text ("BUY SYMBOL at X for breakout target Y",
  or looser phrasing) and extracts the **Core Triad**: Target Symbol,
  Entry Trigger Price, Horizon.
- Same extraction pattern already proven in this codebase — local Ollama,
  strict-JSON-schema-gated output (the `local_parser.py` /
  `evolution.py`'s Analyst-proposal pattern: malformed output is
  rejected, never half-interpreted, retried with the failure reason
  appended rather than blindly re-asked).
- Symbol resolution reuses `dhan_client`'s existing ticker-normalization
  layer (`SECURITY_ID_MAP` + alias resolution) so "the triad" maps onto a
  real, tradeable instrument the rest of the pipeline can actually query
  — not a fuzzy string.
- **Failure mode to design for:** ambiguous or multi-symbol tips, missing
  horizon, or price targets with no clear trigger condition must produce
  an explicit "could not parse a verifiable claim" result, not a
  best-guess triad — an invented triad is worse than an honest refusal
  here, same principle as everywhere else in this system.

## 3. The Verification "Proof" Framework

### 3.1 Alternative data verification
- Tracks momentum anomalies in social sentiment, financial forums, and
  search velocity to evaluate whether a claimed breakout has real
  narrative backing versus being asserted in isolation.
- **This inherits the exact lawful-retrieval boundary already specified
  in `docs/thematic_playbooks_spec.md` §7.5**: respects robots.txt, site
  ToS, and rate limits; never attempts to bypass a paywall or anti-bot
  measure; escalates to a human rather than circumventing when blocked.
  That constraint does not weaken for a commercial product — if anything
  it tightens, since a commercial operation is a much higher-visibility
  target for a ToS dispute than a personal research tool.
- **A second-order risk unique to this use case:** the alternative-data
  sources themselves (social sentiment, forum chatter, search spikes) are
  exactly what a coordinated pump attempts to manufacture. A verification
  engine that treats "narrative backing" as confirming evidence, without
  distinguishing organic interest from manufactured chatter, could end up
  **laundering a pump into a "verified" score.** §3.3's pump-risk
  guardrail is not a nice-to-have bolted onto the output — it needs to be
  an input to the alt-data scoring itself, not just a separate flag
  computed afterward.

### 3.2 Technical validation
- Connects to the existing market data client (`dhan_client` — data-only,
  same as everywhere else in this repo) to verify whether the underlying
  is showing an **actual volume-backed breakout** past a structural
  resistance level at the claimed trigger window.
- Reuses existing primitives rather than inventing new ones: support/
  resistance levels already exist in `trade_planner.py`'s structure logic;
  the relative-strength / breakout-confirmation approach already
  specified as Trigger B in `docs/thematic_playbooks_spec.md` §2 is the
  same shape of test, applied to a single claimed tip instead of a
  sector rotation.
- Volume confirmation matters specifically because price alone breaking a
  level on thin volume is one of the more common false-breakout patterns
  — this is the concrete technical check that should carry most of the
  weight in §3.3's score, precisely because it's the hardest one to fake
  compared to social sentiment.

### 3.3 The Expectancy & Pump-Risk output

1. **Breakout Confidence Score (X%)** — real technical validation
   alignment. **This inherits the calibration requirement already
   established in `docs/self_evolving_brain_map.md` §4.2, non-negotiably,
   and more strictly**: an uncalibrated confidence score shown to *other
   people* who may act on it with real money is a categorically worse
   failure than the same uncalibrated number shown only to you internally
   for your own paper trades. If it isn't calibrated (checked that
   claims labeled ~70% actually resolve favorably ~70% of the time), it
   must not ship — full stop, not "ship with a disclaimer."
2. **Liquidity & Pump-Risk guardrails** — high-alert flags for illiquid
   tickers vulnerable to coordinated retail manipulation (thin float,
   abnormal volume-to-float ratios, social-chatter-to-liquidity mismatch
   per §3.1's second-order risk). This is a genuinely protective,
   pro-user feature and the part of this design most worth keeping
   regardless of how §1 resolves.
3. **The Proof Ledger** — the hard technical facts supporting or
   contradicting the thesis, shown transparently. Recommend neutral,
   evidentiary language ("price crossed resistance level L on volume V,
   Nx the 20-day average" ) over verdict language ("verified" /
   "debunked") — both because it's more honest about what a technical
   check can and can't establish, and because declarative "verified" /
   "this is a pump" language about a specific claim carries its own
   liability surface independent of the RA/IA question in §1.

## 4. Infrastructure — deliberately NOT the personal system's infra

This should **not** share the private engine's VM, database, or
credentials. Reasons compound:
- The personal engine's entire design (decisions #11, #47, #48 and every
  spec in this repo) assumes paper-only, human-gated, single-user,
  zero-blast-radius. A public product has real users, uptime
  expectations, and a security surface — mixing them risks the personal
  system's safety guarantees as collateral damage from the commercial
  one's operational demands.
- **Liability isolation.** If §1's legal review proceeds, counsel will
  almost certainly want the commercial product's data, logs, and
  infrastructure separable from personal trading records — bundling them
  now creates unwinding work later for no present benefit.
- If and when this is greenlit, it should be scoped as its own
  infrastructure project, sized to its own (unknown at this stage)
  traffic/compliance requirements — not appended to the dual-node
  Mac/VM architecture built for one person's paper book.

## 5. Reality checks (technical, secondary to §1)

- **Core Triad ambiguity.** Real tips are often vaguer than the clean
  example in the prompt ("this one's about to move," no explicit price).
  The parser needs a well-defined "not enough structure to verify"
  output, not a forced guess (§2).
- **Alt-data can be gamed** (§3.1) — treat it as corroborating evidence
  at most, never the primary signal; §3.2's volume-confirmed technical
  check should dominate the score.
- **False breakouts are common enough that a single technical check is
  not sufient on its own** — this likely needs the same
  matched-historical-cluster expectancy thinking from
  `docs/self_evolving_brain_map.md` §3 (how often does *this exact
  pattern* actually hold vs. fail) rather than a static rule ("price >
  resistance" = confirmed).
- **Every technical risk above is subordinate to §1.** None of them
  matter if the product can't legally ship in its current framing —
  resolve the regulatory question first; it will likely reshape what
  "the output" is even allowed to look like before the technical risks
  are worth optimizing.
