"""
src/analysis/annual_report_analyzer.py — the 'Harshad Mehta' pipeline core
==========================================================================

Department 8 (the research desk). Forensic reading of annual reports the
way a human analyst does it — MD&A, Notes to Accounts, Contingent
Liabilities, Related Party Transactions, Auditor's Report — hunting BOTH
directions with one rule: every finding carries a verbatim quote and the
page it came from, or it does not exist.

The pipeline, per document:

  1. EXTRACT   — pypdf text per page (lazy import; Mac-side dependency,
                 deliberately NOT in requirements.txt — the VM never runs
                 this). Image-only pages are counted, never guessed.
  2. SECTIONS  — regex header detection tags each page into the
                 load-bearing sections; CSR/BRSR boilerplate is dropped.
                 A 300-page report condenses to a dense, page-marked
                 corpus (~140k chars) — the compute saver that makes a
                 SMALL local model viable at all.
  3. EXTRACTION CALLS — the corpus is split into ~8k-char chunks (a 3B
                 model's honest working size; Ollama's default context
                 silently truncates anything bigger) and routed through
                 `text_intelligence.get_extractor()` (decision #74 — the
                 ONE LLM door; backend is config: ollama today, claude
                 when credits are enabled). Never a direct API call.
  4. VALIDATE  — the anti-hallucination guard THIS design stands on:
                 every finding's quote is checked verbatim against the
                 cited page (whitespace/case-normalized, +/-1 page
                 tolerance). A weak model therefore degrades to FEWER
                 findings, never fake ones. Dropped counts are reported.
  5. SCORE     — net_conviction_score is MECHANICAL v1 (validated finding
                 counts, wins minus flags), not model vibes: reproducible,
                 explainable, and honest about being crude. A model-written
                 synthesis becomes an option only on the claude backend.

Advisory-only (Dept 8 iron rules): writes lake JSONs, never touches the
engine; any influence on a live decision goes through Dept 5 validation
then `regime_filters.advise` — same as every other analysis signal.
Output: data/lake/fundamental_reports/<TICKER>/<FY>.json

CLI, from the project folder (Mac):

    python3 -m src.analysis.annual_report_analyzer path/to/report.pdf \
        --ticker VEDL --fy FY25 [--dry-run]
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAKE_DIR = ROOT / "data" / "lake" / "fundamental_reports"

CONDENSE_CHAR_BUDGET = 480_000     # ~120k tokens of dense corpus; the model
                                   # reads it in ~8k-char chunks, so this
                                   # caps CALL COUNT (~70/report), not
                                   # context. Chosen by MEASUREMENT, not
                                   # taste: recall vs budget over 6 real
                                   # reports x 29 hand-verified needle pages
                                   # plateaus at 28/29 (97%) from 480k up.
                                   # The one permanent residual no budget
                                   # buys: a needle on a page with almost
                                   # no forensic vocabulary (VEDL FY25
                                   # p291). Closing it needs the claude
                                   # backend reading MORE pages, not more
                                   # regex.
CHUNK_CHARS = 8_000                # one extraction call's working size
MIN_PAGE_CHARS = 40                # below this a page is "unreadable"
PAGE_CHAR_CAP = 12_000             # one monster table-page can't eat a section
# The char budget is split per section so a fat section can never starve
# the others (the VEDL lesson: 33 contingent pages crowded out the
# related-party note where the parent-loan needle lives).
SECTION_BUDGET_SHARE = {"auditor": 0.19, "contingent": 0.15,
                        "related": 0.22, "notes": 0.20, "directors": 0.11,
                        "mda": 0.13}
# Within the forensic sections, pages are ranked by ONE shared forensic
# vocabulary (the needle words: loans to the parent, guarantees, disputed
# receivables, impairments deferred...) — the actual related-party note
# outranks the 90 pages that merely mention the phrase, and a loan note
# that never repeats "related party" still surfaces. MD&A keeps reading
# order (it's narrative).
FORENSIC_DENSITY = re.compile(
    r"related part|loan|guarantee|contingent|holding company|promoter|"
    r"brand fee|impair|disput|qualifi|key audit matter|emphasis of matter|"
    r"interest rate|receivable|write[- ]?off|deferred tax|"
    r"dividend|buy[- ]?back|net debt|highest ever|"
    # quality-of-earnings vocabulary (the eMudhra/Jupiter lesson, 2026-07-19):
    # costs moving to the balance sheet and revenue booked before billing
    # are needles too, and so is the order book the bulls lean on
    r"order book|capitali[sz]|unbilled|revenue recognition", re.I)
# The transactional needle phrases — the vocabulary of money actually
# moving toward a related party — count 4x in the ranking: one loan-terms
# sentence buried in a page of regulatory boilerplate must outrank a page
# that merely says "impairment" twenty times (the VEDL page-291 lesson).
FORENSIC_HEAVY = re.compile(
    r"loans? (?:to|from|given)|interest rate|brand fee|"
    r"guarantees? (?:given|issued|provided)|key audit matter|"
    r"emphasis of matter|qualifi|unbilled revenue|capitali[sz]ed|"
    r"audit trail", re.I)
HEAVY_WEIGHT = 4

# Section header patterns, matched on the top of each page. Order = the
# priority the char budget is spent in: where bodies are buried first.
SECTION_PATTERNS = (
    ("auditor",    re.compile(r"independent auditor'?s? report", re.I)),
    ("contingent", re.compile(r"contingent liabilit", re.I)),
    ("related",    re.compile(r"related part(?:y|ies)", re.I)),
    ("notes",      re.compile(
        r"notes (?:forming part of|to) the (?:consolidated |standalone )?"
        r"financial statements", re.I)),
    ("directors",  re.compile(r"(?:directors'?|board'?s) report", re.I)),
    ("mda",        re.compile(r"management discussion (?:and|&) analysis", re.I)),
)
# Pages whose header matches these are boilerplate — dropped outright.
BOILERPLATE = re.compile(
    r"business responsibility (?:and|&) sustainability|"
    r"corporate social responsibility", re.I)
# Whole-page fallback signals: pypdf scrambles multi-column layouts, so an
# auditor-report or contingent-liabilities header often isn't in the first
# 2000 chars. These fire anywhere on the page but demand STRONGER evidence
# (two auditor phrases together; the literal note caption) so index pages
# and cross-references don't false-positive.
PAGE_SIGNALS = (
    ("auditor", re.compile(
        r"(?:key audit matter|basis for opinion|auditor'?s? responsibilit|"
        r"audit trail)",   # the post-2023 Rule 11(g) exception lives in
        re.I)),            # CARO annexure pages with no header of their own
    ("contingent", re.compile(r"contingent liabilities", re.I)),
)
# How many pages a section run may continue past its header before we stop
# attributing pages to it (annual reports interleave sections heavily).
SECTION_RUN_CAP = {"auditor": 15, "contingent": 6, "related": 10,
                   "notes": 80, "directors": 25, "mda": 35}

EXTRACTION_SYSTEM_PROMPT = """You are a Senior Forensic Auditor reading pages of an Indian company's annual report. Report BOTH directions with equal rigor: genuine operational wins AND structural risks.

