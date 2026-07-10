"""
Alpha Trading — Phase 5: the options spread proposer
====================================================

The wiring between market data and the Phase 5 machinery: reads the
underlying's trend (same suggestions.analyze() the equity engine uses),
fetches India VIX and the real Dhan option chain, picks strikes, builds
the regime-matched defined-risk spread via strategy.StrategyConstructor,
sizes it by absolute max loss (OPTIONS_RISK_PER_TRADE_PCT), and — on the
user's approval — journals an entry the plan tracker resolves atomically.

Regime -> structure (DECISIONS.md #27):
  bullish  (uptrend dip / fresh golden cross)  -> bull call spread (debit)
  bearish  (downtrend / fresh death cross)     -> bear put spread (debit)
  neutral  (no directional signal)             -> iron condor (credit),
           STRICTLY blocked when India VIX > 16 or VIX is unavailable.

PAPER ONLY, human-in-the-loop (decision #11): this module proposes and
journals; it never touches a broker. Approved spreads don't move cash at
entry — the tracker net-settles the P&L at the atomic basket exit.

Run interactively from the project folder:

    python3 -m src.options_proposer                   # NIFTY 50 (default)
    python3 -m src.options_proposer "NIFTY BANK"
    python3 -m src.options_proposer --review-pending  # decide market-loop
                                                      # PENDING_APPROVAL entries
                                                      # (no market data fetched)

Every data input is injectable so tests run fully offline.
"""

import os
from datetime import date, timedelta

from src import journal
from src import portfolio as pf
from src.config import OPTIONS_RISK_PER_TRADE_PCT
from src.dhan_client import get_expiry_list, get_india_vix, get_option_chain
from src.strategy import StrategyConstructor
from src.suggestions import analyze

# NSE lot sizes for the option-enabled underlyings (contract spec, not
# market data — revised rarely and loudly by the exchange).
LOT_SIZES = {"NIFTY 50": 75, "NIFTY BANK": 35}

# Never open a position that the 2-days-before-expiry rule would
# immediately close: skip expiries closer than this many days out.
MIN_DAYS_TO_EXPIRY = 7

# Condor short strikes sit ~this far OTM on each side (rounded to a real
# chain strike); protective wings sit WING_STEPS strike-steps further out.
SHORT_STRIKE_OTM_PCT = 2.0
WING_STEPS = 4


def market_view(analysis: dict) -> str:
    """suggestions.analyze() result -> 'bullish' / 'bearish' / 'neutral'.
    Same signal logic as the equity engine: a fresh golden cross or an
    uptrend dip is bullish, any downtrend read is bearish, and a trend
    with no actionable momentum is a range (mean-reversion) view."""
    from src.config import RSI_OVERSOLD
    if not analysis["uptrend"]:
        return "bearish"
    if analysis["fresh_cross"]:
        return "bullish"
    rsi = analysis["rsi"]
    if rsi is not None and rsi <= RSI_OVERSOLD:
        return "bullish"
    return "neutral"


def pick_expiry(expiries: list, today: date = None) -> str | None:
    """First expiry at least MIN_DAYS_TO_EXPIRY days out, or None."""
    today = today or date.today()
    for exp in sorted(expiries or []):
        try:
            if (date.fromisoformat(exp) - today).days >= MIN_DAYS_TO_EXPIRY:
                return exp
        except ValueError:
            continue
    return None


def _strikes(chain: dict) -> list:
    """Sorted strike floats from a Dhan option-chain payload."""
    return sorted(float(s) for s in (chain.get("oc") or {}))


def _premium(chain: dict, strike: float, kind: str) -> float | None:
    """Last traded premium for one leg (kind 'ce'/'pe'), or None. Tries
    the exact key format Dhan uses (six decimals) then a plain match."""
    oc = chain.get("oc") or {}
    node = oc.get(f"{strike:.6f}") or oc.get(str(strike)) or {}
    leg = node.get(kind) or {}
    price = leg.get("last_price")
    return float(price) if price else None  # 0/None -> untradeable leg


