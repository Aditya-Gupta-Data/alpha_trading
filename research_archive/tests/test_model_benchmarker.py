"""
The model benchmarker's pure parts (summarize / needle checks / matrix
render) — offline. The bench itself is an operator script (solo Mac runs,
pulls models); its pipeline internals are already covered by
tests/test_annual_report_analyzer.py.
"""
from scripts.model_benchmarker import needle_checks, render_matchup, summarize


def _result(flags_pages=(154,), quote="capitalised Rs.476 Mn"):
    return {
        "operational_wins": [],
        "shareholder_returns": [{"finding": "div", "quote": "x", "page": 30}],
        "hidden_risks_and_flags": [
            {"finding": "f", "quote": quote, "page": p} for p in flags_pages],
        "net_conviction_score": 0.42,
        "evidence_discipline": {"findings_dropped_unverified": 7,
                                "duplicates_removed": 2, "reduced_away": 3,
                                "chunks_failed": 1},
    }


def test_needle_checks_both_booleans():
    hit = needle_checks(_result(flags_pages=(154,)))
    assert hit == {"caught_154": True, "crisp_476": True}
    miss = needle_checks(_result(flags_pages=(200,), quote="no figure here"))
    assert miss == {"caught_154": False, "crisp_476": False}


def test_summarize_reconstructs_raw_finding_count():
    row = summarize("m", _result(), minutes=12.5, pull_minutes=0.0)
    # kept 2 + dupes 2 + reduced 3 = 7 validated; + 7 dropped = 14 raw
    assert row["raw_findings"] == 14
    assert row["dropped"] == 7 and row["kept_flags"] == 1
    assert row["minutes"] == 12.5 and row["score"] == 0.42


def test_render_matchup_marks_booleans_and_keeps_scales_apart():
    rows = [{"model": "human analyst (benchmark)", "minutes": "—",
             "raw_findings": "—", "dropped": "—", "kept_flags": 3,
             "kept_wins": "—", "score": "55/100 (analyst scale)",
             "caught_154": True, "crisp_476": True, "chunks_failed": "—"},
            summarize("phi3:mini", _result(), 15.0, 2.0)]
    md = render_matchup(rows)
    assert "| phi3:mini | 15.0 |" in md
    assert "| YES | YES |" in md
    assert "different instruments" in md
