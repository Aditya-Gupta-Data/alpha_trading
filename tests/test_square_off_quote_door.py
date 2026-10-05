"""
Ledger Issue 44 — the intraday square-off's real-quote door was dead from
2026-07-15 to 2026-10-05.

`live_bridge._leg_quotes_for` imported `options_proposer._premium`, which
decision #70 removed the day after #69 shipped, inside a bare
`except Exception: return None`. Every call returned None, so every intraday
square-off (#69/#110) declined as "no_chain_quotes" and no capital-rotation
eviction (#115) could execute. Every test stubbed the door, so nothing
noticed. These tests drive the REAL door (only the Dhan call is faked) and
add a codebase-wide guard against an import of a name that does not exist.
Hermetic: no network, tmp journal/portfolio/brain map.

ARCHITECT RULING 1 (2026-10-05): every intraday exit crosses the spread — a
long leg is sold at the BID, a short leg bought back at the ASK, never the
last traded price. The door refuses (None, reason printed) a leg it cannot
cross: no bid on a long, no ask on a short, a crossed book, or a quote more
than 50% off a live last price. The tests below were changed from the
Issue-44 last-traded expectations where the ruling changes them.
"""
import ast
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from src import dhan_client, journal, live_bridge as lb, plan_tracker as pt
from src import portfolio as pf

ROOT = Path(__file__).resolve().parents[1]
# tests/test_options_spreads.py and tests/test_expiry_backstop.py assign stubs
# straight onto plan_tracker (cash settlement, brain connect) and never put
# them back; captured here at collection time, before any test runs
_REAL_SETTLE_CASH = pt._settle_spread_cash
_REAL_BRAIN_CONNECT = pt._brain_connect


def _chain(ltps: dict, key_fmt="{:.6f}") -> dict:
    """{(strike, 'CE'|'PE'): last_price} -> a Dhan-shaped chain."""
    oc = {}
    for (strike, kind), ltp in ltps.items():
        oc.setdefault(key_fmt.format(float(strike)), {})[kind.lower()] = {
            "last_price": ltp, "top_bid_price": ltp - 1 if ltp else 0, "top_ask_price": ltp + 1 if ltp else 0}
    return {"last_price": 23000.0, "oc": oc}


def _book(nodes: dict) -> dict:
    """{(strike, 'CE'|'PE'): (bid, ask, last)} -> a Dhan-shaped chain."""
    oc = {}
    for (strike, kind), (bid, ask, ltp) in nodes.items():
        oc.setdefault(f"{float(strike):.6f}", {})[kind.lower()] = {
            "top_bid_price": bid, "top_ask_price": ask, "last_price": ltp}
    return {"last_price": 23000.0, "oc": oc}


def _entry(short_id="sq000001"):
    # a bear put: long 24000 PE @150, short 23500 PE @70 — debit 80, max profit 420/share
    lot = 75
    return {"short_id": short_id, "date": "2026-07-10", "ticker": "NIFTY 50", "action": "BUY", "price": 80.0,
            "decision": "approved", "signal": "bearish trend", "why": "test",
            "spread": {"strategy": "bear_put_spread", "expiry": "2026-07-21", "lot_size": lot, "lots": 1,
                       "entry_spot": 24500.0, "max_profit": 420.0 * lot, "max_loss": 80.0 * lot,
                       "legs": [{"side": "BUY", "option_type": "PE", "strike": 24000.0, "premium": 150.0},
                                {"side": "SELL", "option_type": "PE", "strike": 23500.0, "premium": 70.0}]}}


WIN = {(24000.0, "PE"): 500.0, (23500.0, "PE"): 120.0}       # last prices; _chain quotes bid/ask at +-1
# crossed: the long 24000 PE sold at its bid 499, the short 23500 PE bought
# back at its ask 121 -> exit mark 378, profit 298/share = 71% of max -> above 65%
WIN_CROSSED = {(24000.0, "PE"): 499.0, (23500.0, "PE"): 121.0}


