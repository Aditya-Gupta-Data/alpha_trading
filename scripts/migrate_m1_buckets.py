# MANUAL OFFLINE TOOL — M1/M4 multi-bucket ledger migration (blueprint docs/m1_multi_bucket_ledger_blueprint.md).
"""Seed the 5 buckets from the flat ledger and backfill portfolio_id. DRY-RUN by default: prints the
plan and the PARITY check and writes nothing (the DB is opened read-only). `--apply` writes inside one
transaction, only when PARITY holds, and logs one `m1_bucket_migration` account event. Idempotent.

Bucket P&L is summed from the settled lock ledger by strategy type (`eqd:` = DARLINGS, else IDX_SPREADS);
PARITY fails unless those sums equal the account's booked realized P&L to the paisa (decision #170).

Rulings (Architect 2026-10-09, Roadmap M4):
  PAPER_10L 5-Bucket Allocation:
    B1: PAPER_10L/IDX_SPREADS  (options spreads, maps all existing options capital and open positions)
    B2: PAPER_10L/EQ_SWING     (equity swing, 0 active positions)
    B3: PAPER_10L/DARLINGS     (darlings delivery, treasury budget if present, 0 active options positions)
    B4: PAPER_10L/WEALTH_LOCK  (long-term wealth lock, 0 active positions)
    B5: PAPER_10L/CROSS_ASSET  (cross-asset macro, 0 active positions)
  Paper shadow accounts: one IDX_SPREADS bucket each.

    python3 scripts/migrate_m1_buckets.py              # dry-run
    python3 scripts/migrate_m1_buckets.py --apply
"""
import argparse, json, sqlite3, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAMILY = {
    "IDX": "index_options",
    "EQ_SWING": "equity_swing",
    "DARLINGS": "darlings",
    "WEALTH_LOCK": "wealth_lock",
    "CROSS_ASSET": "cross_asset",
}


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r and r[0] is not None else 0.0


