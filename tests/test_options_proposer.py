"""
Tests for the Phase 5 options proposer (src/options_proposer.py): regime
mapping, expiry/strike selection off a fake Dhan chain, VIX gating, the
options risk budget, and the journal entry contract the tracker resolves.

Offline — analysis, VIX, expiry, chain, and portfolio are all injected;
no Dhan call, no real journal write.

Run either of these from the project folder:
    python tests/test_options_proposer.py    (simple, no extra installs)
    python -m pytest tests/                   (if you have pytest)
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import options_proposer as op
import src.plan_tracker as plan_tracker

# The #84 hard rupee cap is pinned wide here: these fixtures' synthetic
# chains price a condor's max_loss at ~10.7k/lot, and the suite's job is
# the PIPELINE's mechanics — the cap's own binding behavior is tested
# where it lives (test_equity_desk / the cap refusal below uses op's
# module value directly).
op.MAX_RISK_PER_TRADE_RS = 1_000_000.0


def make_analysis(uptrend=True, fresh_cross=False, rsi=50.0, price=25000.0):
    return {"ticker": "NIFTY 50", "uptrend": uptrend,
            "fresh_cross": fresh_cross, "rsi": rsi, "price": price}


def make_chain(spot=25000.0, step=50.0, span=20, base_premium=100.0):
    """A fake Dhan chain: strikes around spot; premiums fall linearly as
    strikes move OTM (floor 5), keyed the way Dhan keys them."""
    oc = {}
    for i in range(-span, span + 1):
        strike = spot + i * step
        ce = max(5.0, base_premium - i * (base_premium / span) * 0.9)
        pe = max(5.0, base_premium + i * (base_premium / span) * 0.9)
        oc[f"{strike:.6f}"] = {"ce": {"last_price": round(ce, 2)},
                               "pe": {"last_price": round(pe, 2)}}
    return {"last_price": spot, "oc": oc}


FUTURE_EXPIRY = (date.today() + timedelta(days=14)).isoformat()
BIG_BOOK = {"cash": 2_000_000.0, "holdings": {}}


def build(view_analysis, vix=13.0, book=None, chain=None):
    return op.build_proposal(
        "NIFTY 50", analysis=view_analysis, vix=vix,
        expiry=FUTURE_EXPIRY, chain=chain or make_chain(),
        book=book or dict(BIG_BOOK), prices={})


# ------------------------------------------------------------ regime map

def test_market_view_mapping():
    assert op.market_view(make_analysis(uptrend=True, rsi=25)) == "bullish"
    assert op.market_view(make_analysis(uptrend=True, fresh_cross=True)) == "bullish"
    assert op.market_view(make_analysis(uptrend=False)) == "bearish"
    assert op.market_view(make_analysis(uptrend=False, fresh_cross=True)) == "bearish"
    assert op.market_view(make_analysis(uptrend=True, rsi=55)) == "neutral"
    assert op.market_view(make_analysis(uptrend=True, rsi=None)) == "neutral"


def test_pick_expiry_respects_min_days():
    today = date(2026, 7, 6)
    soon = "2026-07-09"      # 3 days — the 2-day exit rule would fire at once
    good = "2026-07-16"      # 10 days
    later = "2026-07-30"
    assert op.pick_expiry([soon, good, later], today=today) == good
    assert op.pick_expiry([soon], today=today) is None
    assert op.pick_expiry([], today=today) is None
    assert op.pick_expiry(["garbage", good], today=today) == good


# ------------------------------------------------------- construction

def test_bullish_view_builds_bull_call_spread_at_atm():
    r = build(make_analysis(uptrend=True, rsi=25))
    p = r["proposal"]
    assert p is not None and p["spread"]["strategy"] == "bull_call_spread"
    legs = {(l["side"], l["strike"]) for l in p["spread"]["legs"]}
    assert legs == {("BUY", 25000.0), ("SELL", 25200.0)}  # ATM / ATM+4*50


def test_bearish_view_builds_bear_put_spread():
    r = build(make_analysis(uptrend=False))
    p = r["proposal"]
    assert p is not None and p["spread"]["strategy"] == "bear_put_spread"
    legs = {(l["side"], l["strike"]) for l in p["spread"]["legs"]}
    assert legs == {("BUY", 25000.0), ("SELL", 24800.0)}


def test_neutral_view_builds_iron_condor_in_calm_vix():
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    p = r["proposal"]
    assert p is not None and p["spread"]["strategy"] == "iron_condor"
    strikes = {(l["side"], l["option_type"], l["strike"]) for l in p["spread"]["legs"]}
    # shorts ~2% OTM (24500 P / 25500 C), wings 4*50=200 beyond:
    assert strikes == {("SELL", "PE", 24500.0), ("BUY", "PE", 24300.0),
                       ("SELL", "CE", 25500.0), ("BUY", "CE", 25700.0)}


def test_neutral_view_blocked_when_vix_high_or_unknown():
    from unittest import mock
    r = build(make_analysis(uptrend=True, rsi=55), vix=17.5)
    assert r["proposal"] is None and "blocked" in r["reason"]
    # "VIX unavailable" means get_india_vix() returns None — force that
    # deterministically instead of depending on the network being down
    # (passing vix=None makes build_proposal go fetch the live VIX).
    with mock.patch.object(op, "get_india_vix", return_value=None):
        r2 = build(make_analysis(uptrend=True, rsi=55), vix=None)
    assert r2["proposal"] is None and "blocked" in r2["reason"]
    # ...while a directional view at the same VIX still proposes:
    r3 = build(make_analysis(uptrend=True, rsi=25), vix=17.5)
    assert r3["proposal"] is not None


def test_dead_strike_quote_refuses_to_build():
    chain = make_chain()
    chain["oc"][f"{25200.0:.6f}"]["ce"]["last_price"] = 0  # untradeable leg
    r = build(make_analysis(uptrend=True, rsi=25), chain=chain)
    assert r["proposal"] is None and "quotes" in r["reason"]


# ------------------------------------------- honest fills (decision #70)

def add_bid_ask(chain, spread_pct=0.02):
    """Give every strike a symmetric bid/ask around its last_price."""
    for node in chain["oc"].values():
        for leg in node.values():
            ltp = leg["last_price"]
            leg["top_bid_price"] = round(ltp * (1 - spread_pct), 2)
            leg["top_ask_price"] = round(ltp * (1 + spread_pct), 2)
    return chain


def test_quoted_chain_fills_buy_at_ask_and_sell_at_bid():
    chain = add_bid_ask(make_chain())
    r = build(make_analysis(uptrend=True, rsi=25), chain=chain)
    legs = {l["side"]: l for l in r["proposal"]["spread"]["legs"]}
    buy_node = chain["oc"][f"{25000.0:.6f}"]["ce"]
    sell_node = chain["oc"][f"{25200.0:.6f}"]["ce"]
    assert legs["BUY"]["premium"] == buy_node["top_ask_price"]
    assert legs["SELL"]["premium"] == sell_node["top_bid_price"]
    assert all(l["fill_basis"] == "quoted"
               for l in r["proposal"]["spread"]["legs"])


def test_ltp_only_chain_is_byte_identical_and_flagged_ltp():
    r = build(make_analysis(uptrend=True, rsi=25))  # make_chain has no bid/ask
    legs = {l["side"]: l for l in r["proposal"]["spread"]["legs"]}
    chain = make_chain()
    assert legs["BUY"]["premium"] == chain["oc"][f"{25000.0:.6f}"]["ce"]["last_price"]
    assert all(l["fill_basis"] == "ltp"
               for l in r["proposal"]["spread"]["legs"])


def test_stale_quote_far_from_ltp_falls_back_to_ltp():
    chain = add_bid_ask(make_chain())
    node = chain["oc"][f"{25000.0:.6f}"]["ce"]
    node["top_ask_price"] = node["last_price"] * 2.0  # crossed/stale book
    r = build(make_analysis(uptrend=True, rsi=25), chain=chain)
    legs = {l["side"]: l for l in r["proposal"]["spread"]["legs"]}
    assert legs["BUY"]["premium"] == node["last_price"]
    assert legs["BUY"]["fill_basis"] == "ltp"
    assert legs["SELL"]["fill_basis"] == "quoted"  # untouched leg still quoted


def test_quoted_entry_legs_skip_the_entry_slippage_ladder():
    spread = {"lot_size": 65, "lots": 1, "entry_spot": 25000.0,
              "legs": [
                  {"side": "BUY", "option_type": "CE", "strike": 25000.0,
                   "premium": 100.0, "fill_basis": "quoted"},
                  {"side": "SELL", "option_type": "CE", "strike": 25200.0,
                   "premium": 60.0, "fill_basis": "quoted"}]}
    legacy = {**spread, "legs": [dict(l, fill_basis="ltp")
                                 for l in spread["legs"]]}
    _, slip_quoted = plan_tracker._spread_exit_costs(spread, 25100.0, 0.5)
    _, slip_legacy = plan_tracker._spread_exit_costs(legacy, 25100.0, 0.5)
    # quoted fills already paid the spread at entry -> strictly less ladder
    assert slip_quoted < slip_legacy
    # exit side is still charged: quoted slippage stays positive
    assert slip_quoted > 0
    # and a legs dict WITHOUT the field (pre-#70 journal rows) behaves
    # exactly like "ltp" — old entries never change economics
    old = {**spread, "legs": [{k: v for k, v in l.items()
                               if k != "fill_basis"} for l in spread["legs"]]}
    _, slip_old = plan_tracker._spread_exit_costs(old, 25100.0, 0.5)
    assert slip_old == slip_legacy


# ------------------------------------------------------------- sizing

def test_sizing_uses_options_risk_budget():
    # On the default Rs.1,00,000 book the equity budget (1%) would refuse
    # any spread; the 10% options budget makes a sub-Rs.10k-max-loss
    # condor affordable at exactly 1 lot. (Richer premiums than the
    # default fixture so the condor credit is realistic for 2% OTM.)
    small_book = {"cash": 100_000.0, "holdings": {}}
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0, book=small_book,
              chain=make_chain(base_premium=200.0))
    p = r["proposal"]
    assert p is not None
    assert p["lots"] == 1 and p["spread"]["lots"] == 1
    assert p["spread"]["max_loss"] <= 100_000 * op.OPTIONS_RISK_PER_TRADE_PCT / 100


def test_unaffordable_spread_returns_reason_not_crash():
    tiny_book = {"cash": 5_000.0, "holdings": {}}
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0, book=tiny_book)
    assert r["proposal"] is None and "risk budget" in r["reason"]


# ----------------------------------------------------- journal contract

def test_journal_entry_is_tracker_resolvable():
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    entry = op.to_journal_entry(r["proposal"], "approved", "range week expected")
    assert entry["short_id"] and entry["action"] == "SPREAD"
    assert entry["decision"] == "approved" and entry["outcome"] is None
    assert entry["pattern_tags"] == ["iron_condor"]
    s = entry["spread"]
    assert s["expiry"] == FUTURE_EXPIRY and s["lots"] >= 1
    assert s["entry_spot"] == 25000.0 and len(s["legs"]) == 4
    # The exact gate the tracker sweep uses:
    assert plan_tracker._spread_trackable(entry)
    # Equity sweep must NOT pick it up (plan is None):
    assert not plan_tracker._trackable(entry)


# ------------------------------------------------- discord surfacing

def test_proposal_alert_formatting():
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    text = op._format_proposal_alert(r["proposal"])
    assert "🚨 **PROPOSAL ALERT: Iron Condor**" in text
    assert "**Market Regime**" in text and "Neutral" in text and "13.00" in text
    # Legs live inside a code block, one per leg:
    block = text.split("```")[1]
    assert block.count("SELL") == 2 and block.count("BUY") == 2
    assert "PE 24500" in block and "CE 25700" in block
    assert "**Economics**" in text and "Net Credit" in text
    assert "Max Loss Rs." in text and "SPAN Margin Rs." in text
    assert "**Action Required**" in text and "human-in-the-loop" in text
    # Never exceeds Discord's hard message cap:
    from src.discord_client import DISCORD_MESSAGE_LIMIT
    assert len(text) <= DISCORD_MESSAGE_LIMIT


def test_memory_block_absent_by_default():
    """No memory_context -> the alert carries no 🧠 Memory block."""
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    assert "🧠" not in op._format_proposal_alert(r["proposal"])


def test_memory_block_rendered_when_present():
    """Phase 6C: a proposal carrying memory_context shows it in the alert."""
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    p = dict(r["proposal"], memory_context="NIFTY 50 —led to→ IT_STRENGTH "
                                           "(confidence 0.90, 1 hop)")
    text = op._format_proposal_alert(p)
    assert "🧠 **Memory (linked patterns)**" in text
    assert "IT_STRENGTH" in text


def test_memory_context_for_uses_injected_engine():
    class FakeEngine:
        def summarize_context(self, node, max_hops=2):
            return f"{node} —linked→ THEME (confidence 0.80, 1 hop)"
    out = op._memory_context_for("NIFTY 50", engine=FakeEngine())
    assert "THEME" in out and "NIFTY 50" in out


def test_memory_context_for_is_failsafe():
    class BoomEngine:
        def summarize_context(self, node, max_hops=2):
            raise RuntimeError("graph unavailable")
    # Never propagates — returns "" so the proposal path is never blocked.
    assert op._memory_context_for("NIFTY 50", engine=BoomEngine()) == ""


def test_skeptic_warning_rendered_in_alert_when_present():
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    p = dict(r["proposal"],
             skeptic_note="⚠️ **Skeptic Agent Warning**: modeled win "
                          "probability 20% — advisory only.")
    text = op._format_proposal_alert(p)
    assert "⚠️ **Skeptic Agent Warning**" in text
    assert "20%" in text
    # ...and absent by default:
    assert "Skeptic" not in op._format_proposal_alert(r["proposal"])


def test_skeptic_note_merges_graph_and_market_data():
    """The Step 3 plumbing, fully offline: a real in-memory GraphEngine
    seeded with edges for the proposal's seeds (ticker + strategy), a spy
    auditor capturing what the proposer hands over — proving graph edges
    AND the proposal's numbers arrive merged, with no network anywhere."""
    from src import brain_map
    from src.graph_engine import GraphEngine, add_edge, ensure_schema

    conn = brain_map.connect(":memory:")
    ensure_schema(conn)
    add_edge(conn, "NIFTY 50", "PRECEDES", "it_strength", 0.8)
    add_edge(conn, "iron_condor", "RESULTS_IN", "loss", 0.9, context="VIX > 20")
    engine = GraphEngine(conn=conn)

    captured = {}

    class SpyAuditor:
        def audit(self, proposal, graph_context=None, memory_stats=None):
            captured["proposal"] = proposal
            captured["edges"] = graph_context
            return {"probability": 0.2, "warn": True, "features": []}

    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)  # iron condor
    note = op._skeptic_note_for(r["proposal"], auditor=SpyAuditor(),
                                engine=engine)
    assert "⚠️ **Skeptic Agent Warning**" in note and "20%" in note
    # Both seed families reached the auditor: the ticker edge AND the
    # strategy-keyed causal edge, de-duplicated.
    sources = {e["source"] for e in captured["edges"]}
    assert sources == {"NIFTY 50", "iron_condor"}
    # The proposal's market numbers rode along untouched.
    assert captured["proposal"]["vix"] == 13.0
    assert captured["proposal"]["spread"]["strategy"] == "iron_condor"


