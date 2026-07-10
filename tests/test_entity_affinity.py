"""
Tests for the Phase-8 entity-affinity learning layer
(src/knowledge_graph/entity_affinity.py) and the raw-deal history ledger it
feeds on (src/ingestion/deals_tracker append/read). Fully offline: an
in-memory brain_map DB, synthetic deal history, no network.

Covers: client canonicalization, ticker→group mapping, the accumulation
math (concentration, net direction), per-day idempotency, decay-friendly
edge projection (only touched pairs), the recency window on the direction
signal, and the DISTRIBUTION/ACCUMULATION advisory verdicts.

Run either of these from the project folder:
    python tests/test_entity_affinity.py     (simple, no extra installs)
    python -m pytest tests/                    (if you have pytest)
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import brain_map, graph_engine
from src.ingestion import deals_tracker as dt
from src.knowledge_graph import entity_affinity as ea


GROUPS = {
    "ticker_to_group": {
        "ADANIENT.NS": "ADANI", "ADANIPORTS.NS": "ADANI",
        "ADANIPOWER.NS": "ADANI", "ADANIGREEN.NS": "ADANI",
        "TCS.NS": "TATA", "TATAMOTORS.NS": "TATA",
        "WIPRO.NS": "IT", "RELIANCE.NS": "RELIANCE",
    },
    "groups": {"ADANI": ["ADANIENT.NS", "ADANIPORTS.NS"], "TATA": ["TCS.NS"]},
    "client_aliases": {},
}


def _deal(ticker, client, side, qty, as_of, value=None, deal_type="bulk"):
    return {"ticker": ticker, "client": client, "side": side, "qty": qty,
            "price": (value / qty if value else None),
            "value_rs": value, "deal_type": deal_type, "as_of": as_of}


# ------------------------------------------------------- canonicalization

def test_canonicalize_client_collapses_variance():
    assert ea.canonicalize_client("SBI MUTUAL FUND A/C SBI BLUECHIP") == "SBI MUTUAL FUND"
    assert ea.canonicalize_client("Societe Generale - ODI") == "SOCIETE GENERALE"
    assert ea.canonicalize_client("Graviton Research Capital LLP") == "GRAVITON RESEARCH CAPITAL"
    assert ea.canonicalize_client("MORGAN STANLEY ASIA (SINGAPORE) 12345") == "MORGAN STANLEY ASIA SINGAPORE"
    # Identity-bearing tokens (FUND/CAPITAL) are NOT stripped.
    assert "FUND" in ea.canonicalize_client("XYZ Opportunities Fund")
    for junk in (None, "", "   ", "12345"):
        assert ea.canonicalize_client(junk) in (None,)


def test_canonicalize_client_alias_wins():
    aliases = {"SOCIETE GENERALE": "SOCGEN"}
    assert ea.canonicalize_client("Societe Generale - ODI", aliases) == "SOCGEN"


def test_group_for_ticker():
    ttg = GROUPS["ticker_to_group"]
    assert ea.group_for_ticker("ADANIENT.NS", ttg) == "ADANI"
    assert ea.group_for_ticker("adanient.ns", ttg) == "ADANI"   # case-insensitive
    assert ea.group_for_ticker("WIPRO.NS", ttg) == "IT"
    assert ea.group_for_ticker("UNKNOWN.NS", ttg) == ea.UNGROUPED
    assert ea.group_for_ticker(None, ttg) == ea.UNGROUPED


def test_load_entity_groups_inverts_and_degrades():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "g.json"
        p.write_text(json.dumps({"groups": {"adani": ["ADANIENT.NS", "adaniports.ns"]}}))
        g = ea.load_entity_groups(p)
        assert g["ticker_to_group"]["ADANIENT.NS"] == "ADANI"
        assert g["ticker_to_group"]["ADANIPORTS.NS"] == "ADANI"   # uppercased
        # Missing / broken files degrade to empty maps.
        assert ea.load_entity_groups(Path(tmp) / "nope.json")["ticker_to_group"] == {}
        broken = Path(tmp) / "b.json"; broken.write_text("{bad")
        assert ea.load_entity_groups(broken)["ticker_to_group"] == {}


# ------------------------------------------------------------- accumulate

def _concentrated_seller_history(as_of="2026-07-20"):
    """One entity, 5 ADANI deals, all selling — a linked distributor."""
    return [_deal(tk, "MISTY SEAS FUND A/C 99", "sell", 10000, as_of, value=1_000_000)
            for tk in ("ADANIENT.NS", "ADANIPORTS.NS", "ADANIPOWER.NS",
                       "ADANIGREEN.NS", "ADANIENT.NS")]


def test_accumulate_folds_counts_and_projects_linked_edge():
    conn = brain_map.connect(":memory:")
    acc = ea.accumulate_entity_affinity(conn, _concentrated_seller_history(),
                                        GROUPS, today=date(2026, 8, 1))
    assert acc["folded"] == 5 and acc["new_days"] == 1 and acc["edges"] == 1
    row = conn.execute("SELECT * FROM entity_affinity WHERE grp='ADANI'").fetchone()
    assert row["client"] == "MISTY SEAS FUND" and row["deal_count"] == 5
    assert row["sell_qty"] == 50000 and row["buy_qty"] == 0
    # The affinity edge is queryable through the graph.
    ge = graph_engine.GraphEngine(conn=conn)
    ctx = ge.get_relevant_context("MISTY SEAS FUND")
    assert ctx and ctx[0]["relation"] == "concentrates_in" and ctx[0]["target"] == "ADANI"


def test_diversified_trader_is_not_linked():
    conn = brain_map.connect(":memory:")
    # 1 ADANI deal among many groups -> concentration well below threshold.
    hist = [_deal("ADANIENT.NS", "DIVERSE FUND", "buy", 500, "2026-07-20", value=50000),
            _deal("TCS.NS", "DIVERSE FUND", "buy", 500, "2026-07-20", value=50000),
            _deal("WIPRO.NS", "DIVERSE FUND", "buy", 500, "2026-07-20", value=50000),
            _deal("RELIANCE.NS", "DIVERSE FUND", "buy", 500, "2026-07-20", value=50000)]
    acc = ea.accumulate_entity_affinity(conn, hist, GROUPS, today=date(2026, 8, 1))
    assert acc["edges"] == 0                          # no link projected
    _, concentration, deals = ea._client_concentration(conn, "DIVERSE FUND")
    assert concentration < ea.MIN_CONCENTRATION


def test_accumulate_is_idempotent_per_day():
    conn = brain_map.connect(":memory:")
    hist = _concentrated_seller_history()
    ea.accumulate_entity_affinity(conn, hist, GROUPS, today=date(2026, 8, 1))
    again = ea.accumulate_entity_affinity(conn, hist, GROUPS, today=date(2026, 8, 1))
    assert again["folded"] == 0 and again["edges"] == 0   # same day never double-counts
    total = conn.execute("SELECT deal_count FROM entity_affinity WHERE grp='ADANI'").fetchone()
    assert total["deal_count"] == 5                        # not 10


def test_only_touched_pairs_reproject_so_edges_can_decay():
    conn = brain_map.connect(":memory:")
    ea.accumulate_entity_affinity(conn, _concentrated_seller_history("2026-07-20"),
                                  GROUPS, today=date(2026, 8, 1))
    # A later day with unrelated (ungrouped) activity: the ADANI edge must
    # NOT be reinforced, so decay_engine can fade it.
    other = [_deal("WIPRO.NS", "SOME HFT", "buy", 100, "2026-07-21", value=10000)]
    acc = ea.accumulate_entity_affinity(conn, _concentrated_seller_history("2026-07-20") + other,
                                        GROUPS, today=date(2026, 8, 1))
    assert acc["new_days"] == 1                # only the new 07-21 day folds
    assert acc["edges"] == 0                   # ADANI pair untouched -> not reprojected


# ------------------------------------------------------- read-model + signal

def test_readmodel_direction_uses_recent_window_only():
    conn = brain_map.connect(":memory:")
    # 5 linking deals long ago (before the window) — establishes the link.
    old = _concentrated_seller_history("2026-01-05")
    ea.accumulate_entity_affinity(conn, old, GROUPS, today=date(2026, 8, 1))
    rm = ea.build_affinity_readmodel(conn, GROUPS, old, today=date(2026, 8, 1),
                                     window_days=45)
    e = rm["groups"]["ADANI"]["linked_entities"][0]
    assert e["client"] == "MISTY SEAS FUND"
    assert e["recent_direction"] == "flat"     # deals are outside the 45d window
    # ...so no advisory fires for a stale link with no recent flow.
    assert ea.evaluate_distribution_signals(rm) == []


def test_distribution_advisory_fires_on_recent_unloading():
    conn = brain_map.connect(":memory:")
    hist = _concentrated_seller_history("2026-07-20")
    ea.accumulate_entity_affinity(conn, hist, GROUPS, today=date(2026, 8, 1))
    rm = ea.build_affinity_readmodel(conn, GROUPS, hist, today=date(2026, 8, 1))
    assert rm["groups"]["ADANI"]["net_bias"] == "distribution"
    adv = ea.evaluate_distribution_signals(rm, today=date(2026, 8, 1))
    assert len(adv) == 1
    assert adv[0]["verdict"] == "DISTRIBUTION" and adv[0]["group"] == "ADANI"
    assert "bearish" in adv[0]["lean"] and "MISTY SEAS FUND" in adv[0]["entities"]


def test_accumulation_advisory_mirror():
    conn = brain_map.connect(":memory:")
    hist = [_deal(tk, "PATIENT WHALE", "buy", 8000, "2026-07-25", value=800_000)
            for tk in ("ADANIENT.NS", "ADANIPORTS.NS", "ADANIPOWER.NS", "ADANIENT.NS")]
    ea.accumulate_entity_affinity(conn, hist, GROUPS, today=date(2026, 8, 1))
    rm = ea.build_affinity_readmodel(conn, GROUPS, hist, today=date(2026, 8, 1))
    adv = ea.evaluate_distribution_signals(rm, today=date(2026, 8, 1))
    assert len(adv) == 1 and adv[0]["verdict"] == "ACCUMULATION"
    assert "bullish" in adv[0]["lean"]


def test_direction_dead_zone_is_mixed():
    assert ea._classify_direction(100, 100) == "mixed"       # perfectly two-way
    assert ea._classify_direction(100, 0) == "accumulating"
    assert ea._classify_direction(0, 100) == "distributing"
    assert ea._classify_direction(0, 0) == "flat"


# ------------------------------------------------------------- orchestrate

def test_run_end_to_end_writes_readmodel_and_advisory_log():
    with tempfile.TemporaryDirectory() as tmp:
        # Raw ledger the run reads from.
        hist_path = Path(tmp) / "deals_history.jsonl"
        with open(hist_path, "w") as f:
            for d in _concentrated_seller_history("2026-07-20"):
                f.write(json.dumps(d) + "\n")
        groups_path = Path(tmp) / "groups.json"
        groups_path.write_text(json.dumps({
            "groups": {"ADANI": ["ADANIENT.NS", "ADANIPORTS.NS",
                                 "ADANIPOWER.NS", "ADANIGREEN.NS"]}}))
        readmodel_path = Path(tmp) / "entity_affinity.json"
        advisory_path = Path(tmp) / "affinity_advisories.jsonl"
        ea.AFFINITY_PATH = readmodel_path
        ea.ADVISORY_LOG_PATH = advisory_path

        conn = brain_map.connect(":memory:")
        summary = ea.run(conn=conn, history_path=hist_path,
                         groups_path=groups_path, today=date(2026, 8, 1))
        assert summary["folded"] == 5 and summary["edges"] == 1
        assert summary["advisories"] == 1
        assert readmodel_path.exists() and advisory_path.exists()
        logged = advisory_path.read_text().strip()
        assert "DISTRIBUTION" in logged


# ------------------------------------------------- raw-deal history ledger

def test_history_ledger_append_dedup_and_read():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hist.jsonl"
        deals = [dt.normalize_deal({"symbol": "ADANIENT", "clientName": "X FUND",
                                    "buySell": "SELL", "qty": 1000, "watp": 100})]
        assert dt.append_raw_deals(deals, "2026-07-20", path=path) == 1
        # Same day again -> no double append.
        assert dt.append_raw_deals(deals, "2026-07-20", path=path) == 0
        # A new day appends.
        assert dt.append_raw_deals(deals, "2026-07-21", path=path) == 1
        rows = dt.read_deal_history(path)
        assert len(rows) == 2
        assert {r["as_of"] for r in rows} == {"2026-07-20", "2026-07-21"}
        assert rows[0]["ticker"] == "ADANIENT.NS"
        # Missing ledger reads as [].
        assert dt.read_deal_history(Path(tmp) / "gone.jsonl") == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError:
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed.")
