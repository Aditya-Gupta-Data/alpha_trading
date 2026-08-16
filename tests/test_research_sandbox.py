"""
The V2 research sandbox (2026-08-11): geo-exposure map, credit monitor,
event-study simulator.

The load-bearing test in this file is the ISOLATION one. Everything else is
a research tool; the isolation rule is what keeps it a research tool.

Hermetic: injected lakes and tmp dirs, no network, no live data files.
"""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from src.ingestion.sandbox import credit_monitor as CM
from src.research import event_study_simulator as ES

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------- THE ISOLATION RULE

def test_the_sandbox_is_imported_by_nothing_on_the_trading_path():
    """The boundary IS the safety property. V1 is frozen; if a live module
    ever imports these, that freeze has been broken by accident."""
    imports = re.compile(
        r"^\s*(from\s+src[\w.]*\s+import\s+[^\n]*"
        r"(event_study_simulator|credit_monitor)"
        r"|import\s+src\.(research|ingestion\.sandbox))", re.M)
    offenders = []
    for py in (ROOT / "src").rglob("*.py"):
        if "research" in py.parts or "sandbox" in py.parts:
            continue
        if imports.search(py.read_text()):
            offenders.append(py.name)
    assert offenders == [], f"sandbox imported by live code: {offenders}"


def test_the_sandbox_is_on_no_cron():
    cron = (ROOT / "scripts" / "setup_cron.sh").read_text()
    assert "src.research" not in cron
    assert "sandbox" not in cron


# ------------------------------------------------------ geo-exposure map

def _geo():
    return json.loads((ROOT / "config" / "geo_revenue_exposure.json").read_text())


def test_geo_map_parses_and_every_row_carries_its_confidence():
    """These are HAND-ENTERED estimates. A row without a confidence and a
    source is an assertion dressed as data."""
    raw = _geo()
    assert raw["exposures"], "the map ships empty"
    for ticker, body in raw["exposures"].items():
        assert ticker.endswith(".NS"), ticker
        assert body["exposures"], ticker
        for e in body["exposures"]:
            assert e["confidence"] in ("high", "medium", "low"), ticker
            assert e.get("source"), f"{ticker}: exposure with no source"
            assert e.get("driver"), f"{ticker}: exposure with no mechanism"
            assert e["kind"] in ("india_state", "country", "region_bloc")


def test_unquantified_share_is_None_never_zero():
    """`None` means 'material but unmeasured'. Zero would mean 'no
    exposure', which is the opposite claim."""
    for body in _geo()["exposures"].values():
        for e in body["exposures"]:
            assert e["share_pct"] is None or e["share_pct"] > 0


def test_every_name_declares_what_kind_of_shock_reaches_it():
    for ticker, body in _geo()["exposures"].items():
        assert body.get("shock_sensitivity"), ticker


# ---------------------------------------------------------- credit monitor

def test_the_vocabulary_matches_the_REAL_sebi_categories():
    """These strings are verbatim SEBI disclosure categories, counted from
    the live lake (15,326 rows / 79 distinct values over 400 sessions). The
    first draft of this vocabulary was written for free-text headlines and
    matched ZERO filings across 703 partitions — this test is what stops
    that regressing."""
    assert CM.classify("Corporate Insolvency Resolution Process") == "INSOLVENCY"
    assert CM.classify("Defaults on Payment of Interest/Principal") == "DEFAULT"
    assert CM.classify("Delay/default in the payment of fines/penalties/dues "
                       "etc. to authority") == "DEFAULT"
    assert CM.classify("Fraud/Default/Arrest") == "DEFAULT"
    assert CM.classify("Credit Rating- Revision") == "RATING_ACTION"
    assert CM.classify("Suspension of Trading") == "SUSPENSION"
    assert CM.classify("Pendency of Litigation(s)/dispute(s) or the outcome "
                       "impacting the Company") == "LITIGATION"
    assert CM.classify("Giving guarantees/indemnity/ becoming a surety for "
                       "third party") == "GUARANTEE"
    assert CM.classify("Allotment of Securities") == "ISSUANCE"


def test_the_plural_that_defeated_the_first_draft():
    """`\\bdefault\\b` does not match "Defaults" — the trailing s defeats the
    word boundary. That one regex detail cost the entire first run."""
    assert CM.classify("Defaults on Payment of Interest/Principal") == "DEFAULT"


def test_insolvency_outranks_everything_it_co_occurs_with():
    assert CM.classify("Corporate Insolvency Resolution Process - "
                       "default on NCD") == "INSOLVENCY"


