"""
scripts/model_benchmarker.py — Dept 8 judgment-stage model A/B bench
====================================================================

Sequential, SOLO benchmark of local models on one annual report through
the real pipeline (`annual_report_analyzer.analyze` — same condenser,
same v1.1 materiality-capped prompt, same validator and v1.2 reduction),
so the only variable is the model. House rules baked in:

  * SOLO execution — never run this alongside the test suite or any
    other load; the 2026-07-19 measurements proved contention (not swap)
    is what turns 19-minute runs into 148-minute ones, and once HUNG a
    concurrent pytest.
  * RAM hygiene — the previous model is UNLOADED (keep_alive=0) before
    each candidate loads, and the candidate is unloaded after its run;
    on an 8GB box two resident models is a swap sentence.
  * 3B-max RULE — this script does not pull anything bigger than ~2.5GB
    weights; the 8B ask was refused (see ed688f2's commit message).
  * Outputs are quarantined per model under
    data/lake/fundamental_reports_auto_bench/<model>/ — benchmarks and
    prior auto runs are never overwritten.

Metrics per model: pull + solo wall minutes, chunks failed, raw findings
emitted, hallucinations dropped by the validator, post-reduction keeps,
mechanical score, and the two needle booleans — caught_page_154 (any
validated finding citing p153-155) and crisp_476 (a quote carrying the
analyst's ₹476 Mn product-development figure).

CLI (Mac, from the project folder, machine otherwise idle):

    python3 -m scripts.model_benchmarker \
        --models phi3:mini qwen2.5:3b-instruct \
        [--pdf <path>] [--ticker EMUDHRA --fy FY26]
"""
import argparse
import glob
import json
import subprocess
import time
from pathlib import Path

from src.analysis.annual_report_analyzer import analyze
from src.local_parser import LocalExtractor

ROOT = Path(__file__).resolve().parent.parent
BENCH_OUT = ROOT / "data" / "lake" / "fundamental_reports_auto_bench"
MATCHUP_PATH = ROOT / "data" / "lake" / "fundamental_reports" / "model_matchup.md"
DEFAULT_PDF_GLOB = ("/Users/adityagupta/Desktop/annual reports/"
                    "AR_29275_EMUDHRA_2025_2026*")

# The human deep-read this bench is judged against (lake: EMUDHRA/FY26).
ANALYST_ROW = {"model": "human analyst (benchmark)", "minutes": "—",
               "raw_findings": "—", "dropped": "—", "kept_flags": 3,
               "kept_wins": "—", "score": "55/100 (analyst scale)",
               "caught_154": True, "crisp_476": True, "chunks_failed": "—"}
# Prior llama3.2:3b measurements (2026-07-19, v1.1 run + offline v1.2
# reduction) for reference. Its v1.1 wall time was measured on a
# CONTENDED machine and is invalid; the v1-prompt solo time was 19.3m.
LLAMA_ROW = {"model": "llama3.2:3b (prior run)", "minutes": "~19 solo*",
             "raw_findings": 186, "dropped": 117, "kept_flags": 10,
             "kept_wins": 0, "score": 0.0, "caught_154": True,
             "crisp_476": False, "chunks_failed": 1}


def ensure_model(model: str) -> float:
    """Pull the model if absent. Returns pull minutes (0 when cached)."""
    listed = subprocess.run(["ollama", "list"], capture_output=True,
                            text=True).stdout
    if model.split(":")[0] in listed and model in listed:
        return 0.0
    t0 = time.time()
    print(f"[bench] pulling {model} ...", flush=True)
    subprocess.run(["ollama", "pull", model], check=True)
    return round((time.time() - t0) / 60, 1)


