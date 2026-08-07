"""
The ghost portfolio (2026-08-07): what the REFUSED trades would have done.

The load-bearing tests here are the honesty ones — an unpriceable ghost
must stay unpriceable and stay OUT of the total, and the module must never
appear on an execution path.

Hermetic: injected chains and tmp lakes, no network, no live files.
"""
import json
from datetime import date

from src import ghost_tracker as GT
from src import proposal_ledger as PL

LOT = 75


def _legs(buy_strike=24600, sell_strike=24700, buy_prem=100.0,
          sell_prem=40.0, opt="CE"):
    return [{"side": "BUY", "option_type": opt, "strike": buy_strike,
             "premium": buy_prem},
            {"side": "SELL", "option_type": opt, "strike": sell_strike,
             "premium": sell_prem}]


def _ghost(underlying="NIFTY 50", fate="REJECTED_RISK_CAP", legs=None,
           entry_net=-60.0, lots=None, expiry="2026-08-27",
           session_date="2026-08-10"):
    return {"session_date": session_date, "underlying": underlying,
            "fate": fate, "reason": "max loss Rs.10,166/lot exceeds the "
                                    "Rs.10,000 hard per-trade risk cap",
            "strategy": "bull_call_spread", "direction": "bullish",
            "legs": legs if legs is not None else _legs(),
            "lot_size": LOT, "lots": lots, "expiry": expiry,
            "entry_net": entry_net, "max_loss": 4500.0}


def _chain(prices):
    """{strike: {'ce': px, 'pe': px}} -> a chain dict in Dhan's shape."""
    return {f"{float(k):.6f}": {side: {"last_price": px}
                                for side, px in v.items()}
            for k, v in prices.items()}


# ------------------------------------------------------------- selection

def test_only_refused_evaluations_are_ghosts():
    """EXECUTED and PROPOSED_PENDING became real trades — the journal owns
    those, and double-counting them here would be fiction."""
    assert GT.is_ghost({"fate": "REJECTED_MARGIN"})
    assert GT.is_ghost({"fate": "REJECTED_OTHER"})
    assert not GT.is_ghost({"fate": "EXECUTED"})
    assert not GT.is_ghost({"fate": "PROPOSED_PENDING"})
    assert not GT.is_ghost({"fate": "NO_MARKET_STATE"})


def test_the_same_refusal_all_day_is_ONE_ghost():
    """The loop re-evaluates every 15 min. Counting each sighting would
    multiply the hypothetical P&L by the polling frequency."""
    same = [_ghost() for _ in range(25)]
    assert len(GT.dedupe(same)) == 1


def test_different_strikes_are_different_ghosts():
    a = _ghost()
    b = _ghost(legs=_legs(buy_strike=24800, sell_strike=24900))
    assert len(GT.dedupe([a, b])) == 2


# --------------------------------------------------------------- pricing

def test_a_debit_spread_that_widened_shows_a_profit():
    """Paid 60 (100−40), now worth 90 (150−60): +30/unit × 75 = +2,250."""
    oc = _chain({24600: {"ce": 150.0}, 24700: {"ce": 60.0}})
    m = GT.mark_ghost(_ghost(lots=2), as_of=date(2026, 8, 12),
                      chain_fn=lambda u, e: oc)
    assert m["status"] == "PRICED"
    assert m["value_entry"] == 60.0 and m["value_now"] == 90.0
    assert m["pnl"] == round(30.0 * LOT * 2, 2)


def test_a_debit_spread_that_narrowed_shows_a_loss():
    oc = _chain({24600: {"ce": 70.0}, 24700: {"ce": 40.0}})
    m = GT.mark_ghost(_ghost(lots=1), as_of=date(2026, 8, 12),
                      chain_fn=lambda u, e: oc)
    assert m["pnl"] == round((30.0 - 60.0) * LOT, 2)


