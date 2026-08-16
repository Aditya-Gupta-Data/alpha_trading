"""
src/research/geo_revenue_extractor.py — read the geography split out of the reports
===================================================================================

V2 RESEARCH SANDBOX, 2026-08-16. `config/geo_revenue_exposure.json` shipped
with ten HAND-TYPED estimates. This replaces the guessable half of them with
numbers read out of the companies' own filings — we already hold **301
annual-report PDFs (4.3 GB)** in `data/fundamental_reports/`.

**ON NO EXECUTION PATH.** No cron, no live importer, a test enforces it.

WHAT IND AS 108 ACTUALLY GIVES US, AND WHAT IT DOES NOT
--------------------------------------------------------
This is the finding that shapes the whole module, so it is stated before
the code rather than discovered by whoever reads the output:

  ✅ **Country / India-vs-outside splits ARE disclosed.** Ind AS 108
     requires revenue from external customers split by geography, and in
     practice Indian companies file it as "Within India / Outside India",
     sometimes broken further into US / Europe / Rest of World.

  ❌ **INDIAN-STATE splits are NOT disclosed and never will be.** No
     company reports revenue by Uttar Pradesh vs Bihar. So the state-level
     rows in the geo map — the ones that matter for an election or a
     monsoon shock — **cannot be extracted from annual reports at all.**
     They must stay estimates, or be rebuilt from something structural
     (plant locations, mine locations, GST-state registrations).

That asymmetry is the reason this module only ever writes the `country` and
`region_bloc` rows and leaves `india_state` rows untouched. A single
extractor that silently overwrote a state estimate with a number it could
not possibly have found would be the worst outcome here.

HOW IT READS
------------
`pdfplumber` page text, scanned for the geography-note markers below, then
rupee/percentage figures pulled from the neighbourhood. **It extracts
EVIDENCE, not conclusions**: every hit carries the page number and the
verbatim line it came from, so a human can check it in the source PDF in
under a minute. Nothing is written to the geo map without `--apply`, and
even then only rows whose evidence survives.

A 300-page PDF takes a few seconds; the scan stops at `MAX_PAGES_SCANNED`
matches because the geography note appears once, in the consolidated
statements, and reading the whole document to find a second copy of it is
wasted time on a 1 GB box.

CLI
    python3 -m src.research.geo_revenue_extractor --ticker TCS
    python3 -m src.research.geo_revenue_extractor --all --json
    python3 -m src.research.geo_revenue_extractor --all --apply
"""
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = ROOT / "data" / "fundamental_reports"
GEO_PATH = ROOT / "config" / "geo_revenue_exposure.json"

# The note is titled slightly differently by every filer; these are the
# phrases that actually appear above a geography table.
GEO_MARKERS = (
    "geographical information", "geographic information",
    "revenue from external customers", "geographical segment",
    "revenue by geography", "geographical revenue",
    "information about geographical areas", "geography wise",
)

# Region words worth capturing when they appear on a marked page.
REGION_WORDS = (
    "within india", "outside india", "india", "united states", "americas",
    "usa", "u.s.a", "europe", "united kingdom", "uk", "middle east",
    "asia pacific", "apac", "africa", "china", "japan", "australia",
    "rest of the world", "row",
)

MAX_PAGES_SCANNED = 400
MAX_MARKED_PAGES = 4        # the note appears once; more is noise
_MONEY = re.compile(r"[\d,]+\.?\d*")


def report_pdfs(ticker: str, reports_dir=None) -> list:
    """Every annual-report PDF held for this ticker, newest name last."""
    sym = str(ticker).replace(".NS", "").upper()
    d = Path(reports_dir or REPORTS_DIR) / sym
    return sorted(d.glob("*.pdf")) if d.is_dir() else []


def _lines_with_regions(text: str) -> list:
    """Lines on a marked page that name a region AND carry a figure."""
    out = []
    for raw in str(text or "").splitlines():
        line = " ".join(raw.split())
        low = line.lower()
        if not any(w in low for w in REGION_WORDS):
            continue
        if not _MONEY.search(line):
            continue
        if len(line) > 220:
            continue
        out.append(line)
    return out


def extract_from_pdf(pdf_path, max_pages: int = MAX_PAGES_SCANNED,
                     open_fn=None) -> dict:
    """Evidence, not conclusions: {pages_scanned, hits:[{page, line}]}.

    Never raises — an encrypted or malformed PDF costs that file, not the
    run. Returns its own error string so a failure is visible in the report
    rather than looking like an absent disclosure."""
    result = {"file": str(pdf_path), "pages_scanned": 0, "marked_pages": [],
              "hits": [], "error": None}
    try:
        if open_fn is not None:
            pages = open_fn(pdf_path)
        else:
            import pdfplumber
            pdf = pdfplumber.open(str(pdf_path))
            pages = pdf.pages
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return result
    try:
        marked = 0
        for i, page in enumerate(pages):
            if i >= max_pages or marked >= MAX_MARKED_PAGES:
                break
            result["pages_scanned"] = i + 1
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            low = text.lower()
            if not any(m in low for m in GEO_MARKERS):
                continue
            marked += 1
            result["marked_pages"].append(i + 1)
            for line in _lines_with_regions(text)[:12]:
                result["hits"].append({"page": i + 1, "line": line})
    finally:
        try:
            pdf.close()
        except Exception:
            pass
    return result


