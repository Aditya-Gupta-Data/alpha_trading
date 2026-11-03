"""AltData edge-class proving court (Dept 5): grade each alt-data relation
in `data/brain_map.db` against forward returns from the bhavcopy lake.

Default run is a verdict only. `--promote` flips GRADUATED classes to
`outcome_derived`; `--revoke RELATION` undoes one. Every run appends its
verdicts to `logs/altdata_proving_court.jsonl`. Not on cron: a promotion is a
person's decision. Distinct from `src/validation/run_proving_court.py`
(the hypothesis court, decision #97).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion import bhavcopy_clerk  # noqa: E402
from src.knowledge_graph import graphrag_eval as ge  # noqa: E402

DEFAULT_DB = ROOT / "data" / "brain_map.db"
DEFAULT_LEDGER = ROOT / "logs" / "altdata_proving_court.jsonl"
BAR_DAYS = 400


def run_court(
    conn: sqlite3.Connection,
    bars_loader=None,
    promote: bool = False,
    horizon_sessions: int = ge.DEFAULT_HORIZON_SESSIONS,
    n_shuffles: int = ge.DEFAULT_PLACEBO_DRAWS,
    seed: int = 0,
    ledger_path: Optional[Path] = DEFAULT_LEDGER,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate every alt-data relation; promote graduates only when asked."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    activations = ge.load_altdata_activations(conn)
    tickers = sorted({a["ticker"] for a in activations})
    loader = bars_loader or (lambda syms: bhavcopy_clerk.bars_for_many(syms, days=BAR_DAYS))
    bars = loader(tickers) if tickers else {}
    verdicts = ge.evaluate_altdata_edge_classes(
        activations, bars, horizon_sessions=horizon_sessions,
        n_shuffles=n_shuffles, seed=seed)
    already = ge.promoted_relations(conn)
    promoted: Dict[str, int] = {}
    if promote:
        for rel, v in verdicts.items():
            if v["graduates"] and rel not in already:
                promoted[rel] = ge.promote_edge_class(conn, v, now_iso)
    report = {
        "run_at": now_iso,
        "activations": len(activations),
        "tickers": len(tickers),
        "promote_requested": promote,
        "already_promoted": sorted(already),
        "promoted_now": promoted,
        "verdicts": verdicts,
    }
    if ledger_path:
        ledger_path = Path(ledger_path)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a") as fh:
            fh.write(json.dumps(report, sort_keys=True, default=str) + "\n")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--promote", action="store_true",
                    help="flip GRADUATED classes to outcome_derived")
    ap.add_argument("--revoke", metavar="RELATION",
                    help="return one promoted relation to altdata_telemetry")
    ap.add_argument("--horizon", type=int, default=ge.DEFAULT_HORIZON_SESSIONS)
    ap.add_argument("--draws", type=int, default=ge.DEFAULT_PLACEBO_DRAWS)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        if args.revoke:
            n = ge.revoke_edge_class(conn, args.revoke)
            print(json.dumps({"revoked": args.revoke, "edges": n}))
            return 0
        report = run_court(conn, promote=args.promote,
                           horizon_sessions=args.horizon, n_shuffles=args.draws)
    finally:
        conn.close()
    slim = {k: v for k, v in report.items() if k != "verdicts"}
    slim["verdicts"] = {r: {k: v[k] for k in ("status", "n", "hit_rate", "wilson_lb",
                                              "sharpe_annualised", "reason")}
                        | {"p_value": (v["placebo"] or {}).get("p_value")}
                        for r, v in report["verdicts"].items()}
    print(json.dumps(slim, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
