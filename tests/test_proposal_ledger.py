"""
The proposal ledger (2026-08-07): one row per evaluation, so the desk's
REFUSALS are queryable instead of being print lines that rotate away.

The reason strings asserted here are the exact ones the live VM logs
carried on 2026-08-06/07 — this file is what stops a reworded refusal
from silently becoming an unclassified row without anyone noticing.

Hermetic: every write goes to tmp_path, no live ledger is touched.
"""
import json

from src import proposal_ledger as PL

STATE = {"vix": 12.0, "expiry": "2026-08-27",
         "analysis": {"price": 24800.0}}


def _rejection(reason):
    return {"proposed": False, "reason": reason, "entry": None}


# ------------------------------------------------------- classification

def test_the_live_refusal_strings_all_classify():
    """Verbatim from the VM's 2026-08-06/07 master_scheduler.log."""
    cases = {
        "max loss Rs.10,166/lot exceeds the Rs.10,000 hard per-trade risk "
        "cap.": "REJECTED_RISK_CAP",
        "max loss Rs.16,608/lot doesn't fit the 10% options risk budget (or "
        "SPAN margin exceeds cash)": "REJECTED_RISK_BUDGET",
        "margin exhaustion: needs Rs.19,686.00 but only Rs.2,957.69 liquid "
        "(Rs.236,466.30 already locked)": "REJECTED_MARGIN",
        "exposure gate: 1 open neutral position(s) on NIFTY BANK already "
        "(`ac895ae4`) — max one per underlying+direction (decision #68)":
            "REJECTED_EXPOSURE",
        "no tradeable quotes at the chosen strikes": "REJECTED_NO_QUOTE",
        "range-bound structure blocked: India VIX unavailable — range-bound "
        "strategies refused (fail safe)": "REJECTED_NO_VIX",
    }
    for reason, fate in cases.items():
        assert PL.classify(_rejection(reason)) == fate, reason


def test_an_unrecognised_refusal_is_never_filed_under_a_familiar_bucket():
    """The visible-failure rule: a reworded gate must show up AS unmapped,
    with its text intact, not be quietly absorbed by the nearest match."""
    r = _rejection("some brand new gate nobody has seen before")
    assert PL.classify(r) == "REJECTED_OTHER"
    row = PL.row_for("NIFTY 50", r)
    assert row["reason"] == "some brand new gate nobody has seen before"


def test_proposed_and_auto_approved_are_different_fates():
    assert PL.classify({"proposed": True, "reason": "ok (auto-approved)",
                        "auto_approved": True}) == "EXECUTED"
    assert PL.classify({"proposed": True, "reason": "ok",
                        "auto_approved": False}) == "PROPOSED_PENDING"


def test_no_market_state_is_its_own_fate():
    assert PL.classify(None) == "NO_MARKET_STATE"


# ------------------------------------------------------------ the row

def test_a_refusal_carries_the_rupees_it_names():
    """A rejected trade has no entry object, so the refusal text is the
    only record of what it would have needed."""
    row = PL.row_for("NIFTY FIN SERVICE", _rejection(
        "margin exhaustion: needs Rs.19,686.00 but only Rs.2,957.69 liquid"),
        state=STATE)
    assert row["needed_rs"] == 19686.0
    assert row["liquid_rs"] == 2957.69
    assert row["vix"] == 12.0 and row["spot"] == 24800.0


def test_a_refusal_never_invents_a_strategy():
    """The proposer never built the structure it was refused for."""
    row = PL.row_for("NIFTY 50", _rejection("no tradeable quotes at the "
                                            "chosen strikes"))
    assert row["strategy"] is None and row["direction"] is None
    assert row["risk_size_rs"] is None


def test_an_executed_row_carries_the_structure_and_the_size():
    entry = {"short_id": "ac895ae4", "shares": 30, "price": 160.4,
             "pattern_tags": ["iron_condor"],
             "risk_levers": {"size": 10000},
             "spread": {"strategy": "iron_condor", "direction": "neutral"}}
    row = PL.row_for("NIFTY BANK", {"proposed": True, "auto_approved": True,
                                    "reason": "ok (auto-approved)",
                                    "entry": entry}, state=STATE)
    assert row["fate"] == "EXECUTED"
    assert row["strategy"] == "iron_condor" and row["direction"] == "neutral"
    assert row["risk_size_rs"] == 10000 and row["short_id"] == "ac895ae4"
    assert row["qty"] == 30 and row["price"] == 160.4


def test_absent_amounts_are_None_not_zero():
    row = PL.row_for("INFY.NS", _rejection("no tradeable quotes"))
    assert row.get("needed_rs") is None and row.get("max_loss_rs") is None


# ------------------------------------------------------------ the file

def test_records_append_and_read_back(tmp_path):
    p = tmp_path / "proposal_ledger.jsonl"
    PL.record("NIFTY 50", _rejection("max loss Rs.10,166/lot exceeds the "
                                     "Rs.10,000 hard per-trade risk cap."),
              state=STATE, path=p)
    PL.record("NIFTY BANK", _rejection("exposure gate: 1 open neutral "
                                       "position(s)"), path=p)
    rows = PL.read_rows(path=p)
    assert [r["fate"] for r in rows] == ["REJECTED_RISK_CAP",
                                         "REJECTED_EXPOSURE"]
    assert json.loads(p.read_text().splitlines()[0])["underlying"] == "NIFTY 50"