def test_skeptic_note_empty_on_abstain_and_failsafe_on_crash():
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)

    class AbstainingAuditor:
        def audit(self, proposal, graph_context=None, memory_stats=None):
            return {"probability": None, "warn": False, "features": []}

    class BoomAuditor:
        def audit(self, proposal, graph_context=None, memory_stats=None):
            raise RuntimeError("model exploded")

    class EmptyEngine:
        def get_relevant_context(self, node, max_hops=2):
            return []

    assert op._skeptic_note_for(r["proposal"], auditor=AbstainingAuditor(),
                                engine=EmptyEngine()) == ""
    assert op._skeptic_note_for(r["proposal"], auditor=BoomAuditor(),
                                engine=EmptyEngine()) == ""


def test_untrained_real_auditor_stays_silent_end_to_end():
    """The scaffolding default with the REAL auditor class: no trained
    model file -> abstain -> no warning text, no exception."""
    from src.skeptic_agent import RandomForestAuditor

    class EmptyEngine:
        def get_relevant_context(self, node, max_hops=2):
            return []

    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    auditor = RandomForestAuditor(model_path="/nonexistent/skeptic.pkl")
    assert op._skeptic_note_for(r["proposal"], auditor=auditor,
                                engine=EmptyEngine()) == ""


def test_session_sends_alert_before_prompt_and_survives_discord_down():
    from unittest import mock
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    order = []

    def fake_notify(text):
        order.append(("discord", text.split("**")[1] if "**" in text else text))
        return False  # Discord unreachable — must not break anything

    def fake_input(prompt=""):
        order.append(("prompt", prompt))
        return "y" if "spread" in prompt.lower() else "test reason"

    with mock.patch.object(op, "build_proposal", return_value=r), \
         mock.patch.object(op, "_notify_discord", side_effect=fake_notify) as notif, \
         mock.patch.object(op.journal, "log") as mock_log, \
         mock.patch("builtins.input", side_effect=fake_input):
        op.run_session("NIFTY 50")

    # Alert fired BEFORE the y/n prompt; decision follow-up after; and the
    # journal write happened despite Discord returning False both times:
    kinds = [k for k, _ in order]
    assert kinds.index("discord") < kinds.index("prompt")
    assert notif.call_count == 2
    assert "PROPOSAL ALERT" in notif.call_args_list[0].args[0]
    assert "APPROVED" in notif.call_args_list[1].args[0]
    mock_log.assert_called_once()
    assert mock_log.call_args.args[0]["decision"] == "approved"


