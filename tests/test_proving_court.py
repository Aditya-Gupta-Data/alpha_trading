"""
The Proving Court nightly job (decision #97, closes SYSTEM_BLUEPRINT GAP 1).
Everything injected: in-memory brain_map, fake bhavcopy bars, fake chain,
tmp_path artifacts. No network, no live DB (the muzzle is tested too).

Run: python -m pytest tests/test_proving_court.py -q
"""

import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.validation import run_proving_court as court
from src.validation import registry as rg, trial, placebo as pb
from src.strategies import glassbreaking as gb

TODAY = date(2026, 9, 15)
FO = {"as_of": "2026-09-15", "banned": ["BANNEDCO"],
      "symbols": {"RELIANCE": {"tier": "tier1"}, "BANNEDCO": {"tier": "tier1"},
                  "SMALLCO": {"tier": "tier2"}, "HAL": {"tier": "tier1"}}}


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    rg.ensure_schema(c)
    trial.ensure_schema(c)
    pb.ensure_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, tag TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS daily_context (date TEXT PRIMARY KEY)")
    return c


def _register(conn, n, discovered_at):
    ids = []
    for i in range(n):
        r = rg.register(conn, "itemset", {"tags": [f"t{i}", f"u{discovered_at}"]},
                        description=f"pattern {i} of {discovered_at}", mining_run="test")
        conn.execute("UPDATE candidate_patterns SET discovered_at = ? WHERE pattern_id = ?",
                     (discovered_at, r["pattern_id"]))
        ids.append(r["pattern_id"])
    conn.commit()
    return ids


def _bhav_bars(sessions, closes, vol=1_000_000.0):
    return [{"session": d, "date": d, "open": c, "high": c + 2, "low": c - 2,
             "close": c, "volume": vol, "prev_close": closes[i - 1] if i else c}
            for i, (d, c) in enumerate(zip(sessions, closes))]


def _sessions(n, end=TODAY):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(out))


def _knife_bars_fn(symbols):
    """RELIANCE is a falling knife (−25% over 40 sessions); HAL is flat."""
    s = _sessions(40)
    out = {}
    for sym in symbols:
        if sym == "RELIANCE":
            out[sym] = _bhav_bars(s, [1000.0 - 6.5 * i for i in range(40)])
        elif sym == "HAL":
            out[sym] = _bhav_bars(s, [500.0 + (i % 3) for i in range(40)])
        else:
            out[sym] = []
    return out


def _chain_fn(symbol, day, today=None):
    if symbol == "RELIANCE.NS":
        return {"buy_strike": 740.0, "sell_strike": 780.0, "buy_premium": 22.0,
                "sell_premium": 8.0, "lot_size": 500, "expiry": "2026-09-30"}
    return None


# ------------------------------------------------------------ promote + enrol

def test_aged_candidates_go_to_trial_and_fresh_ones_wait():
    conn = _conn()
    old = _register(conn, 3, "2026-08-18")
    new = _register(conn, 2, "2026-09-12")
    promoted = court.promote_aged_candidates(conn, TODAY)
    assert set(promoted) == set(old)
    assert {rg.get(conn, p)["status"] for p in old} == {"TRIAL"}
    assert {rg.get(conn, p)["status"] for p in new} == {"CANDIDATE"}
    assert court.promote_aged_candidates(conn, TODAY) == []          # idempotent


def test_enrol_primitives_registers_both_as_trial():
    conn = _conn()
    ids = court.enrol_primitives(conn)
    assert set(ids) == {"falling_knife", "early_breakout"}
    assert all(rg.get(conn, p)["status"] == "TRIAL" for p in ids.values())


# ------------------------------------------------------------ placebos