def test_a_corrupt_line_never_blinds_the_report(tmp_path):
    p = tmp_path / "l.jsonl"
    PL.record("NIFTY 50", _rejection("no tradeable quotes"), path=p)
    with p.open("a") as fh:
        fh.write("{half written\n")
    PL.record("INFY.NS", _rejection("no tradeable quotes"), path=p)
    assert len(PL.read_rows(path=p)) == 2


def test_recording_never_raises_on_an_unwritable_path(tmp_path):
    """Telemetry hangs off the live loop; it does not get to break it."""
    unwritable = tmp_path / "not_a_dir.txt"
    unwritable.write_text("x")
    assert PL.record("NIFTY 50", _rejection("x"),
                     path=unwritable / "nested.jsonl") is None


def test_the_summary_answers_what_stopped_the_desk(tmp_path):
    p = tmp_path / "l.jsonl"
    for _ in range(3):
        PL.record("NIFTY FIN SERVICE", _rejection(
            "margin exhaustion: needs Rs.19,686.00 but only Rs.2,957.69 "
            "liquid"), path=p)
    PL.record("NIFTY BANK", {"proposed": True, "auto_approved": True,
                             "reason": "ok", "entry": {}}, path=p)
    s = PL.summarise(path=p)
    assert s["rows"] == 4
    assert s["by_fate"]["REJECTED_MARGIN"] == 3
    assert s["by_fate"]["EXECUTED"] == 1
    assert s["margin_named_in_refusals_rs"] == round(19686.0 * 3, 2)
    assert s["by_underlying"]["NIFTY FIN SERVICE"]["REJECTED_MARGIN"] == 3


def test_the_summary_can_be_scoped_to_one_session(tmp_path):
    p = tmp_path / "l.jsonl"
    from datetime import datetime
    thu = datetime(2026, 8, 6, 10, 0, tzinfo=PL.IST)
    fri = datetime(2026, 8, 7, 10, 0, tzinfo=PL.IST)
    PL.record("NIFTY 50", _rejection("no tradeable quotes"), now=thu, path=p)
    PL.record("NIFTY 50", _rejection("no tradeable quotes"), now=fri, path=p)
    assert PL.summarise(path=p, session_date="2026-08-07")["rows"] == 1


def test_an_empty_ledger_says_so_rather_than_rendering_nothing(tmp_path):
    assert PL.render_lines(path=tmp_path / "absent.jsonl") == [
        "proposal ledger: no evaluations recorded"]


def test_the_live_ledger_is_never_written_from_a_test():
    """A test that forgets `path` must be a no-op, not a junk row in the
    production file."""
    assert PL.record("NIFTY 50", _rejection("x")) is None


# ------------------------------------------- the expiry hole (08-11 fix)
# A gate-stage refusal (exposure #68, margin) hands back the BUILT
# proposal, whose expiry lives at proposal["spread"]["expiry"] — there is
# no top-level `expiry` key. Reading the wrong place wrote `expiry: None`,
# and a ghost with no expiry can never find a chain: every NIFTY BANK
# ghost on 2026-08-10 died as `no captured chain for expiry None`.

def _built_proposal(expiry="2026-08-25", spot=57800.0):
    return {"ticker": "NIFTY BANK", "view": "neutral", "lots": 2,
            "spread": {"strategy": "iron_condor", "direction": "neutral",
                       "lot_size": 30, "expiry": expiry, "entry_spot": spot,
                       "net_credit": 120.0, "net_debit": None,
                       "max_loss": 8400.0, "max_profit": 3600.0,
                       "legs": [{"side": "SELL", "option_type": "PE",
                                 "strike": 56600, "premium": 100.0},
                                {"side": "BUY", "option_type": "PE",
                                 "strike": 56200, "premium": 40.0}]}}


def test_a_gate_refusal_carries_the_expiry_off_the_spread():
    from src import options_proposer as op
    facts = op._rejected_facts("NIFTY BANK", {"view": "neutral"},
                               _built_proposal())
    assert facts["expiry"] == "2026-08-25"
    assert facts["spot"] == 57800.0
    assert facts["lots"] == 2 and facts["lot_size"] == 30


def test_the_expiry_reaches_the_ledger_row_for_a_gate_refusal():
    from src import options_proposer as op
    result = {"proposed": False, "entry": None,
              "reason": "exposure gate: 1 open neutral position(s) on "
                        "NIFTY BANK already (`ac895ae4`)",
              "rejected": op._rejected_facts("NIFTY BANK", {"view": "neutral"},
                                             _built_proposal())}
    row = PL.row_for("NIFTY BANK", result)
    assert row["fate"] == "REJECTED_EXPOSURE"
    assert row["expiry"] == "2026-08-25"       # was None before the fix
    assert row["entry_net"] == 120.0           # credit structure
    assert len(row["legs"]) == 2


def test_a_build_stage_refusal_still_carries_its_own_expiry():
    """Those were never broken — build_proposal returns its own expiry,
    which is why the hole looked underlying-specific, not stage-specific."""
    from src import options_proposer as op
    facts = op._rejected_facts("NIFTY 50", {
        "view": "bearish", "expiry": "2026-09-29",
        "rejected_spread": {"strategy": "bear_put_spread", "lot_size": 65,
                            "net_debit": 60.0, "net_credit": None,
                            "legs": [{"side": "BUY", "option_type": "PE",
                                      "strike": 24600, "premium": 100.0}]}})
    assert facts["expiry"] == "2026-09-29"