def test_notify_discord_swallows_hard_exceptions():
    from unittest import mock
    with mock.patch("src.notifier.send_discord_message",
                    side_effect=RuntimeError("event loop exploded")):
        assert op._notify_discord("boom") is False  # never raises


# ----------------------------------------------------- pending review

def make_pending_entry(short_id="pend0001", outcome=None):
    r = build(make_analysis(uptrend=True, rsi=55), vix=13.0)
    entry = op.to_journal_entry(
        r["proposal"], "pending_approval",
        "(headless proposal — auto-generated by the market loop, awaiting "
        "human decision)")
    entry["short_id"] = short_id
    entry["outcome"] = outcome
    return entry


def _run_review(entries, answers):
    """review_pending() with journal + input + Discord mocked. Returns
    (decided_count, rewritten_entries, notify_mock, build_mock)."""
    from unittest import mock
    rewritten = {}
    with mock.patch.object(op.journal, "read_all", return_value=entries), \
         mock.patch.object(op.journal, "rewrite_all",
                           side_effect=lambda e: rewritten.update(done=e)) as rw, \
         mock.patch.object(op, "_notify_discord", return_value=True) as notif, \
         mock.patch.object(op, "build_proposal",
                           side_effect=AssertionError(
                               "review mode must NEVER fetch market data")) as bp, \
         mock.patch("builtins.input", side_effect=answers):
        decided = op.review_pending()
    return decided, rewritten.get("done"), notif, rw


