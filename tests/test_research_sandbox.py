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


def test_a_sign_disagreement_between_mean_and_median_is_flagged():
    """From the first full-history run: 'Suspension of Trading' returned
    mean +554% against median -2.82% because one delisted shell ran 20x.
    The sample-size gate does not catch this — n=22 passed it at the 5-day
    window and still reported a +68% mean."""
    skewed = ES._stats([-3.0, -2.0, -4.0, -1.0, 2000.0])
    assert skewed["mean_median_diverge"] is True
    assert skewed["median_pct"] < 0 < skewed["mean_pct"]
    clean = ES._stats([-3.0, -2.0, -4.0, -1.0])
    assert clean["mean_median_diverge"] is False


def test_the_render_leads_with_the_median_and_warns_on_skew():
    rep = {"keyword": "x", "from": "a", "to": "b", "events_matched": 1,
           "distinct_symbols": 1, "benchmark": "NIFTY 50",
           "coverage": {"symbols_with_price_history": 1,
                        "benchmark_series_available": False},
           "results": {"fwd_5d": ES._stats([-3.0, -2.0, -4.0, -1.0, 2000.0])}}
    text = "\n".join(ES.render_lines(rep))
    assert text.index("median") < text.index("mean")
    assert "disagree in sign" in text


def test_the_caveats_name_the_skew_and_the_upward_survivorship_bias(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    rep = ES.run("x", start="2026-08-01", end="2026-08-05", events_dir=d,
                 read_day_fn=lambda day: [], lake_dir=tmp_path)
    joined = " ".join(rep["caveats"]).lower()
    assert "right-skewed" in joined or "skew" in joined
    assert "upward" in joined


# ------------------------------- macro shocks + dated study (2026-08-16)

from src.ingestion.sandbox import macro_shocks_v2 as MS
from src.research import geo_revenue_extractor as GX

ONI_SAMPLE = """ SEAS  YR   TOTAL   ANOM
  JJA 2023  27.50   1.00
  JAS 2023  27.80   1.25
  JJA 2021  26.10  -0.40
  DJF 2016  27.90   2.50
"""


def test_oni_parses_and_a_neutral_reading_is_never_manufactured():
    rows = MS.parse_oni(ONI_SAMPLE + "  BAD row here\n")
    assert len(rows) == 4
    assert all(isinstance(r["anom"], float) for r in rows)


def test_el_nino_uses_NOAAs_own_threshold_and_the_monsoon_seasons_only():
    """Redefining the cutoff and still calling it 'El Nino' would be
    quietly renaming the term; DJF 2016 was a monster event but it is not
    a MONSOON season, so it must not enter a monsoon study."""
    rows = MS.parse_oni(ONI_SAMPLE)
    hits = MS.el_nino_seasons(rows)
    assert {h["season"] for h in hits} == {"JJA", "JAS"}
    assert all(h["anom"] >= MS.EL_NINO_THRESHOLD for h in hits)
    assert "2016" not in " ".join(h["date"] for h in hits)


def test_the_election_calendar_is_now_SOURCED_not_hand_typed():
    """It shipped empty on 2026-08-11 because typing dates from memory
    would have been fabrication. It is populated on 2026-08-16 — from the
    Wikipedia API, with `source_page` on every row so any date is
    traceable. The rule never changed; the sourcing did."""
    rows = MS.load_elections()
    assert len(rows) >= 25
    raw = json.loads((ROOT / "config" / "india_election_calendar.json").read_text())
    assert "FABRICATED" in raw["_why_empty"]          # the reasoning is kept
    assert raw["_sourced"]["source"].startswith("Wikipedia API")
    assert "SECONDARY SOURCE" in raw["_sourced"]["caveat"]


def test_the_geo_map_is_the_join_key_for_a_shock():
    assert "HINDUNILVR.NS" in MS.tickers_for_shock("monsoon")
    assert "TATAMOTORS.NS" in MS.tickers_for_region("United Kingdom")


def test_a_dated_study_needs_distinct_EVENT_DATES_not_just_ticker_days():
    """200 ticker-days off 2 event dates is one fortnight wearing a large
    n. Both gates must hold."""
    events = [{"date": "2026-08-03"}, {"date": "2026-08-04"}]
    rep = ES.run_dates(events, windows=(1,), tickers=["A.NS"],
                       lake_dir=ROOT / "does_not_exist")
    assert rep["distinct_event_dates"] == 2
    assert rep["results"]["fwd_1d"]["verdict"] == "insufficient_sample"
    assert rep["results"]["fwd_1d"]["gate"] == "too few distinct event dates"


def test_a_dated_study_names_its_clustering():
    rep = ES.run_dates([{"date": "2026-08-03"}], tickers=["A.NS"],
                       lake_dir=ROOT / "nope")
    assert any("CLUSTERED" in c for c in rep["caveats"])


def test_only_a_QUANTIFIED_geography_line_counts_as_evidence():
    """A marked page is not a disclosure. MD&A narrative and percentage
    charts hit the markers without carrying the segment table."""
    assert GX.is_quantified("- Within India 72988.91 65637.28")
    assert GX.is_quantified("India 15,775 22,060")
    assert not GX.is_quantified("States and Europe, where specialty therapies form")
    assert not GX.is_quantified("Life Sciences APAC 8.3%")


def test_the_extractor_never_touches_an_india_state_row(tmp_path):
    """No annual report discloses revenue by Indian state. An extractor
    that overwrote a state estimate would be claiming a number it cannot
    possibly have found."""
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps({"exposures": {"X.NS": {"exposures": [
        {"region": "Uttar Pradesh", "kind": "india_state", "share_pct": None,
         "driver": "d", "confidence": "low", "source": "hypothesis"},
        {"region": "United States", "kind": "country", "share_pct": None,
         "driver": "d", "confidence": "low", "source": "hypothesis"}]}}}))
    GX.apply_evidence([{"ticker": "X.NS", "status": "geography_note_found",
                        "source_file": "AR.pdf", "marked_pages": [220],
                        "hits": [{"page": 220, "line": "Within India 72988.91"}]}],
                      geo_path=geo)
    rows = json.loads(geo.read_text())["exposures"]["X.NS"]["exposures"]
    state = [r for r in rows if r["kind"] == "india_state"][0]
    country = [r for r in rows if r["kind"] == "country"][0]
    assert state["confidence"] == "low" and state["source"] == "hypothesis"
    assert country["confidence"] == "high" and "EXTRACTED" in country["source"]


