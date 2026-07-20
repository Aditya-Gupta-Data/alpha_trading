"""
The scrip-master reconciliation clerk, fully offline (a tiny synthetic
master, injected — never the real 27MB fetch): the three verdicts, the
name-normalizing match that accepts the master's own spellings, the
wanted-list lookup, de-duped review cards, and the honesty rule that a
fetch failure is an OUTAGE and never a pass.
"""
import json

from src.ingestion import scrip_master as SM

HEADER = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,"
          "SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SM_SYMBOL_NAME,SEM_SERIES")


def _master_csv(rows):
    return "\n".join([HEADER] + [",".join(r) for r in rows]) + "\n"


# NSE equities + the two indices whose master spelling differs from ours.
DEFAULT_ROWS = [
    ("NSE", "E", "11536", "TCS", "Tata Consultancy", "TCS", "EQ"),
    ("NSE", "E", "3456", "TMPV", "Tata Motors Pass Veh", "TMPV", "EQ"),
    ("NSE", "I", "13", "NIFTY", "Nifty 50", "NIFTY", "X"),
    ("NSE", "I", "25", "BANKNIFTY", "Nifty Bank", "BANKNIFTY", "X"),
    ("NSE", "E", "18143", "GOLDBEES", "Nippon Gold ETF", "GOLDBEES", "EQ"),
]


def _fetch(rows=None):
    return lambda url: _master_csv(rows if rows is not None else DEFAULT_ROWS)


def test_matching_accepts_the_masters_own_spellings():
    """'NIFTY 50' vs 'Nifty 50' and 'NIFTY BANK' vs 'BANKNIFTY' are the
    SAME instrument — a naive equality check would false-alarm weekly."""
    master = SM.index_master(_master_csv(DEFAULT_ROWS))
    for ticker, sid in (("NIFTY 50", "13"), ("NIFTY BANK", "25")):
        r = SM.check_one(ticker, {"id": sid, "seg": "IDX_I",
                                  "inst": "INDEX"}, master)
        assert r["verdict"] == "ok", r


def test_symbol_mismatch_is_caught():
    """The dangerous case: a LIVE id that now trades as something else —
    nothing crashes, we would simply price the wrong instrument."""
    master = SM.index_master(_master_csv(DEFAULT_ROWS))
    r = SM.check_one("TATAMOTORS.NS", {"id": "3456", "seg": "NSE_EQ",
                                       "inst": "EQUITY"}, master)
    assert r["verdict"] == "symbol_mismatch"
    assert r["master_symbol"] == "TMPV"
    assert "TMPV" in r["detail"]


def test_id_not_found_is_caught():
    master = SM.index_master(_master_csv(DEFAULT_ROWS))
    r = SM.check_one("LTIM.NS", {"id": "999999", "seg": "NSE_EQ",
                                 "inst": "EQUITY"}, master)
    assert r["verdict"] == "id_not_found"


def test_reconcile_counts_and_series_reported():
    id_map = {"TCS.NS": {"id": "11536", "seg": "NSE_EQ", "inst": "EQUITY"},
              "TATAMOTORS.NS": {"id": "3456", "seg": "NSE_EQ",
                                "inst": "EQUITY"},
              "LTIM.NS": {"id": "999999", "seg": "NSE_EQ",
                          "inst": "EQUITY"}}
    rep = SM.reconcile(id_map, SM.index_master(_master_csv(DEFAULT_ROWS)))
    assert rep["counts"] == {"ok": 1, "symbol_mismatch": 1,
                             "id_not_found": 1}
    assert rep["status"] == "verified"
    ok_row = next(r for r in rep["rows"] if r["ticker"] == "TCS.NS")
    assert ok_row["series"] == "EQ"      # a series move stays visible


def test_wanted_lookup_finds_unmapped_symbols():
    """GOLDBEES: the standing example — the flywheel merge is blocked
    until its id is verified, so the clerk answers it without anyone
    hand-searching a 27MB file."""
    master = SM.index_master(_master_csv(DEFAULT_ROWS))
    found = SM.lookup_wanted(["GOLDBEES", "NOSUCHTHING"], master)
    assert found["GOLDBEES"] == [{"id": "18143", "seg": "NSE_EQ",
                                  "symbol": "GOLDBEES",
                                  "name": "GOLDBEES", "series": "EQ"}]
    assert found["NOSUCHTHING"] == []    # honest empty, never a guess


def test_fetch_failure_is_an_outage_never_a_pass(tmp_path):
    """The honesty rule: an unreadable master must NOT look like a clean
    run to anything downstream."""
    def boom(url):
        raise TimeoutError("the read operation timed out")

    rep = SM.run(fetch_fn=boom, id_map={"TCS.NS": {"id": "1",
                                                   "seg": "NSE_EQ",
                                                   "inst": "EQUITY"}},
                 wanted=[], report_path=tmp_path / "r.json",
                 ledger_path=tmp_path / "l.jsonl", notify=False)
    assert rep["status"] == "unavailable" and rep["code"] == "SM-408"
    assert rep["checked"] == 0
    assert "not a pass" in rep["note"].lower()
    assert json.loads((tmp_path / "r.json").read_text())["status"] \
        == "unavailable"


def test_empty_master_is_also_an_outage(tmp_path):
    rep = SM.run(fetch_fn=lambda u: HEADER + "\n", id_map={}, wanted=[],
                 report_path=tmp_path / "r.json", notify=False)
    assert rep["status"] == "unavailable" and rep["code"] == "SM-500"


def test_cards_fire_once_per_problem_and_resend_when_discord_fails(tmp_path):
    id_map = {"TATAMOTORS.NS": {"id": "3456", "seg": "NSE_EQ",
                                "inst": "EQUITY"}}
    ledger = tmp_path / "alerts.jsonl"
    args = dict(fetch_fn=_fetch(), id_map=id_map, wanted=[],
                report_path=tmp_path / "r.json", ledger_path=ledger)
    # A failed send must NOT mark the problem seen.
    def failing(text):
        raise RuntimeError("discord down")

    r0 = SM.run(**args, notify_fn=failing)
    assert r0["announced"] == 0 and not ledger.exists()

    cards = []
    r1 = SM.run(**args, notify_fn=cards.append)
    assert r1["announced"] == 1 and len(cards) == 1
    assert "TATAMOTORS.NS" in cards[0]
    assert "PRICING THE WRONG INSTRUMENT" in cards[0]
    # Known-broken name: reported in the file, but never re-carded.
    r2 = SM.run(**args, notify_fn=cards.append)
    assert r2["announced"] == 0 and len(cards) == 1
    assert len(r2["problems"]) == 1          # still visible in the report


def test_clean_run_fires_nothing(tmp_path):
    cards = []
    rep = SM.run(fetch_fn=_fetch(),
                 id_map={"TCS.NS": {"id": "11536", "seg": "NSE_EQ",
                                    "inst": "EQUITY"}},
                 wanted=[], report_path=tmp_path / "r.json",
                 ledger_path=tmp_path / "l.jsonl", notify_fn=cards.append)
    assert rep["problems"] == [] and cards == []
    assert rep["counts"]["ok"] == 1