MATERIALITY CAP (v1.1 — the measured v1 failure was 96-178 indiscriminate flags per report): you are STRICTLY CAPPED at a MAXIMUM of 2 high-conviction findings per response, across all three categories combined. Do NOT flag boilerplate: routine accounting-policy text, generic risk-factor language, standard regulatory declarations, and table-of-contents matter are NOT findings. If nothing on these pages is deeply material, return the three empty lists. Quality over quantity.

Rules:
1. Output STRICT JSON only, exactly: {"operational_wins": [], "shareholder_returns": [], "hidden_risks_and_flags": []}
2. Every item: {"finding": "<one plain-English sentence with the specific amount>", "quote": "<VERBATIM text copied from the page>", "page": <the [PAGE n] number the quote is under>}
3. The quote must be copied character-for-character from the text. If you cannot quote it, do not report it.
4. hidden_risks_and_flags (material only): hidden/contingent debt, related-party loans or fees, auditor qualifications or Key Audit Matters, guarantees, disputed receivables, impairments deferred, costs capitalised to the balance sheet, unbilled revenue outgrowing revenue.
5. operational_wins (material only): production/volume records with numbers, market-share gains, deleveraging, order-book figures, credible forward guidance.
6. shareholder_returns: dividends/buybacks with amounts.
7. Never invent."""


# ------------------------------------------------------ 1. page extraction

def extract_pages(pdf_path) -> list:
    """One string per page. Lazy pypdf import; a page pypdf cannot read
    becomes "" (counted as unreadable downstream, never guessed). NUL
    bytes stripped: some PDFs' embedded font/encoding tables make pypdf
    emit them, and a NUL anywhere in the condensed corpus file makes
    grep (and some other line-based text tools) silently treat the whole
    file as binary and match nothing — a real incident on a small-cap
    filing, 2026-07-18."""
    from pypdf import PdfReader
    pages = []
    reader = PdfReader(str(pdf_path))
    for pg in reader.pages:
        try:
            pages.append((pg.extract_text() or "").replace("\x00", ""))
        except Exception:
            pages.append("")
    return pages


# ------------------------------------------------- 2. section-aware pages

_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
                            "ﬃ": "ffi", "ﬄ": "ffl"})


def _clean(s: str) -> str:
    """PDF-extraction normalization for MATCHING (never for quoting):
    typographic ligatures expanded (annual reports render 'financial' as
    'ﬁnancial' — a regex that doesn't know this matches NOTHING) and
    whitespace collapsed so headers split across lines still match."""
    return re.sub(r"\s+", " ", (s or "").translate(_LIGATURES))


def tag_pages(pages: list) -> list:
    """[(page_no_1based, section_or_None), ...] — a header starts a run
    that continues until another section's header or its run cap. Pages
    matching a priority pattern directly are always tagged even outside a
    run (contingent-liability notes appear deep inside other sections).
    A section hit BEATS the boilerplate drop: a related-party note that
    happens to list a CSR payment is still a related-party note (the
    page-289 lesson)."""
    tags = []
    current, run_left = None, 0
    for i, text in enumerate(pages):
        head = _clean(text[:2500] if text else "")
        hit = next((name for name, pat in SECTION_PATTERNS
                    if pat.search(head)), None)
        if hit is None:
            full = _clean(text or "")
            hit = next((name for name, pat in PAGE_SIGNALS
                        if pat.search(full)), None)
        if hit is None and BOILERPLATE.search(head):
            tags.append((i + 1, None))
            current, run_left = None, 0
            continue
        if hit:
            current, run_left = hit, SECTION_RUN_CAP[hit]
        elif run_left > 0:
            run_left -= 1
        else:
            current = None
        tags.append((i + 1, current))
    return tags


def condense(pages: list, budget: int = CONDENSE_CHAR_BUDGET) -> dict:
    """The dense corpus: tagged pages only, spent in section-priority
    order, each page prefixed with its [PAGE n] marker so every model
    quote stays traceable. Returns the corpus + honest bookkeeping."""
    tags = tag_pages(pages)
    by_section = {name: [] for name, _ in SECTION_PATTERNS}
    unreadable = sum(1 for p in pages if len((p or "").strip()) < MIN_PAGE_CHARS)
    for (page_no, section) in tags:
        if section:
            by_section[section].append(page_no)

    def _density(page_no):
        text = _clean(pages[page_no - 1] or "")
        return (len(FORENSIC_DENSITY.findall(text))
                + HEAVY_WEIGHT * len(FORENSIC_HEAVY.findall(text)))

    def _block(page_no, name):
        text = (pages[page_no - 1] or "").strip()[:PAGE_CHAR_CAP]
        if len(text) < MIN_PAGE_CHARS:
            return None
        return f"[PAGE {page_no}] ({name})\n{text}\n"

    # Pass 1: each section spends its own share, best (most forensic
    # vocabulary) pages first — a fat section can't starve the others.
    blocks, kept, leftover_budget, leftover_pages = [], [], 0, []
    for name, _ in SECTION_PATTERNS:            # priority order
        section_budget = int(budget * SECTION_BUDGET_SHARE[name])
        spent = 0
        for page_no in sorted(by_section[name], key=_density, reverse=True):
            block = _block(page_no, name)
            if block is None:
                continue
            if spent + len(block) > section_budget:
                leftover_pages.append((page_no, name))
                continue
            blocks.append((page_no, block))
            spent += len(block)
            kept.append(page_no)
        leftover_budget += max(0, section_budget - spent)

    # Pass 2: budget a section didn't use rolls over to the best remaining
    # tagged pages ANYWHERE (the NALCO lesson: a thin auditor's report left
    # half its share idle while MD&A's dividend page missed the cut).
    for page_no, name in sorted(leftover_pages,
                                key=lambda t: _density(t[0]), reverse=True):
        if page_no in kept:
            continue
        block = _block(page_no, name)
        if block is None or len(block) > leftover_budget:
            continue
        blocks.append((page_no, block))
        leftover_budget -= len(block)
        kept.append(page_no)

    blocks.sort()                               # corpus stays in page order
    parts = [b for _, b in blocks]
    return {
        "corpus": "".join(parts),
        "pages_total": len(pages),
        "pages_kept": len(kept),
        "kept_page_numbers": sorted(set(kept)),
        "pages_unreadable": unreadable,
        "section_page_counts": {k: len(v) for k, v in by_section.items()},
    }


def chunk_corpus(corpus: str, chunk_chars: int = CHUNK_CHARS) -> list:
    """Split on [PAGE n] boundaries into <=chunk_chars chunks — one call's
    working size for a 3B local model (Ollama's default context window
    silently truncates larger payloads; many small honest calls beat one
    big silently-clipped one)."""
    blocks = re.split(r"(?=\[PAGE \d+\])", corpus)
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > chunk_chars:
            chunks.append(cur)
            cur = ""
        cur += b
    if cur.strip():
        chunks.append(cur)
    return chunks


# ------------------------------------------------ 4. the honesty validator

def _norm(s: str) -> str:
    return _clean(s).strip().lower()


def validate_findings(findings: list, pages: list) -> dict:
    """Keep a finding ONLY if its quote appears verbatim (whitespace/case
    normalized) on the cited page or a neighbour (+/-1 — pypdf page drift).
    This is the guard that lets a small model be usable at all: bad output
    degrades to fewer findings, never to fabricated evidence."""
    kept, dropped = [], 0
    for f in findings or []:
        try:
            page = int(f.get("page", 0))
            quote = _norm(f.get("quote"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not quote or not (1 <= page <= len(pages)):
            dropped += 1
            continue
        neighbourhood = _norm(" ".join(
            pages[i] for i in range(max(0, page - 2), min(len(pages), page + 1))))
        if quote in neighbourhood:
            kept.append(f)
        else:
            dropped += 1
    return {"kept": kept, "dropped_unverified": dropped}


# ------------------------------------- 4b. report-level reduction (v1.2)

MAX_FINDINGS_PER_REPORT = 10   # per category-group; see reduce_findings

def reduce_findings(findings: list, cap: int = MAX_FINDINGS_PER_REPORT) -> dict:
    """The v1.1 measurement's lesson, mechanized: a PER-CHUNK cap cannot
    reach analyst selectivity by construction (~2 findings x ~68 chunks
    still lands ~136 per report — measured: 61 kept flags on eMudhra even
    with the prompt cap). So after validation, reduce per report:
      * dedupe near-identical findings (same normalized quote, or one
        quote containing the other) — chunk overlap repeats needles;
      * keep the top `cap` by forensic weight of the QUOTE itself (heavy
        transactional phrases 4x, then general vocabulary, then quote
        length as the tiebreak).
    Mechanical and documented, like the score: selection bias toward the
    needle vocabulary is a stated property, not a hidden one."""
    deduped = []
    seen = []
    for f in findings or []:
        q = _norm(f.get("quote"))
        if not q or any(q in s or s in q for s in seen):
            continue
        seen.append(q)
        deduped.append(f)

    def weight(f):
        q = _clean(f.get("quote") or "")
        return (HEAVY_WEIGHT * len(FORENSIC_HEAVY.findall(q))
                + len(FORENSIC_DENSITY.findall(q)), len(q))

    ranked = sorted(deduped, key=weight, reverse=True)
    return {"kept": ranked[:cap],
            "reduced_away": max(0, len(deduped) - cap),
            "duplicates_removed": len(findings or []) - len(deduped)}


# --------------------------------------------- 3+5. extraction + scoring

def _merge(dst: dict, src: dict) -> None:
    for key in ("operational_wins", "shareholder_returns",
                "hidden_risks_and_flags"):
        items = src.get(key)
        if isinstance(items, list):
            dst[key].extend(items)


def score(n_wins: int, n_returns: int, n_flags: int) -> float:
    """MECHANICAL v1 (documented as such in the output): starts neutral,
    each validated win/payout adds, each validated flag subtracts harder
    (asymmetric on purpose — a hidden-debt flag outweighs a production
    record). Reproducible and explainable; not a model's opinion."""
    raw = 0.50 + 0.04 * n_wins + 0.03 * n_returns - 0.08 * n_flags
    return round(max(0.0, min(1.0, raw)), 2)


def analyze(pdf_path, ticker: str, fiscal_year: str,
            extractor=None, pages: list = None,
            out_dir: Path = None, write: bool = True) -> dict:
    """The full per-document pipeline. Every seam injectable: `pages`
    skips pypdf, `extractor` needs only .chat_json(system, text) (the
    text_intelligence shape) — tests run fully offline with fakes."""
    if pages is None:
        pages = extract_pages(pdf_path)
    cond = condense(pages)

    if extractor is None:
        from src.text_intelligence import get_extractor
        extractor = get_extractor()

    raw = {"operational_wins": [], "shareholder_returns": [],
           "hidden_risks_and_flags": []}
    failed_chunks = 0
    chunks = chunk_corpus(cond["corpus"])
    for chunk in chunks:
        try:
            out = extractor.chat_json(EXTRACTION_SYSTEM_PROMPT, chunk)
            if isinstance(out, dict):
                _merge(raw, out)
            else:
                failed_chunks += 1
        except Exception:
            failed_chunks += 1          # one bad call never kills the doc
        # v1.1 memory hygiene: keep Python's footprint flat on the 8GB
        # box. (Honest note: Python holds little here — the real RAM
        # lever is the model server, unloaded below after the report.)
        import gc
        gc.collect()
    # Flush the model out of Ollama's memory before the next report —
    # a batch over many PDFs must not leave 2GB resident between docs.
    # Best-effort: extractors without an unload seam (fakes, claude) skip.
    try:
        getattr(extractor, "unload", lambda: None)()
    except Exception:
        pass

    wins = validate_findings(raw["operational_wins"], pages)
    rets = validate_findings(raw["shareholder_returns"], pages)
    flags = validate_findings(raw["hidden_risks_and_flags"], pages)
    r_wins = reduce_findings(wins["kept"])
    r_rets = reduce_findings(rets["kept"])
    r_flags = reduce_findings(flags["kept"])

    result = {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "source_file": str(pdf_path),
        "operational_wins": r_wins["kept"],
        "shareholder_returns": r_rets["kept"],
        "hidden_risks_and_flags": r_flags["kept"],
        "net_conviction_score": score(len(r_wins["kept"]), len(r_rets["kept"]),
                                      len(r_flags["kept"])),
        "score_method": "mechanical_v1.2 (validated findings, deduped + "
                        "reduced to the top 10 per category by forensic "
                        "weight; flags weighted 2x wins)",
        "evidence_discipline": {
            "findings_dropped_unverified": (wins["dropped_unverified"]
                                            + rets["dropped_unverified"]
                                            + flags["dropped_unverified"]),
            "duplicates_removed": (r_wins["duplicates_removed"]
                                   + r_rets["duplicates_removed"]
                                   + r_flags["duplicates_removed"]),
            "reduced_away": (r_wins["reduced_away"] + r_rets["reduced_away"]
                             + r_flags["reduced_away"]),
            "chunks_total": len(chunks),
            "chunks_failed": failed_chunks,
        },
        "condensation": {k: cond[k] for k in
                         ("pages_total", "pages_kept", "pages_unreadable",
                          "section_page_counts")},
    }
    if write:
        out_root = Path(out_dir) if out_dir else LAKE_DIR
        dest = out_root / ticker / f"{fiscal_year}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, indent=2))
        result["written_to"] = str(dest)
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--fy", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="condense + report sizes only; no LLM calls")
    args = ap.parse_args()
    if args.dry_run:
        pg = extract_pages(args.pdf)
        c = condense(pg)
        print(json.dumps({k: c[k] for k in c if k != "corpus"}, indent=2))
        print(f"corpus chars: {len(c['corpus'])} -> "
              f"{len(chunk_corpus(c['corpus']))} chunks")
    else:
        r = analyze(args.pdf, ticker=args.ticker, fiscal_year=args.fy)
        print(json.dumps({k: v for k, v in r.items()
                          if k not in ("operational_wins",
                                       "hidden_risks_and_flags")}, indent=2))
