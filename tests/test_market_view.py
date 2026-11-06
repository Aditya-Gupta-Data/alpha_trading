"""#183 — the Overview's market read: data.market_view + market_highlights, hermetic."""
import json
import sqlite3

from src.dashboard import data as d


def _db(tmp_path):
    p = tmp_path / "brain_map.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE daily_context (date TEXT PRIMARY KEY, vix REAL, vix_band TEXT, macro_source TEXT,
            macro_nifty_short REAL, macro_nifty_medium REAL, macro_bank_short REAL, macro_bank_medium REAL,
            fii_net REAL, dii_net REAL);
        CREATE TABLE dealer_exposure_daily (date TEXT, underlying TEXT, dealer TEXT, spot REAL,
            regime_at_spot TEXT, zero_gamma REAL, call_wall REAL, put_wall REAL);
        CREATE TABLE margin_locks (journal_ref TEXT, margin_rs REAL, released_at TEXT);
        CREATE TABLE paper_margin_locks (account_id TEXT, journal_ref TEXT, margin_rs REAL, released_at TEXT);
    """)
    c.executemany("INSERT INTO daily_context VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("2026-11-05", 15.62, "mid", "mixed", -0.3, 1.0, -0.5, 1.0, 14974.68, -10444.54),
        ("2026-11-04", 15.27, "mid", "mixed", -0.2, 0.4, -0.4, 0.6, 7284.03, -3712.23),
        ("2026-11-01", None, None, "mixed", -1.0, 1.0, None, None, None, None),   # weekend: no flows
        ("2026-10-30", 15.45, "mid", "mixed", -1.0, 1.0, None, None, 3598.54, -817.51),
        ("2026-10-29", 15.20, "mid", "mixed", -1.0, 1.0, None, None, -943.6, 1924.41),
    ])
    c.executemany("INSERT INTO dealer_exposure_daily VALUES (?,?,?,?,?,?,?,?)", [
        ("2026-11-05", "NIFTY 50", "non_client", 24536.6, "positive", 25081.91, 25000.0, 24500.0),
        ("2026-11-05", "TCS.NS", "non_client", 2495.7, "negative", None, 2500.0, 2500.0),
        ("2026-11-05", "TITAN.NS", "non_client", 4834.6, "negative", None, 5000.0, 4800.0),
        ("2026-11-05", "TITAN.NS", "client", 4834.6, "positive", None, 5000.0, 4800.0),   # not the dealer side
        ("2026-11-04", "NIFTY 50", "non_client", 24400.0, "negative", None, 24800.0, 24000.0),
    ])
    c.executemany("INSERT INTO margin_locks VALUES (?,?,?)",
                  [("bear0001", 9000.0, None), ("bear0002", 9000.0, None), ("gone0001", 1.0, "2026-11-01"),
                   ("eqd:dar00001", 5000.0, None)])
    c.executemany("INSERT INTO paper_margin_locks VALUES (?,?,?,?)",
                  [("PAPER_2L", "bear0001", 3000.0, None),               # same trade mirrored: counted once
                   ("PAPER_2L_LIVE", "ironfly1", 3000.0, None),
                   ("PAPER_SHADOW_LEARNER", "bull9999", 3000.0, None)])  # the learner is not the book
    c.commit()
    c.close()
    rows = [
        {"short_id": "bear0001", "ticker": "NIFTY 50", "spread": {"strategy": "bear_put_spread"}},
        {"short_id": "bear0002", "ticker": "NIFTY BANK", "spread": {"strategy": "bear_put_spread"}},
        {"short_id": "ironfly1", "ticker": "INFY.NS", "spread": {"strategy": "iron_butterfly"}},
        {"short_id": "bull9999", "ticker": "TCS.NS", "spread": {"strategy": "long_call"}},
        {"short_id": "r1", "ticker": "TCS.NS", "date": "2026-11-06", "created_at": "2026-11-06T09:40:00+05:30",
         "regime_read": {"view": "bearish", "grade": "bearish", "rsi": 40.0, "vix": 16.2}},
        {"short_id": "r2", "ticker": "TCS.NS", "date": "2026-11-06", "created_at": "2026-11-06T11:40:00+05:30",
         "regime_read": {"view": "bullish", "grade": "strong_bullish", "rsi": 73.26, "vix": 17.12}},
        {"short_id": "r3", "ticker": "HDFCBANK.NS", "date": "2026-11-06", "created_at": "2026-11-06T15:09:54+05:30",
         "regime_read": {"view": "bullish", "grade": "bullish", "rsi": 63.0, "vix": 17.21}},
        {"short_id": "r0", "ticker": "INFY.NS", "date": "2026-11-05", "created_at": "2026-11-05T10:00:00+05:30",
         "regime_read": {"view": "bearish", "grade": "bearish", "rsi": 30.0, "vix": 15.0}},
    ]
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    return p


SNAP = {"as_of": "2026-11-06T15:29:16+05:30", "spots": {"NIFTY 50": 25030.35}}


def test_market_view_reads_every_source(tmp_path):
    v = d.market_view(_db(tmp_path), snapshot=SNAP)
    assert v["engine"]["date"] == "2026-11-06"
    assert [(n["ticker"], n["view"], n["rsi"]) for n in v["engine"]["names"]] == [
        ("HDFCBANK.NS", "bullish", 63.0), ("TCS.NS", "bullish", 73.3)]        # latest read per name, latest day only
    assert v["vix"] == {"close": 15.62, "close_date": "2026-11-05", "close_band": "mid", "intraday": 17.21,
                        "intraday_at": "2026-11-06T15:09:54+05:30", "intraday_band": "high"}
    f = v["flows"]
    assert (f["fii_run_days"], f["fii_run_from"], f["fii_run_sum_cr"]) == (3, "2026-10-30", 25857.25)
    assert f["dii_run_sum_cr"] == -14974.28
    g = v["gamma"]
    assert (g["date"], g["names"], g["short_gamma"], g["long_gamma"]) == ("2026-11-05", 3, 2, 1)
    assert g["indices"]["NIFTY 50"]["call_wall"] == 25000.0
    assert v["macro"]["nifty_short"] == -0.3 and v["macro"]["nifty_medium"] == 1.0
    assert v["book"] == {"bullish": 1, "bearish": 2, "neutral": 1, "unknown": 0, "total": 4}
    assert v["spots"] == {"NIFTY 50": 25030.35} and v["spots_as_of"] == SNAP["as_of"]


def test_highlights_are_plain_numbered_sentences(tmp_path):
    texts = [h["text"] for h in d.market_view(_db(tmp_path), snapshot=SNAP)["highlights"]]
    assert texts[0] == ("Today's read is bullish on 2 of the 2 names it proposed trades on, but the open book "
                        "leans bearish: 1 bullish, 2 bearish, 1 neutral (4 trades).")
    assert any("net buyers for 3 sessions in a row (30 Oct to 5 Nov), ₹25,857 cr in total; domestic funds "
               "(DII) net sold ₹14,974 cr" in t for t in texts)
    assert any("India VIX moved from 15.62 (5 Nov close, mid) to 17.21 by 15:09 on 6 Nov" in t for t in texts)
    assert any("long gamma on NIFTY 50" in t and "NIFTY now 25,030, above the call wall" in t for t in texts)
    assert any("short gamma on 2 of the 3 F&O names" in t for t in texts)
    assert any("headwind short-term (-0.3), tailwind medium-term (+1.0)" in t for t in texts)


def test_highlights_abstain_without_data():
    assert d.market_highlights({}) == []
    quiet = {"vix": {"close": 15.0, "close_band": "mid", "intraday": 15.5, "intraday_band": "mid"},
             "macro": {"nifty_short": 0.4, "nifty_medium": 0.6},
             "flows": {"fii_net_cr": 10.0, "fii_run_days": 1}}
    assert d.market_highlights(quiet) == []            # nothing changed, nothing disagrees, no streak


def test_book_in_line_with_the_read_is_said_plainly():
    view = {"engine": {"names": [{"view": "bearish"}]},
            "book": {"bullish": 0, "bearish": 3, "neutral": 1, "total": 4}}
    assert d.market_highlights(view)[0]["text"] == ("The open book (4 trades) leans bearish: "
                                                    "0 bullish, 3 bearish, 1 neutral.")


def test_missing_tables_and_database(tmp_path):
    assert "error" in d.market_view(tmp_path / "nope.db")
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()
    v = d.market_view(p, snapshot={})
    assert v["flows"] is None and v["gamma"] is None and v["macro"] is None
    assert v["book"]["total"] == 0 and v["engine"] == {"date": None, "names": []} and v["highlights"] == []


def test_direction_copy_matches_the_router():
    from src.strategy_router import ROUTING_TABLE
    assert {k: v["direction"] for k, v in ROUTING_TABLE.items()} == {
        k: v for k, v in d.STRATEGY_DIRECTIONS.items() if k != "equity_long"}