def test_a_credit_spread_prices_from_the_other_side():
    """entry_net > 0 = money in; the cost basis is negative."""
    g = _ghost(legs=[{"side": "SELL", "option_type": "PE", "strike": 24000,
                      "premium": 100.0},
                     {"side": "BUY", "option_type": "PE", "strike": 23900,
                      "premium": 60.0}],
               entry_net=40.0)
    oc = _chain({24000: {"pe": 70.0}, 23900: {"pe": 45.0}})
    m = GT.mark_ghost(g, as_of=date(2026, 8, 12), chain_fn=lambda u, e: oc)
    # value_entry = -40 (credit), value_now = 45 - 70 = -25 -> +15/unit
    assert m["value_entry"] == -40.0 and m["value_now"] == -25.0
    assert m["pnl"] == round(15.0 * LOT, 2)


def test_a_size_refused_ghost_is_priced_at_one_lot_and_SAYS_SO():
    """The engine sized it to ZERO. There is no authorised size, so the
    assumption is made in the open rather than silently."""
    oc = _chain({24600: {"ce": 150.0}, 24700: {"ce": 60.0}})
    m = GT.mark_ghost(_ghost(lots=None), as_of=date(2026, 8, 12),
                      chain_fn=lambda u, e: oc)
    assert m["lots"] == 1 and m["lots_assumed"] is True


# ------------------------------------------------------- honest blindness

def test_a_dead_strike_is_not_a_free_option():
    """last_price 0 reads as NO price. Pricing a leg at zero would hand
    the ghost a fabricated profit."""
    oc = _chain({24600: {"ce": 0}, 24700: {"ce": 60.0}})
    m = GT.mark_ghost(_ghost(), as_of=date(2026, 8, 12),
                      chain_fn=lambda u, e: oc)
    assert m["status"] == "NO_STRIKE" and m["pnl"] is None


def test_a_partially_priced_spread_is_not_a_mark():
    """One missing leg means no verdict — not a mark with three quarters
    of the evidence."""
    oc = _chain({24600: {"ce": 150.0}})          # the short leg is absent
    m = GT.mark_ghost(_ghost(), as_of=date(2026, 8, 12),
                      chain_fn=lambda u, e: oc)
    assert m["status"] == "NO_STRIKE"


def test_the_priceable_set_is_read_from_the_archiver_never_copied():
    """A second copy of that map is a second thing to let rot, and the
    failure is silent: on 2026-08-07 the archiver went 2 -> 9 and a
    duplicated map here would have kept calling seven of them
    unpriceable while their chains sat on disk."""
    from src.ingestion.chain_archiver import UNDERLYINGS
    assert GT.CHAIN_SLUGS is UNDERLYINGS or GT.CHAIN_SLUGS == UNDERLYINGS
    assert "TCS.NS" in GT.CHAIN_SLUGS and len(GT.CHAIN_SLUGS) == 9


def test_an_underlying_with_no_archived_chain_is_reported_not_modelled():
    """Since the 2026-08-07 expansion all nine live underlyings ARE
    archived, so this uses a name outside the universe. The rule is
    unchanged: a modelled price on a refused trade would corrupt the exact
    comparison this module exists to make."""
    m = GT.mark_ghost(_ghost(underlying="SBIN.NS"), as_of=date(2026, 8, 12))
    assert m["status"] == "NO_CHAIN_ARCHIVE" and m["pnl"] is None
    assert "SBIN.NS" in m["detail"]


def test_a_refusal_without_a_structure_is_named_not_dropped():
    """Rows written before the 2026-08-07 structure capture."""
    g = _ghost(legs=[])
    m = GT.mark_ghost(g, as_of=date(2026, 8, 12), chain_fn=lambda u, e: {})
    assert m["status"] == "NO_STRUCTURE"


def test_a_ghost_past_its_expiry_is_not_marked():
    m = GT.mark_ghost(_ghost(expiry="2026-08-01"), as_of=date(2026, 8, 12),
                      chain_fn=lambda u, e: _chain({24600: {"ce": 1.0}}))
    assert m["status"] == "EXPIRED"