def test_placebos_seed_once_per_iso_week_from_the_events_vocabulary():
    conn = _conn()
    conn.executemany("INSERT INTO events (tag) VALUES (?)",
                     [(f"tag{i}",) for i in range(12)])
    conn.commit()
    r = court.seed_placebos(conn, TODAY, rng=random.Random(1))
    assert r["seeded"] == court.PLACEBO_BATCH_SIZE and r["batch"] == "court-w202638"
    assert all(pb.is_placebo(conn, p["pattern_id"]) for p in rg.list_by_status(conn, "CANDIDATE"))
    again = court.seed_placebos(conn, TODAY + timedelta(days=1))
    assert again["seeded"] == 0 and again["skip"] == "already_seeded_this_week"
    nxt = court.seed_placebos(conn, TODAY + timedelta(days=7), rng=random.Random(2))
    assert nxt["seeded"] == court.PLACEBO_BATCH_SIZE and nxt["batch"] != r["batch"]


def test_placebos_abstain_on_a_thin_vocabulary():
    conn = _conn()
    r = court.seed_placebos(conn, TODAY, tag_pool=["a", "b"])
    assert r["seeded"] == 0 and r["skip"].startswith("tag_pool_too_small")


def test_placebos_age_into_trial_like_any_other_hypothesis_and_audit_reads_fdr():
    conn = _conn()
    court.seed_placebos(conn, TODAY - timedelta(days=14), tag_pool=list("abcdefgh"),
                        rng=random.Random(3))
    # the registry stamps discovered_at = now; age the batch by hand
    conn.execute("UPDATE candidate_patterns SET discovered_at = ?",
                 ((TODAY - timedelta(days=14)).isoformat(),))
    conn.commit()
    promoted = court.promote_aged_candidates(conn, TODAY)
    assert len(promoted) == court.PLACEBO_BATCH_SIZE
    audit = court.audit_placebos(conn)
    assert audit["batches"] == 1
    assert audit["fdr"]["state"] == "insufficient placebo n" and audit["fdr"]["alarm"] is False


# ------------------------------------------------------------ feed

def test_universe_is_option_underlyings_plus_tier1_minus_banned():
    u = court.universe(FO)
    assert {"RELIANCE", "HAL", "TCS", "INFY", "HDFCBANK", "ICICIBANK"} <= set(u)
    assert "BANNEDCO" not in u and "SMALLCO" not in u


def test_candidates_carry_bars_today_and_chain_only_where_archived():
    cands = court.build_candidates(TODAY.isoformat(), fo=FO, bars_fn=_knife_bars_fn,
                                   chain_fn=_chain_fn, today=TODAY)
    by = {c["bare"]: c for c in cands}
    assert set(by) == {"RELIANCE", "HAL"}                       # names without bars are dropped
    assert by["RELIANCE"]["symbol"] == "RELIANCE.NS"
    assert by["RELIANCE"]["today"]["date"] == TODAY.isoformat()
    assert by["RELIANCE"]["chain"]["lot_size"] == 500 and by["HAL"]["chain"] is None
    assert by["RELIANCE"]["bars"][-1]["date"] == TODAY.isoformat()


def test_chain_from_lake_reads_the_archived_chain_and_prices_both_legs(monkeypatch):
    from src import lake
    oc = {f"{k:.6f}": {"ce": {"last_price": p, "top_ask_price": p + 0.5, "top_bid_price": p - 0.5},
                       "pe": {"last_price": 1.0}}
          for k, p in ((2900.0, 60.0), (2950.0, 38.0), (3000.0, 22.0), (3050.0, 12.0),
                       (3100.0, 6.0), (3150.0, 3.0), (3200.0, 1.5))}
    rows = [{"underlying": "RELIANCE.NS", "slug": "reliance", "expiry": "2026-09-30",
             "spot": 2960.0, "oc": oc},
            {"underlying": "RELIANCE.NS", "slug": "reliance", "expiry": "2026-09-16",
             "spot": 2960.0, "oc": oc}]
    monkeypatch.setattr(lake, "read_day", lambda ds, day, name=None, root=None:
                        rows if ds == "chains/reliance" else [])
    ch = court.chain_from_lake("RELIANCE.NS", "2026-09-15", today=TODAY)
    assert ch["expiry"] == "2026-09-30"                          # 09-16 is inside the 7-day floor
    assert ch["buy_strike"] == 2950.0 and ch["sell_strike"] == 3150.0   # ATM + 4 steps
    assert ch["buy_premium"] == 38.5 and ch["sell_premium"] == 2.5      # ask / bid (#70)
    assert ch["lot_size"] == 500 and ch["spot"] == 2960.0
    assert court.chain_from_lake("HAL.NS", "2026-09-15", today=TODAY) is None   # not archived