def _sandbox(tmp_path, monkeypatch, entries):
    # another test file leaves a FakeJournal on plan_tracker (known leak, see
    # tests/test_audit_chunk1_fixes.py); bind the real module for this test
    monkeypatch.setattr(pt, "journal", journal)
    monkeypatch.setattr(journal, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    journal.rewrite_all(entries)
    (tmp_path / "portfolio.json").write_text(json.dumps({"cash": 100000.0, "holdings": {}}))
    monkeypatch.setattr(pf, "PORTFOLIO_PATH", tmp_path / "portfolio.json", raising=False)
    from src import brain_map
    monkeypatch.setattr(brain_map, "DEFAULT_DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(pt, "_settle_spread_cash", _REAL_SETTLE_CASH)
    monkeypatch.setattr(pt, "_brain_connect", _REAL_BRAIN_CONNECT)


def _serve(monkeypatch, chain, calls=None):
    def fake(ticker, expiry):
        if calls is not None:
            calls.append((ticker, expiry))
        return chain
    monkeypatch.setattr(dhan_client, "get_option_chain", fake)


def test_the_real_door_returns_crossed_prices_long_at_bid_short_at_ask(monkeypatch):
    """Ruling 1 (changed from Issue 44's last-traded expectation): the long
    leg is priced at the bid it can be sold at, the short at the ask it is
    bought back at."""
    calls = []
    _serve(monkeypatch, _chain(WIN), calls)
    assert lb._leg_quotes_for(_entry()) == WIN_CROSSED
    assert calls == [("NIFTY 50", "2026-07-21")]                 # the chain IS requested (it never was)
    _serve(monkeypatch, _chain(WIN, key_fmt="{}"))               # "24000.0" keys, not "24000.000000"
    assert lb._leg_quotes_for(_entry()) == WIN_CROSSED
    # an asymmetric book proves the side, not an average: bid for the long, ask for the short
    _serve(monkeypatch, _book({(24000.0, "PE"): (480.0, 520.0, 500.0),
                               (23500.0, "PE"): (110.0, 135.0, 120.0)}))
    assert lb._leg_quotes_for(_entry()) == {(24000.0, "PE"): 480.0, (23500.0, "PE"): 135.0}


@pytest.mark.parametrize("nodes, reason", [
    # a long with no bid is REFUSED here, not priced at 0 as the live arm marks it
    ({(24000.0, "PE"): (0, 501.0, 500.0), (23500.0, "PE"): (119.0, 121.0, 120.0)},
     "refused on 24000PE: no bid to sell the long leg"),
    ({(24000.0, "PE"): (499.0, 501.0, 500.0), (23500.0, "PE"): (119.0, 0, 120.0)},
     "refused on 23500PE: no ask to buy the short leg back"),
    ({(24000.0, "PE"): (505.0, 501.0, 500.0), (23500.0, "PE"): (119.0, 121.0, 120.0)},
     "refused on 24000PE: crossed book (bid > ask)"),
    ({(24000.0, "PE"): (499.0, 501.0, 500.0), (23500.0, "PE"): (119.0, 190.0, 120.0)},
     "refused on 23500PE: quote 190 is >50% off last 120"),
], ids=["long_no_bid", "short_no_ask", "crossed_book", "stale_quote"])
def test_the_door_refuses_a_leg_it_cannot_cross_and_names_why(monkeypatch, capsys, nodes, reason):
    _serve(monkeypatch, _book(nodes))
    assert lb._leg_quotes_for(_entry()) is None
    assert f"(square-off quotes for sq000001: {reason})" in capsys.readouterr().out


def test_a_long_with_no_bid_is_refused_where_the_live_arm_would_mark_it_at_zero():
    """The one deliberate difference from live_pricer.crossed_close_price."""
    from src.execution import live_pricer
    leg = {"side": "BUY", "option_type": "PE", "strike": 24000.0}
    q = {"bid": None, "ask": 501.0, "ltp": 500.0}
    assert live_pricer.crossed_close_price(leg, q) == (0.0, None)     # the live arm: marks at 0
    assert lb._crossed_exit_price(leg, q) == (None, "no bid to sell the long leg")


def test_the_door_refuses_without_a_chain_and_names_why(monkeypatch, capsys):
    _serve(monkeypatch, None)
    monkeypatch.setattr(dhan_client, "last_chain_error", lambda: "rate limit (DH-904: Too many requests)")
    assert lb._leg_quotes_for(_entry()) is None
    assert "option chain unavailable (rate limit (DH-904" in capsys.readouterr().out

    def boom(t, e):
        raise ConnectionError("reset by peer")
    monkeypatch.setattr(dhan_client, "get_option_chain", boom)
    assert lb._leg_quotes_for(_entry()) is None
    assert "chain fetch failed: reset by peer" in capsys.readouterr().out


def _entry_side_ladder(spread: dict) -> float:
    qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
    return sum(pt.apply_slippage(l["premium"], "OPTION") * qty for l in spread["legs"])


def _all_frictions(spread: dict, exit_prices: dict) -> float:
    qty = int(spread["lot_size"]) * int(spread.get("lots", 1))
    f = 0.0
    for l in spread["legs"]:
        side = l["side"].upper()
        f += pf.calculate_trade_frictions("OPTION", side, l["premium"], qty)
        f += pf.calculate_trade_frictions("OPTION", "SELL" if side == "BUY" else "BUY",
                                          exit_prices[(float(l["strike"]), l["option_type"])], qty)
    return f


def test_a_square_off_through_the_real_door_settles_on_crossed_prices(tmp_path, monkeypatch):
    """The venue path (config.json arms the paper venue): the EXIT ticket's
    limits ARE the crossed prices, the venue adds its tier slip on them as
    it does on these accounts' crossed entry limits, and the exit-side
    ladder is not charged on top."""
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)    # the 65% take is the trigger under test
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", True)
    entries = [_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    _serve(monkeypatch, _chain(WIN))
    sig = {"short_id": "sq000001", "signal": "profit_take", "capture_pct": 72.0}
    out = lb.intraday_square_off(sig, entries=journal.read_all(), today=date(2026, 7, 14))   # default door
    assert out["status"] == "squared_off", out
    o = journal.read_all()[0]["outcome"]
    assert o["exit_basis"] == "intraday_chain" and o["resolution"] == "profit_take"
    assert o["exit_price_basis"] == "crossed"
    assert o["price"] == 378.0                                        # 499 - 121, not 500 - 120
    ex = o["execution"]
    assert ex["mode"] == "paper_venue" and ex["status"] == "FILLED"
    assert {k: v["limit"] for k, v in ex["fills"].items()} == {"24000PE": 499.0, "23500PE": 121.0}
    venue_rs = ex["venue_slippage_ps"] * 75
    assert o["slippage_rs"] == pytest.approx(_entry_side_ladder(_entry()["spread"]) + venue_rs, abs=0.02)


def test_a_non_venue_square_off_charges_no_exit_ladder_on_crossed_prices(tmp_path, monkeypatch):
    """Ruling 1: the crossing IS the exit slippage (the #70 precedent) — with
    no venue, the booked slippage is the entry-side ladder alone and the P&L
    is the crossed gross less frictions and that ladder."""
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    monkeypatch.setattr("src.config.PAPER_VENUE_ENABLED", False)
    entries = [_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    _serve(monkeypatch, _chain(WIN))
    sig = {"short_id": "sq000001", "signal": "profit_take", "capture_pct": 72.0}
    out = lb.intraday_square_off(sig, entries=journal.read_all(), today=date(2026, 7, 14))
    assert out["status"] == "squared_off", out
    o = journal.read_all()[0]["outcome"]
    assert "execution" not in o and o["exit_price_basis"] == "crossed"
    spread = _entry()["spread"]
    ladder = _entry_side_ladder(spread)
    frictions = _all_frictions(spread, WIN_CROSSED)
    assert o["slippage_rs"] == pytest.approx(ladder, abs=0.01)
    assert o["frictions_rs"] == pytest.approx(frictions, abs=0.01)
    assert o["pnl_rs"] == pytest.approx(298.0 * 75 - frictions - ladder, abs=0.02)
    book = json.loads((tmp_path / "portfolio.json").read_text())
    assert book["cash"] == pytest.approx(100000.0 + o["pnl_rs"], abs=0.01)


def test_the_real_capture_gate_is_judged_on_crossed_prices(tmp_path, monkeypatch, capsys):
    """Last prices at 67% of max profit, crossed at 62%: the take is declined
    on the crossed (realizable) value and the trade stays with the EOD path."""
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    entries = [_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    # last 470 / 110 -> 280/share = 66.7%; crossed 465 / 114 -> 271/share = 64.5% < 65%
    _serve(monkeypatch, _book({(24000.0, "PE"): (465.0, 475.0, 470.0),
                               (23500.0, "PE"): (106.0, 114.0, 110.0)}))
    sig = {"short_id": "sq000001", "signal": "profit_take", "capture_pct": 70.0}
    out = lb.intraday_square_off(sig, entries=journal.read_all(), today=date(2026, 7, 14))
    assert out["status"] == "below_threshold_on_real_quotes"
    assert out["real_capture_pct"] == pytest.approx(271 / 420 * 100, abs=0.01)
    assert journal.read_all()[0].get("outcome") is None


def test_the_bridge_logs_every_square_off_outcome():
    assert lb._square_off_note({"signal": "ratchet_hit"}) == ""                      # advisory only
    assert lb._square_off_note({"squared_off": True}) == " — SQUARED OFF intraday on real quotes"
    assert lb._square_off_note({"square_off_status": "no_chain_quotes"}) == (
        " — intraday fill declined (no_chain_quotes); the EOD path owns it")
    assert lb._square_off_note({"square_off_status": "above_lock_on_real_quotes",
                                "square_off_real_capture_pct": 74.6}) == (
        " — intraday fill declined (above_lock_on_real_quotes, 75% on real quotes); the EOD path owns it")
    assert "declined (error: boom)" in lb._square_off_note({"square_off_status": "error", "square_off_reason": "boom"})


def test_live_cycle_records_a_declined_square_off_on_the_signal(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.RATCHET_ENABLED", False)
    entries = [_entry()]
    _sandbox(tmp_path, monkeypatch, entries)
    _serve(monkeypatch, None)
    fired = lb.live_cycle(["NIFTY 50"], quote_fn=lambda t: {"last_price": 23000.0}, entries=entries,
                          notify_fn=lambda m: None, now_fn=lambda: datetime(2026, 7, 14, 11, 0),
                          square_off_fn=lambda s: lb.intraday_square_off(s, entries=journal.read_all(),
                                                                         today=date(2026, 7, 14)))
    sig = next(s for s in fired if s["signal"] == "profit_take")
    assert sig["square_off_status"] == "no_chain_quotes" and not sig.get("squared_off")
    assert journal.read_all()[0].get("outcome") is None


# ------------------------------------------------- the codebase-wide guard

def _top_level_names(path: Path) -> set:
    """Names a module defines at its top level, statically (no import)."""
    names = set()

    def visit(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []) or [])
                for h in getattr(node, "handlers", []) or []:
                    visit(h.body)
    visit(ast.parse(path.read_text()).body)
    return names


def test_every_src_import_names_something_that_exists():
    """A lazy `from src.x import y` of a name that no longer exists, inside
    a fail-open try, is exactly how the #69 door died silently. Checked
    statically for every import in src/, including those inside functions."""
    src = ROOT / "src"
    bad = []
    for path in sorted(src.rglob("*.py")):
        if "research_archive" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                    and node.module.split(".")[0] == "src"):
                continue
            mod = ROOT.joinpath(*node.module.split("."))
            mod_file = mod.with_suffix(".py") if mod.with_suffix(".py").exists() else mod / "__init__.py"
            if not mod_file.exists():
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno} module {node.module} does not exist")
                continue
            defined = _top_level_names(mod_file)
            for a in node.names:
                if a.name == "*" or a.name in defined or (mod / f"{a.name}.py").exists() or (mod / a.name).is_dir():
                    continue
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.module}.{a.name} does not exist")
    assert bad == [], "\n".join(bad)