def plan(conn, five_buckets: bool = True) -> dict:
    """The seeds, the backfill counts and the PARITY check — read-only."""
    from src import portfolio_manager as pm
    conn.row_factory = sqlite3.Row
    out = {"buckets": [], "backfill": {}, "parity": [], "active_positions": {}, "ok": True}
    now = datetime.now().replace(microsecond=0).isoformat()
    t = _tables(conn)
    # --- PAPER_10L: capital split by ruling (treasury budget → DARLINGS), P&L summed from the ledger
    acct = conn.execute("SELECT starting_capital, realized_pnl FROM account_state WHERE id = 1").fetchone()
    start_cap, booked = float(acct["starting_capital"]), float(acct["realized_pnl"])
    equity = start_cap + booked
    darl_start = _one(conn, "SELECT equity_budget_rs FROM treasury_state WHERE id = 1") \
        if "treasury_state" in t else 0.0
    idx_start = start_cap - darl_start
    darl_realized = _one(conn, "SELECT COALESCE(SUM(pnl_net), 0) FROM margin_locks "
                               "WHERE journal_ref LIKE 'eqd:%' AND released_at IS NOT NULL")
    idx_realized = _one(conn, "SELECT COALESCE(SUM(pnl_net), 0) FROM margin_locks "
                              "WHERE journal_ref NOT LIKE 'eqd:%' AND released_at IS NOT NULL")

    if five_buckets:
        # M4: All 5 buckets for PAPER_10L.
        # B1 gets all options capital; B2, B4, B5 initialize with 0 capital / 0 positions.
        # B3 holds darling treasury budget if configured, else 0.
        bucket_defs = (
            ("PAPER_10L/IDX_SPREADS", FAMILY["IDX"], idx_start, idx_realized, pm.MAX_DRAWDOWN_PCT, pm.MAX_DAILY_LOSS_PCT),
            ("PAPER_10L/EQ_SWING", FAMILY["EQ_SWING"], 0.0, 0.0, 10.0, 3.0),
            ("PAPER_10L/DARLINGS", FAMILY["DARLINGS"], darl_start, darl_realized, 10.0, None),
            ("PAPER_10L/WEALTH_LOCK", FAMILY["WEALTH_LOCK"], 0.0, 0.0, None, None),
            ("PAPER_10L/CROSS_ASSET", FAMILY["CROSS_ASSET"], 0.0, 0.0, 10.0, 3.0),
        )
    else:
        # Legacy M1 2-bucket pair
        bucket_defs = (
            ("PAPER_10L/IDX_SPREADS", FAMILY["IDX"], idx_start, idx_realized, pm.MAX_DRAWDOWN_PCT, pm.MAX_DAILY_LOSS_PCT),
            ("PAPER_10L/DARLINGS", FAMILY["DARLINGS"], darl_start, darl_realized, 10.0, None),
        )

    for pid, fam, start, real, mdd, dl in bucket_defs:
        out["buckets"].append({
            "portfolio_id": pid,
            "account_id": "PAPER_10L",
            "strategy_family": fam,
            "starting_capital": round(start, 2),
            "realized_pnl": round(real, 2),
            "risk_per_trade_pct": pm.ACCOUNT_RISK_PER_TRADE_PCT,
            "max_drawdown_pct": mdd,
            "daily_loss_pct": dl,
            "created_at": now,
        })

    # --- Active positions per bucket: B1 gets all open options positions, B2, B4, B5 have 0
    open_opt_positions = int(_one(conn, "SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL AND journal_ref NOT LIKE 'eqd:%'"))
    open_darl_positions = int(_one(conn, "SELECT COUNT(*) FROM margin_locks WHERE released_at IS NULL AND journal_ref LIKE 'eqd:%'"))
    out["active_positions"] = {
        "PAPER_10L/IDX_SPREADS": open_opt_positions,
        "PAPER_10L/EQ_SWING": 0,
        "PAPER_10L/DARLINGS": open_darl_positions,
        "PAPER_10L/WEALTH_LOCK": 0,
        "PAPER_10L/CROSS_ASSET": 0,
    }

    # --- shadows: one IDX bucket each (options-only by ruling)
    for a in _q(conn, "SELECT account_id, starting_capital, realized_pnl FROM paper_accounts ORDER BY account_id"):
        learner = a["account_id"] == pm.ACCOUNT_PAPER_SHADOW_LEARNER
        out["buckets"].append({
            "portfolio_id": f"{a['account_id']}/IDX_SPREADS",
            "account_id": a["account_id"],
            "strategy_family": FAMILY["IDX"],
            "starting_capital": float(a["starting_capital"]),
            "realized_pnl": round(_one(conn, "SELECT COALESCE(SUM(pnl_net), 0) FROM paper_margin_locks "
                                             "WHERE account_id = ? AND released_at IS NOT NULL",
                                       (a["account_id"],)), 2),
            "risk_per_trade_pct": pm.ACCOUNT_RISK_PER_TRADE_PCT,
            "max_drawdown_pct": None if learner else pm.MAX_DRAWDOWN_PCT,
            "daily_loss_pct": None if learner else pm.MAX_DAILY_LOSS_PCT,
            "created_at": now,
        })

    # --- backfill map (what each existing row already says)
    bf = out["backfill"]
    bf["margin_locks→DARLINGS"] = _one(conn, "SELECT COUNT(*) FROM margin_locks WHERE journal_ref LIKE 'eqd:%'")
    bf["margin_locks→IDX"] = _one(conn, "SELECT COUNT(*) FROM margin_locks WHERE journal_ref NOT LIKE 'eqd:%'")
    bf["paper_margin_locks→<acct>/IDX"] = _one(conn, "SELECT COUNT(*) FROM paper_margin_locks")
    if "paper_live_positions" in t:
        bf["paper_live_positions→<acct>/IDX"] = _one(conn, "SELECT COUNT(*) FROM paper_live_positions")
    if "trade_tickets" in t:
        bf["trade_tickets→by account/source"] = _one(conn, "SELECT COUNT(*) FROM trade_tickets")
    if "outcomes" in t:
        bf["outcomes→PAPER_10L/IDX"] = _one(conn, "SELECT COUNT(*) FROM outcomes WHERE journal_ref NOT LIKE 'sim:%'")
    if "shadow_trades" in t:
        bf["shadow_trades→PAPER_10L/IDX"] = _one(conn, "SELECT COUNT(*) FROM shadow_trades")

    # --- PARITY: Σ bucket equity (ledger-summed P&L) == booked account equity, exactly
    by_acct = {}
    for b in out["buckets"]:
        by_acct[b["account_id"]] = by_acct.get(b["account_id"], 0.0) + b["starting_capital"] + b["realized_pnl"]
    out["parity"].append(_parity("PAPER_10L equity", by_acct.get("PAPER_10L", 0.0), equity))
    for a in _q(conn, "SELECT account_id, starting_capital, realized_pnl FROM paper_accounts ORDER BY account_id"):
        out["parity"].append(_parity(f"{a['account_id']} equity", by_acct.get(a["account_id"], 0.0),
                                     float(a["starting_capital"]) + float(a["realized_pnl"])))
    # a released lock with no P&L is a ledger hole the sums cannot see; a budget above capital has no split
    holes = _one(conn, "SELECT COUNT(*) FROM margin_locks WHERE released_at IS NOT NULL AND pnl_net IS NULL") \
        + _one(conn, "SELECT COUNT(*) FROM paper_margin_locks WHERE released_at IS NOT NULL AND pnl_net IS NULL")
    out["parity"].append(_parity("released locks without P&L", holes, 0))
    out["parity"].append(_parity("PAPER_10L capital split (IDX start >= 0)", min(0.0, idx_start), 0))

    out["ok"] = all(p["ok"] for p in out["parity"])
    return out