def test_review_pending_approves_and_updates_journal():
    entry = make_pending_entry()
    decided, rewritten, notif, _ = _run_review(
        [entry], ["y", "range week confirmed"])
    assert decided == 1
    assert rewritten[0]["decision"] == "approved"
    assert rewritten[0]["why"] == "range week confirmed"
    assert rewritten[0]["short_id"] == "pend0001"   # same entry, updated in place
    # Now a REAL paper position for the tracker (not hypothetical):
    import src.plan_tracker as pt
    assert pt._spread_trackable(rewritten[0])
    # Discord follow-up announced the approval:
    assert "APPROVED" in notif.call_args.args[0]


def test_review_pending_rejects_with_reason():
    entry = make_pending_entry()
    decided, rewritten, notif, _ = _run_review(
        [entry], ["n", "VIX creeping up, not worth it"])
    assert decided == 1
    assert rewritten[0]["decision"] == "rejected"   # codebase's term for a skip
    assert rewritten[0]["why"] == "VIX creeping up, not worth it"
    assert "REJECTED" in notif.call_args.args[0]


def test_review_pending_with_nothing_pending():
    from unittest import mock
    approved = make_pending_entry()
    approved["decision"] = "approved"
    with mock.patch.object(op.journal, "read_all", return_value=[approved]), \
         mock.patch.object(op.journal, "rewrite_all") as rw:
        assert op.review_pending() == 0
    rw.assert_not_called()                          # nothing touched


