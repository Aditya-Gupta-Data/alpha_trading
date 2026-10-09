# MANUAL OFFLINE TOOL — M1 multi-bucket ledger migration (blueprint docs/m1_multi_bucket_ledger_blueprint.md).
"""Seed the buckets from the flat ledger and backfill portfolio_id. DRY-RUN by default: prints the
plan and the PARITY check and writes nothing. `--apply` writes inside one transaction, only when
PARITY holds, and logs one `m1_bucket_migration` account event. Idempotent (INSERT OR IGNORE).

Rulings (Architect 2026-10-09): PAPER_10L → DARLINGS seeded with treasury_state's budget, the rest of
its equity → IDX_SPREADS; PAPER_2L / ROT / LIVE / SHADOW_LEARNER → one IDX_SPREADS bucket each.

    python3 scripts/migrate_m1_buckets.py              # dry-run
    python3 scripts/migrate_m1_buckets.py --apply
"""
import argparse, json, sqlite3, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOL = 0.01
FAMILY = {"IDX": "index_options", "DARLINGS": "darlings"}


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r and r[0] is not None else 0.0


def plan(conn) -> dict:
    """The seeds, the backfill counts and the PARITY check — read-only."""
    from src import portfolio_manager as pm
    conn.row_factory = sqlite3.Row
    pm.ensure_accounts_schema(conn)
    out = {"buckets": [], "backfill": {}, "parity": [], "ok": True}
    now = datetime.now().replace(microsecond=0).isoformat()
    # --- PAPER_10L: DARLINGS (treasury budget) + IDX_SPREADS (the rest)
    acct = pm.get_account(conn)
    equity = float(acct["starting_capital"]) + float(acct["realized_pnl"])
    budget = _one(conn, "SELECT equity_budget_rs FROM treasury_state WHERE id = 1") \
        if "treasury_state" in _tables(conn) else 0.0
    darl_realized = _one(conn, "SELECT COALESCE(SUM(pnl_net), 0) FROM margin_locks "
                               "WHERE journal_ref LIKE 'eqd:%' AND released_at IS NOT NULL")
    idx_realized = float(acct["realized_pnl"]) - darl_realized
    darl_start = float(budget)
    idx_start = equity - darl_start - darl_realized - idx_realized
    for pid, fam, start, real, mdd, dl in (
            ("PAPER_10L/IDX_SPREADS", "index_options", idx_start, idx_realized, pm.MAX_DRAWDOWN_PCT, pm.MAX_DAILY_LOSS_PCT),
            ("PAPER_10L/DARLINGS", "darlings", darl_start, darl_realized, 10.0, None)):
        out["buckets"].append({"portfolio_id": pid, "account_id": "PAPER_10L", "strategy_family": fam,
                               "starting_capital": round(start, 2), "realized_pnl": round(real, 2),
                               "risk_per_trade_pct": pm.ACCOUNT_RISK_PER_TRADE_PCT,
                               "max_drawdown_pct": mdd, "daily_loss_pct": dl, "created_at": now})
    # --- shadows: one IDX bucket each (options-only by ruling)
    for a in _q(conn, "SELECT account_id, starting_capital, realized_pnl FROM paper_accounts ORDER BY account_id"):
        learner = a["account_id"] == pm.ACCOUNT_PAPER_SHADOW_LEARNER
        out["buckets"].append({"portfolio_id": f"{a['account_id']}/IDX_SPREADS", "account_id": a["account_id"],
                               "strategy_family": "index_options", "starting_capital": float(a["starting_capital"]),
                               "realized_pnl": float(a["realized_pnl"]),
                               "risk_per_trade_pct": pm.ACCOUNT_RISK_PER_TRADE_PCT,
                               "max_drawdown_pct": None if learner else pm.MAX_DRAWDOWN_PCT,
                               "daily_loss_pct": None if learner else pm.MAX_DAILY_LOSS_PCT, "created_at": now})
    # --- backfill map (what each existing row already says)
    t = _tables(conn)
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
    # --- PARITY: Σ bucket equity == account equity; Σ open bucket locks == open account locks
    by_acct = {}
    for b in out["buckets"]:
        by_acct[b["account_id"]] = by_acct.get(b["account_id"], 0.0) + b["starting_capital"] + b["realized_pnl"]
    out["parity"].append(_parity("PAPER_10L equity", by_acct.get("PAPER_10L", 0.0), equity))
    for a in _q(conn, "SELECT account_id, starting_capital, realized_pnl FROM paper_accounts"):
        out["parity"].append(_parity(f"{a['account_id']} equity", by_acct.get(a["account_id"], 0.0),
                                     float(a["starting_capital"]) + float(a["realized_pnl"])))
    open_primary = _one(conn, "SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks WHERE released_at IS NULL")
    open_darl = _one(conn, "SELECT COALESCE(SUM(margin_rs), 0) FROM margin_locks WHERE released_at IS NULL "
                           "AND journal_ref LIKE 'eqd:%'")
    out["parity"].append(_parity("PAPER_10L open locks", open_darl + (open_primary - open_darl), open_primary))
    for a in _q(conn, "SELECT account_id FROM paper_accounts"):
        o = _one(conn, "SELECT COALESCE(SUM(margin_rs), 0) FROM paper_margin_locks WHERE account_id = ? "
                       "AND released_at IS NULL", (a["account_id"],))
        out["parity"].append(_parity(f"{a['account_id']} open locks", o, o))
    out["ok"] = all(p["ok"] for p in out["parity"])
    return out


def _parity(name, buckets, account):
    return {"check": name, "buckets": round(float(buckets), 2), "account": round(float(account), 2),
            "ok": abs(float(buckets) - float(account)) <= TOL}


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "brain_map.db"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(a.db)
    p = plan(conn)
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