def test_unpriced_ghosts_stay_out_of_the_total(tmp_path):
    """The headline number must never quietly include a guess."""
    ledger = tmp_path / "proposal_ledger.jsonl"
    rows = [_ghost(),
            _ghost(underlying="SBIN.NS",
                   legs=_legs(buy_strike=1400, sell_strike=1450))]
    with ledger.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    oc = _chain({24600: {"ce": 150.0}, 24700: {"ce": 60.0}})
    rep = GT.run(ledger_path=ledger, session_date="2026-08-10",
                 as_of=date(2026, 8, 12), out_path=tmp_path / "g.jsonl",
                 chain_fn=lambda u, e: oc if u == "NIFTY 50" else None)
    assert rep["ghosts"] == 2 and rep["priced"] == 1
    assert rep["by_status"]["NO_CHAIN_ARCHIVE"] == 1
    assert rep["total_pnl"] == round(30.0 * LOT, 2)
    assert "NO_CHAIN_ARCHIVE" in "\n".join(GT.render_lines(rep))


# ------------------------------------------------------------- the file

def test_marks_are_appended(tmp_path):
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(json.dumps(_ghost()) + "\n")
    out = tmp_path / "ghost_portfolio_pnl.jsonl"
    oc = _chain({24600: {"ce": 150.0}, 24700: {"ce": 60.0}})
    for _ in range(2):
        GT.run(ledger_path=ledger, session_date="2026-08-10",
               as_of=date(2026, 8, 12), out_path=out,
               chain_fn=lambda u, e: oc)
    assert len(out.read_text().splitlines()) == 2   # a mark per run, appended


def test_dry_run_writes_nothing(tmp_path):
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(json.dumps(_ghost()) + "\n")
    out = tmp_path / "g.jsonl"
    GT.run(ledger_path=ledger, session_date="2026-08-10",
           as_of=date(2026, 8, 12), out_path=out, dry_run=True,
           chain_fn=lambda u, e: _chain({24600: {"ce": 150.0},
                                         24700: {"ce": 60.0}}))
    assert not out.exists()


def test_an_empty_session_says_so(tmp_path):
    rep = GT.run(ledger_path=tmp_path / "absent.jsonl",
                 session_date="2026-08-10", as_of=date(2026, 8, 12),
                 out_path=tmp_path / "g.jsonl")
    assert rep["ghosts"] == 0
    assert "no refused trades" in GT.render_lines(rep)[0]


# ----------------------------------------------------- the isolation rule

def test_the_ghost_tracker_is_on_no_execution_path():
    """The boundary IS the safety property. If any live module ever
    imports this, the ghost book has stopped being hypothetical."""
    import re
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent / "src"
    # Mentions in prose are fine (and wanted — the boundary is documented
    # where it matters). An IMPORT is the violation.
    imports = re.compile(r"^\s*(from\s+src(\.\w+)*\s+import\s+[^\n]*"
                         r"ghost_tracker|import\s+src\.ghost_tracker)",
                         re.M)
    offenders = [py.name for py in root.rglob("*.py")
                 if py.name != "ghost_tracker.py"
                 and imports.search(py.read_text())]
    assert offenders == [], f"ghost_tracker is imported by {offenders}"


def test_it_reads_the_ledger_through_the_ledgers_own_reader(tmp_path):
    """No second parser for the same file — one door, so a format change
    cannot make the two disagree."""
    ledger = tmp_path / "l.jsonl"
    with ledger.open("w") as fh:
        fh.write(json.dumps(_ghost()) + "\n")
        fh.write(json.dumps({"session_date": "2026-08-10",
                             "fate": "EXECUTED"}) + "\n")
    assert len(PL.read_rows(path=ledger)) == 2
    assert len(GT.load_ghosts(ledger_path=ledger)) == 1