def _events(rows_by_day, tmp_path):
    """A fake events lake: {day: [rows]} + a read_day_fn over it."""
    d = tmp_path / "events"
    d.mkdir(exist_ok=True)
    for day in rows_by_day:
        (d / f"date={day}").mkdir(exist_ok=True)
    return d, (lambda day: rows_by_day.get(day, []))


def test_credit_report_counts_and_names_the_stressed_symbols(tmp_path):
    days = {
        "2026-08-03": [{"as_of": "2026-08-03", "symbol": "ACME", "ticker": "ACME.NS",
                        "subject": "Credit Rating- Revision"}],
        "2026-08-04": [{"as_of": "2026-08-04", "symbol": "ACME", "ticker": "ACME.NS",
                        "subject": "Defaults on Payment of Interest/Principal"},
                       {"as_of": "2026-08-04", "symbol": "BETA", "ticker": "BETA.NS",
                        "subject": "Board Meeting Intimation"}],
    }
    d, fn = _events(days, tmp_path)
    rep = CM.report("2026-08-01", "2026-08-05", events_dir=d, read_day_fn=fn)
    assert rep["credit_filings"] == 2
    assert rep["by_category"] == {"DEFAULT": 1, "RATING_ACTION": 1}
    neg = rep["symbols_with_negative_credit_news"]
    assert neg["ACME"] == {"total": 2,
                           "by_category": {"RATING_ACTION": 1, "DEFAULT": 1}}
    assert "BETA" not in neg
    assert "Board Meeting Intimation" in rep["unclassified_sample"]


def test_the_credit_monitor_writes_nothing_unless_asked(tmp_path):
    """A research tool that writes by default pollutes the lake."""
    days = {"2026-08-03": [{"as_of": "2026-08-03", "symbol": "ACME",
                            "subject": "Credit Rating- Revision"}]}
    d, fn = _events(days, tmp_path)
    rep = CM.report("2026-08-01", "2026-08-05", events_dir=d, read_day_fn=fn)
    assert rep["written_to"] is None
    out = tmp_path / "credit.jsonl"
    rep2 = CM.report("2026-08-01", "2026-08-05", events_dir=d, read_day_fn=fn,
                     out_path=out)
    assert rep2["written_to"] == str(out)
    assert len(out.read_text().splitlines()) == 1


def test_an_unreadable_partition_costs_that_day_not_the_run(tmp_path):
    days = {"2026-08-03": [{"as_of": "2026-08-03", "symbol": "A",
                            "subject": "Credit Rating- Revision"}],
            "2026-08-04": "boom"}

    def fn(day):
        rows = days.get(day, [])
        if rows == "boom":
            raise RuntimeError("corrupt partition")
        return rows

    d = tmp_path / "events"
    d.mkdir()
    for day in days:
        (d / f"date={day}").mkdir()
    rep = CM.report("2026-08-01", "2026-08-05", events_dir=d, read_day_fn=fn)
    assert rep["credit_filings"] == 1


# ------------------------------------------------------ event study

def test_forward_return_never_uses_the_event_days_own_bar():
    """THE look-ahead rule. Letting day D in would measure the reaction the
    study is trying to predict."""
    series = {"2026-08-03": 100.0, "2026-08-04": 110.0, "2026-08-05": 121.0}
    # base = the 08-03 close; +1 session = 08-04 = +10%
    assert ES.forward_return(series, "2026-08-03", 1) == 10.0
    # +2 sessions = 08-05 = +21%
    assert ES.forward_return(series, "2026-08-03", 2) == 21.0


def test_forward_return_is_None_when_the_window_does_not_fit():
    series = {"2026-08-03": 100.0, "2026-08-04": 110.0}
    assert ES.forward_return(series, "2026-08-03", 5) is None
    assert ES.forward_return(series, "2026-08-04", 1) is None   # nothing after
    assert ES.forward_return({}, "2026-08-03", 1) is None
    assert ES.forward_return(series, "2026-01-01", 1) is None   # no base close


def test_a_weekend_or_holiday_event_uses_the_last_prior_close():
    """Announcements land on non-trading days constantly. The base must be
    the last real close, not a missing bar."""
    series = {"2026-08-07": 100.0, "2026-08-10": 105.0}
    assert ES.forward_return(series, "2026-08-08", 1) == 5.0    # Saturday event


def test_the_keyword_scan_is_case_insensitive_and_quotes_the_headline(tmp_path):
    days = {"2026-08-03": [
        {"as_of": "2026-08-03", "symbol": "ACME", "ticker": "ACME.NS",
         "subject": "Update on ELECTION related disruption"},
        {"as_of": "2026-08-03", "symbol": "BETA", "ticker": "BETA.NS",
         "subject": "Record Date"}]}
    d = tmp_path / "events"
    d.mkdir()
    (d / "date=2026-08-03").mkdir()
    hits = ES.scan_events("election", "2026-08-01", "2026-08-05",
                          events_dir=d, read_day_fn=lambda day: days.get(day, []))
    assert len(hits) == 1
    assert hits[0]["symbol"] == "ACME"
    assert "ELECTION" in hits[0]["subject"]