# ------------------------------------------------------------ the run

def test_run_end_to_end_fires_grades_scores_and_publishes(tmp_path):
    conn = _conn()
    conn.executemany("INSERT INTO events (tag) VALUES (?)", [(f"tag{i}",) for i in range(12)])
    for d in _sessions(30):
        conn.execute("INSERT INTO daily_context (date) VALUES (?)", (d,))
    conn.commit()
    _register(conn, 2, "2026-08-18")
    state, ledger, shadow = tmp_path / "court.json", tmp_path / "court.jsonl", tmp_path / "gb.jsonl"
    s = court.run(today=TODAY, conn=conn, fo=FO, bars_fn=_knife_bars_fn, chain_fn=_chain_fn,
                  state_path=state, ledger_path=ledger, shadow_ledger_path=shadow,
                  pool_rupees=2_000_000.0)
    assert s["skips"] == {}
    assert len(s["promoted"]) == 2
    assert set(s["enrolled"]) == {"falling_knife", "early_breakout"}
    assert s["placebos"]["seeded"] == court.PLACEBO_BATCH_SIZE
    assert s["feed"]["symbols"] == 2 and s["feed"]["signals"] == 1 and s["feed"]["accepted"] == 1
    assert s["fires"] == 1 and s["graded"] == 0                    # fired today, nothing to grade yet
    assert s["registry"]["TRIAL"] == 4                             # 2 aged + 2 primitives
    assert s["registry"]["CANDIDATE"] == court.PLACEBO_BATCH_SIZE
    assert s["fdr"]["batches"] == 1
    cards = s["scorecards"]
    assert cards and all(c["n"] == 0 for c in cards)
    knife = next(c for c in cards if "falling_knife" in c["description"])
    assert knife["status"] == "TRIAL" and knife["reason"].startswith("insufficient n")
    assert json.loads(state.read_text())["date"] == TODAY.isoformat()
    assert len(ledger.read_text().splitlines()) == 1
    assert conn.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0] == 1
    # the fire is on the court's own hypothesis
    pid = gb.enrol_in_court(conn, "falling_knife")
    assert conn.execute("SELECT pattern_id FROM shadow_trades").fetchone()[0] == pid


def test_second_run_grades_the_open_fire_and_resolves_the_court_row(tmp_path):
    conn = _conn()
    state, ledger, shadow = tmp_path / "court.json", tmp_path / "court.jsonl", tmp_path / "gb.jsonl"
    fire_day = TODAY - timedelta(days=21)
    s0 = _sessions(40, end=fire_day)
    knife_then = {"RELIANCE": _bhav_bars(s0, [1000.0 - 6.5 * i for i in range(40)])}

    court.run(today=fire_day, conn=conn, fo=FO, bars_fn=lambda syms: {k: knife_then.get(k, []) for k in syms},
              chain_fn=_chain_fn, state_path=state, ledger_path=ledger,
              shadow_ledger_path=shadow, pool_rupees=2_000_000.0)
    # three weeks later the underlying has ripped: the bull call spread takes profit
    later = _sessions(15, end=TODAY)
    rip = {"RELIANCE": knife_then["RELIANCE"] + _bhav_bars(later, [760.0 + 12 * i for i in range(15)])}
    s = court.run(today=TODAY, conn=conn, fo=FO, bars_fn=lambda syms: {k: rip.get(k, []) for k in syms},
                  chain_fn=lambda *a, **k: None, state_path=state, ledger_path=ledger,
                  shadow_ledger_path=shadow, pool_rupees=2_000_000.0)
    assert s["graded"] == 1
    row = conn.execute("SELECT resolved, result FROM shadow_trades").fetchone()
    assert row["resolved"] == 1 and row["result"] == "win"
    knife = next(c for c in s["scorecards"] if "falling_knife" in c["description"])
    assert knife["n"] == 1 and knife["wins"] == 1 and knife["promote"] is False


