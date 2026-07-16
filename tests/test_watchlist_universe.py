"""
Cash-equity universe contract (2026-07-16 Data Expansion Pack).

Locks the invariant the expansion relies on: every ticker in
config/watchlist.yaml must resolve to a verified instrument in
dhan_client.SECURITY_ID_MAP — otherwise the daily consumers (suggest,
main's alert checks, news_processor) silently skip it and the "tracked
universe" is a lie. Also guards the map itself against the classic
copy-paste failure modes (duplicate ids inside a segment, malformed
entries). 100% offline — pure dict/YAML reads, no Dhan calls.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dhan_client as dc

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"

EXPANSION_TICKERS = [
    "SBIN.NS", "BHARTIARTL.NS", "LT.NS", "AXISBANK.NS", "KOTAKBANK.NS",
    "BAJFINANCE.NS", "ASIANPAINT.NS", "TITAN.NS", "SUNPHARMA.NS",
    "TATASTEEL.NS", "NTPC.NS", "POWERGRID.NS", "ULTRACEMCO.NS",
    "WIPRO.NS", "HCLTECH.NS", "M&M.NS", "HINDALCO.NS", "JSWSTEEL.NS",
]


def _watchlist_tickers() -> set:
    raw = yaml.safe_load(WATCHLIST_PATH.read_text())
    return {rule["ticker"] for rule in raw["watchlist"]}


def test_every_watchlist_ticker_resolves_to_a_verified_instrument():
    unresolved = sorted(t for t in _watchlist_tickers()
                        if dc._resolve(t) is None)
    assert unresolved == [], (
        f"watchlist tickers missing from SECURITY_ID_MAP: {unresolved} — "
        "add scrip-master-verified ids before listing them")


def test_expansion_tickers_are_in_both_map_and_watchlist():
    tickers = _watchlist_tickers()
    for t in EXPANSION_TICKERS:
        assert t in dc.SECURITY_ID_MAP, f"{t} missing from SECURITY_ID_MAP"
        assert t in tickers, f"{t} missing from watchlist.yaml"


def test_map_entries_are_well_formed():
    for ticker, instr in dc.SECURITY_ID_MAP.items():
        assert set(instr) == {"id", "seg", "inst"}, f"bad shape: {ticker}"
        assert instr["id"].isdigit(), f"non-numeric id: {ticker}"
        assert instr["seg"] in {"NSE_EQ", "IDX_I"}, f"unknown seg: {ticker}"
        assert instr["inst"] in {"EQUITY", "INDEX"}, f"unknown inst: {ticker}"


def test_no_duplicate_security_ids_within_a_segment():
    seen: dict = {}
    for ticker, instr in dc.SECURITY_ID_MAP.items():
        key = (instr["seg"], instr["id"])
        assert key not in seen, (
            f"duplicate id {instr['id']} in {instr['seg']}: "
            f"{seen[key]} vs {ticker}")
        seen[key] = ticker


def test_bare_symbol_aliasing_still_works_for_new_names():
    # main/suggest historically pass bare symbols; _resolve appends .NS.
    assert dc._resolve("SBIN") is dc.SECURITY_ID_MAP["SBIN.NS"]
    assert dc._resolve("hcltech") is dc.SECURITY_ID_MAP["HCLTECH.NS"]