def test_review_pending_leaves_already_resolved_entries_alone():
    # A pending entry the tracker already resolved hypothetically must not
    # be decidable after the fact (that would be approving with hindsight):
    resolved = make_pending_entry(outcome={"verdict": "MISSED GAIN — it "
                                           "reached 65% without you"})
    decided, rewritten, _, rw = _run_review([resolved], [])  # no input consumed
    assert decided == 0
    rw.assert_not_called()
    assert resolved["decision"] == "pending_approval"  # untouched


def test_missing_data_degrades_with_reasons():
    from unittest import mock
    # No price history for the underlying:
    with mock.patch.object(op, "analyze", return_value=None):
        r = op.build_proposal("NIFTY 50", vix=13.0, expiry=FUTURE_EXPIRY,
                              chain=make_chain(), book=dict(BIG_BOOK), prices={})
    assert r["proposal"] is None and "history" in r["reason"]
    # Empty option chain:
    r2 = op.build_proposal("NIFTY 50", analysis=make_analysis(),
                           vix=13.0, expiry=FUTURE_EXPIRY,
                           chain={"last_price": 25000.0, "oc": {}},
                           book=dict(BIG_BOOK), prices={})
    assert r2["proposal"] is None and "chain" in r2["reason"]
    # No usable expiry from the exchange:
    with mock.patch.object(op, "get_expiry_list", return_value=[]):
        r3 = op.build_proposal("NIFTY 50", analysis=make_analysis(uptrend=True, rsi=55),
                               vix=13.0, expiry=None,
                               chain=make_chain(), book=dict(BIG_BOOK), prices={})
    assert r3["proposal"] is None and "expiry" in r3["reason"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}  {e}")
    print(f"\n{passed}/{len(tests)} tests passed.")


