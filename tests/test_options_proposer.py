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
