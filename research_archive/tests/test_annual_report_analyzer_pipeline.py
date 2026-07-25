"""
ARCHIVED with research_archive/analysis/annual_report_analyzer.py (Phase-1
cleanup, 2026-07-25). The analyzer half of tests/test_report_pipeline.py,
extracted verbatim; the live report_downloader half stays in tests/.
Imports point at the OLD src path — frozen reference, not collected by pytest.
"""
import json

from src.analysis import annual_report_analyzer as A      # noqa: archived path

def test_extract_pages_strips_nul_bytes(monkeypatch):
    """A corrupted font/encoding table in some real filings makes pypdf
    emit NUL bytes in extract_text() — left in, a single NUL anywhere in
    the condensed corpus file makes grep (and other line-based text
    tools) silently treat the whole file as binary and match nothing.
    Real incident, 2026-07-18 (a small-cap filing)."""
    class FakePage:
        def extract_text(self_):
            return "loan to holding\x00 company at 13.5% interest"

    class FakeReader:
        def __init__(self_, path):
            self_.pages = [FakePage()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    pages = A.extract_pages("fake.pdf")
    assert "\x00" not in pages[0]
    assert pages[0] == "loan to holding company at 13.5% interest"


# ------------------------------------------------- section-aware condenser

def _page(header, body_words=200, filler="operations continued steadily "):
    return header + "\n" + filler * (body_words // 3)


def test_tag_pages_headers_ligatures_and_boilerplate_precedence():
    pages = [
        _page("Chairman's message to shareholders"),
        _page("Independent Auditor's Report on the standalone accounts"),
        _page("NOTES forming part of the consolidated ﬁnancial statements"),
        _page("Business Responsibility & Sustainability Report"),
        # boilerplate phrase AND a section hit -> the section must win
        _page("Related party disclosures incl. corporate social "
              "responsibility contribution"),
    ]
    tags = dict(A.tag_pages(pages))
    assert tags[1] is None
    assert tags[2] == "auditor"
    assert tags[3] == "notes"            # the ﬁ ligature must not blind us
    assert tags[4] is None               # pure boilerplate dropped
    assert tags[5] == "related"          # section beats boilerplate


def test_condense_keeps_dense_pages_marks_them_and_respects_budget():
    loud = _page("Contingent liabilities and commitments",
                 filler="guarantee given to holding company loan ")
    quiet = _page("Contingent liabilities note continued")
    pages = [loud, quiet, _page("plain page")]
    c = A.condense(pages, budget=len(loud) + 120)   # room for ~one block
    assert c["kept_page_numbers"] == [1]            # densest page wins
    assert "[PAGE 1]" in c["corpus"]
    assert c["pages_total"] == 3


def test_condense_rollover_spends_unused_section_budget():
    # One tiny auditor page + many dense related pages: without rollover
    # the related share caps at its slice; with it, leftovers get spent.
    auditor = _page("Independent Auditor's Report", body_words=30)
    related = [_page(f"Related party transactions part {i}",
                     filler="loan to holding company interest rate ")
               for i in range(6)]
    pages = [auditor] + related
    budget = sum(len(p) for p in pages) + 700       # fits everything
    c = A.condense(pages, budget=budget)
    assert c["pages_kept"] == len(pages)


def test_chunk_corpus_splits_on_page_markers():
    corpus = "".join(f"[PAGE {i}] (notes)\n" + "x" * 3000 + "\n"
                     for i in range(1, 8))
    chunks = A.chunk_corpus(corpus, chunk_chars=8000)
    assert len(chunks) >= 3
    assert all(ch.startswith("[PAGE ") for ch in chunks)
    assert sum(len(c) for c in chunks) == len(corpus)


# ---------------------------------------------------- the honesty validator

PAGES = ["", "The company gave a loan of Rs.3,631 crore to its parent "
             "at an interest rate of 13.5% per annum.", ""]


def test_validator_keeps_verbatim_and_drops_fabrication():
    findings = [
        {"finding": "parent loan", "page": 2,
         "quote": "loan of Rs.3,631 crore to its parent"},
        {"finding": "invented", "page": 2,
         "quote": "the auditors resigned in protest"},        # not on page
        {"finding": "wrong page", "page": 3,
         "quote": "loan of Rs.3,631 crore to its parent"},    # +/-1 ok
        {"finding": "no such page", "page": 99, "quote": "loan"},
        {"finding": "empty quote", "page": 2, "quote": ""},
    ]
    v = A.validate_findings(findings, PAGES)
    kept_names = {f["finding"] for f in v["kept"]}
    assert kept_names == {"parent loan", "wrong page"}
    assert v["dropped_unverified"] == 3


def test_score_is_mechanical_and_flag_heavy():
    assert A.score(0, 0, 0) == 0.50
    assert A.score(5, 1, 0) == 0.73
    assert A.score(5, 1, 6) == 0.25          # flags outweigh wins
    assert A.score(0, 0, 20) == 0.0          # clamped


# ------------------------------------------------------------- end-to-end

class FakeExtractor:
    """chat_json-shaped; returns one real finding + one hallucination, and
    crashes on the second chunk to prove per-chunk fail-open."""

    def __init__(self):
        self.calls = 0

    def chat_json(self, system, text):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("model fell over")
        return {"operational_wins": [],
                "shareholder_returns": [],
                "hidden_risks_and_flags": [
                    {"finding": "parent loan at concessional rate",
                     "quote": "loan of Rs.3,631 crore to its parent",
                     "page": 2},
                    {"finding": "hallucinated auditor resignation",
                     "quote": "the auditors resigned in protest", "page": 2},
                ]}


def test_analyze_end_to_end_offline(tmp_path):
    pages = [
        _page("Independent Auditor's Report",
              filler="key audit matter loan guarantee "),
        PAGES[1] + " Related party disclosures follow. " +
        "loan interest rate guarantee " * 30,
    ]
    fake = FakeExtractor()
    r = A.analyze("fake.pdf", ticker="VEDL", fiscal_year="FY25",
                  extractor=fake, pages=pages, out_dir=tmp_path)
    assert fake.calls >= 1
    flags = {f["finding"] for f in r["hidden_risks_and_flags"]}
    assert "parent loan at concessional rate" in flags
    assert "hallucinated auditor resignation" not in flags     # dropped
    assert r["evidence_discipline"]["findings_dropped_unverified"] >= 1
    assert 0.0 <= r["net_conviction_score"] <= 1.0
    written = json.loads((tmp_path / "VEDL" / "FY25.json").read_text())
    assert written["ticker"] == "VEDL"
    assert written["score_method"].startswith("mechanical_v1")