# ============ G3 diversity wiring (2026-08-05) ==========================
#
# 19 of 19 resolved trades were bear_put_spread; bull calls, condors and
# butterflies had fired ZERO times. The diagnostic falsified the obvious
# suspect (VIX never once exceeded the 16 gate — 12 readings, 12.00-14.16)
# and found the cause in market_view: `if not analysis["uptrend"]: return
# "bearish"` ran first and unconditionally, and "neutral" sat at the END of
# the cascade, reachable only when uptrend was TRUE. RANGE was subordinated
# to DIRECTION, so a sideways market below its 200-SMA — exactly when a
# condor is right — could not be seen at all.

def graded(fast_pct, slow_pct, rsi=50.0, fresh_cross=False, price=25000.0):
    """An analyze() dict carrying the SMA DISTANCES the live read now
    returns. `uptrend` is derived so the fixture can never disagree with
    itself."""
    return {"ticker": "NIFTY 50", "uptrend": slow_pct > 0,
            "fresh_cross": fresh_cross, "rsi": rsi, "price": price,
            "sma_fast_distance_pct": fast_pct,
            "sma_slow_distance_pct": slow_pct}


def test_a_flat_market_BELOW_its_200sma_is_now_neutral_not_bearish():
    """THE headline fix. NIFTY BANK's real state on 2026-08-05: spot
    hugging both averages, sma50 1.05% under sma200. The old code called
    this 'bearish' 77 sessions out of 90."""
    assert op.market_view(graded(fast_pct=-0.6, slow_pct=-1.05)) == "neutral"
    assert op.market_view(graded(fast_pct=+0.4, slow_pct=-0.9)) == "neutral"


