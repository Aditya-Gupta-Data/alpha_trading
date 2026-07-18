"""
Department 8 — the annual-report analyzer against its GROUND TRUTH.

Two tiers:

  * Synthetic tests (always run, offline): the quality-of-earnings
    vocabulary added for the small-cap cohort — capitalized development
    costs, unbilled revenue, order book, the post-2023 audit-trail
    exception — must tag and outrank generic pages, deterministically.

  * Benchmark-corpus tests (Mac-only; SKIP where the Desktop PDFs or
    lake benchmarks are absent, e.g. the VM): the condenser must keep
    the needle pages that the human deep-reads of Azad Engineering FY25,
    Jupiter Wagons FY25 and eMudhra FY26 actually cited — read
    dynamically from the benchmark JSONs in the lake, so tightening the
    benchmarks tightens the tests. Measured state when written:
    16/16 needles kept across these three reports at the 480k budget
    (the corpus-wide figure incl. the older reports is 28/29 — VEDL
    p291 is the documented permanent residual).

  NOT asserted here, by ruling: the analyst conviction scores (72/64/55).
  Those live on a human 0-100 judgment scale; the pipeline's mechanical
  0-1 score counts validated findings and depends on the LLM stage —
  asserting one against the other would be a category error and flaky
  besides. The cohort matrix shows both, side by side, unmerged.
"""
import glob
import json
from pathlib import Path

import pytest

from src.analysis import annual_report_analyzer as A
from src.analysis import cohort_comparator as CC

REPORTS_GLOB = "/Users/adityagupta/Desktop/annual reports/{}"
LAKE = Path("data/lake/fundamental_reports")

BENCH = [
    ("AZAD", "FY25", "AR_28933_AZAD_2024_2025*"),
    ("JWL", "FY25", "AR_27897_JWL_2024_2025*"),
    ("EMUDHRA", "FY26", "AR_29275_EMUDHRA_2025_2026*"),
]


def _pdf(pattern):
    hits = glob.glob(REPORTS_GLOB.format(pattern))
    return hits[0] if hits else None