def test_unquantified_hits_leave_the_hand_estimate_alone(tmp_path):
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps({"exposures": {"X.NS": {"exposures": [
        {"region": "Europe", "kind": "country", "share_pct": None,
         "driver": "d", "confidence": "medium", "source": "unverified"}]}}}))
    out = GX.apply_evidence([{"ticker": "X.NS", "status": "geography_note_found",
                              "source_file": "AR.pdf", "marked_pages": [32],
                              "hits": [{"page": 32, "line": "States and Europe, "
                                                            "where specialty"}]}],
                            geo_path=geo)
    assert out["country_rows_evidenced"] == 0
    row = json.loads(geo.read_text())["exposures"]["X.NS"]["exposures"][0]
    assert row["confidence"] == "medium" and row["source"] == "unverified"


def test_the_metals_ids_are_verified_and_steel_is_absent_on_purpose():
    raw = json.loads((ROOT / "config" / "macro_securities.json").read_text())
    for m in ("COPPER", "ALUMINIUM", "ZINC"):
        assert raw[m]["seg"] == "MCX_COMM" and raw[m]["inst"] == "FUTCOM"
        assert raw[m]["id"].isdigit() and raw[m]["_expiry"]
    assert "STEEL" not in raw
    assert "STEELREBAR" in raw["_verified_metals"]
    from src.ingestion.cross_asset import COMMODITY_KEYS
    assert set(COMMODITY_KEYS) == {"CRUDE", "GOLD_INDIA", "COPPER",
                                   "ALUMINIUM", "ZINC"}


# ------------------------- deep history / steel proxy / elections (08-16)

from src.ingestion.sandbox import deep_history as DH
from src.ingestion.sandbox import election_calendar as EC