def test_a_flat_market_ABOVE_its_200sma_is_also_neutral():
    """Symmetry: range is judged on distance, not on which side."""
    assert op.market_view(graded(fast_pct=0.7, slow_pct=1.1)) == "neutral"


def test_a_flat_market_below_the_200sma_ROUTES_TO_IRON_CONDOR():
    """End to end through build_proposal — the structure, not just the
    label. This is the trade the engine could not previously make."""
    res = build(graded(fast_pct=-0.6, slow_pct=-1.05), vix=13.5)
    assert res["view"] == "neutral"
    assert res["proposal"] is not None, res["reason"]
    assert res["proposal"]["spread"]["strategy"] == "iron_condor"


def test_a_real_downtrend_is_still_bearish():
    """The gate got SIGHTED, not looser. A genuine collapse must still
    route to the bear put spread."""
    assert op.market_view(graded(fast_pct=-4.0, slow_pct=-8.0)) == "bearish"
    res = build(graded(fast_pct=-4.0, slow_pct=-8.0), vix=13.0)
    assert res["proposal"]["spread"]["strategy"] == "bear_put_spread"


def test_a_real_uptrend_is_still_bullish():
    assert op.market_view(graded(fast_pct=3.0, slow_pct=6.0)) == "bullish"
    res = build(graded(fast_pct=3.0, slow_pct=6.0), vix=12.0)
    assert res["proposal"]["spread"]["strategy"] == "bull_call_spread"


def test_mixed_sign_averages_are_the_classifiers_own_range_read():
    """spot above one average and below the other IS range-bound —
    trade_planner.classify_trend has always said so, and nothing consumed
    it until now."""
    assert op.market_view(graded(fast_pct=2.5, slow_pct=-2.5)) == "neutral"


# ------------------------------------------------ mean reversion (item 4)

def test_oversold_in_a_MILD_downtrend_now_routes_to_a_bull_call():
    """RSI 25 below the 200-SMA used to be UNREACHABLE: the `uptrend`
    branch returned bearish before RSI was ever consulted."""
    a = graded(fast_pct=-1.7, slow_pct=-1.8, rsi=25.0)
    assert op.market_view(a) == "bullish"
    res = build(a, vix=12.0)
    assert res["proposal"]["spread"]["strategy"] == "bull_call_spread"


def test_the_mean_reversion_window_is_narrow_and_that_is_documented():
    """KNOWN LIMITATION, pinned so it is a decision and not a surprise.
    Mean reversion needs a read that is directional (outside the 1.5% flat
    band) but not `strong_bearish` (trade_planner.STRONG_TREND_PCT = 2.0).
    That leaves only ~0.5pp of slow-SMA distance where an oversold bounce
    can fire. Widening it means moving STRONG_TREND_PCT, which is shared
    with the planner's own matrix — an owner call, not a wiring change."""
    from src.trade_planner import STRONG_TREND_PCT
    assert op.FLAT_BAND_PCT == 1.5 and STRONG_TREND_PCT == 2.0
    assert op.market_view(graded(-1.4, -1.4, rsi=25.0)) == "neutral"      # flat
    assert op.market_view(graded(-1.7, -1.8, rsi=25.0)) == "bullish"      # window
    assert op.market_view(graded(-1.0, -3.0, rsi=25.0)) == "bearish"      # strong


