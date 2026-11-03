# MANUAL OFFLINE TOOL
"""Move PAPER_10L starting capital from IDX_SPREADS to DARLINGS so DARLINGS matches the
treasury's equity budget (decision #179). Dry-run by default; `--apply` writes.

One transaction: both buckets' starting_capital and peak_equity shift by the same amount (so
neither bucket's drawdown jumps), one `capital_allocations` row, a `portfolio_events` row on each
bucket and one `account_events` row. PAPER_10L's summed bucket equity is unchanged and checked.
Refuses when either bucket is halted (a halted bucket's capital is quarantined, #164), when IDX
would go below zero, or when there is nothing to move. Re-running after an apply is a no-op.

    venv/bin/python scripts/reallocate_m4_capital.py            # dry run
    venv/bin/python scripts/reallocate_m4_capital.py --apply    # write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import buckets  # noqa: E402

IDX = "PAPER_10L/IDX_SPREADS"
DARL = "PAPER_10L/DARLINGS"
DEFAULT_DB = ROOT / "data" / "brain_map.db"


def _firm_equity(conn) -> float:
    return round(sum(r["equity"] for r in buckets.summary(conn, buckets.PRIMARY_ACCOUNT)), 2)


def plan(conn, target_rs: float = None) -> dict:
    """What a reallocation would do. `target_rs` defaults to treasury_state.equity_budget_rs."""
    idx, darl = buckets.bucket(conn, IDX), buckets.bucket(conn, DARL)
    if idx is None or darl is None:
        return {"ok": False, "reason": "PAPER_10L IDX_SPREADS / DARLINGS bucket rows missing — run the M1 migration"}
    if target_rs is None:
        try:
            row = conn.execute("SELECT equity_budget_rs FROM treasury_state WHERE id = 1").fetchone()
        except sqlite3.Error:
            row = None
        if row is None:
            return {"ok": False, "reason": "no treasury_state budget — pass --target"}
        target_rs = float(row[0])
    delta = round(float(target_rs) - float(darl["starting_capital"]), 2)
    out = {"target_rs": round(float(target_rs), 2), "delta_rs": delta,
           "idx_start_before": float(idx["starting_capital"]), "darl_start_before": float(darl["starting_capital"]),
           "firm_equity_rs": _firm_equity(conn)}
    if delta == 0:
        return dict(out, ok=False, noop=True, reason="DARLINGS already at the target — nothing to move")
    if delta < 0:
        return dict(out, ok=False, reason=f"DARLINGS is above the target by Rs.{-delta:,.2f}; this tool only moves IDX -> DARLINGS")
    for b in (idx, darl):
        if buckets.is_bucket_halted(conn, b["portfolio_id"]):
            return dict(out, ok=False, reason=f"{b['portfolio_id']} is halted — its capital is quarantined (#164)")
    if delta > float(idx["starting_capital"]):
        return dict(out, ok=False, reason=f"IDX starting capital Rs.{idx['starting_capital']:,.2f} < Rs.{delta:,.2f}")
    return dict(out, ok=True, idx_start_after=round(float(idx["starting_capital"]) - delta, 2),
                darl_start_after=round(float(darl["starting_capital"]) + delta, 2))


def apply(conn, p: dict, actor: str = "owner") -> dict:
    """Write the plan in one transaction; refuses a plan that is not ok."""
    if not p.get("ok"):
        raise RuntimeError(p.get("reason") or "plan not ok")
    d = p["delta_rs"]
    now = datetime.now().replace(microsecond=0).isoformat()
    reason = f"M4 reallocation: DARLINGS to treasury budget Rs.{p['target_rs']:,.2f} (#179)"
    before = _firm_equity(conn)
    if not conn.in_transaction:
        conn.execute("BEGIN")
    try:
        for pid, sign in ((IDX, -1), (DARL, 1)):
            conn.execute("UPDATE portfolios SET starting_capital = round(starting_capital + ?, 2), "
                         "peak_equity = round(peak_equity + ?, 2) WHERE portfolio_id = ?",
                         (sign * d, sign * d, pid))
            conn.execute("INSERT INTO portfolio_events (portfolio_id, ts, event_type, journal_ref, detail) "
                         "VALUES (?, ?, 'capital_reallocated', NULL, ?)",
                         (pid, now, f"{sign * d:+,.2f} — {reason}"))
        conn.execute("INSERT INTO capital_allocations (ts, portfolio_id, delta_rs, from_portfolio_id, reason, actor) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (now, DARL, d, IDX, reason, actor))
        conn.execute("INSERT INTO account_events (ts, event_type, detail) VALUES (?, 'm4_capital_reallocation', ?)",
                     (now, json.dumps(p, sort_keys=True)))
        after = _firm_equity(conn)
        if after != before:
            raise RuntimeError(f"PARITY: PAPER_10L bucket equity moved {before} -> {after}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"applied": True, "delta_rs": d, "firm_equity_rs": after,
            "idx": buckets.bucket(conn, IDX), "darlings": buckets.bucket(conn, DARL)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move PAPER_10L capital IDX_SPREADS -> DARLINGS to the treasury budget")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--target", type=float, help="DARLINGS starting capital to reach (default: treasury budget)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    uri = f"file:{args.db}" + ("" if args.apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        p = plan(conn, args.target)
        print(json.dumps(p, indent=2))
        if not args.apply:
            print("dry-run: nothing written (add --apply)")
            return 0 if p.get("ok") or p.get("noop") else 1
        if p.get("noop"):
            return 0
        res = apply(conn, p)
        print(json.dumps({k: v for k, v in res.items() if k in ("applied", "delta_rs", "firm_equity_rs")}))
        for pid in (IDX, DARL):
            st = buckets.ceiling_state(conn, pid)
            print(pid, json.dumps({k: st[k] for k in ("ceiling_rs", "deployed_rs", "close_only")}))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
