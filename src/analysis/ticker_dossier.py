"""
src/analysis/ticker_dossier.py — the ANTI-SILO lookup (Dept 8)
===============================================================

One question, one answer: "everything this desk knows about <TICKER>".

The problem this solves (owner directive 2026-07-19): research had spread
across several stores that nothing tied together — a forensic verdict here,
business metrics there, a liquidity rank somewhere else, a PDF on disk, a
yfinance capture in another folder. Anyone (a human, a future session, another
department's module) had to KNOW each store existed to use it. That is a silo.

This module is the single front door. It never computes anything new and never
writes: it reads every store the desk maintains and reports what exists, what
is missing, and where each piece lives, so the data can actually be found and
reused.

Stores consulted:
  data/lake/fundamental_reports/<T>/*.json   forensic verdicts (judgment reads)
  data/lake/business_metrics/<T>/*.json      'darling' business metrics
  data/lake/fundamental_reports_auto/<T>/    machine-tier output (quarantined)
  data/lake/fundamentals/<T>/*.json          yfinance statements capture
  data/fundamental_reports/<T>/*.pdf         the source annual-report PDFs
  data/lake/bhavcopy/*.csv                   liquidity rank (avg traded value)
  data/_diligence_resume/*.json              scan queues / dead list / probe log

NULL-honest: a store that has nothing for this ticker is reported as absent,
never faked. Advisory-only, read-only — touches no engine state, no brain_map.

CLI:
    python3 -m src.analysis.ticker_dossier RELIANCE
    python3 -m src.analysis.ticker_dossier RELIANCE --json
"""
import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAKE = ROOT / "data" / "lake"
PDFS = ROOT / "data" / "fundamental_reports"
RESUME = ROOT / "data" / "_diligence_resume"


def _load(fp):
    try:
        return json.load(open(fp))
    except Exception:
        return None


def dossier(ticker: str, with_rank: bool = True) -> dict:
    """Everything the desk knows about one ticker, gathered from every store."""
    t = ticker.upper().strip()
    out = {"ticker": t}

    # --- forensic verdicts (all vintages; latest first) -------------------
    forensic = []
    for fp in sorted(glob.glob(str(LAKE / "fundamental_reports" / t / "*.json")), reverse=True):
        r = _load(fp)
        if not r or "conviction_score" not in r:
            continue
        forensic.append({
            "file": os.path.basename(fp),
            "fiscal_year": r.get("fiscal_year"),
            "score": r.get("conviction_score"),
            "sub_scores": r.get("sub_scores", {}),
            "n_red": len(r.get("red_flags") or []),
            "n_yellow": len(r.get("yellow_flags") or []),
            "n_positive": len(r.get("guidance_and_positives") or []),
            "single_kam": r.get("single_kam"),
            "verdict": r.get("verdict"),
            "analyst": r.get("analyst"),
        })
    out["forensic"] = forensic

    # --- business / 'darling' metrics -------------------------------------
    business = []
    for fp in sorted(glob.glob(str(LAKE / "business_metrics" / t / "*.json")), reverse=True):
        r = _load(fp)
        if not r:
            continue
        business.append({"file": os.path.basename(fp),
                         "fiscal_year": r.get("fiscal_year"),
                         "order_book": r.get("order_book"),
                         "schedule_iii_ratios": r.get("schedule_iii_ratios"),
                         "credit_rating": r.get("credit_rating"),
                         "darling_read": r.get("darling_read")})
    out["business_metrics"] = business

    # --- machine-tier (quarantined, never mixed with judgment reads) ------
    out["machine_tier"] = [os.path.basename(f) for f in
                           glob.glob(str(LAKE / "fundamental_reports_auto" / t / "*.json"))]

    # --- raw sources on disk ---------------------------------------------
    out["source_pdfs"] = [os.path.basename(f) for f in
                          glob.glob(str(PDFS / t / "*.pdf"))]
    out["yfinance_captures"] = [os.path.basename(f) for f in
                                glob.glob(str(LAKE / "fundamentals" / t / "*.json"))]

    # --- liquidity standing (key absent entirely when not computed) -------
    if with_rank:
        try:
            from src.analysis.liquidity_rank import rank_universe
            for r in rank_universe():
                if r["symbol"] == t:
                    out["liquidity"] = {"rank": r["rank"],
                                        "avg_turnover_cr_per_day": round(r["avg_turnover_lacs"] / 100, 1),
                                        "days_traded": r["days"]}
                    break
            else:
                out["liquidity"] = None
        except Exception as e:
            out["liquidity"] = {"error": str(e)[:80]}

    # --- scan-pipeline standing ------------------------------------------
    dead = set()
    dp = RESUME / "dead.txt"
    if dp.exists():
        dead = set(dp.read_text().split())
    q = _load(RESUME / "liquidity_scan_queue.json") or []
    rq = _load(RESUME / "vintage_refresh_queue.json") or []
    out["pipeline"] = {
        "unavailable_at_source": t in dead,
        "queued_for_scan": t in q,
        "queued_for_vintage_refresh": t in rq,
    }
    return out