def _parity(name, buckets, account):
    return {
        "check": name,
        "buckets": round(float(buckets), 2),
        "account": round(float(account), 2),
        "ok": round(float(buckets), 2) == round(float(account), 2),
    }


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def apply(conn, p: dict) -> dict:
    """Write the plan in ONE transaction; refuses when PARITY failed. Idempotent."""
    if not p["ok"]:
        raise RuntimeError("PARITY failed — nothing written")
    t = _tables(conn)
    n = {"buckets": 0, "locks": 0, "rows": 0}
    conn.execute("BEGIN")
    try:
        for b in p["buckets"]:
            cur = conn.execute(
                "INSERT OR IGNORE INTO portfolios (portfolio_id, account_id, strategy_family, starting_capital, "
                "realized_pnl, peak_equity, risk_per_trade_pct, max_drawdown_pct, daily_loss_pct, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (b["portfolio_id"], b["account_id"], b["strategy_family"], b["starting_capital"], b["realized_pnl"],
                 b["starting_capital"] + b["realized_pnl"], b["risk_per_trade_pct"], b["max_drawdown_pct"],
                 b["daily_loss_pct"], b["created_at"]))
            n["buckets"] += cur.rowcount
        # primary locks: eqd: → DARLINGS, else IDX; copied whole (history kept) + stamped in place
        for row in _q(conn, "SELECT journal_ref, margin_rs, locked_at, released_at, pnl_net FROM margin_locks"):
            pid = "PAPER_10L/DARLINGS" if str(row["journal_ref"]).startswith("eqd:") else "PAPER_10L/IDX_SPREADS"
            n["locks"] += conn.execute(
                "INSERT OR IGNORE INTO portfolio_margin_locks (portfolio_id, journal_ref, margin_rs, lots, locked_at, "
                "released_at, pnl_net) VALUES (?, ?, ?, 1, ?, ?, ?)",
                (pid, row["journal_ref"], row["margin_rs"], row["locked_at"], row["released_at"], row["pnl_net"])).rowcount
            n["rows"] += conn.execute("UPDATE margin_locks SET portfolio_id = ? WHERE journal_ref = ? AND portfolio_id IS NULL",
                                      (pid, row["journal_ref"])).rowcount
        for row in _q(conn, "SELECT account_id, journal_ref, margin_rs, lots, locked_at, released_at, pnl_net "
                            "FROM paper_margin_locks"):
            pid = f"{row['account_id']}/IDX_SPREADS"
            n["locks"] += conn.execute(
                "INSERT OR IGNORE INTO portfolio_margin_locks (portfolio_id, journal_ref, margin_rs, lots, locked_at, "
                "released_at, pnl_net) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, row["journal_ref"], row["margin_rs"], row["lots"], row["locked_at"], row["released_at"],
                 row["pnl_net"])).rowcount
            n["rows"] += conn.execute("UPDATE paper_margin_locks SET portfolio_id = ? WHERE account_id = ? AND "
                                      "journal_ref = ? AND portfolio_id IS NULL",
                                      (pid, row["account_id"], row["journal_ref"])).rowcount
        if "paper_live_positions" in t:
            n["rows"] += conn.execute("UPDATE paper_live_positions SET portfolio_id = account_id || '/IDX_SPREADS' "
                                      "WHERE portfolio_id IS NULL").rowcount
        if "trade_tickets" in t:
            n["rows"] += conn.execute("UPDATE trade_tickets SET portfolio_id = CASE WHEN source LIKE 'equity_desk%' "
                                      "THEN 'PAPER_10L/DARLINGS' ELSE COALESCE(account_id, 'PAPER_10L') || '/IDX_SPREADS' END "
                                      "WHERE portfolio_id IS NULL").rowcount
        if "outcomes" in t:
            n["rows"] += conn.execute("UPDATE outcomes SET portfolio_id = 'PAPER_10L/IDX_SPREADS' "
                                      "WHERE portfolio_id IS NULL AND journal_ref NOT LIKE 'sim:%'").rowcount
        if "shadow_trades" in t:
            n["rows"] += conn.execute("UPDATE shadow_trades SET portfolio_id = CASE WHEN mode = 'SHADOW_LEARNER' "
                                      "THEN 'PAPER_SHADOW_LEARNER/IDX_SPREADS' ELSE 'PAPER_10L/IDX_SPREADS' END "
                                      "WHERE portfolio_id IS NULL").rowcount
        conn.execute("INSERT INTO account_events (ts, event_type, detail) VALUES (?, 'm1_bucket_migration', ?)",
                     (datetime.now().replace(microsecond=0).isoformat(),
                      json.dumps({"counts": n, "buckets": [b["portfolio_id"] for b in p["buckets"]],
                                  "parity": p["parity"]})))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="M1/M4 multi-bucket ledger migration")
    ap.add_argument("--db", default=str(ROOT / "data" / "brain_map.db"))
    ap.add_argument("--apply", action="store_true", help="Apply migration to database (default: dry-run)")
    ap.add_argument("--legacy-m1", action="store_true", help="Seed only the 2 legacy M1 buckets instead of all 5")
    a = ap.parse_args(argv)
    if a.apply:
        from src import portfolio_manager as pm
        conn = sqlite3.connect(a.db)
        pm.ensure_accounts_schema(conn)
    else:
        conn = sqlite3.connect(f"file:{Path(a.db).resolve()}?mode=ro", uri=True)
    p = plan(conn, five_buckets=not a.legacy_m1)
    for b in p["buckets"]:
        print(json.dumps(b))
    print("backfill:", json.dumps(p["backfill"]))
    for c in p["parity"]:
        print(("PARITY ok  " if c["ok"] else "PARITY FAIL") + f" {c['check']}: buckets {c['buckets']} vs account {c['account']}")
    if not p["ok"]:
        print("ABORT: parity failed — nothing written")
        return 2
    if not a.apply:
        print("dry-run: nothing written (add --apply)")
        return 0
    print("applied:", json.dumps(apply(conn, p)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
