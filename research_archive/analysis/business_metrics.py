"""
src/analysis/business_metrics.py — the 'darling' reader (Dept 8, advisory)
==========================================================================

The SECOND report produced from every annual report we read. Deliberately a
SEPARATE module, corpus, schema and lake folder from the forensic pipeline:

    annual_report_analyzer.py  -> data/lake/fundamental_reports/   (FORENSIC)
    business_metrics.py        -> data/lake/business_metrics/      (DARLING)

Owner directive 2026-07-19: "ek forensic ek dusri, jisse forensic jo sahi
chal rahi hai usme ungli na ho." The forensic pipeline is proven across 240+
reports — this module NEVER modifies it. It only imports `extract_pages`
(the expensive pypdf step, run once and cached) and `validate_findings`
(the verbatim-quote guard, reused unchanged).

WHAT THIS HUNTS — the things a screener/yfinance cannot give you, that only
the annual report has:

  1. SCHEDULE III RATIOS — mandatory in every Indian annual report since FY22
     and STANDARDISED, so they are directly comparable across companies:
     current ratio, debt-equity, DSCR, ROE, inventory/debtors/payables
     turnover, net capital turnover, net profit ratio, ROCE, RoI — each with
     the prior year AND a compulsory management explanation of any >25% move.
  2. FORWARD VISIBILITY — order book / backlog (and book-to-revenue), capital
     commitments ("contracts remaining to be executed on capital account"),
     capacity/volume (MT, MW, GW, stores, beds), management guidance.
  3. DURABILITY — credit rating and upgrades, customer/segment concentration,
     segment mix shift (premiumisation, export share), R&D intensity.

SAME EVIDENCE DISCIPLINE as the forensic side: every extracted number carries
a verbatim quote + extracted page index, checked by `validate_findings`. No
quote, no field. Derived figures (e.g. book-to-revenue) are marked
`"derived": true` so they are never mistaken for quoted facts. Missing stays
MISSING — never 0 (a bank has no order book; that is not a zero).

Advisory-only, like everything in Dept 8: writes its own lake, never
brain_map.db, never sizes or approves a trade.

CLI:
    python3 -m src.analysis.business_metrics <pdf> --ticker T --fy FY26 --dry-run
"""
import re
from pathlib import Path

# Reuse — never modify — the forensic module's proven primitives.
from src.analysis.annual_report_analyzer import (
    extract_pages,            # expensive pypdf pass (cached by callers)
    validate_findings,        # the verbatim-quote guard
    _clean,                   # ligature/whitespace normaliser
    MIN_PAGE_CHARS,
    PAGE_CHAR_CAP,
)

ROOT = Path(__file__).resolve().parent.parent.parent
LAKE_DIR = ROOT / "data" / "lake" / "business_metrics"

CONDENSE_CHAR_BUDGET = 420_000     # its own budget; forensic's is untouched
CHUNK_CHARS = 8_000

# ---------------------------------------------------------------- sections
# Business-side sections, in the priority the budget is spent. Deliberately a
# DIFFERENT set from the forensic module's (which prioritises auditor/notes).
SECTION_PATTERNS = (
    ("ratios",    re.compile(r"(?:analytical ratios|key financial ratios|"
                             r"ratio analysis|key ratios)", re.I)),
    ("segment",   re.compile(r"segment (?:report|information|results|revenue)", re.I)),
    ("mda",       re.compile(r"management discussion (?:and|&) analysis", re.I)),
    ("directors", re.compile(r"(?:directors'?|board'?s) report", re.I)),
    ("ops",       re.compile(r"(?:operational|business|performance) (?:highlights|review|overview)", re.I)),
)
SECTION_BUDGET_SHARE = {"ratios": 0.22, "segment": 0.20, "mda": 0.28,
                        "directors": 0.18, "ops": 0.12}
SECTION_RUN_CAP = {"ratios": 6, "segment": 10, "mda": 40, "directors": 25, "ops": 12}

# The Schedule III ratio table is the single highest-value page in the report:
# standardised and comparable across every Indian filer. A page carrying 3+ of
# these labels IS that table and must survive condensation.
SCHEDULE_III_LABELS = (
    "current ratio", "debt-equity", "debt equity", "debt service coverage",
    "return on equity", "inventory turnover", "trade receivables turnover",
    "trade payables turnover", "net capital turnover", "net profit ratio",
    "return on capital employed", "return on investment",
)

# General business vocabulary — what makes a page worth keeping.
BUSINESS_DENSITY = re.compile(
    r"order book|backlog|capacity|utilisation|utilization|installed|"
    r"commissioned|credit rating|rating (?:upgrade|revised|assigned)|"
    r"guidance|outlook|segment revenue|market share|volume|"
    r"capital commitment|contracts remaining to be executed|"
    r"return on (?:capital employed|equity)|ebitda|margin|"
    r"revenue from operations|expansion|greenfield|brownfield|"
    r"research and development|per share|realisation|realization", re.I)