def render(d: dict) -> str:
    t = d["ticker"]
    L = [f"═══ {t} ═══"]
    if "liquidity" not in d:
        # --no-rank was used: say so, don't imply the name is illiquid
        L.append("Liquidity   : not computed (--no-rank)")
    elif isinstance(d["liquidity"], dict) and "rank" in d["liquidity"]:
        liq = d["liquidity"]
        L.append(f"Liquidity   : rank #{liq['rank']} · ₹{liq['avg_turnover_cr_per_day']} cr/day avg "
                 f"({liq['days_traded']} days)")
    elif isinstance(d["liquidity"], dict) and "error" in d["liquidity"]:
        L.append(f"Liquidity   : unavailable ({d['liquidity']['error']})")
    else:
        L.append("Liquidity   : not present in the bhavcopy ranking")

    if d["forensic"]:
        L.append(f"\nFORENSIC ({len(d['forensic'])} vintage(s)) — data/lake/fundamental_reports/{t}/")
        for f in d["forensic"]:
            L.append(f"  {f['file']:<16} {f['fiscal_year']:<28} score {f['score']}"
                     f"  [{f['n_red']}R {f['n_yellow']}Y {f['n_positive']}+]")
            if f.get("verdict"):
                v = f["verdict"]
                L.append(f"      {v[:300]}{'…' if len(v) > 300 else ''}")
    else:
        L.append("\nFORENSIC    : none yet")

    if d["business_metrics"]:
        L.append(f"\nBUSINESS METRICS — data/lake/business_metrics/{t}/")
        for b in d["business_metrics"]:
            L.append(f"  {b['file']:<16} {b.get('fiscal_year')}")
            if b.get("order_book"):
                L.append(f"      order book: {b['order_book']}")
            if b.get("schedule_iii_ratios"):
                L.append(f"      Schedule III ratios: {len(b['schedule_iii_ratios'])} captured")
    else:
        L.append("\nBUSINESS    : none yet")

    L.append("\nSOURCES     : " + (", ".join(d["source_pdfs"]) or "no PDF on disk"))
    if d["yfinance_captures"]:
        L.append(f"yfinance    : {len(d['yfinance_captures'])} capture(s)")
    if d["machine_tier"]:
        L.append(f"machine-tier: {len(d['machine_tier'])} file(s) (quarantined, not judgment-grade)")

    p = d["pipeline"]
    flags = [k.replace("_", " ") for k, v in p.items() if v]
    L.append("PIPELINE    : " + (", ".join(flags) if flags else "not queued (nothing pending)"))
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-rank", action="store_true",
                    help="skip the liquidity scan (faster)")
    a = ap.parse_args()
    d = dossier(a.ticker, with_rank=not a.no_rank)
    print(json.dumps(d, indent=1) if a.json else render(d))