def test_the_steel_proxy_is_labelled_a_PROXY_not_a_price():
    """SLX/MT are equities that co-move with steel. Recording them as a
    steel price would let an ETF close be mistaken for an HRC quote."""
    assert DH.DEEP_SERIES["SLX"][1] == "steel_proxy"
    assert DH.DEEP_SERIES["MT"][1] == "steel_proxy"
    from src.ingestion.cross_asset import COMMODITY_KEYS
    # And it must NOT be in the Dhan door — one market-data door per source.
    assert not any(k in COMMODITY_KEYS for k in ("SLX", "MT", "STEEL"))


def test_the_monsoon_year_is_judged_on_its_PEAK_not_its_mean():
    """Averaging JJA with JAS blunts exactly the years that matter."""
    oni = [{"date": "2015-07-01", "season": "JJA", "year": 2015, "anom": 1.44},
           {"date": "2015-08-01", "season": "JAS", "year": 2015, "anom": 1.73},
           {"date": "2015-01-01", "season": "DJF", "year": 2015, "anom": 2.5}]
    peaks = DH.monsoon_years(oni)
    assert peaks == {2015: 1.73}          # DJF excluded, peak not mean


def test_a_season_return_is_None_rather_than_a_shortened_window():
    """A 3-month return and a 6-month return are not the same measurement."""
    series = {"2020-07-01": 100.0}        # no December close
    assert DH.season_return(series, 2020) is None


def test_three_el_nino_years_is_reported_as_insufficient():
    """The dated study's n counts ticker-days; this one counts monsoons.
    Three monsoons is three observations however many rows they generate."""
    s = DH._stats([-1.0, -2.0, -3.0])
    assert s["n"] == 3 and s["median_pct"] == -2.0 and s["hit_rate_pct"] == 0.0


def test_a_multi_phase_election_records_the_LAST_poll_date():
    """'27 March – 29 April 2021' is ONE election polled over five weeks.
    The market event is the resolution, not the first phase."""
    assert EC.last_poll_date("27 March – 29 April 2021 (292 seats)") == "2021-04-29"
    assert EC.last_poll_date("10 February 2022") == "2022-02-10"
    assert EC.last_poll_date("no dates here") is None


def test_by_elections_are_excluded_from_the_calendar():
    """A by-election fills a few seats and changes no government; counting
    it as a full assembly poll pads n with non-events."""
    members = {"query": {"categorymembers": [
        {"title": "2019 Haryana Legislative Assembly election"},
        {"title": "2019 Kerala Legislative Assembly by-elections"},
        {"title": "Category:2019 elections"}]}}
    got = EC.election_pages(2019, fetch_fn=lambda p: members)
    assert got == ["2019 Haryana Legislative Assembly election"]


def test_every_election_row_is_traceable_and_flagged_unverified():
    raw = json.loads((ROOT / "config" / "india_election_calendar.json").read_text())
    assert len(raw["elections"]) >= 25
    for r in raw["elections"]:
        assert r["source_page"], r
        assert r["verified_against_eci"] is False
        assert r["kind"] == "state_assembly"
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", r["date"])


def test_operational_state_rows_are_never_confused_with_revenue():
    """The map now carries two kinds of state row. Reading an operational
    presence as a revenue share would be a category error."""
    raw = json.loads((ROOT / "config" / "geo_revenue_exposure.json").read_text())
    ops = [e for b in raw["exposures"].values() for e in b["exposures"]
           if e.get("basis") == "operational_presence"]
    assert ops, "plant extraction never applied"
    for e in ops:
        assert e["kind"] == "india_state"
        assert e["share_pct"] is None      # presence is not a revenue share
        assert "EXTRACTED" in e["source"]


def test_the_plant_extractor_gates_on_repeated_mentions(tmp_path):
    """A state named once on one page is as likely a CSR sentence as a
    plant."""
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps({"exposures": {"X.NS": {"exposures": []}}}))
    out = GX.apply_plant_states(
        [{"ticker": "X.NS", "status": "states_found", "source_file": "AR.pdf",
          "marked_pages": [10], "states": {"Gujarat": 4, "Kerala": 1}}],
        geo_path=geo, min_mentions=2)
    rows = json.loads(geo.read_text())["exposures"]["X.NS"]["exposures"]
    assert out["state_rows_added"] == 1
    assert [r["region"] for r in rows] == ["Gujarat"]