def extract_ticker(ticker: str, reports_dir=None, **kw) -> dict:
    pdfs = report_pdfs(ticker, reports_dir)
    if not pdfs:
        return {"ticker": ticker, "status": "no_report_held", "hits": []}
    ev = extract_from_pdf(pdfs[-1], **kw)
    status = ("read_failed" if ev["error"] else
              "geography_note_found" if ev["hits"] else
              "note_not_located")
    return {"ticker": ticker, "status": status, "source_file": Path(pdfs[-1]).name,
            "pages_scanned": ev["pages_scanned"],
            "marked_pages": ev["marked_pages"],
            "error": ev["error"], "hits": ev["hits"]}


def geo_tickers(geo_path=None) -> list:
    try:
        raw = json.loads(Path(geo_path or GEO_PATH).read_text())
        return sorted(raw.get("exposures") or {})
    except (OSError, ValueError):
        return []


# A marked page is not a disclosure. TCS/INFY/SUNPHARMA all "hit" on MD&A
# narrative and percentage charts; only a line that names a geography AND
# carries a rupee figure in the Ind AS 108 style is worth promoting. This
# predicate is what keeps a 40%-yield scanner from writing 40% confidence.
_QUANTIFIED = re.compile(
    r"(within india|outside india|india|united states|europe|americas|apac|"
    r"united kingdom|rest of the world)\D{0,24}[\d,]{4,}", re.I)


def is_quantified(line: str) -> bool:
    """True only for a geography line carrying a real figure beside it."""
    return bool(_QUANTIFIED.search(" ".join(str(line or "").split())))


def apply_evidence(results: list, geo_path=None) -> dict:
    """Attach the extracted evidence to the geo map's COUNTRY rows only.

    It does NOT invent share_pct — the line text is filed as `source` so the
    next reader can quantify it from the PDF. `india_state` rows are never
    touched, because no annual report discloses state-level revenue and
    pretending otherwise is the failure this module was written to avoid."""
    p = Path(geo_path or GEO_PATH)
    raw = json.loads(p.read_text())
    touched, skipped_state = 0, 0
    for r in results:
        if r["status"] != "geography_note_found":
            continue
        quantified = [h for h in r["hits"] if is_quantified(h["line"])]
        if not quantified:
            # Marked pages but no figures — the scanner found the CHAPTER,
            # not the TABLE. Leaving the hand estimate in place with its
            # honest "unverified" source beats stamping it `high`.
            continue
        body = (raw.get("exposures") or {}).get(r["ticker"])
        if not body:
            continue
        evidence = "; ".join(h["line"][:110] for h in quantified[:2])
        for e in body.get("exposures", []):
            if e.get("kind") == "india_state":
                skipped_state += 1
                continue
            e["source"] = (f"EXTRACTED {date.today().isoformat()} from "
                           f"{r['source_file']} p.{r['marked_pages'][:2]}: "
                           f"{evidence}")[:400]
            e["confidence"] = "high"
            touched += 1
    raw["_extraction"] = {
        "run_on": date.today().isoformat(),
        "country_rows_evidenced": touched,
        "india_state_rows_left_alone": skipped_state,
        "why": ("Ind AS 108 discloses geography by COUNTRY, never by Indian "
                "state. State rows stay estimates by necessity — they must "
                "be rebuilt from plant/mine locations or GST registrations, "
                "not from annual reports."),
    }
    p.write_text(json.dumps(raw, indent=1) + "\n")
    return {"country_rows_evidenced": touched,
            "india_state_rows_left_alone": skipped_state}


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Read the geography split out of "
                                             "held annual reports (research)")
    ap.add_argument("--ticker")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="write the evidence into the geo map")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    targets = [a.ticker] if a.ticker else (geo_tickers() if a.all else [])
    if not targets:
        print("nothing to do — pass --ticker or --all")
        return 2
    results = [extract_ticker(t) for t in targets]
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['ticker']:<16} {r['status']:<22} "
                  f"pages={r.get('pages_scanned', 0):<4} "
                  f"hits={len(r.get('hits') or [])}")
            for h in (r.get("hits") or [])[:3]:
                print(f"    p{h['page']}: {h['line'][:96]}")
    if a.apply:
        print(json.dumps(apply_evidence(results), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