def test_evaluate_trial_runs_only_past_the_floor_and_can_promote(tmp_path):
    conn = _conn()
    for d in _sessions(60):
        conn.execute("INSERT INTO daily_context (date) VALUES (?)", (d,))
    conn.commit()
    pid = gb.enrol_in_court(conn, "early_breakout")
    days = _sessions(60)
    for i in range(12):                                        # all in the validation window
        d = days[-12 + i]
        ref = trial.record_shadow_fire(conn, pid, d, f"X{i}")["ref"]
        trial.resolve_shadow(conn, ref, "win" if i < 10 else "loss", 1.0 if i < 10 else -1.0, d)
    cards = court.scorecards(conn)
    eb = next(c for c in cards if c["pattern_id"] == pid)
    assert eb["n"] == 12 and eb["evaluated"] is True
    assert eb["final_status"] == "VALIDATED" and rg.get(conn, pid)["status"] == "VALIDATED"


def test_a_broken_stage_is_a_named_skip_not_a_crash(tmp_path):
    conn = _conn()
    s = court.run(today=TODAY, conn=conn, fo=FO,
                  bars_fn=lambda syms: 1 / 0, chain_fn=_chain_fn,
                  state_path=tmp_path / "c.json", ledger_path=tmp_path / "c.jsonl",
                  shadow_ledger_path=tmp_path / "g.jsonl")
    assert "feed" in s["skips"] and "ZeroDivisionError" in s["skips"]["feed"]
    assert s["registry"]["TRIAL"] == 2                         # the primitives still enrolled
    assert (tmp_path / "c.json").exists()


def test_muzzled_under_pytest_without_a_connection(tmp_path):
    s = court.run(today=TODAY, state_path=tmp_path / "c.json", ledger_path=tmp_path / "c.jsonl")
    assert s["skips"] == {"all": "muzzled_under_pytest"}


def test_render_line_is_one_line():
    s = {"date": "2026-09-15", "promoted": ["a"], "placebos": {"seeded": 10},
         "feed": {"symbols": 6, "signals": 1}, "fires": 1, "graded": 0,
         "registry": {"TRIAL": 3, "VALIDATED": 0}, "fdr": {"fdr": {"state": "measured"}}, "skips": {}}
    line = court.render_line(s)
    assert "\n" not in line and "promoted 1" in line and "FDR measured" in line


def test_job_is_wired_into_cron_and_heartbeats():
    root = Path(__file__).resolve().parent.parent
    cron = (root / "scripts" / "setup_cron.sh").read_text()
    assert "0 21 * * *" in cron and "src.validation.run_proving_court" in cron
    from src import ops_monitor, ceo_brief
    # runs AFTER the 20:30 sweep: deliberately NOT a heartbeat job (a 21:00
    # heartbeat would read silent every night); the CEO field's STALE flag
    # is the liveness check instead.
    assert "proving_court.log" not in ops_monitor.EXPECTED_JOBS
    assert "proving_court.log" not in ceo_brief.JOB_DUE_HOUR
    assert ceo_brief.COURT_STALE_DAYS == 2
    src = (root / "src" / "validation" / "run_proving_court.py").read_text()
    for forbidden in ("journal.log(", "firm_treasury", "portfolio_manager", "request_entry"):
        assert forbidden not in src
