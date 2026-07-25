"""
src/analysis/cohort_comparator.py — the cross-sectional research matrix
=======================================================================

Department 8. Reads every conviction JSON in the lake — BOTH kinds:

  * human benchmarks (`data/lake/fundamental_reports/<T>/<FY>.json`, the
    R&D deep-reads: `conviction_score` on a 0-100 analyst scale,
    red/yellow/hidden-debt flag lists, sub_scores), and
  * pipeline outputs (`data/lake/fundamental_reports_auto/...`, the
    automated analyzer: `net_conviction_score` on its mechanical 0-1
    scale, validated finding lists)

— and compiles one comparative matrix: ticker by ticker, analyst score
beside machine score, win/flag counts from each source. The two score
columns are NEVER merged or rescaled into one number: they measure
different things (an analyst's judgment vs a validated-finding count)
and pretending otherwise would be the exact self-deception this system
is built to refuse.

Writes `data/lake/fundamental_reports/cohort_matrix.md`. `--broadcast`
fires ONE summary card through `notifier.fire_broadcast` (the one
Discord door). Deliberately does NOT write brain_map: unvalidated
research enters the options engine's memory only via an explicit
mode-tagged ingest after Department 5 (review #2 quarantine rule).

CLI:  python3 -m src.analysis.cohort_comparator [--broadcast] [--json]
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BENCH_DIR = ROOT / "data" / "lake" / "fundamental_reports"
AUTO_DIR = ROOT / "data" / "lake" / "fundamental_reports_auto"
MATRIX_PATH = BENCH_DIR / "cohort_matrix.md"


def _load_dir(root: Path) -> dict:
    """{(ticker, fy): parsed json} for every <T>/<FY>.json under root.
    Unreadable files are skipped honestly, never guessed at."""
    out = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/*.json")):
        try:
            data = json.loads(path.read_text())
            out[(path.parent.name, path.stem)] = data
        except (OSError, ValueError):
            continue
    return out


def _bench_row(d: dict) -> dict:
    flags = (len(d.get("red_flags") or []) + len(d.get("hidden_debt_flags") or [])
             + len(d.get("yellow_flags") or []))
    return {"analyst_score": d.get("conviction_score"),
            "analyst_flags": flags,
            "analyst_positives": len(d.get("guidance_and_positives") or []),
            "verdict": d.get("verdict")}


def _auto_row(d: dict) -> dict:
    ev = d.get("evidence_discipline") or {}
    return {"auto_score": d.get("net_conviction_score"),
            "auto_wins": len(d.get("operational_wins") or []),
            "auto_flags": len(d.get("hidden_risks_and_flags") or []),
            "auto_dropped": ev.get("findings_dropped_unverified")}


def build_matrix(bench_dir=None, auto_dir=None) -> dict:
    """One merged row per (ticker, FY) seen in either directory."""
    bench = _load_dir(Path(bench_dir) if bench_dir else BENCH_DIR)
    auto = _load_dir(Path(auto_dir) if auto_dir else AUTO_DIR)
    rows = []
    for key in sorted(set(bench) | set(auto)):
        row = {"ticker": key[0], "fiscal_year": key[1]}
        if key in bench:
            row.update(_bench_row(bench[key]))
        if key in auto:
            row.update(_auto_row(auto[key]))
        rows.append(row)
    return {"rows": rows, "n_benchmarked": len(bench), "n_automated": len(auto),
            "n_both": len(set(bench) & set(auto))}


def render_markdown(matrix: dict) -> str:
    def cell(r, k, fmt="{}"):
        v = r.get(k)
        return fmt.format(v) if v is not None else "—"

    lines = [
        "# Research cohort matrix",
        "",
        "Analyst score (0-100, human deep-read) and machine score (0-1,",
        "validated-finding mechanics) are DIFFERENT instruments — read them",
        "side by side, never as one number.",
        "",
        "| Ticker | FY | Analyst score | Analyst flags | Machine score "
        "| Machine wins | Machine flags | Hallucinations dropped |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in matrix["rows"]:
        lines.append(
            f"| {r['ticker']} | {r['fiscal_year']} "
            f"| {cell(r, 'analyst_score')} | {cell(r, 'analyst_flags')} "
            f"| {cell(r, 'auto_score')} | {cell(r, 'auto_wins')} "
            f"| {cell(r, 'auto_flags')} | {cell(r, 'auto_dropped')} |")
    lines += ["", f"Coverage: {matrix['n_benchmarked']} human-benchmarked, "
                  f"{matrix['n_automated']} machine-analyzed, "
                  f"{matrix['n_both']} with both."]
    return "\n".join(lines)


def run(bench_dir=None, auto_dir=None, matrix_path=None,
        broadcast: bool = False, broadcast_fn=None) -> dict:
    matrix = build_matrix(bench_dir, auto_dir)
    md = render_markdown(matrix)
    path = Path(matrix_path) if matrix_path else MATRIX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md)
    matrix["matrix_path"] = str(path)
    if broadcast and matrix["rows"]:
        try:
            if broadcast_fn is None:
                from src.notifier import fire_broadcast
                broadcast_fn = fire_broadcast
            ranked = sorted(
                (r for r in matrix["rows"] if r.get("analyst_score") is not None),
                key=lambda r: r["analyst_score"], reverse=True)
            desc = " · ".join(f"{r['ticker']} {r['analyst_score']}"
                              for r in ranked) or "no scored rows yet"
            broadcast_fn({
                "event": "research_cohort", "ticker": "RESEARCH",
                "date": matrix["rows"][0].get("fiscal_year", ""),
                "description": (f"📚 Cohort matrix updated — "
                                f"{len(matrix['rows'])} report(s).\n"
                                f"Analyst ranking: {desc}\n"
                                f"Full table: data/lake/fundamental_reports/"
                                f"cohort_matrix.md"),
            })
            matrix["broadcast"] = True
        except Exception as e:
            print(f"  (cohort broadcast skipped: {e})")
            matrix["broadcast"] = False
    return matrix


if __name__ == "__main__":
    import sys

    m = run(broadcast="--broadcast" in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(m, indent=2))
    else:
        print(render_markdown(m))
