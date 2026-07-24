"""
tests/test_strategy_scorer.py — SB-1 forward-scoring contract
=============================================================

Covers RESOLVE (embargo until the window elapses), SCORE (reused return math,
correct value + win), ACCUMULATE (idempotent immutable ledger), NULL-honesty,
and — the load-bearing one — FUTURE-BLINDNESS: a graded call is a function of
its own window only; data after the window can never change it.
"""
import json
from datetime import date, timedelta
from pathlib import Path

from src.analysis import strategy_scorer as SC
from src.validation.timelock import assert_future_blind


def _dates(start, n):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _write(lake_dir, key, pairs):
    p = Path(lake_dir) / f"{key}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("date,value\n" + "".join(f"{d},{v}\n" for d, v in pairs))


def _it_series(dates):
    """NIFTY flat at 100; NIFTY_IT flat 100 through the anchor (index 9), then
    +5% by index 19 (the P1 window end), flat 105 after."""
    nifty, it = [], []
    for i, d in enumerate(dates):
        nifty.append((d, 100.0))
        if i <= 9:
            v = 100.0
        elif i <= 19:
            v = 100.0 + (i - 9) * 0.5          # 100 -> 105 across [9, 19]
        else:
            v = 105.0
        it.append((d, v))
    return nifty, it


def _decl(as_of="2024-01-10", recipe="long_it_inr_haven", verdict="PREFER",
          sig=True, horizon="shock", phase="P1_shock"):
    return {"as_of_session": as_of, "declared": True,
            "horizons": {horizon: {
                "declared": True, "phase": phase, "archetype": "A1",
                "strategy_verdict": verdict,
                "top_strategies": [{"name": recipe, "significant": sig}]}}}


def _setup(tmp_path, dates, decl=None):
    lake = tmp_path / "lake"
    nifty, it = _it_series(dates)
    _write(lake, "NIFTY", nifty)
    _write(lake, "NIFTY_IT", it)
    dpath = tmp_path / "decl.jsonl"
    dpath.write_text(json.dumps(decl or _decl()) + "\n")
    return lake, dpath, tmp_path / "scores.jsonl"


# ------------------------------------------------------------------ SCORE

def test_score_measures_forward_excess_and_win(tmp_path):
    lake, dpath, spath = _setup(tmp_path, _dates("2024-01-01", 40))
    out = SC.run(declarations_path=dpath, scores_path=spath, lake_dir=lake,
                 clock=lambda: "2024-02-01")
    assert out["graded"] == 1 and out["wins"] == 1
    row = json.loads(spath.read_text().strip())
    assert row["name"] == "long_it_inr_haven"
    assert row["realized_return"] == 0.05          # IT +5% vs flat NIFTY
    assert row["win"] is True
    assert row["in_sample_verdict"] == "PREFER"
    assert row["declaration_date"] == "2024-01-10"


# ------------------------------------------------------------------ RESOLVE

def test_embargo_holds_until_window_elapses(tmp_path):
    # only 13 sessions: anchor index 9 -> just 3 sessions after, window (10) not done
    lake, dpath, spath = _setup(tmp_path, _dates("2024-01-01", 13))
    out = SC.run(declarations_path=dpath, scores_path=spath, lake_dir=lake,
                 clock=lambda: "2024-01-14")
    assert out["graded"] == 0 and out["pending_declarations"] == 1
    assert not spath.exists()                       # nothing written


# ------------------------------------------------------------------ ACCUMULATE

def test_idempotent_never_double_counts(tmp_path):
    lake, dpath, spath = _setup(tmp_path, _dates("2024-01-01", 40))
    a = SC.run(declarations_path=dpath, scores_path=spath, lake_dir=lake,
               clock=lambda: "2024-02-01")
    b = SC.run(declarations_path=dpath, scores_path=spath, lake_dir=lake,
               clock=lambda: "2024-02-02")
    assert a["graded"] == 1 and b["graded"] == 0
    assert len(spath.read_text().strip().splitlines()) == 1


# ------------------------------------------------------------------ NULL-honest

def test_unpriceable_recipe_is_skipped(tmp_path):
    # a pharma recipe, but the lake has no NIFTY_PHARMA -> no row, no crash
    decl = _decl(recipe="long_pharma")
    lake, dpath, spath = _setup(tmp_path, _dates("2024-01-01", 40), decl=decl)
    out = SC.run(declarations_path=dpath, scores_path=spath, lake_dir=lake,
                 clock=lambda: "2024-02-01")
    assert out["graded"] == 0


def test_old_ledger_line_without_recipes_scores_nothing(tmp_path):
    lake, dpath, spath = _setup(tmp_path, _dates("2024-01-01", 40))
    # a pre-SR-3 line: declared, but no top_strategies
    dpath.write_text(json.dumps({"as_of_session": "2024-01-10", "declared": True,
        "horizons": {"shock": {"declared": True, "phase": "P1_shock",
                               "archetype": "A1"}}}) + "\n")
    out = SC.run(declarations_path=dpath, scores_path=spath, lake_dir=lake,
                 clock=lambda: "2024-02-01")
    assert out["graded"] == 0                        # graceful, not a crash


# ------------------------------------------------------------------ TIMELOCK

def test_score_is_future_blind_beyond_its_window(tmp_path):
    """The load-bearing property: perturbing NIFTY_IT AT AND AFTER the window
    end (index 20+) must NOT change the graded return of the [9, 19] window."""
    dates = _dates("2024-01-01", 40)
    base = [(d, 100.0, it) for (d, _), (_, it) in zip(*_it_series(dates))]

    def salt(rows):
        # wild future IT values strictly after the P1 window end (index 19)
        return [(d, n, (9999.0 if i > 19 else it))
                for i, (d, n, it) in enumerate(rows)]

    counter = {"n": 0}

    def compute(rows):
        counter["n"] += 1
        lake = tmp_path / f"tl{counter['n']}"
        _write(lake, "NIFTY", [(d, n) for d, n, _ in rows])
        _write(lake, "NIFTY_IT", [(d, it) for d, _, it in rows])
        cal = SC._benchmark_calendar(lake)
        block = _decl()["horizons"]["shock"]
        graded, _r, _p = SC.score_horizon(
            "2024-01-10", "shock", block, cal, lake, set(), "2024-02-01")
        return graded[0]["realized_return"] if graded else None

    assert compute(base) == 0.05                     # sanity: it does resolve
    assert_future_blind(compute, base, salt(base),
                        label="strategy_scorer.score_horizon")