def test_a_small_sample_is_labelled_insufficient_not_reported_as_a_finding(tmp_path):
    """n=1 with a +40% mean is exactly the number a research tool must
    refuse to let anyone quote."""
    days = {"2026-08-03": [{"as_of": "2026-08-03", "symbol": "ACME",
                            "ticker": "ACME.NS", "subject": "Downgrade"}]}
    d = tmp_path / "events"
    d.mkdir()
    (d / "date=2026-08-03").mkdir()
    rep = ES.run("Downgrade", start="2026-08-01", end="2026-08-05",
                 windows=(1,), events_dir=d,
                 read_day_fn=lambda day: days.get(day, []),
                 lake_dir=tmp_path / "no_bhavcopy")
    assert rep["events_matched"] == 1
    assert rep["results"]["fwd_1d"]["verdict"] == "insufficient_sample"


def test_no_matches_says_so_rather_than_returning_an_empty_average(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    rep = ES.run("Nonexistent", start="2026-08-01", end="2026-08-05",
                 events_dir=d, read_day_fn=lambda day: [],
                 lake_dir=tmp_path)
    assert rep["verdict"] == "no_events_matched"
    assert "nothing matched" in "\n".join(ES.render_lines(rep))


def test_the_report_always_carries_its_caveats(tmp_path):
    """Survivorship and the string-match limitation must travel WITH the
    numbers, not live in a docstring nobody re-reads."""
    d = tmp_path / "events"
    d.mkdir()
    rep = ES.run("x", start="2026-08-01", end="2026-08-05", events_dir=d,
                 read_day_fn=lambda day: [], lake_dir=tmp_path)
    joined = " ".join(rep["caveats"]).lower()
    assert "survivorship" in joined
    assert "p-value" in joined or "p-values" in joined


def test_stats_reports_n_zero_honestly_rather_than_dividing_by_nothing():
    s = ES._stats([None, None])
    assert s["n"] == 0 and s["mean_pct"] is None and s["hit_rate_pct"] is None


# ------------------------------- the lake-root bug (found by running it)

def test_the_default_reader_uses_the_LAKE_root_not_the_repo_root(tmp_path):
    """`lake.read_day(root=...)` takes the LAKE root (data/lake). Both
    modules originally passed `events_dir.parent.parent`, which resolves to
    `data/` — so the reader looked for `data/data/lake/events`, found
    nothing, and reported '703 partitions scanned, 0 filings' in total
    silence. A wrong path that returns [] instead of raising is the worst
    shape of bug this repo has; this test pins the fix."""
    import inspect
    for mod in (CM, ES):
        src = inspect.getsource(mod)
        assert "root=events_dir.parent)" in src, mod.__name__
        assert "events_dir.parent.parent)" not in src, mod.__name__


def test_the_reader_finds_rows_through_the_real_lake_layout(tmp_path):
    """End-to-end through `lake` itself, on a lake laid out exactly as the
    live one is — the check the injected `read_day_fn` cannot make."""
    from src import lake
    day = "2026-08-03"
    part = tmp_path / "lake" / "events" / f"date={day}"
    part.mkdir(parents=True)
    lake.write_partition("events", day,
                         [{"as_of": day, "symbol": "ACME", "ticker": "ACME.NS",
                           "subject": "Corporate Insolvency Resolution Process"}],
                         root=tmp_path / "lake")
    rep = CM.report(day, day, events_dir=tmp_path / "lake" / "events")
    assert rep["credit_filings"] == 1
    assert rep["by_category"] == {"INSOLVENCY": 1}


def test_a_serial_filer_is_summarised_as_counts_not_a_wall_of_text(tmp_path):
    """JPASSOCIAT filed 128 times over two years of insolvency. The first
    live run printed every one of them on a single line."""
    days = {f"2026-08-{d:02d}": [{"as_of": f"2026-08-{d:02d}", "symbol": "ZOMB",
                                  "subject": "Corporate Insolvency Resolution "
                                             "Process"}] for d in range(1, 21)}
    d, fn = _events(days, tmp_path)
    rep = CM.report("2026-08-01", "2026-08-25", events_dir=d, read_day_fn=fn)
    info = rep["symbols_with_negative_credit_news"]["ZOMB"]
    assert info == {"total": 20, "by_category": {"INSOLVENCY": 20}}
    line = [l for l in CM.render_lines(rep) if "ZOMB" in l][0]
    assert "INSOLVENCY x20" in line and len(line) < 120