def test_oversold_in_a_STRONG_downtrend_stays_bearish():
    """Deliberately NOT a falling knife. The graded read is exactly the
    distinction the old binary `uptrend` bit could not express."""
    a = graded(fast_pct=-5.0, slow_pct=-9.0, rsi=22.0)
    assert op.market_view(a) == "bearish"


def test_a_fresh_cross_still_leads_when_the_trend_agrees():
    a = graded(fast_pct=1.0, slow_pct=2.5, fresh_cross=True, rsi=60.0)
    assert op.market_view(a) == "bullish"


# ------------------------------------------------ iron butterfly (item 3)

def test_high_IV_neutral_routes_to_IRON_BUTTERFLY():
    """construct_iron_butterfly was fully implemented, tested, and callable
    from NOWHERE — no threshold could ever have fired it."""
    res = build(graded(fast_pct=-0.6, slow_pct=-1.05), vix=15.2)
    assert res["view"] == "neutral"
    assert res["proposal"] is not None, res["reason"]
    spread = res["proposal"]["spread"]
    assert spread["strategy"] == "iron_butterfly"
    # the body is ATM on BOTH sides, wings equidistant
    sells = [l for l in spread["legs"] if l["side"] == "SELL"]
    buys = [l for l in spread["legs"] if l["side"] == "BUY"]
    assert len({l["strike"] for l in sells}) == 1          # one body strike
    assert {l["option_type"] for l in sells} == {"CE", "PE"}
    body = sells[0]["strike"]
    assert sorted(l["strike"] for l in buys) == [
        body - op.WING_STEPS * 50.0, body + op.WING_STEPS * 50.0]


def test_the_butterfly_takes_only_the_UPPER_HALF_of_the_tradeable_band():
    """(13, 16] is tradeable; >= 14.5 is its upper half. Below that a
    condor's wider OTM shorts are the better range structure."""
    assert op.BUTTERFLY_MIN_VIX == 14.5
    flat = graded(fast_pct=-0.6, slow_pct=-1.05)
    assert build(flat, vix=14.4)["proposal"]["spread"]["strategy"] == "iron_condor"
    assert build(flat, vix=14.5)["proposal"]["spread"]["strategy"] == "iron_butterfly"


def test_the_hard_VIX_16_gate_still_blocks_BOTH_range_structures():
    """The gate that never actually fired in production must still work.
    Neither credit structure may pass it."""
    flat = graded(fast_pct=-0.6, slow_pct=-1.05)
    for vix in (16.5, 25.0):
        res = build(flat, vix=vix)
        assert res["proposal"] is None
        assert "range-bound structure blocked" in res["reason"]


def test_an_unknown_vix_refuses_both_range_structures_fail_safe():
    res = build(graded(fast_pct=-0.6, slow_pct=-1.05), vix=None)
    assert res["proposal"] is None
    assert "range-bound structure blocked" in res["reason"]


# ------------------------------------------------ back-compat guard

def test_a_legacy_analysis_dict_behaves_EXACTLY_as_before():
    """Any caller or fixture without the SMA distances — including the
    19 trades already in the journal — must route identically."""
    assert op.market_view(make_analysis(uptrend=True, rsi=25)) == "bullish"
    assert op.market_view(make_analysis(uptrend=True, fresh_cross=True)) == "bullish"
    assert op.market_view(make_analysis(uptrend=False)) == "bearish"
    assert op.market_view(make_analysis(uptrend=False, fresh_cross=True)) == "bearish"
    assert op.market_view(make_analysis(uptrend=True, rsi=55)) == "neutral"
    assert op.market_view(make_analysis(uptrend=True, rsi=None)) == "neutral"


def test_the_live_read_now_carries_the_graded_inputs():
    """analyze() must actually emit what market_view now consumes, or the
    live path silently falls back to the legacy branch forever."""
    import inspect
    from src import suggestions
    src = inspect.getsource(suggestions.analyze)
    assert "sma_fast_distance_pct" in src and "sma_slow_distance_pct" in src
    from src import simulator
    sim = inspect.getsource(simulator.analysis_from_closes)
    assert "sma_fast_distance_pct" in sim      # replay must match live