def _nearest_strike(strikes: list, target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def _step(strikes: list) -> float:
    """The chain's strike interval (e.g. 50 for NIFTY)."""
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else 0.0


def build_proposal(underlying: str = "NIFTY 50", *, analysis: dict = None,
                   vix: float = None, expiry: str = None, chain: dict = None,
                   book: dict = None, prices: dict = None,
                   risk_pct: float = None,
                   short_strike_otm_pct: float = None) -> dict:
    """The full pipeline, every input injectable for offline tests.

    `risk_pct` overrides OPTIONS_RISK_PER_TRADE_PCT (e.g. vol_bridge may
    scale it down 30 % under an Expansion regime).  `short_strike_otm_pct`
    overrides SHORT_STRIKE_OTM_PCT for the iron condor's put short strike
    (vol_bridge widen_wings mode widens it to buffer tail risk).  Both fall
    back to their module constants when None.

    Returns {"proposal": dict-or-None, "reason": str, "view": str-or-None,
    "vix": float-or-None} — `reason` always explains a None proposal."""
    _risk_pct = risk_pct if risk_pct is not None else OPTIONS_RISK_PER_TRADE_PCT
    _otm_pct = (short_strike_otm_pct if short_strike_otm_pct is not None
                else SHORT_STRIKE_OTM_PCT)
    if analysis is None:
        analysis = analyze(underlying)
    if analysis is None:
        return {"proposal": None, "view": None, "vix": vix,
                "reason": f"not enough price history for {underlying}"}
    view = market_view(analysis)

    if vix is None:
        vix = get_india_vix()
    if expiry is None:
        expiry = pick_expiry(get_expiry_list(underlying))
    if expiry is None:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "no usable expiry (need >= "
                          f"{MIN_DAYS_TO_EXPIRY} days out)"}
    if chain is None:
        chain = get_option_chain(underlying, expiry)
    if not chain or not chain.get("oc"):
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "option chain unavailable"}

    strikes = _strikes(chain)
    step = _step(strikes)
    if step <= 0 or len(strikes) < 2 * WING_STEPS + 1:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "option chain too thin to build a spread"}
    spot = float(chain.get("last_price") or analysis["price"])
    atm = _nearest_strike(strikes, spot)
    lot_size = LOT_SIZES.get(underlying, 75)
    sc = StrategyConstructor(vix=vix, lot_size=lot_size)

    def leg_premiums(pairs):
        """[(strike, 'ce'/'pe'), ...] -> premiums, or None if any leg has
        no tradeable quote (never build on a dead strike)."""
        prems = [_premium(chain, s, k) for s, k in pairs]
        return None if any(p is None for p in prems) else prems

    if view == "bullish":
        lo, hi = atm, atm + WING_STEPS * step
        prems = leg_premiums([(lo, "ce"), (hi, "ce")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_bull_call_spread(lo, hi, prems[0], prems[1])
        signal = (f"bullish trend read on {underlying} — bull call spread "
                  f"{lo:g}/{hi:g} CE, defined risk")
    elif view == "bearish":
        hi, lo = atm, atm - WING_STEPS * step
        prems = leg_premiums([(hi, "pe"), (lo, "pe")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_bear_put_spread(hi, lo, prems[0], prems[1])
        signal = (f"bearish trend read on {underlying} — bear put spread "
                  f"{hi:g}/{lo:g} PE, defined risk")
    else:  # neutral -> iron condor, VIX-gated inside the constructor
        allowed, why_regime = sc.validate_regime("iron_condor")
        if not allowed:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": f"range-bound structure blocked: {why_regime}"}
        put_short = _nearest_strike(strikes, spot * (1 - _otm_pct / 100))
        call_short = _nearest_strike(strikes, spot * (1 + _otm_pct / 100))
        wing = WING_STEPS * step
        prems = leg_premiums([(put_short, "pe"), (put_short - wing, "pe"),
                              (call_short, "ce"), (call_short + wing, "ce")])
        if prems is None:
            return {"proposal": None, "view": view, "vix": vix,
                    "reason": "no tradeable quotes at the chosen strikes"}
        spread = sc.construct_iron_condor(put_short, call_short, wing,
                                          prems[0], prems[1], prems[2], prems[3])
        signal = (f"neutral range read on {underlying} (VIX {vix:.1f}) — iron "
                  f"condor {put_short:g}P/{call_short:g}C, wings {wing:g} wide")

    if spread is None:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": "structure failed to build (regime gate or "
                          "incoherent strikes)"}

    if book is None:
        book = pf.load()
    if prices is None:
        prices = {}
    lots = sc.size_lots(spread, book, prices, risk_pct=_risk_pct)
    if lots <= 0:
        return {"proposal": None, "view": view, "vix": vix,
                "reason": (f"max loss Rs.{spread['max_loss']:,.0f}/lot doesn't fit "
                           f"the {_risk_pct:g}% options risk "
                           f"budget (or SPAN margin exceeds cash)")}

    net = spread["net_credit"] if spread["net_credit"] is not None else -spread["net_debit"]
    proposal = {
        # journal.new_entry() contract keys:
        "action": "SPREAD",
        "ticker": underlying,
        "shares": spread["lot_size"] * lots,
        "price": abs(net),
        "signal": signal,
        # Phase 5 payload — exactly what plan_tracker._spread_trackable needs:
        "spread": dict(spread, lots=lots, expiry=expiry, entry_spot=spot),
        "view": view,
        "vix": vix,
        "lots": lots,
    }
    return {"proposal": proposal, "view": view, "vix": vix, "reason": "ok"}


def to_journal_entry(proposal: dict, decision: str, why: str) -> dict:
    """A tracker-resolvable journal record: the standard new_entry()
    fields (short_id, date, decision, why, ...) plus the spread payload.
    Regime-Aware Memory: the market conditions the proposal was born
    under (trend view + VIX band) ride along, so the Brain Map can later
    answer "how does this setup do in conditions like these?"."""
    from src.regime import regime_for
    entry = journal.new_entry(proposal, decision, why,
                              pattern_tags=[proposal["spread"]["strategy"]])
    entry["spread"] = proposal["spread"]
    entry["regime"] = regime_for(proposal.get("view"), proposal.get("vix"))
    return entry


def _describe(p: dict) -> list:
    s = p["spread"]
    lines = [
        f"{s['strategy'].replace('_', ' ').title()} on {p['ticker']} "
        f"(view: {p['view']}, VIX: {p['vix'] if p['vix'] is not None else 'n/a'})",
        f"  expiry {s['expiry']}  |  {p['lots']} lot(s) x {s['lot_size']}",
    ]
    for leg in s["legs"]:
        lines.append(f"  {leg['side']:4} {leg['option_type']} {leg['strike']:g} "
                     f"@ Rs.{leg['premium']:,.2f}")
    net = s["net_credit"] if s["net_credit"] is not None else s["net_debit"]
    kind = "credit" if s["net_credit"] is not None else "debit"
    lines += [
        f"  net {kind} Rs.{net:,.2f}/share",
        f"  max loss Rs.{s['max_loss'] * p['lots']:,.0f}  |  "
        f"max profit Rs.{s['max_profit'] * p['lots']:,.0f}  |  "
        f"SPAN margin Rs.{s['margin']['total_margin'] * p['lots']:,.0f} "
        f"(naked would block Rs.{s['margin']['naked_margin'] * p['lots']:,.0f})",
        "  exits: auto at 65% of max profit, or 2 days before expiry (atomic basket)",
    ]
    memory = p.get("memory_context")
    if memory:
        lines.append("  memory (linked patterns):")
        lines += [f"    {line}" for line in memory.splitlines()]
    if p.get("skeptic_note"):
        lines.append("  " + p["skeptic_note"].replace("**", ""))
    return lines


def _memory_context_for(nodes, engine=None) -> str:
    """Phase 6C/6D knowledge-graph lookup, fail-safe. `nodes` is one seed or
    a list of seeds (ticker, regime/view, strategy) — their linked context
    from the Brain Map's `graph_edges` is merged (de-duplicated) into one
    block, or "" when the graph is empty or unavailable. Seeding by strategy
    and view is what surfaces the Phase 6D causal triples (e.g. iron_condor
    RESULTS_IN loss), which are keyed by concept, not ticker. Read-only
    inference (decision #33): never raises, never writes, so the proposal
    path is never blocked. `engine` is injectable so tests stay offline."""
    try:
        if isinstance(nodes, str):
            nodes = [nodes]
        nodes = [n for n in nodes if n]
        if not nodes:
            return ""
        if engine is None:
            from src.graph_engine import GraphEngine
            engine = GraphEngine()
        lines, seen = [], set()
        for node in nodes:
            for line in engine.summarize_context(node).splitlines():
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        print(f"  (memory-graph lookup skipped: {e})")
        return ""


def _memory_seeds(p: dict) -> list:
    """The nodes a proposal is queried against: its underlying, its regime
    view, and its spread strategy — so both ticker-anchored and concept-
    anchored (Phase 6D causal) edges can match."""
    return [p.get("ticker"), p.get("view"), (p.get("spread") or {}).get("strategy")]


def _skeptic_note_for(p: dict, auditor=None, engine=None) -> str:
    """Phase 11: the Random Forest Skeptic's numerical audit of this
    proposal — the quantitative cross-check on the knowledge graph's
    semantic reasoning. Gathers the same seeds' 2-hop graph edges plus the
    Brain Map's realized stats for the strategy/view tags, hands both with
    the proposal's market numbers to skeptic_agent.RandomForestAuditor,
    and returns the strictly formatted "⚠️ Skeptic Agent Warning" block —
    or "" when the model abstains (untrained — the scaffolding default),
    the probability is healthy, or anything at all fails. ADVISORY ONLY:
    this never gates, resizes, or rejects a proposal (the human decides).
    `auditor`/`engine` are injectable so tests stay offline."""
    try:
        if auditor is None:
            from src.skeptic_agent import RandomForestAuditor
            auditor = RandomForestAuditor()
        if engine is None:
            from src.graph_engine import GraphEngine
            engine = GraphEngine()
        edges, seen = [], set()
        for node in _memory_seeds(p):
            if not node:
                continue
            for e in engine.get_relevant_context(node):
                key = (e.get("source"), e.get("relation"), e.get("target"))
                if key not in seen:
                    seen.add(key)
                    edges.append(e)
        memory_stats = None
        try:
            from src import brain_map
            conn = brain_map.connect()
            memory_stats = brain_map.query_similar_events(
                conn, [t for t in ((p.get("spread") or {}).get("strategy"),
                                   p.get("view")) if t])
            conn.close()
        except Exception:
            pass  # stats are one optional feature; the audit runs without
        result = auditor.audit(p, graph_context=edges,
                               memory_stats=memory_stats)
        if not result.get("warn"):
            return ""
        return (f"⚠️ **Skeptic Agent Warning**: modeled win probability "
                f"{result['probability']:.0%} — the Random Forest's numbers "
                f"disagree with this setup's semantic read. Advisory only; "
                f"the decision stays yours (paper only).")
    except Exception as e:
        print(f"  (skeptic audit skipped: {e})")
        return ""


def _format_proposal_alert(p: dict, action_note: str = None) -> str:
    """The rich Discord markdown for a fully constructed proposal, sent
    the moment the terminal pauses for the y/n decision — so the phone
    knows the system is waiting on a human. `action_note` overrides the
    default action line (headless mode explains itself differently).

    When the proposal carries `memory_context` (the Phase 6C graph lookup),
    a 🧠 Memory block of linked historical patterns rides along in the
    rationale — advisory context only, never a rule change (decision #33)."""
    s = p["spread"]
    vix_text = f"{p['vix']:.2f}" if p["vix"] is not None else "n/a"
    legs_block = "\n".join(
        f"{leg['side']:4} {leg['option_type']} {leg['strike']:g} "
        f"@ Rs.{leg['premium']:,.2f}"
        for leg in s["legs"])
    net = s["net_credit"] if s["net_credit"] is not None else s["net_debit"]
    kind = "Net Credit" if s["net_credit"] is not None else "Net Debit"
    lots = p["lots"]
    action = action_note or ("paused for human-in-the-loop approval in "
                             "the terminal session (paper only).")
    memory = p.get("memory_context")
    memory_block = (f"🧠 **Memory (linked patterns)**:\n```\n{memory}\n```\n"
                    if memory else "")
    skeptic = p.get("skeptic_note")
    skeptic_block = f"{skeptic}\n" if skeptic else ""
    return (
        f"🚨 **PROPOSAL ALERT: {s['strategy'].replace('_', ' ').title()}**\n"
        f"**Market Regime**: {p['view'].title()} view on {p['ticker']} | "
        f"India VIX {vix_text} | expiry {s['expiry']}\n"
        f"**Legs** ({lots} lot(s) x {s['lot_size']}):\n"
        f"```\n{legs_block}\n```\n"
        f"**Economics**: {kind} Rs.{net:,.2f}/share | "
        f"Max Loss Rs.{s['max_loss'] * lots:,.0f} | "
        f"Max Profit Rs.{s['max_profit'] * lots:,.0f} | "
        f"SPAN Margin Rs.{s['margin']['total_margin'] * lots:,.0f}\n"
        f"{memory_block}"
        f"{skeptic_block}"
        f"⏸️ **Action Required**: {action}"
    )


def _notify_discord(text: str) -> bool:
    """Fire-and-forget Discord push from this sync CLI. Fail-safe: an
    unconfigured webhook or any error just prints a note — the terminal
    prompt is never blocked or crashed by Discord being unreachable."""
    import asyncio
    from src import notifier
    try:
        return asyncio.run(notifier.send_discord_message(text))
    except Exception as e:
        print(f"  (discord notify failed: {e})")
        return False


AUTO_APPROVE_ENV_KEY = "PAPER_AUTO_APPROVE"
AUTO_APPROVE_WHY = ("(auto-approved: PAPER_AUTO_APPROVE learning mode — "
                    "paper journal only; no broker exists anywhere in this "
                    "system, decision #11's no-execution rule untouched)")


def paper_auto_approve_enabled() -> bool:
    """True ONLY when PAPER_AUTO_APPROVE is explicitly truthy in the
    environment/.env. Default is OFF: the human Approve/Reject gate
    (decision #11's human-in-the-loop, reaffirmed by the Discord-ingestion
    spec §3) stays exactly as it is unless the user deliberately flips
    this switch to maximize learning data. Checked per call so a .env
    change takes effect without a restart.

    Physical isolation from real capital is structural, not conditional:
    the ONLY thing an approval ever does in this codebase is journal a
    paper decision and lock simulated margin — there is no broker client,
    no order endpoint, no real-capital code path for this to reach
    (dhan_client is data-only by hard rule)."""
    return (os.environ.get(AUTO_APPROVE_ENV_KEY, "")
            .strip().lower() in ("1", "true", "yes"))


def run_headless(underlying: str = "NIFTY 50", state: dict = None) -> dict:
    """The market loop's entry point: build the proposal, fire the rich
    Discord alert, journal the entry as PENDING_APPROVAL, and return
    IMMEDIATELY — no input(), no terminal pause, ever.

    `state` (optional) is a dict of build_proposal keyword overrides
    (analysis/vix/expiry/chain/book/prices) — the injection seam the
    Phase 7 simulator and the market loop's fetch_market_state() use.

    Pending entries are tracked hypothetically like rejected ones (user's
    call): if nobody ever decides, the tracker still scores what the
    setup would have done.

    PAPER_AUTO_APPROVE mode: when the env switch is on, the freshly
    journaled pending entry is immediately decided through decide_pending
    — the SAME code path a human tap takes, so the margin gate, the
    journal rewrite, the Discord confirmation and the "opened" broadcast
    all behave identically; only the finger on the button changes. The
    entry's `why` carries the auto-approval marker for the audit trail.

    Returns {"proposed": bool, "reason": str, "entry": dict-or-None,
    "auto_approved": bool}."""
    state = dict(state or {})
    vol_overrides = state.pop("vol_overrides", {})
    bp_extras = {k: vol_overrides[k]
                 for k in ("risk_pct", "short_strike_otm_pct")
                 if k in vol_overrides}
    result = build_proposal(underlying, **state, **bp_extras)
    if result["proposal"] is None:
        return {"proposed": False, "reason": result["reason"], "entry": None}
    p = result["proposal"]
    # Phase 6C: enrich the Discord rationale with linked historical patterns
    # from the knowledge graph (fail-safe: "" when the graph is empty).
    p["memory_context"] = _memory_context_for(_memory_seeds(p))
    # Phase 11: the Random Forest Skeptic's numerical audit — right before
    # the alert is formatted ("" until a trained model exists).
    p["skeptic_note"] = _skeptic_note_for(p)
    entry = to_journal_entry(
        p, "pending_approval",
        "(headless proposal — auto-generated by the market loop, awaiting "
        "human decision)")
    # Phase 2 (holy-grail plan §5.1): stamp what EVERY layer said at this
    # exact moment onto the entry — the substrate per-layer reliability is
    # learned from. Simulator-injected runs stamp too (their state carries
    # the as-of analysis/vix; local artifacts read as-is). Fail-open: a
    # stamp failure never blocks the proposal.
    from src.confluence.evidence import capture_for_entry
    capture_for_entry(entry, underlying, analysis=state.get("analysis"),
                      vix=state.get("vix"))
    # Phase 2 (§5.3): the decision receipt — the WHY-context that isn't
    # otherwise journaled, frozen at proposal time so a human (or a future
    # session) can reconstruct this firing without re-deriving anything.
    # Additive key, fail-open like the stamp.
    try:
        entry["receipt"] = {
            "underlying": underlying,
            "vix": state.get("vix"),
            "analysis": {k: (state.get("analysis") or {}).get(k)
                         for k in ("uptrend", "fresh_cross", "rsi", "price")},
            "vol_overrides": dict(vol_overrides) if vol_overrides else {},
            "book": "sandbox" if "book" in state else "real",
            "memory_context": p.get("memory_context") or "",
            "skeptic_note": p.get("skeptic_note") or "",
        }
    except Exception:
        pass
    # Phase 6G: the capital layer's strict entry guard — lock the SPAN
    # margin against the Rs.10L account pool, or reject SILENTLY (no
    # journal line, no Discord alert; the manager logs the exhaustion/halt
    # event). Guards only the REAL paper book: a caller-injected `book`
    # (simulator, tests, what-if runs) is its own capital world, so the
    # real account never gates it — and never gets touched by it.
    if "book" not in state:
        from src import portfolio_manager as pm
        allowed, gate_reason = pm.gate_headless_entry(
            entry["short_id"], pm.required_margin_for(p))
        if not allowed:
            return {"proposed": False, "reason": gate_reason, "entry": None}
    journal.log(entry)
    # Auto-approve NEVER applies to an injected book: decide_pending's
    # margin gate only knows the REAL Rs.10L account, so auto-approving a
    # sandboxed (simulator / what-if) proposal would lock real margin from
    # a run that was promised its own capital world (see the gate comment
    # above). Sandbox callers decide their own entries.
    auto_mode = paper_auto_approve_enabled() and "book" not in state
    if auto_mode:
        action_note = ("auto-proposed by the market loop — PAPER_AUTO_APPROVE "
                       f"is ON, so trade id `{entry['short_id']}` is being "
                       "journaled as APPROVED automatically, margin gate "
                       "permitting (paper only; the plan tracker manages "
                       "the exit).")
    else:
        action_note = ("auto-proposed by the market loop and journaled as "
                       f"PENDING_APPROVAL (trade id `{entry['short_id']}`) — "
                       "type `/pending` here for Approve/Reject buttons, or "
                       "run `python3 -m src.options_proposer "
                       "--review-pending` in a terminal (paper only).")
    _notify_discord(_format_proposal_alert(p, action_note=action_note))
    if auto_mode:
        verdict = decide_pending(entry["short_id"], approve=True,
                                 why=AUTO_APPROVE_WHY)
        if verdict["status"] == "approved":
            return {"proposed": True, "reason": "ok (auto-approved)",
                    "entry": verdict["entry"], "auto_approved": True}
        # margin_blocked etc. — the entry stays pending for a human; report
        # honestly instead of pretending the auto-approval happened.
        return {"proposed": True,
                "reason": f"proposed; auto-approval declined "
                          f"({verdict.get('reason', verdict['status'])})",
                "entry": entry, "auto_approved": False}
    return {"proposed": True, "reason": "ok", "entry": entry,
            "auto_approved": False}


def run_session(underlying: str = "NIFTY 50") -> None:
    print(f"Options proposer — {underlying} (paper only)\n")
    result = build_proposal(underlying)
    if result["proposal"] is None:
        print(f"No proposal: {result['reason']}")
        return
    p = result["proposal"]
    # Phase 6C: attach knowledge-graph memory context for the rationale.
    p["memory_context"] = _memory_context_for(_memory_seeds(p))
    # Phase 11: numerical skeptic audit ("" while the model is untrained).
    p["skeptic_note"] = _skeptic_note_for(p)
    for line in _describe(p):
        print(line)
    # Surface the proposal to Discord BEFORE pausing for the decision, so
    # the phone gets the full picture while the terminal waits. Fail-safe:
    # an unreachable Discord never stops the session.
    _notify_discord(_format_proposal_alert(p))
    answer = input("\nTake this spread on paper? [y/N] ").strip().lower()
    decision = "approved" if answer == "y" else "rejected"
    why = input("Why? (one line) ").strip() or "(no reason given)"
    _journal_entry = to_journal_entry(p, decision, why)
    journal.log(_journal_entry)
    # Short follow-up with the outcome (the alert above already carried
    # the full detail); the resolution side is pushed by the API loop
    # when the tracker closes the basket.
    marker = "✅" if decision == "approved" else "❌"
    _notify_discord(f"{marker} **Decision on {p['ticker']} "
                    f"{p['spread']['strategy'].replace('_', ' ')}: "
                    f"{decision.upper()}**\nWhy: {why}")
    if decision == "approved":
        print("\nJournaled as approved — the plan tracker manages the exit "
              "from here (65% profit take / pre-expiry rule). Cash settles "
              "net at the exit.")
        try:
            from src.notifier import fire_broadcast
            s = p["spread"]
            fire_broadcast({
                "event": "opened",
                "ticker": p["ticker"],
                "date": _journal_entry["date"],
                "strategy": s.get("strategy"),
                "short_id": _journal_entry.get("short_id"),
                "lots": p["lots"],
                "lot_size": s.get("lot_size", 75),
                "max_loss": float(s.get("max_loss", 0)) * p["lots"],
                "max_profit": float(s.get("max_profit", 0)) * p["lots"],
                "expiry": s.get("expiry", ""),
                "signal": p.get("signal", ""),
            })
        except Exception as _bcast_err:
            print(f"  (broadcast alert skipped: {_bcast_err})")
    else:
        print("\nJournaled as skipped — the tracker will score the skip.")


def _describe_pending(entry: dict) -> list:
    """Terminal display for one stored pending entry — built entirely from
    the journaled spread payload, no market data fetched."""
    s = entry["spread"]
    net = s["net_credit"] if s.get("net_credit") is not None else s.get("net_debit")
    kind = "credit" if s.get("net_credit") is not None else "debit"
    lots = s.get("lots", 1)
    lines = [
        f"{s['strategy'].replace('_', ' ').title()} on {entry['ticker']} "
        f"(proposed {entry['date']}, expiry {s['expiry']})",
        f"  signal at proposal: {entry.get('signal')}",
        f"  {lots} lot(s) x {s['lot_size']}",
    ]
    for leg in s["legs"]:
        lines.append(f"  {leg['side']:4} {leg['option_type']} {leg['strike']:g} "
                     f"@ Rs.{leg['premium']:,.2f}")
    lines += [
        f"  net {kind} Rs.{net:,.2f}/share  |  "
        f"max loss Rs.{s['max_loss'] * lots:,.0f}  |  "
        f"max profit Rs.{s['max_profit'] * lots:,.0f}",
        "  exits: auto at 65% of max profit, or 2 days before expiry (atomic basket)",
    ]
    return lines


def decide_pending(trade_id: str, approve: bool, why: str = "") -> dict:
    """The two-way Discord bridge's headless twin of review_pending():
    decide ONE stored pending_approval entry, located by its journal
    short_id, with exactly the CLI's semantics —

      approve=True  -> decision "approved" ON PAPER (the plan tracker takes
                       over; NO broker call anywhere, decision #11), and
      approve=False -> decision "rejected" (this codebase's canonical skip).

    Entries the tracker already resolved hypothetically (outcome set) are
    left as-is — no approving with hindsight (decision #31). Fires the same
    fail-safe Discord confirmation the interactive review does.

    Returns {"status": "approved"|"rejected"|"not_found"|"already_resolved",
             "entry": dict-or-None}."""
    entries = journal.read_all()
    target = None
    for e in entries:
        if (e.get("decision") == "pending_approval"
                and e.get("short_id") == trade_id):
            target = e
            break
    if target is None:
        return {"status": "not_found", "entry": None}
    if target.get("outcome"):
        return {"status": "already_resolved", "entry": target}

    if approve:
        # Phase 6J: approval is the moment a trade is ACCEPTED, so the
        # capital layer must grant its margin first (idempotent no-op when
        # the headless gate already locked it at proposal time). A
        # margin-blocked approval leaves the entry pending — nothing is
        # journaled, broadcast, or settled.
        spread = target.get("spread") or {}
        per_lot = (spread.get("margin") or {}).get("total_margin")
        if per_lot is not None:
            from src import portfolio_manager as pm
            required = float(per_lot) * int(spread.get("lots", 1))
            allowed, gate_reason = pm.gate_headless_entry(trade_id, required)
            if not allowed:
                return {"status": "margin_blocked", "entry": target,
                        "reason": gate_reason}

    decision = "approved" if approve else "rejected"
    target["decision"] = decision
    target["why"] = (why or "").strip() or "(no reason given)"
    journal.rewrite_all(entries)
    if not approve:
        # Phase 6G: a human rejection frees the entry's margin lock right
        # away (zero P&L — the trade never happened). Safe no-op if the
        # entry predates the capital layer.
        from src import portfolio_manager as pm
        pm.release_entry(trade_id, 0.0)
    marker = "✅" if approve else "❌"
    strategy = (target.get("spread") or {}).get("strategy", "proposal")
    _notify_discord(f"{marker} **Pending decision on {target['ticker']} "
                    f"{strategy.replace('_', ' ')}: {decision.upper()}**\n"
                    f"Why: {target['why']}")
    if approve:
        try:
            from src.notifier import fire_broadcast
            spread = target.get("spread") or {}
            lots = int(spread.get("lots", 1))
            fire_broadcast({
                "event": "opened",
                "ticker": target.get("ticker", "?"),
                "date": date.today().isoformat(),
                "strategy": spread.get("strategy"),
                "short_id": target.get("short_id"),
                "lots": lots,
                "lot_size": int(spread.get("lot_size", 75)),
                "max_loss": float(spread.get("max_loss", 0)) * lots,
                "max_profit": float(spread.get("max_profit", 0)) * lots,
                "expiry": spread.get("expiry", "?"),
                "signal": target.get("signal", ""),
            })
        except Exception as _bcast_err:
            print(f"  (broadcast alert skipped: {_bcast_err})")
    return {"status": decision, "entry": target}


def review_pending() -> int:
    """Close the market-loop's loop: read the journal for
    decision == "pending_approval" entries (no market data fetched — the
    stored spread payload is the whole proposal) and decide each one:

      y -> decision becomes "approved" ON PAPER: the plan tracker now
           treats it as a held position and net-settles cash at the
           atomic basket exit. NO broker call is made anywhere —
           dhan_client is data-only by hard project rule (decision #11 /
           Phase 13 gate); "execute" in this system means paper.
      n -> decision becomes "rejected" (this codebase's term for a skip,
           so the scorecard/review flows keep seeing it) + your why.

    Entries that already resolved hypothetically (outcome set before you
    decided) are left as-is and reported. Returns how many entries were
    decided."""
    entries = journal.read_all()
    pending = [(i, e) for i, e in enumerate(entries)
               if e.get("decision") == "pending_approval"]
    if not pending:
        print("No pending proposals found.")
        return 0

    decided = 0
    for i, entry in pending:
        print()
        if entry.get("outcome"):
            print(f"(already resolved) {entry['ticker']} "
                  f"{entry['spread']['strategy']} from {entry['date']} — "
                  f"verdict: {entry['outcome'].get('verdict')}. Left as-is.")
            continue
        for line in _describe_pending(entry):
            print(line)
        answer = input("\nTake this spread on paper? [y/N] ").strip().lower()
        decision = "approved" if answer == "y" else "rejected"
        why = input("Why? (one line) ").strip() or "(no reason given)"
        entry["decision"] = decision
        entry["why"] = why
        decided += 1
        marker = "✅" if decision == "approved" else "❌"
        _notify_discord(f"{marker} **Pending decision on {entry['ticker']} "
                        f"{entry['spread']['strategy'].replace('_', ' ')}: "
                        f"{decision.upper()}**\nWhy: {why}")
        if decision == "approved":
            print("  approved on paper — the plan tracker manages the exit "
                  "from here.")
        else:
            print("  skipped — the tracker will score the skip.")

    if decided:
        journal.rewrite_all(entries)
        print(f"\n{decided} pending proposal(s) decided and journaled.")
    return decided


if __name__ == "__main__":
    import sys
    if "--review-pending" in sys.argv:
        review_pending()
    else:
        run_session(sys.argv[1] if len(sys.argv) > 1 else "NIFTY 50")