def _needle_pages(ticker, fy):
    data = json.loads((LAKE / ticker / f"{fy}.json").read_text())
    pages = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("page"), int):
                pages.append(x["page"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(data)
    return set(pages)


corpus_present = all(_pdf(g) and (LAKE / t / f"{fy}.json").exists()
                     for t, fy, g in BENCH)


# ------------------------------------------- synthetic: the QoE vocabulary

def test_capitalized_rnd_and_unbilled_revenue_are_heavy_signals():
    qoe = ("Product development expenditure of Rs.476 Mn was capitalised "
           "as intangible assets under development; data centre costs of "
           "Rs.213 Mn were also capitalised. Unbilled revenue grew to "
           "Rs.1,035 Mn from Rs.736 Mn.")
    generic = "The company continued its operations across its segments."
    assert len(A.FORENSIC_HEAVY.findall(qoe)) >= 3      # capitalised x2 + unbilled
    assert len(A.FORENSIC_HEAVY.findall(generic)) == 0
    assert len(A.FORENSIC_DENSITY.findall("order book of Rs.6,080 crore")) >= 1


def test_qoe_page_outranks_generic_filler_in_condensation():
    filler = "Notes forming part of the financial statements\n" + \
        "The segment performed in line with expectations. " * 40
    qoe_page = "Notes forming part of the financial statements\n" + \
        "Unbilled revenue increased 41%. Product development costs were " \
        "capitalised as intangible assets. " * 20
    pages = [filler, qoe_page, filler]
    c = A.condense(pages, budget=len(qoe_page) + 400)
    assert c["kept_page_numbers"] == [2]


def test_audit_trail_exception_page_is_tagged_auditor_without_a_header():
    page = ("v. The Company has neither declared nor paid any dividend. "
            "vi. The accounting software did not have the audit trail "
            "(edit log) facility enabled at the database level.")
    tags = dict(A.tag_pages([page]))
    assert tags[1] == "auditor"


def test_revenue_recognition_kam_vocabulary_counts():
    kam = ("Key Audit Matter: revenue recognition — the risk that revenue "
           "is overstated given management incentives.")
    assert len(A.FORENSIC_HEAVY.findall(kam)) >= 1
    assert len(A.FORENSIC_DENSITY.findall(kam)) >= 2


# ---------------------------------- benchmark corpus (Mac-only, skips clean)

@pytest.mark.skipif(not corpus_present,
                    reason="benchmark PDFs/lake JSONs not on this machine")
@pytest.mark.parametrize("ticker,fy,pattern", BENCH,
                         ids=[b[0] for b in BENCH])
def test_condenser_keeps_every_benchmark_needle_page(ticker, fy, pattern):
    truth = _needle_pages(ticker, fy)
    assert truth, f"benchmark JSON for {ticker} cites no pages"
    pages = A.extract_pages(_pdf(pattern))
    kept = set(A.condense(pages)["kept_page_numbers"])
    missing = truth - kept
    assert not missing, (f"{ticker} {fy}: benchmark needle pages {sorted(missing)} "
                         f"lost in condensation")


@pytest.mark.skipif(not corpus_present,
                    reason="benchmark PDFs/lake JSONs not on this machine")
def test_named_needles_from_the_owner_brief_survive():
    """The specific pages the owner's brief called out by name."""
    for ticker, fy, pattern, must_keep in [
        ("AZAD", "FY25", "AR_28933_AZAD_2024_2025*", {12, 14}),
        ("JWL", "FY25", "AR_27897_JWL_2024_2025*", {10, 186}),
        ("EMUDHRA", "FY26", "AR_29275_EMUDHRA_2025_2026*", {154}),
    ]:
        kept = set(A.condense(A.extract_pages(_pdf(pattern)))["kept_page_numbers"])
        assert must_keep <= kept, f"{ticker}: {sorted(must_keep - kept)} lost"


# --------------------------------------------------- the cohort comparator

def _bench_json(score, flags=2, positives=3):
    return {"conviction_score": score, "verdict": "HOLD",
            "red_flags": [{}] * flags, "hidden_debt_flags": [],
            "yellow_flags": [{}], "guidance_and_positives": [{}] * positives}


def _auto_json(score, wins=4, flags=1, dropped=2):
    return {"net_conviction_score": score,
            "operational_wins": [{}] * wins,
            "shareholder_returns": [],
            "hidden_risks_and_flags": [{}] * flags,
            "evidence_discipline": {"findings_dropped_unverified": dropped}}


def _seed(root, sub, ticker, fy, payload):
    d = root / sub / ticker
    d.mkdir(parents=True)
    (d / f"{fy}.json").write_text(json.dumps(payload))


def test_cohort_matrix_merges_both_schemas_without_rescaling(tmp_path):
    _seed(tmp_path, "bench", "AZAD", "FY25", _bench_json(72))
    _seed(tmp_path, "bench", "JWL", "FY25", _bench_json(64))
    _seed(tmp_path, "auto", "AZAD", "FY25", _auto_json(0.62))
    m = CC.run(bench_dir=tmp_path / "bench", auto_dir=tmp_path / "auto",
               matrix_path=tmp_path / "matrix.md")
    assert m["n_benchmarked"] == 2 and m["n_automated"] == 1 and m["n_both"] == 1
    azad = next(r for r in m["rows"] if r["ticker"] == "AZAD")
    assert azad["analyst_score"] == 72 and azad["auto_score"] == 0.62
    md = (tmp_path / "matrix.md").read_text()
    assert "| AZAD | FY25 | 72 |" in md and "0.62" in md
    assert "DIFFERENT instruments" in md          # the no-rescaling contract
    jwl = next(r for r in m["rows"] if r["ticker"] == "JWL")
    assert "auto_score" not in jwl                # honest absence, no guess


def test_cohort_broadcast_fires_once_and_is_optional(tmp_path):
    _seed(tmp_path, "bench", "AZAD", "FY25", _bench_json(72))
    cards = []
    m = CC.run(bench_dir=tmp_path / "bench", auto_dir=tmp_path / "auto",
               matrix_path=tmp_path / "matrix.md",
               broadcast=True, broadcast_fn=cards.append)
    assert m["broadcast"] is True and len(cards) == 1
    assert cards[0]["event"] == "research_cohort"
    assert "AZAD 72" in cards[0]["description"]

    quiet = CC.run(bench_dir=tmp_path / "bench", auto_dir=tmp_path / "auto",
                   matrix_path=tmp_path / "matrix.md")
    assert "broadcast" not in quiet and len(cards) == 1


def test_cohort_handles_missing_dirs_honestly(tmp_path):
    m = CC.run(bench_dir=tmp_path / "nope", auto_dir=tmp_path / "nada",
               matrix_path=tmp_path / "matrix.md")
    assert m["rows"] == [] and m["n_both"] == 0
    assert (tmp_path / "matrix.md").exists()