# Transactional/forward phrases count 4x — one order-book sentence buried in a
# page of narrative must outrank a page that says "margin" twenty times.
BUSINESS_HEAVY = re.compile(
    r"order book|backlog|capital commitment|"
    r"contracts remaining to be executed|credit rating|"
    r"capacity utilisation|capacity utilization|"
    r"return on capital employed|guidance|"
    r"installed capacity|market share", re.I)
HEAVY_WEIGHT = 4
SCHEDULE_III_BONUS = 40            # dwarfs everything: never drop the ratio table


def _is_schedule_iii(text: str) -> bool:
    t = _clean(text).lower()
    return sum(1 for lab in SCHEDULE_III_LABELS if lab in t) >= 3


def tag_pages(pages: list) -> list:
    """[(page_no_1based, section_or_None), ...] for the BUSINESS sections.
    A Schedule III ratio page is always tagged 'ratios' wherever it appears —
    it is frequently deep inside the notes with no section header of its own."""
    tags = []
    current, run_left = None, 0
    for i, text in enumerate(pages):
        head = _clean(text[:2500] if text else "")
        hit = next((name for name, pat in SECTION_PATTERNS if pat.search(head)), None)
        if hit is None and _is_schedule_iii(text or ""):
            hit = "ratios"
        if hit:
            current, run_left = hit, SECTION_RUN_CAP[hit]
        elif run_left > 0:
            run_left -= 1
        else:
            current = None
        tags.append((i + 1, current))
    return tags


def condense_business(pages: list, budget: int = CONDENSE_CHAR_BUDGET) -> dict:
    """The business corpus: same mechanics as the forensic condenser but its
    own sections and vocabulary, so the two never compete for budget. Pages
    carry [PAGE n] markers so every quote stays traceable."""
    tags = tag_pages(pages)
    by_section = {name: [] for name, _ in SECTION_PATTERNS}
    for page_no, section in tags:
        if section:
            by_section[section].append(page_no)

    def _density(page_no):
        text = _clean(pages[page_no - 1] or "")
        score = (len(BUSINESS_DENSITY.findall(text))
                 + HEAVY_WEIGHT * len(BUSINESS_HEAVY.findall(text)))
        if _is_schedule_iii(pages[page_no - 1] or ""):
            score += SCHEDULE_III_BONUS
        return score

    def _block(page_no, name):
        text = (pages[page_no - 1] or "").strip()[:PAGE_CHAR_CAP]
        if len(text) < MIN_PAGE_CHARS:
            return None
        return f"[PAGE {page_no}] ({name})\n{text}\n"

    blocks, kept, leftover_budget, leftover = [], [], 0, []
    for name, _ in SECTION_PATTERNS:
        section_budget = int(budget * SECTION_BUDGET_SHARE[name])
        spent = 0
        for page_no in sorted(by_section[name], key=_density, reverse=True):
            block = _block(page_no, name)
            if block is None:
                continue
            if spent + len(block) > section_budget:
                leftover.append((page_no, name))
                continue
            blocks.append((page_no, block))
            spent += len(block)
            kept.append(page_no)
        leftover_budget += max(0, section_budget - spent)

    for page_no, name in sorted(leftover, key=lambda t: _density(t[0]), reverse=True):
        if page_no in kept:
            continue
        block = _block(page_no, name)
        if block is None or len(block) > leftover_budget:
            continue
        blocks.append((page_no, block))
        leftover_budget -= len(block)
        kept.append(page_no)

    blocks.sort()
    ratio_pages = [p for p, _ in tags if p and _is_schedule_iii(pages[p - 1] or "")]
    return {
        "corpus": "".join(b for _, b in blocks),
        "pages_total": len(pages),
        "pages_kept": len(kept),
        "kept_page_numbers": sorted(set(kept)),
        "schedule_iii_pages": ratio_pages,       # the comparable-ratio goldmine
        "section_page_counts": {k: len(v) for k, v in by_section.items()},
    }


# ------------------------------------------------------------- output shape
SCHEMA_HINT = {
    "ticker": "", "doc_type": "annual_report", "fiscal_year": "",
    "source_file": "", "pages": 0, "analyzed_on": "", "analyst": "",
    # every metric below: {"value":…, "unit":…, "quote":"verbatim", "page":N}
    # omit the key entirely when absent — NEVER 0, NEVER null-as-zero.
    "order_book": {}, "book_to_revenue": {},        # derived -> "derived": true
    "capital_commitments": {}, "capacity": [],
    "schedule_iii_ratios": {},                      # {ratio_name: {curr, prior, quote, page}}
    "ratio_variance_notes": [],                     # management's >25% explanations
    "credit_rating": {}, "concentration": [], "segment_mix": [],
    "guidance": [], "rnd": {},
    "darling_read": "",                             # one paragraph, the business verdict
    "confidence": "", "caveats": [],
}

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--fy", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="condense + report sizes only")
    args = ap.parse_args()
    pg = extract_pages(args.pdf)
    c = condense_business(pg)
    print(json.dumps({k: c[k] for k in c if k != "corpus"}, indent=2))
    print(f"business corpus chars: {len(c['corpus'])}")