def needle_checks(result: dict) -> dict:
    """Sniper-recon correction (2026-07-19, ledger Issue 17): the
    benchmark's 'page 154' is the report's PRINTED page number; the
    Rs.476 Mn sentence lives on EXTRACTED page 156 (offset 2). The
    window is extracted 154-158 so printed-page cites land inside it."""
    every = (result["operational_wins"] + result["shareholder_returns"]
             + result["hidden_risks_and_flags"])
    caught = any(f.get("page") in (154, 155, 156, 157, 158) for f in every)
    crisp = any("476" in (f.get("quote") or "") for f in every)
    return {"caught_154": caught, "crisp_476": crisp}


def summarize(model: str, result: dict, minutes: float,
              pull_minutes: float) -> dict:
    ev = result["evidence_discipline"]
    kept = (len(result["operational_wins"])
            + len(result["shareholder_returns"])
            + len(result["hidden_risks_and_flags"]))
    validated = kept + ev["duplicates_removed"] + ev["reduced_away"]
    row = {"model": model,
           "minutes": minutes,
           "pull_minutes": pull_minutes,
           "raw_findings": validated + ev["findings_dropped_unverified"],
           "dropped": ev["findings_dropped_unverified"],
           "kept_flags": len(result["hidden_risks_and_flags"]),
           "kept_wins": len(result["operational_wins"]),
           "score": result["net_conviction_score"],
           "chunks_failed": ev["chunks_failed"]}
    row.update(needle_checks(result))
    return row


def bench_one(model: str, pdf: str, ticker: str, fy: str) -> dict:
    pull_minutes = ensure_model(model)
    ex = LocalExtractor(model=model)
    ex.unload()                       # start from clean RAM, whoever ran last
    t0 = time.time()
    result = analyze(pdf, ticker=ticker, fiscal_year=fy, extractor=ex,
                     out_dir=BENCH_OUT / model.replace(":", "_"))
    minutes = round((time.time() - t0) / 60, 1)
    ex.unload()                       # analyze() also unloads; belt+braces
    return summarize(model, result, minutes, pull_minutes)


def render_matchup(rows: list) -> str:
    def b(v):
        return {True: "YES", False: "no"}.get(v, v)

    lines = [
        "# Model matchup — Dept 8 judgment stage (eMudhra FY26)",
        "",
        "Same condenser, same v1.1 materiality prompt, same validator and",
        "v1.2 reduction — the model is the only variable. Solo runs.",
        "",
        "| Model | Solo min | Raw findings | Hallucinations dropped "
        "| Kept flags | Kept wins | Score | Failed chunks | p154 caught "
        "| crisp ₹476 Mn |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['model']} | {r['minutes']} | {r['raw_findings']} "
            f"| {r['dropped']} | {r['kept_flags']} | {r['kept_wins']} "
            f"| {r['score']} | {r['chunks_failed']} | {b(r['caught_154'])} "
            f"| {b(r['crisp_476'])} |")
    lines += ["", "*llama3.2:3b time from its earlier v1-prompt solo run; "
                  "its v1.1 timing was contention-invalidated. Scores: "
                  "machine rows are the mechanical 0-1, the analyst row is "
                  "a 0-100 judgment — different instruments, never merged."]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--ticker", default="EMUDHRA")
    ap.add_argument("--fy", default="FY26")
    args = ap.parse_args()
    pdf = args.pdf or glob.glob(DEFAULT_PDF_GLOB)[0]

    rows = [ANALYST_ROW, LLAMA_ROW]
    for model in args.models:
        try:
            row = bench_one(model, pdf, args.ticker, args.fy)
        except Exception as e:          # one broken model never kills the bench
            row = {"model": f"{model} (FAILED: {e})", "minutes": "—",
                   "raw_findings": "—", "dropped": "—", "kept_flags": "—",
                   "kept_wins": "—", "score": "—", "chunks_failed": "—",
                   "caught_154": "—", "crisp_476": "—"}
        rows.append(row)
        print("[bench] " + json.dumps(row), flush=True)

    md = render_matchup(rows)
    MATCHUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATCHUP_PATH.write_text(md)
    print(md, flush=True)
    print("BENCH_DONE", flush=True)


if __name__ == "__main__":
    main()
